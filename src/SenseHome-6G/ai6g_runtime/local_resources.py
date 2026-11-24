"""Read-only local llama.cpp slots and physical-GPU telemetry.

No inference, queue estimate or synthetic load is generated. GPU memory is a
global shared physical-device reading, never per-backend exclusive capacity.
"""
from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import math
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from ai6g_runtime.router import HealthObservation, ResourceObservation
else:
    from .router import HealthObservation, ResourceObservation

SOURCE = 'local_readonly_nvidia_smi_gpu_global_shared_and_llama_slots'
SENSITIVE_KEYS = {'api_key', 'apikey', 'authorization', 'password', 'access_token',
                  'refresh_token', 'token', 'prompt', 'content', 'messages', 'text',
                  'generated_text', 'input', 'output', 'system_prompt', 'user_prompt',
                  'last_output', 'prompt_text', 'input_text', 'output_text'}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def redact(value):
    """Keep slot metadata while omitting prompt/response text and credentials."""
    if isinstance(value, dict):
        return {str(key): '[redacted]' if str(key).lower() in SENSITIVE_KEYS else redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def _local_url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise ValueError('resource and health endpoints must be loopback HTTP(S) URLs')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('URL credentials, query and fragment are not accepted')
    return value


def _number(value):
    try:
        numeric = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return numeric if math.isfinite(numeric) and numeric >= 0 else None


def parse_gpu_csv(text, selector='0'):
    """Select one exact GPU index or UUID; unavailable values stay unknown."""
    for row in csv.reader(io.StringIO(text), skipinitialspace=True):
        if len(row) != 5:
            continue
        index, uuid, name, free, total = [item.strip() for item in row]
        if str(selector) not in (index, uuid):
            continue
        free, total = _number(free), _number(total)
        if free is None or total is None or free > total:
            return None
        return {'index': index, 'uuid': uuid, 'name': name,
                'free_mem_mb': free, 'total_mem_mb': total, 'unit': 'MiB'}
    return None


def _fetch_json(url, timeout):
    opener = _opener()
    with opener.open(url, timeout=timeout) as response:
        data = response.read(2*1024*1024+1)
    if len(data) > 2*1024*1024:
        raise ValueError('slots response too large')
    return json.loads(data.decode('utf-8'))


class LocalResourceProvider:
    """HealthMonitor-compatible callable with separately auditable overhead.

    collect() samples GPU once for all requested aliases. __call__(alias)
    samples again so the returned timestamp always reflects a fresh attempt.
    Errors produce unknown metrics; they never invalidate model health.
    """
    def __init__(self, slots_urls, *, gpu_selector='0', nvidia_smi='nvidia-smi',
                 timeout_s=2., runner=None, fetch_json=None, clock=time.time,
                 monotonic=time.perf_counter, evidence_path=None):
        if not isinstance(slots_urls, dict) or not slots_urls:
            raise ValueError('nonempty alias-to-slots URL mapping required')
        self.slots_urls = {str(alias): _local_url(url) for alias, url in slots_urls.items()}
        if any(not alias.strip() for alias in self.slots_urls):
            raise ValueError('backend alias must be nonempty')
        if not math.isfinite(float(timeout_s)) or timeout_s <= 0:
            raise ValueError('positive timeout_s required')
        self.gpu_selector, self.nvidia_smi, self.timeout_s = str(gpu_selector), nvidia_smi, float(timeout_s)
        self.runner, self.fetch_json = runner or subprocess.run, fetch_json or _fetch_json
        self.clock, self.monotonic = clock, monotonic
        self.evidence_path = Path(evidence_path) if evidence_path else None
        self.latest = None

    def collect(self, names=None):
        names = list(self.slots_urls) if names is None else list(names)
        if any(name not in self.slots_urls for name in names):
            raise ValueError('unknown resource alias')
        started, started_at = self.monotonic(), self.clock()
        errors, gpu, slots_raw, slots_counts, source_times = {}, None, {}, {}, {}
        raw_gpu = {'stdout': None, 'stderr': None, 'returncode': None}
        command = [self.nvidia_smi, '--query-gpu=index,uuid,name,memory.free,memory.total',
                   '--format=csv,noheader,nounits']
        gpu_started = self.monotonic()
        try:
            result = self.runner(command, capture_output=True, text=True, encoding='utf-8',
                                 errors='replace', timeout=self.timeout_s, check=False)
            raw_gpu = {'stdout': result.stdout, 'stderr': result.stderr, 'returncode': result.returncode}
            if result.returncode != 0:
                errors['nvidia_smi'] = f'exit_status_{result.returncode}'
            else:
                gpu = parse_gpu_csv(result.stdout, self.gpu_selector)
                if gpu is None:
                    errors['nvidia_smi'] = 'selected_gpu_or_memory_unavailable'
        except Exception as exc:
            errors['nvidia_smi'] = type(exc).__name__
        gpu_observed_at = self.clock()
        source_times['nvidia_smi_ms'] = max(0, (self.monotonic()-gpu_started)*1000)
        for name in names:
            one_started = self.monotonic()
            try:
                payload = self.fetch_json(self.slots_urls[name], self.timeout_s)
                slots_raw[name] = redact(payload)
                if not isinstance(payload, list) or not payload or any(
                    not isinstance(item, dict) or not isinstance(item.get('is_processing'), bool) for item in payload):
                    errors[name] = 'slots_processing_flags_unavailable'
                    slots_counts[name] = None
                else:
                    slots_counts[name] = sum(item['is_processing'] for item in payload)
            except Exception as exc:
                errors[name] = type(exc).__name__
                slots_counts[name] = None
                slots_raw[name] = None
            source_times[name+'_slots_ms'] = max(0, (self.monotonic()-one_started)*1000)
        observed_at = self.clock()
        # A resource tuple is no newer than its oldest successful source; this
        # avoids rejuvenating GPU memory after a slow or failed slots read.
        resource_stamp = min(gpu_observed_at, observed_at)
        resources = {name: {'queue_ms': None, 'free_mem_mb': None if gpu is None else gpu['free_mem_mb'],
            'inflight': slots_counts[name], 'source': SOURCE, 'observed_at': resource_stamp} for name in names}
        snapshot = {'schema': 'ai6g.local_resources.v1', 'source_kind': 'local_readonly_telemetry',
            'started_at': started_at, 'observed_at': observed_at,
            'collection_duration_ms': max(0, (self.monotonic()-started)*1000),
            'source_durations_ms': source_times, 'resources': resources, 'gpu': gpu,
            'gpu_memory_scope': 'physical GPU global free; shared by both services and other processes',
            'inflight_scope': 'count of llama slots whose is_processing is exactly true; not queued requests',
            'queue_scope': 'unmeasured and unknown; no queue latency inferred from busy slots',
            'errors': errors, 'raw': {'nvidia_smi': raw_gpu, 'llama_slots': slots_raw},
            'model_inference_requests': 0, 'telemetry_http_gets': len(names)}
        self.latest = copy.deepcopy(snapshot)
        if self.evidence_path:
            try:
                self.evidence_path.parent.mkdir(parents=True, exist_ok=True)
                with self.evidence_path.open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(snapshot, ensure_ascii=False, allow_nan=False)+'\n')
            except OSError as exc:
                snapshot['errors']['evidence_persistence'] = type(exc).__name__
                self.latest = copy.deepcopy(snapshot)
        return snapshot

    def __call__(self, name):
        return self.collect([name])['resources'][name]


