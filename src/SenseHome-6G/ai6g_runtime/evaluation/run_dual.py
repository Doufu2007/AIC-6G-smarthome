"""Independent real dual-model experiment runner for AI6G.

The runner deliberately lives outside the frozen runtime/evaluator.  It uses
the same model adapters, protocol validation, privacy guard and DeviceStore as
the product runtime, while keeping gold annotations on the offline scoring
side.  Calibration runs both configured models under the ``local-model`` alias
so a strict-privacy task cannot accidentally become a model-capability test of
cloud routing.  Evaluation then uses the measured calibration rates as the
quality observations consumed by the adaptive router.

No network request is made at import time.  Use ``--mode calibrate`` first,
then ``--mode evaluate --calibration ...`` when both endpoints are available.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in (None, ""):
    # ``run_dual.py`` lives one level below the package root (evaluation/).
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from ai6g_runtime.benchmark import FixedRouter, load_tasks
    from ai6g_runtime.evaluator import (
        _deadline_correct,
        _deadline_for_task,
        _latency_values,
        _percentile,
        semantic_score_row,
    )
    from ai6g_runtime.executor import DeviceStore
    from ai6g_runtime.health_monitor import ProbeTask, load_calibration
    from ai6g_runtime.models import OpenAICompatibleBackend, RuleBackend
    from ai6g_runtime.router import HealthObservation, ObservationCache, TaskRouter
    from ai6g_runtime.runtime import TaskRuntime
else:
    from ..benchmark import FixedRouter, load_tasks
    from ..evaluator import (
        _deadline_correct,
        _deadline_for_task,
        _latency_values,
        _percentile,
        semantic_score_row,
    )
    from ..executor import DeviceStore
    from ..health_monitor import ProbeTask, load_calibration
    from ..models import OpenAICompatibleBackend, RuleBackend
    from ..router import HealthObservation, ObservationCache, TaskRouter
    from ..runtime import TaskRuntime


SCHEMA_CALIBRATION = "ai6g.dual_model_calibration.v1"
SCHEMA_EVALUATION = "ai6g.dual_model_evaluation.v1"
REPEATS = 3
MAX_TOKENS = 384
SEED = 20261103
PROBE_TIMEOUT_S = 5.0
DEFAULT_TTL_S = 5.0
STRATEGIES = ("rules", "local", "cloud", "adaptive")
CORE_SOURCE_FILES = (
    "evaluation/run_dual.py",
    "runtime.py",
    "models.py",
    "router.py",
    "protocol.py",
    "executor.py",
    "health_monitor.py",
    "evaluator.py",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_sha() -> str:
    return _sha256(Path(__file__).resolve())


def parse_ttl(value: str | float) -> float:
    """Parse an observation TTL without allowing NaN or infinity."""

    try:
        ttl_s = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("AI6G_OBSERVATION_TTL_S must be a number") from exc
    if not math.isfinite(ttl_s) or ttl_s < 0:
        raise ValueError("AI6G_OBSERVATION_TTL_S must be a finite non-negative number")
    return ttl_s


def source_manifest(*, calibration_path: Path | None = None) -> dict[str, Any]:
    """Hash the core runtime sources and optional calibration bytes."""

    package_root = Path(__file__).resolve().parents[1]
    files: dict[str, str] = {}
    for relative in CORE_SOURCE_FILES:
        path = package_root / relative
        if not path.is_file():
            raise FileNotFoundError(f"core source file missing: {path}")
        files[relative] = _sha256(path)
    calibration_sha = _sha256(calibration_path) if calibration_path is not None else None
    payload = {
        "schema": "ai6g.source_manifest.v1",
        "files": files,
        "calibration_file_sha256": calibration_sha,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return {**payload, "sha256": digest, "manifest_sha256": digest}


def _safe_endpoint(endpoint: str | None) -> str | None:
    """Keep endpoint identity auditable without serialising URL credentials."""

    if not endpoint:
        return None
    try:
        from urllib.parse import urlsplit, urlunsplit

        parsed = urlsplit(endpoint)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except (TypeError, ValueError):
        return str(endpoint).split("?", 1)[0]


def _json_safe(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return repr(value)


def _model_specs() -> dict[str, dict[str, Any]]:
    """Read the two declared deployments without ever exposing API keys."""

    return {
        "local-model": {
            "deployment": "local-1.5B",
            "endpoint": os.getenv("AI6G_LOCAL_URL"),
            "model": os.getenv("AI6G_LOCAL_MODEL", "1.5B"),
            "api_key": os.getenv("AI6G_LOCAL_API_KEY"),
        },
        "cloud-model": {
            "deployment": "cloud-7B",
            "endpoint": os.getenv("AI6G_CLOUD_URL"),
            "model": os.getenv("AI6G_CLOUD_MODEL", "7B"),
            "api_key": os.getenv("AI6G_CLOUD_API_KEY"),
        },
    }


def _fixed_config() -> dict[str, Any]:
    return {
        "temperature": 0,
        "seed": SEED,
        "max_tokens": MAX_TOKENS,
        "response_format": "json_object",
        "system_prompt": "OpenAICompatibleBackend default system prompt",
    }


def _backend(name: str, spec: Mapping[str, Any]) -> OpenAICompatibleBackend | None:
    endpoint = spec.get("endpoint")
    if not endpoint:
        return None
    return OpenAICompatibleBackend(
        name=name,
        endpoint=str(endpoint),
        model=str(spec.get("model") or name),
        api_key=spec.get("api_key"),
        max_tokens=MAX_TOKENS,
        seed=SEED,
    )


def _backends(specs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"local-rules": RuleBackend()}
    for name in ("local-model", "cloud-model"):
        adapter = _backend(name, specs[name])
        if adapter is not None:
            result[name] = adapter
    return result


def _new_runtime(
    backends: Mapping[str, Any],
    strategy: str,
    *,
    cache: ObservationCache | None,
    log_path: Path,
) -> TaskRuntime:
    if strategy == "adaptive":
        if cache is None:
            raise ValueError("adaptive runtime requires an observation cache")
        router: Any = TaskRouter(cache)
    else:
        route_name = {
            "rules": "local-rules",
            "local": "local-model",
            "cloud": "cloud-model",
        }.get(strategy)
        if route_name is None:
            raise ValueError(f"unknown strategy: {strategy}")
        router = FixedRouter(route_name, f"FIXED_{strategy.upper()}")
    return TaskRuntime(
        router=router,
        backends=backends,
        store=DeviceStore(),
        log_path=log_path,
    )


def _handle(runtime: TaskRuntime, text: str, request_id: str) -> dict[str, Any]:
    started = time.perf_counter()
    row = runtime.handle(text, request_id=request_id)
    elapsed = max(0.0, (time.perf_counter() - started) * 1000.0)
    # This is measured outside TaskRuntime and is intentionally kept separate
    # from its entry-to-state timing scope.
    row["external_elapsed_ms"] = elapsed
    row["request_external_perf_counter_ms"] = elapsed
    row["request_only_elapsed_ms"] = elapsed
    return row


def _summary(rows: Sequence[Mapping[str, Any]], runs: int) -> dict[str, Any]:
    statuses = Counter(str(row.get("semantic_status")) for row in rows)
    errors = Counter(str(row["error_code"]) for row in rows if row.get("error_code"))
    latencies, scope = _latency_values(rows)
    flags = [_deadline_correct(row, elapsed_ms=float(row["external_elapsed_ms"]))
             if isinstance(row.get("external_elapsed_ms"), (int, float)) else _deadline_correct(row)
             for row in rows]
    evaluable = [flag for flag in flags if flag is not None]
    semantic_successes = sum(bool(row.get("semantic_success")) for row in rows)
    engineering_successes = sum(bool(row.get("engineering_ok")) for row in rows)
    deadline_correct = sum(flag is True for flag in evaluable)
    request_only = [
        float(row["request_only_elapsed_ms"])
        for row in rows
        if isinstance(row.get("request_only_elapsed_ms"), (int, float))
    ]
    return {
        "runs": runs,
        "requests": len(rows),
        "engineering_successes": engineering_successes,
        "semantic_successes": semantic_successes,
        "semantic_success_rate": semantic_successes / len(rows) if rows else 0.0,
        "model_real_rows": sum(bool(row.get("model_real")) for row in rows),
        "semantic_statuses": dict(sorted(statuses.items())),
        "errors": dict(sorted(errors.items())),
        "p50_total_ms": _percentile(latencies, 0.50),
        "p95_total_ms": _percentile(latencies, 0.95),
        "p50_request_only_ms": _percentile(request_only, 0.50),
        "p95_request_only_ms": _percentile(request_only, 0.95),
        "latency_scope": "external:elapsed_plus_probe" if rows and any(
            float(row.get("probe_refresh_elapsed_ms", 0.0) or 0.0) > 0.0 for row in rows
        ) else scope,
        "deadline_correct": deadline_correct,
        "deadline_evaluable": len(evaluable),
        "deadline_correct_rate": deadline_correct / len(evaluable) if evaluable else None,
    }


def _row_evidence(
    *,
    strategy: str,
    repeat: int,
    task_index: int,
    task: Mapping[str, Any],
    raw_row: Mapping[str, Any],
    score: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep raw model evidence while emitting a stable, reviewable row."""

    row = {
        "strategy": strategy,
        "repeat": repeat,
        "task_id": task.get("id", task_index),
        "task_text": task.get("text"),
        "engineering_ok": bool(score.get("engineering_ok")),
        "semantic_success": bool(score.get("semantic_success")),
        "semantic_status": score.get("status"),
        "semantic_reason": score.get("reason"),
        "model_real": bool(raw_row.get("model_real")),
        "error_code": raw_row.get("error_code"),
        "timing": copy.deepcopy(raw_row.get("timing", {})),
        "external_elapsed_ms": raw_row.get("external_elapsed_ms"),
        "request_external_perf_counter_ms": raw_row.get("request_external_perf_counter_ms"),
        "request_only_elapsed_ms": raw_row.get("request_only_elapsed_ms"),
        "probe_refresh_elapsed_ms": raw_row.get("probe_refresh_elapsed_ms", 0.0),
        "probe_refresh_ms": raw_row.get("probe_refresh_ms", 0.0),
        "probe_record_indices": list(raw_row.get("probe_record_indices", [])),
        "probe_records_before": raw_row.get("probe_records_before"),
        "probe_records_after": raw_row.get("probe_records_after"),
        "route": copy.deepcopy(raw_row.get("route", {})),
        "deadline_ms": _deadline_for_task(task),
        "result_kind": raw_row.get("result_kind"),
        "model_payload": _json_safe(raw_row.get("model_payload")),
        "raw_response": _json_safe(raw_row.get("raw_response")),
        "model_error": raw_row.get("model_error"),
    }
    if score.get("expected_actions") is not None:
        row["expected_actions"] = _json_safe(score.get("expected_actions"))
    if score.get("actual_actions") is not None:
        row["actual_actions"] = _json_safe(score.get("actual_actions"))
    return row


