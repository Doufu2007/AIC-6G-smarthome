"""Application restart checks; all mutable fixtures live beneath _work."""
import http.client
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
from live_v4 import IntegratedSession


class PersistentRuntimeTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / '_work/intrinsic_fixes/persistence_tests'
        parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='case_', dir=parent)
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name) / 'data'

    def start(self, *, mqtt=True):
        session = IntegratedSession(port=0, models=False, mqtt=mqtt,
                                    strategy='local-first', data_dir=self.data_dir)
        worker = threading.Thread(target=session.server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(session.close)
        return session

    def request(self, session, method, path, value=None):
        connection = http.client.HTTPConnection('127.0.0.1', session.server.server_port, timeout=20)
        try:
            connection.request(method, path, json.dumps(value).encode() if value is not None else None,
                               {'Content-Type': 'application/json'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def command(self, session, text, command_id):
        status, result = self.request(session, 'POST', '/api/command',
                                      {'command': text, 'context': {'command_id': command_id}})
        self.assertEqual(status, 200, result)
        return result

    def test_same_data_directory_rejects_second_process_and_unlocks_after_close(self):
        # Removing the OS lock would let the second application open the same DBs.
        first = self.start(mqtt=False)
        attempt = subprocess.run([sys.executable, '-X', 'utf8', str(HERE / 'live_v4.py'),
                                  '--port', '0', '--rules-only', '--json-only',
                                  '--data-dir', str(self.data_dir)],
                                 capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertNotEqual(attempt.returncode, 0)
        self.assertIn('DATA_DIRECTORY_ALREADY_IN_USE', attempt.stderr)
        first.close()
        second = self.start(mqtt=False)
        self.assertNotEqual(first.run_dir, second.run_dir)
        self.assertTrue(second.manifest['persistence']['resumed'])

    def test_unsupported_data_schema_preserves_existing_files(self):
        # Accepting a newer schema would silently reinterpret stored data.
        self.data_dir.mkdir()
        schema = self.data_dir / 'schema.json'
        schema.write_text('{"schema":"sensehome.integrated-data.v1","version":999}', encoding='utf-8')
        state = self.data_dir / 'device_state.json'
        state.write_bytes(b'persisted-state-must-survive')
        before = {p.name: p.read_bytes() for p in (schema, state)}
        with self.assertRaisesRegex(ValueError, 'UNSUPPORTED_DATA_SCHEMA'):
            self.start(mqtt=False)
        self.assertEqual({p.name: p.read_bytes() for p in (schema, state)}, before)
        # A failed initialization must release the lock for an operator retry.
        with schema.open('w', encoding='utf-8') as stream:
            json.dump({'schema': 'sensehome.integrated-data.v1', 'version': 1,
                       'execution_mode': 'pc-json-simulation', 'platform_id': 'software-home'}, stream)
        self.start(mqtt=False).close()

    def test_json_state_survives_complete_application_restart(self):
        # Re-seeding the bridge on every startup would turn this light back off.
        first = self.start(mqtt=False)
        applied = self.command(first, '把客厅灯光设为60%', 'json-persisted')
        self.assertTrue(applied['semantic_complete'], applied)
        first.close()
        second = self.start(mqtt=False)
        state = self.request(second, 'GET', '/api/state')[1]
        self.assertEqual(state['lights']['客厅']['brightness'], 60)
        self.assertTrue((self.data_dir / 'bridge/command_requests.sqlite3').is_file())

    def test_mqtt_state_receipts_and_unknown_survive_complete_application_restart(self):
        # Putting any DB back under run_dir, replaying unknown commands, or
        # trusting an old bridge cache would violate these observable results.
        first = self.start()
        applied = self.command(first, '把客厅灯光设为60%', 'persisted-applied')
        self.assertTrue(applied['semantic_complete'], applied)
        self.assertEqual(self.request(first, 'GET', '/api/receipts/persisted-applied')[1]['execution_state'], 'applied')
        version = first.bridge.gateway_receipt_executor.snapshot()['state_version']
        entity = next(process for role, process, _ in first.mqtt.children if role == 'entity')
        entity.terminate()
        entity.wait(timeout=10)
        unknown = self.command(first, '把客厅灯光设为20%', 'persisted-unknown')
        self.assertFalse(unknown['semantic_complete'], unknown)
        self.assertEqual(self.request(first, 'GET', '/api/receipts/persisted-unknown')[1]['execution_state'], 'unknown')
        first.close()
        cached_state_path = self.data_dir / 'bridge/device_state.json'
        cached_state = json.loads(cached_state_path.read_text(encoding='utf-8'))
        cached_state['lights']['客厅'] = {'is_on': False, 'brightness': 0}
        cached_state_path.write_text(json.dumps(cached_state, ensure_ascii=False), encoding='utf-8')

        second = self.start()
        self.assertNotEqual(first.run_dir, second.run_dir)
        self.assertTrue(second.manifest['persistence']['resumed'])
        state = self.request(second, 'GET', '/api/state')[1]
        self.assertEqual(state['lights']['客厅']['brightness'], 60)
        self.assertEqual(state['_mqtt_observation']['state_version'], version)
        self.assertEqual(self.request(second, 'GET', '/api/receipts/persisted-applied')[1]['execution_state'], 'applied')
        self.assertEqual(self.request(second, 'GET', '/api/receipts/persisted-unknown')[1]['execution_state'], 'unknown')
        self.assertTrue(self.command(second, '把客厅灯光设为60%', 'persisted-applied')['semantic_complete'])
        self.assertFalse(self.command(second, '把客厅灯光设为20%', 'persisted-unknown')['semantic_complete'])
        self.assertEqual(second.bridge.gateway_receipt_executor.snapshot()['state_version'], version)
        with closing(sqlite3.connect(self.data_dir / 'mqtt/gateway.sqlite3')) as db:
            self.assertEqual(dict(db.execute('SELECT command_id, status FROM commands')),
                             {'persisted-applied': 'applied', 'persisted-unknown': 'unknown'})
        with closing(sqlite3.connect(self.data_dir / 'mqtt/entity.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM entity_commands').fetchone()[0], 1)
        self.assertTrue((self.data_dir / 'bridge/command_requests.sqlite3').is_file())
        second.close()
        events = [json.loads(line) for line in (second.run_dir / 'mqtt/entity_events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertFalse(any(row.get('envelope', {}).get('operation') == 'submit' for row in events))
        self.assertTrue(second.manifest['original_state_and_events_unchanged'])


if __name__ == '__main__':
    unittest.main()
