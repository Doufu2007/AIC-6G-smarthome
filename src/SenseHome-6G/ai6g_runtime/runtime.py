"""End-to-end SenseHome task runtime.

The runtime is the evidence boundary: it timestamps every stage, validates model
plans before execution, and records failures as failures instead of inferring
semantic success from a backend name.
"""
from __future__ import annotations

import json
import time
import uuid
import math
import hashlib
from dataclasses import replace
from pathlib import Path
from threading import Event, RLock
from typing import Any, Mapping

from .executor import DeviceStore, ExecutionResult
from .protocol import ProtocolError, TaskSpec, classify_task, validate_plan


class TaskRuntime:
    def __init__(
        self,
        *,
        router: Any,
        backends: Mapping[str, Any],
        store: DeviceStore | None = None,
        log_path: str | Path | None = None,
        default_deadline_ms: int | None = None,
        trusted_backends: set[str] | None = None,
    ) -> None:
        self.router = router
        self.backends = dict(backends)
        self.store = store or DeviceStore()
        self.log_path = Path(log_path) if log_path else None
        self.default_deadline_ms = default_deadline_ms
        # Trust is deployment configuration, never inferred from a backend label.
        self.trusted_backends = set(trusted_backends or ()) | {"local-rules"}
        self._lock = RLock()
        self._log_lock = RLock()
        self._results: dict[str, dict[str, Any]] = {}
        self._inflight: dict[str, tuple[str, Event]] = {}

    def health(self, backend: str, *, reachable: bool, latency_ms: float,
               quality_score: float, checked_at: float | None = None,
               error_rate: float = 0.0, detail: str = "",
               quality_by_family: Mapping[str, float] | None = None,
               resources: Mapping[str, Any] | None = None) -> None:
        """Publish an observation to routers that expose an ObservationCache."""
        cache = getattr(self.router, "cache", None)
        if cache is None:
            return
        from .router import HealthObservation, ResourceObservation
        resource_observation = None if resources is None else ResourceObservation(**dict(resources))
        cache.put(HealthObservation(
            backend=backend,
            reachable=bool(reachable),
            latency_ms=float(latency_ms),
            quality_score=float(quality_score),
            checked_at=time.time() if checked_at is None else float(checked_at),
            error_rate=float(error_rate),
            detail=detail,
            quality_by_family=dict(quality_by_family or {}),
            resources=resource_observation,
        ))

    @staticmethod
    def _route_dict(decision: Any) -> dict[str, Any]:
        return {
            "backend": str(getattr(decision, "backend", "unknown")),
            "reason": str(getattr(decision, "reason", "unknown")),
            "estimated_latency_ms": getattr(decision, "estimated_latency_ms", None),
            "quality_score": getattr(decision, "quality_score", None),
            "observation_age_ms": getattr(decision, "observation_age_ms", None),
            "refresh_required": bool(getattr(decision, "refresh_required", False)),
            "resource_observation": getattr(decision, "resource_observation", {}),
        }

    @staticmethod
    def _timing(starts: dict[str, float], ended: float) -> dict[str, float]:
        result: dict[str, float] = {}
        previous = starts.get("request", ended)
        for key in ("classify", "route", "model", "validate", "execute", "log"):
            marker = starts.get(key, previous)
            result[f"{key}_ms"] = max(0.0, (marker - previous) * 1000.0)
            previous = marker
        result["total_ms"] = max(0.0, (ended - starts.get("request", ended)) * 1000.0)
        return result

    def _append_log(self, record: dict[str, Any]) -> None:
        if not self.log_path:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def _finish(self, record: dict[str, Any], marks: dict[str, float]) -> dict[str, Any]:
        """Persist one immutable evidence row and expose explicit timing scopes.

        ``total_ms`` is measured through state submission, before persistence;
        ``handler_total_ms`` is returned to the caller and includes the first
        JSONL append.  The row is written once so concurrent evidence cannot be
        corrupted by a post-hoc last-line rewrite.
        """
        with self._log_lock:
            service_ended = time.perf_counter()
            timing = self._timing(marks, service_ended)
            timing["service_ms"] = timing["total_ms"]
            timing["scope"] = "entry_to_state_before_log"
            record["timing"] = timing
            log_started = time.perf_counter()
            record['audit_persisted'] = self.log_path is not None
            record['audit_status'] = 'persisted' if self.log_path is not None else 'not_configured'
            try:
                self._append_log(record)
            except OSError as exc:
                # A committed action must not be disguised as an unexecuted failure.
                record.update(ok=False, error_code='audit_persist_failed', error=str(exc),
                              audit_persisted=False, audit_status='failed',
                              commit_succeeded=bool(record.get('execution', {}).get('ok') or record.get('execution', {}).get('late_commit')))
                try:
                    self.store.mark_audit(record['request_id'], 'failed')
                except OSError:
                    record['audit_status'] = 'unknown'
            else:
                try:
                    self.store.mark_audit(record['request_id'], 'persisted' if self.log_path is not None else 'not_configured')
                except OSError as exc:
                    record.update(ok=False, error_code='audit_unknown', error=str(exc), audit_persisted=False,
                                  audit_status='unknown', commit_succeeded=bool(record.get('execution', {}).get('ok') or record.get('execution', {}).get('late_commit')))
            ended = time.perf_counter()
            record["timing"]["log_ms"] = max(0.0, (ended - log_started) * 1000.0)
            record["timing"]["handler_total_ms"] = max(0.0, (ended - marks["request"]) * 1000.0)
        with self._lock:
            self._results[record["request_id"]] = record
        return record

    @staticmethod
    def _execution_dict(result: ExecutionResult | None, *, changed: bool = False) -> dict[str, Any]:
        if result is None:
            return {"ok": False, "changed": False}
        return {
            "ok": result.ok,
            "request_id": result.request_id,
            "applied_actions": result.applied_actions,
            "error": result.error,
            "idempotent_replay": result.idempotent_replay,
            "late_commit": result.late_commit,
            "commit_succeeded": bool(result.ok or result.late_commit),
            "changed": bool(changed),
        }

    def _early_failure(self, request_id, text, code, detail):
        return {'ok': False, 'request_id': request_id, 'task': text,
                'error_code': code, 'error': detail, 'result_kind': 'failure',
                'execution': {'ok': False, 'changed': False}, 'state': self.state(),
                'route': {}, 'plan': None, 'model_real': False, 'timing': {}}

    def _replay(self, record):
        replay = json.loads(json.dumps(record, ensure_ascii=False))
        replay.setdefault('execution', {}).update(idempotent_replay=True, changed=False)
        replay.update(replay=True, state=self.state(), state_scope='current_at_replay')
        return replay

    def handle(self, text: str, *, request_id: str | None = None,
               now: float | None = None, deadline_at: float | None = None,
               started_at: float | None = None) -> dict[str, Any]:
        if request_id is None:
            request_id = str(uuid.uuid4())
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 256:
            return self._early_failure(str(request_id), text, 'invalid_request_id', 'request_id must be a non-empty string of at most 256 characters')
        if not isinstance(text, str) or not text.strip():
            return self._early_failure(request_id, text, 'invalid_task', 'text must be non-empty')
        text = text.strip()
        key = hashlib.sha256(text.encode('utf8')).hexdigest()
        with self._lock:
            previous = self._results.get(request_id)
            if previous is not None:
                if previous['task'] != text:
                    return self._early_failure(request_id, text, 'request_id_conflict', 'request_id is already bound to different text')
                return self._replay(previous)
            pending = self._inflight.get(request_id)
            if pending is not None:
                if pending[0] != key:
                    return self._early_failure(request_id, text, 'request_id_conflict', 'request_id is in flight with different text')
                event, owner = pending[1], False
            else:
                receipt = self.store.receipt(request_id)
                if receipt is not None:
                    if receipt['request_key'] != key:
                        return self._early_failure(request_id, text, 'request_id_conflict', 'request_id is already bound to different text')
                    record = {'ok': bool(receipt['execution']['ok']), 'request_id': request_id, 'task': text,
                              'execution': receipt['execution'], 'plan': receipt['plan'],
                              'result_kind': receipt['plan']['result_kind'], 'model_real': False,
                              'result_origin': 'durable_receipt', 'route': {'backend': 'durable-replay', 'reason': 'PERSISTED_COMMIT'},
                              'state': self.state(), 'timing': {}, 'timestamp': time.time(),
                              'audit_status': receipt.get('audit_status', 'unknown'),
                              'audit_persisted': receipt.get('audit_status') == 'persisted'}
                    if not record['ok']:
                        record.update(error_code=receipt['execution'].get('error', 'execution_failed'),
                                      error=receipt['execution'].get('error'), commit_succeeded=True)
                    if record['audit_status'] not in ('persisted', 'not_configured'):
                        record.update(ok=False, error_code='audit_unknown',
                                      error='durable action receipt has no confirmed audit record',
                                      commit_succeeded=True)
                    self._results[request_id] = record
                    return self._replay(record)
                event, owner = Event(), True
                self._inflight[request_id] = (key, event)
        if not owner:
            start = time.perf_counter() if started_at is None else float(started_at)
            budget = self.default_deadline_ms if self.default_deadline_ms is not None else classify_task(text).deadline_ms
            until = start+budget/1000
            if deadline_at is not None:
                until = min(until, float(deadline_at))
            if not event.wait(max(0, until-time.perf_counter())):
                return self._early_failure(request_id, text, 'deadline_exceeded', 'deadline exceeded waiting for original request')
            with self._lock:
                return self._replay(self._results[request_id])
        try:
            result = self._handle_once(text, request_id=request_id, now=now,
                                       deadline_at=deadline_at, started_at=started_at)
        except Exception as exc:
            result = self._early_failure(request_id, text, 'runtime_error', f'{type(exc).__name__}: {exc}')
        finally:
            with self._lock:
                # Always wake duplicates, including adapter/logging failures.
                if 'result' not in locals():
                    result = self._early_failure(request_id, text, 'runtime_error', 'request interrupted')
                self._results[request_id] = result
                self._inflight.pop(request_id, None)
                event.set()
        return result

    def _handle_once(self, text: str, *, request_id: str,
                     now: float | None = None, deadline_at: float | None = None,
                     started_at: float | None = None) -> dict[str, Any]:

        start = time.perf_counter() if started_at is None else float(started_at)
        marks: dict[str, float] = {"request": start}
        timestamp = time.time() if now is None else float(now)
        route: dict[str, Any] = {}
        observation: dict[str, Any] = {}
        model_real = False
        execution_result: ExecutionResult | None = None
        error_code: str | None = None
        error_detail: str | None = None
        model_error: Any = None
        raw_response: Any = None
        task: TaskSpec | None = None
        payload: Any = None
        reservation: str | None = None

        def release_reservation() -> None:
            nonlocal reservation
            if reservation is None:
                return
            token, reservation = reservation, None
            self.router.release(token)

        try:
            task = classify_task(text)
            if self.default_deadline_ms is not None:
                task = replace(task, deadline_ms=int(self.default_deadline_ms))
            task_deadline = start + task.deadline_ms / 1000.0
            deadline_at = task_deadline if deadline_at is None else min(float(deadline_at), task_deadline)
            if not math.isfinite(deadline_at) or time.perf_counter() >= deadline_at:
                raise TimeoutError("request budget exhausted before routing")
            marks["classify"] = time.perf_counter()
            route_task = replace(task, deadline_ms=max(0, int((deadline_at - time.perf_counter()) * 1000)))
            if callable(getattr(self.router, "choose_and_reserve", None)):
                decision, reservation = self.router.choose_and_reserve(
                    route_task, now=time.time() if now is None else timestamp
                )
            else:
                decision = self.router.choose(route_task, now=time.time() if now is None else timestamp)
            route = self._route_dict(decision)
            observation = {
                "age_ms": route.get("observation_age_ms"),
                "refresh_required": route.get("refresh_required", False),
            }
            marks["route"] = time.perf_counter()
            backend_name = route.get("backend")
            privacy = str(getattr(task, "privacy", "standard"))
            if privacy in {"strict", "local", "local_only"} and backend_name not in self.trusted_backends:
                error_code = "privacy_violation"
                error_detail = "strict privacy task requires an explicitly trusted deployment backend"
            else:
                backend = self.backends.get(backend_name)
            if error_code is not None:
                release_reservation()
            elif backend is None:
                error_code, error_detail = "route_unavailable", f"backend not configured: {backend_name}"
                release_reservation()
            else:
                remaining_s = deadline_at - time.perf_counter()
                if remaining_s <= 0:
                    raise TimeoutError("request budget exhausted before inference")
                try:
                    model_result = backend.infer(task, timeout_s=remaining_s)
                finally:
                    release_reservation()
                marks["model"] = time.perf_counter()
                model_real = bool(getattr(model_result, "model_real", False))
                payload = getattr(model_result, "payload", None)
                model_error = getattr(model_result, "error", None)
                raw_response = getattr(model_result, "raw_response", None)
                model_ok = bool(getattr(model_result, "ok", False))
                if time.perf_counter() >= deadline_at:
                    error_code, error_detail = "deadline_exceeded", "model response exceeded task deadline"
                elif not model_ok:
                    error_code = "model_unavailable" if "unavailable" in str(model_error).lower() or "endpoint" in str(model_error).lower() else "model_failed"
                    error_detail = str(model_error or "model returned failure")
                else:
                    try:
                        plan = validate_plan(payload, task)
                    except (ProtocolError, TypeError, ValueError) as exc:
                        error_code, error_detail = "invalid_model_output", str(exc)
                    marks["validate"] = time.perf_counter()
                    if time.perf_counter() >= deadline_at:
                        error_code, error_detail = "deadline_exceeded", "request budget exhausted before state commit"
                    if error_code is None:
                        before = self.store.snapshot()
                        execution_result = self.store.apply(plan, request_id, deadline_at=deadline_at,
                            request_key=hashlib.sha256(text.encode('utf8')).hexdigest())
                        after = self.store.snapshot()
                        changed = before != after
                        marks["execute"] = time.perf_counter()
                        if not execution_result.ok:
                            error_code = ("deadline_exceeded" if execution_result.error == "deadline_exceeded"
                                          else "late_commit" if execution_result.error == "late_commit"
                                          else "execution_failed")
                            error_detail = execution_result.error
                        else:
                            result = {
                                "ok": True,
                                "request_id": request_id,
                                "task": task.text,
                                "task_class": task.task_class,
                                "route": route,
                                "observation": observation,
                                "model_real": model_real,
                                "model_payload": payload,
                                "model_error": model_error,
                                "raw_response": raw_response,
                                "plan": {"actions": [a.__dict__ for a in plan.actions], "explanation": plan.explanation, "confidence": plan.confidence},
                                "execution": self._execution_dict(execution_result, changed=changed),
                                "state": self.state(),
                                "result_kind": plan.result_kind,
                                "timing": {},
                                "timestamp": timestamp,
                            }
                            return self._finish(result, marks)
        except TimeoutError as exc:
            error_code, error_detail = "deadline_exceeded", str(exc)
        except (ProtocolError, TypeError, ValueError) as exc:
            error_code, error_detail = "invalid_task", str(exc)
            marks.setdefault("classify", time.perf_counter())
        except Exception as exc:  # the runtime must return an evidence record on unexpected adapter errors
            error_code, error_detail = "runtime_error", f"{type(exc).__name__}: {exc}"
        finally:
            release_reservation()

        marks.setdefault("classify", time.perf_counter())
        marks.setdefault("route", time.perf_counter())
        marks.setdefault("model", time.perf_counter())
        marks.setdefault("validate", time.perf_counter())
        marks.setdefault("execute", time.perf_counter())
        result = {
            "ok": False,
            "request_id": request_id,
            "task": getattr(task, "text", text),
            "task_class": getattr(task, "task_class", None),
            "route": route,
            "observation": observation,
            "model_real": model_real,
            "model_payload": payload,
            "model_error": model_error,
            "raw_response": raw_response,
            "error_code": error_code or "runtime_error",
            "error": error_detail,
            "plan": None,
            "execution": self._execution_dict(execution_result,
                                               changed=bool(execution_result and execution_result.late_commit)),
            "state": self.state(),
            "result_kind": "failure",
            "timing": self._timing(marks, time.perf_counter()),
            "timestamp": timestamp,
        }
        return self._finish(result, marks)

    def state(self) -> dict[str, Any]:
        return self.store.snapshot()

    def logs(self) -> list[dict[str, Any]]:
        if not self.log_path or not self.log_path.exists():
            return []
        rows = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows
