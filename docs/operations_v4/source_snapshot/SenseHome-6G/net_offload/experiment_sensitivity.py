"""One-factor-at-a-time exploratory policy sensitivity, preserving every row."""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path
from typing import Any

from experiment_inputs import HERE, canonical_hash, load_json, load_profiles, make_trace, write_json
from experiment_replay import replay, save_rows, seal_artifacts, source_manifest
from experiment_stats import aggregate


def variants(config: dict[str, Any], profiles: dict[str, Any]):
    yield "default", copy.deepcopy(config), copy.deepcopy(profiles)
    for field, values in (("recovery_samples", (0, 1, 5)),
                          ("switch_margin_ms", (0, 5, 40)),
                          ("queue_max_ms", (100, 700, 1000))):
        for value in values:
            current = copy.deepcopy(config)
            current[field] = value
            yield f"{field}={value}", current, copy.deepcopy(profiles)
    for value in (80, 300):
        current = copy.deepcopy(profiles)
        current["interactive_inference"]["wan_rtt_max_ms"] = value
        yield f"wan_rtt_max_ms={value}", copy.deepcopy(config), current


def calibration_trace(per_phase: int) -> dict[str, Any]:
    trace = make_trace(20261201, per_phase)
    template = copy.deepcopy(trace["samples"][0])
    # Isolate resource and network gates. The main trace intentionally combines
    # faults; these additional OFAT probes avoid attributing flat results to
    # an insensitive policy when a different gate is dominating.
    for scenario, values in (("queue_boundary", (80, 200, 350, 650)),
                             ("rtt_boundary", (80, 120, 200, 300))):
        for value in values:
            for _ in range(10):
                sample = copy.deepcopy(template)
                index = len(trace["samples"])
                sample.update(index=index, at_ms=index * 100, scenario=scenario,
                              service="interactive_inference", event=f"{scenario}:{value}")
                if scenario == "queue_boundary":
                    sample["compute"]["cloud"]["model_ready"] = False
                    sample["compute"]["local"].update(queue_ms=value, inflight=6)
                else:
                    sample["compute"]["local"]["inflight"] = 9
                    sample["network"]["wan"].update(rtt_ms=value, jitter_ms=0)
                    for role in ("probe", "infer"):
                        sample["outcomes"]["wan-" + role].update(rtt_ms=value, refused=False)
                trace["samples"].append(sample)
    trace["origin"] = "synthetic_design_proxy_plus_isolated_gate_calibration"
    trace["trace_id"] = canonical_hash({k: v for k, v in trace.items() if k != "trace_id"})
    return trace


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cli", default=str(HERE / ("policy_cli.exe" if sys.platform == "win32" else "policy_cli")))
    parser.add_argument("--per-phase", type=int, default=50)
    args = parser.parse_args()
    if args.out.exists() and any(args.out.iterdir()):
        parser.error("output must be empty")
    args.out.mkdir(parents=True, exist_ok=True)
    config = load_json(HERE / "experiments/policy_config.json")
    profiles = load_profiles()["profiles"]
    # Deliberately separate exploratory seed from the three final replay seeds.
    trace = calibration_trace(args.per_phase)
    rows, results, switches = [], [], []
    for name, cfg, services in variants(config, profiles):
        current, changes = replay(trace, services, cfg, args.cli, arms=("joint",))
        for row in current:
            row["variant"] = name
        for change in changes:
            change["variant"] = name
        rows.extend(current)
        switches.extend(changes)
        results.append({"variant": name, "config": cfg, "profiles": services,
                        "statistics": aggregate(current), "switches": len(changes)})
        print(f"sensitivity {name} rows={len(current)}", flush=True)
    save_rows(args.out / "raw_requests.jsonl", rows)
    write_json(args.out / "trace.json", trace)
    write_json(args.out / "switches.json", switches)
    write_json(args.out / "summary.json", {
        "schema_version": "sensehome.sensitivity.v1", "command": sys.argv,
        "evidence_type": "exploratory_synthetic_parameter_sensitivity",
        "sources": source_manifest(args.cli), "trace_id": trace["trace_id"],
        "raw_count": len(rows), "variants": results,
        "limits": "One synthetic seed, OFAT only; no exhaustive search or unbiased optimality claim",
    })
    seal_artifacts(args.out)


if __name__ == "__main__":
    main()
