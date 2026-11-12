"""Record a new private HTTP/MQTT smoke run using the pinned real local models.

--check validates the launch plan without importing the gateway or starting any
process. --run-real explicitly runs the existing IntegratedSession; fixture or
rules-only modes are deliberately unavailable in this evidence script.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import http.client
import json
from pathlib import Path
import secrets
import shutil
import sys
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
MODEL_CONFIG = ROOT / 'docs/demo_v3/local_models.json'
PROTECTED = (ROOT / 'openvela_smart_home/device_state.json',
             ROOT / 'openvela_smart_home/offload_events.jsonl')
SOURCES = (Path(__file__), ROOT / 'docs/demo_v4/live_v4.py', ROOT / 'docs/demo_v3/run_live.py',
           ROOT / 'docs/demo_v3/runtime.py', ROOT / 'docs/demo_v3/planning_recovery.py',
           ROOT / 'docs/deployment/fused_eval.py', ROOT / 'docs/trial_v2/run_isolated_gateway.py',
           ROOT / 'openvela_smart_home/dashboard/bridge_server.py',
           ROOT / 'openvela_smart_home/dashboard/device_actions.py',
           ROOT / 'openvela_smart_home/dashboard/backend_monitor.py',
           ROOT / 'openvela_smart_home/dashboard/clarification_sessions.py',
           ROOT / 'openvela_smart_home/dashboard/state_freshness.py',
           ROOT / 'openvela_smart_home/dashboard/bounded_http.py',
           ROOT / 'SenseHome-6G/net_offload/backends/openai_backend.py',
           ROOT / 'SenseHome-6G/net_offload/online_metrics.py',
           ROOT / 'SenseHome-6G/net_offload/resource_policy.py',
           ROOT / 'SenseHome-6G/net_offload/command_requests.py',
           ROOT / 'SenseHome-6G/net_offload/target_contract.py',
           ROOT / 'SenseHome-6G/net_offload/durable_receipts.py',
           ROOT / 'SenseHome-6G/net_offload/mqtt_receipts.py')


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    path = Path(path)
    if not path.is_file():
        return None
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def hashes(paths):
    return {p.relative_to(ROOT).as_posix(): digest(p) for p in paths}


def write_new(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


def preflight():
    """Read only: actual executable/model pin verification is LocalRuntime's job."""
    cfg = json.loads(MODEL_CONFIG.read_text(encoding='utf-8'))
    executable = (MODEL_CONFIG.parent / cfg['runtime']['path']).resolve(strict=True)
    if executable.name.lower() != 'llama-server.exe':
        raise ValueError('REAL_SMOKE_REQUIRES_PINNED_LLAMA_SERVER')
    models = {}
    for tier in ('local', 'cloud'):
        spec = cfg['models'][tier]
        path = (MODEL_CONFIG.parent / spec['path']).resolve(strict=True)
        with path.open('rb') as stream:
            if path.suffix.lower() != '.gguf' or stream.read(4) != b'GGUF':
                raise ValueError('REAL_SMOKE_REQUIRES_GGUF_WEIGHTS:' + tier)
        models[tier] = {'path': str(path), 'bytes': path.stat().st_size,
                        'model_id': spec['model_id'], 'port': spec['port'],
                        'expected_sha256': spec['sha256']}
    return {'checked_at_utc': now(), 'model_config': str(MODEL_CONFIG),
            'model_config_sha256': digest(MODEL_CONFIG), 'runtime': str(executable),
            'runtime_expected_sha256': cfg['runtime']['sha256'], 'models': models,
            'launch': {'models': True, 'mqtt': True, 'strategy': 'local-first', 'http_host': '127.0.0.1'},
            'check_scope': 'file presence, model headers and pinned launch plan; no processes or inference started'}


