"""Product recovery regressions. Synthetic clients; no real HA or hardware."""
import copy
from pathlib import Path
import sys
import subprocess
import json
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from platform_execution import ExecutionStore


class Client:
    def __init__(self):
        self.states = {e: dict(entity_id=e, state='off', attributes={}, last_updated='v0')
                       for e in ('light.a', 'light.b')}
        self.calls = []

    def get_state(self, entity):
        return copy.deepcopy(self.states[entity])

    def call_service(self, entity, service, data):
        self.calls.append((entity, service, copy.deepcopy(data)))
        self.states[entity].update(state='on' if service.endswith('turn_on') else 'off',
                                   last_updated='v' + str(len(self.calls)))
        return []


def plan(*entities):
    return [dict(entity_id=e, service='light.turn_on', data={}) for e in entities]


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'execution.sqlite'
        self.client = Client()
        self.clock = [100.0]

    def tearDown(self):
        self.temp.cleanup()

    def store(self, **kwargs):
        return ExecutionStore(self.path, self.client, now=lambda: self.clock[0], **kwargs)

    def test_bounded_polling_completes_normal_asynchronous_device(self):
        original = self.client.get_state
        normal = self.client.call_service
        pending = []
        def enqueue(entity, service, data):
            pending.append((entity, service, data))
        def read(entity):
            if pending:
                entity_id, service, data = pending.pop(0)
                # First GET may still return pre-operation state.
                before = original(entity)
                normal(entity_id, service, data)
                return before
            return original(entity)
        self.client.call_service = enqueue
        self.client.get_state = read
        store = self.store()
        result = store.execute('async', plan('light.a', 'light.b'),
                               store.snapshot(['light.a', 'light.b']), authorized=True)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual([e[0] for e in self.client.calls], ['light.a', 'light.b'])

    def test_reconcile_continues_only_never_dispatched_remainder(self):
        normal = self.client.call_service
        pending = []
        def enqueue(entity, service, data):
            if entity == 'light.a':
                self.client.calls.append((entity, service, data))
                pending.append(entity)
            else:
                normal(entity, service, data)
        self.client.call_service = enqueue
        store = self.store()
        initial = store.execute('resume', plan('light.a', 'light.b'),
                                store.snapshot(['light.a', 'light.b']), authorized=True)
        self.assertEqual(initial['status'], 'unknown')
        self.client.states['light.a'].update(state='on', last_updated='settled')
        after = self.store().reconcile('resume')
        self.assertEqual(after['status'], 'verified_after_unknown')
        self.assertEqual([x[0] for x in self.client.calls], ['light.a', 'light.b'])
        self.assertEqual(self.client.states['light.b']['state'], 'on')

    def test_restart_after_first_dispatch_resumes_without_redispatch(self):
        normal = self.client.call_service
        crashed = [False]
        def crash(entity, service, data):
            normal(entity, service, data)
            if not crashed[0]:
                crashed[0] = True
                raise KeyboardInterrupt('simulated process interruption')
        self.client.call_service = crash
        store = self.store()
        with self.assertRaises(KeyboardInterrupt):
            store.execute('crash', plan('light.a', 'light.b'),
                          store.snapshot(['light.a', 'light.b']), authorized=True)
        result = self.store().reconcile('crash')
        self.assertEqual(result['status'], 'verified_after_unknown')
        self.assertEqual([x[0] for x in self.client.calls], ['light.a', 'light.b'])

    def test_reconcile_never_overwrites_an_active_executor_success(self):
        entered, release = threading.Event(), threading.Event()
        normal = self.client.call_service
        def blocked(entity, service, data):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('barrier timed out')
            return normal(entity, service, data)
        self.client.call_service = blocked
        executor, reconciler = self.store(), self.store()
        expected = executor.snapshot(['light.a'])
        outcomes = []
        thread = threading.Thread(target=lambda: outcomes.append(
            executor.execute('race', plan('light.a'), expected, authorized=True)))
        thread.start()
        self.assertTrue(entered.wait(3))
        try:
            interim = reconciler.reconcile('race')
            self.assertEqual(interim.get('reason'), 'execution_in_progress')
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes[0]['status'], 'succeeded')
        self.assertEqual(reconciler.get_receipt('race')['status'], 'succeeded')

    def test_competing_requests_cannot_dispatch_same_entity_concurrently(self):
        entered, release = threading.Event(), threading.Event()
        normal = self.client.call_service
        count = [0]
        def blocked_first(entity, service, data):
            count[0] += 1
            if count[0] == 1:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('barrier timed out')
            return normal(entity, service, data)
        self.client.call_service = blocked_first
        first, second = self.store(), self.store()
        expected = first.snapshot(['light.a'])
        outcomes = []
        thread = threading.Thread(target=lambda: outcomes.append(
            first.execute('first', plan('light.a'), expected, authorized=True)))
        thread.start()
        self.assertTrue(entered.wait(3))
        try:
            other = second.execute('second', plan('light.a'), expected, authorized=True)
            self.assertEqual(other['status'], 'rejected')
            self.assertEqual(other['reason'], 'entity_busy')
        finally:
            release.set()
            thread.join(3)
        self.assertEqual(len(self.client.calls), 1)

    def test_expired_authorization_does_not_continue_remainder(self):
        normal = self.client.call_service
        def crash(entity, service, data):
            normal(entity, service, data)
            raise KeyboardInterrupt('interrupted')
        self.client.call_service = crash
        store = self.store()
        with self.assertRaises(KeyboardInterrupt):
            store.execute('expired', plan('light.a', 'light.b'),
                          store.snapshot(['light.a', 'light.b']), authorized=True)
        self.clock[0] += 31
        result = self.store().reconcile('expired')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(len(self.client.calls), 1)
        self.assertEqual(result['steps'][1]['status'], 'not_dispatched')

    def test_explicit_unsupported_capability_rejects_before_service(self):
        self.client.states['light.a']['attributes']['brightness_supported'] = False
        store = self.store()
        action = [dict(entity_id='light.a', service='light.turn_on', data={'brightness_pct': 50})]
        result = store.execute('unsupported', action, store.snapshot(['light.a']), authorized=True)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['reason'], 'unsupported_capability')
        self.assertEqual(self.client.calls, [])

    def test_interruption_after_request_creation_does_not_false_succeed(self):
        store = self.store()
        expected = store.snapshot(['light.a', 'light.b'])
        def crash(_request_id):
            raise KeyboardInterrupt('before executor claim')
        store._claim = crash
        with self.assertRaises(KeyboardInterrupt):
            store.execute('creation', plan('light.a', 'light.b'), expected, authorized=True)
        result = self.store().reconcile('creation')
        self.assertEqual(result['status'], 'verified_after_unknown')
        self.assertEqual([x[0] for x in self.client.calls], ['light.a', 'light.b'])

    def test_cross_process_reconcile_cannot_read_during_active_dispatch(self):
        entered, release = threading.Event(), threading.Event()
        normal = self.client.call_service
        def blocked(entity, service, data):
            entered.set()
            if not release.wait(5): raise RuntimeError('barrier timeout')
            return normal(entity, service, data)
        self.client.call_service = blocked
        store = self.store()
        expected = store.snapshot(['light.a'])
        outcomes = []
        worker = threading.Thread(target=lambda: outcomes.append(
            store.execute('process-lock', plan('light.a'), expected, authorized=True)))
        worker.start()
        self.assertTrue(entered.wait(3))
        script = ('import sys,json;sys.path.insert(0,sys.argv[1]);'
                  'from platform_execution import ExecutionStore;'
                  'print(json.dumps(ExecutionStore(sys.argv[2],object()).reconcile("process-lock")))')
        try:
            process = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', script,
                                      str(Path(__file__).resolve().parents[1]), str(self.path)],
                                     capture_output=True, text=True, timeout=4)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout)['reason'], 'execution_in_progress')
        finally:
            release.set()
            worker.join(3)
        self.assertEqual(outcomes[0]['status'], 'succeeded')
        self.assertEqual(len(self.client.calls), 1)

    def test_original_card_expiry_can_be_stricter_than_snapshot_ttl(self):
        normal = self.client.call_service
        def crash(entity, service, data):
            normal(entity, service, data)
            raise KeyboardInterrupt('interrupted')
        self.client.call_service = crash
        store = self.store()
        with self.assertRaises(KeyboardInterrupt):
            store.execute('card-ttl', plan('light.a', 'light.b'), store.snapshot(['light.a', 'light.b']),
                          authorized=True, authorization_expires_at=102)
        self.clock[0] = 103
        result = self.store().reconcile('card-ttl')
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('authorization_expired', result['reason'])
        self.assertEqual(len(self.client.calls), 1)

    def test_expired_partial_request_releases_proven_safe_entity_reservations(self):
        normal = self.client.call_service
        def crash(entity, service, data):
            normal(entity, service, data)
            raise KeyboardInterrupt('interrupted')
        self.client.call_service = crash
        store = self.store()
        with self.assertRaises(KeyboardInterrupt):
            store.execute('old', plan('light.a', 'light.b'), store.snapshot(['light.a', 'light.b']),
                          authorized=True, authorization_expires_at=102)
        self.clock[0] = 103
        self.assertEqual(self.store().reconcile('old')['status'], 'unknown')
        self.client.call_service = normal
        fresh = self.store()
        result = fresh.execute('new-consent', plan('light.b'), fresh.snapshot(['light.b']), authorized=True)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual([x[0] for x in self.client.calls], ['light.a', 'light.b'])

    def test_unknown_entity_stays_reserved_but_never_sent_entity_can_be_reused(self):
        normal = self.client.call_service
        def never_settle(entity, service, data):
            self.client.calls.append((entity, service, data))
        self.client.call_service = never_settle
        first = self.store()
        self.assertEqual(first.execute('unknown-first', plan('light.a', 'light.b'),
                         first.snapshot(['light.a', 'light.b']), authorized=True)['status'], 'unknown')
        self.client.call_service = normal
        fresh = self.store()
        a = fresh.execute('unsafe-overlap', plan('light.a'), fresh.snapshot(['light.a']), authorized=True)
        self.assertEqual(a.get('reason'), 'entity_busy')
        b = fresh.execute('safe-new', plan('light.b'), fresh.snapshot(['light.b']), authorized=True)
        self.assertEqual(b['status'], 'succeeded')

    def test_local_validation_that_expires_consent_cannot_post(self):
        def slow_validation(entity, service, data, state=None):
            self.clock[0] += 3
        self.client.validate_action = slow_validation
        store = self.store()
        result = store.execute('slow-validation', plan('light.a'), store.snapshot(['light.a']),
                               authorized=True, authorization_expires_at=102)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.client.calls, [])

    def test_hard_process_exit_releases_lock_and_recovery_never_resends_first_step(self):
        state_file = Path(self.temp.name) / 'states.json'
        state_file.write_text(json.dumps(self.client.states), encoding='utf8')
        script = '''import sys,json,os
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from platform_execution import ExecutionStore
class CrashClient:
    def get_state(self,e):return json.loads(Path(sys.argv[3]).read_text(encoding='utf8'))[e]
    def call_service(self,e,s,d):
        state=json.loads(Path(sys.argv[3]).read_text(encoding='utf8'))
        state[e].update(state='on',last_updated='child-applied')
        Path(sys.argv[3]).write_text(json.dumps(state),encoding='utf8')
        os._exit(23)
store=ExecutionStore(sys.argv[2],CrashClient(),now=lambda:100)
entities=['light.a','light.b']
store.execute('hard-exit',[dict(entity_id=e,service='light.turn_on',data={}) for e in entities],store.snapshot(entities),authorized=True)
'''
        process = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', script,
            str(Path(__file__).resolve().parents[1]), str(self.path), str(state_file)],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(process.returncode, 23, process.stderr)
        self.client.states = json.loads(state_file.read_text(encoding='utf8'))
        result = self.store().reconcile('hard-exit')
        self.assertEqual(result['status'], 'verified_after_unknown')
        self.assertEqual([x[0] for x in self.client.calls], ['light.b'])
        self.assertEqual(self.client.states['light.a']['last_updated'], 'child-applied')


if __name__ == '__main__':
    unittest.main(verbosity=2)
