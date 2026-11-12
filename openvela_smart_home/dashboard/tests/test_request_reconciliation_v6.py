"""Request recovery against isolated SQLite software entities, without MQTT."""
from contextlib import ExitStack, closing
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
DASH = ROOT / 'openvela_smart_home/dashboard'
NET = ROOT / 'SenseHome-6G/net_offload'
sys.path[:0] = [str(DASH), str(NET)]
from command_requests import RequestJournal
import command_requests
from durable_receipts import SQLiteReceiptExecutor, request_hash
from mqtt_receipts import SoftwareEntityStore
from offload_gateway import BackendResult, OffloadGateway, RuleBackend

ACTION = [{'name': 'light_control', 'params': {'location': '客厅', 'brightness': 60}}]


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        work = ROOT / '_work/intrinsic_v6_fix'
        work.mkdir(parents=True, exist_ok=True)
        self.folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=work)))
        spec = importlib.util.spec_from_file_location('v6_reconciliation_bridge', DASH / 'bridge_server.py')
        self.bridge = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {
            'SENSEHOME_POLICY_CLI': str(self.folder / 'absent-policy-cli'),
            'SENSEHOME_STATE_FILE': str(self.folder / 'state.json'),
            'SENSEHOME_REQUEST_DB': str(self.folder / 'requests.sqlite3'),
            'SENSEHOME_EVENT_LOG': str(self.folder / 'events.jsonl'),
            'SENSEHOME_CLOUD_URL': '', 'SENSEHOME_LOCAL_URL': '', 'SENSEHOME_RECEIPT_DB': '',
        }):
            spec.loader.exec_module(self.bridge)
        self.stack.callback(self.bridge.close_gateway)
        self.bridge.STATE_FILE = str(self.folder / 'state.json')
        self.bridge.REQUEST_DB = str(self.folder / 'requests.sqlite3')
        self.bridge.save_state(self.bridge.default_state())
        self.entity = SoftwareEntityStore(self.folder / 'entity.sqlite3', 'reconciliation-home',
                                         self.bridge.load_state())
        entity = self.entity

        class Transport:
            def __init__(self):
                self.submissions = self.queries = self.snapshots = 0
                self.available = True
                self.lose_ack = False
                self.snapshot_error = False
                self.block_thread = None
                self.entered, self.release = threading.Event(), threading.Event()

            def submit(self, payload):
                self.submissions += 1
                receipt = entity.submit(payload)
                return None if self.lose_ack else receipt

            def lookup(self, command_id):
                self.queries += 1
                return entity.lookup(command_id)

            def snapshot(self):
                self.snapshots += 1
                if threading.current_thread().name == self.block_thread:
                    self.entered.set()
                    if not self.release.wait(5):
                        raise TimeoutError('test did not release delayed lookup')
                    return None
                if self.snapshot_error:
                    raise ConnectionError('isolated unavailable observation')
                return entity.snapshot() if self.available else None

            def connection_state(self):
                return {'connected': True, 'subscribed': True, 'generation': 1}

        self.transport = Transport()
        self.restart()
        self.configure_gateway()
        self.addCleanup(self.stack.close)

    def restart(self):
        self.bridge.request_journal = None
        self.bridge.gateway_receipt_executor = SQLiteReceiptExecutor(
            self.folder / 'receipts.sqlite3', self.transport, platform_id='reconciliation-home')

    def configure_gateway(self, *, model=False):
        class Policy:
            def decide(self, *args):
                return ('cloud-model' if model else 'local-rules'), 'FIXTURE', ''

        class Planner:
            def infer(self, *args):
                return BackendResult(True, False, {}, deepcopy(ACTION), source='cloud-model')

        self.bridge.gateway = OffloadGateway(Policy(), {
            'local-rules': RuleBackend(self.bridge._gateway_local_handler), 'cloud-model': Planner()},
            receipt_executor=self.bridge._gateway_action_executor)
        if model:
            self.bridge.gateway.classify = lambda command: 'complex'

    def test_leave_scene_reconciles_supported_scope_despite_unsupported_security(self):
        self.transport.available = False
        initial = self.bridge.process_http_command('离家模式', {'command_id': 'leave-recover'})
        self.assertEqual(initial['execution_state'], 'applied')
        self.assertFalse(initial['supported_scope_complete'])
        self.assertEqual(initial['request_state'], 'observation_pending')
        self.transport.available = True
        self.restart()
        before = self.transport.snapshots
        result = self.bridge.lookup_http_request('leave-recover')
        self.assertGreater(self.transport.snapshots, before)
        self.assertEqual(result['request_state'], 'finished')
        self.assertTrue(result['supported_scope_complete'])
        self.assertTrue(result['partial_complete'])
        self.assertFalse(result['task_complete'])
        self.assertEqual(result['unsupported_capabilities'], ['security_arming'])
        self.assertEqual(self.transport.submissions, 1)

    def test_delayed_missing_lookup_cannot_revert_a_confirmed_durable_result(self):
        self.transport.lose_ack = True
        initial = self.bridge.process_http_command('把客厅灯光设为60%', {'command_id': 'concurrent-polls'})
        self.assertEqual(initial['request_state'], 'unknown')
        self.transport.block_thread = 'delayed-reconciliation'
        results = {}
        worker = threading.Thread(name=self.transport.block_thread,
            target=lambda: results.setdefault('delayed', self.bridge.lookup_http_request('concurrent-polls')))
        worker.start()
        try:
            self.assertTrue(self.transport.entered.wait(5))
            confirmed = self.bridge.lookup_http_request('concurrent-polls')
            self.assertTrue(confirmed['task_complete'])
        finally:
            self.transport.release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results['delayed']['task_complete'])
        stored = RequestJournal(self.bridge.REQUEST_DB).lookup('concurrent-polls')
        self.assertEqual(stored['request_state'], 'finished')
        self.assertTrue(stored['task_complete'])
        self.assertEqual(stored['receipt'], confirmed['receipt'])
        self.assertEqual(self.transport.submissions, 1)

    def test_newer_device_state_is_reported_separately_from_confirmed_history(self):
        result = self.bridge.process_http_command('把客厅灯光设为60%', {'command_id': 'history'})
        self.assertTrue(result['task_complete'])
        payload = {'command_id': 'other-control', 'platform_id': 'reconciliation-home', 'command': '其他控制',
                   'actions': [{'name': 'light_control', 'params': {'location': '客厅', 'brightness': 0}}]}
        payload['request_sha256'] = request_hash(payload)
        self.entity.submit(payload)
        self.restart()
        current = self.bridge.lookup_http_request('history')
        self.assertTrue(current['task_complete'])
        self.assertIsInstance(current.get('current_goal_acceptance'), dict)
        self.assertFalse(current['current_goal_acceptance']['accepted'])
        self.assertEqual(current['current_observation']['status'], 'targets_unconfirmed')
        self.assertEqual(current['current_observation']['state_version'], 2)
        self.assertEqual(self.transport.submissions, 1)

    def test_unavailable_current_observation_preserves_confirmed_history_after_restart(self):
        confirmed = self.bridge.process_http_command('把客厅灯光设为60%', {'command_id': 'confirmed'})
        self.assertTrue(confirmed['task_complete'])
        self.transport.available = False
        self.restart()
        result = self.bridge.lookup_http_request('confirmed')
        self.assertEqual(result['request_state'], 'finished')
        self.assertTrue(result['task_complete'])
        self.assertEqual(result['goal_acceptance'], confirmed['goal_acceptance'])
        self.assertEqual(result['current_observation']['status'], 'unavailable')
        self.assertEqual(self.transport.submissions, 1)

    def test_cache_error_during_lookup_keeps_applied_receipt_queryable(self):
        self.transport.available = False
        initial = self.bridge.process_http_command('把客厅灯光设为60%', {'command_id': 'lookup-cache'})
        self.transport.available = True
        with patch.object(self.bridge, 'save_state', side_effect=PermissionError('isolated lookup cache failure')):
            pending = self.bridge.lookup_http_request('lookup-cache')
        self.assertEqual(pending['execution_state'], 'applied')
        self.assertEqual(pending['receipt'], initial['receipt'])
        self.assertEqual(pending['request_state'], 'observation_pending')
        self.assertEqual(pending['error'], 'OBSERVATION_PENDING')
        self.assertTrue(self.bridge.lookup_http_request('lookup-cache')['task_complete'])
        self.assertEqual(self.transport.submissions, 1)

    def test_legacy_journal_migrates_and_cas_survives_restart(self):
        path = self.folder / 'legacy.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE requests (command_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, '
                       'command TEXT NOT NULL, context_json TEXT NOT NULL, result_json TEXT, '
                       'created_at REAL NOT NULL, updated_at REAL NOT NULL)')
            db.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?)', ('legacy', 'legacy-hash', '打开客厅灯', '{}',
                       json.dumps({'request_state': 'unknown', 'task_complete': False}), 1., 1.))
        journal = RequestJournal(path)
        old = journal.lookup('legacy')
        self.assertEqual(old.get('result_revision'), 0)
        confirmed = journal.reconcile('legacy', {**old, 'request_state': 'finished',
            'execution_state': 'applied', 'task_complete': True})
        self.assertEqual(confirmed['result_revision'], 1)
        result = RequestJournal(path).reconcile('legacy', old)
        self.assertTrue(result['task_complete'])
        self.assertEqual(result['result_revision'], 1)

    def test_reconcile_releases_sqlite_before_reading_active_request_lock(self):
        journal = self.bridge._get_request_journal()
        with self.assertRaises(RuntimeError):
            journal.execute('打开客厅灯', {'command_id': 'interrupted-lock'},
                            lambda: (_ for _ in ()).throw(RuntimeError('isolated interruption')))
        original = journal.lookup('interrupted-lock')
        reading = threading.Event()
        outcome = {}
        original_result = journal._result

        def result_after_transaction(row, **kwargs):
            if threading.current_thread().name == 'journal-lock-test':
                reading.set()
                with command_requests._guard:
                    pass
            return original_result(row, **kwargs)

        def reconcile():
            outcome['result'] = journal.reconcile('interrupted-lock', original)

        blocked = None
        worker = threading.Thread(name='journal-lock-test', target=reconcile)
        with patch.object(journal, '_result', side_effect=result_after_transaction):
            try:
                with command_requests._guard:
                    worker.start()
                    self.assertTrue(reading.wait(5))
                    try:
                        with closing(sqlite3.connect(journal.path, timeout=.1)) as db, db:
                            db.execute('BEGIN IMMEDIATE')
                    except sqlite3.OperationalError as exc:
                        blocked = str(exc)
            finally:
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertIsNone(blocked, 'reconciliation held SQLite while awaiting the active-request lock')
        self.assertIn('result', outcome)

    def test_post_receipt_observation_errors_recover_without_a_second_submit(self):
        for model in (False, True):
            for failure in ('cache', 'snapshot'):
                with self.subTest(model=model, failure=failure):
                    self.configure_gateway(model=model)
                    command_id = 'recover-%s-%s' % (model, failure)
                    with ExitStack() as failures:
                        if failure == 'cache':
                            failures.enter_context(patch.object(self.bridge, 'save_state',
                                side_effect=PermissionError('isolated UI cache failure')))
                        else:
                            self.transport.snapshot_error = True
                        before = self.transport.submissions
                        try:
                            initial = self.bridge.process_http_command('把客厅灯光设为60%', {'command_id': command_id})
                        finally:
                            self.transport.snapshot_error = False
                    self.assertEqual(initial.get('execution_state'), 'applied')
                    self.assertEqual(initial['receipt']['status'], 'applied')
                    self.assertTrue(initial['actions'])
                    self.assertTrue(initial['execution_complete'])
                    self.assertFalse(initial['task_complete'])
                    self.assertEqual(initial['request_state'], 'observation_pending')
                    self.assertEqual(initial['error'], 'OBSERVATION_PENDING')
                    self.restart()
                    recovered = self.bridge.lookup_http_request(command_id)
                    self.assertEqual(recovered['request_state'], 'finished')
                    self.assertTrue(recovered['task_complete'])
                    self.assertIsNone(recovered.get('observation_error'))
                    self.assertEqual(self.transport.submissions, before + 1)


if __name__ == '__main__':
    unittest.main()
