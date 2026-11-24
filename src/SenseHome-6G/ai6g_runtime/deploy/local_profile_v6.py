"""Loopback v6 deployment with guard-bound calibration and budgeted observation."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from http.server import ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import sys
import threading
import time
from urllib.parse import urlsplit

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai6g_runtime.deploy import local_profile_v5 as v5
from ai6g_runtime.executor import DeviceStore
from ai6g_runtime.grounding_v6 import GroundedBackend
from ai6g_runtime.local_resources import LocalResourceProvider, _local_url
from ai6g_runtime.models import OpenAICompatibleBackend, RuleBackend
from ai6g_runtime.observability_v6 import BudgetedHealthMonitor, LatencyEvidence
from ai6g_runtime.protocol import classify_task
from ai6g_runtime.router import ObservationCache, TaskRouter
from ai6g_runtime.runtime import TaskRuntime
from ai6g_runtime.serve import RuntimeHandler, _health_url

SEED, MAX_TOKENS, DEFAULT_PROMPT = v5.SEED, v5.MAX_TOKENS, v5.DEFAULT_PROMPT
GUARD_FILE = Path(__file__).resolve().parents[1]/'grounding_v6.py'


@dataclass(frozen=True)
class LocalProfile(v5.LocalProfile):
    latencies: dict


class LocalProfileHandler(RuntimeHandler):
    """Expose runtime tasks and reads while reserving health publication internally."""
    def do_POST(self):
        if self.path == '/v1/health':
            self.close_connection = True
            self._send(403, {'ok': False, 'error_code': 'health_read_only',
                'error': 'Health observations are published only by the internal monitor; this endpoint is read-only.'})
            return
        super().do_POST()


def load_profile(endpoints_file, prompt_file, calibration_file):
    """Verify the raw capture through v5, then bind its v6 guard projection."""
    loaded = v5.load_profile(endpoints_file, prompt_file, calibration_file)
    endpoints = v5._read_json(endpoints_file)
    payload = v5._read_json(calibration_file)
    pipeline = payload.get('scoring_pipeline')
    guard_sha = v5._sha(GUARD_FILE)
    if endpoints.get('project_version') != 6 or payload.get('project_version') != 6:
        raise ValueError('explicit v6 deployment and derived runtime calibration required')
    if not isinstance(pipeline, dict) or pipeline.get('guard_sha256') != guard_sha:
        raise ValueError('runtime calibration guard SHA256 is missing or stale')
    if (pipeline.get('kind') != 'offline_projection_of_captured_output_through_shared_grounding_guard'
            or pipeline.get('quality_population') != 'public model-eligible classifier subset'
            or pipeline.get('capture_original_unchanged') is not True
            or type(pipeline.get('actual_model_dispatches_in_this_projection')) is not int
            or pipeline['actual_model_dispatches_in_this_projection'] != 0
            or not isinstance(pipeline.get('script_sha256'), str)
            or not re.fullmatch('[0-9a-fA-F]{64}', pipeline['script_sha256'])):
        raise ValueError('explicit offline scoring pipeline binding required')
    sidecar = v5._read_json(Path(calibration_file).with_name('calibration_capture.manifest.json'))
    manifest = sidecar['manifest']
    if payload.get('capture_manifest') != sidecar:
        raise ValueError('derived calibration capture manifest differs from original sidecar')
    if (manifest.get('deployment_identity_sha256') != v5._sha(endpoints_file)
            or manifest.get('deployment_identity') != endpoints):
        raise ValueError('calibration deployment identity differs from supplied endpoints')
    measured = payload.get('measured_at')
    if (isinstance(measured, bool) or not isinstance(measured, (int, float))
            or not math.isfinite(measured) or not 0 <= measured <= time.time()):
        raise ValueError('calibration measured_at must be a finite historical timestamp')
    services, latencies = {}, {}
    by_name = {item['name']: item for item in endpoints['services']}
    for alias, service in loaded.services.items():
        raw = by_name[service['name']]
        if raw.get('parallel') != 1 or isinstance(raw.get('parallel'), bool):
            raise ValueError('v6 deployment requires explicitly recorded single-slot model capacity')
        health = raw.get('health_url')
        if not isinstance(health, str):
            raise ValueError('explicit local model health URL required')
        _local_url(health)
        chat, live = urlsplit(service['endpoint']), urlsplit(health)
        if ((chat.scheme, chat.hostname, chat.port) != (live.scheme, live.hostname, live.port)
                or live.path != '/health'):
            raise ValueError('model /health must share its chat endpoint origin')
        value = payload['backends'][alias].get('latency_p95_ms')
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value <= 0):
            raise ValueError('measured positive calibration latency_p95_ms required')
        latencies[alias] = LatencyEvidence(float(value), payload['source_sha256'], float(measured))
        services[alias] = {**service, 'health_url': health, 'parallel': 1, 'capacity': 1}
    metadata = {**loaded.metadata, 'schema': 'sensehome.local_profile.v6', 'project_version': 6,
                'guard_sha256': guard_sha, 'scoring_pipeline': pipeline,
                'calibration_measured_at': measured,
                'latencies': {name: asdict(value) for name, value in latencies.items()},
                'max_inflight': 1, 'observation_policy': 'HTTP liveness, offline quality/p95, budgeted raw canary and passive business latency'}
    return LocalProfile(services, loaded.prompt, loaded.calibration, metadata, latencies)


class AuditedLocalBackend(OpenAICompatibleBackend):
    """Capture the original model result before any grounding intervention."""
    def __init__(self, *args, audit_path, audit_lock, **kwargs):
        super().__init__(*args, **kwargs)
        self.audit_path, self.audit_lock = Path(audit_path), audit_lock

    def infer(self, task, timeout_s):
        started = time.time()
        result = super().infer(task, timeout_s)
        record = {'schema': 'sensehome.local_model_call.v6', 'started_at': started,
                  'finished_at': time.time(), 'backend': self.name,
                  'scope': 'health_probe' if getattr(task, 'intent', None) == 'health_probe' else 'task',
                  'model': self.model, 'endpoint': self.endpoint,
                  'text': str(getattr(task, 'text', task)), 'result': asdict(result)}
        with self.audit_lock, self.audit_path.open('a', encoding='utf8') as handle:
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False)+'\n')
        return result


class PassiveBusinessBackend:
    """Measure business wall time without generating a monitoring request."""
    def __init__(self, backend, monitor):
        self.backend, self.monitor, self.name = backend, monitor, backend.name

    def __getattr__(self, key):
        return getattr(self.backend, key)

    def infer(self, task, timeout_s):
        if isinstance(task, str):
            task = classify_task(task)
        started = time.perf_counter()
        ok = False
        try:
            result = self.backend.infer(task, timeout_s)
            ok = bool(result.ok)
            return result
        finally:
            if getattr(task, 'intent', None) != 'health_probe':
                self.monitor.record_business(self.name, ok=ok,
                    latency_ms=max(0., (time.perf_counter()-started)*1000))


class QueueBoundCache(ObservationCache):
    """Project queue bounds only in a choosing thread; writes keep raw evidence."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._selection = threading.local()

    @contextmanager
    def routing_at(self, now, resource_ttl_s):
        previous = getattr(self._selection, 'view', None)
        self._selection.view = (now, resource_ttl_s)
        try:
            yield
        finally:
            self._selection.view = previous

    def get(self, backend):
        observed = super().get(backend)
        view = getattr(self._selection, 'view', None)
        if observed is None or view is None or observed.resources is None:
            return observed
        resource = observed.resources
        if resource.observed_at is None or resource.queue_ms is None:
            return observed
        age = view[0]-resource.observed_at
        if 0 <= age <= view[1]:
            return replace(observed, latency_ms=max(observed.latency_ms, resource.queue_ms))
        return observed


