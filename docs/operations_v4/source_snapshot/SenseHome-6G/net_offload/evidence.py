#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提交前证据包的轻量 schema 检查。"""

from __future__ import annotations

import math
from typing import Any, Mapping

from network_trace import NetworkTrace


def validate_results_schema(data: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("meta", "stub_parameters", "harness", "summaries", "paired_traces", "raw_runs"):
        if key not in data:
            errors.append(f"missing top-level field: {key}")
    if not isinstance(data.get("summaries"), list):
        errors.append("summaries must be a list")
    else:
        latency_fields = ("probe_median_ms", "policy_median_ms", "backend_median_ms", "total_user_median_ms")
        for index, summary in enumerate(data["summaries"]):
            if not isinstance(summary, Mapping):
                errors.append(f"summary[{index}] must be an object")
                continue
            for key in ("phase", "arm", "overall"):
                if key not in summary:
                    errors.append(f"summary[{index}] missing field: {key}")
            overall = summary.get("overall", {})
            if not isinstance(overall, Mapping):
                errors.append(f"summary[{index}].overall must be an object")
                continue
            for key in latency_fields:
                if key not in overall:
                    errors.append(f"summary[{index}].overall missing field: {key}")
                elif not _nonnegative_finite(overall[key]):
                    errors.append(f"summary[{index}].overall invalid {key}")
    traces = data.get("paired_traces")
    if not isinstance(traces, Mapping):
        errors.append("paired_traces must be an object")
        traces = {}
    else:
        if not traces:
            errors.append("paired_traces must not be empty")
        for name, trace in traces.items():
            if not isinstance(trace, Mapping):
                errors.append(f"paired_traces[{name}] must be an object")
                continue
            if not trace.get("trace_id"):
                errors.append(f"paired_traces[{name}] missing trace_id")
            if not isinstance(trace.get("samples"), list):
                errors.append(f"paired_traces[{name}] samples must be a list")
            elif not trace["samples"]:
                errors.append(f"paired_traces[{name}] samples must not be empty")
            try:
                parsed = NetworkTrace.from_dict(dict(trace))
                if [s.index for s in parsed.samples] != list(range(len(parsed.samples))):
                    errors.append(f"paired_traces[{name}] sample indexes must be contiguous")
                for sample in parsed.samples:
                    for link in ("wan", "lan"):
                        for role in ("probe", "infer"):
                            sample.outcome(role, link)
            except (KeyError, TypeError, ValueError) as exc:
                errors.append(f"paired_traces[{name}] invalid: {exc}")

    raw_runs = data.get("raw_runs")
    paired_rows = {}
    if not isinstance(raw_runs, list) or not raw_runs:
        errors.append("raw_runs must be a non-empty list")
    else:
        for index, run in enumerate(raw_runs):
            prefix = f"raw_runs[{index}]"
            if not isinstance(run, Mapping):
                errors.append(f"{prefix} must be an object")
                continue
            phase, arm = run.get("phase"), run.get("arm")
            if phase not in traces or arm not in {"adaptive", "static-cloud"}:
                errors.append(f"{prefix} has invalid phase/arm")
                continue
            pair_key = (phase, arm)
            if pair_key in paired_rows:
                errors.append(f"{prefix} duplicate paired arm")
            rows = run.get("rows")
            if not isinstance(rows, list) or not rows:
                errors.append(f"{prefix}.rows must be a non-empty list")
                continue
            trace = traces[phase]
            if not isinstance(trace, Mapping):
                errors.append(f"{prefix} references malformed phase trace")
                continue
            samples = trace.get("samples", [])
            if not isinstance(samples, list):
                errors.append(f"{prefix} references malformed phase samples")
                continue
            references = []
            for row_index, row in enumerate(rows):
                label = f"{prefix}.rows[{row_index}]"
                if not isinstance(row, Mapping):
                    errors.append(f"{label} must be an object")
                    continue
                if row.get("source") not in {"stub-assumption", "local-rules", "rule-timing-proxy", "real-model"}:
                    errors.append(f"{label} invalid or missing source")
                if row.get("trace_id") != trace.get("trace_id"):
                    errors.append(f"{label} trace_id does not match phase trace")
                trace_index = row.get("trace_index")
                if (not isinstance(trace_index, int) or isinstance(trace_index, bool)
                        or not 0 <= trace_index < len(samples)):
                    errors.append(f"{label} trace_index out of range")
                references.append((trace_index, row.get("task")))
                latency_fields = ("probe_ms", "policy_ms", "backend_ms", "total_user_ms")
                valid_latency = True
                for field in latency_fields:
                    if not _nonnegative_finite(row.get(field)):
                        errors.append(f"{label} invalid {field}")
                        valid_latency = False
                if valid_latency and not math.isclose(
                    row["total_user_ms"], row["probe_ms"] + row["policy_ms"] + row["backend_ms"],
                    rel_tol=1e-9, abs_tol=0.01,
                ):
                    errors.append(f"{label} total_user_ms does not equal latency parts")
            if len({ref[0] for ref in references}) != len(references):
                errors.append(f"{prefix} duplicate trace_index")
            paired_rows[pair_key] = references
        for phase in traces:
            adaptive = paired_rows.get((phase, "adaptive"))
            static = paired_rows.get((phase, "static-cloud"))
            if adaptive is None or static is None or adaptive != static:
                errors.append(f"phase {phase} paired arms do not reference identical index/task rows")
    return errors


def _nonnegative_finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
