"""Explicit, loopback-only V5 local-model runtime; existing model services are external."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
from http.server import ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time
from urllib.parse import urlsplit

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from ai6g_runtime.executor import DeviceStore
from ai6g_runtime.health_monitor import load_calibration
from ai6g_runtime.local_resources import LocalResourceProvider, _local_url
from ai6g_runtime.models import OpenAICompatibleBackend, RuleBackend
from ai6g_runtime.router import ObservationCache, TaskRouter
from ai6g_runtime.runtime import TaskRuntime
from ai6g_runtime.serve import RuntimeHandler, _health_url, start_probe_thread

SEED = 20261104
MAX_TOKENS = 384
SCHEMAS = {'sensehome.local_endpoints.v5', 'sensehome.local_endpoints.v5b',
           'sensehome.deployment_identity.v5'}
ALIASES = {'local-small': 'local-model', 'local-large': 'cloud-model'}
DEFAULT_PROMPT = Path(__file__).with_name('household_protocol_prompt_v5.txt')


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _canonical_sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


@dataclass(frozen=True)
class LocalProfile:
    services: dict
    prompt: str
    calibration: dict
    metadata: dict


def load_profile(endpoints_file, prompt_file, calibration_file):
    """Bind two named models to the exact measured prompt and capture evidence.

    Calibration companions are required alongside calibration.json. No network
    operation or model inference occurs while validating these local files.
    """
    endpoints_file, prompt_file, calibration_file = map(Path,
        (endpoints_file, prompt_file, calibration_file))
    endpoints = _read_json(endpoints_file)
    if not isinstance(endpoints, dict) or endpoints.get('schema') not in SCHEMAS:
        raise ValueError('unknown local endpoint profile schema')
    services = {}
    raw = endpoints.get('services')
    if not isinstance(raw, list) or len(raw) != 2:
        raise ValueError('exactly local-small and local-large services required')
    for item in raw:
        if not isinstance(item, dict) or item.get('name') not in ALIASES:
            raise ValueError('unknown local service name')
        alias = ALIASES[item['name']]
        if alias in services:
            raise ValueError('duplicate local service name')
        model = item.get('model')
        if not isinstance(model, str) or not model.strip():
            raise ValueError('nonempty model alias required')
        for key, value in item.items():
            if key.lower() in {'api_key', 'apikey', 'authorization', 'token', 'password'} and value:
                raise ValueError('local profile does not accept credentials')
            if key == 'endpoint' or key.endswith('_url'):
                if not isinstance(value, str):
                    raise ValueError('endpoint URL must be a string')
                _local_url(value)
        endpoint, slots = item.get('endpoint'), item.get('slots_url')
        if not isinstance(endpoint, str) or not isinstance(slots, str):
            raise ValueError('explicit endpoint and slots_url required')
        parsed, slots_parsed = urlsplit(endpoint), urlsplit(slots)
        if parsed.path != '/v1/chat/completions' or slots_parsed.path != '/slots':
            raise ValueError('chat-completions and slots paths required')
        if (parsed.scheme, parsed.hostname, parsed.port) != (
                slots_parsed.scheme, slots_parsed.hostname, slots_parsed.port):
            raise ValueError('slots endpoint must share the model origin')
        services[alias] = {key: item[key] for key in ('name', 'model', 'endpoint', 'slots_url')}
        for key in ('weight_sha256', 'runtime_sha256', 'license', 'inference_backend', 'device_name'):
            if key in item:
                services[alias][key] = item[key]
    if len({item['endpoint'] for item in services.values()}) != 2:
        raise ValueError('two distinct model endpoints required')
    # Frozen experiment_v5 sends a stripped string. controlled._backend_identity
    # hashes the JSON scalar rather than its bare UTF-8 bytes.
    prompt = prompt_file.read_text(encoding='utf-8-sig').strip()
    if not prompt.strip():
        raise ValueError('prompt file must be nonempty')
    prompt_sha = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
    prompt_canonical_sha = _canonical_sha(prompt)
    calibration = load_calibration(calibration_file)
    if set(calibration) != set(services):
        raise ValueError('calibration must contain exactly local-model and cloud-model')
    capture = calibration_file.with_name('calibration_capture.jsonl')
    manifest_file = capture.with_suffix('.manifest.json')
    source_sha = _sha(capture)
    calibration_payload = _read_json(calibration_file)
    if calibration_payload.get('source_sha256') != source_sha or any(
            entry.source_sha256 != source_sha for entry in calibration.values()):
        raise ValueError('calibration source hash differs from actual capture bytes')
    metadata = _read_json(manifest_file)
    manifest = metadata.get('manifest', {})
    fingerprint = metadata.get('fingerprint')
    if manifest.get('schema') != 'ai6g.model_capture.manifest.v5' or fingerprint != _canonical_sha(manifest):
        raise ValueError('invalid calibration manifest fingerprint')
    count = 0
    for line in capture.read_text(encoding='utf-8').splitlines():
        if line.strip():
            count += 1
            if json.loads(line).get('capture_fingerprint') != fingerprint:
                raise ValueError('foreign row in calibration capture')
    if count == 0:
        raise ValueError('calibration capture must not be empty')
    for alias, service in services.items():
        measured = manifest.get('backends', {}).get(alias, {})
        decoding = manifest.get('decoding', {}).get(alias, {})
        expected = {'model': service['model'], 'endpoint': service['endpoint'],
                    'prompt_sha256': prompt_canonical_sha, 'max_tokens': MAX_TOKENS}
        if any(measured.get(key) != value for key, value in expected.items()):
            raise ValueError(f'calibration model/endpoint/prompt mismatch for {alias}')
        if any(decoding.get(key) != value for key, value in {
                'seed': SEED, 'max_tokens': MAX_TOKENS, 'temperature': 0,
                'response_format': 'json_object'}.items()):
            raise ValueError(f'calibration decoding mismatch for {alias}')
        # A selected profile may include additional deployment metadata, but
        # supplied weight hashes must agree with the measured identity.
        identity = manifest.get('deployment_identity', {}).get('services', [])
        measured_identity = next((row for row in identity if row.get('name') == service['name']), {})
        if measured_identity and service.get('weight_sha256') != measured_identity.get('weight_sha256'):
            raise ValueError(f'calibration weight identity mismatch for {alias}')
    return LocalProfile(services, prompt, calibration, {
        'schema': 'sensehome.local_profile.v5', 'endpoint_profile_schema': endpoints['schema'],
        'endpoints_sha256': _sha(endpoints_file), 'prompt_file_sha256': _sha(prompt_file),
        'prompt_effective_sha256': prompt_sha, 'prompt_manifest_sha256': prompt_canonical_sha,
        'calibration_sha256': _sha(calibration_file),
        'calibration_capture_sha256': source_sha, 'calibration_fingerprint': fingerprint,
        'seed': SEED, 'max_tokens': MAX_TOKENS, 'temperature': 0,
        'response_format': 'json_object', 'services': services,
        'trusted_backends': sorted(services),
        'topology': 'two logical models on one machine, shared physical GPU/RAM; not an edge/cloud or 6G test',
        'queue_ms': None,
        'resource_scope': 'read-only GPU global free memory and model slots; queue remains unknown',
    })


class AuditedLocalBackend(OpenAICompatibleBackend):
    """Persist complete API responses for both business calls and probe calls."""
    def __init__(self, *args, audit_path, audit_lock, **kwargs):
        super().__init__(*args, **kwargs)
        self.audit_path, self.audit_lock = Path(audit_path), audit_lock

    def infer(self, task, timeout_s):
        started = time.time()
        result = super().infer(task, timeout_s)
        record = {'schema': 'sensehome.local_model_call.v5', 'started_at': started,
                  'finished_at': time.time(), 'backend': self.name,
                  'scope': 'health_probe' if getattr(task, 'intent', None) == 'health_probe' else 'task',
                  'model': self.model, 'text': str(getattr(task, 'text', task)),
                  'result': asdict(result)}
        with self.audit_lock, self.audit_path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False)+'\n')
        return result


class LocalProfileServer:
    """Own only a fresh state directory and this runtime's HTTP/probe threads."""
    def __init__(self, profile, *, state_dir, host='127.0.0.1', port=8091,
                 probe_interval_s=2., probe_timeout_s=5., gpu_selector='0',
                 nvidia_smi='nvidia-smi'):
        if host not in {'127.0.0.1', 'localhost', '::1'}:
            raise ValueError('runtime host must be loopback')
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise ValueError('port must be 0..65535')
        if not math.isfinite(probe_interval_s) or probe_interval_s < 0:
            raise ValueError('probe interval must be finite and nonnegative')
        if not math.isfinite(probe_timeout_s) or probe_timeout_s <= 0:
            raise ValueError('probe timeout must be finite and positive')
        self.host = '127.0.0.1' if host == 'localhost' else host
        self.port, self.profile = port, profile
        self.interval, self.timeout = probe_interval_s, probe_timeout_s
        self.state_dir = Path(state_dir).resolve()
        # Atomic exclusive mkdir prevents two processes from sharing a state file.
        # It also prevents accidental reuse or overwriting of prior evidence.
        self.state_dir.mkdir(parents=True, exist_ok=False)
        self.server = self.server_thread = self.probe_thread = self.probe_stop = None
        self.closed = False
        audit_lock = threading.Lock()
        backends = {'local-rules': RuleBackend()}
        for name, service in profile.services.items():
            backends[name] = AuditedLocalBackend(name, service['endpoint'], service['model'],
                system_prompt=profile.prompt, seed=SEED, max_tokens=MAX_TOKENS,
                audit_path=self.state_dir/'model_calls.jsonl', audit_lock=audit_lock)
        trusted = set(profile.services)
        self.runtime = TaskRuntime(router=TaskRouter(ObservationCache(ttl_s=5.),
            trusted_backends=trusted), backends=backends,
            store=DeviceStore(state_path=self.state_dir/'state.json'),
            log_path=self.state_dir/'requests.jsonl', trusted_backends=trusted)
        self.resource_provider = LocalResourceProvider(
            {name: service['slots_url'] for name, service in profile.services.items()},
            gpu_selector=gpu_selector, nvidia_smi=nvidia_smi,
            evidence_path=self.state_dir/'resources.jsonl')
        self.metadata = {**profile.metadata, 'pid': os.getpid(), 'status': 'created',
                         'created_at': time.time(), 'probe_interval_s': self.interval,
                         'probe_timeout_s': self.timeout,
                         'state_single_writer': 'exclusive new directory, owned by one runtime process'}
        self._metadata()

    def _metadata(self):
        temporary = self.state_dir/'profile.json.tmp'
        temporary.write_text(json.dumps(self.metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        # Windows readers opened without FILE_SHARE_DELETE can briefly block
        # atomic replacement. Preserve the old complete JSON during bounded
        # retries, and surface a persistent permission denial to the caller.
        for attempt in range(20):
            try:
                temporary.replace(self.state_dir/'profile.json')
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(.05)

    def start(self):
        if self.closed or self.server is not None:
            raise RuntimeError('profile server already started or stopped')
        handler = type('BoundLocalProfileHandler', (RuntimeHandler,), {'runtime': self.runtime})
        server_type = ThreadingHTTPServer
        if self.host == '::1':
            server_type = type('IPv6LocalProfileServer', (ThreadingHTTPServer,), {'address_family': socket.AF_INET6})
        self.server = server_type((self.host, self.port), handler)
        self.port = int(self.server.server_address[1])
        self.server_thread = threading.Thread(target=self.server.serve_forever,
            name='ai6g-local-profile-http', daemon=True)
        self.server_thread.start()
        self.probe_stop, self.probe_thread = start_probe_thread(self.runtime,
            health_url=_health_url(self.host, self.port), calibration=self.profile.calibration,
            interval_s=self.interval, timeout_s=self.timeout,
            probe_log=self.state_dir/'probes.jsonl', resource_provider=self.resource_provider)
        self.metadata.update(status='running', started_at=time.time(),
                             url=_health_url(self.host, self.port).removesuffix('/v1/health'))
        self._metadata()
        return self

    def close(self):
        if self.closed:
            return
        if self.probe_stop is not None:
            self.probe_stop.set()
        if self.probe_thread is not None:
            # Keep HTTP health publication alive until the in-flight probe ends.
            self.probe_thread.join()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(timeout=2)
        self.closed = True
        self.metadata.update(status='stopped', stopped_at=time.time())
        self._metadata()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--endpoints-file', required=True)
    parser.add_argument('--prompt-file', default=str(DEFAULT_PROMPT))
    parser.add_argument('--calibration', required=True)
    parser.add_argument('--state-dir', required=True, help='must be a new directory')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8091, help='0 lets the OS select a free port')
    parser.add_argument('--probe-interval', type=float, default=2.)
    parser.add_argument('--probe-timeout', type=float, default=5.)
    parser.add_argument('--gpu', default='0')
    parser.add_argument('--nvidia-smi', default='nvidia-smi')
    args = parser.parse_args(argv)
    try:
        loaded = load_profile(args.endpoints_file, args.prompt_file, args.calibration)
        with LocalProfileServer(loaded, state_dir=args.state_dir, host=args.host, port=args.port,
                probe_interval_s=args.probe_interval, probe_timeout_s=args.probe_timeout,
                gpu_selector=args.gpu, nvidia_smi=args.nvidia_smi) as app:
            stop = threading.Event()
            previous = {}
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
