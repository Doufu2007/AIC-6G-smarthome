"""Versioned synthetic application-network/compute traces with frozen outcomes.

No sample is a field capture, packet trace, ns-3 output, or physical 6G result.
Independent keyed streams ensure that policy traffic cannot shift failures.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import random
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCENARIOS = (
    "healthy", "wan_degraded", "wan_blackout", "local_queue", "cloud_queue", "local_model_down",
    "local_memory", "cloud_down", "both_models_down", "bandwidth_collapse",
    "handover", "jitter_burst", "lan_blackout", "recovered",
)
ARMS = ("joint", "network-only", "compute-only", "static-cloud",
        "static-local", "rules-only", "no-hysteresis")
MODES = {"joint": 0, "network-only": 1, "compute-only": 2,
         "static-cloud": 3, "static-local": 4, "rules-only": 5,
         "no-hysteresis": 0}


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + "\n",
                    encoding="utf-8")


def load_profiles(path: str | Path = HERE / "experiments/service_profiles.json") -> dict[str, Any]:
    data = load_json(path)
    if data.get("schema_version") != "sensehome.services.v1":
        raise ValueError("INVALID_SERVICE_SCHEMA")
    profiles = data["profiles"]
    if not profiles:
        raise ValueError("EMPTY_SERVICE_PROFILES")
    for profile in profiles.values():
        if profile["task_class"] not in {"simple", "complex", "safety"}:
            raise ValueError("INVALID_TASK_CLASS")
        for field in ("deadline_ms", "request_bytes", "response_bytes",
                      "wan_rtt_max_ms", "wan_jitter_max_ms", "wan_loss_max_pct"):
            number(profile[field], field, 0, 1000000)
        if profile["deadline_ms"] <= 0 or not 0 < profile["reliability_target"] <= 1:
            raise ValueError("INVALID_SERVICE_BUDGET")
    return data


def validate_config(config: dict[str, Any]) -> None:
    if config.get("schema_version") != "sensehome.policy.v1":
        raise ValueError("INVALID_POLICY_SCHEMA")
    for field in ("queue_max_ms", "inflight_max", "free_mem_min_mb", "local_infer_ms",
                  "cloud_infer_ms", "switch_margin_ms", "recovery_samples",
                  "timeout_ms", "observation_max_age_ms"):
        value = config[field]
        if type(value) is not int or not 0 <= value <= 1000000:
            raise ValueError("INVALID_POLICY_" + field.upper())
    if config["timeout_ms"] <= 0 or config["recovery_samples"] > 10000:
        raise ValueError("INVALID_POLICY_BOUND")
    for field in ("observation_lookup_ms", "policy_model_ms", "execution_model_ms", "rule_model_ms"):
        number(config[field], field, 0, 1000000)


def number(value: Any, name: str, minimum: float, maximum: float) -> None:
    if (isinstance(value, bool) or not isinstance(value, (float, int))
            or not math.isfinite(value) or not minimum <= value <= maximum):
        raise ValueError("INVALID_" + name.upper())


def rng_for(seed: int, index: int, role: str) -> random.Random:
    return random.Random(int(canonical_hash([seed, index, role]), 16))


def make_trace(seed: int, per_phase: int = 100) -> dict[str, Any]:
    if type(seed) is not int or type(per_phase) is not int or not 5 <= per_phase <= 100000:
        raise ValueError("INVALID_TRACE_GENERATION_ARGUMENT")
    samples = []
    services = ("interactive_inference", "interactive_inference", "sensor_batch",
                "interactive_inference", "safety_control")
    for phase in SCENARIOS:
        for phase_index in range(per_phase):
            index = len(samples)
            wan = {"reachable": True, "rtt_ms": 25, "jitter_ms": 3,
                   "loss_pct": 0, "bandwidth_kbps": 20000}
            lan = {"reachable": True, "rtt_ms": 3, "jitter_ms": 1,
                   "loss_pct": 0, "bandwidth_kbps": 100000}
            local = {"model_ready": True, "queue_ms": 0, "free_mem_mb": 2048, "inflight": 0}
            cloud = {"model_ready": True, "queue_ms": 0, "free_mem_mb": 16384, "inflight": 0}
            event = phase
            if phase == "wan_degraded":
                wan.update(rtt_ms=420, jitter_ms=60, loss_pct=8)
            elif phase == "wan_blackout":
                wan.update(reachable=False, loss_pct=100)
            elif phase == "local_queue":
                local.update(queue_ms=650, inflight=12)
            elif phase == "cloud_queue":
                cloud.update(queue_ms=650, inflight=12)
            elif phase == "local_model_down":
                local.update(model_ready=False)
            elif phase == "local_memory":
                local.update(free_mem_mb=96)
            elif phase == "cloud_down":
                cloud.update(model_ready=False)
            elif phase == "both_models_down":
                local.update(model_ready=False)
                cloud.update(model_ready=False)
            elif phase == "bandwidth_collapse":
                wan.update(bandwidth_kbps=64)
            elif phase == "handover":
                # Freeze a correlated burst, not IID "packet loss".
                outage = phase_index % 20 < 6
                wan.update(reachable=not outage, loss_pct=100 if outage else 0,
                           rtt_ms=90 if not outage else 25)
                local["model_ready"] = phase_index % 20 >= 3
                event = "handover-edge-unreachable" if outage else "handover-reattach"
            elif phase == "jitter_burst":
                high = phase_index % 2 == 0
                # Costs straddle the local/cloud boundary by <15ms. This is a
                # purpose-built stability stressor, not a captured radio trace.
                wan.update(rtt_ms=80 if high else 60, jitter_ms=10)
            elif phase == "lan_blackout":
                lan.update(reachable=False, loss_pct=100)
            outcomes = {}
            for name, link in (("wan", wan), ("lan", lan)):
                for role in ("probe", "infer"):
                    rng = rng_for(seed, index, name + "-" + role)
                    outcomes[name + "-" + role] = {
                        "refused": not link["reachable"] or rng.random() < link["loss_pct"] / 100,
                        "rtt_ms": round(max(0, link["rtt_ms"] +
                                            rng.uniform(-link["jitter_ms"], link["jitter_ms"])), 6),
                    }
            samples.append({
                "index": index, "at_ms": index * 100, "phase_index": phase_index,
                "scenario": phase, "event": event, "service": services[index % 5],
                "network": {"wan": wan, "lan": lan},
                "compute": {"local": local, "cloud": cloud},
                "compute_source": "injected_synthetic_endpoint_state",
                "observed_age_ms": 0,
                "outcomes": outcomes,
            })
    payload = {"schema_version": "sensehome.trace.v1", "generator_version": 3,
               "origin": "synthetic_design_proxy", "seed": seed,
               "time_basis": "ordered open-loop application requests, 100ms spacing; no packet scheduler",
               "samples": samples}
    return {**payload, "trace_id": canonical_hash(payload)}


def validate_trace(data: dict[str, Any], profiles: dict[str, Any]) -> None:
    if data.get("schema_version") != "sensehome.trace.v1" or not data.get("samples"):
        raise ValueError("INVALID_TRACE_SCHEMA")
    payload = {key: value for key, value in data.items() if key != "trace_id"}
    if canonical_hash(payload) != data.get("trace_id"):
        raise ValueError("TRACE_ID_MISMATCH")
    previous_time = -1
    for index, sample in enumerate(data["samples"]):
        if type(sample["index"]) is not int or sample["index"] != index:
            raise ValueError("NONCONTIGUOUS_TRACE_INDEX")
        if sample["service"] not in profiles:
            raise ValueError("UNKNOWN_SERVICE")
        number(sample["at_ms"], "at_ms", 0, 1e12)
        number(sample["observed_age_ms"], "observation_age", 0, 1e12)
        if sample["at_ms"] < previous_time:
            raise ValueError("NONMONOTONIC_TRACE")
        previous_time = sample["at_ms"]
        for name in ("wan", "lan"):
            link = sample["network"][name]
            if type(link["reachable"]) is not bool:
                raise ValueError("INVALID_REACHABILITY")
            for field, limit in (("rtt_ms", 60000), ("jitter_ms", 60000),
                                 ("loss_pct", 100), ("bandwidth_kbps", 1e9)):
                number(link[field], field, 0, limit)
            if link["bandwidth_kbps"] <= 0:
                raise ValueError("NONPOSITIVE_BANDWIDTH")
            for role in ("probe", "infer"):
                outcome = sample["outcomes"][name + "-" + role]
                if type(outcome["refused"]) is not bool:
                    raise ValueError("INVALID_OUTCOME")
                number(outcome["rtt_ms"], "outcome_rtt", 0, 120000)
        for endpoint in ("local", "cloud"):
            state = sample["compute"][endpoint]
            if state["model_ready"] is not None and type(state["model_ready"]) is not bool:
                raise ValueError("INVALID_MODEL_READY")
            for field in ("queue_ms", "free_mem_mb", "inflight"):
                if state[field] is not None:
                    if type(state[field]) is not int:
                        raise ValueError("NONINTEGER_COMPUTE_STATE")
                    number(state[field], field, 0, 1000000)


def transfer_ms(profile: dict[str, Any], link: dict[str, Any]) -> float | None:
    bandwidth = link.get("bandwidth_kbps")
    if bandwidth is None:
        return None
    number(bandwidth, "bandwidth_kbps", 0, 1e9)
    if bandwidth == 0:
        raise ValueError("NONPOSITIVE_BANDWIDTH")
    # kbps is decimal kbit/s: bytes * 8 / kbps yields milliseconds.
    return 8 * (profile["request_bytes"] + profile["response_bytes"]) / bandwidth


def apply_patch_to_trace(data: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """Helper for independent validation fixtures, not used to mutate raw input."""
    out = copy.deepcopy(data)
    out.update(changes)
    out["trace_id"] = canonical_hash({k: v for k, v in out.items() if k != "trace_id"})
    return out
