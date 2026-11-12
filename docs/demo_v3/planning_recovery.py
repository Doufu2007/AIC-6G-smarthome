"""Optional, once-only recovery of a failed planning call on loopback services.

The original C policy still selects the primary slot.  This adapter only plans:
it never executes actions, updates devices, or rewrites the original gateway.
Use the existing no-redirect ``http.client`` backend transports.  A configured
launch budget gates the second invocation; each synchronous backend owns its
own health/inference timeouts and may finish after that budget.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import math
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

NET = Path(__file__).resolve().parents[2] / "SenseHome-6G/net_offload"
if str(NET) not in sys.path:
    sys.path.insert(0, str(NET))
from offload_gateway import BackendResult, OffloadGateway  # noqa: E402
from target_contract import request_goals  # noqa: E402


_TRANSPORT_ERRORS = frozenset({
    "HTTP_503", "HTTP_504", "TimeoutError", "ConnectionRefusedError",
    "ConnectionResetError", "ConnectionAbortedError", "RemoteDisconnected",
    "BrokenPipeError",
})
_UPSTREAM_TRANSPORT_ERRORS = frozenset("UPSTREAM_" + code for code in _TRANSPORT_ERRORS)


def _loopback_url(value: str) -> str:
    """Reject names, alternate encodings, credentials and URL control fields."""
    if not isinstance(value, str) or value != value.strip() or "?" in value or "#" in value:
        raise ValueError("recovery_requires_literal_loopback_http_url")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("recovery_requires_literal_loopback_http_url") from exc
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username is not None or parsed.password is not None
            or any(char.isspace() for char in value) or "\\" in value):
        raise ValueError("recovery_requires_literal_loopback_http_url")
    host = "[::1]" if parsed.hostname == "::1" else "127.0.0.1"
    allowed_netloc = host if port is None else f"{host}:{port}"
    if parsed.netloc != allowed_netloc or (port is not None and not 1 <= port <= 65535):
        raise ValueError("recovery_requires_literal_loopback_http_url")
    return value.rstrip("/")


def _backend_url(backend: Any) -> str:
    """Bind the allowlist to the actual existing transport configuration."""
    url = getattr(backend, "base_url", None)
    if url is None:
        url = getattr(getattr(backend, "config", None), "upstream_url", None)
    if url is None:
        raise ValueError("recovery_backend_requires_explicit_transport_url")
    if getattr(backend, "follow_redirects", False):
        raise ValueError("recovery_transport_must_not_follow_redirects")
    return _loopback_url(url)


def _status(result: BackendResult) -> dict[str, Any]:
    return {"ok": bool(result.ok), "semantic_complete": bool(result.semantic_complete),
            "action_count": len(result.actions), "error": result.error,
            "failure_kind": result.failure_kind, "source": result.source,
            "model_ms": result.model_ms}


def _terminal_response(result: BackendResult) -> bool:
    if result.failure_kind not in (None, "transport_error", "transport_failure"):
        return True
    if isinstance(result.error, str) and re.fullmatch(r"HTTP_[34]\d{2}", result.error):
        return True
    response = result.response
    if response.get("actions") or response.get("semantic_complete"):
        return True
    response_error = response.get("error")
    if isinstance(response_error, str):
        code = response_error.removeprefix("UPSTREAM_")
        if code.startswith(("UNSAFE_", "INVALID_", "CLARIFICATION_", "ACTION_", "MODEL_REFUSAL")) or code == "JSONDecodeError":
            return True
    for key in ("needs_clarification", "requires_clarification", "clarification_required",
                "refusal", "unsafe", "rejected"):
        if response.get(key):
            return True
    return False


def _transient(result: BackendResult) -> bool:
    if _terminal_response(result):
        return False
    if result.error in _TRANSPORT_ERRORS:
        return True
    # DirectModelBackend wraps its upstream transport failures as HTTP 502.
    # A generic 502 can also mean invalid model JSON, so it is never enough.
    return result.error == "HTTP_502" and result.response.get("error") in _UPSTREAM_TRANSPORT_ERRORS


def _resolved_request_without_plan(command: str, result: BackendResult) -> bool:
    # Empty actions are safe, but do not complete a fully specified supported
    # request. Only this bounded grammar can establish that no slot is missing;
    # explicit refusals/clarifications and every uncertain input stay terminal.
    return (result.ok and not result.actions and not result.semantic_complete
            and not result.error and not _terminal_response(result)
            and request_goals(command) is not None)


class PlanningRecoveryBackend:
    """Wrap one policy-selected planner with at most one alternate planner.

    ``primary_url``/``secondary_url`` must match the backend's ``base_url`` or
    ``config.upstream_url``. Existing HttpJsonBackend and DirectModelBackend use
    no-redirect http.client transports. Arbitrary transports must obey the same
    contract; enabling ``follow_redirects`` explicitly is rejected.

    ``recovery_records`` retains adapter-level audit records even when the
    unchanged gateway replaces a failed response with its rule fallback.
    These records describe software execution, not physical-device evidence.
    """
    def __init__(self, primary: Any, secondary: Any, *, primary_name: str,
                 secondary_name: str, primary_url: str, secondary_url: str,
                 launch_budget_ms: float = 120_000,
                 clock: Callable[[], float] = time.monotonic,
                 total_budget_ms: float | None = None,
                 primary_budget_ms: float | None = None):
        if isinstance(primary, PlanningRecoveryBackend) or isinstance(secondary, PlanningRecoveryBackend):
            raise ValueError("recovery_wrappers_cannot_be_nested")
        if primary is secondary or not primary_name or not secondary_name or primary_name == secondary_name:
            raise ValueError("recovery_requires_distinct_planners")
        if (isinstance(launch_budget_ms, bool) or not isinstance(launch_budget_ms, (int, float))
                or not math.isfinite(launch_budget_ms) or launch_budget_ms <= 0):
            raise ValueError("recovery_launch_budget_must_be_positive_finite")
        self.primary, self.secondary = primary, secondary
        self.primary_name, self.secondary_name = primary_name, secondary_name
        self.primary_url, self.secondary_url = _loopback_url(primary_url), _loopback_url(secondary_url)
        if self.primary_url == self.secondary_url:
            raise ValueError("recovery_requires_distinct_endpoints")
        self.launch_budget_ms = float(launch_budget_ms)
        for value in (total_budget_ms, primary_budget_ms):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError('deadline_budget_must_be_positive_finite')
        self.total_budget_ms = float(total_budget_ms) if total_budget_ms is not None else None
        self.primary_budget_ms = float(primary_budget_ms) if primary_budget_ms is not None else (
            self.total_budget_ms * .4 if self.total_budget_ms is not None else None)
        if self.total_budget_ms is not None and self.primary_budget_ms >= self.total_budget_ms:
            raise ValueError('primary_budget_must_leave_reserve_window')
        self.clock = clock
        self.recovery_records: list[dict[str, Any]] = []
        self._validate_endpoints()

    def _validate_endpoints(self) -> None:
        if (_backend_url(self.primary) != self.primary_url
                or _backend_url(self.secondary) != self.secondary_url):
            raise ValueError("recovery_transport_url_does_not_match_allowlist")

    def health(self) -> dict[str, Any]:
        # Do not give the C initial selection synthetic health from a reserve.
        self._validate_endpoints()
        return deepcopy(self.primary.health())

    def resource_snapshot(self):
        snapshot = getattr(self.primary, 'resource_snapshot', None)
        return snapshot() if callable(snapshot) else {}

    @staticmethod
    def _infer(backend: Any, name: str, command: str, context: Any) -> BackendResult:
        try:
            return backend.infer(command, deepcopy(context))
        except Exception as exc:  # Planner exceptions never escape as success.
            return BackendResult(False, False, {}, [], error=type(exc).__name__, source=name)

    def infer(self, command: str, context: Mapping[str, Any] | None = None) -> BackendResult:
        self._validate_endpoints()
        started = self.clock()
        original_context = deepcopy(context)
        original_context = dict(original_context or {})
        deadline = started + self.total_budget_ms / 1000 if self.total_budget_ms is not None else None
        supplied = original_context.get('_deadline_monotonic')
        if isinstance(supplied, (int, float)) and not isinstance(supplied, bool) and math.isfinite(supplied):
            deadline = min(deadline, supplied) if deadline is not None else supplied
        def remaining():
            return None if deadline is None else max(0., deadline - self.clock())
        def timed_context(primary=False):
            value = deepcopy(original_context)
            left = remaining()
            if left is not None:
                value['_deadline_monotonic'] = deadline
                value['_planning_timeout_s'] = min(left, self.primary_budget_ms / 1000) if primary and self.primary_budget_ms is not None else left
            return value
        def expired():
            left = remaining()
            return left is not None and left <= 0
        if expired():
            return BackendResult(False, False, {'error': 'DEADLINE_EXCEEDED'}, [], error='DEADLINE_EXCEEDED')
        primary_started = self.clock()
        primary = self._infer(self.primary, self.primary_name, command, timed_context(primary=True))
        primary_ms = (self.clock() - primary_started) * 1000
        meta: dict[str, Any] = {
            "attempted": False, "from": self.primary_name, "to": self.secondary_name,
            "actual_backend": self.primary_name, "primary_error": primary.error,
            "primary_status": _status(primary), "secondary_status": None,
            "primary_response": deepcopy(dict(primary.response)), "secondary_response": None,
            "secondary_health": None, "primary_ms": round(primary_ms, 3),
            "health_ms": 0.0, "secondary_ms": 0.0, "elapsed_ms": 0.0,
            "budget_ms": self.total_budget_ms or self.launch_budget_ms,
            "budget_scope": "end_to_end_planning" if deadline is not None else "secondary_launch_only",
            "reason": "primary_success" if primary.ok else "not_recoverable",
        }

        def elapsed_ms() -> float:
            return max(0.0, (self.clock() - started) * 1000)

        def finish(result: BackendResult) -> BackendResult:
            if expired():
                meta['reason'] = 'deadline_exceeded'
                result = BackendResult(False, False, {'error': 'DEADLINE_EXCEEDED'}, [],
                                       error='DEADLINE_EXCEEDED', source=meta['actual_backend'],
                                       failure_kind='deadline')
            meta["elapsed_ms"] = round(elapsed_ms(), 3)
            self.recovery_records.append(deepcopy(meta))
            response = {**deepcopy(dict(result.response)), "actual_backend": meta["actual_backend"],
                        "recovery": deepcopy(meta)}
            return replace(result, response=response, actions=deepcopy(result.actions),
                           source=f"{meta['actual_backend']}:{result.source}")

        if OffloadGateway.classify(command) != "complex":
            meta["reason"] = "task_not_complex"
            return finish(primary)
        empty_resolved = _resolved_request_without_plan(command, primary)
        if empty_resolved:
            meta['primary_error'] = 'EMPTY_PLAN_FOR_RESOLVED_REQUEST'
            meta['reason'] = 'primary_returned_no_plan'
        if not empty_resolved and (primary.ok or primary.actions or primary.semantic_complete or not _transient(primary)):
            return finish(primary)
        if expired():
            return finish(primary)
        if elapsed_ms() >= self.launch_budget_ms:
            meta["reason"] = "launch_budget_exhausted"
            return finish(primary)

        self._validate_endpoints()
        health_started = self.clock()
        try:
            timed_health = getattr(self.secondary, 'health_with_timeout', None)
            health = timed_health(min(.5, remaining())) if callable(timed_health) and remaining() is not None else self.secondary.health()
        except Exception as exc:
            health = {"ready": False, "reachable": False, "error": type(exc).__name__}
        meta["health_ms"] = round((self.clock() - health_started) * 1000, 3)
        meta["secondary_health"] = deepcopy(health)
        if not isinstance(health, Mapping) or health.get("ready") is not True or health.get("reachable") is not True:
            meta["reason"] = "secondary_not_ready_or_unreachable"
            return finish(primary)
        if elapsed_ms() >= self.launch_budget_ms:
            meta["reason"] = "launch_budget_exhausted"
            return finish(primary)

        self._validate_endpoints()
        meta["attempted"] = True
        meta["actual_backend"] = self.secondary_name
        secondary_started = self.clock()
        if expired():
            return finish(primary)
        secondary = self._infer(self.secondary, self.secondary_name, command, timed_context())
        meta["secondary_ms"] = round((self.clock() - secondary_started) * 1000, 3)
        meta["secondary_status"] = _status(secondary)
        meta["secondary_response"] = deepcopy(dict(secondary.response))
        meta["reason"] = ('secondary_returned_no_plan' if _resolved_request_without_plan(command, secondary)
                          else "secondary_success" if secondary.ok else "secondary_failed_terminal")
        return finish(secondary)
