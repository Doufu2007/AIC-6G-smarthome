"""Controlled local recovery; synthetic states are not remote cancellation evidence."""
import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from platform_execution import ExecutionStore, HAClient, UnknownEntity


class Client:
    def __init__(self):
        self.states = {e: dict(entity_id=e, state='off', attributes={}, last_updated='v0')
                       for e in ('light.a', 'light.b')}
        self.calls = []
        self.reads = []
        self.apply = False

    def get_state(self, entity):
        self.reads.append(entity)
        return copy.deepcopy(self.states[entity])

    def call_service(self, entity, service, data):
        self.calls.append((entity, service, copy.deepcopy(data)))
        if self.apply:
            self.states[entity].update(state='on', last_updated='v' + str(len(self.calls)))
        return []


def plan(*entities):
    return [dict(entity_id=e, service='light.turn_on', data={}) for e in entities]


class OperatorResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'execution.sqlite'
        self.client = Client()
        self.store = self.reopen()

    def tearDown(self):
        self.temp.cleanup()

    def reopen(self):
        return ExecutionStore(self.path, self.client, now=lambda: 100.0,
                              verify_timeout_seconds=0)

    def execute(self, request_id, *entities, store=None, authorized=True):
        store = store or self.store
        return store.execute(request_id, plan(*entities), store.snapshot(entities),
                             authorized=authorized)

    def unknown(self, request_id='old', *entities):
        result = self.execute(request_id, *(entities or ('light.a',)))
        self.assertEqual(result['status'], 'unknown')
        return result

    def resolve(self, request_id='old', store=None, **kwargs):
        store = store or self.store
        self.assertTrue(callable(getattr(store, 'resolve_unknown', None)),
                        'ExecutionStore needs the controlled unknown-resolution API')
        values = dict(operator='local-operator', reason='Checked the platform activity',
                      disposition='verified_settled', platform_settled=True)
        values.update(kwargs)
        return store.resolve_unknown(request_id, **values)

    def owners(self):
        with closing(sqlite3.connect(self.path)) as db:
            return dict(db.execute('SELECT entity_id,request_id FROM entity_owners'))

    def test_verified_settled_preserves_unknown_history_and_persists_operator_record(self):
        original = self.unknown()
        with closing(sqlite3.connect(self.path)) as db:
            original_result = db.execute('SELECT result FROM requests WHERE request_id=?', ('old',)).fetchone()[0]
        traffic = (list(self.client.calls), list(self.client.reads))
        result = self.resolve()
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['reason'], original['reason'])
        self.assertEqual(result['steps'], original['steps'])
        record = result['operator_resolution']
        self.assertEqual(record['operator'], 'local-operator')
        self.assertEqual(record['reason'], 'Checked the platform activity')
        self.assertEqual(record['disposition'], 'verified_settled')
        self.assertIs(record['platform_settled'], True)
        self.assertIs(record['acknowledge_late_risk'], False)
        self.assertIs(record['remote_cancelled'], False)
        self.assertEqual(record['resolved_at'], 100.0)
        self.assertEqual(record['released_entities'], ['light.a'])
        self.assertEqual((self.client.calls, self.client.reads), traffic)
        self.assertEqual(self.reopen().get_receipt('old'), result)
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute('SELECT status,result FROM requests WHERE request_id=?', ('old',)).fetchone()
        self.assertEqual(row, ('unknown', original_result))

    def test_release_allows_only_a_new_explicitly_authorized_request(self):
        self.unknown()
        self.resolve()
        self.client.apply = True
        denied = self.execute('without-consent', 'light.a', authorized=False)
        self.assertEqual(denied['reason'], 'authorization_required')
        self.assertEqual(len(self.client.calls), 1)
        result = self.execute('new-consent', 'light.a', store=self.reopen())
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.store.get_receipt('old')['status'], 'unknown')

    def test_dispositions_require_literal_platform_or_late_risk_confirmation(self):
        self.unknown()
        for value in (False, None, 1, 'true'):
            with self.subTest(disposition='verified_settled', value=value):
                result = self.resolve(platform_settled=value)
                self.assertEqual(result['status'], 'rejected')
                self.assertEqual(result['reason'], 'platform_settled_confirmation_required')
                self.assertEqual(self.owners(), {'light.a': 'old'})
            with self.subTest(disposition='accept_late_risk', value=value):
                result = self.resolve(disposition='accept_late_risk', platform_settled=False,
                                      acknowledge_late_risk=value)
                self.assertEqual(result['status'], 'rejected')
                self.assertEqual(result['reason'], 'late_risk_confirmation_required')
                self.assertEqual(self.owners(), {'light.a': 'old'})
        result = self.resolve(disposition='accept_late_risk', platform_settled=False,
                              acknowledge_late_risk=True)
        self.assertEqual(result['status'], 'unknown')
        self.assertIs(result['operator_resolution']['acknowledge_late_risk'], True)
        self.assertIs(result['operator_resolution']['platform_settled'], False)
        self.assertEqual(self.owners(), {})

    def test_missing_identity_reason_or_valid_disposition_cannot_release(self):
        self.unknown()
        invalid = [({'operator': ''}, 'invalid_operator'),
                   ({'operator': '  '}, 'invalid_operator'),
                   ({'operator': None}, 'invalid_operator'),
                   ({'reason': ''}, 'invalid_resolution_reason'),
                   ({'reason': '  '}, 'invalid_resolution_reason'),
                   ({'reason': 12}, 'invalid_resolution_reason'),
                   ({'disposition': 'cancelled'}, 'invalid_disposition')]
        for kwargs, reason in invalid:
            with self.subTest(kwargs=kwargs):
                result = self.resolve(**kwargs)
                self.assertEqual((result['status'], result['reason']), ('rejected', reason))
                self.assertEqual(self.owners(), {'light.a': 'old'})
                self.assertNotIn('operator_resolution', self.store.get_receipt('old'))

    def test_unknown_id_invalid_id_and_completed_request_cannot_be_resolved(self):
        self.assertEqual(self.resolve('missing')['reason'], 'unknown_request_id')
        self.assertEqual(self.resolve(' ')['reason'], 'invalid_request_id')
        self.client.apply = True
        self.assertEqual(self.execute('success', 'light.a')['status'], 'succeeded')
        result = self.resolve('success')
        self.assertEqual((result['status'], result['reason']),
                         ('rejected', 'request_not_unknown'))
        self.assertEqual(self.store.get_receipt('success')['status'], 'succeeded')
        self.execute('denied', 'light.b', authorized=False)
        self.assertEqual(self.resolve('denied')['reason'], 'unknown_request_id')

    def test_active_dispatch_cannot_be_resolved_in_this_or_another_process(self):
        entered, release = threading.Event(), threading.Event()
        normal = self.client.call_service
        def blocked(entity, service, data):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test barrier timed out')
            return normal(entity, service, data)
        self.client.call_service = blocked
        self.client.apply = True
        outcomes = []
        thread = threading.Thread(target=lambda: outcomes.append(self.execute('active', 'light.a')))
        thread.start()
        self.assertTrue(entered.wait(3))
        try:
            result = self.resolve('active', store=self.reopen())
            self.assertEqual((result['status'], result['reason']),
                             ('rejected', 'execution_in_progress'))
            script = ('import sys,json;sys.path.insert(0,sys.argv[1]);'
                      'from platform_execution import ExecutionStore;'
                      'print(json.dumps(ExecutionStore(sys.argv[2],object()).resolve_unknown('
                      '"active",operator="other-process",reason="checked",'
                      'disposition="accept_late_risk",acknowledge_late_risk=True)))')
            process = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', script,
                                      str(Path(__file__).resolve().parents[1]), str(self.path)],
                                     capture_output=True, text=True, timeout=3)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout)['reason'], 'execution_in_progress')
            self.assertEqual(self.owners(), {'light.a': 'active'})
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes[0]['status'], 'succeeded')
        self.assertNotIn('operator_resolution', self.store.get_receipt('active'))

    def test_active_reconciliation_of_unknown_cannot_be_resolved(self):
        self.unknown()
        entered, release = threading.Event(), threading.Event()
        normal = self.client.get_state
        def blocked(entity):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test barrier timed out')
            return normal(entity)
        self.client.get_state = blocked
        outcomes = []
        thread = threading.Thread(target=lambda: outcomes.append(self.reopen().reconcile('old')))
        thread.start()
        self.assertTrue(entered.wait(3))
        try:
            result = self.resolve(store=self.reopen())
            self.assertEqual((result['status'], result['reason']),
                             ('rejected', 'execution_in_progress'))
            self.assertEqual(self.owners(), {'light.a': 'old'})
        finally:
            release.set()
            thread.join(3)
        self.assertEqual(outcomes[0]['status'], 'unknown')
        self.assertNotIn('operator_resolution', self.store.get_receipt('old'))

    def test_resolved_partial_plan_cannot_reconcile_reclaim_or_dispatch_remainder(self):
        self.unknown('old', 'light.a', 'light.b')
        self.unknown('other', 'light.b')
        result = self.resolve()
        self.assertEqual(result['operator_resolution']['released_entities'], ['light.a'])
        self.assertEqual(self.owners(), {'light.b': 'other'})
        # A late first action now appears applied; normal recovery would try to continue.
        self.client.states['light.a'].update(state='on', last_updated='late-apply')
        self.client.apply = True
        traffic = (list(self.client.calls), list(self.client.reads))
        recovered = self.reopen().reconcile('old')
        self.assertEqual(recovered['status'], 'unknown')
        self.assertEqual(recovered['steps'][1]['status'], 'not_dispatched')
        self.assertEqual(recovered['operator_resolution'], result['operator_resolution'])
        self.assertEqual((self.client.calls, self.client.reads), traffic)
        self.assertEqual(self.owners(), {'light.b': 'other'})
        self.assertEqual(self.execute('new-a', 'light.a')['status'], 'succeeded')
        self.assertEqual(self.execute('blocked-b', 'light.b')['reason'], 'entity_busy')
        self.assertEqual(self.owners(), {'light.b': 'other'})

    def test_same_request_execute_is_read_only_after_resolution(self):
        self.unknown()
        # Keep the original expected map: retrying the same request must return its journal.
        with closing(sqlite3.connect(self.path)) as db:
            expected = json.loads(db.execute('SELECT expected FROM requests WHERE request_id=?', ('old',)).fetchone()[0])
        resolved = self.resolve()
        self.client.apply = True
        traffic = (list(self.client.calls), list(self.client.reads))
        result = self.reopen().execute('old', plan('light.a'), expected, authorized=True)
        self.assertEqual(result['status'], 'unknown')
        self.assertIs(result['duplicate'], True)
        self.assertEqual(result['operator_resolution'], resolved['operator_resolution'])
        self.assertEqual((self.client.calls, self.client.reads), traffic)
        self.assertEqual(self.owners(), {})

    def test_repeated_resolution_cannot_overwrite_first_operator_or_clear_new_owner(self):
        self.unknown()
        first = self.resolve()
        self.unknown('new-owner', 'light.a')
        repeated = self.resolve(operator='replacement', reason='Different account and reason',
                                disposition='accept_late_risk', acknowledge_late_risk=True)
        self.assertEqual(repeated['operator_resolution'], first['operator_resolution'])
        self.assertIs(repeated['duplicate'], True)
        self.assertEqual(self.owners(), {'light.a': 'new-owner'})
        self.assertEqual(self.reopen().get_receipt('old')['operator_resolution'], first['operator_resolution'])

    def test_concurrent_operators_leave_one_immutable_resolution(self):
        self.unknown()
        start = threading.Barrier(2)
        results, errors = [], []
        def resolve(operator):
            try:
                store = self.reopen()
                start.wait(3)
                results.append(self.resolve(operator=operator, store=store))
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=resolve, args=(name,)) for name in ('operator-a', 'operator-b')]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertEqual(errors, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        record = self.reopen().get_receipt('old')['operator_resolution']
        self.assertIn(record['operator'], ('operator-a', 'operator-b'))
        successes = [r for r in results if r['status'] == 'unknown']
        self.assertGreaterEqual(len(successes), 1)
        for result in successes:
            self.assertEqual(result['operator_resolution'], record)
        for result in results:
            if result['status'] == 'rejected':
                self.assertEqual(result['reason'], 'execution_in_progress')
        self.assertEqual(self.owners(), {})
        self.assertEqual(len(self.client.calls), 1)

    def test_hard_exit_inflight_can_be_resolved_without_reconcile_or_platform_traffic(self):
        script = '''import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from platform_execution import ExecutionStore
class CrashClient:
    def get_state(self,entity):
        return dict(entity_id=entity,state='off',attributes={},last_updated='v0')
    def call_service(self,entity,service,data):
        Path(sys.argv[3]).write_text(entity,encoding='utf8')
        os._exit(23)
store=ExecutionStore(sys.argv[2],CrashClient(),now=lambda:100,verify_timeout_seconds=0)
if sys.argv[5]=='before_claim':
    store._claim=lambda request_id:os._exit(24)
entities=['light.a','light.b']
store.execute(sys.argv[4],[dict(entity_id=e,service='light.turn_on',data={}) for e in entities],
              store.snapshot(entities),authorized=True)
'''
        for checkpoint, status, code in [('before_claim', 'preparing', 24),
                                         ('during_send', 'dispatching', 23)]:
            with self.subTest(checkpoint=checkpoint):
                path = Path(self.temp.name) / (checkpoint + '.sqlite')
                call_file = Path(self.temp.name) / (checkpoint + '.sent')
                store = ExecutionStore(path, self.client, now=lambda: 100.0,
                                       verify_timeout_seconds=0)
                process = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', script,
                    str(Path(__file__).resolve().parents[1]), str(path), str(call_file), checkpoint, checkpoint],
                    capture_output=True, text=True, timeout=4)
                self.assertEqual(process.returncode, code, process.stderr)
                original = store.get_receipt(checkpoint)
                self.assertEqual(original['status'], 'unknown')
                with closing(sqlite3.connect(path)) as db:
                    before = db.execute('SELECT status,result FROM requests WHERE request_id=?', (checkpoint,)).fetchone()
                    steps_before = db.execute('SELECT status FROM steps ORDER BY ordinal').fetchall()
                self.assertEqual(before, (status, None))
                self.assertEqual(call_file.exists(), checkpoint == 'during_send')
                traffic = (list(self.client.calls), list(self.client.reads))
                result = self.resolve(checkpoint, store=store)
                self.assertEqual(result['status'], 'unknown')
                for key, value in original.items():
                    self.assertEqual(result[key], value)
                self.assertEqual(result['operator_resolution']['released_entities'], ['light.a', 'light.b'])
                reopened = ExecutionStore(path, self.client, now=lambda: 100.0,
                                          verify_timeout_seconds=0)
                self.assertEqual(reopened.reconcile(checkpoint)['operator_resolution'], result['operator_resolution'])
                self.assertEqual((self.client.calls, self.client.reads), traffic)
                with closing(sqlite3.connect(path)) as db:
                    after = db.execute('SELECT status,result FROM requests WHERE request_id=?', (checkpoint,)).fetchone()
                    steps_after = db.execute('SELECT status FROM steps ORDER BY ordinal').fetchall()
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM entity_owners').fetchone()[0], 0)
                self.assertEqual(after, before)
                self.assertEqual(steps_after, steps_before)
                self.client.apply = True
                fresh = reopened.execute('fresh-' + checkpoint, plan('light.a', 'light.b'),
                    reopened.snapshot(['light.a', 'light.b']), authorized=True)
                self.assertEqual(fresh['status'], 'succeeded')


