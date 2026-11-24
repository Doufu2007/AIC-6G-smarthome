"""Reproducible benchmark runner for the AI6G runtime."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ai6g_runtime.router import RouteDecision
else:
    from .router import RouteDecision


@dataclass
class FixedRouter:
    backend: str
    reason: str

    def choose(self, task: Any, now: float | None = None) -> RouteDecision:
        return RouteDecision(
            backend=self.backend,
            reason=self.reason,
            estimated_latency_ms=None,
            quality_score=None,
            observation_age_ms=None,
            refresh_required=False,
        )

    def record_result(self, backend: str, ok: bool, latency_ms: float, now: float | None = None) -> None:
        return None


def load_tasks(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if isinstance(value, str):
            value = {"text": value}
        if not isinstance(value, dict) or not isinstance(value.get("text"), str):
            raise ValueError("each task must be an object with a text field")
        rows.append(value)
    return rows


def _router_for(strategy: str, runtime: Any) -> Any:
    if strategy == "adaptive":
        return runtime.router
    if strategy in {"local", "local-model"}:
        return FixedRouter("local-model", "FIXED_LOCAL")
    if strategy in {"cloud", "cloud-model"}:
        return FixedRouter("cloud-model", "FIXED_CLOUD")
    if strategy in {"rules", "local-rules"}:
        return FixedRouter("local-rules", "FIXED_RULES")
    raise ValueError(f"unknown strategy: {strategy}")


def run(tasks: Iterable[Mapping[str, Any]], runtime: Any, strategy: str = "adaptive") -> dict[str, Any]:
    old_router = runtime.router
    runtime.router = _router_for(strategy, runtime)
    rows = []
    try:
        for index, task in enumerate(tasks):
            text = str(task["text"])
            request_id = f"{strategy}-{task.get('id', index)}"
            rows.append(runtime.handle(text, request_id=request_id))
    finally:
        runtime.router = old_router
    successful = [row for row in rows if row.get("ok") and row.get("execution", {}).get("ok")]
    total_ms = [float(row.get("timing", {}).get("total_ms", 0.0)) for row in rows]
    return {
        "strategy": strategy,
        "n": len(rows),
        "success": len(successful),
        "success_rate": len(successful) / len(rows) if rows else 0.0,
        "p50_total_ms": statistics.median(total_ms) if total_ms else None,
        "max_total_ms": max(total_ms) if total_ms else None,
        "errors": {code: sum(1 for row in rows if row.get("error_code") == code)
                   for code in sorted({row.get("error_code") for row in rows if row.get("error_code")})},
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--strategy", default="rules", choices=("adaptive", "rules", "local", "cloud"))
    args = parser.parse_args(argv)
    if __package__ in (None, ""):
        from ai6g_runtime.executor import DeviceStore
        from ai6g_runtime.models import OpenAICompatibleBackend, RuleBackend
        from ai6g_runtime.router import ObservationCache, TaskRouter
        from ai6g_runtime.runtime import TaskRuntime
    else:
        from .executor import DeviceStore
        from .models import OpenAICompatibleBackend, RuleBackend
        from .router import ObservationCache, TaskRouter
        from .runtime import TaskRuntime
    root = Path(args.out)
    root.parent.mkdir(parents=True, exist_ok=True)
    backends = {"local-rules": RuleBackend()}
    local_url = os.getenv("AI6G_LOCAL_URL")
    cloud_url = os.getenv("AI6G_CLOUD_URL")
    if local_url:
        backends["local-model"] = OpenAICompatibleBackend(
            "local-model", local_url, os.getenv("AI6G_LOCAL_MODEL", "local"), os.getenv("AI6G_LOCAL_API_KEY")
        )
    if cloud_url:
        backends["cloud-model"] = OpenAICompatibleBackend(
            "cloud-model", cloud_url, os.getenv("AI6G_CLOUD_MODEL", "cloud"), os.getenv("AI6G_CLOUD_API_KEY")
        )
    runtime = TaskRuntime(
        router=TaskRouter(ObservationCache()),
        backends=backends,
        store=DeviceStore(),
        log_path=root.with_suffix(".jsonl"),
    )
    report = run(load_tasks(args.tasks), runtime, args.strategy)
    root.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
