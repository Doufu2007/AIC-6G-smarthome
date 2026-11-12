"""Real isolated gateway/MQTT cancellation; no models or actual participant."""
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class CleanupTests(unittest.TestCase):
    def test_cancel_closes_and_erases_own_command_logs(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = subprocess.Popen([sys.executable, '-X', 'utf8',
                str(ROOT / 'docs/trial_v4/run_trial.py'), '--rules-only', '--port', '0', '--gateway-port', '0',
                '--record-kind', 'automation_test', '--data-dir', temporary], cwd=ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
            try:
                line = proc.stdout.readline()
                self.assertTrue(line, 'trial process did not report readiness')
                info = json.loads(line)
                port = int(info['url'].rsplit(':', 1)[1])
                def req(path, value=None, token=None):
                    c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
                    c.request('POST' if value else 'GET', path,
                        json.dumps(value, ensure_ascii=False).encode('utf-8') if value else None,
                        {'Content-Type': 'application/json', 'X-Collector-Token': token or ''})
                    r = c.getresponse()
                    body = r.read().decode('utf-8')
                    c.close()
                    return body
                page = req('/')
                token = page.split("const token='")[1].split("'")[0]
                record = json.loads(req('/api/start', {'consent': True, 'role': 'user'}, token))
                sid = record['session_id']
                req('/api/baseline', {'session_id': sid, 'skipped': True}, token)
                task = json.loads(req('/api/task', {'session_id': sid, 'command': '打开客厅的灯',
                    'origin': 'self_authored'}, token))
                self.assertTrue(task['state_execution_confirmed'])
                v4_manifest = ROOT / record['system_provenance']['run_manifest']
                v4_dir = v4_manifest.parent
                manifest = json.loads(v4_manifest.read_text(encoding='utf-8'))
                v3_dir = (ROOT / manifest['legacy_run_manifest']).parent
                self.assertTrue((v3_dir / 'offload_events.jsonl').is_file())
                self.assertTrue((v4_dir / 'mqtt/gateway.sqlite3').is_file())
                req('/api/cancel', {'session_id': sid}, token)
                output, error = proc.communicate(timeout=35)
                self.assertEqual(proc.returncode, 0, error)
                cleanup = json.loads(output.strip())['private_run_cleanup']
                self.assertTrue(cleanup['all_absent'])
                self.assertFalse(v3_dir.exists())
                self.assertFalse(v4_dir.exists())
                self.assertFalse(list(Path(temporary).rglob('*.json')))
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
