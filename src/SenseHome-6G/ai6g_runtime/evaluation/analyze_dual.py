"""Audit a dual-model evaluation with text-overlap deduplication.

The evaluator report contains three repeated observations for each of the
holdout tasks.  This script leaves that report and all gold files untouched,
removes holdout tasks whose normalized text also appears in the development
set, and derives task-level, category-level, routing, error, latency, and
token-usage summaries. Probe requests and tokens are reported separately.
Task latency retains the input report's scope, including synchronous probes
when the evaluator declares external:elapsed_plus_probe.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping


def _read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        rows.append(value)
    return rows


def _sha256_bytes(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_manifest(
    manifest: Mapping[str, Any],
    holdout_tasks: list[dict[str, Any]],
    holdout_path: str | Path,
) -> dict[str, Any]:
    files = manifest.get("files") if isinstance(manifest.get("files"), Mapping) else {}
    entry = files.get("tasks_test_v1.jsonl") if isinstance(files, Mapping) else None
    if not isinstance(entry, Mapping):
        raise ValueError("manifest is missing files.tasks_test_v1.jsonl")
    expected_count = entry.get("records")
    expected_ids = entry.get("ids")
    actual_ids = [_task_id(row) for row in holdout_tasks]
    if expected_count is not None and int(expected_count) != len(holdout_tasks):
        raise ValueError("manifest holdout record count does not match input")
    if isinstance(expected_ids, list) and [str(value) for value in expected_ids] != actual_ids:
        raise ValueError("manifest holdout IDs do not match input")
    actual_sha256 = _sha256_bytes(holdout_path)
    expected_sha256 = entry.get("byte_sha256")
    if isinstance(expected_sha256, str) and expected_sha256 != actual_sha256:
        raise ValueError("manifest holdout byte_sha256 does not match input")
    return {
        "schema": manifest.get("schema"),
        "holdout_records_match": expected_count is None or int(expected_count) == len(holdout_tasks),
        "holdout_ids_match": not isinstance(expected_ids, list) or [str(value) for value in expected_ids] == actual_ids,
        "holdout_byte_sha256": actual_sha256,
        "holdout_byte_sha256_match": expected_sha256 is None or expected_sha256 == actual_sha256,
    }


def _text(row: Mapping[str, Any]) -> str:
    value = row.get("text", row.get("task_text", ""))
    if not isinstance(value, str):
        raise ValueError("task text must be a string")
    return value


def normalize_text(value: str) -> str:
    """Normalize only Unicode compatibility forms and whitespace for matching."""

    return " ".join(unicodedata.normalize("NFKC", value).split())


def _task_id(row: Mapping[str, Any]) -> str:
    value = row.get("id", row.get("task_id"))
    if not isinstance(value, str) or not value:
        raise ValueError("task id must be a non-empty string")
    return value


def find_text_overlap(
    development_tasks: Iterable[Mapping[str, Any]],
    holdout_tasks: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return exact normalized-text overlaps, retaining all source IDs."""

    development_by_text: dict[str, list[str]] = defaultdict(list)
    development_text: dict[str, str] = {}
    for row in development_tasks:
        task_id = _task_id(row)
        normalized = normalize_text(_text(row))
        development_by_text[normalized].append(task_id)
        development_text.setdefault(normalized, _text(row))

    overlaps: list[dict[str, Any]] = []
    for row in holdout_tasks:
        task_id = _task_id(row)
        normalized = normalize_text(_text(row))
        development_ids = development_by_text.get(normalized, [])
        if development_ids:
            overlaps.append(
                {
                    "holdout_id": task_id,
                    "development_ids": sorted(development_ids),
                    "text": _text(row),
                    "normalized_text": normalized,
                }
            )
    return overlaps


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _percentile(values: Iterable[float], percentile: float) -> float | None:
    clean = sorted(value for value in values if _finite_number(value) is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    position = (len(clean) - 1) * percentile / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return clean[lower]
    fraction = position - lower
    return clean[lower] + (clean[upper] - clean[lower]) * fraction


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _usage(row: Mapping[str, Any]) -> dict[str, int]:
    raw = row.get("raw_response")
    usage = raw.get("usage") if isinstance(raw, Mapping) else None
    if not isinstance(usage, Mapping):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
    }