def http_record(session, method, path, payload=None):
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
    row = {'started_at_utc': now(), 'method': method, 'path': path,
           'request': deepcopy(payload), 'http_status': None, 'raw_response_text': None,
           'response': None, 'error': None}
    started = time.perf_counter()
    connection = http.client.HTTPConnection('127.0.0.1', session.server.server_port, timeout=22)
    try:
        connection.request(method, path, body,
                           {'Content-Type': 'application/json'} if body is not None else {})
        response = connection.getresponse()
        raw = response.read(16 * 1024 * 1024 + 1)
        row['http_status'] = response.status
        row['response_headers'] = dict(response.getheaders())
        row['raw_response_text'] = raw.decode('utf-8', errors='replace')
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError('SMOKE_RESPONSE_TOO_LARGE')
        row['response'] = json.loads(raw)
    except Exception as exc:
        row['error'] = type(exc).__name__ + ':' + str(exc)
    finally:
        row['wall_ms'] = round((time.perf_counter() - started) * 1000, 3)
        connection.close()
    return row


def model_records(session):
    records = {}
    for tier, backend in session.session.raw_backends.items():
        with backend._record_lock:
            records[tier] = {'metadata': deepcopy(backend.metadata),
                             'attempts': deepcopy(backend.attempts), 'probes': deepcopy(backend.probes)}
    return records