class HAAuditIsolationTests(unittest.TestCase):
    def setUp(self):
        # Reuse only the real localhost API fixture, not its existing tests.
        from test_platform_execution import Fixture
        self.fixture = Fixture()
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.blocked = self.path / 'log-is-a-directory'
        self.blocked.mkdir()

    def tearDown(self):
        self.fixture.close()
        self.temp.cleanup()

    def audit(self, client):
        self.assertTrue(hasattr(client, 'last_audit_status'),
                        'HA audit outcome must be independent of the API outcome')
        return client.last_audit_status

    def test_failed_ha_audit_does_not_hide_successful_get_or_post(self):
        client = HAClient(self.fixture.url, 'fixture-token', audit_path=self.blocked)
        try:
            before = client.get_state('input_boolean.living')
            response = client.call_service('input_boolean.living', 'input_boolean.turn_on', {})
            after = client.get_state('input_boolean.living')
        except Exception as exc:
            self.fail('supplemental HA audit masked successful HTTP: ' + type(exc).__name__)
        self.assertEqual(before['state'], 'off')
        self.assertEqual(response, [])
        self.assertEqual(after['state'], 'on')
        status = self.audit(client)
        self.assertEqual(status['status'], 'failed')
        self.assertIn(status['error_type'], ('PermissionError', 'IsADirectoryError'))
        self.assertNotIn('fixture-token', json.dumps(status))
        self.assertEqual(len([c for c in self.fixture.calls if c[0] == 'POST']), 1)

    def test_post_audit_failure_cannot_downgrade_durable_success(self):
        broken_path = self.blocked
        class PostAuditBreakClient(HAClient):
            def call_service(self, entity_id, service, data):
                self.audit_path = broken_path
                return super().call_service(entity_id, service, data)
        client = PostAuditBreakClient(self.fixture.url, 'fixture-token',
                                     audit_path=self.path / 'initially-writable.jsonl')
        store = ExecutionStore(self.path / 'execution.sqlite', client, now=lambda: 100,
                               verify_timeout_seconds=0)
        action = [dict(entity_id='input_boolean.living', service='input_boolean.turn_on', data={})]
        expected = store.snapshot(['input_boolean.living'])
        result = store.execute('audit-independent', action, expected, authorized=True)
        self.assertEqual(self.fixture.states['input_boolean.living']['state'], 'on')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(store.get_receipt('audit-independent')['status'], 'succeeded')
        self.assertEqual(self.audit(client)['status'], 'failed')
        self.assertEqual(store.execute('audit-independent', action, expected, authorized=True)['status'], 'succeeded')
        self.assertEqual(len([c for c in self.fixture.calls if c[0] == 'POST']), 1)

    def test_failed_ha_audit_does_not_replace_original_platform_error(self):
        client = HAClient(self.fixture.url, 'fixture-token', audit_path=self.blocked)
        try:
            client.get_state('input_boolean.absent')
        except Exception as exc:
            self.assertIsInstance(exc, UnknownEntity, 'audit must not replace the original API error')
        else:
            self.fail('missing entity must still raise UnknownEntity')
        self.assertEqual(self.audit(client)['status'], 'failed')

    def test_ha_audit_redacts_token_and_keeps_thread_local_outcomes(self):
        log = self.path / 'redacted.jsonl'
        client = HAClient(self.fixture.url, 'fixture-token', audit_path=log)
        self.fixture.states['input_boolean.living']['attributes']['note'] = 'fixture-token'
        actual = client.get_state('input_boolean.living')
        self.assertEqual(actual['attributes']['note'], 'fixture-token')
        self.assertEqual(self.audit(client)['status'], 'written')
        text = log.read_text(encoding='utf8')
        self.assertNotIn('fixture-token', text)
        self.assertEqual(json.loads(text)['request_headers']['Authorization'], '<REDACTED>')
        outcomes, errors = [], []
        def failed_worker():
            try:
                client.audit_path = self.blocked
                client.get_state('input_boolean.living')
                outcomes.append(self.audit(client))
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=failed_worker)
        thread.start()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(outcomes[0]['status'], 'failed')
        self.assertEqual(self.audit(client)['status'], 'written')

    def test_concurrent_ha_audit_records_are_complete_and_redacted(self):
        log = self.path / 'concurrent.jsonl'
        client = HAClient(self.fixture.url, 'fixture-token', audit_path=log)
        self.fixture.states['input_boolean.living']['attributes']['note'] = 'fixture-token' * 2000
        start = threading.Barrier(6)
        errors, statuses = [], []
        def read():
            try:
                start.wait(3)
                for _ in range(2):
                    self.assertEqual(client.get_state('input_boolean.living')['state'], 'off')
                    statuses.append(self.audit(client)['status'])
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=read) for _ in range(6)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(statuses, ['written'] * 12)
        text = log.read_text(encoding='utf8')
        self.assertNotIn('fixture-token', text)
        records = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(len(records), 12)
        self.assertTrue(all(record['status'] == 200 for record in records))


if __name__ == '__main__':
    unittest.main(verbosity=2)