def _task_metadata(tasks: Iterable[Mapping[str, Any]], retained_ids: set[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in tasks:
        task_id = _task_id(row)
        if task_id not in retained_ids:
            continue
        gold = row.get("gold") if isinstance(row.get("gold"), Mapping) else {}
        categories = gold.get("categories", row.get("categories", []))
        if not isinstance(categories, list):
            categories = []
        result[task_id] = {
            "text": _text(row),
            "categories": sorted(str(category) for category in categories),
        }
    return result


def _direction(delta: float | None, *, epsilon: float = 1e-12) -> str:
    if delta is None or abs(delta) <= epsilon:
        return "no_change"
    return "gain" if delta > 0 else "loss"


def _metric_comparison(candidate: Mapping[str, Any], baseline: Mapping[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    for key, direction in (
        ("semantic_rate", "higher_is_better"),
        ("engineering_rate", "higher_is_better"),
        ("p50_external_elapsed_ms", "lower_is_better"),
        ("p95_external_elapsed_ms", "lower_is_better"),
        ("total_tokens", "lower_is_better"),
    ):
        candidate_value = candidate.get(key)
        baseline_value = baseline.get(key)
        delta = None
        if candidate_value is not None and baseline_value is not None:
            delta = float(candidate_value) - float(baseline_value)
        gain = _direction(delta)
        if direction == "lower_is_better" and gain != "no_change":
            gain = "gain" if gain == "loss" else "loss"
        metrics[key] = {
            "baseline": baseline_value,
            "candidate": candidate_value,
            "delta": delta,
            "direction": direction,
            "assessment": gain,
        }
    return metrics


def _summarize_rows(
    rows: list[dict[str, Any]],
    *,
    task_metadata: Mapping[str, Mapping[str, Any]],
    repeats: int,
) -> dict[str, Any]:
    if not rows:
        raise ValueError("strategy has no retained rows")
    semantic_successes = sum(bool(row.get("semantic_success")) for row in rows)
    engineering_successes = sum(bool(row.get("engineering_ok")) for row in rows)
    latencies = [
        value
        for row in rows
        if (value := _finite_number(row.get("external_elapsed_ms"))) is not None
    ]
    routes = Counter(str((row.get("route") or {}).get("backend", "unknown")) for row in rows)
    route_reasons = Counter(str((row.get("route") or {}).get("reason", "unknown")) for row in rows)
    statuses = Counter(str(row.get("semantic_status", "unknown")) for row in rows)
    errors = Counter(str(row["error_code"]) for row in rows if row.get("error_code"))
    usages = Counter()
    usage_by_backend: dict[str, Counter[str]] = defaultdict(Counter)
    requests_by_backend = Counter()
    model_real_requests = 0
    usage_rows = 0
    for row in rows:
        backend = str((row.get("route") or {}).get("backend", "unknown"))
        requests_by_backend[backend] += 1
        if row.get("model_real"):
            model_real_requests += 1
        usage = _usage(row)
        if usage["total_tokens"] or usage["prompt_tokens"] or usage["completion_tokens"]:
            usage_rows += 1
        for key, value in usage.items():
            usages[key] += value
            usage_by_backend[backend][key] += value

    task_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        task_rows[str(row["task_id"])].append(row)
    task_aggregates: list[dict[str, Any]] = []
    for task_id in sorted(task_rows):
        task_group = task_rows[task_id]
        task_semantic = sum(bool(row.get("semantic_success")) for row in task_group)
        task_engineering = sum(bool(row.get("engineering_ok")) for row in task_group)
        task_aggregates.append(
            {
                "task_id": task_id,
                "text": task_metadata.get(task_id, {}).get("text", task_group[0].get("task_text", "")),
                "categories": task_metadata.get(task_id, {}).get("categories", []),
                "rows": len(task_group),
                "expected_repeats": repeats,
                "semantic_successes": task_semantic,
                "semantic_rate": _rate(task_semantic, len(task_group)),
                "engineering_successes": task_engineering,
                "engineering_rate": _rate(task_engineering, len(task_group)),
                "semantic_statuses": dict(sorted(Counter(str(row.get("semantic_status", "unknown")) for row in task_group).items())),
            }
        )

    task_count = len(task_rows)
    summary: dict[str, Any] = {
        "task_count": task_count,
        "repeats": repeats,
        "row_count": len(rows),
        "expected_row_count": task_count * repeats,
        "request_count": len(rows),
        "semantic_successes": semantic_successes,
        "semantic_rate": _rate(semantic_successes, len(rows)),
        "engineering_successes": engineering_successes,
        "engineering_rate": _rate(engineering_successes, len(rows)),
        "task_macro_semantic_rate": statistics.mean(item["semantic_rate"] for item in task_aggregates),
        "task_macro_engineering_rate": statistics.mean(item["engineering_rate"] for item in task_aggregates),
        "tasks_all_repeats_semantically_successful": sum(item["semantic_successes"] == item["rows"] for item in task_aggregates),
        "semantic_statuses": dict(sorted(statuses.items())),
        "errors": dict(sorted(errors.items())),
        "routing": {
            "backend_counts": dict(sorted(routes.items())),
            "reason_counts": dict(sorted(route_reasons.items())),
            "requests_by_backend": dict(sorted(requests_by_backend.items())),
        },
        "usage": {
            "model_real_requests": model_real_requests,
            "rows_with_usage": usage_rows,
            "prompt_tokens": usages["prompt_tokens"],
            "completion_tokens": usages["completion_tokens"],
            "total_tokens": usages["total_tokens"],
            "by_backend": {key: dict(sorted(value.items())) for key, value in sorted(usage_by_backend.items())},
            "estimated_usd": None,
            "estimated_usd_note": "Provider pricing was not assumed; token counts are retained from raw model responses.",
        },
        "latency": {
            "field": "external_elapsed_ms",
            "scope": "external:elapsed_plus_probe" if any(
                row.get("latency_scope") == "external:elapsed_plus_probe" for row in rows
            ) else "external:elapsed_ms",
            "rows_with_latency": len(latencies),
            "p50_external_elapsed_ms": _percentile(latencies, 50),
            "p95_external_elapsed_ms": _percentile(latencies, 95),
            "probe_time_included": any(
                row.get("latency_scope") == "external:elapsed_plus_probe" for row in rows
            ),
        },
        "task_aggregates": task_aggregates,
    }
    summary["p50_external_elapsed_ms"] = summary["latency"]["p50_external_elapsed_ms"]
    summary["p95_external_elapsed_ms"] = summary["latency"]["p95_external_elapsed_ms"]
    summary["total_tokens"] = usages["total_tokens"]
    return summary


def _category_summary(
    rows: list[dict[str, Any]], task_metadata: Mapping[str, Mapping[str, Any]], repeats: int
) -> dict[str, dict[str, Any]]:
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        categories = task_metadata.get(str(row["task_id"]), {}).get("categories", [])
        for category in categories:
            by_category[category].append(row)
    result: dict[str, dict[str, Any]] = {}
    for category, category_rows in sorted(by_category.items()):
        task_ids = {str(row["task_id"]) for row in category_rows}
        semantic = sum(bool(row.get("semantic_success")) for row in category_rows)
        engineering = sum(bool(row.get("engineering_ok")) for row in category_rows)
        result[category] = {
            "task_count": len(task_ids),
            "repeats": repeats,
            "row_count": len(category_rows),
            "semantic_successes": semantic,
            "semantic_rate": _rate(semantic, len(category_rows)),
            "engineering_successes": engineering,
            "engineering_rate": _rate(engineering, len(category_rows)),
        }
    return result


def analyze(
    report: Mapping[str, Any],
    development_tasks: list[dict[str, Any]],
    holdout_tasks: list[dict[str, Any]],
    *,
    manifest: Mapping[str, Any] | None = None,
    report_path: str | Path | None = None,
    development_path: str | Path | None = None,
    holdout_path: str | Path | None = None,
) -> dict[str, Any]:
    overlaps = find_text_overlap(development_tasks, holdout_tasks)
    manifest_validation = _validate_manifest(manifest, holdout_tasks, holdout_path) if manifest is not None and holdout_path is not None else None
    overlap_ids = {item["holdout_id"] for item in overlaps}
    retained_ids = {_task_id(row) for row in holdout_tasks} - overlap_ids
    repeats = int(report.get("repeats", 1))
    task_metadata = _task_metadata(holdout_tasks, retained_ids)
    if retained_ids != set(task_metadata):
        raise ValueError("holdout task metadata has duplicate or missing IDs")
    all_rows = report.get("rows")
    if not isinstance(all_rows, list):
        raise ValueError("report rows must be a list")
    strategy_names = sorted({str(row.get("strategy")) for row in all_rows})
    strategies: dict[str, Any] = {}
    for strategy in strategy_names:
        rows = [row for row in all_rows if str(row.get("strategy")) == strategy and str(row.get("task_id")) in retained_ids]
        summary = _summarize_rows(rows, task_metadata=task_metadata, repeats=repeats)
        declared_scope = report.get("strategies", {}).get(strategy, {}).get("latency_scope")
        if declared_scope:
            summary["latency"]["scope"] = declared_scope
            summary["latency"]["probe_time_included"] = declared_scope == "external:elapsed_plus_probe"
        summary["categories"] = _category_summary(rows, task_metadata, repeats)
        strategies[strategy] = summary

    baseline = strategies.get("rules")
    comparisons: dict[str, Any] = {}
    if baseline is not None:
        for strategy, summary in strategies.items():
            if strategy == "rules":
                continue
            comparisons[strategy] = _metric_comparison(summary, baseline)
    adaptive_comparisons: dict[str, Any] = {}
    adaptive = strategies.get("adaptive")
    if adaptive is not None:
        for baseline_name in ("rules", "local", "cloud"):
            baseline_summary = strategies.get(baseline_name)
            if baseline_summary is not None:
                adaptive_comparisons[baseline_name] = _metric_comparison(adaptive, baseline_summary)

    probe_cost = report.get("probe_cost") if isinstance(report.get("probe_cost"), Mapping) else {}
    return {
        "schema": "ai6g.dual_evaluation_analysis.v1",
        "source": {
            "report": str(report_path) if report_path is not None else None,
            "development_tasks": str(development_path) if development_path is not None else None,
            "holdout_tasks": str(holdout_path) if holdout_path is not None else None,
            "report_schema": report.get("schema"),
        },
        "manifest_validation": manifest_validation,
        "deduplication": {
            "method": "normalized exact text overlap (Unicode NFKC plus whitespace collapse)",
            "development_task_count": len(development_tasks),
            "holdout_task_count": len(holdout_tasks),
            "overlap_count": len(overlaps),
            "overlaps": overlaps,
            "excluded_holdout_ids": sorted(overlap_ids),
            "retained_holdout_ids": sorted(retained_ids),
            "retained_task_count": len(retained_ids),
            "gold_modified": False,
        },
        "boundary": {
            "repeats": repeats,
            "unit_of_analysis": "task_id aggregated across repeats; repeated rows are not independent tasks",
            "retained_rows_per_strategy": len(retained_ids) * repeats,
            "task_latency_field": "external_elapsed_ms",
            "task_latency_scope_by_strategy": {name: item["latency"]["scope"] for name, item in strategies.items()},
            "probe_time_included_by_strategy": {name: item["latency"]["probe_time_included"] for name, item in strategies.items()},
            "probe_cost_reported_separately": True,
            "probe_cost": dict(probe_cost),
            "statistical_significance_claimed": False,
        },
        "strategies": strategies,
        "comparison_to_rules": comparisons,
        "adaptive_comparison_to_baselines": adaptive_comparisons,
    }


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def to_markdown(analysis: Mapping[str, Any]) -> str:
    dedup = analysis["deduplication"]
    boundary = analysis["boundary"]
    strategies = analysis["strategies"]
    lines = [
        "# 双模型评测去重敏感性审计",
        "",
        f"本报告由 `analyze_dual.py` 从 `{analysis['source']['report']}` 派生。原始报告和 gold 未修改；重复观测按 `task_id` 聚合，不能视为独立样本。",
        "",
        "## 数据边界与文本去重",
        "",
        f"- 开发集：{dedup['development_task_count']} 条；留出集：{dedup['holdout_task_count']} 条。",
        f"- 按 Unicode NFKC 与空白折叠后的精确文本重叠：{dedup['overlap_count']} 条；排除留出 ID：{', '.join(dedup['excluded_holdout_ids']) or '无'}。",
        f"- 去重后：{dedup['retained_task_count']} 条任务；每策略 {boundary['retained_rows_per_strategy']} 行（{boundary['repeats']} 次重复）。",
        f"- 任务时延使用 `external_elapsed_ms`，各策略范围：`{json.dumps(boundary['task_latency_scope_by_strategy'], ensure_ascii=False, sort_keys=True)}`。标记为 `external:elapsed_plus_probe` 的策略包含同步健康刷新；探针请求和 token 成本单列。",
        "- 本审计不做显著性检验，也不把 3 次重复当作 3 个独立任务。",
        "",
        "重叠明细：",
        "",
        "| 留出 ID | 开发 ID | 文本 |",
        "|---|---|---|",
    ]
    for item in dedup["overlaps"]:
        lines.append(f"| {item['holdout_id']} | {', '.join(item['development_ids'])} | {item['text']} |")
    lines += ["", "## 策略总体结果", "", "行级率用于保留每次重复的观察；任务宏平均先在每个 task_id 内聚合重复，再对任务取平均。", "", "| 策略 | 任务数 | 行数 | 行级语义率 | 任务宏语义率 | 行级工程率 | 任务宏工程率 | P50 外部时延 ms | P95 外部时延 ms | 总 token |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for strategy, summary in strategies.items():
        lines.append(
            f"| {strategy} | {summary['task_count']} | {summary['row_count']} | "
            f"{_fmt(summary['semantic_rate'] * 100, 2)}% | {_fmt(summary['task_macro_semantic_rate'] * 100, 2)}% | "
            f"{_fmt(summary['engineering_rate'] * 100, 2)}% | {_fmt(summary['task_macro_engineering_rate'] * 100, 2)}% | "
            f"{_fmt(summary['p50_external_elapsed_ms'])} | {_fmt(summary['p95_external_elapsed_ms'])} | {summary['total_tokens']} |"
        )

    lines += ["", "## 相对 rules 的收益与不收益", "", "正值的语义/工程率差表示百分点收益；时延和 token 的方向按更低更好解释。结果是描述性比较，不代表统计显著。", "", "| 策略 | 语义率 Δ pp | 工程率 Δ pp | P50 时延 Δ ms | P95 时延 Δ ms | token Δ |", "|---|---:|---:|---:|---:|---:|"]
    for strategy, metrics in analysis["comparison_to_rules"].items():
        lines.append(
            f"| {strategy} | {_fmt(metrics['semantic_rate']['delta'] * 100 if metrics['semantic_rate']['delta'] is not None else None, 2)} | "
            f"{_fmt(metrics['engineering_rate']['delta'] * 100 if metrics['engineering_rate']['delta'] is not None else None, 2)} | "
            f"{_fmt(metrics['p50_external_elapsed_ms']['delta'])} | {_fmt(metrics['p95_external_elapsed_ms']['delta'])} | "
            f"{_fmt(metrics['total_tokens']['delta'], 0)} |"
        )

    lines += ["", "### adaptive 相对固定模型", "", "该表补充 adaptive 与固定 local/cloud 的描述性差异；不能解释为因果或显著性结论。", "", "| 对照基线 | adaptive 语义率 Δ pp | adaptive 工程率 Δ pp | adaptive P50 时延 Δ ms | adaptive P95 时延 Δ ms | adaptive token Δ |", "|---|---:|---:|---:|---:|---:|"]
    for baseline_name, metrics in analysis["adaptive_comparison_to_baselines"].items():
        lines.append(
            f"| {baseline_name} | {_fmt(metrics['semantic_rate']['delta'] * 100 if metrics['semantic_rate']['delta'] is not None else None, 2)} | "
            f"{_fmt(metrics['engineering_rate']['delta'] * 100 if metrics['engineering_rate']['delta'] is not None else None, 2)} | "
            f"{_fmt(metrics['p50_external_elapsed_ms']['delta'])} | {_fmt(metrics['p95_external_elapsed_ms']['delta'])} | "
            f"{_fmt(metrics['total_tokens']['delta'], 0)} |"
        )

    lines += ["", "## 按类别结果", "", "下表按任务 gold 中的类别聚合；一条任务可属于多个类别，因此类别行不能相加为总任务数。", "", "| 类别 | 任务数 | rules 语义率 | local 语义率 | cloud 语义率 | adaptive 语义率 |", "|---|---:|---:|---:|---:|---:|"]
    categories = sorted({category for summary in strategies.values() for category in summary["categories"]})
    for category in categories:
        task_count = next((strategies[name]["categories"][category]["task_count"] for name in strategies if category in strategies[name]["categories"]), 0)
        cells = [category, str(task_count)]
        for strategy in ("rules", "local", "cloud", "adaptive"):
            value = strategies.get(strategy, {}).get("categories", {}).get(category, {}).get("semantic_rate")
            cells.append("—" if value is None else f"{value * 100:.2f}%")
        lines.append("| " + " | ".join(cells) + " |")

    lines += ["", "## 路由、错误与请求成本", ""]
    for strategy, summary in strategies.items():
        lines += [f"### {strategy}", "", f"- 路由后端：`{json.dumps(summary['routing']['backend_counts'], ensure_ascii=False, sort_keys=True)}`。", f"- 路由原因：`{json.dumps(summary['routing']['reason_counts'], ensure_ascii=False, sort_keys=True)}`。", f"- 错误：`{json.dumps(summary['errors'], ensure_ascii=False, sort_keys=True)}`。", f"- 请求数 {summary['request_count']}，真实模型请求 {summary['usage']['model_real_requests']}，prompt/completion/total token = {summary['usage']['prompt_tokens']}/{summary['usage']['completion_tokens']}/{summary['usage']['total_tokens']}。", ""]

    lines += ["## 探针边界", "", f"各策略任务时延是否包含同步探针：`{json.dumps(boundary['probe_time_included_by_strategy'], sort_keys=True)}`。探针请求和 token 成本单列，不重复加入任务 token：请求 {boundary['probe_cost'].get('probe_requests', '—')} 次，总外部耗时 {_fmt(boundary['probe_cost'].get('total_external_elapsed_ms'))} ms，总 token {boundary['probe_cost'].get('total_tokens', '—')}。", ""]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit dual-model evaluation with text-overlap deduplication.")
    parser.add_argument("--report", required=True, help="evaluation_dual_v3.json")
    parser.add_argument("--development", required=True, help="development tasks.jsonl")
    parser.add_argument("--holdout", required=True, help="independent holdout tasks_test_v1.jsonl")
    parser.add_argument("--manifest", required=True, help="taskset_manifest.json used to verify holdout identity")
    parser.add_argument("--out", required=True, help="output stem or .json path; .md is written beside it")
    args = parser.parse_args(argv)
    report = _read_json(args.report)
    development = _read_jsonl(args.development)
    holdout = _read_jsonl(args.holdout)
    manifest = _read_json(args.manifest)
    analysis = analyze(
        report,
        development,
        holdout,
        manifest=manifest,
        report_path=args.report,
        development_path=args.development,
        holdout_path=args.holdout,
    )
    output = Path(args.out)
    json_path = output if output.suffix.lower() == ".json" else output.with_suffix(".json")
    markdown_path = json_path.with_suffix(".md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(to_markdown(analysis), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "markdown": str(markdown_path), "retained_task_count": analysis["deduplication"]["retained_task_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
