"""Malformed public patrol contexts must fail before request admission."""
from contextlib import ExitStack
import http.client
import http.server
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
DASH = ROOT / 'openvela_smart_home/dashboard'
sys.path[:0] = [str(DASH), str(ROOT / 'SenseHome-6G/net_offload')]


class PatrolContextTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        spec = importlib.util.spec_from_file_location('patrol_context_v6_bridge', DASH / 'bridge_server.py')
        self.bridge = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {
            'SENSEHOME_STATE_FILE': str(folder / 'state.json'),
            'SENSEHOME_EVENT_LOG': str(folder / 'events.jsonl'),
            'SENSEHOME_REQUEST_DB': str(folder / 'requests.sqlite3'),
            'SENSEHOME_POLICY_CLI': str(folder / 'absent-cli'),
            'SENSEHOME_RECEIPT_DB': '', 'SENSEHOME_LOCAL_URL': '', 'SENSEHOME_CLOUD_URL': '',
        }):
            spec.loader.exec_module(self.bridge)
        self.stack.callback(self.bridge.close_gateway)
        self.admit = self.stack.enter_context(patch.object(self.bridge, 'process_http_command', return_value={'task_complete': False}))
        self.stack.enter_context(patch.object(self.bridge.BridgeHandler, 'log_message'))
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), self.bridge.BridgeHandler)
        self.server.daemon_threads = True
        worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        worker.start()
        self.stack.callback(self.server.server_close)
        self.stack.callback(worker.join, 3)
        self.stack.callback(self.server.shutdown)

    def post(self, path, context):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request('POST', path, json.dumps({'command': '打开客厅灯', 'context': context}),
                               {'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_both_mutation_routes_reject_nonobject_context_before_admission(self):
        for path in ('/api/command', '/api/patrol/trigger'):
            for context in (1, True, [[1, 2]], None, 'text'):
                with self.subTest(path=path, context=context):
                    self.admit.reset_mock()
                    status, result = self.post(path, context)
                    self.assertEqual(status, 400)
                    self.assertEqual(result['error'], 'INVALID_CONTEXT')
                    self.admit.assert_not_called()

    def test_object_context_is_preserved(self):
        status, _ = self.post('/api/patrol/trigger', {'command_id': 'valid-patrol'})
        self.assertEqual(status, 200)
        self.admit.assert_called_once_with('服务器主动巡检', {'command_id': 'valid-patrol'}, patrol=True)


if __name__ == '__main__':
    unittest.main()
