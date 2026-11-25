import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class CareStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.db_path = Path(temporary.name) / 'care.sqlite3'
        self.clock = 100.0

    def now(self):
        return self.clock

    def store(self, timeout=10):
        from care import CareStore

        return CareStore(self.db_path, ack_timeout_seconds=timeout, now=self.now)

    def test_duplicate_event_returns_same_incident_and_notice(self):
        store = self.store()
        incident = store.create_event('sos-1', area_id='bedroom', occurred_at=98)
        self.clock = 101
        duplicate = store.create_event('sos-1', area_id='bedroom', occurred_at=98)

        self.assertEqual(incident, duplicate)
        self.assertEqual(incident['id'], 'sos-1')
        self.assertEqual(incident['source'], 'simulated')
        self.assertEqual(incident['type'], 'sos')
        self.assertEqual(incident['status'], 'pending_ack')
        self.assertEqual(incident['deadline_at'], 110)
        self.assertEqual(incident['occurred_at'], 98)
        self.assertIsNone(incident['acknowledged_at'])
        self.assertEqual(len(incident['notifications']), 1)
        notice = incident['notifications'][0]
        self.assertEqual(notice['status'], 'delivered')
        self.assertEqual(notice['channel'], 'demo_inbox')
        self.assertEqual(notice['kind'], 'initial')

    def test_conflicting_duplicate_is_rejected(self):
        store = self.store()
        expected = store.create_event('sos-1', area_id='bedroom', occurred_at=99)
        with self.assertRaises(ValueError):
            store.create_event('sos-1', area_id='study', occurred_at=99)
        with self.assertRaises(ValueError):
            store.create_event('sos-1', area_id='bedroom', occurred_at=98)
        self.assertEqual(store.get_incident('sos-1'), expected)

    def test_acknowledged_incident_remains_acknowledged_after_deadline(self):
        store = self.store()
        store.create_event('sos-1')
        self.clock = 103
        result = store.acknowledge('sos-1')
        self.clock = 125
        self.assertEqual(store.expire_due(), 0)
        self.assertEqual(store.acknowledge('sos-1'), result)
        self.assertEqual(result['status'], 'acked')
        self.assertEqual(result['acknowledged_at'], 103)
        self.assertEqual([entry['kind'] for entry in result['timeline']],
                         ['sos_received', 'initial_notice', 'acknowledged'])

    def test_restart_keeps_original_deadline_and_expires_only_once(self):
        self.store().create_event('sos-1')
        self.clock = 110
        restarted = self.store(timeout=200)
        self.assertEqual(restarted.expire_due(), 1)
        self.assertEqual(restarted.expire_due(), 0)
        incident = restarted.get_incident('sos-1')
        self.assertEqual(incident['status'], 'escalated')
        self.assertEqual(incident['escalated_at'], 110)
        self.assertEqual([notice['kind'] for notice in incident['notifications']], ['initial', 'escalation'])
        self.assertEqual(restarted.list_incidents(), [incident])

    def test_query_enforces_deadline_without_worker(self):
        self.store().create_event('sos-1')
        self.clock = 110
        incident = self.store().get_incident('sos-1')
        self.assertEqual(incident['status'], 'escalated')
        self.assertIsNone(incident['acknowledged_at'])

    def test_ack_racing_deadline_cannot_overwrite_escalation(self):
        self.store().create_event('sos-1')
        self.clock = 110
        acknowledgement_store, escalation_store = self.store(), self.store()
        ready = threading.Barrier(2)

        def after_barrier(call):
            ready.wait(timeout=2)
            return call()

        with ThreadPoolExecutor(max_workers=2) as executor:
            acknowledgement = executor.submit(after_barrier, lambda: acknowledgement_store.acknowledge('sos-1'))
            escalation = executor.submit(after_barrier, escalation_store.expire_due)
            self.assertEqual(acknowledgement.result(timeout=5)['status'], 'escalated')
            escalation.result(timeout=5)

        incident = self.store().get_incident('sos-1')
        self.assertIsNone(incident['acknowledged_at'])
        self.assertEqual([notice['kind'] for notice in incident['notifications']], ['initial', 'escalation'])

    def test_bad_input_does_not_create_incident(self):
        store = self.store()
        for event_id, area_id, occurred_at in (('../bad', 'bedroom', 99), ('sos-1', ' ', 99),
                                               ('sos-1', 'bedroom', float('inf')), ('sos-1', 'bedroom', True)):
            with self.subTest(event_id=event_id, area_id=area_id, occurred_at=occurred_at):
                with self.assertRaises(ValueError):
                    store.create_event(event_id, area_id=area_id, occurred_at=occurred_at)
        self.assertEqual(store.list_incidents(), [])
        self.assertIsNone(store.acknowledge('missing'))
        self.assertIsNone(store.get_incident('missing'))


if __name__ == '__main__':
    unittest.main()
