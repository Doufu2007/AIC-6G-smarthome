"""Regression tests for HTTP availability, planner deadlines and request identity."""
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import socket
import time

ROOT = Path(__file__).resolve().parents[2]
for folder in ('docs/demo_v3', 'docs/trial_v2', 'SenseHome-6G/net_offload'):
    sys.path.insert(0, str(ROOT / folder))
from run_isolated_gateway import make_isolated_server
from planning_recovery import PlanningRecoveryBackend
from offload_gateway import BackendResult


def test_health_responds_while_other_command_waits():
    entered, release = threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            entered.set()
            assert release.wait(3)
            self.send_json({'done': True})
        def do_GET(self):
            self.send_json({'healthy': True})
    server, _ = make_isolated_server(SimpleNamespace(BridgeHandler=Handler), 0)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    def request(method, path):
        c = HTTPConnection('127.0.0.1', server.server_port, timeout=.4)
        try:
            c.request(method, path, '{}', {'Content-Type': 'application/json'})
            r = c.getresponse()
            return r.status, json.loads(r.read())
        finally:
            c.close()
    with ThreadPoolExecutor(1) as pool:
        command = pool.submit(request, 'POST', '/api/command')
        try:
            assert entered.wait(1)
            assert request('GET', '/api/health') == (200, {'healthy': True})
        finally:
            release.set()
            try:
                command.result()
            except TimeoutError:
                pass
            server.shutdown()
            server.server_close()
            worker.join(2)


class Clock:
    value = 0.
    def __call__(self): return self.value


class Planner:
    def __init__(self, clock, url, duration, success):
        self.clock, self.base_url, self.duration, self.success = clock, url, duration, success
        self.seen = []
    def health(self): return {'ready': True, 'reachable': True}
    def infer(self, command, context):
        self.seen.append(dict(context or {}))
        allowance = (context or {}).get('_planning_timeout_s', self.duration)
        self.clock.value += min(self.duration, allowance)
        ok = self.success and self.duration <= allowance
        return BackendResult(ok, ok, {}, [], error=None if ok else 'TimeoutError')


def test_slow_primary_has_own_budget_and_leaves_time_for_backup():
    clock = Clock()
    a, b = Planner(clock, 'http://127.0.0.1:10001', 15, False), Planner(clock, 'http://127.0.0.1:10002', .1, True)
    wrapper = PlanningRecoveryBackend(a, b, primary_name='a', secondary_name='b',
        primary_url=a.base_url, secondary_url=b.base_url, launch_budget_ms=10000,
        total_budget_ms=10000, primary_budget_ms=4000, clock=clock)
    result = wrapper.infer('关闭书房的灯并打开书房窗帘')
    assert result.ok and len(b.seen) == 1
    assert a.seen[0]['_planning_timeout_s'] <= 4
    assert result.response['recovery']['elapsed_ms'] <= 10000


def test_late_secondary_plan_is_discarded():
    clock = Clock()
    a, b = Planner(clock, 'http://127.0.0.1:10001', 1, False), Planner(clock, 'http://127.0.0.1:10002', 15, True)
    def ignoring_timeout(command, context):
        clock.value += 15
        return BackendResult(True, True, {}, [{'name': 'light_control', 'params': {'location': '书房', 'action': 'off'}}])
    b.infer = ignoring_timeout
    wrapper = PlanningRecoveryBackend(a, b, primary_name='a', secondary_name='b',
        primary_url=a.base_url, secondary_url=b.base_url, total_budget_ms=10000,
        primary_budget_ms=4000, clock=clock)
    result = wrapper.infer('关闭书房的灯并打开书房窗帘')
    assert not result.ok and result.actions == []
    assert result.error == 'DEADLINE_EXCEEDED'


def test_request_id_is_bound_before_planning_and_survives_reopen(tmp_path):
    from command_requests import RequestJournal
    calls = []
    journal = RequestJournal(tmp_path / 'requests.sqlite3')
    def operation():
        calls.append(1)
        return {'semantic_complete': True, 'interface_ok': True, 'actions': []}
    first = journal.execute('打开客厅灯', {'command_id': 'request-a', 'session_id': 'session-a'}, operation)
    second = RequestJournal(tmp_path / 'requests.sqlite3').execute('打开客厅灯', {'command_id': 'request-a', 'session_id': 'session-a'}, operation)
    assert len(calls) == 1 and first['semantic_complete'] and second['replayed']
    conflict = journal.execute('关闭客厅灯', {'command_id': 'request-a', 'session_id': 'session-a'}, operation)
    assert conflict['error'] == 'COMMAND_ID_CONFLICT' and len(calls) == 1


def test_inflight_duplicate_does_not_plan_twice_and_interrupted_stays_unknown(tmp_path):
    from command_requests import RequestJournal
    entered, release = threading.Event(), threading.Event()
    journal = RequestJournal(tmp_path / 'requests.sqlite3')
    calls = []
    def operation():
        calls.append(1); entered.set()
        assert release.wait(2)
        return {'semantic_complete': False, 'interface_ok': False, 'actions': [], 'execution_state': 'unknown'}
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(journal.execute, '打开客厅灯', {'command_id': 'request-a'}, operation)
        assert entered.wait(1)
        duplicate = journal.execute('打开客厅灯', {'command_id': 'request-a'}, operation)
        release.set(); first.result()
    assert len(calls) == 1 and duplicate['request_state'] == 'processing'
    assert journal.lookup('request-a')['execution_state'] == 'unknown'


def test_http_absolute_deadline_covers_slow_dripping_headers():
    sys.path.insert(0, str(ROOT / 'docs/deployment'))
    from fused_eval import exchange
    ready = threading.Event()
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0)); listener.listen()
    def serve():
        conn, _ = listener.accept()
        with conn:
            conn.recv(4096)
            try:
                for character in b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{}':
                    conn.send(bytes([character])); time.sleep(.03)
            except OSError:
                pass
    worker = threading.Thread(target=serve); worker.start()
    started = time.monotonic()
    try:
        row = exchange(f'http://127.0.0.1:{listener.getsockname()[1]}', 'GET', 'models', None, .12)
        elapsed = time.monotonic() - started
        assert row['error'] and elapsed < .35
    finally:
        listener.close(); worker.join(3)
