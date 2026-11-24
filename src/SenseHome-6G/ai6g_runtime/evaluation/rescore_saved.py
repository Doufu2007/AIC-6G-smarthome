"""Offline rescorer for saved AI6G gold reports.

This tool never calls a model, never reruns a task, and never overwrites an
existing report or JSONL evidence file. It reads report-level rows already
saved by evaluator.py, applies the frozen deadline rule, and writes a new
``*_rescored.json`` plus ``*_rescored.md`` pair.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

SCORER_SCHEMA = "ai6g.saved_rescore.v1"
DEFAULT_REPORTS = (
    "holdout_test_v1.json",
    "calibration_v1.json",
    "holdout_test_7b_v1.json",
)


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _row_external_elapsed(row: Mapping[str, Any]) -> float | None:
    for key in ("external_elapsed_ms", "elapsed_ms"):
        value = _numeric(row.get(key))
        if value is not None:
            return value
    timing = row.get("timing", {})
    if isinstance(timing, Mapping):
        return _numeric(timing.get("external_elapsed_ms"))
    return None


def _row_runtime_elapsed(row: Mapping[str, Any]) -> tuple[float | None, str]:
    timing = row.get("timing", {})
    if not isinstance(timing, Mapping):
        return None, "runtime:unknown"
    return _numeric(timing.get("total_ms")), str(timing.get("scope") or "entry_to_state_before_log")


def _latencies(rows: Sequence[Mapping[str, Any]]) -> tuple[list[float], str]:
    external = [_row_external_elapsed(row) for row in rows]
    if rows and all(value is not None for value in external):
        return [float(value) for value in external if value is not None], "external:elapsed_ms"
    values: list[float] = []
    scopes: set[str] = set()
    for row in rows:
        value, scope = _row_runtime_elapsed(row)
        if value is not None:
            values.append(value)
        scopes.add(scope)
    return values, (next(iter(scopes)) if len(scopes) == 1 else "runtime:mixed")


def _percentile(values: Sequence[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * p
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _rescore_row(row: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    updated = copy.deepcopy(dict(row))
    change: dict[str, Any] | None = None
    status = updated.get("semantic_status")
    reason = str(updated.get("semantic_reason") or "")
    if status == "not_evaluable" and reason.startswith("expected_explicit_"):
        change = {
            "strategy": updated.get("strategy"),
            "repeat": updated.get("repeat"),
            "task_id": updated.get("task_id"),
            "field": "semantic_status",
            "before": "not_evaluable",
            "after": "incorrect",
            "reason": reason,
        }
        updated["semantic_status"] = "incorrect"
        # semantic_success was already false in the saved report. Keep it
        # unchanged so the semantic-success count is auditable and invariant.

    deadline = _numeric(updated.get("deadline_ms"))
    elapsed = _row_external_elapsed(updated)
    elapsed_scope = "external:elapsed_ms" if elapsed is not None else None
    if elapsed is None:
        elapsed, runtime_scope = _row_runtime_elapsed(updated)
        elapsed_scope = runtime_scope if elapsed is not None else None
    deadline_correct: bool | None
    if deadline is None:
        deadline_correct = None
    else:
        # Deadline correctness is deliberately semantic: both conditions are
        # required. No backend/engineering success shortcut is allowed.
        deadline_correct = bool(
            updated.get("semantic_success") is True
            and elapsed is not None
            and elapsed <= deadline
        )
    updated["rescore_deadline_elapsed_ms"] = elapsed
    updated["rescore_deadline_scope"] = elapsed_scope
    updated["rescore_deadline_correct"] = deadline_correct
    return updated, change


def _summary(rows: Sequence[Mapping[str, Any]], runs: int) -> dict[str, Any]:
    values, scope = _latencies(rows)
    statuses = Counter(str(row.get("semantic_status")) for row in rows)
    errors = Counter(str(row["error_code"]) for row in rows if row.get("error_code"))
    flags = [row.get("rescore_deadline_correct") for row in rows]
    evaluable = [flag for flag in flags if flag is not None]
    semantic_successes = sum(bool(row.get("semantic_success")) for row in rows)
    engineering_successes = sum(bool(row.get("engineering_ok")) for row in rows)
    return {
        "runs": runs,
        "requests": len(rows),
        "engineering_successes": engineering_successes,
        "semantic_successes": semantic_successes,
        "semantic_success_rate": semantic_successes / len(rows) if rows else 0.0,
        "model_real_rows": sum(bool(row.get("model_real")) for row in rows),
        "semantic_statuses": dict(sorted(statuses.items())),
        "errors": dict(sorted(errors.items())),
        "p50_total_ms": _percentile(values, 0.50),
        "p95_total_ms": _percentile(values, 0.95),
        "latency_scope": scope,
        "deadline_correct": sum(1 for flag in evaluable if flag is True),
        "deadline_evaluable": len(evaluable),
        "deadline_correct_rate": (
            sum(1 for flag in evaluable if flag is True) / len(evaluable)
            if evaluable else None
        ),
    }


def rescore_report(report: Mapping[str, Any], *, original_path: Path, output_path: Path) -> dict[str, Any]:
    if not isinstance(report, Mapping) or not isinstance(report.get("rows"), list):
        raise ValueError("saved report must contain a rows list")
    rows: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    for row in report["rows"]:
        if not isinstance(row, Mapping):
            raise ValueError("every saved report row must be an object")
        updated, change = _rescore_row(row)
        rows.append(updated)
        if change is not None:
            changes.append(change)

    by_strategy: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_strategy.setdefault(str(row.get("strategy", "unknown")), []).append(row)
    strategies: dict[str, Any] = {}
    original_strategies = report.get("strategies", {})
    for strategy, strategy_rows in by_strategy.items():
        original_summary = original_strategies.get(strategy, {}) if isinstance(original_strategies, Mapping) else {}
        runs = int(original_summary.get("runs", report.get("repeats", 1)) or 1)
        strategies[strategy] = _summary(strategy_rows, runs)

    output = copy.deepcopy(dict(report))
    output["rows"] = rows
    output["strategies"] = strategies
    original_bytes = original_path.read_bytes()
    scorer_bytes = Path(__file__).read_bytes()
    output["rescore"] = {
        "schema": SCORER_SCHEMA,
        "rule_version": "deadline_requires_semantic_success_and_elapsed_leq_deadline_v1",
        "original_report_path": str(original_path),
        "output_report_path": str(output_path),
        "original_report_sha256": hashlib.sha256(original_bytes).hexdigest(),
        "scorer_source_sha256": hashlib.sha256(scorer_bytes).hexdigest(),
        "source_report_unchanged": True,
        "rows_rescored": len(rows),
        "status_changes": changes,
        "status_change_count": len(changes),
        "semantic_success_count_before_and_after": sum(bool(row.get("semantic_success")) for row in rows),
        "notes": [
            "Only saved report rows were read; no model, runtime, task, or JSONL rerun occurred.",
            "Old not_evaluable rows with reason expected_explicit_* are relabeled incorrect; semantic_success is unchanged.",
            "deadline_correct requires semantic_success=true and elapsed_ms<=deadline_ms; missing elapsed is false when a deadline exists.",
        ],
    }
    return output


def report_to_markdown(report: Mapping[str, Any]) -> str:
    meta = report.get("rescore", {})
    lines = [
        "# AI6G Saved Report Rescored",
        "",
        f"Tasks: {report.get('task_count', 0)}; repeats: {report.get('repeats', 0)}",
        f"Original report SHA-256: `{meta.get('original_report_sha256', '')}`",
        f"Scorer source SHA-256: `{meta.get('scorer_source_sha256', '')}`",
        f"Status changes: {meta.get('status_change_count', 0)}",
        "",
        "| Strategy | Requests | Engineering OK | Semantic success | Semantic rate | Deadline correct | Deadline evaluable | P50(ms) | P95(ms) | Statuses |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for strategy, summary in report.get("strategies", {}).items():
        statuses = ", ".join(f"{k}={v}" for k, v in summary.get("semantic_statuses", {}).items())
        def fmt(value: Any) -> str:
            return "-" if value is None else f"{float(value):.3f}"
        lines.append(
            f"| {strategy} | {summary.get('requests', 0)} | {summary.get('engineering_successes', 0)} | "
            f"{summary.get('semantic_successes', 0)} | {float(summary.get('semantic_success_rate', 0.0)):.3f} | "
            f"{summary.get('deadline_correct', 0)} | {summary.get('deadline_evaluable', 0)} | "
            f"{fmt(summary.get('p50_total_ms'))} | {fmt(summary.get('p95_total_ms'))} | {statuses} |"
        )
    lines += [
        "",
        "此文件由已保存报告离线重评分生成；没有重新调用模型，也没有修改原始 JSON/JSONL。",
        "deadline_correct 只在 semantic_success=true 且 elapsed_ms<=deadline_ms 时计为 true。",
        "",
    ]
    return "\n".join(lines)


def rescore_file(path: Path, output_dir: Path | None = None) -> tuple[Path, Path]:
    report = json.loads(path.read_text(encoding="utf-8"))
    target_dir = output_dir or path.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = path.stem
    output_json = target_dir / f"{stem}_rescored.json"
    output_md = target_dir / f"{stem}_rescored.md"
    if output_json.exists() or output_md.exists():
        raise FileExistsError(f"refusing to overwrite existing rescore output for {path}")
    rescored = rescore_report(report, original_path=path, output_path=output_json)
    output_json.write_text(json.dumps(rescored, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output_md.write_text(report_to_markdown(rescored) + "\n", encoding="utf-8")
    return output_json, output_md


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Offline rescore saved AI6G gold reports")
    parser.add_argument("--evidence-dir", default="SenseHome-6G/ai6g_runtime/evidence")
    parser.add_argument("--report", action="append", dest="reports", help="report JSON basename/path; repeatable")
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args(argv)
    evidence_dir = Path(args.evidence_dir)
    paths = [Path(item) for item in args.reports] if args.reports else [evidence_dir / name for name in DEFAULT_REPORTS]
    output_dir = Path(args.out_dir) if args.out_dir else None
    results = []
    for path in paths:
        if not path.is_absolute() and not path.exists():
            path = evidence_dir / path
        if not path.exists():
            raise FileNotFoundError(path)
        results.append(rescore_file(path, output_dir))
    for json_path, md_path in results:
        print(f"wrote {json_path}")
        print(f"wrote {md_path}")


if __name__ == "__main__":
    main()
