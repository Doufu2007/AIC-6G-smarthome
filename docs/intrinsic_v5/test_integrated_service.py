"""Whole source pipeline: controlled HTTP planners and real local MQTT software devices."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import http.client
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'docs/demo_v4'))
sys.path.insert(0, str(ROOT / 'docs/demo_v3'))
from live_v4 import IntegratedSession
import run_live


def runtime_class():
    spec = importlib.util.spec_from_file_location('intrinsic_http_planner_fixture', ROOT / 'docs/demo_v3/test_run_live.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ControlledRuntime


def request(port, method, path, value=None):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=12)
    try:
        conn.request(method, path, json.dumps(value).encode() if value is not None else None,
                     {'Content-Type': 'application/json'})
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def test_concurrent_http_requests_clarification_and_mqtt_receipts(monkeypatch):
    Runtime = runtime_class()
    monkeypatch.setattr(run_live, 'LocalRuntime', Runtime)
    with IntegratedSession(port=0, models=True, mqtt=True, strategy='local-first') as session:
        runtime = Runtime.instances[-1]
        port = session.server.server_port
        serving = threading.Thread(target=session.server.serve_forever)
        serving.start()
        def command(text, identifier, sid='conversation-a'):
            return request(port, 'POST', '/api/command', {'command': text, 'context': {'command_id': identifier, 'session_id': sid}})[1]
        runtime.block_posts = True
        # This regression owns planner blocking/concurrent HTTP behavior;
        # explicit compilation is independently covered by V6 public tests.
        original_handle = session.bridge.gateway.handle
        def force_planner(*args, **kwargs):
            kwargs.pop('prefer_explicit_rules', None)
            return original_handle(*args, **kwargs)
        monkeypatch.setattr(session.bridge.gateway, 'handle', force_planner)
        text = '把客厅灯光设为60%，再把卧室灯光设为30%'
        try:
            with ThreadPoolExecutor(1) as pool:
                pending = pool.submit(command, text, 'integration-main')
                try:
                    assert runtime.post_started.wait(3)
                    started = time.monotonic()
                    assert request(port, 'GET', '/api/health')[0] == 200
                    assert request(port, 'GET', '/api/state')[0] == 200
                    assert time.monotonic() - started < .5
                    duplicate = command(text, 'integration-main')
                    assert duplicate['request_state'] == 'processing'
                    independent = command('关闭书房的灯', 'integration-independent', 'another-session')
                    assert independent['task_complete']
                finally:
                    runtime.release_post.set()
                done = pending.result(timeout=8)
            assert done['task_complete'] and done['execution_complete']
            posts = dict(runtime.posts)
            repeated = command(text, 'integration-main')
            assert repeated['replayed'] and repeated['task_complete'] and runtime.posts == posts
            conflict = command('关闭客厅的灯', 'integration-main')
            assert conflict['error'] == 'COMMAND_ID_CONFLICT'
            assert request(port, 'GET', '/api/requests/integration-main')[1]['task_complete']
            first = command('有点暗', 'clarify-1')
            assert first['clarification_required']
            second = command('卧室', 'clarify-2')
            assert second['clarification_required'] and second['missing_slots'] == ['value']
            completed = command('调到60%', 'clarify-3')
            assert completed['task_complete']
            snapshot = session.bridge.gateway_receipt_executor.snapshot()
            assert snapshot['software_state']['lights']['卧室']['brightness'] == 60
            patrol = request(port, 'POST', '/api/patrol/trigger', {'context': {'command_id': 'patrol-1'}})[1]
            assert patrol['status'] == 'ok' and patrol['report']['task_complete']
            patrol_version = session.bridge.gateway_receipt_executor.snapshot()['state_version']
            repeated_patrol = request(port, 'POST', '/api/patrol/trigger', {'context': {'command_id': 'patrol-1'}})[1]
            assert repeated_patrol['report']['replayed']
            assert session.bridge.gateway_receipt_executor.snapshot()['state_version'] == patrol_version
            assert request(port, 'GET', '/api/patrol/trigger')[0] == 405
            leaving = command('离家模式', 'scene-1')
            assert leaving['supported_scope_complete']
            assert not leaving['task_complete'] and 'security_arming' in leaving['unsupported_capabilities']
        finally:
            runtime.release_post.set()
            session.close()
            serving.join(5)
        assert not serving.is_alive()


def test_live_adapter_reports_admission_queue_and_updates_measured_task_costs(monkeypatch):
    Runtime = runtime_class()
    monkeypatch.setattr(run_live, 'LocalRuntime', Runtime)
    with IntegratedSession(port=0, models=True, mqtt=False, strategy='joint') as session:
        runtime = Runtime.instances[-1]
        raw = session.session.raw_backends['local']
        runtime.block_posts = True
        text = '把客厅灯光设为60%，再把卧室灯光设为30%'
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(raw.infer, text)
            assert runtime.post_started.wait(3)
            second = pool.submit(raw.infer, text)
            try:
                until = time.monotonic() + 2
                while raw.resource_snapshot()['waiting_requests'] != 1 and time.monotonic() < until:
                    threading.Event().wait(.01)
                metrics = raw.resource_snapshot()
                assert metrics['inflight'] == 1 and metrics['waiting_requests'] == 1
                assert metrics['queue_ms'] > 0 and metrics['upstream_queue_ms'] is None
                assert raw.health()['ready'] is True
            finally:
                runtime.release_post.set()
            assert first.result().ok and second.result().ok
        costs = raw.resource_snapshot()['cost_by_task']['control_multi']
        assert costs['samples'] >= 2 and costs['usable']
        attempt = raw.attempts[-1]['upstream_exchanges'][0]
        assert all(key in attempt for key in ('connect_request_ms', 'response_headers_ms', 'response_body_ms'))
        assert raw.resource_snapshot()['inflight'] == 0
