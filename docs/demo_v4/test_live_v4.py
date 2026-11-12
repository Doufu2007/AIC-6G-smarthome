import http.client
import json
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from live_v4 import IntegratedSession, resolve_strategy


class LiveV4Tests(unittest.TestCase):
    def test_explicit_strategy_and_invalid_rejected(self):
        self.assertEqual(resolve_strategy('local-first')[0], 'local-first')
        with self.assertRaises(ValueError):
            resolve_strategy('made-up')

    def test_integrated_mqtt_rule_http_and_current_state(self):
        with IntegratedSession(port=0, models=False, mqtt=True, strategy='local-first') as session:
            worker = threading.Thread(target=session.server.serve_forever, daemon=True)
            worker.start()
            port = session.server.server_port
            def req(method, path, value=None):
                c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
                c.request(method, path, json.dumps(value).encode() if value is not None else None,
                          {'Content-Type': 'application/json'})
                r = c.getresponse()
                status, body = r.status, json.loads(r.read())
                c.close()
                return status, body
            status, body = req('POST', '/api/command', {'command': '打开客厅的灯',
                'context': {'command_id': 'live-v4-a'}})
            self.assertEqual(status, 200)
            self.assertTrue(body['semantic_complete'])
            self.assertEqual(req('GET', '/api/receipts/live-v4-a')[1]['execution_state'], 'applied')
            self.assertTrue(req('GET', '/api/state')[1]['lights']['客厅']['is_on'])
            self.assertFalse(session.manifest['physical_execution_confirmed'])
            self.assertTrue(session.manifest['mqtt_enabled'])
        worker.join(timeout=5)
        self.assertTrue(session.manifest['original_state_and_events_unchanged'])


if __name__ == '__main__':
    unittest.main()
