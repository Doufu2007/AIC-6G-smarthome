"""Repeated, auditable evaluation for AI6G runtime strategies."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ai6g_runtime.benchmark import load_tasks, run
else:
    from .benchmark import load_tasks, run


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


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


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
    value = _numeric(timing.get("total_ms"))
    scope = str(timing.get("scope") or "entry_to_state_before_log")
    return value, f"runtime:{scope}"


def _latency_values(rows: Sequence[Mapping[str, Any]]) -> tuple[list[float], str]:
    """Select one fair latency source for all rows in a summary.

    An external elapsed measurement is used only when every row provides one;
    otherwise all rows use the runtime timing scope and the report names that
    scope explicitly.  This avoids mixing incomparable clocks.
    """
    external = [_row_external_elapsed(row) for row in rows]
    if rows and all(value is not None for value in external):
        return [float(value) for value in external if value is not None], "external:elapsed_ms"
    runtime_values: list[float] = []
    scopes: set[str] = set()
    for row in rows:
        value, scope = _row_runtime_elapsed(row)
        if value is not None:
            runtime_values.append(value)
        scopes.add(scope)
    scope = next(iter(scopes)) if len(scopes) == 1 else "runtime:mixed"
    return runtime_values, scope


def _deadline_for_task(task: Mapping[str, Any]) -> float | None:
    value = task.get("deadline_ms")
    if value is None and isinstance(task.get("gold"), Mapping):
        value = task["gold"].get("deadline_ms")
    return _numeric(value)


def _deadline_correct(row: Mapping[str, Any], elapsed_ms: float | None = None) -> bool | None:
    deadline = _numeric(row.get("_deadline_ms", row.get("deadline_ms")))
    if deadline is None:
        return None
    # Deadline correctness is a semantic metric.  Generic engineering
    # evaluations without a gold semantic result are intentionally
    # non-evaluable rather than counting execution success as correctness.
    if "semantic_success" not in row:
        return None
    if row.get("semantic_success") is not True:
        return False
    if elapsed_ms is None:
        elapsed_ms = _row_external_elapsed(row)
    if elapsed_ms is None:
        elapsed_ms, _ = _row_runtime_elapsed(row)
    if elapsed_ms is None:
        return False
    return bool(row.get("error_code") != "deadline_exceeded"
                and elapsed_ms <= deadline)


def _summarize(rows: Sequence[Mapping[str, Any]], runs: int) -> dict[str, Any]:
    successes = [row for row in rows if row.get("ok") and row.get("execution", {}).get("ok")]
    latencies, latency_scope = _latency_values(rows)
    errors = Counter(str(row["error_code"]) for row in rows if row.get("error_code"))
    real_rows = sum(1 for row in rows if row.get("model_real") is True)
    deadline_flags = [_deadline_correct(row) for row in rows]
    deadline_evaluable = [flag for flag in deadline_flags if flag is not None]
    return {
        "runs": runs,
        "requests": len(rows),
        "successes": len(successes),
        "success_rate": len(successes) / len(rows) if rows else 0.0,
        "p50_total_ms": _percentile(latencies, 0.50),
        "p95_total_ms": _percentile(latencies, 0.95),
        "max_total_ms": max(latencies) if latencies else None,
        "latency_scope": latency_scope,
        "deadline_correct": sum(1 for flag in deadline_evaluable if flag),
        "deadline_evaluable": len(deadline_evaluable),
        "deadline_correct_rate": (sum(1 for flag in deadline_evaluable if flag) / len(deadline_evaluable)
                                  if deadline_evaluable else None),
        "model_real_rows": real_rows,
        "errors": dict(sorted(errors.items())),
    }


def evaluate(
    tasks: Iterable[Mapping[str, Any]],
    runtime_factory: Callable[[int], Any],
    *,
    strategies: Sequence[str] = ("adaptive", "rules", "local", "cloud"),
    repeats: int = 1,
) -> dict[str, Any]:
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    task_rows = list(tasks)
    if not strategies:
        raise ValueError("at least one strategy is required")
    result: dict[str, Any] = {
        "schema": "ai6g.evaluation.v1",
        "repeats": int(repeats),
        "task_count": len(task_rows),
        "strategies": {},
        "evidence": {"real_model_rows": 0, "model_real": False},
    }
    for strategy in strategies:
        all_rows: list[Mapping[str, Any]] = []
        for run_index in range(repeats):
            runtime = runtime_factory(run_index)
            run_report = run(task_rows, runtime, strategy)
            for task, row in zip(task_rows, run_report.get("rows", [])):
                enriched = dict(row)
                enriched["_deadline_ms"] = _deadline_for_task(task)
                all_rows.append(enriched)
        summary = _summarize(all_rows, repeats)
        result["strategies"][strategy] = summary
        result["evidence"]["real_model_rows"] += summary["model_real_rows"]
    result["evidence"]["model_real"] = result["evidence"]["real_model_rows"] > 0
    result["evidence"]["interpretation"] = (
        "真实模型证据存在，需附端点/模型/提示词和原始JSONL。"
        if result["evidence"]["model_real"]
        else "当前结果没有真实模型行；规则路径只能作为工程基线。"
    )
    return result


def report_to_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# AI+6G Runtime 重复实验报告",
        "",
        f"任务数：{report.get('task_count', 0)}；重复次数：{report.get('repeats', 0)}",
        "",
        "| 策略 | 重复 | 请求 | 成功率 | P50(ms) | P95(ms) | deadline_correct | model_real 行数 | 错误构成 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for strategy, summary in report.get("strategies", {}).items():
        errors = ", ".join(f"{key}={value}" for key, value in summary.get("errors", {}).items()) or "无"
        def fmt(value: Any) -> str:
            return "-" if value is None else f"{float(value):.3f}"
        lines.append(
            f"| {strategy} | {summary.get('runs', 0)} | {summary.get('requests', 0)} | "
            f"{float(summary.get('success_rate', 0.0)):.3f} | {fmt(summary.get('p50_total_ms'))} | "
            f"{fmt(summary.get('p95_total_ms'))} | {summary.get('deadline_correct', 0)} | "
            f"{summary.get('model_real_rows', 0)} | {errors} |"
        )
    evidence = report.get("evidence", {})
    lines += [
        "",
        f"证据状态：model_real={'true' if evidence.get('model_real') else 'false'}。",
        str(evidence.get("interpretation", "")),
        "",
        "规则基线、模型不可用、非法输出和超时结果均按失败记录；未配置真实模型时不得将本表解释为语义正确率。",
        "",
    ]
    return "\n".join(lines)



def task_set_sha256(tasks: Iterable[Mapping[str, Any]]) -> str:
    """Hash the parsed frozen task records, including their gold annotations."""
    payload = json.dumps(list(tasks), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    import hashlib
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _canonical_actions(actions: Any) -> list[dict[str, Any]]:
    if not isinstance(actions, list):
        return []
    result = []
    for item in actions:
        if not isinstance(item, Mapping):
            result.append({"_invalid": True})
            continue
        action = {key: value for key, value in item.items() if value is not None}
        # ``validate_plan`` and ``DeviceStore`` treat an omitted light_on
        # brightness as the protocol default (100).  Normalize both sides so
        # a model payload that omits this optional field is equivalent to the
        # explicit gold action.
        if action.get("type") == "light_on" and "brightness" not in action:
            action["brightness"] = 100
        result.append(action)
    # Action order does not affect the final device state for the frozen
    # multi-action tasks. Sorting preserves multiplicity while accepting an
    # otherwise equivalent permutation.
    return sorted(result, key=lambda action: json.dumps(action, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _flatten(mapping: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    result: dict[tuple[str, ...], Any] = {}
    for key, value in mapping.items():
        path = prefix + (str(key),)
        if isinstance(value, Mapping):
            result.update(_flatten(value, path))
        else:
            result[path] = value
    return result


def _state_changes(before: Any, after: Any) -> dict[tuple[str, ...], Any]:
    before_flat = _flatten(before if isinstance(before, Mapping) else {})
    after_flat = _flatten(after if isinstance(after, Mapping) else {})
    changes: dict[tuple[str, ...], Any] = {}
    for path in set(before_flat) | set(after_flat):
        if before_flat.get(path) != after_flat.get(path):
            changes[path] = after_flat.get(path)
    return changes


def semantic_score_row(row: Mapping[str, Any], gold: Mapping[str, Any]) -> dict[str, Any]:
    """Score one row offline; gold is never used by routing or model adapters."""
    engineering_ok = bool(row.get("ok") and row.get("execution", {}).get("ok"))
    if not engineering_ok:
        return {"status": "failed", "semantic_success": False, "engineering_ok": False,
                "reason": str(row.get("error_code") or "execution_failed")}

    expected_actions = _canonical_actions(gold.get("expected_action", []))
    actual_actions = _canonical_actions((row.get("plan") or {}).get("actions", []))

    def no_state_side_effects() -> dict[str, Any] | None:
        initial = row.get("initial_state")
        if isinstance(initial, Mapping):
            changes = _state_changes(initial, row.get("state", {}))
            if changes:
                path = next(iter(changes))
                return {"status": "incorrect", "semantic_success": False,
                        "engineering_ok": True, "reason": "illegal_side_effect",
                        "path": list(path)}
        return None

    if bool(gold.get("must_reject")):
        # Refusal is an explicit protocol result. Do not turn a normal action
        # or a backend failure into a fabricated success.
        if row.get("result_kind") == "refusal" and not actual_actions:
            side_effect = no_state_side_effects()
            if side_effect is not None:
                return side_effect
            return {"status": "semantic_success", "semantic_success": True, "engineering_ok": True,
                    "reason": "explicit_refusal"}
        return {"status": "incorrect", "semantic_success": False, "engineering_ok": True,
                "reason": "expected_explicit_refusal_not_returned"}
    if not expected_actions and bool(gold.get("allow_clarification")):
        if row.get("result_kind") == "clarification" and not actual_actions:
            side_effect = no_state_side_effects()
            if side_effect is not None:
                return side_effect
            return {"status": "semantic_success", "semantic_success": True, "engineering_ok": True,
                    "reason": "explicit_clarification"}
        return {"status": "incorrect", "semantic_success": False, "engineering_ok": True,
                "reason": "expected_explicit_clarification_not_returned"}
    if actual_actions != expected_actions:
        return {"status": "incorrect", "semantic_success": False, "engineering_ok": True,
                "reason": "exact_action_mismatch", "expected_actions": expected_actions,
                "actual_actions": actual_actions}

    expected_state = gold.get("expected_state", {})
    final_state = row.get("state", {})
    expected_flat = _flatten(expected_state if isinstance(expected_state, Mapping) else {})
    final_flat = _flatten(final_state if isinstance(final_state, Mapping) else {})
    for path, expected_value in expected_flat.items():
        if final_flat.get(path) != expected_value:
            return {"status": "incorrect", "semantic_success": False, "engineering_ok": True,
                    "reason": "expected_state_mismatch", "path": list(path)}
    initial = row.get("initial_state")
    if isinstance(initial, Mapping):
        changes = _state_changes(initial, final_state)
        for path in changes:
            if path not in expected_flat:
                return {"status": "incorrect", "semantic_success": False, "engineering_ok": True,
                        "reason": "illegal_side_effect", "path": list(path)}
    return {"status": "semantic_success", "semantic_success": True, "engineering_ok": True,
            "reason": "exact_actions_and_state"}


def evaluate_gold(
    tasks: Iterable[Mapping[str, Any]],
    runtime_factory: Callable[[int], Any],
    *,
    strategies: Sequence[str] = ("adaptive", "rules"),
    repeats: int = 1,
    task_source_sha256: str | None = None,
) -> dict[str, Any]:
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    task_rows = list(tasks)
    if not strategies:
        raise ValueError("at least one strategy is required")
    report: dict[str, Any] = {
        "schema": "ai6g.gold_evaluation.v1",
        "task_count": len(task_rows),
        "repeats": int(repeats),
        "task_set_sha256": task_set_sha256(task_rows),
        "source_sha256": task_source_sha256,
        "strategies": {},
        "rows": [],
    }
    for strategy in strategies:
        scored_rows: list[dict[str, Any]] = []
        for repeat_index in range(repeats):
            for task_index, task in enumerate(task_rows):
                runtime = runtime_factory(repeat_index * max(1, len(task_rows)) + task_index)
                before = runtime.state() if hasattr(runtime, "state") else None
                run_report = run([task], runtime, strategy)
                row = dict(run_report.get("rows", [{}])[0])
                row["_deadline_ms"] = _deadline_for_task(task)
                if before is not None:
                    row["initial_state"] = before
                score = semantic_score_row(row, task.get("gold", {}))
                report_row = {
                    "strategy": strategy,
                    "repeat": repeat_index,
                    "task_id": task.get("id", task_index),
                    "task_text": task.get("text"),
                    "engineering_ok": score["engineering_ok"],
                    "semantic_success": score["semantic_success"],
                    "semantic_status": score["status"],
                    "semantic_reason": score.get("reason"),
                    "model_real": bool(row.get("model_real")),
                    "error_code": row.get("error_code"),
                    "timing": row.get("timing", {}),
                    "route": row.get("route", {}),
                    "deadline_ms": row.get("_deadline_ms"),
                    "external_elapsed_ms": row.get("external_elapsed_ms", row.get("elapsed_ms")),
                }
                scored_rows.append(report_row)
                report["rows"].append(report_row)
        statuses = Counter(item["semantic_status"] for item in scored_rows)
        errors = Counter(item["error_code"] for item in scored_rows if item.get("error_code"))
        latency_values, latency_scope = _latency_values(scored_rows)
        deadline_flags = [_deadline_correct(item) for item in scored_rows]
        deadline_evaluable = [flag for flag in deadline_flags if flag is not None]
        report["strategies"][strategy] = {
            "runs": repeats,
            "requests": len(scored_rows),
            "engineering_successes": sum(item["engineering_ok"] for item in scored_rows),
            "semantic_successes": sum(item["semantic_success"] for item in scored_rows),
            "semantic_success_rate": sum(item["semantic_success"] for item in scored_rows) / len(scored_rows) if scored_rows else 0.0,
            "model_real_rows": sum(item["model_real"] for item in scored_rows),
            "semantic_statuses": dict(sorted(statuses.items())),
            "errors": dict(sorted(errors.items())),
            "p50_total_ms": _percentile(latency_values, 0.50),
            "p95_total_ms": _percentile(latency_values, 0.95),
            "latency_scope": latency_scope,
            "deadline_correct": sum(1 for flag in deadline_evaluable if flag),
            "deadline_evaluable": len(deadline_evaluable),
            "deadline_correct_rate": (sum(1 for flag in deadline_evaluable if flag) / len(deadline_evaluable)
                                      if deadline_evaluable else None),
        }
    report["evidence"] = {
        "gold_used_only_offline": True,
        "gold_sent_to_model": False,
        "model_real_rows": sum(summary["model_real_rows"] for summary in report["strategies"].values()),
        "interpretation": "semantic scores are exact offline checks over frozen gold; failures and missing expected refusal/clarification results remain non-success.",
    }
    return report


def gold_report_to_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Frozen AI6G Gold Evaluation",
        "",
        f"Tasks: {report.get('task_count', 0)}; repeats: {report.get('repeats', 0)}",
        f"Task-set SHA-256: `{report.get('task_set_sha256', '')}`",
        f"Source SHA-256: `{report.get('source_sha256') or 'not supplied'}`",
        "",
        "| Strategy | Requests | Engineering OK | Semantic success | Semantic rate | deadline_correct | P50(ms) | P95(ms) | Latency scope | model_real rows | Statuses |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|---:|---|",
    ]
    for strategy, summary in report.get("strategies", {}).items():
        statuses = ", ".join(f"{k}={v}" for k, v in summary.get("semantic_statuses", {}).items())
        fmt = lambda value: "-" if value is None else f"{float(value):.3f}"
        lines.append(
            f"| {strategy} | {summary.get('requests', 0)} | {summary.get('engineering_successes', 0)} | "
            f"{summary.get('semantic_successes', 0)} | {summary.get('semantic_success_rate', 0.0):.3f} | "
            f"{summary.get('deadline_correct', 0)} | {fmt(summary.get('p50_total_ms'))} | "
            f"{fmt(summary.get('p95_total_ms'))} | {summary.get('latency_scope', '-')} | "
            f"{summary.get('model_real_rows', 0)} | {statuses} |"
        )
    lines += ["", "Gold was used offline only; it was not sent to a model or router.", ""]
    return "\n".join(lines)

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--out", required=True, help="JSON report path; Markdown is written beside it")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--strategies", default="adaptive,rules,local,cloud")
    parser.add_argument(
        "--gold",
        action="store_true",
        help="score each task against its frozen gold annotation offline",
    )
    parser.add_argument(
        "--source",
        "--source-sha256",
        dest="source_path",
        help="optional source file whose bytes should be hashed (defaults to --tasks in --gold mode)",
    )
    args = parser.parse_args(argv)
    if __package__ in (None, ""):
        from ai6g_runtime.serve import build_runtime
    else:
        from .serve import build_runtime
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    # A fresh log file is required for every runtime instance in gold mode;
    # the number of instances is repeats * task_count, so do not cap this
    # counter by a guessed task count.
    serial = 0
    def factory(_: int) -> Any:
        nonlocal serial
        log_path = output.with_name(f"{output.stem}.run{serial}.jsonl")
        serial += 1
        return build_runtime(log_path)

    tasks = load_tasks(args.tasks)
    strategies = tuple(part.strip() for part in args.strategies.split(",") if part.strip())
    if args.gold:
        source_path = Path(args.source_path) if args.source_path else Path(args.tasks)
        source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        report = evaluate_gold(
            tasks,
            factory,
            strategies=strategies,
            repeats=args.repeats,
            task_source_sha256=source_sha256,
        )
        markdown = gold_report_to_markdown(report)
    else:
        report = evaluate(tasks, factory, strategies=strategies, repeats=args.repeats)
        markdown = report_to_markdown(report)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    output.with_suffix(".md").write_text(markdown, encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
