"""Real subprocess/HTTP check that the isolated runner preserves production behavior."""
import hashlib
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def request(port, method, path, body=None, extra_headers=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    headers = {'Content-Type': 'application/json', **(extra_headers or {})}
    connection.request(method, path, json.dumps(body).encode('utf-8') if body is not None else None, headers)
    response = connection.getresponse()
    raw = response.read()
    result = json.loads(raw.decode('utf-8'))
    connection.close()
    return response.status, result


class IsolatedGatewayTests(unittest.TestCase):
    def test_production_rules_run_on_private_state_and_unconfigured_complex_task_stays_failed(self):
        script = Path(__file__).with_name('run_isolated_gateway.py')
        self.assertTrue(script.is_file(), 'The isolated production gateway runner is not implemented')
        protected = [ROOT / 'openvela_smart_home/device_state.json', ROOT / 'openvela_smart_home/offload_events.jsonl']
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None for p in protected}
        work = ROOT / '_work/trial_v2'
        work.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=work) as temporary:
            run_dir = Path(temporary) / 'run'
            process = subprocess.Popen([sys.executable, str(script), '--port', '0', '--run-dir', str(run_dir)],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
            try:
                line = process.stdout.readline()
                self.assertTrue(line, process.stderr.read() if process.poll() is not None else 'No startup receipt')
                info = json.loads(line)
                self.assertEqual(info['binding'], '127.0.0.1')
                self.assertFalse(info['model_endpoints_configured'])
                self.assertEqual(info['patrol_interval_s'], 0)
                status, first = request(info['port'], 'POST', '/api/command', {'command': '打开客厅的灯'})
                self.assertEqual(status, 200)
                self.assertTrue(first['semantic_complete'])
                _, state = request(info['port'], 'GET', '/api/state')
                self.assertTrue(state['lights']['客厅']['is_on'])
                _, complex_result = request(info['port'], 'POST', '/api/command', {
                    'command': '把客厅灯光设为60%，再把卧室灯光设为30%'})
                self.assertFalse(complex_result['semantic_complete'])
                self.assertEqual(complex_result['actions'], [])
                status, _ = request(info['port'], 'GET', '/api/patrol/trigger')
                self.assertEqual(status, 404)
                status, _ = request(info['port'], 'POST', '/__shutdown', {},
                    {'X-Isolated-Shutdown': info['shutdown_token']})
                self.assertEqual(status, 202)
                process.wait(timeout=10)
                self.assertEqual(process.returncode, 0)
                self.assertTrue((run_dir / 'device_state.json').is_file())
                self.assertTrue((run_dir / 'offload_events.jsonl').is_file())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
            after = {p: hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None for p in protected}
            self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
