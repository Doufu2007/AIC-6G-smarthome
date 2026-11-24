"""Version-isolated paired experiment for persistent resource-aware routing.

Real model outputs may be collected once and replayed under frozen software
impairments. Replay is explicitly not a live multi-arm or physical 6G test.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from ai6g_runtime.evaluation import controlled as v4
    from ai6g_runtime.router import HealthObservation, ObservationCache, ResourceObservation, RouteDecision, TaskRouter
    from ai6g_runtime.models import ModelResult, RuleBackend
    from ai6g_runtime.executor import DeviceStore
    from ai6g_runtime.protocol import classify_task
    from ai6g_runtime.runtime import TaskRuntime
    from ai6g_runtime.health_monitor import ProbeTask
    from ai6g_runtime.evaluator import semantic_score_row
else:
    from . import controlled as v4
    from ..router import HealthObservation, ObservationCache, ResourceObservation, RouteDecision, TaskRouter
    from ..models import ModelResult, RuleBackend
    from ..executor import DeviceStore
    from ..protocol import classify_task
    from ..runtime import TaskRuntime
    from ..health_monitor import ProbeTask
    from ..evaluator import semantic_score_row

MODELS = v4.MODELS
STRATEGIES = ('rules', 'fixed_local', 'legal_cloud', 'static', 'fixed_ttl',
              'adaptive', 'full_resource', 'no_resource', 'no_hysteresis')
OBSERVED = {'static', 'fixed_ttl', 'adaptive', 'full_resource', 'no_resource', 'no_hysteresis'}
RESOURCE = {'full_resource', 'no_hysteresis'}
ADAPTIVE = {'adaptive', 'full_resource', 'no_resource', 'no_hysteresis'}
LOGICAL_EPOCH_MS = 1_700_000_000_000.0


@dataclass(frozen=True)
class ExperimentConfig:
    strategies: tuple[str, ...] = STRATEGIES
    repeats: int = 1
    online: bool = False
    shared_rule_fastpath: bool = True
    shared_safety_fastpath: bool = True
    trusted_backends: tuple[str, ...] = ()
    ttl_ms: float = 5000
    min_ttl_ms: float = 250
    max_ttl_ms: float = 10000
    step_ms: float = 1000
    probe_timeout_ms: float = 3000
    event_latency_ratio: float = 1.5
    bootstrap_samples: int = 2000
    seed: int = 20261104
    input_usd_per_million: float | None = None
    output_usd_per_million: float | None = None
    deadline_ms: int | None = None
    resource_ttl_ms: float = 5000
    max_queue_ms: float = 250
    min_free_mem_mb: float = 256
    max_inflight: int = 8
    switch_margin_ms: float = 10
    limit: int | None = None

    def __post_init__(self):
        if not self.strategies or len(set(self.strategies)) != len(self.strategies) or set(self.strategies)-set(STRATEGIES):
            raise ValueError('strategies must be nonempty, unique and known')
        if self.repeats < 1 or self.bootstrap_samples < 1:
            raise ValueError('positive repeats/bootstrap_samples required')
        for name in ('ttl_ms', 'min_ttl_ms', 'max_ttl_ms', 'step_ms', 'probe_timeout_ms',
                     'resource_ttl_ms', 'max_queue_ms', 'min_free_mem_mb', 'switch_margin_ms'):
            v4._finite(getattr(self, name), name)
        if self.min_ttl_ms > self.max_ttl_ms or self.event_latency_ratio < 1:
            raise ValueError('invalid TTL or event ratio')
        if self.max_inflight < 1 or (self.limit is not None and self.limit < 1):
            raise ValueError('positive max_inflight/limit required')
        if self.deadline_ms is not None:
            v4._finite(self.deadline_ms, 'deadline_ms', 1)
        for name in ('input_usd_per_million', 'output_usd_per_million'):
            if getattr(self, name) is not None:
                v4._finite(getattr(self, name), name)


class ResourceTrace(v4.Trace):
    """Service impairments and separately tagged observable telemetry.

    resources=null means no telemetry; age_ms=null means unknown timestamp.
    Resource values are injected scenario observations, never physical readings.
    Only events with step<=current_step are visible, no future access.
    """
    def __init__(self, path=None):
        super().__init__(path)
        for event in self.events:
            resource = event.get('resources')
            if resource is not None:
                if not isinstance(resource, dict):
                    raise ValueError('resources must be object or null')
                if set(resource)-{'queue_ms', 'free_mem_mb', 'inflight', 'source', 'age_ms'}:
                    raise ValueError('unknown resource field')
                age = resource.get('age_ms', 0)
                if age is not None:
                    v4._finite(age, 'resource age_ms')
                ResourceObservation(**{k: v for k, v in resource.items() if k != 'age_ms'})

    def resources_at(self, step, backend, *, now_ms):
        latest = None
        for event in self.events:
            if event['step'] > step:
                break
            if event['backend'] in (backend, '*') and 'resources' in event:
                latest = event['resources']
        if latest is None:
            return None
        value = dict(latest)
        age = value.pop('age_ms', 0)
        value.setdefault('source', 'injected_resource_scenario')
        value['observed_at'] = None if age is None else max(0, now_ms-age)/1000
        return ResourceObservation(**value)


class PolicySession:
    """One real production router for the entire arm, including hysteresis.

    Only public methods are used for selection/reservations. For restart-only
    restoration, the router's private last-route mapping is serialized; no
    active reservation survives a completed synchronous task.
    """
    def __init__(self, strategy, config, state=None):
        self.strategy, self.config = strategy, config
        self.state = copy.deepcopy(state) if state is not None else v4._initial_policy()
        self.cache = ObservationCache(ttl_s=1e12 if strategy == 'static' else config.ttl_ms/1000)
        margin = config.switch_margin_ms if strategy in {'full_resource', 'no_resource'} else 0
        self.router = TaskRouter(self.cache, trusted_backends=set(config.trusted_backends),
            resource_ttl_s=config.resource_ttl_ms/1000, max_queue_ms=config.max_queue_ms,
            min_free_mem_mb=config.min_free_mem_mb, max_inflight=config.max_inflight,
            switch_margin_ms=margin)
        for item in self.state.get('last_routes', []):
            self.router._last_backend[tuple(item['key'])] = item['backend']
        for data in self.state.get('cache', []):
            entry = dict(data)
            if entry.get('resources') is not None:
                entry['resources'] = ResourceObservation(**entry['resources'])
            self.publish(HealthObservation(**entry))

    def publish(self, observation):
        if self.strategy not in RESOURCE:
            observation = replace(observation, resources=None)
        self.cache.put(observation)

    def snapshot(self):
        result = copy.deepcopy(self.state)
        result['last_routes'] = [{'key': list(key), 'backend': backend}
            for key, backend in sorted(self.router._last_backend.items())]
        result['cache'] = [asdict(obs) for name in MODELS if (obs := self.cache.get(name)) is not None]
        if self.router.inflight_snapshot():
            raise RuntimeError('reservation leaked across completed task')
        return result


class _PolicyRouter:
    def __init__(self, session, request, family):
        self.session, self.request, self.family = session, request, family
        self.cache = session.cache

    def _route(self, task, reserve):
        strategy, config = self.session.strategy, self.session.config
        if strategy == 'rules' or v4._fastpath(task, config):
            return RouteDecision('local-rules', 'UNIFORM_POLICY_RULE_PATH', 0, 1, 0, False), None
        if strategy in OBSERVED:
            overrides = {'family': self.family, 'rule_eligible': False}
            if not config.shared_safety_fastpath:
                overrides['policy_result'] = None
                if task.task_class == 'safety':
                    overrides['task_class'] = 'complex'
            route_task = replace(task, **overrides)
            now = (self.request.start_clock+self.request.elapsed())/1000
            if reserve:
                return self.session.router.choose_and_reserve(route_task, now=now)
            return self.session.router.choose(route_task, now=now), None
        backend = 'local-model' if strategy == 'fixed_local' else 'cloud-model'
        if task.privacy in ('strict', 'local', 'local_only') and backend not in config.trusted_backends:
            backend = 'local-model' if 'local-model' in config.trusted_backends else 'unavailable'
        return RouteDecision(backend, strategy.upper(), None, None, None, False), None

    def choose(self, task, now=None):
        return self._route(task, False)[0]

    def choose_and_reserve(self, task, now=None):
        return self._route(task, True)

    def release(self, token):
        self.session.router.release(token)


def _refresh(request, session, family, calibration):
    strategy, config, state = session.strategy, session.config, session.state
    ttl = state['adaptive_ttl_ms'] or config.ttl_ms
    session.cache.ttl_s = 1e12 if strategy == 'static' else ttl/1000
    strict = request.task.privacy in ('strict', 'local', 'local_only')
    allowed = [name for name in MODELS if not strict or name in config.trusted_backends]
    for name in allowed:
        previous = state['observations'].get(name)
        age = float('inf') if previous is None else request.start_clock-previous['checked_ms']
        due = previous is None
        if strategy == 'fixed_ttl':
            due = due or age >= config.ttl_ms
        elif strategy in ADAPTIVE:
            limit = min(ttl, max(config.min_ttl_ms, request.task.deadline_ms/2))
            due = due or state['last_failed'] or state['latency_shift'] or age >= limit
        if due and request.remaining() > 0:
            result, duration = request.invoke(name, ProbeTask(), min(config.probe_timeout_ms, request.remaining())/1000, 'probe')
            changed = previous is not None and (not result.ok or not previous['reachable'] or
                max(duration, previous['latency_ms'])/max(.001, min(duration, previous['latency_ms'])) >= config.event_latency_ratio)
            state['observations'][name] = {'reachable': bool(result.ok), 'latency_ms': duration,
                'checked_ms': request.start_clock+request.elapsed(), 'source': 'probe'}
            if strategy in ADAPTIVE:
                ttl = max(config.min_ttl_ms, ttl/2) if changed else min(config.max_ttl_ms, ttl*1.25)
                state['adaptive_ttl_ms'] = ttl
        observed, entry = state['observations'].get(name), calibration.get(name, {})
        if observed is not None and v4._quality(calibration, name, family) is not None:
            resource = request.trace.resources_at(request.step, name, now_ms=request.start_clock+request.elapsed())
            session.publish(HealthObservation(name, observed['reachable'], observed['latency_ms'],
                entry.get('quality_score') or 0, observed['checked_ms']/1000,
                quality_by_family=entry.get('quality_by_family', {}), resources=resource))


def _text_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _append(path, row):
    with Path(path).open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n')
        handle.flush()
        os.fsync(handle.fileno())


def capture_outputs(tasks_path, out_path, *, backends, trusted_backends=(), timeout_ms=30000,
                    resume=False, include_probes=True, stop_after=None, identity_path=None):
    """Capture independent live outputs without passing gold to any backend."""
    tasks, out = v4.load_frozen_tasks(tasks_path), Path(out_path)
    manifest = {'schema': 'ai6g.model_capture.manifest.v5', 'tasks_sha256': v4._file_hash(tasks_path),
        'deployment_identity_sha256': v4._file_hash(identity_path) if identity_path else None,
        'deployment_identity': json.loads(Path(identity_path).read_text(encoding='utf-8-sig')) if identity_path else None,
        'backends': v4._backend_identity(backends), 'trusted_backends': sorted(trusted_backends),
        'decoding': {name: {'temperature': 0, 'response_format': 'json_object',
            'seed': getattr(backend, 'seed', None), 'max_tokens': getattr(backend, 'max_tokens', None)}
            for name, backend in backends.items()},
        'timeout_ms': timeout_ms, 'include_probes': include_probes, 'gold_online': False,
        'sources': {name: v4._file_hash(Path(__file__).resolve().parents[1]/name)
                    for name in ('models.py', 'protocol.py', 'evaluation/controlled_v5.py')}}
    fingerprint, mp = v4._hash(manifest), out.with_suffix('.manifest.json')
    done, records = set(), []
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() or mp.exists():
        if not resume or not mp.exists() or json.loads(mp.read_text(encoding='utf-8'))['fingerprint'] != fingerprint:
            raise ValueError('capture exists or frozen capture fingerprint mismatch')
        for line in out.read_text(encoding='utf-8').splitlines() if out.exists() else []:
            row = json.loads(line)
            key = (row['task_id'], row['backend'])
            if key in done:
                raise ValueError('duplicate captured response')
            done.add(key)
            records.append(row)
    else:
        mp.write_text(json.dumps({'fingerprint': fingerprint, 'manifest': manifest}, ensure_ascii=False, indent=2), encoding='utf-8')
    pending = [({'id': '__probe__'}, ProbeTask(), 'probe')] if include_probes else []
    pending += [(row, classify_task(row['text']), 'task') for row in tasks]
    executed = 0
    for row, task, phase in pending:
        for name in MODELS:
            if (row['id'], name) in done:
                continue
            if stop_after is not None and executed >= stop_after:
                return records
            skipped = task.privacy in ('strict', 'local', 'local_only') and name not in trusted_backends if phase == 'task' else False
            started = time.perf_counter()
            if skipped:
                result = ModelResult(False, error='strict_privacy_capture_skipped', backend=name)
            else:
                result = backends[name].infer(task, timeout_s=timeout_ms/1000)
            record = {'schema': 'ai6g.model_capture.v5', 'capture_fingerprint': fingerprint,
                'backend': name, 'task_id': row['id'], 'text_sha256': _text_hash(task.text),
                'phase': phase, 'skipped': skipped, 'result': asdict(result),
                'elapsed_ms': (time.perf_counter()-started)*1000, 'captured_at': time.time(),
                'actual_endpoint_dispatch': not skipped, 'gold_online': False}
            _append(out, record)
            records.append(record)
            done.add((row['id'], name))
            executed += 1
    return records


class ReplayBackend:
    """Consumes a captured result once per logical invocation, never HTTP."""
    def __init__(self, name, records, identity):
        self.name, self.records, self.identity = name, records, identity
        self.task_id = None
        self.endpoint = identity.get('endpoint', '')
        self.model = identity.get('model')
        self.system_prompt = None
        self.max_tokens = identity.get('max_tokens')

    def record(self, task):
        task_id = '__probe__' if isinstance(task, ProbeTask) else self.task_id
        item = self.records.get((task_id, self.name))
        if item is None or item['text_sha256'] != _text_hash(task.text):
            raise ValueError(f'missing or mismatched captured response: {task_id}/{self.name}')
        return item


def load_replay(path):
    path = Path(path)
    metadata = json.loads(path.with_suffix('.manifest.json').read_text(encoding='utf-8'))
    if metadata['fingerprint'] != v4._hash(metadata['manifest']):
        raise ValueError('invalid capture manifest fingerprint')
    records = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        row = json.loads(line)
        key = (row['task_id'], row['backend'])
        if key in records or row.get('capture_fingerprint') != metadata['fingerprint']:
            raise ValueError('duplicate or foreign captured response')
        records[key] = row
    return {name: ReplayBackend(name, records, metadata['manifest']['backends'][name]) for name in MODELS}, metadata


def _capture_workload(records):
    rows = list(records.values())
    actual = [row for row in rows if row['actual_endpoint_dispatch']]
    tokens, unknown = 0, 0
    for row in actual:
        raw = row['result'].get('raw_response') or {}
        usage = raw.get('usage', {})
        total = usage.get('total_tokens')
        if total is None and 'prompt_tokens' in usage and 'completion_tokens' in usage:
            total = usage['prompt_tokens']+usage['completion_tokens']
        if isinstance(total, (int, float)) and total >= 0:
            tokens += total
        else:
            unknown += 1
    return {'scope': 'one-time real capture acquisition, distinct from replay logical workloads',
        'rows': len(rows), 'actual_endpoint_dispatches': len(actual),
        'probe_dispatches': sum(row['phase'] == 'probe' for row in actual),
        'task_dispatches': sum(row['phase'] == 'task' for row in actual),
        'captured_model_real_rows': sum(row['result']['model_real'] for row in actual),
        'unsuccessful_or_unparseable_rows': sum(not row['result']['ok'] for row in actual),
        'elapsed_ms': sum(row['elapsed_ms'] for row in actual), 'provider_tokens': tokens,
        'unknown_usage_requests': unknown, 'usd': None}


class _Request(v4._Request):
    def invoke(self, name, task, timeout_s, phase='task'):
        backend = self.backends.get(name)
        if not isinstance(backend, ReplayBackend):
            result, duration = super().invoke(name, task, timeout_s, phase)
            self.calls[-1]['response_source_kind'] = 'live_endpoint' if self.config.online and name != 'local-rules' else 'contract_or_rules'
            self.calls[-1]['actual_endpoint_dispatch'] = bool(self.config.online and self.calls[-1]['dispatched'] and name != 'local-rules')
            self.calls[-1]['inference_wall_budget_ms'] = max(0, duration-self.calls[-1]['injected_delay_ms'])
            return result, duration
        item, budget = backend.record(task), min(timeout_s*1000, self.remaining())
        trace = self.trace.at(self.step, name)
        raw_result = ModelResult(**item['result'])
        payload, raw = raw_result.payload, raw_result.raw_response
        request_bytes = len(task.text.encode('utf-8'))
        response_bytes = len(json.dumps(raw or payload or {}, ensure_ascii=False).encode('utf-8'))
        bandwidth = trace['bandwidth_kbps']
        outbound = 0 if bandwidth is None else request_bytes*8/bandwidth
        inbound = 0 if bandwidth is None else response_bytes*8/bandwidth
        pre = trace['rtt_ms']+trace['queue_ms']+outbound+(0 if trace['reachable'] else trace['disconnect_ms'])
        dispatched = trace['reachable'] and pre < budget and not item['skipped']
        model_ms = item['elapsed_ms'] if dispatched else 0
        required = pre+model_ms+(inbound if raw_result.ok and dispatched else 0)
        duration = min(max(0, budget), required)
        pre_consumed = min(duration, pre)
        inference_consumed = max(0, min(model_ms, duration-pre_consumed))
        inbound_consumed = max(0, duration-pre_consumed-inference_consumed)
        self.virtual_ms += duration
        expired = required > budget or self.remaining() <= 0
        error = 'deadline_exceeded' if expired else ('injected_disconnect' if not trace['reachable'] else raw_result.error)
        result = replace(raw_result, ok=bool(raw_result.ok and dispatched and not expired), error=error, model_real=False)
        captured_usage = raw.get('usage', {}) if isinstance(raw, dict) else {}
        # An aborted inference has unknown token cost. Do not reuse the full
        # completed capture's usage as if the hypothetical partial call ended.
        usage = captured_usage if dispatched and duration >= required else {}
        tokens = usage.get('total_tokens', usage.get('prompt_tokens', 0)+usage.get('completion_tokens', 0))
        self.calls.append({'backend': name, 'phase': phase, 'trace_step': self.step, 'trace': trace,
            'dispatched': bool(dispatched), 'ok': result.ok, 'model_real': False,
            'captured_model_real': bool(raw_result.model_real), 'capture_task_id': item['task_id'],
            'actual_endpoint_dispatch': False, 'response_source_kind': 'real_output_controlled_replay',
            'error': result.error, 'raw_response': raw, 'payload': payload, 'usage': usage, 'captured_usage': captured_usage,
            'total_tokens': tokens, 'usage_known': 'total_tokens' in usage or 'prompt_tokens' in usage and 'completion_tokens' in usage,
            'elapsed_ms': duration, 'captured_elapsed_ms': item['elapsed_ms'],
            'inference_wall_budget_ms': inference_consumed,
            'injected_delay_ms': pre_consumed+inbound_consumed, 'request_text_bytes': request_bytes,
            'response_json_bytes': response_bytes, 'token_source': 'captured_provider_usage'})
        return result, duration


def _run_one(row, session, *, config, trace, step, backends, calibration, repeat, position):
    task = classify_task(row['text'])
    task = replace(task, deadline_ms=config.deadline_ms or int(row.get('deadline_ms', task.deadline_ms)))
    family, strategy, state = str(row.get('family', task.family)), session.strategy, session.state
    for backend in backends.values():
        if isinstance(backend, ReplayBackend):
            backend.task_id = row['id']
    request = _Request(task, config, trace, step, backends)
    # A fixed positive logical epoch preserves stale telemetry age even on the
    # first step, while ResourceObservation correctly rejects negative stamps.
    request.start_clock = max(state['clock_ms'], LOGICAL_EPOCH_MS+step*config.step_ms)
    if strategy in OBSERVED and not v4._fastpath(task, config):
        _refresh(request, session, family, calibration)
    router = _PolicyRouter(session, request, family)
    adapters = {name: v4._Adapter(request, name) for name in (*MODELS, 'local-rules')}
    runtime = TaskRuntime(router=router, backends=adapters, store=DeviceStore(),
        default_deadline_ms=task.deadline_ms, trusted_backends=set(config.trusted_backends))
    before = runtime.state()
    effective_start = request.started-request.virtual_ms/1000
    result = runtime.handle(task.text, request_id=f'{repeat}:{row["id"]}:{strategy}',
        started_at=effective_start, deadline_at=effective_start+task.deadline_ms/1000)
    elapsed = request.elapsed()
    if elapsed > task.deadline_ms and not result['ok']:
        result['error_code'] = 'deadline_exceeded'
    result['initial_state'] = before
    score, calls = semantic_score_row(result, row['gold']), request.calls
    if strategy in ADAPTIVE:
        completed = [c for c in calls if c['phase'] != 'probe' and c['backend'] in MODELS]
        if completed:
            latest = completed[-1]
            prior, duration = state['observations'].get(latest['backend']), latest['elapsed_ms']
            state['latency_shift'] = bool(prior and max(duration, prior['latency_ms'])/
                max(.001, min(duration, prior['latency_ms'])) >= config.event_latency_ratio)
            state['observations'][latest['backend']] = {'reachable': latest['ok'], 'latency_ms': duration,
                'checked_ms': request.start_clock+elapsed, 'source': 'task'}
    state.update(clock_ms=request.start_clock+elapsed, last_failed=not result['ok'], last_family=family)
    cost = v4._cost(calls, config)
    cost['actual_endpoint_dispatches'] = sum(c.get('actual_endpoint_dispatch', False) for c in calls)
    cost['dispatch_failures'] = sum(c['dispatched'] and not c['ok'] for c in calls if c['backend'] in MODELS)
    cost['replay_invocations'] = sum(c['dispatched'] and c.get('response_source_kind') == 'real_output_controlled_replay' for c in calls)
    model_calls = [c for c in calls if c['backend'] in MODELS]
    cost['invoke_elapsed_ms'] = sum(c['elapsed_ms'] for c in model_calls)
    cost['inference_wall_budget_ms'] = sum(c['inference_wall_budget_ms'] for c in model_calls)
    cost['injected_service_ms'] = sum(c['injected_delay_ms'] for c in model_calls)
    cost['model_elapsed_ms_scope'] = 'legacy field alias for invoke elapsed including service impairments, not GPU compute'
    cost['inference_wall_scope'] = 'live HTTP wall interval or replay budget consumed from captured HTTP interval; not GPU kernel time'
    cost['token_scope'] = 'known completed provider usage; unknown_usage_requests includes hypothetical aborted replay calls'
    cost['usd'] = None  # local electricity/depreciation and provider bills are not known
    cost['money_scope'] = 'unknown actual money; token/model/probe costs are reported separately'
    return {**result, 'row_id': f'{repeat}:{row["id"]}:{strategy}', 'task_id': row['id'],
        'cluster_id': row.get('cluster_id', row['id']), 'template_group': row.get('template_group'),
        'task_text': task.text, 'family': family, 'strategy': strategy, 'repeat': repeat,
        'trace_step': step, 'arm_position': position, 'deadline_ms': task.deadline_ms,
        'engineering_ok': score['engineering_ok'], 'semantic_success': score['semantic_success'],
        'semantic_reason': score.get('reason'), 'deadline_correct': score['semantic_success'] and elapsed <= task.deadline_ms,
        'wrong_side_effect': v4._wrong_effect(before, result['state'], row['gold'], (result.get('plan') or {}).get('actions', [])),
        'total_elapsed_ms': elapsed, 'actual_wall_elapsed_ms': (time.perf_counter()-request.started)*1000,
        'virtual_injected_ms': request.virtual_ms, 'latency_scope': 'outer_entry_to_state_including_probes_and_replay_or_live_calls',
        'model_real': any(c['model_real'] for c in calls), 'calls': calls, 'call_count': len(calls),
        'cost': cost, 'total_tokens': cost['total_tokens'], 'model_requests': cost['model_requests'],
        'policy_state_after': session.snapshot()}


def report_markdown(report):
    lines = ['# SenseHome v5 resource experiment', '',
        f'Source: **{report["source_kind"]}**. Complete: {report["complete"]}.',
        f'Fingerprint: `{report["fingerprint"]}`.', '',
        '| Strategy | n | Semantic | Deadline correct | Wrong effects | P95 ms | Calls | Probes | Tokens |',
        '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for name, group in report['strategies'].items():
        p95 = '-' if group['p95_total_ms'] is None else f'{group["p95_total_ms"]:.2f}'
        lines.append(f'| {name} | {group["requests"]} | {group["semantic_success"]} | {group["deadline_correct"]} | '
            f'{group["wrong_side_effect"]} | {p95} | {group["model_requests"]} | {group["probe_requests"]} | {group["total_tokens"]} |')
    lines += ['', 'The production TaskRouter is persistent within each arm. Device state resets for each task/arm.',
        'Gold is offline only. Every arm shares rule/safety/privacy/protocol/executor constraints.',
        'All software impairments and resource observations are scenario injections, not a measured physical network.',
        'Replay consumes captured outputs and captured wall latency. It performs zero live model calls; its logical costs are not additional bills.',
        'Capture is a separate actual inference workload. Models may share one host/GPU; aliases do not imply a physical edge/cloud topology.',
        'Money is unknown. Full latency includes probes and all failed attempts; no successful-only latency filtering.',
        'Intervals are cluster paired descriptive bootstrap. The two primary comparisons use Bonferroni 97.5% intervals.', '']
    for item in report['comparisons']:
        interval = item.get('ci975_bonferroni', item['ci95'])
        level = item.get('confidence_level', .95)
        lines.append(f'- {item["comparison_role"]}: {item["arm"]} vs {item["reference"]}, {item["metric"]} '
            f'difference {item["estimate"]:.4f}, {level:.3f} CI {interval}.')
    return '\n'.join(lines)+'\n'


def _comparison(rows, arm, reference, config, primary=False):
    result = v4.paired_bootstrap(rows, arm, reference, samples=config.bootstrap_samples, seed=config.seed)
    if primary:
        # Reuse the same paired cluster estimand with 2-sided Bonferroni bounds.
        groups = {}
        for row in rows:
            if row['strategy'] in (arm, reference):
                groups.setdefault((row['cluster_id'], row['task_id'], row['repeat']), {})[row['strategy']] = row['deadline_correct']
        by_task = {}
        for (cluster, task, repeat), values in groups.items():
            by_task.setdefault((cluster, task), []).append(float(values[arm])-float(values[reference]))
        by_cluster = {}
        for (cluster, _), values in by_task.items():
            by_cluster.setdefault(cluster, []).append(sum(values)/len(values))
        means = [sum(v)/len(v) for _, v in sorted(by_cluster.items())]
        rng = v4.random.Random(config.seed)
        draws = [sum(rng.choices(means, k=len(means)))/len(means) for _ in range(config.bootstrap_samples)]
        result['ci975_bonferroni'] = [v4._percentile(draws, .0125), v4._percentile(draws, .9875)]
        result['confidence_level'] = .975
        result['multiplicity'] = 'Bonferroni two predeclared primary comparisons'
        result['interpretation'] = 'paired cluster descriptive bootstrap; adjusted family confidence bounds, no causal external-validity claim'
    result['comparison_role'] = 'primary_predeclared' if primary else 'secondary_ablation'
    return result


def evaluate(tasks_path, out_path, *, calibration_path=None, trace_path=None, config=None,
             backends=None, resume=False, stop_after=None, replay_path=None, identity_path=None):
    config, tasks_path, out = config or ExperimentConfig(), Path(tasks_path), Path(out_path)
    tasks = v4.load_frozen_tasks(tasks_path)
    if config.limit is not None:
        tasks = tasks[:config.limit]
    calibration, trace = v4._calibration(calibration_path), ResourceTrace(trace_path)
    if (config.online or replay_path) and not calibration and set(config.strategies) & OBSERVED:
        raise ValueError('measured-quality strategies require frozen calibration')
    capture_metadata = None
    capture_workload = None
    if replay_path:
        if config.online or backends is not None:
            raise ValueError('replay is incompatible with online/custom backends')
        backends, capture_metadata = load_replay(replay_path)
        if capture_metadata['manifest']['tasks_sha256'] != v4._file_hash(tasks_path):
            raise ValueError('replay task file fingerprint mismatch')
        captured = backends[MODELS[0]].records
        required = [(row['id'], name, row['text']) for row in tasks for name in MODELS]
        if set(config.strategies) & OBSERVED:
            required += [('__probe__', name, ProbeTask().text) for name in MODELS]
        for task_id, name, text in required:
            row = captured.get((task_id, name))
            if row is None or row['text_sha256'] != _text_hash(text):
                raise ValueError(f'incomplete capture or mismatched text: {task_id}/{name}')
        capture_workload = _capture_workload(captured)
    else:
        backends = dict(backends) if backends is not None else v4.build_backends(config)
    source_kind = 'real_output_controlled_replay' if replay_path else 'real_endpoints' if config.online else 'contract_simulator'
    package = Path(__file__).resolve().parents[1]
    sources = ('evaluation/controlled_v5.py', 'evaluation/controlled.py', 'runtime.py', 'router.py', 'protocol.py',
               'executor.py', 'models.py', 'evaluator.py', 'health_monitor.py')
    manifest = {'schema': 'ai6g.controlled.manifest.v5', 'tasks_sha256': v4._file_hash(tasks_path),
        'deployment_identity_sha256': v4._file_hash(identity_path) if identity_path else None,
        'deployment_identity': json.loads(Path(identity_path).read_text(encoding='utf-8-sig')) if identity_path else None,
        'calibration_sha256': v4._file_hash(calibration_path) if calibration_path else None,
        'trace_sha256': v4._file_hash(trace_path) if trace_path else None,
        'capture_sha256': v4._file_hash(replay_path) if replay_path else None,
        'capture_metadata': capture_metadata, 'config': asdict(config), 'source_kind': source_kind,
        'sources': {name: v4._file_hash(package/name) for name in sources},
        'backends': capture_metadata['manifest']['backends'] if capture_metadata else v4._backend_identity(backends),
        'task_count': len(tasks), 'gold_online': False, 'quality_source': 'frozen_calibration_or_unknown',
        'resource_source': 'frozen_injected_scenario_or_unknown', 'persistent_product_router': True,
        'trace_repeat_semantics': 'absolute step=repeat*n+index; events persist, never implicitly reset',
        'logical_epoch_ms': LOGICAL_EPOCH_MS,
        'physical_topology': 'not inferred from backend aliases'}
    fingerprint, journal, mp = v4._hash(manifest), out.with_suffix('.rows.jsonl'), out.with_suffix('.manifest.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    rows, done = [], set()
    sessions = {name: PolicySession(name, config) for name in config.strategies}
    if mp.exists() or journal.exists():
        if not resume or not mp.exists() or json.loads(mp.read_text(encoding='utf-8'))['fingerprint'] != fingerprint:
            raise ValueError('existing output or frozen experiment fingerprint mismatch')
        valid_bytes = 0
        lines = journal.read_bytes().splitlines(keepends=True) if journal.exists() else []
        for index, line in enumerate(lines):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                if index != len(lines)-1:
                    raise ValueError('corrupt journal before final row')
                break
            if row['row_id'] in done:
                raise ValueError('duplicate journal row')
            rows.append(row)
            done.add(row['row_id'])
            sessions[row['strategy']] = PolicySession(row['strategy'], config, row['policy_state_after'])
            valid_bytes += len(line)
        if journal.exists():
            with journal.open('r+b') as handle:
                handle.truncate(valid_bytes)
                if valid_bytes and not lines[len(rows)-1].endswith(b'\n'):
                    handle.seek(0, os.SEEK_END)
                    handle.write(b'\n')
    else:
        mp.write_text(json.dumps({'fingerprint': fingerprint, 'manifest': manifest}, ensure_ascii=False, indent=2), encoding='utf-8')
    orders, executed, stopped = [], 0, False
    for repeat in range(config.repeats):
        for index, task in enumerate(tasks):
            step = repeat*len(tasks)+index
            shift = step % len(config.strategies)
            order = list(config.strategies[shift:]+config.strategies[:shift])
            orders.append(order)
            for position, name in enumerate(order):
                if f'{repeat}:{task["id"]}:{name}' in done:
                    continue
                if stop_after is not None and executed >= stop_after:
                    stopped = True
                    break
                row = v4._clean(_run_one(task, sessions[name], config=config, trace=trace, step=step,
                    backends=backends, calibration=calibration, repeat=repeat, position=position))
                _append(journal, row)
                rows.append(row)
                done.add(row['row_id'])
                executed += 1
            if stopped:
                break
        if stopped:
            break
    complete = len(rows) == len(tasks)*config.repeats*len(config.strategies)
    comparisons = []
    if complete and 'full_resource' in config.strategies:
        for reference in ('no_resource', 'adaptive', 'no_hysteresis'):
            if reference in config.strategies:
                comparisons.append(_comparison(rows, 'full_resource', reference, config, reference != 'no_hysteresis'))
    summary = v4._summaries(rows, config.strategies)
    for name, item in summary.items():
        group = [row for row in rows if row['strategy'] == name]
        for key in ('actual_endpoint_dispatches', 'dispatch_failures', 'replay_invocations', 'model_elapsed_ms',
                    'invoke_elapsed_ms', 'inference_wall_budget_ms', 'injected_service_ms', 'network_bytes'):
            item[key] = sum(row['cost'][key] for row in group)
        item['usd'] = None
        item['provider_tokens'] = item['total_tokens'] if config.online else 0
        item['captured_usage_replayed_tokens'] = item['total_tokens'] if replay_path else 0
    report = {'schema': 'ai6g.controlled.v5', 'source_kind': source_kind, 'complete': complete,
        'fingerprint': fingerprint, 'manifest': manifest, 'task_count': len(tasks), 'repeats': config.repeats,
        'capture_workload': capture_workload, 'strategy_order': orders,
        'strategies': summary, 'comparisons': comparisons, 'rows': rows}
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    out.with_suffix('.md').write_text(report_markdown(report), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('tasks', 'out'):
        parser.add_argument('--'+flag, required=True)
    for flag in ('calibration', 'trace', 'replay', 'identity'):
        parser.add_argument('--'+flag)
    for flag in ('collect', 'online', 'resume', 'no-rule-fastpath', 'no-safety-fastpath'):
        parser.add_argument('--'+flag, action='store_true')
    parser.add_argument('--strategies', default=','.join(STRATEGIES))
    parser.add_argument('--trusted-backends', default='')
    for flag, default in [('repeats', 1), ('bootstrap-samples', 2000), ('seed', 20261104),
                          ('max-inflight', 8), ('deadline-ms', None), ('limit', None), ('stop-after', None)]:
        parser.add_argument('--'+flag, type=int, default=default)
    for flag, default in [('ttl-ms', 5000), ('min-ttl-ms', 250), ('max-ttl-ms', 10000),
        ('step-ms', 1000), ('probe-timeout-ms', 3000), ('resource-ttl-ms', 5000), ('max-queue-ms', 250),
        ('min-free-mem-mb', 256), ('switch-margin-ms', 10), ('capture-timeout-ms', 30000)]:
        parser.add_argument('--'+flag, type=float, default=default)
    args = parser.parse_args(argv)
    values = {k: getattr(args, k) for k in ExperimentConfig.__dataclass_fields__ if hasattr(args, k)}
    values.update(strategies=tuple(args.strategies.split(',')), trusted_backends=tuple(filter(None, args.trusted_backends.split(','))),
        shared_rule_fastpath=not args.no_rule_fastpath, shared_safety_fastpath=not args.no_safety_fastpath)
    config = ExperimentConfig(**values)
    try:
        if args.collect:
            real = replace(config, online=True)
            records = capture_outputs(args.tasks, args.out, backends=v4.build_backends(real),
                trusted_backends=config.trusted_backends, timeout_ms=args.capture_timeout_ms,
                resume=args.resume, stop_after=args.stop_after, identity_path=args.identity)
            print(json.dumps({'capture_rows': len(records), 'output': args.out}))
        else:
            report = evaluate(args.tasks, args.out, calibration_path=args.calibration, trace_path=args.trace,
                config=config, resume=args.resume, stop_after=args.stop_after, replay_path=args.replay, identity_path=args.identity)
            print(report_markdown(report))
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
