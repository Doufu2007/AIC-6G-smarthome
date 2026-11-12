#!/usr/bin/env python3
"""Controlled HTTP failures and real proxy admission waits before real models.

Existing model processes and all production code remain unchanged. The proxy's
wait is imposed and measured at dispatch, not inferred to be an upstream GPU
queue. Devices remain isolated JSON simulation; localhost is not WAN or 6G.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import http.server
import ipaddress
import json
from pathlib import Path
import shutil
import statistics
import threading
import time
from urllib.parse import urlparse
import uuid

import fused_eval as frozen

ROOT = frozen.ROOT
CASES = ROOT / 'SenseHome-6G/net_offload/backends/eval_commands.reviewed.jsonl'
CALIBRATION = ROOT / 'docs/experiments/20261111_fused_real/reviewed_20_sealed_repeat3/calibration.json'
CASE_IDS = ('C01', 'C04')
CONDITIONS = (
    {'id': 'healthy', 'local_mode': 'healthy', 'cloud_mode': 'healthy', 'local_delay_ms': 0, 'cloud_delay_ms': 0},
    {'id': 'cloud_http503', 'local_mode': 'healthy', 'cloud_mode': 'http_503', 'local_delay_ms': 0, 'cloud_delay_ms': 0},
    {'id': 'local_http503', 'local_mode': 'http_503', 'cloud_mode': 'healthy', 'local_delay_ms': 0, 'cloud_delay_ms': 0},
    {'id': 'cloud_admission700', 'local_mode': 'healthy', 'cloud_mode': 'healthy', 'local_delay_ms': 0, 'cloud_delay_ms': 700},
    {'id': 'local_admission700', 'local_mode': 'healthy', 'cloud_mode': 'healthy', 'local_delay_ms': 700, 'cloud_delay_ms': 0},
)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ControlledProxy:
    """Loopback-only reverse proxy with transparent request/response recording."""
    def __init__(self, upstream, *, mode='healthy', delay_ms=0):
        parsed = urlparse(upstream)
        try:
            is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            is_loopback = parsed.hostname == 'localhost'
        if parsed.scheme != 'http' or not is_loopback or parsed.username or parsed.password:
            raise ValueError('ONLY_LOOPBACK_HTTP_UPSTREAM_ALLOWED')
        if mode not in ('healthy', 'http_503') or not 0 <= delay_ms <= 1000:
            raise ValueError('INVALID_CONTROLLED_PROXY_CONDITION')
        self.upstream, self.mode, self.delay_ms = upstream, mode, delay_ms
        self.records = []
        self.lock = threading.Lock()
        self.last_wait_ms, self.last_sample_at, self.inflight = None, None, 0
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, status, body):
                raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self.dispatch('GET')

            def do_POST(self):
                self.dispatch('POST')

            def dispatch(self, method):
                started = time.perf_counter()
                trace = uuid.uuid4().hex
                path = self.path.split('?', 1)[0]
                record = {'trace_id': trace, 'started_at': frozen.now(), 'method': method,
                          'path': path, 'condition_mode': owner.mode,
                          'configured_admission_wait_ms': owner.delay_ms,
                          'upstream_forwarded': False, 'upstream_status': None}
                if method == 'GET' and path == '/health':
                    with owner.lock:
                        body = {'ready': owner.mode != 'http_503',
                                'last_admission_wait_ms': owner.last_wait_ms,
                                'admission_sample_age_ms': None if owner.last_sample_at is None
                                    else round((time.perf_counter() - owner.last_sample_at) * 1000, 3),
                                'inflight': owner.inflight,
                                'source': 'controlled_proxy_not_gpu_queue',
                                'queue_semantics': 'last measured fixed proxy dispatch wait; held under constant experimental condition'}
                    record.update(http_status=200, response=body)
                    with owner.lock:
                        owner.records.append(record)
                    self.send(200, body)
                    return
                if path not in ('/v1/models', '/v1/chat/completions') or method not in ('GET', 'POST'):
                    self.send(404, {'error': 'PROXY_PATH_NOT_ALLOWED'})
                    return
                payload = None
                if method == 'POST':
                    size = int(self.headers.get('Content-Length', '0'))
                    if size <= 0 or size > frozen.MAX_RESPONSE_BYTES:
                        self.send(400, {'error': 'INVALID_REQUEST_LENGTH'})
                        return
                    payload = json.loads(self.rfile.read(size))
                    record['upstream_request'] = payload
                if method == 'POST' and owner.mode == 'http_503':
                    status, body = 503, {'error': 'CONTROLLED_PROXY_HTTP_503'}
                    record['actual_admission_wait_ms'] = 0.0
                else:
                    with owner.lock:
                        owner.inflight += int(method == 'POST')
                    queued_at = time.perf_counter()
                    if method == 'POST':
                        # This delay is real time spent inside our proxy, not a fake health value.
                        threading.Event().wait(owner.delay_ms / 1000)
                        measured_wait = round((time.perf_counter() - queued_at) * 1000, 3)
                        with owner.lock:
                            owner.last_wait_ms = measured_wait
                            owner.last_sample_at = time.perf_counter()
                        record['actual_admission_wait_ms'] = measured_wait
                    endpoint = path.removeprefix('/v1/')
                    upstream = frozen.exchange(owner.upstream, method, endpoint, payload, timeout_s=90)
                    status = upstream['http_status'] or 502
                    body = deepcopy(upstream['response'])
                    record.update(upstream_forwarded=True, upstream_status=upstream['http_status'],
                                  upstream_exchange=upstream)
                    if not body:
                        body = {'error': upstream['error'] or 'EMPTY_PROXY_RESPONSE'}
                    with owner.lock:
                        owner.inflight -= int(method == 'POST')
                body['_sensehome_proxy_trace'] = {'trace_id': trace,
                    'upstream_forwarded': record['upstream_forwarded'],
                    'proxy_admission_wait_ms': record.get('actual_admission_wait_ms'),
                    'source': 'controlled_local_HTTP_proxy'}
                record.update(http_status=status, response=deepcopy(body),
                              wall_ms=round((time.perf_counter() - started) * 1000, 3))
                with owner.lock:
                    owner.records.append(record)
                self.send(status, body)

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.base_url = f'http://127.0.0.1:{self.server.server_port}'
        self.url = self.base_url + '/v1'

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


class ProxyObservedBackend(frozen.DirectModelBackend):
    def __init__(self, proxy, model, tier):
        super().__init__(proxy.url, model, tier, timeout_s=90, max_tokens=256)
        self.proxy = proxy
        self.metadata.update(proxy_controlled=True,
            admission_measurement='actual proxy dispatch wait; not upstream GPU queue')

    def health(self):
        health = super().health()
        extra = frozen.exchange(self.proxy.base_url, 'GET', 'health', None, 5)
        telemetry = extra['response']
        health['exchanges'].append(extra)
        health['probe_http_calls'] += 1
        health.update(ready=bool(health.get('ready') and telemetry.get('ready')),
            queue_ms=telemetry.get('last_admission_wait_ms'),
            queue_scope='measured_proxy_dispatch_wait_last_sample_not_gpu_queue',
            queue_sample_age_ms=telemetry.get('admission_sample_age_ms'),
            inflight=telemetry.get('inflight'), proxy_telemetry=telemetry,
            telemetry_source='real_proxy_HTTP_health_no_observation_override')
        return health


def selected_cases():
    dataset = frozen.read_dataset(CASES)
    cases = [case for case in dataset['cases'] if case['id'] in CASE_IDS]
    if {case['id'] for case in cases} != set(CASE_IDS):
        raise ValueError('REGISTERED_CASE_ID_MISSING')
    return cases, {**{key: value for key, value in dataset.items() if key != 'cases'},
                   'selected_case_ids': list(CASE_IDS),
                   'use_scope': 'existing_human_frozen_regression_fault_check_not_new_generalization'}


def source_paths():
    old = frozen.load_json(ROOT / 'docs/experiments/20261111_fused_real/reviewed_20_sealed_repeat3/report.json')
    return [ROOT / relative for relative in old['source_sha256']] + [Path(__file__)]


def register(out, endpoints):
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'registration.json').exists():
        raise FileExistsError('REGISTRATION_ALREADY_EXISTS')
    cases, meta = selected_cases()
    frozen.write_json(out / 'cases.human_subset.json', {'cases': cases, 'metadata': meta})
    calibration = frozen.load_json(CALIBRATION)
    config = frozen.load_json(frozen.NET / 'experiments/policy_config.json')
    for tier, measured in calibration['inference_cost_ms'].items():
        config[tier + '_infer_ms'] = measured
    sources = {path.relative_to(ROOT).as_posix(): digest(path) for path in source_paths()}
    registration = {'schema_version': 'sensehome.real-http-fault-registration.v1',
        'registered_at': frozen.now(), 'planned_rows': 120, 'case_ids': list(CASE_IDS),
        'repeat': 3, 'arms': list(frozen.ARMS), 'conditions': list(CONDITIONS),
        'condition_order': 'fixed as listed; each condition has fresh independent C policy processes; no cross-condition recovery state',
        'within_condition': 'same input and initial state; same measured health per paired block; arm order rotates by case and repeat; three repeats continuous within each arm',
        'state_reset': 'device state reset for each arm; strategy history continuous within condition and recreated between conditions',
        'recovery_control': 'no network readiness overrides; model503 leaves inventory network reachable but proxy ready false; 3-sample C recovery threshold unchanged',
        'warmup': 'one fixed non-evaluation request per endpoint per condition; measured and recorded separately, including controlled failure',
        'warmup_command': '读取室外温度，然后把浴室窗帘位置设为22%',
        'source_sha256': sources, 'dataset_sha256': digest(CASES),
        'selected_cases_sha256': digest(out / 'cases.human_subset.json'),
        'calibration_sha256': digest(CALIBRATION), 'calibration_cost_ms': calibration['inference_cost_ms'],
        'policy_config': config, 'original_endpoints': endpoints,
        'queue_measurement': '700ms fixed proxy dispatch wait physically enforced; last observed wait held while condition constant, age saved; not GPU queue, load, or WAN congestion',
        'failure_measurement': 'actual proxy POST HTTP503 without forwarding; underlying model process stays healthy; not service crash or real network outage',
        'post_run_rule': 'single registered run; no delay amplification, configuration/prompt/model/guard changes or selective omission after outcomes',
        'primary_metrics': ['nonempty simulated task completed without fallback/error', 'proxy HTTP status',
                            'actual forwarded upstream model POST status', 'all-request gateway wall time', 'backend selection'],
        'claims_limit': 'conditional local controlled-fault behavior; no independent cloud, physical actuator, real-user, WAN or 6G claims'}
    frozen.write_json(out / 'registration.json', registration)
    snapshot = out / 'source_snapshot'
    snapshot.mkdir()
    for path in source_paths():
        shutil.copy2(path, snapshot / path.name)
    shutil.copy2(CALIBRATION, out / 'calibration.json')
    return registration


def annotate_proxy_calls(report, proxies):
    records = {row['trace_id']: row for proxy in proxies.values() for row in proxy.records}
    for row in report['rows']:
        traces = [exchange.get('response', {}).get('_sensehome_proxy_trace', {}).get('trace_id')
                  for attempt in row.get('backend_attempts', [])
                  for exchange in attempt.get('upstream_exchanges', []) if exchange['method'] == 'POST']
        used = [records[trace] for trace in traces if trace in records]
        if len(used) != len(traces):
            raise ValueError('PROXY_TRACE_UNMATCHED')
        row.update(proxy_trace_ids=traces, proxy_post_attempts=len(used),
            actual_upstream_model_post_attempts=sum(bool(value['upstream_forwarded']) for value in used),
            actual_upstream_model_post_http200=sum(value['upstream_forwarded'] and value['upstream_status'] == 200 for value in used),
            model_calls_scope='legacy field counts client-to-proxy POST attempts; actual upstream calls counted separately')
    return report


def summarize_condition(report):
    arms = {}
    for arm in frozen.ARMS:
        rows = [row for row in report['rows'] if row['arm'] == arm]
        times = sorted(row['gateway_wall_ms'] for row in rows)
        arms[arm] = {'n_rows': len(rows), 'n_completed': sum(bool(row.get('task_completed')) for row in rows),
            'n_protection_refusal': sum(bool(row.get('protection_refusal')) for row in rows),
            'n_fallback': sum(bool(row.get('fallback')) for row in rows),
            'n_interface_ok': sum(bool(row.get('interface_ok')) for row in rows),
            'proxy_post_attempts': sum(row['proxy_post_attempts'] for row in rows),
            'actual_upstream_model_post_attempts': sum(row['actual_upstream_model_post_attempts'] for row in rows),
            'actual_upstream_model_post_http200': sum(row['actual_upstream_model_post_http200'] for row in rows),
            'gateway_median_ms_all_requests': statistics.median(times),
            'gateway_max_ms_all_requests': max(times),
            'selected_backend_counts': {backend: sum(row['gateway_event']['selected_backend'] == backend for row in rows)
                                        for backend in ('local-model', 'cloud-model', 'local-rules')}}
    return {'n_rows': len(report['rows']), 'status': report['status'], 'by_arm': arms,
            'statistics': 'descriptive; 2 commands repeated on one shared host, no independent-user significance inference'}


def run_registered(out, registration):
    if any((out / 'conditions').glob('*/rows.jsonl')):
        raise FileExistsError('REGISTERED_MATRIX_ALREADY_HAS_ROWS_NO_RERUN')
    for relative, expected in registration['source_sha256'].items():
        if digest(ROOT / relative) != expected:
            raise ValueError('REGISTERED_SOURCE_CHANGED')
    if digest(CALIBRATION) != registration['calibration_sha256'] or digest(CASES) != registration['dataset_sha256']:
        raise ValueError('REGISTERED_INPUT_CHANGED')
    probes = {tier: frozen.exchange(config['url'], 'GET', 'models', None, 5)
              for tier, config in registration['original_endpoints'].items()}
    frozen.write_json(out / 'upstream_probe.json', {'checked_at': frozen.now(), 'probes': probes})
    if not all(probe['http_status'] == 200 for probe in probes.values()):
        frozen.write_json(out / 'execution_status.json', {'status': 'blocked', 'reason': 'REAL_UPSTREAM_UNAVAILABLE',
            'actual_model_post_attempts': 0, 'checked_at': frozen.now()})
        return 1
    cases, meta = selected_cases()
    results = {}
    for condition in registration['conditions']:
        directory = out / 'conditions' / condition['id']
        directory.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            proxies = {tier: stack.enter_context(ControlledProxy(endpoint['url'], mode=condition[tier + '_mode'],
                            delay_ms=condition[tier + '_delay_ms']))
                       for tier, endpoint in registration['original_endpoints'].items()}
            backends = {tier: ProxyObservedBackend(proxies[tier], endpoint['model'], tier)
                        for tier, endpoint in registration['original_endpoints'].items()}
            warmups = []
            for tier, backend in backends.items():
                first = len(backend.attempts)
                backend.infer(registration['warmup_command'], {})
                warmups.append({'tier': tier, 'not_evaluation': True, 'attempts': deepcopy(backend.attempts[first:])})
            frozen.write_json(directory / 'warmups.json', warmups)
            report = frozen.evaluate(cases, backends, frozen.NET / 'policy_cli.exe', directory,
                repeat=registration['repeat'], policy_config=registration['policy_config'], dataset_meta=meta,
                calibration=frozen.load_json(out / 'calibration.json'),
                conditions=[{'id': condition['id'], 'override': {}}])
            # New files only: preserve the inherited report separately, then add transparent proxy counts.
            shutil.copy2(directory / 'report.json', directory / 'report.gateway_raw.json')
            report = annotate_proxy_calls(report, proxies)
            report['controlled_proxy_scope'] = registration['claims_limit']
            report['proxy_counts_warning'] = 'legacy model_calls includes proxy503 attempts; actual_upstream_model_post_attempts is explicit per row'
            frozen.write_json(directory / 'report.json', report)
            (directory / 'rows.jsonl').write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n'
                                                       for row in report['rows']), encoding='utf-8')
            frozen.write_json(directory / 'proxy_records.json', {tier: proxy.records for tier, proxy in proxies.items()})
            results[condition['id']] = summarize_condition(report)
            frozen.write_json(directory / 'condition_summary.json', results[condition['id']])
            if report['status'] != 'finished' or report['summary']['n_runner_failed']:
                break
    unchanged = all(digest(ROOT / relative) == expected for relative, expected in registration['source_sha256'].items())
    total = sum(value['n_rows'] for value in results.values())
    result = {'registered_at': registration['registered_at'], 'finished_at': frozen.now(),
        'status': 'finished' if total == registration['planned_rows'] and unchanged else 'incomplete',
        'n_rows': total, 'source_unchanged': unchanged, 'conditions': results,
        'scope': registration['claims_limit'],
        'registration_sha256': digest(out / 'registration.json'),
        'primary_aggregation': 'per-condition only; do not pool equal-weight artificial conditions into real-world reliability'}
    frozen.write_json(out / 'summary.json', result)
    frozen.write_json(out / 'execution_status.json', {'status': result['status'], 'n_rows': total, 'source_unchanged': unchanged})
    lines = ['# 真实模型与受控HTTP代理验证', '',
        '各端点仍为同机模型；HTTP503及700ms准入等待发生在自建代理中，均为软件实验条件，不是GPU队列、真实WAN故障或6G测量。设备为隔离JSON模拟。', '',
        '|条件|策略|非空模拟完成|代理POST尝试|实际转发模型POST|全请求中位ms|', '|---|---|---:|---:|---:|---:|']
    for condition, values in results.items():
        for arm, values in values['by_arm'].items():
            lines.append(f"|{condition}|{arm}|{values['n_completed']}/{values['n_rows']}|{values['proxy_post_attempts']}|{values['actual_upstream_model_post_attempts']}|{values['gateway_median_ms_all_requests']:.3f}|")
    lines += ['', '每条件独立重建C策略状态，条件内重复连续。计分直接复用冻结运行器，失败/回退/保护拒绝与完成分开；主矩阵与每条件两次预热分开。2条已有人工命令的重复是受控回归，不是新用户泛化。不同条件不混算“真实世界可靠性”。', '',
              '完整来源、注册、代理HTTP原文及逐条评分见registration.json、各conditions目录；旧600行及生产源码未修改。', '']
    (out / 'summary.md').write_text('\n'.join(lines), encoding='utf-8')
    return 0 if result['status'] == 'finished' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--local-url', default='http://127.0.0.1:62352/v1')
    parser.add_argument('--cloud-url', default='http://127.0.0.1:42354/v1')
    parser.add_argument('--local-model', default='Qwen2.5-1.5B-Instruct-Q4_K_M')
    parser.add_argument('--cloud-model', default='Qwen3-4B-Q4_K_M')
    parser.add_argument('--register-only', action='store_true')
    parser.add_argument('--run-registered', action='store_true')
    args = parser.parse_args()
    if args.run_registered:
        registration = frozen.load_json(args.out / 'registration.json')
    else:
        registration = register(args.out, {'local': {'url': args.local_url, 'model': args.local_model},
                                          'cloud': {'url': args.cloud_url, 'model': args.cloud_model}})
    if args.register_only:
        print(json.dumps({'registered': True, 'planned_rows': registration['planned_rows']}))
        return 0
    return run_registered(args.out, registration)


if __name__ == '__main__':
    raise SystemExit(main())
