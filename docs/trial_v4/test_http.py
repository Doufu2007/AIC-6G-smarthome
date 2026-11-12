"""Actual collector HTTP, automated cohort; no participant record retained."""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_trial import DemandTrialStore, HERE
from collector import make_server


class CollectorHTTPTests(unittest.TestCase):
    def test_baseline_protocol_and_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = DemandTrialStore(tmp, 'automation_test', provenance={'models_enabled': True})
            server = make_server(store, 0, page_path=HERE / 'index.html')
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            def request(path, value=None):
                c = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
                c.request('POST' if value is not None else 'GET', path,
                    json.dumps(value).encode() if value is not None else None,
                    {'Content-Type': 'application/json', 'X-Collector-Token': server.collector_token})
                r = c.getresponse()
                raw = r.read().decode('utf-8')
                status = r.status
                c.close()
                return status, raw
            try:
                status, page = request('/')
                self.assertEqual(status, 200)
                self.assertIn('baseline-step', page)
                self.assertIn('base-acceptable_wait', page)
                status, config = request('/api/config')
                self.assertTrue(json.loads(config)['system_provenance']['models_enabled'])
                _, value = request('/api/start', {'consent': True, 'role': 'user'})
                sid = json.loads(value)['session_id']
                payload = {'session_id': sid, 'origin': 'self_authored', 'command': '开灯'}
                self.assertEqual(request('/api/task', payload)[0], 400)
                self.assertEqual(request('/api/baseline', {'session_id': sid, 'skipped': True})[0], 200)
                self.assertEqual(request('/api/task', payload)[0], 200)
                _, saved = request('/api/submit', {'session_id': sid, 'confirm_submit': True,
                    'reason': '这是自动协议检查，不是真人意见'})
                self.assertEqual(json.loads(saved)['record_kind'], 'automation_test')
                self.assertEqual(store.summary()['participant_feedback_count'], 0)
                self.assertEqual(request('/api/cancel', {'session_id': sid})[0], 200)
                self.assertFalse(list(Path(tmp).rglob('*.json')))
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
