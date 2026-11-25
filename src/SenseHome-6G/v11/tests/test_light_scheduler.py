from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from cli import DEFAULT_CATALOG, DEFAULT_STATES
from controller import Controller, SoftwareClient
from scheduler import LightScheduler


class LightSchedulerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'schedules.sqlite'
        self.client = SoftwareClient(DEFAULT_STATES)
        self.controller = Controller(directory.name, DEFAULT_CATALOG, self.client)
        self.now = 1000.0
        self.scheduler = self.reopen()

    def reopen(self):
        return LightScheduler(self.path, self.controller, now=lambda: self.now)

    def prepare(self):
        return self.scheduler.prepare('light.reading', 'light.turn_on', 10)

    def test_confirmation_and_persistence(self):
        draft = self.prepare()
        self.now += 2
        self.assertEqual(self.scheduler.run_due(), [])
        with self.assertRaises(ValueError):
            self.scheduler.confirm(draft['schedule_id'], 'invalid')
        task = self.scheduler.confirm(draft['schedule_id'], draft['digest'])
        self.assertEqual(task['execute_at'], 1012)
        self.now = 1011
        self.assertEqual(self.reopen().run_due(), [])
        self.now = 1012
        finished, = self.reopen().run_due()
        self.assertEqual(finished['status'], 'succeeded')
        self.assertEqual(finished['receipt']['steps'][0]['status'], 'verified')
        self.assertEqual(self.client.get_state('light.reading')['state'], 'on')
        self.assertEqual(self.reopen().run_due(), [])
        self.assertEqual(self.client.calls, 1)

    def test_cancellation_prevents_dispatch(self):
        task = self.prepare()
        self.scheduler.confirm(task['schedule_id'], task['digest'])
        self.assertEqual(self.scheduler.cancel(task['schedule_id'])['status'], 'cancelled')
        self.now += 10
        self.assertEqual(self.scheduler.run_due(), [])
        self.assertEqual(self.client.calls, 0)

    def test_competing_workers_dispatch_once(self):
        task = self.prepare()
        self.scheduler.confirm(task['schedule_id'], task['digest'])
        worker = self.reopen()
        self.now += 10
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda scheduler: scheduler.run_due(), [worker, self.scheduler]))
        self.assertEqual(sum(map(len, responses)), 1)
        self.assertEqual(self.client.calls, 1)

    def test_interrupted_dispatch_is_not_retried(self):
        task = self.prepare()
        self.scheduler.confirm(task['schedule_id'], task['digest'])
        self.now += 10
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE light_schedules SET status='running' WHERE schedule_id=?",
                               (task['schedule_id'],))
        recovered, = self.reopen().run_due()
        self.assertEqual(recovered['status'], 'unknown')
        self.assertEqual(self.client.calls, 0)

    def test_missed_action_expires_after_restart(self):
        task = self.prepare()
        self.scheduler.confirm(task['schedule_id'], task['digest'])
        self.now += 21
        finished, = self.reopen().run_due()
        self.assertEqual(finished['status'], 'expired')
        self.assertEqual(self.client.calls, 0)

    def test_changed_entity_configuration_rejects_approved_task(self):
        task = self.prepare()
        self.scheduler.confirm(task['schedule_id'], task['digest'])
        self.controller.catalog[0]['room'] = '门口'
        self.now += 10
        finished, = self.reopen().run_due()
        self.assertEqual(finished['status'], 'rejected')
        self.assertEqual(self.client.calls, 0)

    def test_restart_reuses_readback_verified_receipt(self):
        task = self.prepare()
        self.scheduler.confirm(task['schedule_id'], task['digest'])
        self.now += 10
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE light_schedules SET status='running' WHERE schedule_id=?",
                               (task['schedule_id'],))
        self.controller.receipt = lambda request_id: {'status': 'verified_after_unknown', 'request_id': request_id}
        recovered, = self.reopen().run_due()
        self.assertEqual(recovered['status'], 'verified_after_unknown')
        self.assertEqual(self.client.calls, 0)

    def test_rejects_non_light_or_delays_outside_ten_seconds(self):
        for entity, service, delay in [('cover.study', 'cover.open_cover', 10),
                                       ('light.missing', 'light.turn_on', 10),
                                       ('light.reading', 'light.turn_on', 5)]:
            with self.subTest(entity=entity, service=service, delay=delay), self.assertRaises(ValueError):
                self.scheduler.prepare(entity, service, delay)
