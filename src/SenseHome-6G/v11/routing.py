"""Connect the existing TaskRouter to catalog-bound, guarded proposals.

Quality observations must name their calibration source. Observations and model
fixtures are deployment inputs, not evidence of a quality or 6G advantage.
"""
from __future__ import annotations

from dataclasses import asdict
import copy
import math
from pathlib import Path
import re
import sys
import threading
import time
import uuid

RUNTIME_PARENT = str(Path(__file__).resolve().parents[1])
if RUNTIME_PARENT not in sys.path:
    sys.path.insert(0, RUNTIME_PARENT)
from ai6g_runtime.router import TaskRouter, HealthObservation, ResourceObservation
from ai6g_runtime.protocol import TaskSpec
from deadline import BoundedCallPool, CapacityExceeded, DeadlineExceeded, call_with_timeout


_ROUTE_CALL_POOL = BoundedCallPool(max_workers=8, name='deadline-route')


def _reserve_probe_capacity(router, backend, deadline_at):
    """Adapt legacy TaskRouter capacity accounting for an unqualified probe.

    ``choose_and_reserve`` requires fresh health, so it cannot admit the probe
    that obtains that health. Keep this compatibility adapter in one place and
    share the router's exact lock, token map and counts with normal inference.
    An incompatible router disables probes; its normal public routing API is
    otherwise untouched. No fabricated health observation grants capacity.
    """
    lock = getattr(router, '_lock', None)
    tokens = getattr(router, '_reservations', None)
    active = getattr(router, '_active_by_backend', None)
    limit = getattr(router, 'max_inflight', None)
    if (not isinstance(tokens, dict) or not isinstance(active, dict) or
            type(limit) is not int or limit < 1 or
            not callable(getattr(lock, 'acquire', None)) or
            not callable(getattr(lock, 'release', None)) or
            not callable(getattr(router, 'release', None))):
        return None
    remaining = deadline_at - time.monotonic()
    if remaining <= 0:
        return None
    try:
        acquired = lock.acquire(timeout=remaining)
    except (TypeError, ValueError, OverflowError):
        return None
    if not acquired:
        return None
    try:
        used = active.get(backend, 0)
        if type(used) is not int or used < 0 or used >= limit:
            return None
        token = uuid.uuid4().hex
        tokens[token] = backend
        active[backend] = used + 1
        return token
    finally:
        lock.release()


