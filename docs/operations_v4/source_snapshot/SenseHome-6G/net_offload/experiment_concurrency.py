"""Actual thread/queue/IPC/whitelist timings using bounded synthetic model services.

The sleep-based services are contract stubs, not AI models or a network emulator.
Each caller has isolated simulated devices; policy and endpoint queues are shared.
"""
from __future__ import annotations

import argparse
import copy
import math
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from experiment_inputs import HERE, load_json, load_profiles, make_trace, write_json
from experiment_replay import (contract_action, isolated_state, save_rows,
                               seal_artifacts, source_manifest, verify_action_contract)
from experiment_stats import aggregate
from offload_gateway import (BackendResult, LinkSnapshot, OffloadGateway,
                             SubprocessPolicyClient)
from resource_policy import ResourcePolicyClient


class QueuedStub:
    def __init__(self, name: str, slots: int, service_ms: int,
                 ready: bool = True, max_waiters: int = 64):
        self.name, self.slots, self.service_ms = name, slots, service_ms
        self.ready, self.max_waiters = ready, max_waiters
        self.active = self.waiting = self.peak = 0
        self.condition = threading.Condition()
        self.calls = 0
        self._tickets = deque()
        self._next_ticket = 0
        self.admission_order = []

    def snapshot(self) -> dict[str, Any]:
        with self.condition:
            return {
                "model_ready": self.ready,
                "queue_ms": math.ceil((self.active + self.waiting) / self.slots) * self.service_ms,
                "inflight": self.active + self.waiting,
                # Host RAM is not GPU memory; do not invent free VRAM.
                "free_mem_mb": None,
                "source": "live_stub_queue_and_readiness",
                "field_sources": {"model_ready": "live_stub_ready_flag",
                                  "inflight": "locked_active_plus_waiting_counter",
                                  "queue_ms": "counter_based_service_time_estimate",
                                  "free_mem_mb": "unknown"},
            }

    def infer(self, command: str, context: dict[str, Any] | None = None) -> BackendResult:
        del command, context
        started = time.perf_counter()
        with self.condition:
            self.calls += 1
            if not self.ready:
                return BackendResult(False, False, {}, [], error="MODEL_UNAVAILABLE", source=self.name)
            if self.waiting >= self.max_waiters and self.active >= self.slots:
                return BackendResult(False, False, {}, [], error="QUEUE_FULL", source=self.name)
            self.waiting += 1
            ticket = self._next_ticket
            self._next_ticket += 1
            self._tickets.append(ticket)
            try:
                # FIFO admission prevents new callers from repeatedly barging
                # ahead of a notified waiter under closed-loop load.
                while self.active >= self.slots or self._tickets[0] != ticket:
                    self.condition.wait(timeout=.1)
                self._tickets.popleft()
                self.admission_order.append(ticket)
                self.active += 1
                self.peak = max(self.peak, self.active)
                self.condition.notify_all()
            finally:
                self.waiting -= 1
        queue_ms = (time.perf_counter() - started) * 1000
        service_start = time.perf_counter()
        try:
            time.sleep(self.service_ms / 1000)
            service_ms = (time.perf_counter() - service_start) * 1000
            return BackendResult(
                True, False,
                {"answer": "fixed planner stub", "measured_queue_ms": queue_ms,
                 "measured_service_ms": service_ms, "injected_service_ms": self.service_ms},
                contract_action("complex"), model_ms=service_ms, source=self.name)
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()


class ContractRules:
    def infer(self, command: str, context: dict[str, Any] | None = None) -> BackendResult:
        del command
        task = context["task_class"]
        complete, actions = verify_action_contract(task) if task != "complex" else (False, [])
        return BackendResult(True, complete, {"answer": "authorized local contract"},
                             actions, source="local-rules",
                             error=None if complete else "UNRESOLVED_NO_NLU")


class RuntimePolicy(ResourcePolicyClient):
    def __init__(self, cli: str, config: dict[str, Any], arm: str,
                 endpoints: dict[str, QueuedStub]):
        super().__init__(cli, config, arm)
        self.endpoints = endpoints
        self.request = threading.local()
        self.observation_sequence = 0

    def decide(self, task_class: str, link: LinkSnapshot) -> tuple[str, str, str]:
        del task_class
        sample = copy.deepcopy(self.request.sample)
        # This snapshot is taken under the gateway's policy transaction lock;
        # submission indices can arrive out of order and are not cache IDs.
        self.observation_sequence += 1
        sample["observation_id"] = self.observation_sequence
        sample["compute"] = {name: endpoint.snapshot() for name, endpoint in self.endpoints.items()}
        sample["compute_source"] = "live_shared_bounded_stub_queues"
        self.request.observation = sample
        self.observe(sample, self.request.profile)
        return SubprocessPolicyClient.decide(self, self.request.profile["task_class"], link)


