"""Exercise the public HTTP boundary of the local rehearsal, not UI fixtures."""
import json
from pathlib import Path
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def test_rehearsal_actual_http_and_health_write_blocked(tmp_path):
    from ai6g_runtime.deploy.rehearsal_v7 import build_server
    server = build_server(tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path, body=None):
        req = urllib.request.Request(base+path,
            data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
            headers={'Content-Type': 'application/json'})
        try:
            response = opener.open(req, timeout=4)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.read()
    try:
        status, html = request('/')
        assert status == 200 and '软件状态'.encode() in html
        _, info = request('/v1/info')
        assert json.loads(info)['model_real'] is False
        assert request('/v1/health', {'backend': 'cloud-model', 'reachable': True})[0] == 403
        text = '打开书房灯然后关闭厨房窗帘'
        _, preview = request('/v1/contract', {'text': text})
        assert json.loads(preview)['supported'] is True
        # Preview is read-only.
        assert json.loads(request('/v1/state')[1])['lights']['书房']['is_on'] is False
        _, first = request('/v1/tasks', {'text': text, 'request_id': 'http-contract-one'})
        first = json.loads(first)
        assert first['ok'] and first['execution']['applied_actions'] == 2
        assert first['model_real'] is False
        assert first['state']['lights']['书房']['is_on'] is True
        assert first['state']['curtains']['厨房']['is_open'] is False
        _, replay = request('/v1/tasks', {'text': text, 'request_id': 'http-contract-one'})
        assert json.loads(replay)['execution']['idempotent_replay'] is True
        assert request('/v1/tasks', {'text': '关闭书房灯', 'request_id': 'http-contract-one'})[0] == 409
        _, unsupported = request('/v1/tasks', {'text': '十分钟后关闭书房灯', 'request_id': 'later'})
        unsupported = json.loads(unsupported)
        assert unsupported['execution']['applied_actions'] == 0
        assert unsupported['state']['lights']['书房']['is_on'] is True
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_rehearsal_rejects_cross_origin_task_write(tmp_path):
    from ai6g_runtime.deploy.rehearsal_v7 import build_server
    server = build_server(tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/tasks',
        data=b'{"text":"test"}', headers={'Content-Type':'application/json', 'Origin':'https://unrelated.example'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        try:
            opener.open(req, timeout=3)
            raise AssertionError('cross-origin request unexpectedly accepted')
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
