"""Exercise the real isolated C gateway and HTTP routes, without model weights."""
from __future__ import annotations

import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def request(port, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=8)
    try:
        payload = None if body is None else json.dumps(body).encode('utf-8')
        connection.request(method, path, payload, {'Content-Type': 'application/json', **(headers or {})})
        response = connection.getresponse()
        raw = response.read().decode('utf-8')
        return response.status, raw
    finally:
        connection.close()


def json_request(*args, **kwargs):
    status, raw = request(*args, **kwargs)
    return status, json.loads(raw)


@pytest.fixture
def private_run():
    work = ROOT / '_work/demo_v3'
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='automated_live_test_', dir=work) as directory:
        yield Path(directory) / 'new_run'


def test_cli_serves_page_and_real_rules_success_failure_then_stops_without_touching_originals(private_run):
    script = HERE / 'run_live.py'
    assert script.is_file(), 'The one-command live runner is missing'
    protected = [ROOT / 'openvela_smart_home/device_state.json', ROOT / 'openvela_smart_home/offload_events.jsonl']
    before = {p: sha(p) for p in protected}
    process = subprocess.Popen([sys.executable, str(script), '--port', '0', '--run-dir', str(private_run)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
    try:
        receipt = json.loads(process.stdout.readline())
        assert receipt['binding'] == '127.0.0.1'
        assert receipt['mode'] == 'rules-only'
        assert receipt['model_endpoints_configured'] is False
        assert receipt['patrol_interval_s'] == 0
        port = receipt['port']
        for route in ('/', '/web_panel.html'):
            status, page = request(port, 'GET', route)
            assert status == 200 and 'PC设备仿真' in page
            assert '执行未确认' in page
        status, first = json_request(port, 'POST', '/api/command', {'command': '打开客厅的灯'})
        assert status == 200 and first['interface_ok'] and first['semantic_complete']
        _, state = json_request(port, 'GET', '/api/state')
        assert state['lights']['客厅']['is_on'] is True
        _, failed = json_request(port, 'POST', '/api/command', {'command': '把客厅灯光设为60%，再把卧室灯光设为30%'})
        assert failed['semantic_complete'] is False and failed['actions'] == []
        _, health = json_request(port, 'GET', '/api/health')
        assert health['backends']['local-model']['ready'] is False
        assert health['backends']['cloud-model']['ready'] is False
        assert health['backends']['cloud-model'].get('queue_ms') is None
        assert health['backends']['cloud-model'].get('free_mem_mb') is None
        for route in ('/api/patrol/trigger', '/api/patrol/log', '/anything'):
            assert json_request(port, 'GET', route)[0] == 404
        assert json_request(port, 'GET', '/', headers={'Origin': 'http://untrusted.example'})[0] == 403
        assert json_request(port, 'POST', '/__shutdown', {})[0] == 403
        assert json_request(port, 'POST', '/__shutdown', {}, {'X-Isolated-Shutdown': receipt['shutdown_token']})[0] == 202
        process.wait(timeout=12)
        assert process.returncode == 0, process.stderr.read()
        manifest = json.loads((private_run / 'run_manifest.json').read_text(encoding='utf-8'))
        assert manifest['status'] == 'stopped'
        assert manifest['original_state_and_events_unchanged'] is True
        assert manifest['policy_process_exit_code'] == 0
        assert manifest['monitor_thread_stopped'] is True
        assert (private_run / 'offload_events.jsonl').is_file()
        assert (private_run / 'health_snapshots.jsonl').is_file()
        assert {p: sha(p) for p in protected} == before
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


def test_import_factory_binds_loopback_and_closes_even_without_serving(private_run):
    import run_live
    session = run_live.make_live_server(port=0, run_dir=private_run)
    policy = session.bridge.gateway.policy
    try:
        assert session.server.server_address[0] == '127.0.0.1'
        assert session.bridge.gateway.backends['local-model'] is session.bridge.gateway_backends['local-model']
        assert session.bridge.backend_monitor.backends['cloud-model'] is session.bridge.gateway_backends['cloud-model']
    finally:
        session.close()
    assert policy.proc.poll() == 0
    session.close()  # Own cleanup is safely idempotent.


def test_run_directory_cannot_reuse_existing_or_escape_private_root(private_run, tmp_path):
    import run_live
    private_run.mkdir()
    sentinel = private_run / 'keep.txt'
    sentinel.write_text('unchanged', encoding='utf-8')
    with pytest.raises((ValueError, FileExistsError)):
        run_live.make_live_server(port=0, run_dir=private_run)
    with pytest.raises(ValueError):
        run_live.make_live_server(port=0, run_dir=tmp_path / 'outside')
    assert sentinel.read_text(encoding='utf-8') == 'unchanged'
    assert not (tmp_path / 'outside').exists()


def test_bind_failure_releases_real_policy_and_monitor(private_run, monkeypatch):
    import run_live
    captured = []
    original = run_live.production_bridge
    def observe(directory):
        bridge = original(directory)
        captured.append((bridge, bridge.gateway.policy))
        return bridge
    monkeypatch.setattr(run_live, 'production_bridge', observe)
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        with pytest.raises(OSError):
            run_live.make_live_server(port=occupied.getsockname()[1], run_dir=private_run)
    bridge, policy = captured[0]
    assert policy.proc.poll() == 0
    assert bridge.gateway is None and bridge.backend_monitor is None
    manifest = json.loads((private_run / 'run_manifest.json').read_text(encoding='utf-8'))
    assert manifest['status'] == 'failed'
    assert manifest['original_state_and_events_unchanged'] is True


class ControlledRuntime:
    """Replace only model-process ownership with two literal-loopback fixtures."""
    instances = []
    def __init__(self, directory, config_path=None):
        self.servers, self.threads, self.endpoints_by_tier = [], [], {}
        self.closed = False
        self.mode = {'local': 'healthy', 'cloud': 'healthy'}
        self.posts = {'local': 0, 'cloud': 0}
        self.block_posts = False
        self.post_started = threading.Event()
        self.release_post = threading.Event()
        self.inventory = {'source': 'automation_controlled_HTTP_fixtures_no_model_weights'}
        self.manifest = {'status': 'starting', 'children': []}
        self.instances.append(self)

    def __enter__(self):
        owner = self
        for tier in ('local', 'cloud'):
            def handler(slot):
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *args): pass
                    def respond(self, value, status=200):
                        raw = json.dumps(value).encode('utf-8')
                        self.send_response(status)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Content-Length', str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                    def do_GET(self):
                        assert self.path == '/v1/models'
                        self.respond({'data': [{'id': slot + '-fixture'}]})
                    def do_POST(self):
                        assert self.path == '/v1/chat/completions'
                        self.rfile.read(int(self.headers['Content-Length']))
                        owner.posts[slot] += 1
                        if owner.block_posts:
                            owner.post_started.set()
                            assert owner.release_post.wait(timeout=5)
                        if owner.mode[slot] == 'post503':
                            return self.respond({'error': 'controlled_http_503'}, 503)
                        plan = {'answer': '', 'actions': [
                            {'name': 'light_control', 'params': {'location': '客厅', 'action': 'set', 'brightness': 60}},
                            {'name': 'light_control', 'params': {'location': '卧室', 'action': 'set', 'brightness': 30}}]}
                        self.respond({'model': slot + '-fixture', 'choices': [{'message': {'content': json.dumps(plan)}}]})
                return Handler
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(tier))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.servers.append(server)
            self.threads.append(thread)
            self.endpoints_by_tier[tier] = {'url': f'http://127.0.0.1:{server.server_port}/v1', 'model': tier + '-fixture'}
        self.manifest['status'] = 'ready'
        return self

    def endpoints(self): return self.endpoints_by_tier

    def close(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=3)
        self.closed = True
        self.manifest['status'] = 'stopped'


