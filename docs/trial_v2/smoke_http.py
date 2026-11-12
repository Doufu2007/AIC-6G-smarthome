"""Run the collector's real local HTTP boundary against a labelled test fixture.

This script creates automation_test records in a temporary folder only. It
neither contacts shared models nor creates participant feedback.
"""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import tempfile
import threading

from collector import PRESETS, TrialStore, make_server


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_body(self, body):
        raw = json.dumps(body).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        assert self.path == '/api/state'
        self.send_body({'lights': {'客厅': {'is_on': False, 'brightness': 0}},
                        'source': 'automation_fixture_not_production_gateway'})

    def do_POST(self):
        assert self.path == '/api/command'
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        self.send_body({'source': 'automation_fixture_not_a_model', 'answer': 'TEST CLAIM: done',
                        'semantic_complete': True, 'interface_ok': True, 'actions': []})


def call(server, method, path, data=None, token=True, origin=None, host=None):
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['X-Collector-Token'] = server.collector_token
    if origin:
        headers['Origin'] = origin
    if host:
        headers['Host'] = host
    raw = json.dumps(data).encode('utf-8') if data is not None else None
    connection.request(method, path, body=raw, headers=headers)
    response = connection.getresponse()
    content = response.read()
    status = response.status
    connection.close()
    if response.headers.get('Content-Type', '').startswith('application/json'):
        content = json.loads(content.decode('utf-8'))
    return status, content


def main():
    report = {'schema': 'sensehome.collector-http-smoke.v2',
              'started_at_utc': datetime.now(timezone.utc).isoformat(),
              'scope': 'automation_test local HTTP fixture; no real user, model or physical device evidence',
              'checks': []}
    fixture = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    threading.Thread(target=fixture.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            store = TrialStore(Path(directory), 'automation_test', f'http://127.0.0.1:{fixture.server_port}')
            server = make_server(store, 0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                status, html = call(server, 'GET', '/', token=False)
                assert status == 200 and b'<html lang="zh-CN">' in html
                assert '__COLLECTOR_TOKEN__' not in html.decode('utf-8')
                report['checks'].append('Chinese HTML served with concrete same-origin token')
                status, _ = call(server, 'POST', '/api/start', {'consent': True}, token=False)
                assert status == 403
                status, _ = call(server, 'POST', '/api/start', {'consent': True}, origin='https://example.com')
                assert status == 403
                status, _ = call(server, 'GET', '/', host='outside.example')
                assert status == 403
                status, _ = call(server, 'GET', '/../collector.py')
                assert status == 404
                report['checks'].append('Token, Origin, Host and fixed-route boundaries reject unauthorized access')
                status, _ = call(server, 'POST', '/api/start', {'consent': False, 'role': 'user'})
                assert status == 400 and not list(Path(directory).rglob('*.json'))
                report['checks'].append('Disagreement writes no record')
                status, session = call(server, 'POST', '/api/start', {'consent': True, 'role': 'user'})
                assert status == 200
                session_id = session['session_id']
                status, task = call(server, 'POST', '/api/task', {
                    'session_id': session_id, 'command': PRESETS['T1']['command'], 'origin': 'preset', 'preset_id': 'T1'})
                assert status == 200 and task['transport'] == 'actual_loopback_gateway_http'
                assert task['task_completed'] is False and not task['state_execution_confirmed']
                assert not list(Path(directory).rglob('*.json'))
                report['checks'].append('Real local HTTP200 fixture claim with no action/state change is not task completion')
                status, _ = call(server, 'POST', '/api/submit', {'session_id': session_id,
                    'clear_or_confusing': 'AUTOMATED_SMOKE_TEST_NOT_USER_FEEDBACK'})
                assert status == 400
                status, saved = call(server, 'POST', '/api/submit', {'session_id': session_id,
                    'confirm_submit': True, 'clear_or_confusing': '=1+2', 'reason': 'AUTOMATED_SMOKE_TEST_NOT_USER_FEEDBACK'})
                assert status == 200 and saved['record_kind'] == 'automation_test'
                assert store.summary()['participant_feedback_count'] == 0
                assert store.summary()['valid_connected_trial_count'] == 0
                assert store.summary()['automation_test_count'] == 1
                report['summary_after_automation_submit'] = store.summary()
                status, csv = call(server, 'GET', '/api/export.csv')
                assert status == 200 and "'=1+2" in csv.decode('utf-8-sig')
                status, exported = call(server, 'GET', '/api/export.json')
                assert status == 200 and exported['records'][0]['feedback']['clear_or_confusing'] == '=1+2'
                report['checks'].append('Explicit submit stores automation cohort separately; CSV neutralizes formulas and JSON preserves raw text')
                status, _ = call(server, 'POST', '/api/cancel', {'session_id': session_id})
                assert status == 200 and not list(Path(directory).rglob('*.json'))
                report['summary_after_withdrawal'] = store.summary()
                report['checks'].append('Withdrawal deletes the saved record')
            finally:
                server.shutdown()
                server.server_close()
    finally:
        fixture.shutdown()
        fixture.server_close()
    report['status'] = 'passed'
    report['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
    output = Path(__file__).with_name('verification')
    output.mkdir(exist_ok=True)
    (output / 'http_smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
