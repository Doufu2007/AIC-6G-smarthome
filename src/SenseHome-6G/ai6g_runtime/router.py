"""Freshness-aware route selection for local/edge/cloud execution."""
from __future__ import annotations

import threading
import time
import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class ResourceObservation:
    queue_ms: float | None = None
    free_mem_mb: float | None = None
    inflight: int | None = None
    source: str | None = None
    observed_at: float | None = None

    def __post_init__(self) -> None:
        for name in ("queue_ms", "free_mem_mb", "observed_at"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a finite non-negative number")
            try:
                numeric = float(value)
            except (OverflowError, TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be a finite non-negative number") from exc
            if not math.isfinite(numeric) or numeric < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
        if self.inflight is not None and (
            isinstance(self.inflight, bool) or not isinstance(self.inflight, int) or self.inflight < 0
        ):
            raise ValueError("inflight must be a non-negative integer")
        if self.source is not None and (not isinstance(self.source, str) or not self.source.strip()):
            raise ValueError("resource source must be a non-empty string")


@dataclass(frozen=True)
class HealthObservation:
    backend: str
    reachable: bool
    latency_ms: float
    quality_score: float
    checked_at: float
    error_rate: float = 0.0
    detail: str = ""
    quality_by_family: dict[str, float] = field(default_factory=dict)
    resources: ResourceObservation | None = None


class ObservationCache:
    def __init__(self, ttl_s: float = 5.0) -> None:
        self.ttl_s = float(ttl_s)
        if not math.isfinite(self.ttl_s) or self.ttl_s < 0:
            raise ValueError("ttl_s must be finite and non-negative")
        self._items: Dict[str, HealthObservation] = {}
        self._lock = threading.RLock()

    def put(self, observation: HealthObservation) -> None:
        for value in (observation.latency_ms, observation.quality_score, observation.checked_at, observation.error_rate):
            if not math.isfinite(float(value)):
                raise ValueError("health values must be finite")
        if observation.latency_ms < 0 or not 0 <= observation.quality_score <= 1 or not 0 <= observation.error_rate <= 1:
            raise ValueError("health latency/quality/error values out of range")
        if any(not math.isfinite(float(v)) or not 0 <= float(v) <= 1 for v in observation.quality_by_family.values()):
            raise ValueError("family quality values must be finite probabilities")
        if observation.resources is not None and not isinstance(observation.resources, ResourceObservation):
            raise ValueError("resources must be a ResourceObservation")
        with self._lock:
            self._items[observation.backend] = observation

    def get(self, backend: str) -> Optional[HealthObservation]:
        with self._lock:
            return self._items.get(backend)

    def age_s(self, backend: str, now: Optional[float] = None) -> Optional[float]:
        obs = self.get(backend)
        if obs is None:
            return None
        current = time.time() if now is None else float(now)
        return current - obs.checked_at

    def is_fresh(self, backend: str, now: Optional[float] = None) -> bool:
        age = self.age_s(backend, now)
        return age is not None and 0 <= age <= self.ttl_s

    def needs_refresh(self, backend: str, now: Optional[float] = None) -> bool:
        return not self.is_fresh(backend, now)


@dataclass(frozen=True)
class RouteDecision:
    backend: str
    reason: str
    estimated_latency_ms: float
    quality_score: float
    observation_age_ms: Optional[float]
    refresh_required: bool
    resource_observation: dict[str, Any] = field(default_factory=dict)


def _value(task: Any, name: str, default: Any) -> Any:
    value = getattr(task, name, default)
    if hasattr(value, "value"):
        value = value.value
    return value


class TaskRouter:
    """Select a backend using only current task fields and timestamped health."""

    def __init__(
        self,
        cache: Optional[ObservationCache] = None,
        *,
        trusted_backends: set[str] | None = None,
        resource_ttl_s: float = 5.0,
        max_queue_ms: float = 250.0,
        min_free_mem_mb: float = 256.0,
        max_inflight: int = 8,
        switch_margin_ms: float = 10.0,
    ) -> None:
        self.cache = cache or ObservationCache()
        self.trusted_backends = set(trusted_backends or ()) | {"local-rules"}
        numeric_limits = (resource_ttl_s, max_queue_ms, min_free_mem_mb, switch_margin_ms)
        if any(isinstance(value, bool) or not math.isfinite(float(value)) or float(value) < 0 for value in numeric_limits):
            raise ValueError("resource and switch limits must be finite and non-negative")
        if isinstance(max_inflight, bool) or not isinstance(max_inflight, int) or max_inflight < 1:
            raise ValueError("max_inflight must be a positive integer")
        self.resource_ttl_s = float(resource_ttl_s)
        self.max_queue_ms = float(max_queue_ms)
        self.min_free_mem_mb = float(min_free_mem_mb)
        self.max_inflight = max_inflight
        self.switch_margin_ms = float(switch_margin_ms)
        self._lock = threading.RLock()
        self._last_backend: dict[tuple[str, str, str], str] = {}
        self._reservations: dict[str, str] = {}
        self._active_by_backend: dict[str, int] = {}

    def choose(self, task: Any, now: Optional[float] = None) -> RouteDecision:
        with self._lock:
            return self._choose_locked(task, now)

    def choose_and_reserve(self, task: Any, now: Optional[float] = None) -> tuple[RouteDecision, str | None]:
        """Choose a model route and atomically reserve its Runtime capacity."""
        with self._lock:
            decision = self._choose_locked(task, now)
            if decision.backend not in {"local-model", "cloud-model"}:
                return decision, None
            token = uuid.uuid4().hex
            self._reservations[token] = decision.backend
            self._active_by_backend[decision.backend] = self._active_by_backend.get(decision.backend, 0) + 1
            return decision, token

    def release(self, token: str | None) -> None:
        if token is None:
            return
        with self._lock:
            backend = self._reservations.pop(token, None)
            if backend is None:
                return
            active = self._active_by_backend.get(backend, 0) - 1
            if active > 0:
                self._active_by_backend[backend] = active
            else:
                self._active_by_backend.pop(backend, None)

    def inflight_snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self._active_by_backend)

    def _resource_status(self, observation: HealthObservation, backend: str, now: float) -> dict[str, Any]:
        resource = observation.resources
        runtime_inflight = self._active_by_backend.get(backend, 0)
        if resource is None:
            return {
                "backend": backend, "used": False, "fresh": False, "source": None,
                "observed_at": None, "age_ms": None, "queue_ms": None,
                "free_mem_mb": None, "inflight": None, "runtime_inflight": runtime_inflight,
            }
        age_s = None if resource.observed_at is None else now - float(resource.observed_at)
        fresh = age_s is not None and 0 <= age_s <= self.resource_ttl_s
        values_present = any(value is not None for value in (resource.queue_ms, resource.free_mem_mb, resource.inflight))
        return {
            "backend": backend,
            "used": bool(fresh and values_present),
            "fresh": bool(fresh),
            "source": resource.source,
            "observed_at": resource.observed_at,
            "age_ms": None if age_s is None else age_s * 1000.0,
            "queue_ms": resource.queue_ms,
            "free_mem_mb": resource.free_mem_mb,
            "inflight": resource.inflight,
            "runtime_inflight": runtime_inflight,
        }

    def _resource_pressure(self, status: dict[str, Any]) -> tuple[bool, bool]:
        external_pressure = status["fresh"] and (
            (status["queue_ms"] is not None and status["queue_ms"] > self.max_queue_ms)
            or (status["free_mem_mb"] is not None and status["free_mem_mb"] < self.min_free_mem_mb)
            or (status["inflight"] is not None and status["inflight"] >= self.max_inflight)
        )
        runtime_pressure = status["runtime_inflight"] >= self.max_inflight
        return bool(external_pressure or runtime_pressure), bool(external_pressure)

    def _choose_locked(self, task: Any, now: Optional[float] = None) -> RouteDecision:
        current = time.time() if now is None else float(now)
        task_class = str(_value(task, "task_class", "complex"))
        family = str(_value(task, "family", "general"))
        privacy = str(_value(task, "privacy", "standard"))
        route_key = (task_class, family, privacy)
        rule_eligible = bool(_value(task, "rule_eligible", False))
        if _value(task, "policy_result", None):
            return RouteDecision("local-rules", "SAFETY_POLICY_" + str(task.policy_result).upper(), 0.0, 1.0, 0.0, False)
        if rule_eligible:
            return RouteDecision("local-rules", "TASK_SIMPLE_EXPLICIT_SINGLE_ACTION", 0.0, 1.0, 0.0, False)
        if task_class == "safety":
            # Safety language is handled by the deterministic local path, which
            # returns an explicit clarification/refusal with no actuator actions.
            return RouteDecision("local-rules", "SAFETY_LOCAL_CLARIFICATION", 0.0, 1.0, 0.0, False)
        if task_class == "simple" and not rule_eligible:
            # Ambiguous, negated, or multi-action commands must use a model.
            pass
        if privacy in ("strict", "local", "local_only"):
            allowed = tuple(name for name in ("local-model", "cloud-model") if name in self.trusted_backends)
        else:
            allowed = ("local-model", "cloud-model")

        required = float(_value(task, "quality_required", _value(task, "quality_floor", _value(task, "quality", 0.75))))
        deadline = float(_value(task, "deadline_ms", 0) or 0)
        candidates = []
        stale_seen = False
        missing_seen = False
        quality_low_seen = False
        resource_rejected_seen = False
        runtime_rejected_seen = False
        rejected_resource_status: dict[str, Any] | None = None
        for name in allowed:
            obs = self.cache.get(name)
            if obs is None:
                missing_seen = True
                continue
            age_s = self.cache.age_s(name, current) or 0.0
            fresh = 0 <= age_s <= self.cache.ttl_s
            stale_seen = stale_seen or not fresh
            quality = obs.quality_by_family.get(family, obs.quality_score)
            if fresh and obs.reachable and quality < required:
                quality_low_seen = True
            if fresh and obs.reachable and quality >= required:
                resource_status = self._resource_status(obs, name, current)
                overloaded, external_pressure = self._resource_pressure(resource_status)
                if overloaded:
                    resource_rejected_seen = resource_rejected_seen or external_pressure
                    runtime_rejected_seen = runtime_rejected_seen or (
                        resource_status["runtime_inflight"] >= self.max_inflight
                    )
                    rejected_resource_status = rejected_resource_status or resource_status
                    continue
                candidates.append((name, obs, age_s * 1000.0, fresh, quality, resource_status))

        if not candidates:
            age = self.cache.age_s("local-model", current)
            if resource_rejected_seen or runtime_rejected_seen:
                reason = "RESOURCE_CAPACITY_UNAVAILABLE"
            else:
                reason = "NO_TRUSTED_BACKEND" if not allowed else (
                    "NO_FRESH_BACKEND_STALE" if stale_seen else (
                        "QUALITY_UNQUALIFIED" if quality_low_seen else "NO_FRESH_BACKEND_UNREACHABLE"
                    )
                )
            return RouteDecision(
                "unavailable",
                reason,
                float("inf"),
                0.0,
                None if age is None else age * 1000.0,
                bool(stale_seen or missing_seen),
                rejected_resource_status or {},
            )

        feasible = [item for item in candidates if deadline <= 0 or item[1].latency_ms <= deadline]
        pool = feasible or candidates
        selected = min(pool, key=lambda item: (item[1].latency_ms, -item[4]))
        hysteresis_used = False
        previous_name = self._last_backend.get(route_key)
        previous = next((item for item in pool if item[0] == previous_name), None)
        if feasible and previous is not None and selected[0] != previous[0]:
            improvement = float(previous[1].latency_ms) - float(selected[1].latency_ms)
            if 0 < improvement < self.switch_margin_ms:
                selected = previous
                hysteresis_used = True
        name, obs, age_ms, fresh, quality, resource_status = selected
        reason = "DEADLINE_FEASIBLE_FASTEST" if feasible else "QUALITY_FEASIBLE_FASTEST"
        if resource_rejected_seen:
            reason = "RESOURCE_CAPACITY_AVOIDED_" + reason
        elif runtime_rejected_seen:
            reason = "RUNTIME_CAPACITY_AVOIDED_" + reason
        if hysteresis_used:
            reason = "HYSTERESIS_" + reason
        if stale_seen:
            reason = "STALE_OBSERVATION_PRESENT_" + reason
        if deadline > 0 and not feasible:
            reason = "NO_CANDIDATE_MEETS_DEADLINE"
        if not fresh:
            reason = "OBSERVATION_STALE_" + reason
        if missing_seen and name == "cloud-model":
            reason = "LOCAL_UNAVAILABLE_" + reason
        self._last_backend[route_key] = name
        return RouteDecision(
            name,
            reason,
            float(obs.latency_ms),
            float(quality),
            age_ms,
            stale_seen or not fresh,
            resource_status,
        )

