"""Recomputable statistics; failures remain in latency and deadline populations."""
from __future__ import annotations

import math
import random
import statistics
from collections import Counter, defaultdict
from typing import Any, Iterable


def quantile(values: Iterable[float], fraction: float) -> float | None:
    """Hyndman/Fan type 7, also the default linear sample quantile in NumPy."""
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * fraction
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def wilson(successes: int, total: int) -> list[float] | None:
    if not total:
        return None
    z = 1.959963984540054
    p = successes / total
    center = (p + z * z / (2 * total)) / (1 + z * z / total)
    radius = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return [max(0, center - radius), min(1, center + radius)]


def summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [row["total_user_ms"] for row in rows]
    completed = [row for row in rows if row["contract_complete"]]
    meets = sum(bool(row["contract_complete"] and
                     row["total_user_ms"] <= row["deadline_ms"]) for row in rows)
    n = len(rows)
    return {
        "n": n, "p50_ms": quantile(values, .5), "p95_ms": quantile(values, .95),
        "p99_ms": quantile(values, .99),
        "mean_ms": statistics.fmean(values) if n else None,
        "completed_p95_ms": quantile([row["total_user_ms"] for row in completed], .95),
        "contract_completed": len(completed),
        "contract_completion_rate": len(completed) / n if n else None,
        "contract_completion_wilson95": wilson(len(completed), n),
        "deadline_met": meets, "deadline_met_rate": meets / n if n else None,
        "deadline_violation_rate": 1 - meets / n if n else None,
        "deadline_met_wilson95": wilson(meets, n),
        "fallbacks": sum(row.get("fallback_attempted", False) for row in rows),
        "backend_counts": dict(sorted(Counter(row["backend"] for row in rows).items())),
        "reason_counts": dict(sorted(Counter(row["reason"] for row in rows).items())),
        "failure_counts": dict(sorted(Counter(
            row["error"] for row in rows if row.get("error")).items())),
        "p99_tail_observations": max(0, n - math.ceil(.99 * n)),
        "p99_caution": n < 1000,
    }


def bootstrap_mean(values: list[float], seed: int = 9127, n: int = 2000) -> list[float]:
    rng = random.Random(seed)
    samples = [statistics.fmean(rng.choices(values, k=len(values))) for _ in range(n)]
    return [quantile(samples, .025), quantile(samples, .975)]


def paired_effects(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped = defaultdict(dict)
    for row in rows:
        grouped[(row["seed"], row["trace_index"], row["service"])][row["arm"]] = row
    clusters = defaultdict(lambda: defaultdict(list))
    for (seed, _, service), arms in grouped.items():
        joint = arms.get("joint")
        if joint is None:
            continue
        for arm, other in arms.items():
            if arm == "joint":
                continue
            met = lambda row: int(row["contract_complete"] and row["total_user_ms"] <= row["deadline_ms"])
            clusters[(service, arm, "delta_total_ms")][seed].append(
                joint["total_user_ms"] - other["total_user_ms"])
            clusters[(service, arm, "delta_deadline_met_rate")][seed].append(
                met(joint) - met(other))
    effects = []
    for (service, arm, metric), seeds in sorted(clusters.items()):
        values = [statistics.fmean(seed_rows) for seed_rows in seeds.values()]
        effects.append({
            "service": service, "comparator": arm, "metric": metric,
            "direction": "joint minus comparator",
            "estimate": statistics.fmean(values),
            "bootstrap95_seed_cluster": bootstrap_mean(values),
            "seed_clusters": len(values),
            "paired_requests": sum(len(items) for items in seeds.values()),
            "caution": "Few synthetic seed clusters; exploratory, not field inference",
        })
    return effects


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cells = defaultdict(list)
    for row in rows:
        cells[(row["scenario"], row["service"], row["arm"])].append(row)
    arms = defaultdict(list)
    for row in rows:
        arms[(row["service"], row["arm"])].append(row)
    return {
        "method": {
            "quantiles": "linear/type-7; all requests including failures",
            "deadline": "completed contract AND full total_user_ms <= per-service deadline",
            "intervals": "Wilson95 descriptive rates; paired seed-cluster bootstrap 2000 draws",
            "no_field_inference": True,
            "p99_warning": "Tail estimates are exploratory, especially n<1000",
        },
        "cells": [{"scenario": k[0], "service": k[1], "arm": k[2], **summary(v)}
                  for k, v in sorted(cells.items())],
        "overall": [{"service": k[0], "arm": k[1], **summary(v)}
                    for k, v in sorted(arms.items())],
        "paired_effects": paired_effects(rows),
    }
