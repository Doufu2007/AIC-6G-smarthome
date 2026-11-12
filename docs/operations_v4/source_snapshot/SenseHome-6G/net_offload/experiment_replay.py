"""C-policy trace replay with explicitly modeled (not wall-clock) service costs."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import statistics
import sys
import time
from datetime import datetime
from collections import Counter
from pathlib import Path
from typing import Any

from experiment_inputs import (ARMS, HERE, canonical_hash, load_json, load_profiles,
                               make_trace, transfer_ms, validate_trace, write_json)
from experiment_stats import aggregate
from resource_policy import ResourcePolicyClient

DASHBOARD = HERE.parents[1] / "openvela_smart_home/dashboard"
sys.path.insert(0, str(DASHBOARD))
from device_actions import execute_device_actions  # noqa: E402


def contract_action(task_class: str) -> list[dict[str, Any]]:
    if task_class == "complex":
        return [{"name": "light_control", "params": {
            "location": "客厅", "action": "set", "brightness": 60}}]
    if task_class == "simple":
        return [{"name": "read_temperature", "params": {"location": "客厅"}}]
    return []


def isolated_state() -> dict[str, Any]:
    return {"lights": {"客厅": {"is_on": False, "brightness": 0}},
            "temperature": {"客厅": {"temp": 26, "humidity": 50}},
            "curtains": {"客厅": {"is_open": False, "position": 0}}}


def verify_action_contract(task_class: str) -> tuple[bool, list[dict[str, Any]]]:
    if task_class == "safety":
        # Local, read-only status. No actuator is present or authorized.
        return True, []
    state = isolated_state()
    actions = contract_action(task_class)
    executed = execute_device_actions(state, actions)
    correct = bool(executed["semantic_complete"])
    if task_class == "complex":
        correct = correct and state["lights"]["客厅"]["brightness"] == 60
    return correct, executed["actions"]


def modeled_result(sample: dict[str, Any], profile: dict[str, Any], backend: str,
                   config: dict[str, Any]) -> dict[str, Any]:
    task = profile["task_class"]
    costs = {"observation_ms": config["observation_lookup_ms"] if task == "complex" else 0,
             "policy_ms": config["policy_model_ms"], "network_ms": 0,
             "queue_ms": 0, "model_ms": 0, "execution_ms": 0,
             "fallback_ms": 0}
    error = None
    final = backend
    complete = False
    fallback = False
    actions = []
    if backend == "local-rules":
        costs["model_ms"] = config["rule_model_ms"]
        complete = task != "complex"
        error = None if complete else "UNRESOLVED_NO_NLU"
    else:
        endpoint, link_name = ("local", "lan") if backend == "local-model" else ("cloud", "wan")
        state = sample["compute"][endpoint]
        link = sample["network"][link_name]
        outcome = sample["outcomes"][link_name + "-infer"]
        if not sample["network"]["lan"]["reachable"] or outcome["refused"]:
            # Conservatively use a deadline timeout for injected blackouts.
            costs["network_ms"] = config["timeout_ms"]
            error = "LINK_UNAVAILABLE"
        elif state["model_ready"] is False:
            costs["network_ms"] = outcome["rtt_ms"]
            error = "MODEL_UNAVAILABLE"
        elif state["free_mem_mb"] is not None and state["free_mem_mb"] < config["free_mem_min_mb"]:
            costs["network_ms"] = outcome["rtt_ms"]
            error = "MODEL_OUT_OF_MEMORY"
        else:
            costs["network_ms"] = outcome["rtt_ms"] + transfer_ms(profile, link)
            costs["queue_ms"] = state["queue_ms"] or 0
            costs["model_ms"] = config["local_infer_ms" if endpoint == "local" else "cloud_infer_ms"]
            if sum(costs.values()) > config["timeout_ms"]:
                # Deadline cancellation: service time before fallback is bounded.
                costs["network_ms"] = min(costs["network_ms"], config["timeout_ms"])
                remaining = config["timeout_ms"] - costs["network_ms"]
                costs["queue_ms"] = min(costs["queue_ms"], remaining)
                costs["model_ms"] = min(costs["model_ms"], remaining - costs["queue_ms"])
                error = "MODEL_TIMEOUT"
            else:
                complete = True
        if error:
            fallback = True
            final = "local-rules"
            costs["fallback_ms"] = config["rule_model_ms"]
            complete = task != "complex"
    if complete:
        complete, actions = verify_action_contract(task)
        costs["execution_ms"] = config["execution_model_ms"]
    return {
        "backend": final, "selected_backend": backend,
        "contract_complete": complete, "actions": actions,
        "whitelist_status": "validated" if complete else "no_action",
        "fallback_attempted": fallback, "error": error,
        "latency_components_ms": costs, "total_user_ms": sum(costs.values()),
    }


def replay(trace: dict[str, Any], profiles: dict[str, Any], config: dict[str, Any],
           cli: str, arms: tuple[str, ...] = ARMS) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    validate_trace(trace, profiles)
    rows, switches = [], []
    for arm in arms:
        previous = None
        with_client = ResourcePolicyClient(cli, config, arm)
        try:
            for sample in trace["samples"]:
                profile = profiles[sample["service"]]
                start = time.perf_counter()
                backend, reason, log = with_client.select(sample, profile)
                measured_policy_ms = (time.perf_counter() - start) * 1000
                result = modeled_result(sample, profile, backend, config)
                row = {
                    "schema_version": "sensehome.replay-row.v1",
                    "mode": "modeled_service_time_with_measured_C_policy",
                    "trace_id": trace["trace_id"], "trace_index": sample["index"],
                    "seed": trace["seed"], "scenario": sample["scenario"],
                    "service": sample["service"], "arm": arm,
                    "task_class": profile["task_class"], "command": profile["command"],
                    "input_hash": canonical_hash(sample),
                    "policy_config_hash": canonical_hash(config),
                    "network": sample["network"], "compute": sample["compute"],
                    "compute_source": sample["compute_source"],
                    "outcomes": sample["outcomes"], "event": sample["event"],
                    "deadline_ms": profile["deadline_ms"],
                    "reason": reason, "policy_log": log,
                    # Measured host/IPC cost is separately named, not silently
                    # mixed into a deterministic modeled latency result.
                    "measured_policy_roundtrip_ms": measured_policy_ms,
                    **result,
                }
                rows.append(row)
                if profile["task_class"] == "complex":
                    if previous and previous["selected_backend"] != backend:
                        switches.append({
                            "seed": trace["seed"], "trace_id": trace["trace_id"],
                            "arm": arm, "from_index": previous["trace_index"],
                            "to_index": sample["index"],
                            "from_backend": previous["selected_backend"], "to_backend": backend,
                            "reason": reason, "policy_log": log,
                            "scenario": sample["scenario"], "event": sample["event"],
                            "network": sample["network"], "compute": sample["compute"],
                        })
                    previous = row
        finally:
            with_client.close()
    return rows, switches


def stable_projection(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key != "measured_policy_roundtrip_ms"}


def save_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n")


def source_manifest(cli: str) -> dict[str, Any]:
    paths = list(HERE.glob("*.py")) + list(HERE.glob("*.c")) + list(HERE.glob("*.h"))
    paths += list((HERE / "experiments").glob("*.json"))
    paths += list((HERE / "tests").glob("*.py"))
    paths += list(DASHBOARD.glob("*.py")) + list(DASHBOARD.glob("*.html"))
    paths += list((DASHBOARD / "tests").glob("*.py"))
    paths += [Path(cli)]
    paths += list((HERE.parents[1] / "docs/deployment").glob("*.py"))
    return {path.relative_to(HERE).as_posix() if path.is_relative_to(HERE) else str(path):
            hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(set(paths))}


def seal_artifacts(directory: Path) -> None:
    write_json(directory / "checksums.json", {
        "algorithm": "sha256",
        "files": {path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in sorted(directory.rglob("*"))
                  if path.is_file() and path.name not in {"checksums.json", "audit.json"}},
    })


def render_report(data: dict[str, Any]) -> str:
    lines = ["# Network/Compute Experiment Report", "",
             "This is synthetic design-proxy replay, not physical 6G, field traffic, or model inference.",
             "Service latencies below are explicitly modeled. C policy IPC runtime is recorded separately.",
             "All routes retain local safety isolation. Failed or unresolved requests violate deadlines.",
             "", "## Overall Fixed Planner Contract", "",
             "| Service | Arm | n | p50 ms | p95 ms | p99 ms | Contract % | Deadline % |",
             "|---|---|---:|---:|---:|---:|---:|---:|"]
    for row in data["statistics"]["overall"]:
        lines.append("| {service} | {arm} | {n} | {p50_ms:.2f} | {p95_ms:.2f} | "
                     "{p99_ms:.2f} | {complete:.2f} | {met:.2f} |".format(
                         **row, complete=100 * row["contract_completion_rate"],
                         met=100 * row["deadline_met_rate"]))
    lines += ["", "## Switch Evidence", "",
              f"Complex-task route changes: {dict(Counter(s['arm'] for s in data['switches']))}",
              "", "See switches.json for exact before/after indices, reasons and endpoint observations.",
              "", "## Statistical Limits", "",
              "- Quantiles: linear/type-7 over all requests, including failures.",
              "- Completion is checked against a fixed authorized device contract, not NLU quality.",
              "- Wilson intervals are descriptive; trace events are correlated.",
              "- Paired effects use identical request hashes and seed-cluster bootstrap.",
              "- A few synthetic seeds cannot support population-level reliability claims.",
              "- Service reliability targets are design goals, not achieved radio guarantees.",
              "- Model service times, memory and trace queue states are declared injected parameters.",
              "- Concurrency results are a separate real-thread stub test, not these modeled values.",
              ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cli", default=str(HERE / ("policy_cli.exe" if sys.platform == "win32" else "policy_cli")))
    parser.add_argument("--seeds", default="20261105,20261106,20261107")
    parser.add_argument("--per-phase", type=int, default=200)
    parser.add_argument("--trace", type=Path, action="append")
    parser.add_argument("--profiles", type=Path, default=HERE / "experiments/service_profiles.json")
    parser.add_argument("--config", type=Path, default=HERE / "experiments/policy_config.json")
    args = parser.parse_args()
    if args.out.exists() and any(args.out.iterdir()):
        parser.error("output must be empty; raw evidence is never overwritten")
    args.out.mkdir(parents=True, exist_ok=True)
    profiles = load_profiles(args.profiles)
    config = load_json(args.config)
    traces = [load_json(path) for path in args.trace] if args.trace else [
        make_trace(int(seed), args.per_phase) for seed in args.seeds.split(",")]
    if len({trace["seed"] for trace in traces}) != len(traces):
        parser.error("duplicate seeds invalidate pairing")
    rows, switches = [], []
    started = time.perf_counter()
    for trace in traces:
        validate_trace(trace, profiles["profiles"])
        write_json(args.out / f"trace_{trace['seed']}.json", trace)
        current, changes = replay(trace, profiles["profiles"], config, args.cli)
        rows.extend(current)
        switches.extend(changes)
        print(f"seed={trace['seed']} requests={len(current)} switches={len(changes)}", flush=True)
    save_rows(args.out / "raw_requests.jsonl", rows)
    write_json(args.out / "switches.json", switches)
    stats = aggregate(rows)
    manifest = {
        "schema_version": "sensehome.experiment.v1", "date": datetime.now().date().isoformat(),
        "python": sys.version, "platform": platform.platform(), "command": sys.argv,
        "elapsed_wall_s": time.perf_counter() - started,
        "evidence_type": "synthetic_application_trace_and_modeled_service_cost",
        "sources": source_manifest(args.cli), "config": config, "profiles": profiles,
        "trace_ids": [trace["trace_id"] for trace in traces],
        "arms": list(ARMS), "raw_count": len(rows),
        "deterministic_rows_hash": canonical_hash([stable_projection(row) for row in rows]),
        "measured_policy_roundtrip_median_ms": statistics.median(
            row["measured_policy_roundtrip_ms"] for row in rows),
    }
    write_json(args.out / "summary.json", {"manifest": manifest, "statistics": stats})
    (args.out / "report.md").write_text(render_report(
        {"statistics": stats, "switches": switches}), encoding="utf-8")
    seal_artifacts(args.out)
    print(f"wrote {len(rows)} raw rows to {args.out}", flush=True)


if __name__ == "__main__":
    main()