def run_cell(cli: str, config: dict[str, Any], profiles: dict[str, Any], arm: str,
             concurrency: int, repetition: int, count: int, scenario: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    local = QueuedStub("local-model", slots=2, service_ms=12)
    cloud = QueuedStub("cloud-model", slots=8, service_ms=35, ready=scenario != "cloud_down")
    config = {**config, "local_infer_ms": 12, "cloud_infer_ms": 35}
    policy = RuntimePolicy(cli, config, arm, {"local": local, "cloud": cloud})
    link = LinkSnapshot(wan_ok=True, wan_rtt_ms=25, lan_ok=True, lan_rtt_ms=3)

    def execute(actions, context):
        from device_actions import execute_device_actions
        return execute_device_actions(context["state"], actions)

    gateway = OffloadGateway(policy, {
        "local-model": local, "cloud-model": cloud, "local-rules": ContractRules(),
    }, link_snapshot=link, action_executor=execute)
    base = make_trace(20261105, 5)["samples"][0]
    barrier = threading.Barrier(concurrency)
    cell_start = time.perf_counter()
    cpu_start = time.process_time()

    def request(index: int) -> dict[str, Any]:
        # Only the first wave is synchronized. Subsequent waves are closed-loop:
        # finite clients start the next request when their previous call returns.
        if index < concurrency:
            barrier.wait(timeout=30)
        service = ("interactive_inference", "interactive_inference",
                   "sensor_batch", "interactive_inference", "safety_control")[index % 5]
        profile = profiles[service]
        sample = copy.deepcopy(base)
        sample.update(index=index, service=service, scenario=scenario)
        policy.request.sample = sample
        policy.request.profile = profile
        state = isolated_state()
        begin = time.perf_counter()
        event = gateway.handle(profile["command"], {"task_class": profile["task_class"], "state": state})
        wall = (time.perf_counter() - begin) * 1000
        action_complete = event["semantic_complete"]
        if profile["task_class"] == "complex":
            action_complete = action_complete and state["lights"]["客厅"]["brightness"] == 60
        return {
            "schema_version": "sensehome.concurrent-row.v1",
            "mode": "wall_clock_real_threads_C_IPC_bounded_contract_stubs",
            "seed": repetition, "trace_index": index, "scenario": scenario,
            "concurrency": concurrency, "repetition": repetition, "arm": arm,
            "service": service, "task_class": profile["task_class"],
            "deadline_ms": profile["deadline_ms"], "total_user_ms": wall,
            "contract_complete": bool(action_complete),
            "backend": event["backend"], "reason": event["reason"],
            "error": event["error"], "fallback_attempted": event["fallback_attempted"],
            "observation": policy.request.observation,
            "event": event,
            "latency_scope": "gateway call including classification/policy lock/IPC/queue/stub/whitelist; excludes load-generator scheduling",
        }

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            rows = list(pool.map(request, range(count)))
    finally:
        policy.close()
    elapsed = time.perf_counter() - cell_start
    info = {"arm": arm, "concurrency": concurrency, "repetition": repetition,
            "scenario": scenario, "requests": count, "cell_wall_s": elapsed,
            "requests_per_second": count / elapsed,
            "python_process_cpu_ms": (time.process_time() - cpu_start) * 1000,
            "cpu_scope": "this Python process only; excludes C subprocess and external GPU",
            "effective_policy_config": config,
            "stub_slots": {"local": local.slots, "cloud": cloud.slots},
            "max_waiters_per_endpoint": 64,
            "local_peak_active": local.peak, "cloud_peak_active": cloud.peak,
            "final_active": local.active + cloud.active,
            "final_waiting": local.waiting + cloud.waiting}
    info["fifo_verified"] = all(endpoint.admission_order == sorted(endpoint.admission_order)
                                for endpoint in (local, cloud))
    return rows, info


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--cli", default=str(HERE / ("policy_cli.exe" if sys.platform == "win32" else "policy_cli")))
    parser.add_argument("--levels", default="1,8,32,64")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--requests", type=int, default=128)
    parser.add_argument("--inflight-max", type=int, default=64,
                        help="Explicit bounded-stub calibration, not a real GPU capacity claim")
    args = parser.parse_args()
    levels = [int(value) for value in args.levels.split(",")]
    if (not levels or min(levels) < 1 or max(levels) > 128 or
            args.requests < max(levels) or args.repetitions < 1):
        parser.error("invalid concurrency/count/repetition")
    if args.out.exists() and any(args.out.iterdir()):
        parser.error("output must be empty")
    args.out.mkdir(parents=True, exist_ok=True)
    config = load_json(HERE / "experiments/policy_config.json")
    config["inflight_max"] = args.inflight_max
    profiles = load_profiles()["profiles"]
    rows, cells = [], []
    for repetition in range(args.repetitions):
        for level in levels:
            for scenario in ("healthy", "cloud_down"):
                # Rotate arm order to reduce systematic warm-up/order bias.
                arms = ("joint", "network-only") if repetition % 2 == 0 else ("network-only", "joint")
                for arm in arms:
                    current, info = run_cell(args.cli, config, profiles, arm, level,
                                             repetition, args.requests, scenario)
                    rows.extend(current)
                    cells.append(info)
                    print(f"concurrency={level} rep={repetition} scenario={scenario} arm={arm}", flush=True)
    save_rows(args.out / "raw_requests.jsonl", rows)
    # Cells must not mix concurrency levels in their aggregate identifiers.
    groups = []
    for level in levels:
        stats = aggregate([row for row in rows if row["concurrency"] == level])
        # The wall-clock test has no paired network seeds; don't imply that it does.
        stats["paired_effects"] = []
        groups.append({"concurrency": level, "statistics": stats})
    write_json(args.out / "summary.json", {
        "schema_version": "sensehome.concurrency.v1", "command": sys.argv,
        "platform": __import__("platform").platform(), "python": sys.version,
        "sources": source_manifest(args.cli),
        "evidence_type": "measured_wall_clock_on_bounded_sleep_stubs_and_PC_devices",
        "load_model": "closed-loop finite clients, synchronized first wave; no coordinated-omission correction",
        "limits": ["No real model, GPU, radio, physical actuators or HTTP in this test",
                   "Unknown memory stays null; queue estimates and measured queue delays are distinct",
                   "Runtime arm outcomes are not deterministic; compare distributions, not identical wall times"],
        "raw_count": len(rows), "cells": cells, "groups": groups,
    })
    seal_artifacts(args.out)
    print(f"wrote {len(rows)} measured rows to {args.out}", flush=True)


if __name__ == "__main__":
    main()
