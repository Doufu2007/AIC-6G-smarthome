"""Real HTTP/process reliability checks with a clearly labeled model fixture.

This measures software integration, never language-model quality or 6G radio.
Only child processes created by this script are terminated.
"""
from __future__ import annotations
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PACKAGE = Path(__file__).resolve().parents[1]
TEXT = '打开客厅灯，同时拉开客厅窗帘'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def http(url, payload=None, timeout=8):
    started = time.perf_counter()
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf8')
    request = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        with OPENER.open(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    return {'http_status': status, 'elapsed_ms': (time.perf_counter()-started)*1000,
            'body': json.loads(raw)}


class Fixture(BaseHTTPRequestHandler):
    def do_POST(self):
        size = int(self.headers.get('Content-Length', 0))
        request = json.loads(self.rfile.read(size))
        with self.server.control:
            mode = self.server.mode
            self.server.calls.append({'time': time.time(), 'mode': mode, 'request': request,
                                      'source_kind': 'protocol_fixture', 'llm_evidence': False})
        if mode == 'disconnect':
            self.connection.close()
            return
        if mode in ('delay', 'timeout'):
            time.sleep(.2 if mode == 'delay' else 3.0)
        payload = {'result_kind': 'action', 'actions': [
            {'type': 'light_on', 'location': '客厅', 'brightness': 100},
            {'type': 'curtain_open', 'location': '客厅'}], 'confidence': .99}
        if mode == 'invalid_action':
            payload['actions'][0]['location'] = '不存在'
        content = '{bad json' if mode == 'bad_json' else json.dumps(payload, ensure_ascii=False)
        body = json.dumps({'model': 'contract-fixture', 'source_kind': 'protocol_fixture',
            'choices': [{'message': {'content': content}}],
            'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}).encode('utf8')
        try:
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, *_):
        pass


def quantile(values, q):
    values = sorted(values)
    return values[min(len(values)-1, round((len(values)-1)*q))]


def run(out, concurrency=(1, 4, 8), requests_per_cell=24):
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    fixture = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    fixture.mode, fixture.control, fixture.calls = 'normal', threading.Lock(), []
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    runtime_port = port()
    base = f'http://127.0.0.1:{runtime_port}'
    log, state = out/'requests.jsonl', out/'state.json'
    child, output_handle = None, None
    rows, checks, cells = [], [], []
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith('AI6G_'):
            environment.pop(key)
    environment.update(AI6G_CLOUD_URL=f'http://127.0.0.1:{fixture.server_port}/v1/chat/completions',
                       AI6G_CLOUD_MODEL='contract-fixture', AI6G_OBSERVATION_TTL_S='30')

    def start():
        nonlocal child, output_handle
        output_handle = (out/'runtime-process.log').open('ab')
        child = subprocess.Popen([sys.executable, '-u', str(PACKAGE/'serve.py'), '--port', str(runtime_port),
            '--log', str(log), '--state', str(state)], env=environment, stdout=output_handle,
            stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        until = time.monotonic()+12
        while time.monotonic() < until:
            if child.poll() is not None:
                raise RuntimeError('runtime child exited; see runtime-process.log')
            try:
                http(base+'/v1/health', timeout=.5)
                return
            except OSError:
                time.sleep(.05)
        raise TimeoutError('runtime startup timed out')

    def stop():
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(5)
        if output_handle:
            output_handle.close()

    def health(age=0):
        # Artificial quality belongs to this fixture test, never model calibration.
        http(base+'/v1/health', {'backend': 'cloud-model', 'reachable': True, 'latency_ms': 1,
             'quality_score': .99, 'checked_at': time.time()-age, 'detail': 'protocol_fixture_only'})

    def call(identity, text=TEXT):
        result = http(base+'/v1/tasks', {'text': text, 'request_id': identity})
        result.update(request_id=identity, text=text, source_kind='process_protocol_fixture', llm_evidence=False)
        return result

    def check(name, ok, **details):
        checks.append({'check': name, 'passed': bool(ok), **details})

    try:
        start()
        for mode in ('normal', 'delay'):
            fixture.mode = mode
            for workers in concurrency:
                health()
                started = time.perf_counter()
                with concurrent.futures.ThreadPoolExecutor(workers) as pool:
                    batch = list(pool.map(lambda i: call(f'{mode}-{workers}-{i}'), range(requests_per_cell)))
                elapsed = time.perf_counter()-started
                rows.extend(batch)
                cells.append({'mode': mode, 'concurrency': workers, 'n': len(batch),
                    'ok': sum(r['body']['ok'] for r in batch), 'elapsed_s': elapsed,
                    'throughput_per_s': len(batch)/elapsed,
                    'p50_ms': quantile([r['elapsed_ms'] for r in batch], .5),
                    'p95_ms': quantile([r['elapsed_ms'] for r in batch], .95)})
                check(f'{mode}_concurrency_{workers}', all(r['body']['ok'] for r in batch))
        fixture.mode = 'delay'
        health()
        count_before = len(fixture.calls)
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            duplicates = list(pool.map(lambda _: call('duplicate-flight'), range(8)))
        rows.extend(duplicates)
        check('concurrent_id_single_inference', len(fixture.calls)-count_before == 1 and all(r['body']['ok'] for r in duplicates),
              observed_model_requests=len(fixture.calls)-count_before)
        conflict = call('duplicate-flight', '关闭客厅的灯')
        rows.append(conflict)
        check('different_text_same_id_conflict', conflict['http_status'] == 409)
        for mode in ('bad_json', 'invalid_action', 'disconnect'):
            fixture.mode = mode
            health()
            before = http(base+'/v1/state')['body']
            result = call('fault-'+mode)
            rows.append(result)
            after = http(base+'/v1/state')['body']
            check(mode+'_failure_no_commit', not result['body']['ok'] and before == after,
                  error_code=result['body'].get('error_code'))
        health(age=60)
        before_calls = len(fixture.calls)
        stale = call('stale')
        rows.append(stale)
        check('stale_observation_no_model_call', not stale['body']['ok'] and len(fixture.calls) == before_calls)
        fixture.mode = 'normal'
        health()
        before_calls = len(fixture.calls)
        privacy = call('privacy', '禁止联网，'+TEXT)
        rows.append(privacy)
        check('strict_privacy_never_dispatched', not privacy['body']['ok'] and len(fixture.calls) == before_calls)
        for identity, text, kind in [('refusal', '火灾发生时请锁上客厅门', 'refusal'),
                                     ('clarification', '仅模拟打开客厅的灯，不执行动作', 'clarification')]:
            before = http(base+'/v1/state')['body']
            response = call(identity, text)
            rows.append(response)
            check(kind+'_explicit_no_commit', response['body']['ok'] and response['body']['result_kind'] == kind
                  and http(base+'/v1/state')['body'] == before)
        fixture.mode = 'timeout'
        health()
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            late_future = pool.submit(call, 'late')
            time.sleep(.1)
            newest = call('newer', '关闭客厅的灯')
            late = late_future.result()
        rows.extend([newest, late])
        time.sleep(.65)  # allow the abandoned fixture response to arrive
        check('late_response_does_not_overwrite_new_state', not late['body']['ok'] and
              not http(base+'/v1/state')['body']['lights']['客厅']['is_on'], error_code=late['body'].get('error_code'))
        fixture.mode = 'normal'
        health()
        recovery_started = time.perf_counter()
        recovery = call('recovery')
        rows.append(recovery)
        check('recovery_succeeds', recovery['body']['ok'], recovery_request_ms=(time.perf_counter()-recovery_started)*1000)
        newest = call('before-restart', '关闭客厅的灯')
        rows.append(newest)
        before_restart = http(base+'/v1/state')['body']
        stop()
        restart_started = time.perf_counter()
        start()
        count_before = len(fixture.calls)
        replay = call('recovery')
        rows.append(replay)
        check('restart_restores_state_and_receipt', replay['body']['ok'] and replay['body'].get('result_origin') == 'durable_receipt'
              and http(base+'/v1/state')['body'] == before_restart, restart_readiness_ms=(time.perf_counter()-restart_started)*1000)
        logged = http(base+'/v1/logs')['body']
        ids = [r['request_id'] for r in logged]
        check('jsonl_valid_unique_canonical_requests', len(ids) == len(set(ids)))
        for response in rows:
            body = response['body']
            if body.get('ok') and not body.get('replay'):
                check('log_receipt_'+response['request_id'], response['request_id'] in ids)
        (out/'state-final.json').write_text(json.dumps(http(base+'/v1/state')['body'], ensure_ascii=False, indent=2), encoding='utf8')
    finally:
        stop()
        fixture.shutdown()
        fixture.server_close()
        (out/'client.rows.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in rows), encoding='utf8')
        (out/'fixture.rows.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in fixture.calls), encoding='utf8')
    report = {'schema': 'ai6g.process_reliability.v1', 'source_kind': 'real_http_processes_with_protocol_fixture',
              'llm_evidence': False, 'physical_devices': False, 'real_6g': False,
              'source_sha256': {name: hashlib.sha256((PACKAGE/name).read_bytes()).hexdigest()
                                for name in ('runtime.py', 'executor.py', 'serve.py', 'evaluation/integration_check.py')},
              'note': 'Model adapter model_real marks HTTP success; the endpoint here is a protocol fixture. These are engineering checks, not LLM scores.',
              'fixture_config': {'observation_ttl_s': 30, 'published_quality': .99, 'stale_test_age_s': 60,
                                 'delay_s': .2, 'timeout_response_s': 3, 'quality_source': 'artificial fixture input'},
              'concurrency_cells': cells, 'checks': checks, 'passed': all(c['passed'] for c in checks),
              'client_rows': len(rows), 'fixture_requests': len(fixture.calls)}
    (out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    lines = ['# 独立进程可靠性验收', '', '真实HTTP/运行时子进程 + 人工协议端点；无真实LLM/硬件/6G性能结论。', '',
             f'验收：{report["passed"]}；客户端记录 {len(rows)} 条。', '',
             '|模式|并发|请求|成功|P50 ms|P95 ms|吞吐/秒|', '|---|---:|---:|---:|---:|---:|---:|']
    for cell in cells:
        lines.append(f'|{cell["mode"]}|{cell["concurrency"]}|{cell["n"]}|{cell["ok"]}|{cell["p50_ms"]:.2f}|{cell["p95_ms"]:.2f}|{cell["throughput_per_s"]:.2f}|')
    lines += ['', '错误、超时、坏JSON、断连、过期观测、隐私、拒绝、澄清、迟到、恢复和跨重启receipt逐项判定见report.json。',
              '状态与幂等receipt在同一文件原子替换；单运行时写入者，尚不支持多个运行时进程共写同一状态文件。']
    (out/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--concurrency', default='1,4,8')
    parser.add_argument('--requests-per-cell', type=int, default=24)
    args = parser.parse_args()
    report = run(args.out, tuple(int(n) for n in args.concurrency.split(',')), args.requests_per_cell)
    print(json.dumps({'passed': report['passed'], 'checks': len(report['checks']), 'client_rows': report['client_rows']}))
    raise SystemExit(0 if report['passed'] else 1)