class RoutedPlanner:
    """A single planner interface for rules, calibrated local and cloud models.

    Default operation stays on the verified rule path. Explicit model routing
    uses exact independent parsing unless semantic expansion is explicitly
    enabled. No model candidate grants execution authority.
    """

    def __init__(self, backends, *, router=None, prefer_rules=True,
                 privacy='standard', trusted_backends=(), deadline_ms=3000,
                 quality_floor=.8, calibrations=None, resource_provider=None,
                 now=time.time, allow_model_proposals=False):
        if not isinstance(backends, dict) or set(backends) - {'local-model', 'cloud-model'}:
            raise ValueError('only local-model/cloud-model backend names are supported')
        if any(not callable(value) for value in backends.values()):
            raise ValueError('model backends must be callable')
        if privacy not in ('standard', 'strict', 'local', 'local_only'):
            raise ValueError('invalid privacy policy')
        if type(deadline_ms) not in (int, float) or not math.isfinite(deadline_ms) or deadline_ms <= 0:
            raise ValueError('deadline_ms must be finite and positive')
        if type(quality_floor) not in (int, float) or not math.isfinite(quality_floor) or not 0 <= quality_floor <= 1:
            raise ValueError('quality_floor must be a finite probability')
        if type(allow_model_proposals) is not bool:
            raise ValueError('allow_model_proposals must be boolean')
        self.backends = dict(backends)
        self.prefer_rules = prefer_rules
        self.allow_model_proposals = allow_model_proposals
        self.privacy = privacy
        self.deadline_ms = float(deadline_ms)
        self.quality_floor = float(quality_floor)
        self.router = router or TaskRouter(trusted_backends=set(trusted_backends))
        self.calibrations = dict(calibrations or {})
        self.resource_provider = resource_provider
        self.now = now
        self._local = threading.local()
        self._probe_lock = threading.RLock()

    @property
    def last_route(self):
        return copy.deepcopy(getattr(self._local, 'route', {}))

    def observe(self, backend, *, reachable, latency_ms, quality_score,
                calibration_source_sha256, checked_at=None, resources=None,
                quality_by_family=None):
        if backend not in self.backends:
            raise ValueError('observation has no configured backend')
        if not isinstance(calibration_source_sha256, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', calibration_source_sha256):
            raise ValueError('explicit calibration source SHA-256 required')
        if type(reachable) is not bool:
            raise ValueError('reachable must be boolean')
        observed = self.now() if checked_at is None else checked_at
        if type(observed) not in (int, float) or not math.isfinite(observed) or observed > self.now():
            raise ValueError('invalid observation timestamp')
        for number in (latency_ms, quality_score):
            if type(number) not in (int, float) or not math.isfinite(number):
                raise ValueError('observations must be finite numbers')
        resource = None if resources is None else ResourceObservation(**resources)
        self.router.cache.put(HealthObservation(
            backend=backend, reachable=reachable, latency_ms=float(latency_ms),
            quality_score=float(quality_score), checked_at=float(observed),
            quality_by_family=dict(quality_by_family or {}), resources=resource,
            detail='calibration_sha256=' + calibration_source_sha256.lower()))

    def _privacy(self, view):
        if self.privacy != 'standard':
            return self.privacy
        if any(word in view['request'] for word in ('不要上传', '不要联网', '禁止联网', '仅在本地', '只在本地')):
            return 'strict'
        return 'standard'

    def _refresh(self, privacy, deadline_at):
        """Explicit configured probes contain no device data or user request."""
        records = []
        remaining = deadline_at - time.monotonic()
        if remaining <= 0 or not self._probe_lock.acquire(timeout=max(0, remaining)):
            return [dict(error_type='DeadlineExceeded', reason='PROBE_LOCK_DEADLINE')]
        try:
            for name, backend in self.backends.items():
                if privacy != 'standard' and name not in self.router.trusted_backends:
                    continue
                if not self.router.cache.needs_refresh(name, self.now()):
                    continue
                calibration = self.calibrations.get(name)
                if not calibration:
                    continue
                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    break
                started = time.perf_counter()
                error = None
                reservation = _reserve_probe_capacity(self.router, name, deadline_at)
                if reservation is None:
                    records.append(dict(backend=name, elapsed_ms=0, error_type='CapacityExceeded',
                        reason='PROBE_CAPACITY_UNAVAILABLE', resource_error=None, metrics={}))
                    continue
                release_in_caller = True
                try:
                    probe_view = dict(request='健康检查：请返回clarification，actions为空。不要操作设备。', catalog=[], states={})
                    remaining = deadline_at - time.monotonic()
                    if remaining <= 0:
                        raise CapacityExceeded('probe budget elapsed before admission')
                    release_in_caller = False
                    _, error, metrics = call_with_timeout(
                        self._invoke_reserved, remaining, backend, probe_view, deadline_at, reservation,
                        pool=_ROUTE_CALL_POOL)
                    reachable = error is None
                    error = None if error is None else type(error).__name__
                except CapacityExceeded:
                    release_in_caller = True
                    records.append(dict(backend=name, elapsed_ms=(time.perf_counter() - started) * 1000,
                        error_type='CapacityExceeded', reason='PROBE_WORKER_CAPACITY_UNAVAILABLE',
                        resource_error=None, metrics={}))
                    continue
                except Exception as exc:
                    reachable = False
                    error = type(exc).__name__
                    metrics = {}
                finally:
                    if release_in_caller:
                        self.router.release(reservation)
                elapsed = (time.perf_counter() - started) * 1000
                resources = None
                resource_error = None
                if self.resource_provider and time.monotonic() < deadline_at:
                    try:
                        resources = call_with_timeout(self.resource_provider,
                            deadline_at - time.monotonic(), name,
                            deadline_at - time.monotonic(), pool=_ROUTE_CALL_POOL)
                    except Exception as exc:
                        resource_error = type(exc).__name__
                self.observe(name, reachable=reachable, latency_ms=elapsed,
                    quality_score=calibration['quality_score'],
                    calibration_source_sha256=calibration['source_sha256'],
                    quality_by_family=calibration.get('quality_by_family'), resources=resources)
                records.append(dict(backend=name, elapsed_ms=elapsed, error_type=error,
                    resource_error=resource_error, metrics=metrics))
        finally:
            self._probe_lock.release()
        return records

    def _invoke_reserved(self, backend, view, deadline_at, reservation):
        try:
            return self._invoke_backend(backend, view, deadline_at)
        finally:
            # Probes and user inference both retain the exact Runtime token
            # through a caller timeout, until their actual worker exits.
            self.router.release(reservation)

    @staticmethod
    def _invoke_backend(backend, view, deadline_at):
        """Capture thread-local metrics in the same worker as the model call."""
        candidate = error = None
        try:
            remaining = deadline_at - time.monotonic()
            if remaining <= 0:
                raise DeadlineExceeded('model deadline elapsed before invocation')
            if hasattr(backend, 'generate'):
                candidate = backend.generate(view, timeout=remaining)
            else:
                candidate = backend(view)
        except Exception as exc:
            error = exc
        return candidate, error, copy.deepcopy(getattr(backend, 'last_metrics', {}))

    def __call__(self, view):
        from catalog_planner import catalog_rules, guard_payload, clarification, request_privacy_mode
        started = time.perf_counter()
        deadline_at = time.monotonic() + self.deadline_ms / 1000
        rules = catalog_rules(copy.deepcopy(view))
        self._local.route = {}

        def finish(payload, route):
            elapsed = (time.perf_counter() - started) * 1000
            route.update(planning_ms=elapsed, budget_ms=self.deadline_ms,
                         deadline_met=elapsed <= self.deadline_ms)
            self._local.route = copy.deepcopy(route)
            return payload

        rule_clarification = rules['result_kind'] == 'clarification'
        privacy_request = request_privacy_mode(view.get('request'))
        if privacy_request == 'offline':
            return finish(clarification('此请求要求完全离线，已停止模型路由'),
                          dict(backend='local-rules', reason='OFFLINE_PRIVACY_BLOCK', privacy='local_only'))
        if privacy_request == 'no_upload':
            return finish(rules, dict(backend='local-rules', reason='NO_UPLOAD_LOCAL_RULES', privacy='strict'))
        if ((rule_clarification and not self.allow_model_proposals) or
                (not rule_clarification and self.prefer_rules)):
            return finish(rules, dict(backend='local-rules', reason='SEMANTIC_GATE' if rules['result_kind'] == 'clarification' else 'RULES_FIRST'))
        privacy = self._privacy(view)
        probes = self._refresh(privacy, deadline_at)
        remaining_ms = max(0, (deadline_at - time.monotonic()) * 1000)
        task = TaskSpec(text=view['request'], task_class='complex', family='catalog',
                        privacy=privacy, rule_eligible=False,
                        deadline_ms=max(1, int(remaining_ms)), quality_floor=self.quality_floor)
        decision, reservation = self.router.choose_and_reserve(task, now=self.now())
        selected = asdict(decision)
        if not math.isfinite(selected['estimated_latency_ms']):
            selected['estimated_latency_ms'] = None
        selected.update(privacy=privacy, probes=probes)
        release_in_caller = True
        try:
            if (decision.backend not in self.backends or remaining_ms <= 0 or
                    decision.estimated_latency_ms > remaining_ms):
                return finish(rules, dict(backend='local-rules', reason='NO_FEASIBLE_MODEL_RULE_FALLBACK', selection=selected))
            backend = self.backends[decision.backend]
            call_started = time.perf_counter()
            def invoke_reserved():
                return self._invoke_reserved(backend, copy.deepcopy(view), deadline_at, reservation)
            try:
                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    raise CapacityExceeded('model budget exhausted before admission')
                release_in_caller = False
                candidate, error, metrics = call_with_timeout(invoke_reserved,
                    remaining, pool=_ROUTE_CALL_POOL)
                if error is not None:
                    selected['metrics'] = metrics
                    raise error
            except CapacityExceeded:
                # Admission failed: no worker exists to release this token.
                release_in_caller = True
                selected.update(model_call_ms=(time.perf_counter() - call_started) * 1000,
                                metrics={})
                return finish(rules, dict(backend='local-rules', reason='MODEL_CAPACITY_RULE_FALLBACK',
                    error_type='CapacityExceeded', selection=selected))
            except Exception as exc:
                selected.update(model_call_ms=(time.perf_counter() - call_started) * 1000,
                                metrics=selected.get('metrics', {}))
                return finish(rules, dict(backend='local-rules', reason='MODEL_ERROR_RULE_FALLBACK',
                    error_type=type(exc).__name__, selection=selected))
            selected.update(model_call_ms=(time.perf_counter() - call_started) * 1000,
                            metrics=metrics)
            if self.allow_model_proposals:
                from catalog_planner import guard_model_proposal
                payload = guard_model_proposal(candidate, view)
            else:
                payload = guard_payload(candidate, view)
            if time.monotonic() > deadline_at:
                payload = clarification('模型规划超过本次时限，请重新请求')
                selected['reason'] = 'ACTUAL_MODEL_DEADLINE_EXCEEDED'
            return finish(payload, selected)
        finally:
            if release_in_caller:
                self.router.release(reservation)
