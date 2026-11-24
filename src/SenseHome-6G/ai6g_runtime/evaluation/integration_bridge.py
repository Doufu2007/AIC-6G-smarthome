"""Real independent-process HTTP integration; software rules, no model claims.

Starts the product Runtime and dashboard Bridge in separate Python processes,
checks request IDs/state/policy outcomes, exercises concurrency 1/4/8, then
actually stops/restarts Runtime to verify explicit failure and state recovery.
All files are isolated inside --out. No model endpoints or provider keys are
inherited by child processes. This validates software delivery, not external
users, deployed devices, GPU capacity or 6G radio performance.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, ProxyHandler, Request

ROOT = Path(__file__).resolve().parents[3]


def _free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _http(url, payload=None, timeout=5):
    started = time.perf_counter()
    req = Request(url, data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                  headers={'Content-Type': 'application/json'})
    try:
        try:
            response = build_opener(ProxyHandler({})).open(req, timeout=timeout)
        except HTTPError as exc:
            response = exc
        with response:
            data = json.loads(response.read().decode('utf-8'))
            status = response.status
    except (OSError, ValueError, URLError) as exc:
        status, data = 0, {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
    return {'http_status': status, 'elapsed_ms': (time.perf_counter()-started)*1000, 'response': data}


def _wait_ready(process, url):
    end = time.perf_counter()+10
    while time.perf_counter() < end:
        if process.poll() is not None:
            raise RuntimeError(f'child process exited early ({process.returncode})')
        if _http(url, timeout=.3)['http_status'] == 200:
            return
        time.sleep(.05)
    raise RuntimeError(f'child readiness timeout: {url}')


def _stop(process):
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _percentile(values, quantile):
    values = sorted(values)
    pos = (len(values)-1)*quantile
    low = int(pos)
    return values[low]+(values[min(low+1, len(values)-1)]-values[low])*(pos-low)


def run(out, *, concurrency=(1, 4, 8), requests_per_level=24):
    out = Path(out).resolve()
    if requests_per_level < 1 or not concurrency or any(n < 1 for n in concurrency):
        raise ValueError('positive load settings required')
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError('integration output must be an empty directory')
    runtime_port, bridge_port = _free_port(), _free_port()
    runtime_base, bridge_base = f'http://127.0.0.1:{runtime_port}', f'http://127.0.0.1:{bridge_port}'
    runtime_script = ROOT/'SenseHome-6G/ai6g_runtime/serve.py'
    bridge_script = ROOT/'openvela_smart_home/dashboard/bridge_server.py'
    env = {k: v for k, v in os.environ.items() if not k.startswith(('AI6G_', 'SENSEHOME_AI6G_'))}
    env.update(PYTHONUTF8='1', PYTHONUNBUFFERED='1')
    processes, handles, children, records = [], [], [], []
    runtime, bridge = None, None
    checks, load = {}, []

    def start(script, args, child_env, label):
        stdout, stderr = (out/(label+'.stdout.log')).open('wb'), (out/(label+'.stderr.log')).open('wb')
        handles.extend((stdout, stderr))
        command = [sys.executable, str(script), *map(str, args)]
        child = subprocess.Popen(command, cwd=ROOT, env=child_env, stdout=stdout, stderr=stderr,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        children.append(child)
        processes.append({'label': label, 'pid': child.pid, 'command': command,
                          'source_sha256': hashlib.sha256(script.read_bytes()).hexdigest()})
        return child

    def start_runtime(label):
        child = start(runtime_script, ['--host', '127.0.0.1', '--port', runtime_port,
            '--log', out/'runtime_requests.jsonl', '--state', out/'runtime_state.json'], env, label)
        _wait_ready(child, runtime_base+'/v1/health')
        return child

    def request(phase, path, payload=None):
        record = {'phase': phase, 'path': path, 'payload': payload, 'wall_time': time.time(),
                  **_http(bridge_base+path, payload)}
        records.append(record)
        return record

    def command(phase, text, identity):
        return request(phase, '/api/command', {'command': text, 'request_id': identity})

    failure = None
    try:
        runtime = start_runtime('runtime_initial')
        bridge_env = {**env, 'AI6G_RUNTIME_URL': runtime_base+'/v1/tasks', 'AI6G_RUNTIME_TIMEOUT_S': '2'}
        bridge = start(bridge_script, ['--host', '127.0.0.1', '--port', bridge_port,
            '--patrol-interval', 1, '--state', out/'legacy_unused.json'], bridge_env, 'bridge')
        _wait_ready(bridge, bridge_base+'/api/meta')
        initial = request('initial', '/api/state')['response']
        first = command('action', '打开客厅的灯', 'bridge-action-1')['response']
        state = request('action', '/api/state')['response']
        checks['request_id_preserved'] = first.get('request_id') == 'bridge-action-1'
        checks['state_from_runtime'] = state.get('_meta', {}).get('state_source') == 'ai6g_runtime' and state['lights']['客厅']['is_on'] is True
        checks['no_random_sensor_data'] = 'sensors' not in state and state['_meta']['sensor_source'] == 'not_connected'
        duplicate = command('idempotent', '打开客厅的灯', 'bridge-action-1')['response']
        checks['duplicate_is_replay'] = bool((duplicate.get('runtime') or {}).get('execution', {}).get('idempotent_replay'))
        refusal = command('refusal', '燃气泄漏时打开客厅灯', 'bridge-refusal-1')['response']
        clarification = command('clarification', '不要打开客厅的灯', 'bridge-clarify-1')['response']
        checks['refusal_real_outcome'] = refusal.get('result_kind') == 'refusal' and not refusal.get('actions')
        checks['clarification_real_outcome'] = clarification.get('result_kind') == 'clarification' and not clarification.get('actions')
        no_route = command('route_failure', '客厅有点冷，请给建议', 'bridge-no-route-1')
        checks['failure_code_preserved'] = no_route['http_status'] == 422 and no_route['response'].get('error_code') == 'route_unavailable'
        disabled = [request('disabled', path, body) for path, body in
            [('/api/light', {'location': '客厅'}), ('/api/patrol/toggle', {}), ('/api/patrol/trigger', None)]]
        checks['legacy_writes_disabled'] = all(row['http_status'] == 409 for row in disabled)
        checks['patrol_disabled'] = request('disabled', '/api/patrol/log')['response'].get('disabled') is True
        for workers in concurrency:
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(command, f'load-{workers}', '打开客厅的灯', f'bridge-load-{workers}-{i}') for i in range(requests_per_level)]
                batch = [future.result() for future in futures]
            elapsed = time.perf_counter()-started
            latencies = [row['elapsed_ms'] for row in batch]
            load.append({'concurrency': workers, 'requests': len(batch),
                'successes': sum(row['response'].get('ok') is True for row in batch),
                'p50_ms': statistics.median(latencies), 'p95_ms': _percentile(latencies, .95),
                'wall_s': elapsed, 'throughput_per_s': len(batch)/elapsed,
                'scope': 'local rule/software state service over independent-process HTTP; no GPU or radio claim'})
        checks['concurrency_1_4_8'] = all(row['successes'] == requests_per_level for row in load)
        state_before_stop = request('before_disconnect', '/api/state')['response']
        _stop(runtime)
        disconnected = command('disconnect', '关闭客厅的灯', 'bridge-disconnected-1')
        checks['disconnect_fails_explicitly'] = disconnected['http_status'] == 503 and disconnected['response'].get('error_code') == 'runtime_unavailable' and not disconnected['response'].get('actions')
        runtime = start_runtime('runtime_recovered')
        recovered = request('recovery', '/api/state')['response']
        checks['persisted_state_after_restart'] = recovered.get('lights') == state_before_stop.get('lights')
        after = command('recovery', '关闭客厅的灯', 'bridge-recovered-1')['response']
        checks['recovered_action_executes'] = after.get('ok') is True and after.get('runtime', {}).get('state', {}).get('lights', {}).get('客厅', {}).get('is_on') is False
        checks['legacy_file_never_written'] = not (out/'legacy_unused.json').exists()
    except Exception as exc:
        failure = f'{type(exc).__name__}: {exc}'
        checks['runner_completed'] = False
    finally:
        for child in reversed(children):
            _stop(child)
        for handle in handles:
            handle.close()
    runtime_rows = [json.loads(line) for line in (out/'runtime_requests.jsonl').read_text(encoding='utf-8').splitlines()] if (out/'runtime_requests.jsonl').exists() else []
    ids = Counter(row.get('request_id') for row in runtime_rows)
    expected = {'bridge-action-1', 'bridge-refusal-1', 'bridge-clarify-1', 'bridge-no-route-1', 'bridge-recovered-1'}
    expected.update(f'bridge-load-{workers}-{i}' for workers in concurrency for i in range(requests_per_level))
    checks['runtime_ids_reconcile'] = set(ids) == expected and all(count == 1 for count in ids.values())
    checks['all_model_real_false'] = all(not row.get('model_real') for row in runtime_rows)
    report = {'schema': 'ai6g.bridge_process_integration.v1', 'passed': all(checks.values()),
              'source_kind': 'real_http_processes_rules_only', 'checks': checks, 'load': load,
              'processes': processes, 'records': records, 'runtime_rows': len(runtime_rows), 'failure': failure,
              'limits': ['software devices only', 'no LLM model result', 'no physical sensors or 6G radio',
                         'restart tests state persistence, not cross-restart idempotency',
                         'load metrics are local engineering observations; not capacity claims']}
    (out/'bridge_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (out/'bridge_http.jsonl').write_text(''.join(json.dumps(row, ensure_ascii=False)+'\n' for row in records), encoding='utf-8')
    lines = ['# 独立进程桥接验收', '', f'通过：{report["passed"]}；来源：真实HTTP进程、规则后端。', '',
             *[f'- {name}: {passed}' for name, passed in checks.items()], '',
             '不代表真实模型、硬件传感器、用户试点或6G空口实验。', '',
             '| 并发 | 请求 | 成功 | P50 ms | P95 ms | 请求/秒 |', '|---|---:|---:|---:|---:|---:|']
    lines += [f'| {row["concurrency"]} | {row["requests"]} | {row["successes"]} | {row["p50_ms"]:.3f} | {row["p95_ms"]:.3f} | {row["throughput_per_s"]:.2f} |' for row in load]
    (out/'bridge_report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    hashes = {str(path.relative_to(out)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in out.iterdir() if path.is_file()}
    (out/'SHA256.json').write_text(json.dumps(hashes, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--concurrency', default='1,4,8')
    parser.add_argument('--requests-per-level', type=int, default=24)
    args = parser.parse_args(argv)
    report = run(args.out, concurrency=tuple(int(x) for x in args.concurrency.split(',')), requests_per_level=args.requests_per_level)
    print(json.dumps({'passed': report['passed'], 'checks': report['checks'], 'failure': report['failure']}, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