class QueueBoundRouter(TaskRouter):
    def __init__(self, cache=None, **kwargs):
        selected = QueueBoundCache() if cache is None else cache
        if not isinstance(selected, QueueBoundCache):
            raise ValueError('queue-bound router requires its nonmutating observation cache')
        super().__init__(selected, **kwargs)

    def _choose_locked(self, task, now=None):
        current = time.time() if now is None else float(now)
        with self.cache.routing_at(current, self.resource_ttl_s):
            return super()._choose_locked(task, current)


class LocalProfileServer(v5.LocalProfileServer):
    """Own a fresh runtime directory and HTTP/monitor threads; never model PIDs."""
    def __init__(self, profile, *, state_dir, host='127.0.0.1', port=8091,
                 probe_interval_s=2., probe_timeout_s=5., gpu_selector='0',
                 nvidia_smi='nvidia-smi', canary_ttl_s=60., min_canary_interval_s=30.):
        if host not in {'127.0.0.1', 'localhost', '::1'}:
            raise ValueError('runtime host must be loopback')
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError('port must be 0..65535')
        for name, value, allow_zero in [('probe interval', probe_interval_s, True),
                                       ('probe timeout', probe_timeout_s, False),
                                       ('canary TTL', canary_ttl_s, False),
                                       ('canary minimum interval', min_canary_interval_s, False)]:
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0 or (not allow_zero and value == 0)):
                raise ValueError(f'{name} must be finite and positive (interval may be zero)')
        if min_canary_interval_s > canary_ttl_s:
            raise ValueError('canary minimum interval must fit TTL')
        self.host = '127.0.0.1' if host == 'localhost' else host
        self.port, self.profile = port, profile
        self.interval, self.timeout = float(probe_interval_s), float(probe_timeout_s)
        self.state_dir = Path(state_dir).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=False)
        self.server = self.server_thread = self.probe_thread = self.probe_stop = None
        self.closed = False
        audit_lock = threading.Lock()
        models = {name: AuditedLocalBackend(name, service['endpoint'], service['model'],
                    system_prompt=profile.prompt, seed=SEED, max_tokens=MAX_TOKENS,
                    audit_path=self.state_dir/'model_calls.jsonl', audit_lock=audit_lock)
                  for name, service in profile.services.items()}
        trusted = set(profile.services)
        self.runtime = TaskRuntime(router=QueueBoundRouter(QueueBoundCache(ttl_s=5.),
            trusted_backends=trusted, max_inflight=1), backends={'local-rules': RuleBackend()},
            store=DeviceStore(state_path=self.state_dir/'state.json'),
            log_path=self.state_dir/'requests.jsonl', trusted_backends=trusted)
        self.resource_provider = LocalResourceProvider(
            {name: service['slots_url'] for name, service in profile.services.items()},
            gpu_selector=gpu_selector, nvidia_smi=nvidia_smi,
            evidence_path=self.state_dir/'resources.jsonl')
        self.monitor = BudgetedHealthMonitor(backends=models,
            health_urls={name: service['health_url'] for name, service in profile.services.items()},
            calibration=profile.calibration, latencies=profile.latencies, publish=self.runtime.health,
            resource_provider=self.resource_provider, canary_ttl_s=canary_ttl_s,
            min_canary_interval_s=min_canary_interval_s, max_backoff_s=max(300., min_canary_interval_s),
            canary_timeout_s=self.timeout, evidence_path=self.state_dir/'probes.jsonl')
        self.runtime.backends.update({name: PassiveBusinessBackend(GroundedBackend(backend), self.monitor)
                                      for name, backend in models.items()})
        self.metadata = {**profile.metadata, 'pid': os.getpid(), 'status': 'created',
                         'created_at': time.time(), 'probe_interval_s': self.interval,
                         'probe_timeout_s': self.timeout, 'canary_ttl_s': canary_ttl_s,
                         'min_canary_interval_s': min_canary_interval_s,
                         'queue_latency_policy': 'max(measured_inference_wall_ms, fresh_known_queue_ms); no addition',
                         'state_single_writer': 'exclusive new directory, owned by one runtime process',
                         'model_services_owned': False}
        self._metadata()

    def start(self):
        if self.closed or self.server is not None:
            raise RuntimeError('profile server already started or stopped')
        handler = type('BoundLocalProfileV6Handler', (LocalProfileHandler,), {'runtime': self.runtime})
        server_type = ThreadingHTTPServer
        if self.host == '::1':
            server_type = type('IPv6LocalProfileV6Server', (ThreadingHTTPServer,), {'address_family': socket.AF_INET6})
        self.server = server_type((self.host, self.port), handler)
        self.port = int(self.server.server_address[1])
        self.server_thread = threading.Thread(target=self.server.serve_forever,
            name='ai6g-local-profile-v6-http', daemon=True)
        self.server_thread.start()
        self.probe_stop = threading.Event()

        def worker():
            self.monitor.run_once()
            if self.interval:
                while not self.probe_stop.wait(self.interval):
                    self.monitor.run_once()

        self.probe_thread = threading.Thread(target=worker, name='ai6g-budgeted-health-v6', daemon=True)
        self.probe_thread.start()
        self.metadata.update(status='running', started_at=time.time(),
                             url=_health_url(self.host, self.port).removesuffix('/v1/health'))
        self._metadata()
        return self

    def close(self):
        if self.closed:
            return
        super().close()
        self.metadata['monitor_totals'] = dict(self.monitor.totals)
        self._metadata()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoints-file', required=True)
    parser.add_argument('--prompt-file', default=str(DEFAULT_PROMPT))
    parser.add_argument('--calibration', required=True)
    parser.add_argument('--state-dir', required=True, help='must be a new directory')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8091)
    parser.add_argument('--probe-interval', type=float, default=2.)
    parser.add_argument('--probe-timeout', type=float, default=5.)
    parser.add_argument('--canary-ttl', type=float, default=60.)
    parser.add_argument('--min-canary-interval', type=float, default=30.)
    parser.add_argument('--gpu', default='0')
    parser.add_argument('--nvidia-smi', default='nvidia-smi')
    args = parser.parse_args(argv)
    try:
        loaded = load_profile(args.endpoints_file, args.prompt_file, args.calibration)
        with LocalProfileServer(loaded, state_dir=args.state_dir, host=args.host, port=args.port,
                probe_interval_s=args.probe_interval, probe_timeout_s=args.probe_timeout,
                gpu_selector=args.gpu, nvidia_smi=args.nvidia_smi,
                canary_ttl_s=args.canary_ttl, min_canary_interval_s=args.min_canary_interval) as app:
            stop, previous = threading.Event(), {}
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, lambda *_: stop.set())
            try:
                app.start()
                print(json.dumps({'status': 'running', 'url': app.metadata['url'],
                    'state_dir': str(app.state_dir), 'pid': os.getpid()}, ensure_ascii=False), flush=True)
                while not stop.wait(.2):
                    if (app.state_dir/'STOP').exists():
                        break
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