def run(output):
    plan = preflight()
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    before = hashes(PROTECTED)
    source_before = hashes(SOURCES)
    write_new(output / 'launch_plan.json', plan)
    write_new(output / 'protected_before.json', before)
    session, server_thread = None, None
    requests, checks, errors = [], {}, []
    runtime_manifest, attempts = None, {}
    started = now()
    try:
        sys.path.insert(0, str(ROOT / 'docs/demo_v4'))
        from live_v4 import IntegratedSession
        session = IntegratedSession(port=0, models=True, mqtt=True, strategy='local-first',
                                    model_config=MODEL_CONFIG, data_dir=output / 'data')
        runtime = session.session.runtime
        if runtime is None or not runtime.children or any(p.poll() is not None for p in runtime.children):
            raise RuntimeError('REAL_MODEL_CHILDREN_NOT_RUNNING')
        if any(b.metadata.get('evidence_kind') != 'real-model'
               for b in session.session.raw_backends.values()):
            raise RuntimeError('FIXTURE_IS_NOT_REAL_MODEL_EVIDENCE')
        server_thread = threading.Thread(target=session.server.serve_forever, name='real-smoke-http')
        server_thread.start()
        token = secrets.token_hex(6)
        def command(category, text, sequence=1, session_name=None):
            context = {'command_id': f'smoke-{token}-{category}-{sequence}',
                       'session_id': f'smoke-{token}-{session_name or category}'}
            row = http_record(session, 'POST', '/api/command', {'command': text, 'context': context})
            row.update(category=category, sequence=sequence)
            requests.append(row)
            write_new(output / f'http_{len(requests):02d}_{category}_{sequence}.json', row)
            return row.get('response') or {}

        write_new(output / 'initial_health_http.json', http_record(session, 'GET', '/api/health'))
        write_new(output / 'initial_state_http.json', http_record(session, 'GET', '/api/state'))
        single = command('single-rule', '把客厅灯光设为60%')
        checks['single_rule_task_complete'] = single.get('task_complete') is True
        complex_result = command('two-room-complex', '把客厅灯光设为35%，再把卧室灯光设为25%')
        checks['complex_task_complete'] = complex_result.get('task_complete') is True
        clarification = [command('clarification', text, sequence=index, session_name='clarification')
                         for index, text in enumerate(('有点暗', '卧室', '调到60%'), 1)]
        checks['clarification_first_two_require_more_information'] = all(
            row.get('clarification_required') is True and row.get('task_complete') is False
            for row in clarification[:2])
        checks['clarification_third_task_complete'] = clarification[-1].get('task_complete') is True
        leave = command('leave-scene', '离家模式')
        checks['leave_never_claims_full_task_complete'] = leave.get('task_complete') is False
        checks['leave_supported_scope_complete'] = leave.get('supported_scope_complete') is True
        checks['leave_declares_unavailable_security'] = 'security_arming' in leave.get('unsupported_capabilities', [])
        checks['all_command_http_status_200'] = all(row['http_status'] == 200 and row['error'] is None for row in requests)
        checks['all_action_results_are_software_scope'] = all(
            (row.get('response') or {}).get('execution_scope') == 'mqtt-software-entity'
            for row in requests if (row.get('response') or {}).get('execution_state') is not None)
        write_new(output / 'final_state_http.json', http_record(session, 'GET', '/api/state'))
        write_new(output / 'final_entity_snapshot.json', session.bridge.gateway_receipt_executor.snapshot())
        attempts = model_records(session)
        runtime_manifest = deepcopy(runtime.manifest)
    except BaseException as exc:
        errors.append({'phase': 'run', 'type': type(exc).__name__, 'message': str(exc),
                       'traceback': traceback.format_exc()})
    finally:
        if session is not None:
            try:
                session.close()
            except BaseException as exc:
                errors.append({'phase': 'cleanup', 'type': type(exc).__name__, 'message': str(exc)})
            if server_thread is not None:
                server_thread.join(timeout=5)
                checks['http_thread_stopped'] = not server_thread.is_alive()
            attempts = model_records(session)
            runtime_manifest = deepcopy(session.session.runtime.manifest) if session.session.runtime else runtime_manifest
            write_new(output / 'integrated_manifest.json', session.manifest)
            write_new(output / 'gateway_run_manifest.json', session.session.manifest)
            if runtime_manifest is not None:
                write_new(output / 'runtime_manifest.json', runtime_manifest)
            for name in ('health_snapshots.jsonl', 'offload_events.jsonl', 'model_backend_records.json',
                         'local_server.log', 'cloud_server.log'):
                source = session.session.run_dir / name
                if source.is_file():
                    with source.open('rb') as incoming, (output / name).open('xb') as outgoing:
                        shutil.copyfileobj(incoming, outgoing)
            checks['model_children_stopped'] = all(p.poll() is not None for p in session.session.runtime.children)
            checks['mqtt_children_stopped'] = not session.mqtt.children
            checks['policy_process_stopped'] = session.session.policy.proc.poll() is not None
        after = hashes(PROTECTED)
        checks['original_state_and_events_unchanged'] = before == after
        checks['source_unchanged_during_smoke'] = source_before == hashes(SOURCES)
        write_new(output / 'protected_after.json', after)
        write_new(output / 'model_attempts.json', attempts)
        posts = [row for backend in attempts.values() for attempt in backend['attempts']
                 for row in attempt.get('upstream_exchanges', []) if row.get('method') == 'POST']
        successful_posts = [row for row in posts if row.get('http_status') == 200
                            and row.get('response', {}).get('choices') and not row.get('error')]
        checks['at_least_one_real_model_response_recorded'] = bool(successful_posts)
        summary = {'schema': 'sensehome.intrinsic-real-smoke.v1', 'started_at_utc': started,
                   'finished_at_utc': now(), 'output_directory': str(output),
                   'status': 'passed' if checks and all(checks.values()) and not errors else 'failed_or_incomplete',
                   'checks': checks, 'errors': errors, 'command_requests': requests,
                   'real_model_http_post_count': len(posts), 'real_model_successful_post_count': len(successful_posts),
                   'strategy': 'local-first', 'model_config_sha256': plan['model_config_sha256'],
                   'source_sha256': source_before, 'protected_before_sha256': before,
                   'protected_after_sha256': after,
                   'execution_scope': 'mqtt-software-entity', 'physical_execution_confirmed': False,
                   'evidence_scope': 'New local pinned-model HTTP run and MQTT software entity only; no fixture, cloud GPU, physical device or 6G measurement; old sealed results do not validate this run.'}
        write_new(output / 'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--check', action='store_true')
    modes.add_argument('--run-real', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.check:
        print(json.dumps(preflight(), ensure_ascii=False, indent=2))
        return 0
    output = args.output or ROOT / '_work/intrinsic_fixes' / ('real_smoke_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_' + secrets.token_hex(4))
    result = run(output)
    print(json.dumps({key: result[key] for key in ('status', 'output_directory', 'checks', 'real_model_http_post_count')}, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
