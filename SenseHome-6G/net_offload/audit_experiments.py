"""Independent artifact/hash, matrix, statistical, and optionally C-replay audit."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from experiment_inputs import ARMS, HERE, canonical_hash, load_json, validate_trace, write_json
from experiment_replay import modeled_result, replay, stable_projection
from experiment_stats import aggregate
from offload_gateway import LinkSnapshot


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def read_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def replay_captured_policy(client, event: dict[str, Any]):
    task_class = event["task_class"]
    if task_class == "complex":
        observation = event.get("observation")
        require(isinstance(observation, dict), "MISSING_CAPTURED_OBSERVATION")
        return client.decide_observation(task_class, observation)
    return client.decide(task_class, LinkSnapshot())


def audit(directory: Path, cli: str, rerun: bool = False) -> dict[str, Any]:
    seals = load_json(directory / "checksums.json")
    for name, expected in seals["files"].items():
        require((directory / name).resolve().is_relative_to(directory.resolve()), "CHECKSUM_PATH_ESCAPE")
        require(hashlib.sha256((directory / name).read_bytes()).hexdigest() == expected,
                "ARTIFACT_HASH_MISMATCH:" + name)
    data = load_json(directory / "summary.json")
    rows = read_rows(directory / "raw_requests.jsonl")
    concurrent = data.get("schema_version") == "sensehome.concurrency.v1"
    sensitivity = data.get("schema_version") == "sensehome.sensitivity.v1"
    safety = data.get("schema_version") == "sensehome.safety.v1"
    http_gateway = data.get("schema_version") == "sensehome.http-experiment.v1"
    manifest = data if concurrent or sensitivity or safety or http_gateway else data["manifest"]
    for name, expected in manifest["sources"].items():
        path = Path(name)
        path = path if path.is_absolute() else HERE / path
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            # Historical runs may preserve content-addressed original sources.
            frozen = directory.parent / "frozen_sources" / expected / path.name
            require(frozen.is_file() and hashlib.sha256(frozen.read_bytes()).hexdigest() == expected,
                    "SOURCE_CHANGED:" + name)
    require(len(rows) == manifest["raw_count"], "RAW_COUNT_MISMATCH")
    if http_gateway:
        return audit_http(directory, data, rows, cli, rerun)
    if safety:
        from experiment_safety import run_case
        require(all(run_case(row["case"], row["actions"], row["expected_allowed"]) == row
                    for row in rows), "WHITELIST_RERUN_MISMATCH")
        require(all(row["passed"] for row in rows), "WHITELIST_FAILED")
        return {"passed": True, "mode": "whitelist_atomicity", "cases": len(rows),
                "rejected": data["rejected"],
                "accepted_positive_controls": data["accepted_positive_controls"]}
    for row in rows:
        require(math.isfinite(row["total_user_ms"]) and row["total_user_ms"] >= 0, "INVALID_LATENCY")
        if row["task_class"] in {"safety", "simple"}:
            require(row["backend"] == "local-rules", "LOCAL_ISOLATION_VIOLATION")
    if sensitivity:
        trace = load_json(directory / "trace.json")
        for variant in data["variants"]:
            selected = [row for row in rows if row["variant"] == variant["variant"]]
            validate_trace(trace, variant["profiles"])
            require(len(selected) == len(trace["samples"]), "SENSITIVITY_COVERAGE")
            require(aggregate(selected) == variant["statistics"], "SENSITIVITY_STATS_MISMATCH")
            if rerun:
                fresh, changes = replay(trace, variant["profiles"], variant["config"], cli, arms=("joint",))
                require([stable_projection({k: v for k, v in row.items() if k != "variant"})
                         for row in selected] == [stable_projection(row) for row in fresh],
                        "SENSITIVITY_REPLAY_MISMATCH")
                require(len(changes) == variant["switches"], "SENSITIVITY_SWITCH_MISMATCH")
        return {"passed": True, "mode": "exploratory_sensitivity", "raw_rows": len(rows),
                "variants": len(data["variants"]), "exact_C_policy_rerun": rerun}
    if concurrent:
        for group in data["groups"]:
            selected = [row for row in rows if row["concurrency"] == group["concurrency"]]
            calculated = aggregate(selected)
            calculated["paired_effects"] = []
            require(calculated == group["statistics"], "CONCURRENCY_STATS_MISMATCH")
        require(all(cell["final_active"] == cell["final_waiting"] == 0
                    and cell["local_peak_active"] <= 2 and cell["cloud_peak_active"] <= 8
                    and cell.get("fifo_verified", True)
                    for cell in data["cells"]), "QUEUE_LEAK_OR_CAPACITY_VIOLATION")
        expected_keys = Counter(
            (cell["arm"], cell["concurrency"], cell["scenario"], cell["repetition"])
            for cell in data["cells"])
        require(all(n == 1 for n in expected_keys.values()), "DUPLICATE_CONCURRENCY_CELL")
        for cell in data["cells"]:
            selected = [row for row in rows if all(row[name] == cell[name] for name in (
                "arm", "concurrency", "scenario", "repetition"))]
            require(len(selected) == cell["requests"], "CONCURRENCY_CELL_COUNT")
        return {"passed": True, "mode": "wall_clock_stub", "raw_rows": len(rows),
                "checks": ["artifact hashes", "source hashes", "percentile/statistical recomputation",
                           "queue capacity/drain", "cell coverage", "local safety isolation"],
                "not_claimed": "Runtime timings are stochastic, not exact trace replay"}

    profiles, config = manifest["profiles"]["profiles"], manifest["config"]
    traces = [load_json(path) for path in sorted(directory.glob("trace_*.json"))]
    require(len(traces) > 0, "MISSING_TRACES")
    require({trace["trace_id"] for trace in traces} == set(manifest["trace_ids"]), "TRACE_MANIFEST_MISMATCH")
    lookup = {}
    for trace in traces:
        validate_trace(trace, profiles)
        for sample in trace["samples"]:
            lookup[(trace["trace_id"], sample["index"])] = sample
    pairings = defaultdict(list)
    for row in rows:
        key = (row["trace_id"], row["trace_index"])
        require(key in lookup, "ORPHAN_ROW")
        sample = lookup[key]
        require(row["input_hash"] == canonical_hash(sample), "PAIRED_INPUT_MISMATCH")
        require(row["policy_config_hash"] == canonical_hash(config), "CONFIG_MISMATCH")
        require(row["seed"] in [trace["seed"] for trace in traces
                                if trace["trace_id"] == row["trace_id"]], "SEED_MISMATCH")
        profile = profiles[sample["service"]]
        require(row["service"] == sample["service"] and row["scenario"] == sample["scenario"], "CELL_MISMATCH")
        require(row["deadline_ms"] == profile["deadline_ms"], "DEADLINE_MISMATCH")
        require(math.isclose(sum(row["latency_components_ms"].values()), row["total_user_ms"],
                             abs_tol=1e-8), "LATENCY_COMPONENT_MISMATCH")
        result = modeled_result(sample, profile, row["selected_backend"], config)
        require(all(row[name] == value for name, value in result.items()), "MODELED_COST_MISMATCH")
        pairings[key].append(row["arm"])
    require(set(pairings) == set(lookup), "MISSING_REQUESTS")
    require(all(sorted(arms) == sorted(ARMS) for arms in pairings.values()), "INCOMPLETE_ARMS")
    require(canonical_hash([stable_projection(row) for row in rows]) ==
            manifest["deterministic_rows_hash"], "RAW_PROJECTION_HASH_MISMATCH")
    require(aggregate(rows) == data["statistics"], "STATISTICS_RECOMPUTATION_MISMATCH")
    switches = load_json(directory / "switches.json")
    row_lookup = {(row["trace_id"], row["arm"], row["trace_index"]): row for row in rows}
    for switch in switches:
        before = row_lookup[(switch["trace_id"], switch["arm"], switch["from_index"])]
        after = row_lookup[(switch["trace_id"], switch["arm"], switch["to_index"])]
        require(before["selected_backend"] == switch["from_backend"]
                and after["selected_backend"] == switch["to_backend"]
                and after["reason"] == switch["reason"], "SWITCH_EVIDENCE_MISMATCH")
    if rerun:
        expected = defaultdict(list)
        for row in rows:
            expected[row["trace_id"]].append(stable_projection(row))
        for trace in traces:
            fresh, _ = replay(trace, profiles, config, cli)
            require([stable_projection(row) for row in fresh] == expected[trace["trace_id"]],
                    "C_POLICY_REPLAY_DIVERGENCE")
    return {
        "passed": True, "mode": "synthetic_modeled_trace", "raw_rows": len(rows),
        "paired_requests": len(pairings), "switches_checked": len(switches),
        "exact_C_policy_rerun": rerun,
        "checks": ["artifact hashes", "source hashes", "trace hashes and validation",
                   "all arms share exact per-request inputs", "complete matrix",
                   "latency components and outcomes", "percentiles and intervals",
                   "switch before/after", "local safety isolation"],
    }


def audit_http(directory, data, rows, cli, rerun):
    from experiment_http_gateway import confirmed, summarize_http
    from resource_policy import ResourcePolicyClient
    require(all(row.get("workload") in {"matrix", "stress"} for row in rows),
            "HTTP_UNKNOWN_WORKLOAD")
    require(summarize_http(rows) == data["cells"], "HTTP_STATS_MISMATCH")
    trace_files = [load_json(path) for path in directory.glob("trace_*.json")]
    require({trace["trace_id"] for trace in trace_files} == set(data["trace_ids"]),
            "HTTP_TRACE_MANIFEST_MISMATCH")
    traces = {trace["seed"]: trace for trace in trace_files if trace["seed"] != 20261119}
    stress_trace = next((trace for trace in trace_files if trace["seed"] == 20261119), None)
    require(stress_trace is not None, "MISSING_HTTP_STRESS_TRACE")
    for trace in trace_files:
        validate_trace(trace, data["profiles"])
    matrix = [row for row in rows if row["workload"] == "matrix"]
    expected_matrix = {(seed, arm, sample["index"]) for seed, trace in traces.items()
                       for sample in trace["samples"] for arm in data["matrix_arms"]}
    observed_matrix = [(row["seed"], row["arm"], row["trace_index"]) for row in matrix]
    require(len(observed_matrix) == len(expected_matrix) and
            set(observed_matrix) == expected_matrix, "HTTP_MATRIX_COVERAGE")
    require(len({row["request_key"] for row in rows}) == len(rows), "DUPLICATE_HTTP_REQUEST")
    stress_base = stress_trace["samples"][0]
    stress_services = ("interactive_inference", "interactive_inference", "sensor_batch",
                       "interactive_inference", "safety_control")
    for row in rows:
        require(row["configured_input_hash"] == canonical_hash(row["input"]), "HTTP_INPUT_HASH")
        if row["workload"] == "matrix":
            require(row["input"] == traces[row["seed"]]["samples"][row["trace_index"]],
                    "HTTP_TRACE_DIVERGENCE")
            continue
        require(row["seed"] == 20261119, "HTTP_STRESS_SEED_MISMATCH")
        require(row["trace_index"] >= 0, "HTTP_STRESS_TRACE_INDEX")
        expected = dict(stress_base)
        expected["network"] = {name: dict(value) for name, value in stress_base["network"].items()}
        expected["compute"] = {name: dict(value) for name, value in stress_base["compute"].items()}
        expected["scenario"] = row["scenario"]
        if row["scenario"] == "cloud_down":
            expected["compute"]["cloud"]["model_ready"] = False
        expected["index"] = row["trace_index"]
        expected["service"] = stress_services[row["trace_index"] % len(stress_services)]
        require(row["service"] == expected["service"], "HTTP_STRESS_SERVICE_DIVERGENCE")
        require(row["input"] == expected, "HTTP_STRESS_TRACE_DIVERGENCE")
    persisted = {}
    event_groups = []
    for path in sorted(directory.rglob("events.jsonl")):
        events = read_rows(path)
        require(len({event["decision_sequence"] for event in events}) == len(events),
                "DUPLICATE_DECISION_SEQUENCE")
        event_groups.append(events)
        for event in events:
            require(event["request_key"] not in persisted, "DUPLICATE_PERSISTED_EVENT")
            persisted[event["request_key"]] = event
    for row in rows:
        response = row["response"]
        event = response.get("gateway_event")
        require(event is not None and persisted.get(row["request_key"]) == event,
                "MISSING_OR_DIVERGENT_PERSISTED_EVENT")
        require(row["contract_complete"] == (row["response_status"] == 200 and confirmed(row["service"], response)),
                "HTTP_CONTRACT_MISMATCH")
        require(row["total_user_ms"] + .1 >= event["latency"]["total_user_ms"], "HTTP_LATENCY_SCOPE")
        require(math.isfinite(row["total_user_ms"]) and row["total_user_ms"] >= 0, "INVALID_HTTP_LATENCY")
        require(event["policy"]["implementation"] == "dual_endpoint_C_policy", "LEGACY_ENTRY_STILL_ACTIVE")
        if row["service"] in {"safety_control", "sensor_batch"}:
            require(event["backend"] == "local-rules", "HTTP_LOCAL_ISOLATION")
    require(len(persisted) == len(rows), "ORPHAN_EVENT")
    stress = [row for row in rows if row["workload"] == "stress"]
    expected_stress = {(level, scenario, arm, repetition, index)
                       for level in data["levels"] for scenario in ("healthy", "cloud_down")
                       for arm in ("joint", "network-only") for repetition in range(data["repetitions"])
                       for index in range(data["requests_per_cell"])}
    observed_stress = [(row["concurrency"], row["scenario"], row["arm"], row["repetition"], row["trace_index"])
                       for row in stress]
    require(len(observed_stress) == len(expected_stress) and set(observed_stress) == expected_stress,
            "HTTP_STRESS_COVERAGE")
    require(all(q["active"] == 0 and q["waiting"] == 0 and q["peak"] <= q["slots"] and q["fifo"]
                for cell in data["queue_cells"] for q in cell["queues"].values()), "HTTP_QUEUE_CAPACITY_OR_LEAK")
    lookup = {row["request_key"]: row for row in rows}
    for change in load_json(directory / "switches.json"):
        after = lookup[change["request_key"]]
        before = next(row for row in matrix if row["seed"] == change["seed"] and row["arm"] == change["arm"]
                      and row["trace_index"] == change["from_index"])
        require(after["response"]["selected_backend"] == change["to_backend"]
                and before["response"]["selected_backend"] == change["from_backend"]
                and after["reason"] == change["reason"], "HTTP_SWITCH_MISMATCH")
    if rerun:
        for events in event_groups:
            if not events:
                continue
            first = events[0]["policy"]
            client = ResourcePolicyClient(cli, first["config"], first["arm"])
            try:
                for event in sorted(events, key=lambda value: value["decision_sequence"]):
                    actual = replay_captured_policy(client, event)
                    require(actual[:2] == (event["selected_backend"], event["decision_reason"]),
                            "CAPTURED_OBSERVATION_C_REPLAY_DIVERGENCE")
            finally:
                client.close()
    return {"passed": True, "mode": "default_HTTP_gateway_measured_contract",
            "raw_rows": len(rows), "matrix_rows": len(matrix), "stress_rows": len(stress),
            "persisted_events": len(persisted), "captured_observation_C_replay": rerun,
            "checks": ["default initializer policy", "configured trace identity", "full workload coverage",
                       "persistent event equality", "contract effects", "latency scope",
                       "statistics recomputation", "FIFO/capacity/drain", "switch evidence"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--rerun", action="store_true")
    parser.add_argument("--cli", default=str(HERE / ("policy_cli.exe" if sys.platform == "win32" else "policy_cli")))
    args = parser.parse_args()
    result = audit(args.directory, args.cli, args.rerun)
    write_json(args.directory / "audit.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