def test_models_wires_real_HTTP_adapters_C_choice_and_once_only_planner_recovery(private_run, monkeypatch):
    import run_live
    monkeypatch.setattr(run_live, 'LocalRuntime', ControlledRuntime)
    session = run_live.make_live_server(port=0, models=True, run_dir=private_run)
    runtime = ControlledRuntime.instances[-1]
    thread = threading.Thread(target=session.server.serve_forever, daemon=True)
    thread.start()
    policy = session.bridge.gateway.policy
    try:
        assert policy.config['local_infer_ms'] == 1116 and policy.config['cloud_infer_ms'] == 1361
        assert policy.arm == 'joint'
        assert session.bridge.gateway.backends['local-model'] is session.bridge.gateway_backends['local-model']
        assert session.bridge.backend_monitor.backends['local-model'] is session.bridge.gateway_backends['local-model']
        _, health = json_request(session.server.server_port, 'GET', '/api/health')
        assert health['backends']['local-model']['ready'] is True
        assert health['backends']['cloud-model']['ready'] is True
        assert health['backends']['local-model'].get('queue_ms') is None
        assert health['backends']['local-model'].get('free_mem_mb') is None
        runtime.mode['local'] = 'post503'
        _, result = json_request(session.server.server_port, 'POST', '/api/command', {'command': '把客厅灯光设为60%，再把卧室灯光设为30%'})
        assert result['semantic_complete'] is True and result['interface_ok'] is True
        assert result['selected_backend'] == 'local-model'
        assert result['recovery']['attempted'] is True
        assert result['actual_backend'] == 'cloud-model'
        assert runtime.posts == {'local': 1, 'cloud': 1}
        _, state = json_request(session.server.server_port, 'GET', '/api/state')
        assert state['lights']['客厅']['brightness'] == 60
        assert state['lights']['卧室']['brightness'] == 30
    finally:
        session.close()
        thread.join(timeout=3)
    assert runtime.closed and policy.proc.poll() == 0 and not thread.is_alive()
    rows = [json.loads(row) for row in (private_run / 'offload_events.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(rows) == 1 and rows[0]['semantic_complete'] is True


def test_model_start_failure_still_closes_own_runtime_and_C(private_run, monkeypatch):
    import run_live
    calls = []
    class FailingRuntime:
        def __init__(self, *args, **kwargs): calls.append('init')
        def __enter__(self): raise RuntimeError('controlled_model_start_failure')
        def close(self): calls.append('closed')
    monkeypatch.setattr(run_live, 'LocalRuntime', FailingRuntime)
    with pytest.raises(RuntimeError, match='controlled_model_start_failure'):
        run_live.make_live_server(port=0, models=True, run_dir=private_run)
    assert calls == ['init', 'closed']
    manifest = json.loads((private_run / 'run_manifest.json').read_text(encoding='utf-8'))
    assert manifest['status'] == 'failed' and manifest['policy_process_exit_code'] == 0


def test_concurrent_health_and_planning_keep_raw_HTTP_records_separate(private_run, monkeypatch):
    import run_live
    monkeypatch.setattr(run_live, 'LocalRuntime', ControlledRuntime)
    session = run_live.make_live_server(port=0, models=True, run_dir=private_run)
    runtime = ControlledRuntime.instances[-1]
    session.monitor.close()
    session.monitor._thread.join(timeout=3)
    runtime.block_posts = True
    raw = session.raw_backends['local']
    inference = threading.Thread(target=raw.infer, args=('把客厅灯光设为60%，再把卧室灯光设为30%',))
    health_done = threading.Event()
    def check_health():
        raw.health()
        health_done.set()
    health = threading.Thread(target=check_health)
    try:
        inference.start()
        assert runtime.post_started.wait(timeout=3)
        health.start()
        health_done.wait(timeout=.1)
        runtime.release_post.set()
        inference.join(timeout=5)
        health.join(timeout=5)
        assert not inference.is_alive() and not health.is_alive()
        assert [row['method'] for row in raw.attempts[-1]['upstream_exchanges']] == ['POST']
        assert [row['method'] for row in raw.probes[-1]['exchanges']] == ['GET']
    finally:
        runtime.release_post.set()
        session.close()
