"""Restartable controlled experiment using the PRODUCT TaskRuntime/TaskRouter.

Default: contract simulator, model_real=False, no LLM/network performance claim.
--online calls OpenAICompatibleBackend endpoints from AI6G_LOCAL/CLOUD_* env.
Gold is read only after execution. All strategies share policy validation,
trusted-deployment settings and a fresh DeviceStore for every task/arm.

Public task JSONL: id, text, optional deadline_ms/family, gold (offline only).
Trace JSONL: {"step":0,"backend":"cloud-model","rtt_ms":50,
"queue_ms":20,"bandwidth_kbps":512,"reachable":true}. Events persist; '*' means
both models. step is repeat*task_count+task_index; all arms get the same step.
Only backend wrappers see trace states; routing sees completed observations.
Offline impairments advance virtual time; online impairments actually sleep.
Bandwidth covers UTF-8 task text and serialized response JSON, not HTTP headers.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from ai6g_runtime.evaluator import semantic_score_row
    from ai6g_runtime.executor import DeviceStore
    from ai6g_runtime.health_monitor import ProbeTask
    from ai6g_runtime.models import ModelResult, OpenAICompatibleBackend, RuleBackend
    from ai6g_runtime.protocol import classify_task, validate_plan
    from ai6g_runtime.router import HealthObservation, ObservationCache, RouteDecision, TaskRouter
    from ai6g_runtime.runtime import TaskRuntime
else:
    from ..evaluator import semantic_score_row
    from ..executor import DeviceStore
    from ..health_monitor import ProbeTask
    from ..models import ModelResult, OpenAICompatibleBackend, RuleBackend
    from ..protocol import classify_task, validate_plan
    from ..router import HealthObservation, ObservationCache, RouteDecision, TaskRouter
    from ..runtime import TaskRuntime

STRATEGIES = ('rules', 'fixed_local', 'fixed_cloud', 'legal_cloud', 'cascade',
              'static', 'fixed_ttl', 'event', 'adaptive')
MODELS = ('local-model', 'cloud-model')
OBSERVED = {'static', 'fixed_ttl', 'event', 'adaptive'}


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _finite(value, name, minimum=0):
    if isinstance(value, bool):
        raise ValueError(f'{name} must be finite and >= {minimum}')
    value = float(value)
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f'{name} must be finite and >= {minimum}')
    return value


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
    cascade_confidence: float = 0.8
    bootstrap_samples: int = 2000
    seed: int = 20261104
    input_usd_per_million: float | None = None
    output_usd_per_million: float | None = None

    def __post_init__(self):
        if not self.strategies or len(set(self.strategies)) != len(self.strategies):
            raise ValueError('strategies must be non-empty and unique')
        if set(self.strategies)-set(STRATEGIES):
            raise ValueError('unknown strategy')
        if self.repeats < 1 or self.bootstrap_samples < 1:
            raise ValueError('repeats/bootstrap_samples must be positive')
        for name in ('ttl_ms', 'min_ttl_ms', 'max_ttl_ms', 'step_ms', 'probe_timeout_ms'):
            _finite(getattr(self, name), name)
        if self.min_ttl_ms > self.max_ttl_ms or self.event_latency_ratio < 1:
            raise ValueError('invalid TTL bounds or event latency ratio')
        if not 0 <= self.cascade_confidence <= 1:
            raise ValueError('invalid cascade confidence')
        for name in ('input_usd_per_million', 'output_usd_per_million'):
            if getattr(self, name) is not None:
                _finite(getattr(self, name), name)


def load_frozen_tasks(path):
    rows, ids = [], set()
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get('text'), str) or not row['text'].strip():
            raise ValueError('task requires non-empty text')
        identity = row.get('id')
        if not isinstance(identity, str) or not identity or identity in ids:
            raise ValueError('missing or duplicate task id')
        if not isinstance(row.get('gold'), dict):
            raise ValueError('task requires offline gold')
        if 'deadline_ms' in row:
            _finite(row['deadline_ms'], 'deadline_ms', 1)
        ids.add(identity)
        rows.append(row)
    if not rows:
        raise ValueError('empty task set')
    return rows


def _calibration(path):
    if path is None:
        return {}
    payload = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    result = {}
    for name, entry in payload.get('backends', payload).items():
        if name not in MODELS:
            continue
        source = entry.get('source_sha256')
        if not isinstance(source, str) or len(source) != 64 or any(c not in '0123456789abcdefABCDEF' for c in source):
            raise ValueError('calibration source_sha256 required')
        def score(value):
            value = _finite(value, 'quality_score')
            if value > 1:
                raise ValueError('quality_score exceeds 1')
            return value
        profiles = dict(entry.get('quality_by_family', {}))
        profiles.update(entry.get('families', {}))
        result[name] = {'source_sha256': source,
                        'quality_score': score(entry['quality_score']) if 'quality_score' in entry else None,
                        'quality_by_family': {str(k): score(v.get('quality_score') if isinstance(v, dict) else v)
                                              for k, v in profiles.items()}}
    return result


def _quality(calibration, name, family):
    entry = calibration.get(name, {})
    return entry.get('quality_by_family', {}).get(family, entry.get('quality_score'))


class Trace:
    """Deterministic service impairment, inaccessible to the route policy."""
    def __init__(self, path=None):
        self.events = []
        if path:
            for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get('backend') not in (*MODELS, '*'):
                    raise ValueError('invalid trace backend')
                if isinstance(event.get('step'), bool) or not isinstance(event.get('step'), int) or event['step'] < 0:
                    raise ValueError('trace step must be non-negative integer')
                if 'reachable' in event and not isinstance(event['reachable'], bool):
                    raise ValueError('trace reachable must be boolean')
                for name in ('rtt_ms', 'queue_ms', 'bandwidth_kbps', 'disconnect_ms'):
                    if name in event:
                        _finite(event[name], name, .001 if name == 'bandwidth_kbps' else 0)
                self.events.append(event)
            self.events.sort(key=lambda event: event['step'])

    def at(self, step, backend):
        state = {'reachable': True, 'rtt_ms': 0, 'queue_ms': 0,
                 'bandwidth_kbps': None, 'disconnect_ms': 100}
        for event in self.events:
            if event['step'] > step:
                break
            if event['backend'] in (backend, '*'):
                state.update({k: v for k, v in event.items() if k not in ('step', 'backend')})
        return state


class ContractSimulator:
    """A gold-free protocol fixture; never a language-model result."""
    def __init__(self, name):
        self.name = name

    def infer(self, task, timeout_s):
        if isinstance(task, ProbeTask):
            payload = {'result_kind': 'clarification', 'actions': [], 'explanation': 'contract probe'}
        else:
            result = RuleBackend().infer(task, timeout_s)
            payload = result.payload if result.ok else {'result_kind': 'clarification', 'actions': [],
                                                       'explanation': 'contract simulator cannot infer request'}
        tokens = max(1, len(task.text.encode('utf-8'))//3)
        return ModelResult(True, payload, backend=self.name, model_real=False,
                           raw_response={'usage': {'prompt_tokens': tokens, 'completion_tokens': 10,
                                                   'total_tokens': tokens+10}, 'source_kind': 'contract_simulator'})


def build_backends(config):
    if not config.online:
        return {name: ContractSimulator(name) for name in MODELS}
    result = {}
    for name, prefix in [('local-model', 'AI6G_LOCAL'), ('cloud-model', 'AI6G_CLOUD')]:
        endpoint = os.getenv(prefix+'_URL')
        if not endpoint:
            raise ValueError(f'--online requires {prefix}_URL')
        result[name] = OpenAICompatibleBackend(name, endpoint, os.getenv(prefix+'_MODEL', name),
                                               os.getenv(prefix+'_API_KEY'), seed=config.seed)
    return result


def _backend_identity(backends):
    result = {}
    for name, backend in backends.items():
        url = urlsplit(getattr(backend, 'endpoint', ''))
        host = url.hostname or ''
        if url.port:
            host += f':{url.port}'
        result[name] = {'class': f'{type(backend).__module__}.{type(backend).__qualname__}',
                        'model': getattr(backend, 'model', None),
                        'endpoint': urlunsplit((url.scheme, host, url.path, '', '')),
                        'prompt_sha256': _hash(getattr(backend, 'system_prompt', None)),
                        'max_tokens': getattr(backend, 'max_tokens', None)}
    return result


def _percentile(values, quantile):
    if not values:
        return None
    values = sorted(values)
    pos = (len(values)-1)*quantile
    low = int(pos)
    return values[low]+(values[min(low+1, len(values)-1)]-values[low])*(pos-low)


def paired_bootstrap(rows, arm, reference, *, metric='deadline_correct', samples=2000, seed=20261104):
    """Average repeats per task, then tasks per cluster_id; resample clusters."""
    groups = {arm: {}, reference: {}}
    task_clusters = {}
    for row in rows:
        if row['strategy'] not in groups:
            continue
        key = (str(row['task_id']), int(row['repeat']))
        cluster = str(row.get('cluster_id', row['task_id']))
        if key[0] in task_clusters and task_clusters[key[0]] != cluster:
            raise ValueError('inconsistent task cluster')
        task_clusters[key[0]] = cluster
        group = groups[row['strategy']]
        if key in group or row.get(metric) is None:
            raise ValueError('invalid duplicate/missing paired metric')
        group[key] = float(row[metric])
    if not groups[arm] or set(groups[arm]) != set(groups[reference]):
        raise ValueError('complete task/repeat pairs required')
    differences = defaultdict(list)
    for key, value in groups[arm].items():
        differences[key[0]].append(value-groups[reference][key])
    clustered = defaultdict(list)
    for task, values in differences.items():
        clustered[task_clusters[task]].append(statistics.mean(values))
    clusters = [statistics.mean(values) for _, values in sorted(clustered.items())]
    rng = random.Random(seed)
    draws = [statistics.mean(rng.choices(clusters, k=len(clusters))) for _ in range(samples)]
    return {'arm': arm, 'reference': reference, 'metric': metric, 'estimate': statistics.mean(clusters),
            'ci95': [_percentile(draws, .025), _percentile(draws, .975)], 'clusters': len(clusters),
            'paired_rows': len(groups[arm]), 'samples': samples, 'unit': 'cluster_id or task_id; repeats/tasks averaged',
            'interpretation': 'descriptive; exploratory comparisons without multiplicity adjustment'}


class _Request:
    def __init__(self, task, config, trace, step, backends):
        self.task, self.config, self.trace, self.step, self.backends = task, config, trace, step, backends
        self.started = time.perf_counter()
        self.virtual_ms = 0.0
        self.calls = []

    def elapsed(self):
        return (time.perf_counter()-self.started)*1000+self.virtual_ms

    def remaining(self):
        return max(0, self.task.deadline_ms-self.elapsed())

    def invoke(self, name, task, timeout_s, phase='task'):
        budget = min(timeout_s*1000, self.remaining())
        impairment = self.trace.at(self.step, name) if name != 'local-rules' else {
            'reachable': True, 'rtt_ms': 0, 'queue_ms': 0, 'bandwidth_kbps': None, 'disconnect_ms': 0}
        started = time.perf_counter()
        bandwidth = impairment['bandwidth_kbps']
        request_bytes = len(task.text.encode('utf-8'))
        transfer_out = 0 if bandwidth is None else request_bytes*8/bandwidth
        pre = impairment['rtt_ms']+impairment['queue_ms']+transfer_out
        if not impairment['reachable']:
            pre += impairment['disconnect_ms']
        injected = min(pre, budget)
        if self.config.online and injected:
            time.sleep(injected/1000)
        dispatched = False
        if budget <= pre or not impairment['reachable']:
            result = ModelResult(False, error='deadline_exceeded' if impairment['reachable'] else 'injected_disconnect', backend=name)
        else:
            backend = RuleBackend() if name == 'local-rules' else self.backends.get(name)
            try:
                dispatched = backend is not None
                result = backend.infer(task, timeout_s=max(.001, (budget-pre)/1000)) if backend else ModelResult(False, error='backend not configured')
            except Exception as exc:
                result = ModelResult(False, error=f'{type(exc).__name__}: {exc}', backend=name)
        raw, payload = result.raw_response, result.payload
        response_bytes = len(json.dumps(raw or payload or {}, ensure_ascii=False).encode('utf-8'))
        transfer_in = 0 if bandwidth is None else response_bytes*8/bandwidth
        if bandwidth is not None and result.ok:
            residual = max(0, budget-((time.perf_counter()-started)*1000 if self.config.online else injected))
            inbound = min(transfer_in, residual)
            injected += inbound
            if self.config.online and inbound:
                time.sleep(inbound/1000)
        if not self.config.online:
            self.virtual_ms += injected
        usage = raw.get('usage', {}) if isinstance(raw, Mapping) else {}
        total = usage.get('total_tokens')
        if total is None and 'prompt_tokens' in usage and 'completion_tokens' in usage:
            total = usage['prompt_tokens']+usage['completion_tokens']
        tokens = int(total) if isinstance(total, (int, float)) and total >= 0 else 0
        duration = (time.perf_counter()-started)*1000+(0 if self.config.online else injected)
        expired = self.remaining() <= 0 or (result.ok and transfer_in > 0 and injected < pre+transfer_in)
        self.calls.append({'backend': name, 'phase': phase, 'trace_step': self.step, 'trace': impairment,
                           'dispatched': dispatched,
                           'ok': bool(result.ok and not expired), 'model_real': bool(result.model_real),
                           'error': 'deadline_exceeded' if expired else result.error,
                           'raw_response': raw, 'payload': payload, 'usage': dict(usage),
                           'total_tokens': tokens, 'usage_known': total is not None,
                           'elapsed_ms': duration, 'injected_delay_ms': injected,
                           'request_text_bytes': request_bytes, 'response_json_bytes': response_bytes,
                           'token_source': 'provider_usage' if self.config.online else 'contract_or_rules'})
        if expired:
            return replace(result, ok=False, error='deadline_exceeded'), duration
        return result, duration


class _Adapter:
    def __init__(self, request, name):
        self.request, self.name = request, name

    def infer(self, task, timeout_s):
        return self.request.invoke(self.name, task, timeout_s)[0]


class _Cascade:
    """Escalate based on observable failure/confidence/calibration, never gold."""
    def __init__(self, request, calibration, family, trusted):
        self.request, self.calibration, self.family, self.trusted = request, calibration, family, trusted

    def infer(self, task, timeout_s):
        result, _ = self.request.invoke('local-model', task, timeout_s)
        quality = _quality(self.calibration, 'local-model', self.family)
        try:
            plan = validate_plan(result.payload, task) if result.ok else None
        except (TypeError, ValueError):
            plan = None
        low_confidence = plan is None or plan.confidence is None or plan.confidence < self.request.config.cascade_confidence
        low_quality = quality is None or quality < task.quality_floor
        strict = task.privacy in ('strict', 'local', 'local_only')
        cloud_legal = not strict or 'cloud-model' in self.trusted
        if cloud_legal and (low_confidence or low_quality) and self.request.remaining() > 0:
            return self.request.invoke('cloud-model', task, self.request.remaining()/1000, 'cascade')[0]
        return result


def _fastpath(task, config):
    if config.shared_safety_fastpath and (getattr(task, 'policy_result', None) or task.task_class == 'safety'):
        return True
    return config.shared_rule_fastpath and task.rule_eligible


class _PolicyRouter:
    """Experimental fixed policies, or the production TaskRouter over a cache."""
    def __init__(self, strategy, request, cache, family, trusted):
        self.strategy, self.request, self.cache, self.family, self.trusted = strategy, request, cache, family, trusted

    def choose(self, task, now=None):
        if self.strategy == 'rules' or _fastpath(task, self.request.config):
            return RouteDecision('local-rules', 'UNIFORM_POLICY_RULE_PATH', 0, 1, 0, False)
        if self.strategy in OBSERVED:
            overrides = {'rule_eligible': False}
            available = {field.name for field in fields(task)}
            if 'family' in available:
                overrides['family'] = self.family
            if not self.request.config.shared_safety_fastpath:
                if 'policy_result' in available:
                    overrides['policy_result'] = None
                if task.task_class == 'safety':
                    overrides['task_class'] = 'complex'
            route_task = replace(task, **overrides)
            logical_now = (self.request.start_clock+self.request.elapsed())/1000
            return TaskRouter(self.cache, trusted_backends=self.trusted).choose(route_task, now=logical_now)
        name = {'fixed_local': 'local-model', 'fixed_cloud': 'cloud-model',
                'legal_cloud': 'cloud-model', 'cascade': 'cascade-model'}[self.strategy]
        if self.strategy == 'legal_cloud' and task.privacy in ('strict', 'local', 'local_only') and name not in self.trusted:
            name = 'local-model' if 'local-model' in self.trusted else 'unavailable'
        return RouteDecision(name, self.strategy.upper(), None, None, None, False)


def _initial_policy():
    return {'observations': {}, 'clock_ms': 0, 'last_family': None,
            'last_failed': False, 'latency_shift': False, 'adaptive_ttl_ms': None}


def _refresh(request, strategy, state, family, calibration, trusted):
    config, task = request.config, request.task
    ttl = state['adaptive_ttl_ms'] or config.ttl_ms
    # static deliberately trusts its initial measurement; other arms explicitly age it.
    cache = ObservationCache(ttl_s=(1e12 if strategy == 'static' else
                            config.max_ttl_ms/1000 if strategy == 'event' else ttl/1000))
    strict = task.privacy in ('strict', 'local', 'local_only')
    allowed = [name for name in MODELS if not strict or name in trusted]
    for name in allowed:
        previous = state['observations'].get(name)
        age = float('inf') if previous is None else request.start_clock-previous['checked_ms']
        due = previous is None
        if strategy == 'fixed_ttl':
            due = due or age >= config.ttl_ms
        elif strategy == 'event':
            due = due or state['last_failed'] or state['latency_shift'] or state['last_family'] != family or age >= config.max_ttl_ms
        elif strategy == 'adaptive':
            limit = min(ttl, max(config.min_ttl_ms, task.deadline_ms/2))
            due = due or state['last_failed'] or state['latency_shift'] or age >= limit
        if due and request.remaining() > 0:
            result, duration = request.invoke(name, ProbeTask(), min(config.probe_timeout_ms, request.remaining())/1000, 'probe')
            changed = previous is not None and (not result.ok or not previous['reachable'] or
                      max(duration, previous['latency_ms'])/max(.001, min(duration, previous['latency_ms'])) >= config.event_latency_ratio)
            state['observations'][name] = {'reachable': bool(result.ok), 'latency_ms': duration,
                'checked_ms': request.start_clock+request.elapsed(), 'source': 'probe'}
            if strategy == 'adaptive':
                ttl = max(config.min_ttl_ms, ttl/2) if changed else min(config.max_ttl_ms, ttl*1.25)
                state['adaptive_ttl_ms'] = ttl
        observed = state['observations'].get(name)
        entry = calibration.get(name, {})
        if observed is not None and _quality(calibration, name, family) is not None:
            cache.put(HealthObservation(name, observed['reachable'], observed['latency_ms'],
                entry.get('quality_score') or 0, observed['checked_ms']/1000,
                quality_by_family=entry.get('quality_by_family', {})))
    return cache


def _flatten(value, prefix=()):
    result = {}
    for key, item in value.items():
        path = (*prefix, key)
        if isinstance(item, Mapping):
            result.update(_flatten(item, path))
        else:
            result[path] = item
    return result


def _wrong_effect(before, after, gold, actions):
    expected, old, new = _flatten(gold.get('expected_state', {})), _flatten(before), _flatten(after)
    wrong_state = any(new.get(path) != value for path, value in old.items()
                      if path not in expected or new.get(path) != expected[path])
    def canon(items):
        return Counter((a.get('type'), a.get('location'), a.get('brightness')
                        if a.get('type') == 'set_brightness' else None) for a in items)
    return wrong_state or bool(canon(actions)-canon(gold.get('expected_action', [])))


def _cost(calls, config):
    attempts = [c for c in calls if c['backend'] != 'local-rules']
    model_calls = [c for c in attempts if c['dispatched']]
    money = None
    if not model_calls:
        money = 0
    elif config.online and config.input_usd_per_million is not None and config.output_usd_per_million is not None and all(
            'prompt_tokens' in c['usage'] and 'completion_tokens' in c['usage'] for c in model_calls):
        money = sum((c['usage']['prompt_tokens']*config.input_usd_per_million+
                     c['usage']['completion_tokens']*config.output_usd_per_million)/1e6 for c in model_calls)
    return {'model_requests': len(model_calls), 'model_attempts': len(attempts),
            'probe_requests': sum(c['phase'] == 'probe' for c in model_calls),
            'probe_attempts': sum(c['phase'] == 'probe' for c in attempts),
            'total_tokens': sum(c['total_tokens'] for c in model_calls),
            'provider_tokens': sum(c['total_tokens'] for c in model_calls) if config.online else 0,
            'unknown_usage_requests': sum(not c['usage_known'] for c in model_calls),
            'probe_elapsed_ms': sum(c['elapsed_ms'] for c in attempts if c['phase'] == 'probe'),
            'probe_tokens': sum(c['total_tokens'] for c in model_calls if c['phase'] == 'probe'),
            'model_elapsed_ms': sum(c['elapsed_ms'] for c in attempts), 'usd': money,
            'network_bytes': sum(c['request_text_bytes']+c['response_json_bytes'] for c in model_calls),
            'money_scope': 'provider usage * explicit rates; GPU rental/taxes excluded',
            'bytes_scope': 'UTF-8 task text + serialized response JSON; HTTP headers excluded'}


def _run_one(row, strategy, state, *, config, trace, step, backends, calibration, repeat, position):
    task = classify_task(row['text'])
    if 'deadline_ms' in row:
        task = replace(task, deadline_ms=int(row['deadline_ms']))
    family = str(row.get('family', getattr(task, 'family', task.task_class)))
    request = _Request(task, config, trace, step, backends)
    request.start_clock = max(state['clock_ms'], step*config.step_ms)
    trusted = set(config.trusted_backends) | {'local-rules'}
    cache = _refresh(request, strategy, state, family, calibration, trusted) if strategy in OBSERVED and not _fastpath(task, config) else ObservationCache()
    router = _PolicyRouter(strategy, request, cache, family, trusted)
    adapters = {name: _Adapter(request, name) for name in (*MODELS, 'local-rules')}
    adapters['cascade-model'] = _Cascade(request, calibration, family, trusted)
    runtime_trusted = trusted | ({'cascade-model'} if 'local-model' in trusted else set())
    runtime = TaskRuntime(router=router, backends=adapters, store=DeviceStore(),
                          default_deadline_ms=task.deadline_ms, trusted_backends=runtime_trusted)
    before = runtime.state()
    # Offline virtual probe time consumes the same deadline before TaskRuntime;
    # adapter budget checks prevent virtual late responses from reaching execution.
    effective_start = request.started-request.virtual_ms/1000
    result = runtime.handle(task.text, request_id=f'{repeat}:{row["id"]}:{strategy}',
                            started_at=effective_start, deadline_at=effective_start+task.deadline_ms/1000)
    elapsed = request.elapsed()
    if elapsed > task.deadline_ms and not result['ok']:
        result['error_code'] = 'deadline_exceeded'
    result['initial_state'] = before
    score = semantic_score_row(result, row['gold'])
    calls = request.calls
    if strategy in ('event', 'adaptive'):
        completed = [c for c in calls if c['phase'] != 'probe' and c['backend'] in MODELS]
        if completed:
            latest = completed[-1]
            prior = state['observations'].get(latest['backend'])
            duration = latest['elapsed_ms']
            state['latency_shift'] = bool(prior and max(duration, prior['latency_ms'])/
                max(.001, min(duration, prior['latency_ms'])) >= config.event_latency_ratio)
            state['observations'][latest['backend']] = {'reachable': latest['ok'], 'latency_ms': duration,
                'checked_ms': request.start_clock+elapsed, 'source': 'task'}
    state.update(clock_ms=request.start_clock+elapsed, last_failed=not result['ok'], last_family=family)
    cost = _cost(calls, config)
    return {**result, 'row_id': f'{repeat}:{row["id"]}:{strategy}', 'task_id': row['id'],
            'cluster_id': row.get('cluster_id', row['id']), 'template_group': row.get('template_group'),
            'task_text': task.text, 'family': family, 'strategy': strategy, 'repeat': repeat,
            'trace_step': step, 'arm_position': position, 'deadline_ms': task.deadline_ms,
            'engineering_ok': score['engineering_ok'], 'semantic_success': score['semantic_success'],
            'semantic_reason': score.get('reason'), 'deadline_correct': score['semantic_success'] and elapsed <= task.deadline_ms,
            'wrong_side_effect': _wrong_effect(before, result['state'], row['gold'], (result.get('plan') or {}).get('actions', [])),
            'total_elapsed_ms': elapsed, 'actual_wall_elapsed_ms': (time.perf_counter()-request.started)*1000,
            'virtual_injected_ms': request.virtual_ms,
            'latency_scope': 'outer_request_entry_to_state_including_probes_and_cascade',
            'model_real': any(c['model_real'] for c in calls), 'calls': calls, 'call_count': len(calls),
            'cost': cost, 'total_tokens': cost['total_tokens'], 'model_requests': cost['model_requests'],
            'policy_state_after': copy.deepcopy(state)}


def _clean(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Mapping):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    return value


def _summaries(rows, strategies):
    result = {}
    for name in strategies:
        group = [row for row in rows if row['strategy'] == name]
        n = len(group)
        money = [row['cost']['usd'] for row in group]
        result[name] = {'requests': n,
            **{key: sum(bool(row[key]) for row in group) for key in
               ('engineering_ok', 'semantic_success', 'deadline_correct', 'wrong_side_effect')},
            'semantic_rate': sum(row['semantic_success'] for row in group)/n if n else None,
            'deadline_correct_rate': sum(row['deadline_correct'] for row in group)/n if n else None,
            'p50_total_ms': _percentile([row['total_elapsed_ms'] for row in group], .5),
            'p95_total_ms': _percentile([row['total_elapsed_ms'] for row in group], .95),
            'model_real_rows': sum(row['model_real'] for row in group),
            **{key: sum(row['cost'][key] for row in group) for key in ('model_requests', 'model_attempts', 'probe_requests', 'probe_attempts',
               'probe_elapsed_ms', 'probe_tokens', 'total_tokens', 'provider_tokens', 'unknown_usage_requests')},
            'usd': sum(money) if all(item is not None for item in money) else None,
            'errors': dict(Counter(row['error_code'] for row in group if row.get('error_code')))}
    return result


def report_markdown(report):
    lines = ['# AI6G controlled experiment', '', f'Source: **{report["source_kind"]}**; complete: {report["complete"]}.',
             f'Task SHA-256: `{report["manifest"]["tasks_sha256"]}`; fingerprint: `{report["fingerprint"]}`.', '',
             '| Strategy | Requests | Semantic | Deadline correct | Wrong effects | P50 ms | P95 ms | Model calls | Probes | Tokens |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    def fmt(value):
        return '-' if value is None else f'{value:.3f}'
    for name, entry in report['strategies'].items():
        lines.append(f'| {name} | {entry["requests"]} | {entry["semantic_success"]} | {entry["deadline_correct"]} | '
                     f'{entry["wrong_side_effect"]} | {fmt(entry["p50_total_ms"])} | {fmt(entry["p95_total_ms"])} | '
                     f'{entry["model_requests"]} | {entry["probe_requests"]} | {entry["total_tokens"]} |')
    lines += ['', 'Gold is used offline only. Every arm/task has a fresh software device state.',
              'All arms execute through product TaskRuntime, validate_plan and DeviceStore; observation arms delegate choice to product TaskRouter.',
              'Latency includes synchronous probes, cascade attempts and injected impairments; probe cost is also itemized, never subtracted.',
              'Offline injected time is virtual. Online impairments sleep; these software impairments do not measure a physical 6G link.',
              'Money is unknown without provider usage and explicit rates; GPU rental/taxes are excluded.',
              'Quality comes from frozen calibration, or remains unknown. Trust is explicit deployment configuration.',
              'Cyclic arm rotation is deterministic. Paired cluster bootstrap intervals are descriptive, without multiplicity adjustment.', '']
    for item in report['comparisons']:
        lines.append(f'- [{item["comparison_role"]}] {item["arm"]} vs {item["reference"]}, {item["metric"]}: {item["estimate"]:.4f}, '
                     f'CI95 {item["ci95"]}, clusters={item["clusters"]}.')
    if report['source_kind'] == 'contract_simulator':
        lines += ['', '**Contract simulator only: numbers do not measure a real model or live network.**']
    return '\n'.join(lines)+'\n'


def evaluate(tasks_path, out_path, *, calibration_path=None, trace_path=None, config=None,
             backends=None, resume=False, stop_after=None):
    config = config or ExperimentConfig()
    tasks_path, out_path = Path(tasks_path), Path(out_path)
    tasks = load_frozen_tasks(tasks_path)
    calibration = _calibration(calibration_path)
    if config.online and not calibration and set(config.strategies) & (OBSERVED | {'cascade'}):
        raise ValueError('online measured-quality strategies require --calibration')
    backends = dict(backends) if backends is not None else build_backends(config)
    if config.online and any(isinstance(backend, ContractSimulator) for backend in backends.values()):
        raise ValueError('online cannot use contract simulator')
    trace = Trace(trace_path)
    package = Path(__file__).resolve().parents[1]
    sources = ('evaluation/controlled.py', 'runtime.py', 'router.py', 'protocol.py',
               'executor.py', 'models.py', 'evaluator.py', 'health_monitor.py')
    manifest = {'schema': 'ai6g.controlled.manifest.v1', 'tasks_sha256': _file_hash(tasks_path),
                'calibration_sha256': _file_hash(calibration_path) if calibration_path else None,
                'trace_sha256': _file_hash(trace_path) if trace_path else None, 'config': asdict(config),
                'sources': {name: _file_hash(package/name) for name in sources},
                'backends': _backend_identity(backends), 'task_count': len(tasks),
                'gold_online': False, 'quality_source': 'frozen_calibration_or_unknown',
                'calibration_is_scenario_input_offline': not config.online}
    fingerprint = _hash(manifest)
    journal, manifest_path = out_path.with_suffix('.rows.jsonl'), out_path.with_suffix('.manifest.json')
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows, completed = [], set()
    states = {name: _initial_policy() for name in config.strategies}
    if manifest_path.exists() or journal.exists():
        if not resume:
            raise ValueError('output already exists; use --resume or a new output path')
        previous = json.loads(manifest_path.read_text(encoding='utf-8'))
        if previous['fingerprint'] != fingerprint:
            raise ValueError('frozen experiment fingerprint mismatch')
        lines = journal.read_bytes().splitlines(keepends=True) if journal.exists() else []
        valid_bytes = 0
        for index, line in enumerate(lines):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                if index != len(lines)-1:
                    raise ValueError('corrupt journal before final row')
                break
            if row['row_id'] in completed:
                raise ValueError('duplicate completed journal row')
            completed.add(row['row_id'])
            rows.append(row)
            states[row['strategy']] = row['policy_state_after']
            valid_bytes += len(line)
        if journal.exists():
            with journal.open('r+b') as handle:
                handle.truncate(valid_bytes)
                if valid_bytes and not lines[len(rows)-1].endswith(b'\n'):
                    handle.seek(0, os.SEEK_END)
                    handle.write(b'\n')
    else:
        manifest_path.write_text(json.dumps({'fingerprint': fingerprint, 'manifest': manifest},
                                            ensure_ascii=False, indent=2), encoding='utf-8')
    orders, executed, stopped = [], 0, False
    for repeat in range(config.repeats):
        for index, task in enumerate(tasks):
            step = repeat*len(tasks)+index
            shift = step % len(config.strategies)
            order = list(config.strategies[shift:]+config.strategies[:shift])
            orders.append(order)
            for position, name in enumerate(order):
                row_id = f'{repeat}:{task["id"]}:{name}'
                if row_id in completed:
                    continue
                if stop_after is not None and executed >= stop_after:
                    stopped = True
                    break
                row = _clean(_run_one(task, name, states[name], config=config, trace=trace, step=step,
                    backends=backends, calibration=calibration, repeat=repeat, position=position))
                with journal.open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n')
                    handle.flush()
                    os.fsync(handle.fileno())
                rows.append(row)
                completed.add(row_id)
                executed += 1
            if stopped:
                break
        if stopped:
            break
    complete = len(rows) == len(tasks)*config.repeats*len(config.strategies)
    comparisons = []
    if complete and len(config.strategies) > 1:
        reference = 'fixed_local' if 'fixed_local' in config.strategies else config.strategies[0]
        pairs = [(name, reference) for name in config.strategies if name != reference]
        primary_pairs = [('adaptive', baseline) for baseline in ('legal_cloud', 'fixed_ttl')
                         if 'adaptive' in config.strategies and baseline in config.strategies]
        pairs = list(dict.fromkeys(primary_pairs+pairs))
        for name, baseline in pairs:
            for metric in ('deadline_correct', 'semantic_success', 'wrong_side_effect', 'total_elapsed_ms',
                           'total_tokens', 'model_requests'):
                item = paired_bootstrap(rows, name, baseline, metric=metric,
                                        samples=config.bootstrap_samples, seed=config.seed)
                item['comparison_role'] = ('primary' if metric == 'deadline_correct' else 'secondary_predeclared_pair') if (name, baseline) in primary_pairs else 'exploratory'
                comparisons.append(item)
    report = {'schema': 'ai6g.controlled.v1', 'source_kind': 'real_endpoints' if config.online else 'contract_simulator',
              'complete': complete, 'fingerprint': fingerprint, 'manifest': manifest,
              'task_count': len(tasks), 'repeats': config.repeats, 'strategy_order': orders,
              'strategies': _summaries(rows, config.strategies), 'comparisons': comparisons, 'rows': rows}
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    out_path.with_suffix('.md').write_text(report_markdown(report), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--tasks', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--calibration')
    parser.add_argument('--trace')
    parser.add_argument('--online', action='store_true')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--strategies', default=','.join(STRATEGIES))
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--no-rule-fastpath', action='store_true')
    parser.add_argument('--no-safety-fastpath', action='store_true')
    parser.add_argument('--trusted-backends', default='', help='comma-separated verified deployment aliases; default none')
    for name, default in [('ttl-ms', 5000), ('min-ttl-ms', 250), ('max-ttl-ms', 10000),
                          ('step-ms', 1000), ('probe-timeout-ms', 3000)]:
        parser.add_argument('--'+name, type=float, default=default)
    parser.add_argument('--bootstrap-samples', type=int, default=2000)
    parser.add_argument('--seed', type=int, default=20261104)
    parser.add_argument('--input-usd-per-million', type=float)
    parser.add_argument('--output-usd-per-million', type=float)
    args = parser.parse_args(argv)
    try:
        config = ExperimentConfig(strategies=tuple(args.strategies.split(',')), repeats=args.repeats,
            online=args.online, shared_rule_fastpath=not args.no_rule_fastpath,
            shared_safety_fastpath=not args.no_safety_fastpath,
            trusted_backends=tuple(name for name in args.trusted_backends.split(',') if name),
            ttl_ms=args.ttl_ms, min_ttl_ms=args.min_ttl_ms, max_ttl_ms=args.max_ttl_ms,
            step_ms=args.step_ms, probe_timeout_ms=args.probe_timeout_ms,
            bootstrap_samples=args.bootstrap_samples, seed=args.seed,
            input_usd_per_million=args.input_usd_per_million,
            output_usd_per_million=args.output_usd_per_million)
        report = evaluate(args.tasks, args.out, calibration_path=args.calibration,
                          trace_path=args.trace, config=config, resume=args.resume)
    except (ValueError, OSError, KeyError) as exc:
        parser.error(str(exc))
    print(report_markdown(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
