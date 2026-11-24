"""Real model health probes for the AI6G runtime.

The monitor calls each configured OpenAI-compatible backend directly with a
dedicated probe request.  The response is never passed to ``TaskRuntime`` and
therefore cannot execute a device action.  A health observation is published
only when a calibration file supplies an explicit quality score and source
hash; missing calibration is reported as unknown rather than filled with a
made-up default.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
import math
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ai6g_runtime.models import OpenAICompatibleBackend
    from ai6g_runtime.router import ResourceObservation
else:
    from .models import OpenAICompatibleBackend
    from .router import ResourceObservation


HEALTH_PROBE_TEXT = (
    "AI6G_HEALTH_PROBE: reply with a JSON clarification object only; "
    "do not operate devices and do not infer or execute any user request."
)
RESOURCE_COST_NOTE = (
    "Each health probe sends one real model request per configured backend; "
    "probe latency, tokens, bandwidth, and provider cost count toward runtime resources."
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class CalibrationEntry:
    """An explicit quality calibration for one backend."""

    quality_score: float
    source_sha256: str
    quality_by_family: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        score = self.quality_score
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0.0 <= float(score) <= 1.0:
            raise ValueError("quality_score must be a number between 0 and 1")
        if not isinstance(self.source_sha256, str) or not _SHA256_RE.fullmatch(self.source_sha256):
            raise ValueError("source_sha256 must be a 64-character SHA-256 hex string")
        if not isinstance(self.quality_by_family, dict) or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1
            for v in self.quality_by_family.values()
        ):
            raise ValueError("family quality must be measured finite probabilities")


def load_calibration(path: str | Path) -> dict[str, CalibrationEntry]:
    """Load strict per-backend quality calibration from JSON.

    Both ``{"backends": {...}}`` and a direct backend mapping are accepted.
    Every entry must include ``quality_score`` and ``source_sha256``; the
    monitor never invents a score when an entry is absent.
    """

    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("calibration JSON must be an object")
    raw_backends = payload.get("backends", payload)
    if not isinstance(raw_backends, Mapping):
        raise ValueError("calibration backends must be an object")
    result: dict[str, CalibrationEntry] = {}
    for backend, raw in raw_backends.items():
        if not isinstance(backend, str) or not backend.strip():
            raise ValueError("calibration backend names must be non-empty strings")
        if not isinstance(raw, Mapping):
            raise ValueError(f"calibration entry for {backend} must be an object")
        try:
            result[backend] = CalibrationEntry(
                quality_score=raw["quality_score"],
                source_sha256=raw["source_sha256"],
                quality_by_family=dict(raw.get("quality_by_family", {})),
            )
        except KeyError as exc:
            raise ValueError(f"calibration entry for {backend} is missing {exc.args[0]}") from exc
    return result


def calibration_file_sha256(path: str | Path) -> str:
    """Return the hash of the calibration file bytes for evidence metadata."""

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@dataclass(frozen=True)
class ProbeReport:
    backend: str
    reachable: bool
    model_real: bool
    latency_ms: float
    checked_at: float
    quality_score: float | None
    calibration_source_sha256: str | None
    published: bool
    detail: str
    error: str | None = None
    skipped: bool = False
    resources: dict[str, Any] | None = None
    resource_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProbeTask:
    """Minimal non-actuating task object passed to a model adapter."""

    text: str = HEALTH_PROBE_TEXT
    task_class: str = "simple"
    intent: str = "health_probe"
    location: str | None = None
    rule_eligible: bool = False


class HealthMonitor:
    """Probe model backends and publish timestamped health observations."""

    def __init__(
        self,
        *,
        health_url: str,
        backends: Mapping[str, Any],
        calibration: Mapping[str, CalibrationEntry | Mapping[str, Any]] | None = None,
        timeout_s: float = 5.0,
        ttl_s: float = 5.0,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.perf_counter,
        resource_provider: Callable[[str], Mapping[str, Any] | None] | None = None,
    ) -> None:
        if not isinstance(health_url, str) or not health_url.strip():
            raise ValueError("health_url must be non-empty")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if ttl_s < 0:
            raise ValueError("ttl_s must be non-negative")
        self.health_url = health_url
        self.backends = dict(backends)
        self.timeout_s = float(timeout_s)
        self.ttl_s = float(ttl_s)
        self.clock = clock
        self.monotonic = monotonic
        if resource_provider is not None and not callable(resource_provider):
            raise ValueError("resource_provider must be callable")
        self.resource_provider = resource_provider
        self.calibration = self._normalize_calibration(calibration or {})
        # Bypass ambient HTTP proxy settings for localhost and explicitly
        # configured private endpoints, matching the model adapter behavior.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._last_checked: dict[str, float] = {}

    @staticmethod
    def _normalize_calibration(
        calibration: Mapping[str, CalibrationEntry | Mapping[str, Any]],
    ) -> dict[str, CalibrationEntry]:
        result: dict[str, CalibrationEntry] = {}
        for backend, entry in calibration.items():
            if isinstance(entry, CalibrationEntry):
                result[str(backend)] = entry
                continue
            if not isinstance(entry, Mapping):
                raise ValueError(f"calibration entry for {backend} must be an object")
            try:
                result[str(backend)] = CalibrationEntry(
                    quality_score=entry["quality_score"],
                    source_sha256=entry["source_sha256"],
                    quality_by_family=dict(entry.get("quality_by_family", {})),
                )
            except KeyError as exc:
                raise ValueError(f"calibration entry for {backend} is missing {exc.args[0]}") from exc
        return result

    def _timestamp(self) -> float:
        """Use an observation timestamp from the clock, never a future forecast."""

        value = float(self.clock())
        # A production clock is wall time.  Clamping protects the health API if
        # a misconfigured clock returns a future value; tests may inject a
        # deterministic past clock without affecting this behavior.
        return min(value, time.time())

    def _publish(self, payload: Mapping[str, Any]) -> tuple[bool, str | None]:
        request = urllib.request.Request(
            self.health_url,
            data=json.dumps(dict(payload), ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(request, timeout=self.timeout_s) as response:
                status_value = getattr(response, "status", None)
                status = int(status_value if status_value is not None else response.getcode())
                response.read()
            if 200 <= status < 300:
                return True, None
            return False, f"health endpoint returned HTTP {status}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return False, f"health publish failed: {exc}"

    def _probe(self, name: str, backend: Any) -> ProbeReport:
        started = self.monotonic()
        result: Any = None
        error: str | None = None
        try:
            # This direct backend call is deliberately outside TaskRuntime; a
            # probe response is observed and discarded, so no device action can
            # be executed as a side effect of health monitoring.
            result = backend.infer(ProbeTask(), timeout_s=self.timeout_s)
            if not bool(getattr(result, "ok", False)):
                error = str(getattr(result, "error", None) or "probe backend returned failure")
        except Exception as exc:  # adapter boundary: a failed probe is data
            error = f"probe exception: {type(exc).__name__}: {exc}"
        latency_ms = max(0.0, (self.monotonic() - started) * 1000.0)
        model_real = bool(result is not None and getattr(result, "ok", False) and getattr(result, "model_real", False))
        reachable = bool(model_real and error is None)
        if not reachable and error is None:
            error = "probe did not produce a real model response"
        checked_at = self._timestamp()
        entry = self.calibration.get(name)
        quality_score = None if entry is None else entry.quality_score
        source_hash = None if entry is None else entry.source_sha256
        detail = f"health_probe model_real={str(model_real).lower()}"
        if error:
            detail += f" error={error}"
        if entry is None:
            detail += "; calibration missing, quality_score unknown, observation not published"
            self._last_checked[name] = checked_at
            return ProbeReport(
                backend=name,
                reachable=reachable,
                model_real=model_real,
                latency_ms=latency_ms,
                checked_at=checked_at,
                quality_score=None,
                calibration_source_sha256=None,
                published=False,
                detail=detail,
                error=error,
            )
        resources, resource_error = self._resource_snapshot(name)
        if resource_error:
            detail += f"; resource telemetry unknown ({resource_error})"
        payload = {
            "backend": name,
            "reachable": reachable,
            "latency_ms": latency_ms,
            "quality_score": entry.quality_score,
            "quality_by_family": entry.quality_by_family,
            "checked_at": checked_at,
            "error_rate": 0.0 if reachable else 1.0,
            "detail": f"{detail}; calibration_source_sha256={entry.source_sha256}",
        }
        if resources is not None:
            payload["resources"] = resources
        published, publish_error = self._publish(payload)
        if publish_error:
            detail += f"; {publish_error}"
            error = error or publish_error
        self._last_checked[name] = checked_at
        return ProbeReport(
            backend=name,
            reachable=reachable,
            model_real=model_real,
            latency_ms=latency_ms,
            checked_at=checked_at,
            quality_score=entry.quality_score,
            calibration_source_sha256=entry.source_sha256,
            published=published,
            detail=detail,
            error=error,
            resources=resources,
            resource_error=resource_error,
        )

    def _resource_snapshot(self, backend: str) -> tuple[dict[str, Any] | None, str | None]:
        if self.resource_provider is None:
            return None, None
        try:
            supplied = self.resource_provider(backend)
            if supplied is None:
                return None, None
            if not isinstance(supplied, Mapping):
                raise ValueError("provider result must be an object")
            observation = ResourceObservation(**dict(supplied))
            if observation.observed_at is None:
                raise ValueError("observed_at is required")
            if all(value is None for value in (observation.queue_ms, observation.free_mem_mb, observation.inflight)):
                return None, None
            return asdict(observation), None
        except Exception as exc:  # telemetry failures do not invalidate model health
            return None, type(exc).__name__

    def run_once(self) -> list[ProbeReport]:
        """Probe every configured backend once, regardless of TTL."""

        return [self._probe(name, backend) for name, backend in self.backends.items()]

    def probe_backend(self, backend: str) -> ProbeReport:
        """Probe one named backend immediately, regardless of TTL."""

        if backend not in self.backends:
            raise KeyError(f"unknown backend: {backend}")
        return self._probe(backend, self.backends[backend])

    def run_due(self, *, now: float | None = None) -> list[ProbeReport]:
        """Probe only backends whose last observation is older than ``ttl_s``."""

        reports: list[ProbeReport] = []
        for name, backend in self.backends.items():
            current = self._timestamp() if now is None else min(float(now), time.time())
            previous = self._last_checked.get(name)
            if previous is not None and current - previous < self.ttl_s:
                reports.append(
                    ProbeReport(
                        backend=name,
                        reachable=False,
                        model_real=False,
                        latency_ms=0.0,
                        checked_at=current,
                        quality_score=None,
                        calibration_source_sha256=None,
                        published=False,
                        detail=f"skipped: observation age {current - previous:.3f}s is below ttl {self.ttl_s:.3f}s",
                        skipped=True,
                    )
                )
                continue
            reports.append(self._probe(name, backend))
        return reports

    def run_forever(self, interval_s: float):
        """Yield due-probe reports indefinitely at the requested poll interval."""

        if interval_s <= 0:
            raise ValueError("interval_s must be positive")
        while True:
            yield self.run_due()
            time.sleep(float(interval_s))


def _build_backends_from_env() -> dict[str, OpenAICompatibleBackend]:
    backends: dict[str, OpenAICompatibleBackend] = {}
    specs = (
        ("local-model", "AI6G_LOCAL_URL", "AI6G_LOCAL_MODEL", "AI6G_LOCAL_API_KEY", "local"),
        ("cloud-model", "AI6G_CLOUD_URL", "AI6G_CLOUD_MODEL", "AI6G_CLOUD_API_KEY", "cloud"),
    )
    for name, url_key, model_key, api_key_key, default_model in specs:
        endpoint = os.getenv(url_key)
        if not endpoint:
            continue
        backends[name] = OpenAICompatibleBackend(
            name,
            endpoint,
            os.getenv(model_key, default_model),
            os.getenv(api_key_key),
        )
    return backends


def _cli_report(reports: Sequence[ProbeReport], *, mode: str) -> dict[str, Any]:
    return {
        "mode": mode,
        "resource_cost": RESOURCE_COST_NOTE,
        "reports": [report.to_dict() for report in reports],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe configured AI6G model backends and publish /v1/health observations.")
    parser.add_argument("--health-url", default=os.getenv("AI6G_HEALTH_URL", "http://127.0.0.1:8090/v1/health"))
    parser.add_argument("--calibration", help="JSON file with per-backend quality_score and source_sha256")
    parser.add_argument("--timeout-s", type=float, default=5.0)
    parser.add_argument("--ttl-s", type=float, default=5.0)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="probe every configured backend once")
    mode.add_argument("--interval", type=float, help="poll every N seconds; probes run only after TTL")
    args = parser.parse_args(argv)
    try:
        calibration = load_calibration(args.calibration) if args.calibration else {}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"invalid calibration: {exc}")
    backends = _build_backends_from_env()
    if not backends:
        parser.error("configure at least one backend with AI6G_LOCAL_URL or AI6G_CLOUD_URL")
    monitor = HealthMonitor(
        health_url=args.health_url,
        backends=backends,
        calibration=calibration,
        timeout_s=args.timeout_s,
        ttl_s=args.ttl_s,
    )
    if args.interval is not None:
        for reports in monitor.run_forever(args.interval):
            print(json.dumps(_cli_report(reports, mode="interval"), ensure_ascii=False), flush=True)
    else:
        reports = monitor.run_once()
        print(json.dumps(_cli_report(reports, mode="once"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
