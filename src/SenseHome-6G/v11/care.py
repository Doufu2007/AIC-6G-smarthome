"""Persistent SOS simulator. Notifications exist only in its demo inbox."""

from contextlib import closing, contextmanager
from pathlib import Path
import re
import sqlite3
import time


class CareStore:
    def __init__(self, db_path, *, ack_timeout_seconds=30, now=time.time):
        if (type(ack_timeout_seconds) not in (int, float)
                or not 0 < ack_timeout_seconds <= 86400):
            raise ValueError('ack timeout must be between zero and one day')
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ack_timeout_seconds = ack_timeout_seconds
        self.now = now
        with self._write() as db:
            db.execute('''
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY,
                    area_id TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    deadline_at REAL NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending_ack', 'acked', 'escalated')),
                    acknowledged_at REAL,
                    escalated_at REAL
                )
            ''')
            db.execute('''CREATE INDEX IF NOT EXISTS incident_deadlines
                          ON incidents(deadline_at) WHERE status='pending_ack' ''')

    @contextmanager
    def _write(self):
        with closing(sqlite3.connect(self.db_path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            with db:
                db.execute('BEGIN IMMEDIATE')
                yield db

    @staticmethod
    def _check_id(event_id):
        if not isinstance(event_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', event_id):
            raise ValueError('invalid SOS event ID')

    @staticmethod
    def _expire(db, now):
        return db.execute('''UPDATE incidents SET status='escalated', escalated_at=?
                             WHERE status='pending_ack' AND deadline_at<=?''', (now, now)).rowcount

    @staticmethod
    def _notice(event_id, kind, at):
        return {
            'id': f'{event_id}:{kind}', 'kind': kind, 'channel': 'demo_inbox',
            'status': 'delivered', 'delivered_at': at,
        }

    @classmethod
    def _result(cls, row):
        if row is None:
            return None
        incident = dict(row)
        event_id = incident['id']
        created_at = incident['created_at']
        timeline = [
            {'kind': 'sos_received', 'at': created_at},
            {'kind': 'initial_notice', 'at': created_at},
        ]
        notices = [cls._notice(event_id, 'initial', created_at)]
        if incident['acknowledged_at'] is not None:
            timeline.append({'kind': 'acknowledged', 'at': incident['acknowledged_at']})
        if incident['escalated_at'] is not None:
            at = incident['escalated_at']
            timeline.append({'kind': 'escalation_notice', 'at': at})
            notices.append(cls._notice(event_id, 'escalation', at))
        return {**incident, 'event_id': event_id, 'source': 'simulated', 'type': 'sos',
                'timeline': timeline, 'notifications': notices}

    def create_event(self, event_id, *, area_id='bedroom', occurred_at=None):
        self._check_id(event_id)
        if (not isinstance(area_id, str) or not 0 < len(area_id.strip()) <= 80
                or not all(character.isprintable() for character in area_id)):
            raise ValueError('invalid room ID')
        area_id = area_id.strip()
        if occurred_at is not None and (type(occurred_at) not in (int, float)
                                       or not 0 <= occurred_at <= 253402300799):
            raise ValueError('invalid occurrence time')
        with self._write() as db:
            now = self.now()
            self._expire(db, now)
            prior = db.execute('SELECT * FROM incidents WHERE id=?', (event_id,)).fetchone()
            if prior is not None:
                if prior['area_id'] != area_id or (occurred_at is not None and prior['occurred_at'] != occurred_at):
                    raise ValueError('event ID already exists with a different payload')
                return self._result(prior)
            db.execute('''INSERT INTO incidents
                          (id, area_id, occurred_at, created_at, deadline_at, status)
                          VALUES (?, ?, ?, ?, ?, 'pending_ack')''',
                       (event_id, area_id, now if occurred_at is None else occurred_at,
                        now, now + self.ack_timeout_seconds))
            return self._result(db.execute('SELECT * FROM incidents WHERE id=?', (event_id,)).fetchone())

    def get_incident(self, event_id):
        self._check_id(event_id)
        with self._write() as db:
            self._expire(db, self.now())
            return self._result(db.execute('SELECT * FROM incidents WHERE id=?', (event_id,)).fetchone())

    def list_incidents(self):
        with self._write() as db:
            self._expire(db, self.now())
            return [self._result(row) for row in db.execute('SELECT * FROM incidents ORDER BY created_at DESC, id')]

    def acknowledge(self, event_id):
        self._check_id(event_id)
        with self._write() as db:
            now = self.now()
            self._expire(db, now)
            db.execute('''UPDATE incidents SET status='acked', acknowledged_at=?
                          WHERE id=? AND status='pending_ack' ''', (now, event_id))
            return self._result(db.execute('SELECT * FROM incidents WHERE id=?', (event_id,)).fetchone())

    def expire_due(self):
        with self._write() as db:
            return self._expire(db, self.now())
