"""Actual default HTTP command entry -> dual C policy -> HTTP fixtures -> whitelist.

No AI/GPU/radio measurements: endpoints serve fixed contract plans with declared
application-delay/failure injection. Client wall times include event persistence.
"""
from __future__ import annotations

import argparse
import copy
import http.client
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import platform
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import patch

from experiment_inputs import (ARMS, HERE, canonical_hash, load_json, load_profiles,
                               make_trace, write_json)
from experiment_concurrency import QueuedStub
from experiment_replay import DASHBOARD, save_rows, seal_artifacts, source_manifest
from experiment_stats import summary

sys.path.insert(0, str(DASHBOARD))
import bridge_server as bridge

COMMANDS = {
    "interactive_inference": "请把客厅调整为阅读模式",
    "safety_control": "查询安防状态",
    "sensor_batch": "读取客厅温度",
}


class FixtureServer(ThreadingHTTPServer):
    request_queue_size = 128


class EndpointFixture:
    def __init__(self, endpoint: str):
        self.endpoint = endpoint
        self.lock = threading.Lock()
        self.sample = make_trace(0, 5)["samples"][0]
        self.live = False
        self.service = QueuedStub(endpoint, 2 if endpoint == "local" else 8,
                                  12 if endpoint == "local" else 35)
        self.records = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/health":
                    self.reply(404, {"error": "NOT_FOUND"})
                    return
                sample, live = owner.snapshot()
                link = sample["network"]["lan" if owner.endpoint == "local" else "wan"]
                outcome = sample["outcomes"][("lan" if owner.endpoint == "local" else "wan") + "-probe"]
                if not link["reachable"] or outcome["refused"]:
                    self.reply(503, {"ready": False, "source": "injected_connection_failure_fixture"})
                    return
                time.sleep(outcome["rtt_ms"] / 1000)
                state = owner.service.snapshot() if live else dict(sample["compute"][owner.endpoint])
                state["ready"] = state["model_ready"]
                self.reply(200, {**state, "source": "live_bounded_stub" if live else "injected_compute_fixture"})

            def do_POST(self):
                started = time.perf_counter()
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                request = json.loads(raw)
                sample, live = owner.snapshot()
                name = "lan" if owner.endpoint == "local" else "wan"
                outcome = sample["outcomes"][name + "-infer"]
                state = sample["compute"][owner.endpoint]
                error, status, result = None, 200, {}
                network_ms = 0.0
                if not sample["network"]["lan"]["reachable"] or outcome["refused"]:
                    error, status = "INJECTED_CONNECTION_FAILURE", 503
                elif state["model_ready"] is False:
                    error, status = "INJECTED_MODEL_DOWN", 503
                elif state["free_mem_mb"] is not None and state["free_mem_mb"] < 256:
                    error, status = "INJECTED_LOW_MEMORY", 503
                else:
                    begin = time.perf_counter()
                    time.sleep(outcome["rtt_ms"] / 1000)
                    network_ms = (time.perf_counter() - begin) * 1000
                    if not live:
                        time.sleep((state["queue_ms"] or 0) / 1000)
                    plan = owner.service.infer(request["command"])
                    if not plan.ok:
                        error, status = plan.error, 503
                    else:
                        result = dict(plan.response)
                        result.update(
                            actions=[{"name": "light_control", "params": {
                                "location": "客厅", "brightness": 75}}],
                            semantic_complete=False,
                            source="http-contract-fixture-not-AI",
                            model_ms=plan.model_ms,
                            measured_fixture_network_delay_ms=network_ms,
                        )
                if error:
                    result.update(error=error, actions=[], semantic_complete=False)
                result["experiment_request_key"] = request.get("context", {}).get("experiment_request_key")
                encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
                # Explicit application serialization delay, not measured radio throughput.
                rate = sample["network"][name]["bandwidth_kbps"]
                body_delay_ms = (len(raw) + len(encoded)) * 8 / rate
                time.sleep(body_delay_ms / 1000)
                with owner.lock:
                    owner.records.append({
                        "endpoint": owner.endpoint, "trace_index": sample["index"],
                        "request_key": result["experiment_request_key"], "status": status,
                        "source": "live_stub_queue" if live else "injected_trace",
                        "measured_handler_ms": (time.perf_counter() - started) * 1000,
                        "injected_serialization_ms": body_delay_ms,
                        "result": result,
                    })
                self.reply(status, result)

            def reply(self, status, value):
                body = json.dumps(value, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_):
                pass

        self.server = FixtureServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def configure(self, sample, live=False):
        with self.lock:
            self.sample, self.live = copy.deepcopy(sample), live
            self.service.ready = bool(sample["compute"][self.endpoint]["model_ready"])

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.sample), self.live

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