def _run_one_model_calibration(
    *,
    backend_name: str,
    spec: Mapping[str, Any],
    tasks: Sequence[Mapping[str, Any]],
    task_path: Path,
    out_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run one deployment under the local-model alias for all 3 repeats."""

    adapter = _backend("local-model", spec)
    if adapter is None:
        raise RuntimeError(f"{backend_name} endpoint is not configured")
    log_path = out_path.with_name(f"{out_path.stem}.{backend_name}.jsonl")
    rows: list[dict[str, Any]] = []
    for repeat in range(REPEATS):
        for task_index, task in enumerate(tasks):
            runtime = _new_runtime(
                {"local-model": adapter},
                "local",
                cache=None,
                log_path=log_path,
            )
            before = runtime.state()
            request_id = f"calibrate-{backend_name}-r{repeat}-t{task.get('id', task_index)}"
            raw = _handle(runtime, str(task["text"]), request_id)
            raw["initial_state"] = before
            raw["_deadline_ms"] = _deadline_for_task(task)
            score = semantic_score_row(raw, task.get("gold", {}))
            rows.append(_row_evidence(
                strategy=backend_name,
                repeat=repeat,
                task_index=task_index,
                task=task,
                raw_row=raw,
                score=score,
            ))
    summary = _summary(rows, REPEATS)
    return {
        "alias": "local-model",
        "deployment": spec.get("deployment"),
        "model": spec.get("model"),
        "endpoint": _safe_endpoint(str(spec.get("endpoint"))),
        "quality_score": summary["semantic_success_rate"],
        "source_sha256": _sha256(task_path),
        "requests": summary["requests"],
        "semantic_successes": summary["semantic_successes"],
        "semantic_success_rate": summary["semantic_success_rate"],
        "model_real_rows": summary["model_real_rows"],
        "log_path": str(log_path),
        "summary": summary,
    }, rows


def _deployment_disclosure(specs: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    same_card = os.getenv("AI6G_CLOUD_SAME_CARD", "unspecified")
    return {
        "local": "AI6G_LOCAL_URL/AI6G_LOCAL_MODEL is treated as the local 1.5B deployment.",
        "cloud": "AI6G_CLOUD_URL/AI6G_CLOUD_MODEL is treated as the cloud 7B deployment.",
        "same_card_remote_prototype": (
            "The cloud endpoint may be a remote prototype on the same physical GPU/card; "
            "this experiment does not claim independent cloud infrastructure."
        ),
        "same_card_flag": same_card,
        "configured_endpoints": {
            name: _safe_endpoint(str(spec.get("endpoint"))) if spec.get("endpoint") else None
            for name, spec in specs.items()
        },
    }


def calibrate(tasks_path: Path, out_path: Path) -> dict[str, Any]:
    tasks = load_tasks(tasks_path)
    if len(tasks) != 12:
        raise ValueError(f"calibration requires exactly 12 tasks, got {len(tasks)}")
    specs = _model_specs()
    backends: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    for name in ("local-model", "cloud-model"):
        entry, model_rows = _run_one_model_calibration(
            backend_name=name,
            spec=specs[name],
            tasks=tasks,
            task_path=tasks_path,
            out_path=out_path,
        )
        backends[name] = entry
        rows.extend(model_rows)
    return {
        "schema": SCHEMA_CALIBRATION,
        "mode": "calibrate",
        "repeats": REPEATS,
        "task_count": len(tasks),
        "task_source_path": str(tasks_path),
        "task_source_sha256": _sha256(tasks_path),
        "runner_source_sha256": _source_sha(),
        "source_manifest": source_manifest(),
        "fixed_model_config": _fixed_config(),
        "backend_disclosure": _deployment_disclosure(specs),
        "backends": backends,
        "rows": rows,
        "evidence": {
            "gold_used_only_offline": True,
            "gold_sent_to_model": False,
            "calibration_alias": "local-model for both deployments",
            "interpretation": "quality_score is the measured overall semantic_success_rate over 12 calibration tasks x 3 repeats.",
        },
    }


def _probe(
    *,
    name: str,
    backend: Any,
    quality_score: float,
    calibration_source_sha256: str,
) -> dict[str, Any]:
    wall_started = time.time()
    started = time.perf_counter()
    result: Any = None
    error: str | None = None
    try:
        result = backend.infer(ProbeTask(), timeout_s=PROBE_TIMEOUT_S)
        if not bool(getattr(result, "ok", False)):
            error = str(getattr(result, "error", None) or "probe backend returned failure")
    except Exception as exc:  # adapter boundary: failed probes are evidence
        error = f"probe exception: {type(exc).__name__}: {exc}"
    elapsed_ms = max(0.0, (time.perf_counter() - started) * 1000.0)
    model_real = bool(result is not None and getattr(result, "ok", False) and getattr(result, "model_real", False))
    reachable = bool(model_real and error is None)
    if not reachable and error is None:
        error = "probe did not produce a real model response"
    checked_at = min(time.time(), wall_started)
    raw = _json_safe(getattr(result, "raw_response", None)) if result is not None else None
    usage = raw.get("usage") if isinstance(raw, Mapping) else None
    if not isinstance(usage, Mapping):
        usage = {}
    return {
        "backend": name,
        "probe_text": ProbeTask.text,
        "reachable": reachable,
        "model_real": model_real,
        "ok": bool(result is not None and getattr(result, "ok", False)),
        "latency_ms": float(getattr(result, "latency_ms", elapsed_ms) or elapsed_ms),
        "external_elapsed_ms": elapsed_ms,
        "request_external_perf_counter_ms": elapsed_ms,
        "checked_at": checked_at,
        "quality_score": quality_score,
        "calibration_source_sha256": calibration_source_sha256,
        "error": error,
        "model_error": getattr(result, "error", None) if result is not None else None,
        "raw_response": raw,
        "usage": dict(usage),
        "resource_cost": {
            "requests": 1,
            "external_elapsed_ms": elapsed_ms,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "estimated_usd": None,
            "estimated_usd_note": "Provider pricing was not assumed; token/latency evidence is retained.",
        },
    }


def _ensure_observations(
    *,
    cache: ObservationCache,
    backends: Mapping[str, Any],
    calibration: Mapping[str, Any],
    probe_records: list[dict[str, Any]],
    probe_log: Path,
    ttl_s: float,
) -> dict[str, Any]:
    """Refresh due observations and return timing/evidence indices for one task."""

    probe_record_start = len(probe_records)
    probe_started: float | None = None
    for name in ("local-model", "cloud-model"):
        now = time.time()
        observation = cache.get(name)
        if observation is not None and not cache.needs_refresh(name, now):
            continue
        backend = backends.get(name)
        if backend is None:
            # Missing endpoint is deliberately left without a synthetic health
            # observation, so adaptive routing returns unavailable.
            continue
        entry = calibration.get(name)
        quality = float(entry.quality_score if hasattr(entry, "quality_score") else entry["quality_score"])
        source_sha = str(entry.source_sha256 if hasattr(entry, "source_sha256") else entry["source_sha256"])
        if probe_started is None:
            probe_started = time.perf_counter()
        record = _probe(
            name=name,
            backend=backend,
            quality_score=quality,
            calibration_source_sha256=source_sha,
        )
        probe_records.append(record)
        probe_log.parent.mkdir(parents=True, exist_ok=True)
        with probe_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        cache.put(HealthObservation(
            backend=name,
            reachable=bool(record["reachable"]),
            latency_ms=float(record["latency_ms"]),
            quality_score=quality,
            checked_at=min(float(record["checked_at"]), time.time()),
            error_rate=0.0 if record["reachable"] else 1.0,
            detail="dual-run ProbeTask; calibration quality score",
        ))
    probe_refresh_ms = (
        max(0.0, (time.perf_counter() - probe_started) * 1000.0)
        if probe_started is not None
        else 0.0
    )
    return {
        "probe_refresh_ms": probe_refresh_ms,
        "probe_record_indices": list(range(probe_record_start, len(probe_records))),
    }


def evaluate(tasks_path: Path, calibration_path: Path, out_path: Path) -> dict[str, Any]:
    tasks = load_tasks(tasks_path)
    calibration = load_calibration(calibration_path)
    missing = [name for name in ("local-model", "cloud-model") if name not in calibration]
    if missing:
        raise ValueError(f"calibration is missing backend entries: {', '.join(missing)}")
    specs = _model_specs()
    backends = _backends(specs)
    ttl_s = parse_ttl(os.getenv("AI6G_OBSERVATION_TTL_S", str(DEFAULT_TTL_S)))
    all_rows: list[dict[str, Any]] = []
    strategies: dict[str, Any] = {}
    probe_records: list[dict[str, Any]] = []
    probe_log = out_path.with_name(f"{out_path.stem}.probes.jsonl")
    for strategy in STRATEGIES:
        strategy_rows: list[dict[str, Any]] = []
        for repeat in range(REPEATS):
            cache = ObservationCache(ttl_s=ttl_s) if strategy == "adaptive" else None
            log_path = out_path.with_name(f"{out_path.stem}.{strategy}.run{repeat}.jsonl")
            for task_index, task in enumerate(tasks):
                task_started = time.perf_counter()
                probe_count_before = len(probe_records)
                probe_metadata = {"probe_refresh_ms": 0.0, "probe_record_indices": []}
                if cache is not None:
                    probe_metadata = _ensure_observations(
                        cache=cache,
                        backends=backends,
                        calibration=calibration,
                        probe_records=probe_records,
                        probe_log=probe_log,
                        ttl_s=ttl_s,
                    )
                runtime = _new_runtime(
                    backends,
                    strategy,
                    cache=cache,
                    log_path=log_path,
                )
                before = runtime.state()
                request_id = f"evaluate-{strategy}-r{repeat}-t{task.get('id', task_index)}"
                raw = _handle(runtime, str(task["text"]), request_id)
                request_only_elapsed = float(raw.get("external_elapsed_ms") or 0.0)
                raw["request_only_elapsed_ms"] = request_only_elapsed
                raw["probe_refresh_ms"] = float(probe_metadata.get("probe_refresh_ms", 0.0))
                # Retain the earlier descriptive key for compatibility with
                # any in-flight v3 tooling while making the canonical key
                # match the evidence schema.
                raw["probe_refresh_elapsed_ms"] = raw["probe_refresh_ms"]
                raw["probe_record_indices"] = list(probe_metadata.get("probe_record_indices", []))
                # The externally visible task interval includes synchronous
                # health refreshes that occurred immediately before routing.
                raw["external_elapsed_ms"] = max(
                    0.0, (time.perf_counter() - task_started) * 1000.0
                )
                raw["request_external_perf_counter_ms"] = raw["external_elapsed_ms"]
                raw["probe_records_before"] = probe_count_before
                raw["probe_records_after"] = len(probe_records)
                raw["initial_state"] = before
                raw["_deadline_ms"] = _deadline_for_task(task)
                score = semantic_score_row(raw, task.get("gold", {}))
                row = _row_evidence(
                    strategy=strategy,
                    repeat=repeat,
                    task_index=task_index,
                    task=task,
                    raw_row=raw,
                    score=score,
                )
                strategy_rows.append(row)
                all_rows.append(row)
        strategies[strategy] = _summary(strategy_rows, REPEATS)
    probe_cost = {
        "probe_requests": len(probe_records),
        "total_external_elapsed_ms": sum(float(r["external_elapsed_ms"]) for r in probe_records),
        "total_model_latency_ms": sum(float(r["latency_ms"]) for r in probe_records),
        "total_tokens": sum(
            int(r["usage"].get("total_tokens", 0) or 0)
            for r in probe_records
            if isinstance(r.get("usage"), Mapping)
        ),
        "estimated_usd": None,
        "estimated_usd_note": "Provider pricing was not assumed; every probe raw response and usage block is retained.",
        "probe_log": str(probe_log),
    }
    return {
        "schema": SCHEMA_EVALUATION,
        "mode": "evaluate",
        "repeats": REPEATS,
        "task_count": len(tasks),
        "task_source_path": str(tasks_path),
        "task_source_sha256": _sha256(tasks_path),
        "calibration_path": str(calibration_path),
        "calibration_file_sha256": _sha256(calibration_path),
        "runner_source_sha256": _source_sha(),
        "source_manifest": source_manifest(calibration_path=calibration_path),
        "fixed_model_config": _fixed_config(),
        "observation_ttl_s": ttl_s,
        "backend_disclosure": _deployment_disclosure(specs),
        "strategies": strategies,
        "rows": all_rows,
        "probes": probe_records,
        "probe_cost": probe_cost,
        "evidence": {
            "gold_used_only_offline": True,
            "gold_sent_to_model_or_router": False,
            "adaptive_missing_or_unqualified_backend": "unavailable is preserved; no quality score is raised or lowered.",
            "task_reset": "new DeviceStore and TaskRuntime for every strategy/repeat/task request",
        },
    }


def _markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# AI6G Dual Model Experiment",
        "",
        f"Mode: `{report.get('mode')}`; tasks: {report.get('task_count', 0)}; repeats: {report.get('repeats', 0)}",
        f"Runner source SHA-256: `{report.get('runner_source_sha256', '')}`",
        f"Task source SHA-256: `{report.get('task_source_sha256', '')}`",
        f"Source manifest SHA-256: `{(report.get('source_manifest') or {}).get('sha256', '')}`",
        "",
        "Cloud deployment disclosure: the 7B endpoint may be a remote prototype on the same physical GPU/card; this report does not claim independent cloud infrastructure.",
        "",
    ]
    if report.get("mode") == "calibrate":
        lines += [
            "| Backend | Model | Alias | Requests | Semantic successes | Quality score | Model-real rows |",
            "|---|---|---|---:|---:|---:|---:|",
        ]
        for name, entry in report.get("backends", {}).items():
            lines.append(
                f"| {name} | {entry.get('model')} | {entry.get('alias')} | {entry.get('requests', 0)} | "
                f"{entry.get('semantic_successes', 0)} | {float(entry.get('quality_score', 0.0)):.3f} | {entry.get('model_real_rows', 0)} |"
            )
    else:
        lines += [
            "| Strategy | Requests | Engineering OK | Semantic success | Semantic rate | Deadline correct | P50(ms) | P95(ms) | Model-real rows |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for strategy, summary in report.get("strategies", {}).items():
            fmt = lambda value: "-" if value is None else f"{float(value):.3f}"
            lines.append(
                f"| {strategy} | {summary.get('requests', 0)} | {summary.get('engineering_successes', 0)} | "
                f"{summary.get('semantic_successes', 0)} | {float(summary.get('semantic_success_rate', 0.0)):.3f} | "
                f"{summary.get('deadline_correct', 0)} | {fmt(summary.get('p50_total_ms'))} | "
                f"{fmt(summary.get('p95_total_ms'))} | {summary.get('model_real_rows', 0)} |"
            )
        cost = report.get("probe_cost", {})
        lines += [
            "",
            f"Probe requests: {cost.get('probe_requests', 0)}; external probe time: {float(cost.get('total_external_elapsed_ms', 0.0)):.3f} ms; total tokens: {cost.get('total_tokens', 0)}.",
            f"Probe raw evidence: `{cost.get('probe_log', '')}`",
        ]
    lines += [
        "",
        "Gold annotations are used only after each request for offline semantic scoring; they are never sent to a model or router.",
        "",
    ]
    return "\n".join(lines)


def _resolve_out(raw: str, mode: str) -> Path:
    path = Path(raw)
    if path.suffix.lower() != ".json":
        path = path / ("dual_calibration.json" if mode == "calibrate" else "dual_evaluate.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {path}")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the independent AI6G dual-model calibration/evaluation.")
    parser.add_argument("--mode", choices=("calibrate", "evaluate"), required=True)
    parser.add_argument("--out", required=True, help="JSON output path or a directory")
    parser.add_argument("--calibration", help="calibration JSON produced by --mode calibrate")
    parser.add_argument("--tasks", help="task JSONL; defaults to the frozen calibration/holdout set")
    args = parser.parse_args(argv)
    default_tasks = (
        Path(__file__).with_name("tasks_calibration_v1.jsonl")
        if args.mode == "calibrate"
        else Path(__file__).with_name("tasks_test_v1.jsonl")
    )
    tasks_path = Path(args.tasks) if args.tasks else default_tasks
    out_path = _resolve_out(args.out, args.mode)
    if args.mode == "calibrate":
        report = calibrate(tasks_path, out_path)
    else:
        if not args.calibration:
            parser.error("--calibration is required in evaluate mode")
        report = evaluate(tasks_path, Path(args.calibration), out_path)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    out_path.with_suffix(".md").write_text(_markdown(report), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"rows", "probes"}}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
