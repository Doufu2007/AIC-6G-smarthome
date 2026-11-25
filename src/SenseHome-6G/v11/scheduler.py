"""Persist and dispatch explicitly approved, short-delay light actions."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid

from proposal import canonical, digest


_COMPLETED = {'succeeded', 'verified_after_unknown', 'rejected', 'expired', 'unknown'}


class LightScheduler:
    delay_seconds = 10
    draft_ttl_seconds = 30
    max_lateness_seconds = 10

    def __init__(self, db_path, controller, *, now=time.time):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.controller = controller
        self.now = now
        with self._db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS light_schedules (
                schedule_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                entity_id TEXT NOT NULL, entity_configuration TEXT NOT NULL,
                service TEXT NOT NULL, delay_seconds INTEGER NOT NULL,
                created_at REAL NOT NULL, expires_at REAL NOT NULL,
                status TEXT NOT NULL, execute_at REAL, request_id TEXT NOT NULL UNIQUE,
                receipt TEXT
            )''')

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _get(db, schedule_id):
        row = db.execute('SELECT * FROM light_schedules WHERE schedule_id=?', (schedule_id,)).fetchone()
        if row is None:
            raise KeyError(schedule_id)
        value = dict(row)
        value.pop('entity_configuration')
        if value['receipt'] is not None:
            value['receipt'] = json.loads(value['receipt'])
        return value

    def _light(self, entity_id):
        return next((entry for entry in self.controller.catalog if entry['entity_id'] == entity_id
                     and entry['kind'] == 'light' and entry.get('available') is True), None)

    def prepare(self, entity_id, service, delay_seconds=10):
        entry = self._light(entity_id)
        if entry is None or service not in ('light.turn_on', 'light.turn_off'):
            raise ValueError('only configured light on/off actions can be scheduled')
        if type(delay_seconds) is not int or delay_seconds != self.delay_seconds:
            raise ValueError('only a ten-second delay is supported')
        schedule_id = uuid.uuid4().hex
        created = self.now()
        expiry = created + self.draft_ttl_seconds
        configuration = canonical(entry)
        card_digest = digest(dict(schedule_id=schedule_id, entity_id=entity_id, service=service,
                                  delay_seconds=delay_seconds, expires_at=expiry, entity_configuration=configuration))
        with self._db() as db:
            db.execute('''INSERT INTO light_schedules
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_confirmation', NULL, ?, NULL)''',
                       (schedule_id, card_digest, entity_id, configuration, service, delay_seconds,
                        created, expiry, 'schedule-' + schedule_id))
            return self._get(db, schedule_id)

    def confirm(self, schedule_id, digest):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._get(db, schedule_id)
            if digest != task['digest']:
                raise ValueError('schedule digest mismatch')
            if task['status'] == 'awaiting_confirmation':
                now = self.now()
                if now >= task['expires_at']:
                    db.execute("UPDATE light_schedules SET status='expired' WHERE schedule_id=?", (schedule_id,))
                else:
                    db.execute("UPDATE light_schedules SET status='scheduled', execute_at=? WHERE schedule_id=?",
                               (now + task['delay_seconds'], schedule_id))
            return self._get(db, schedule_id)

    def cancel(self, schedule_id):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._get(db, schedule_id)
            if task['status'] in ('awaiting_confirmation', 'scheduled'):
                db.execute("UPDATE light_schedules SET status='cancelled' WHERE schedule_id=?", (schedule_id,))
            return self._get(db, schedule_id)

    def list_schedules(self):
        with self._db() as db:
            ids = db.execute('SELECT schedule_id FROM light_schedules ORDER BY created_at DESC, schedule_id').fetchall()
            return [self._get(db, row['schedule_id']) for row in ids]

    def run_due(self):
        now = self.now()
        with self._db() as db:
            db.execute("UPDATE light_schedules SET status='expired' WHERE status='awaiting_confirmation' AND expires_at<=?",
                       (now,))
            ids = db.execute("""SELECT schedule_id FROM light_schedules WHERE status='running'
                             OR (status='scheduled' AND execute_at<=?) ORDER BY execute_at""", (now,)).fetchall()
        finished = []
        for row in ids:
            schedule_id = row['schedule_id']
            # The shared process lock distinguishes an interrupted worker from an active one.
            with self.controller.execution._request_guard('scheduler-worker-' + schedule_id) as acquired:
                if acquired:
                    result = self._run_one(schedule_id)
                    if result is not None:
                        finished.append(result)
        return finished

    def _run_one(self, schedule_id):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._get(db, schedule_id)
            if task['status'] not in ('scheduled', 'running'):
                return None
            recovery = task['status'] == 'running'
            configuration = db.execute('SELECT entity_configuration FROM light_schedules WHERE schedule_id=?',
                                       (schedule_id,)).fetchone()[0]
            db.execute("UPDATE light_schedules SET status='running' WHERE schedule_id=?", (schedule_id,))

        if recovery:
            receipt = self.controller.receipt(task['request_id'])
            if receipt is None or receipt['status'] not in _COMPLETED:
                receipt = dict(status='unknown', reason='scheduled_dispatch_interrupted')
        else:
            receipt = self._dispatch(task, configuration)
        with self._db() as db:
            db.execute("UPDATE light_schedules SET status=?, receipt=? WHERE schedule_id=? AND status='running'",
                       (receipt['status'], canonical(receipt), schedule_id))
            return self._get(db, schedule_id)

    def _dispatch(self, task, configuration):
        deadline = task['execute_at'] + self.max_lateness_seconds
        if self.now() > deadline:
            return dict(status='expired', reason='schedule_missed_execution_window')
        entry = self._light(task['entity_id'])
        if entry is None or canonical(entry) != configuration:
            return dict(status='rejected', reason='scheduled_entity_configuration_changed')
        command = ('打开' if task['service'] == 'light.turn_on' else '关闭') + entry['room'] + '的' + entry['name']
        expected = [dict(entity_id=task['entity_id'], service=task['service'], data={})]
        card = self.controller.propose(command)
        if card['status'] != 'awaiting_confirmation' or card['payload']['actions'] != expected:
            return dict(status='rejected', reason='scheduled_action_no_longer_valid')
        if self.now() > deadline:
            return dict(status='expired', reason='schedule_missed_execution_window')
        try:
            return self.controller.confirm(dict(proposal_id=card['proposal_id'], digest=card['digest'],
                                                request_id=task['request_id'], confirmed=True))
        except ValueError:
            return self.controller.receipt(task['request_id']) or dict(status='rejected', reason='scheduled_confirmation_failed')