def http_call(port: int, method: str, path: str, payload=None) -> tuple[int, Any, float]:
    start = time.perf_counter()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    try:
        conn.request(method, path, body=body, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        data = json.loads(response.read())
        return response.status, data, (time.perf_counter() - start) * 1000
    finally:
        conn.close()


def confirmed(service: str, response: dict[str, Any]) -> bool:
    if not response.get("semantic_complete") or not response.get("interface_ok"):
        return False
    if service == "safety_control":
        return response.get("backend") == "local-rules" and not response.get("actions")
    if service == "interactive_inference":
        return any(action.get("name") == "light_control"
                   and action.get("params", {}).get("location") == "客厅"
                   and action.get("params", {}).get("brightness") == 75
                   for action in response.get("actions", []))
    return "温度" in str(response.get("observation", ""))


@contextmanager
def default_gateway(directory: Path, cli: str, config: dict, arm: str):
    """Use _init_gateway unchanged, with only documented operator env overrides."""
    directory.mkdir(parents=True, exist_ok=True)
    config_path = directory / "effective_config.json"
    write_json(config_path, config)
    endpoints = {name: EndpointFixture(name) for name in ("local", "cloud")}
    original_file = bridge.STATE_FILE
    bridge.STATE_FILE = str(directory / "device_state.json")
    bridge.save_state(bridge.default_state())
    env = {
        "SENSEHOME_CLOUD_URL": endpoints["cloud"].url,
        "SENSEHOME_LOCAL_URL": endpoints["local"].url,
        "SENSEHOME_POLICY_CLI": cli, "SENSEHOME_POLICY_CONFIG": str(config_path),
        "SENSEHOME_POLICY_ARM": arm, "SENSEHOME_INFER_TIMEOUT_S": "2",
        "SENSEHOME_MONITOR_INTERVAL_S": ".02",
        "SENSEHOME_EVENT_LOG": str(directory / "events.jsonl"),
    }
    server, thread = None, None
    try:
        with patch.dict(os.environ, env):
            bridge._init_gateway(start_monitor=False)
        if bridge.gateway_init_error:
            raise RuntimeError(bridge.gateway_init_error)
        server = FixtureServer(("127.0.0.1", 0), bridge.BridgeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        yield server.server_port, endpoints
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if thread:
            thread.join(3)
        bridge.close_gateway()
        bridge.STATE_FILE = original_file
        for endpoint in endpoints.values():
            endpoint.close()
        write_json(directory / "backend_records.json", {
            name: endpoint.records for name, endpoint in endpoints.items()})


def request_row(port: int, sample: dict, profile: dict, arm: str,
                seed: int, workload: str, concurrency: int = 1, repetition: int = 0) -> dict:
    service = sample["service"]
    key = f"{workload}:{seed}:{sample['scenario']}:{sample['index']}:{concurrency}:{repetition}:{arm}"
    request = {"command": COMMANDS[service], "context": {"experiment_request_key": key}}
    started = time.perf_counter()
    try:
        status, response, elapsed = http_call(port, "POST", "/api/command", request)
        event = response.get("gateway_event", {})
        complete = status == 200 and confirmed(service, response)
        error = event.get("error")
        if not complete and not error:
            error = "CONTRACT_NOT_COMPLETED"
    except (OSError, ValueError, http.client.HTTPException) as exc:
        elapsed = (time.perf_counter() - started) * 1000
        status, response, event, complete, error = None, {}, {}, False, type(exc).__name__
    return {
        "schema_version": "sensehome.http-row.v1", "mode": "measured_default_HTTP_entry_contract_fixtures",
        "workload": workload, "seed": seed, "trace_index": sample["index"],
        "scenario": sample["scenario"], "service": service, "arm": arm,
        "concurrency": concurrency, "repetition": repetition,
        "request_key": key, "configured_input_hash": canonical_hash(sample),
        "input": sample, "request": request, "response_status": status,
        "response": response, "total_user_ms": elapsed,
        "contract_complete": complete, "deadline_ms": profile["deadline_ms"],
        "backend": event.get("backend", "no-response"), "reason": event.get("reason", "NO_EVENT"),
        "fallback_attempted": event.get("fallback_attempted", False), "error": error,
    }


def summarize_http(rows):
    keys = ("workload", "concurrency", "scenario", "service", "arm")
    groups = {}
    for row in rows:
        groups.setdefault(tuple(row[key] for key in keys), []).append(row)
    return [{**dict(zip(keys, key)), **summary(value)} for key, value in sorted(groups.items())]


def run_http(out: Path, cli: str, per_phase: int = 10, requests: int = 128,
             repetitions: int = 3, levels=(1, 8, 32, 64),
             seeds=(20261116, 20261117, 20261118), arms=ARMS):
    if out.exists() and any(out.iterdir()):
        raise ValueError("OUTPUT_MUST_BE_EMPTY")
    out.mkdir(parents=True, exist_ok=True)
    config = load_json(HERE / "experiments/policy_config.json")
    config.update(local_infer_ms=12, cloud_infer_ms=35)
    profiles = load_profiles()["profiles"]
    rows, switches, traces, queue_cells = [], [], [], []
    # Hide access-log noise only; request handler and gateway initialization are unchanged.
    with patch.object(bridge.BridgeHandler, "log_message", lambda *args: None):
        for seed_index, seed in enumerate(seeds):
            trace = make_trace(seed, per_phase)
            traces.append(trace)
            write_json(out / f"trace_{seed}.json", trace)
            ordered_arms = list(arms[seed_index % len(arms):] + arms[:seed_index % len(arms)])
            for arm in ordered_arms:
                previous = None
                directory = out / "matrix" / f"{seed}_{arm}"
                with default_gateway(directory, cli, config, arm) as (port, endpoints):
                    for sample in trace["samples"]:
                        for endpoint in endpoints.values():
                            endpoint.configure(sample)
                        bridge.backend_monitor.refresh_once()
                        row = request_row(port, sample, profiles[sample["service"]], arm, seed, "matrix")
                        rows.append(row)
                        if profiles[sample["service"]]["task_class"] == "complex":
                            selected = row["response"].get("selected_backend", row["backend"])
                            if previous and previous["selected_backend"] != selected:
                                switches.append({
                                    "seed": seed, "arm": arm, "from_index": previous["trace_index"],
                                    "to_index": sample["index"], "from_backend": previous["selected_backend"],
                                    "to_backend": selected, "reason": row["reason"],
                                    "request_key": row["request_key"],
                                    "observation": row["response"].get("resource_observation"),
                                })
                            previous = {"selected_backend": selected, "trace_index": sample["index"]}
                print(f"HTTP matrix seed={seed} arm={arm} rows={len(trace['samples'])}", flush=True)
        # Explicit stub capacity configuration, retained in every cell artifact.
        stress_config = {**config, "inflight_max": 64}
        for repetition in range(repetitions):
            for level in levels:
                for scenario in ("healthy", "cloud_down"):
                    for arm in (("joint", "network-only") if repetition % 2 == 0 else ("network-only", "joint")):
                        directory = out / "stress" / f"{repetition}_{level}_{scenario}_{arm}"
                        with default_gateway(directory, cli, stress_config, arm) as (port, endpoints):
                            base = make_trace(20261119, 5)["samples"][0]
                            base["scenario"] = scenario
                            if scenario == "cloud_down":
                                base["compute"]["cloud"]["model_ready"] = False
                            for endpoint in endpoints.values():
                                endpoint.configure(base, live=True)
                            bridge.backend_monitor.refresh_once()
                            bridge.backend_monitor.start()
                            barrier = threading.Barrier(level)

                            def request(index):
                                if index < level:
                                    barrier.wait(30)
                                sample = copy.deepcopy(base)
                                service = ("interactive_inference", "interactive_inference", "sensor_batch",
                                           "interactive_inference", "safety_control")[index % 5]
                                sample.update(index=index, service=service)
                                return request_row(port, sample, profiles[service], arm, 20261119,
                                                   "stress", level, repetition)
                            start = time.perf_counter()
                            with ThreadPoolExecutor(max_workers=level) as pool:
                                current = list(pool.map(request, range(requests)))
                            elapsed = time.perf_counter() - start
                            rows.extend(current)
                            queue_cells.append({
                                "concurrency": level, "scenario": scenario, "arm": arm,
                                "repetition": repetition, "requests": requests,
                                "elapsed_s": elapsed, "throughput_rps": requests / elapsed,
                                "config": stress_config,
                                "queues": {name: {
                                    "active": endpoint.service.active, "waiting": endpoint.service.waiting,
                                    "peak": endpoint.service.peak, "slots": endpoint.service.slots,
                                    "fifo": endpoint.service.admission_order == sorted(endpoint.service.admission_order),
                                } for name, endpoint in endpoints.items()},
                            })
                        print(f"HTTP stress c={level} {scenario} rep={repetition} {arm}", flush=True)
    stress_trace = make_trace(20261119, 5)
    write_json(out / "trace_stress_20261119.json", stress_trace)
    save_rows(out / "raw_requests.jsonl", rows)
    write_json(out / "switches.json", switches)
    write_json(out / "summary.json", {
        "schema_version": "sensehome.http-experiment.v1",
        "date": datetime.now(timezone.utc).date().isoformat(),
        "raw_count": len(rows), "trace_ids": [trace["trace_id"] for trace in traces] + [stress_trace["trace_id"]],
        "sources": source_manifest(cli), "command": sys.argv, "python": sys.version,
        "platform": platform.platform(), "config": config, "profiles": profiles,
        "matrix_arms": list(arms), "seeds": list(seeds),
        "levels": list(levels), "repetitions": repetitions, "requests_per_cell": requests,
        "runtime_timeouts": {"health_socket_s": .5, "inference_socket_s": 2, "client_socket_s": 8,
                             "not_a_strict_total_deadline": True},
        "cells": summarize_http(rows), "queue_cells": queue_cells,
        "scope": {
            "latency": "client HTTP wall clock, includes default server/event persistence/state whitelist",
            "models": "fixed authorized fixture plans and bounded sleep services, NOT AI accuracy",
            "network": "application delay/503/body-rate injection, NOT packet or physical 6G trace",
            "pairing": "same configured workloads, measured health differs; no paired causal HTTP effects",
            "load": "finite-client closed loop; generator scheduling excluded, no coordinated-omission correction",
            "memory": "matrix memory injected, live stress memory unknown",
            "defaults": "real _init_gateway and BridgeHandler, operator config overrides recorded",
        },
    })
    seal_artifacts(out)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cli", default=str(HERE / ("policy_cli.exe" if os.name == "nt" else "policy_cli")))
    parser.add_argument("--per-phase", type=int, default=10)
    parser.add_argument("--requests", type=int, default=128)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--levels", default="1,8,32,64")
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    levels = tuple(int(value) for value in args.levels.split(","))
    if (not levels or min(levels) < 1 or max(levels) > 64 or
            args.requests < max(levels) or args.repetitions < 1 or args.per_phase < 5):
        parser.error("invalid sample/concurrency arguments")
    run_http(args.out.resolve(), args.cli, 5 if args.quick else args.per_phase,
             16 if args.quick else args.requests, 1 if args.quick else args.repetitions,
             (1, 8) if args.quick else levels, (20261116,) if args.quick else (20261116, 20261117, 20261118),
             ("joint", "network-only") if args.quick else ARMS)


if __name__ == "__main__":
    main()