def merge_measured_health(measured, resources):
    """Add resources without inventing or refreshing any model health value."""
    required = {'backend', 'reachable', 'latency_ms', 'quality_score', 'checked_at'}
    if not isinstance(measured, dict) or not required <= set(measured) or not isinstance(measured['reachable'], bool):
        raise ValueError('complete measured health fields required')
    try:
        obs = ResourceObservation(**resources)
        HealthObservation(measured['backend'], measured['reachable'], measured['latency_ms'],
                          measured['quality_score'], measured['checked_at'])
        if any(_number(measured[key]) is None for key in ('latency_ms', 'quality_score', 'checked_at')) or not 0 <= measured['quality_score'] <= 1:
            raise ValueError('invalid health number')
        if obs.observed_at is None:
            raise ValueError('missing resource timestamp')
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('invalid measured health or resource observation') from exc
    return {**copy.deepcopy(measured), 'resources': copy.deepcopy(resources)}


def publish_snapshot(health_url, snapshot, measured_health, *, timeout_s=2.):
    """Explicitly publish fresh resources alongside separately measured health."""
    health_url = _local_url(health_url)
    opener = _opener()
    started, results = time.perf_counter(), []
    for name, resources in snapshot['resources'].items():
        if name not in measured_health:
            results.append({'backend': name, 'published': False, 'error': 'measured_health_missing'})
            continue
        payload = merge_measured_health(measured_health[name], resources)
        if payload['backend'] != name:
            raise ValueError('measured health alias mismatch')
        request = urllib.request.Request(health_url, data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with opener.open(request, timeout=timeout_s) as response:
                response_body = response.read(65536).decode('utf-8', errors='replace')
                try:
                    acknowledged = json.loads(response_body)
                except ValueError:
                    acknowledged = None
                published = 200 <= response.status < 300 and isinstance(acknowledged, dict) and acknowledged.get('ok') is True
                results.append({'backend': name, 'published': published,
                    'http_status': response.status, 'response': redact(acknowledged) if acknowledged is not None else '[non-JSON acknowledgement omitted]',
                    'error': None if published else 'health_acknowledgement_missing_or_false'})
        except Exception as exc:
            results.append({'backend': name, 'published': False, 'error': type(exc).__name__})
    return {'publication_duration_ms': (time.perf_counter()-started)*1000, 'results': results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--slots', action='append', required=True, help='backend_alias=http://127.0.0.1:port/slots')
    parser.add_argument('--out', required=True, help='append-only telemetry evidence JSONL')
    parser.add_argument('--gpu', default='0', help='physical GPU index or UUID')
    parser.add_argument('--nvidia-smi', default='nvidia-smi')
    parser.add_argument('--timeout-s', type=float, default=2.)
    parser.add_argument('--samples', type=int, default=1)
    parser.add_argument('--interval-s', type=float, default=1.)
    parser.add_argument('--health-url', help='optional loopback runtime /v1/health')
    parser.add_argument('--health-input', help='JSON mapping alias to previously measured full health payload')
    args = parser.parse_args(argv)
    if args.samples < 1 or args.interval_s < 0 or not math.isfinite(args.interval_s):
        parser.error('positive sample count and finite nonnegative interval required')
    if bool(args.health_url) != bool(args.health_input):
        parser.error('--health-url and --health-input must be supplied together')
    try:
        urls = dict(value.split('=', 1) for value in args.slots)
        provider = LocalResourceProvider(urls, gpu_selector=args.gpu, nvidia_smi=args.nvidia_smi,
            timeout_s=args.timeout_s, evidence_path=args.out)
        measured = json.loads(Path(args.health_input).read_text(encoding='utf-8-sig')) if args.health_input else None
        for index in range(args.samples):
            snapshot = provider.collect()
            if measured is not None:
                publication = publish_snapshot(args.health_url, snapshot, measured, timeout_s=args.timeout_s)
                with Path(str(args.out)+'.publication.jsonl').open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(publication, ensure_ascii=False)+'\n')
            print(json.dumps({key: snapshot[key] for key in ('observed_at', 'collection_duration_ms', 'resources', 'errors')}, ensure_ascii=False))
            if index+1 < args.samples:
                time.sleep(args.interval_s)
    except (ValueError, OSError, TypeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
