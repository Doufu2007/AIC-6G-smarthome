#!/usr/bin/env python3
"""Evaluate device action plans against reviewed reference labels.

The model's semantic_complete flag is never used as a correctness label.
No requests are made unless this CLI is explicitly run with an endpoint.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import http.client
import json
import math
import os
import statistics
import time
from pathlib import Path
from urllib.parse import urlparse

from backends.openai_backend import validate_actions


def canonical_actions(actions):
    actions = validate_actions(actions)
    normalized = []
    for action in actions:
        name, params = action["name"], action["params"]
        row = {"name": name, "location": params["location"]}
        if name == "light_control":
            operation = params.get("action", "set" if "brightness" in params else None)
            row["brightness"] = 0 if operation == "off" else params.get("brightness", 100)
        elif name == "curtain_control":
            operation = params.get("action", "set" if "position" in params else None)
            row["position"] = 0 if operation == "close" else params.get("position", 100)
        normalized.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
    # Independent device actions can appear in either order. Retain duplicate
    # actions so extra/unrequested operations do not get a free pass.
    return sorted(normalized)


def actions_equal(expected, actual):
    try:
        return canonical_actions(expected) == canonical_actions(actual)
    except (ValueError, TypeError, KeyError):
        return False


def load_cases(path, allow_draft_labels=False):
    cases = []
    seen = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        if not isinstance(case, dict) or not isinstance(case.get("command"), str) or not case.get("id") or case["id"] in seen:
            raise ValueError("INVALID_OR_DUPLICATE_CASE")
        validate_actions(case.get("expected_actions"))
        if case.get("human_reviewed") is not True and not allow_draft_labels:
            raise ValueError("DRAFT_LABELS_REQUIRE_REVIEW_OR_EXPLICIT_OPT_IN")
        seen.add(case["id"])
        cases.append(case)
    if not cases:
        raise ValueError("EMPTY_CASES")
    return cases


def request_backend(base_url, command, context=None, timeout_s=90.0):
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query:
        raise ValueError("INVALID_ENDPOINT")
    connection_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_cls(parsed.hostname, parsed.port, timeout=timeout_s)
    headers = {"Content-Type": "application/json"}
    wrapper_token = os.environ.get("SENSEHOME_WRAPPER_TOKEN", "")
    if wrapper_token:
        headers["Authorization"] = "Bearer " + wrapper_token
    started = time.perf_counter()
    try:
        body = json.dumps({"command": command, "context": context or {}}, ensure_ascii=False).encode("utf-8")
        connection.request("POST", parsed.path.rstrip("/") + "/infer", body, headers)
        response = connection.getresponse()
        raw = response.read(262145)
        elapsed = (time.perf_counter() - started) * 1000.0
        if len(raw) > 262144:
            return response.status, {"error": "RESPONSE_TOO_LARGE"}, elapsed
        result = json.loads(raw)
        return response.status, result if isinstance(result, dict) else {"error": "INVALID_OBJECT"}, elapsed
    except Exception as exc:
        # Emit only the exception class; request/header text may contain keys.
        return 0, {"error": type(exc).__name__}, (time.perf_counter() - started) * 1000.0
    finally:
        connection.close()


def evaluate_cases(base_url, cases, repeat=1, timeout_s=90.0):
    rows = []
    for iteration in range(repeat):
        for case in cases:
            status, result, elapsed = request_backend(base_url, case["command"], case.get("context"), timeout_s)
            http_ok = 200 <= status < 300
            actions = result.get("actions")
            correct = http_ok and actions_equal(case["expected_actions"], actions)
            rows.append({
                "id": case["id"], "iteration": iteration,
                "category": case.get("category", "uncategorized"),
                "command": case["command"], "expected_actions": case["expected_actions"],
                "actual_actions": actions, "http_status": status, "interface_ok": http_ok,
                "action_correct": bool(correct), "request_ms": round(elapsed, 3),
                "model_ms": result.get("model_ms"), "model_ms_kind": result.get("model_ms_kind"),
                "source": result.get("source", "unknown"), "error": result.get("error"),
                "configured_model": result.get("configured_model"),
                "upstream_model": result.get("upstream_model"),
                "model_revision": result.get("model_revision", "unknown"),
                "gpu_info": result.get("gpu_info"), "gpu_info_source": result.get("gpu_info_source"),
            })

    def aggregate(subset):
        count = len(subset)
        delays = sorted(row["request_ms"] for row in subset)
        return {
            "n_cases": count,
            "n_http_ok": sum(row["interface_ok"] for row in subset),
            "n_action_correct": sum(row["action_correct"] for row in subset),
            "action_accuracy_pct": round(100 * sum(row["action_correct"] for row in subset) / count, 2) if count else None,
            "request_median_ms": statistics.median(delays) if count else None,
            "request_p95_ms": delays[max(0, math.ceil(count * 0.95) - 1)] if count else None,
        }

    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "label_status": "human_reviewed" if all(case.get("human_reviewed") is True for case in cases) else "draft_labels_not_human_verified",
        "scope": "model_action_planning_only_no_device_execution",
        "correctness_source": "reference_actions_not_model_self_report",
        "repeat": repeat,
        "summary": aggregate(rows),
        "by_category": {category: aggregate([row for row in rows if row["category"] == category]) for category in sorted({row["category"] for row in rows})},
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(description="Evaluate a configured SenseHome real-model adapter")
    parser.add_argument("--url", required=True)
    parser.add_argument("--cases", default=str(Path(__file__).parent / "backends" / "eval_commands.reviewed.jsonl"))
    parser.add_argument("--out", required=True)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--allow-draft-labels", action="store_true")
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    try:
        cases = load_cases(args.cases, args.allow_draft_labels)
    except (ValueError, OSError):
        parser.error("Review the JSONL labels first, or explicitly use --allow-draft-labels for preliminary checks.")
    report = evaluate_cases(args.url, cases, args.repeat, args.timeout)
    report["dataset_sha256"] = hashlib.sha256(Path(args.cases).read_bytes()).hexdigest()
    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"label_status": report["label_status"], **report["summary"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
