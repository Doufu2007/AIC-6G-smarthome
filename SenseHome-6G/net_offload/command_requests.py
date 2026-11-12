"""Bind client identity to the original request before any planning or execution.

An interrupted/unknown request is queried, never automatically planned again.
The receipt executor separately binds the eventual device plan to this same ID.
"""
from contextlib import closing
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from durable_receipts import canonical, checked_id

_active = set()
_guard = threading.RLock()
_active_resolution = ContextVar('sensehome_request_resolution', default=None)


def bind_active_resolution(*, command_id=None, **resolution):
    """Persist only an in-process server binding; never put callbacks in context."""
    active = _active_resolution.get()
    if active is None:
        return None
    journal, active_id = active
    if command_id is not None and command_id != active_id:
        raise ValueError('REQUEST_RESOLUTION_ID_MISMATCH')
    return journal.bind_resolution(active_id, **resolution)


class RequestJournal:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS requests (command_id TEXT PRIMARY KEY, '
                       'request_hash TEXT NOT NULL, command TEXT NOT NULL, context_json TEXT NOT NULL, '
                       'result_json TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS request_resolutions (command_id TEXT PRIMARY KEY, '
                       'request_hash TEXT NOT NULL, resolved_command TEXT NOT NULL, contract_json TEXT, '
                       'created_at REAL NOT NULL, updated_at REAL NOT NULL)')
            if 'result_revision' not in {row['name'] for row in db.execute('PRAGMA table_info(requests)')}:
                db.execute('ALTER TABLE requests ADD COLUMN result_revision INTEGER NOT NULL DEFAULT 0')

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        return db

    def _row(self, command_id):
        with closing(self._connect()) as db:
            row = db.execute('SELECT * FROM requests WHERE command_id=?', (command_id,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _unconfirmed(command_id, state, error):
        return {'command_id': command_id, 'request_state': state, 'semantic_complete': False,
                'task_complete': False, 'interface_ok': False, 'actions': [], 'error': error,
                'answer': '本次请求完成情况未确认；仅查询原请求，请勿作为新命令重发。'}

    def _result(self, row, *, replayed=True):
        if row['result_json'] is not None:
            return {**json.loads(row['result_json']), 'replayed': replayed,
                    'result_revision': row['result_revision']}
        key = (str(self.path), row['command_id'])
        with _guard:
            running = key in _active
        return {**self._unconfirmed(row['command_id'], 'processing' if running else 'interrupted',
                                    'REQUEST_PROCESSING' if running else 'REQUEST_INTERRUPTED'),
                'replayed': replayed, 'result_revision': row['result_revision']}

    def execute(self, command, context, operation):
        context = {k: deepcopy(v) for k, v in dict(context or {}).items() if not k.startswith('_')}
        command_id = checked_id(context.get('command_id', uuid.uuid4().hex))
        context['command_id'] = command_id
        digest = hashlib.sha256(canonical({'command': command, 'context': context}).encode()).hexdigest()
        key = (str(self.path), command_id)
        with _guard:
            with closing(self._connect()) as db, db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM requests WHERE command_id=?', (command_id,)).fetchone()
                if row is not None:
                    if row['request_hash'] != digest:
                        return self._unconfirmed(command_id, 'conflict', 'COMMAND_ID_CONFLICT')
                    return self._result(dict(row))
                now = time.time()
                db.execute('INSERT INTO requests (command_id,request_hash,command,context_json,result_json,created_at,updated_at) '
                           'VALUES (?,?,?,?,NULL,?,?)',
                           (command_id, digest, command, canonical(context), now, now))
            _active.add(key)
        binding_token = _active_resolution.set((self, command_id))
        try:
            result = dict(operation())
            uncertain = result.get('execution_state') == 'unknown'
            awaiting_observation = (result.get('execution_state') == 'applied' and not
                                    (result.get('task_complete') or result.get('supported_scope_complete')))
            result.update(command_id=command_id, request_state=('unknown' if uncertain else
                'observation_pending' if awaiting_observation else 'finished'), replayed=False)
            with closing(self._connect()) as db, db:
                db.execute('UPDATE requests SET result_json=?,updated_at=?,result_revision=result_revision+1 WHERE command_id=?',
                           (canonical(result), time.time(), command_id))
            return self._result(self._row(command_id), replayed=False)
        finally:
            _active_resolution.reset(binding_token)
            with _guard:
                _active.discard(key)

    def lookup(self, command_id):
        command_id = checked_id(command_id)
        row = self._row(command_id)
        if row is None:
            return self._unconfirmed(command_id, 'not_found', 'REQUEST_NOT_FOUND')
        return self._result(row)

    def original(self, command_id):
        row = self._row(checked_id(command_id))
        return None if row is None else {'command': row['command'], 'context': json.loads(row['context_json'])}

    def resolution(self, command_id):
        """Read the server's immutable resolution, separate from client context."""
        with closing(self._connect()) as db:
            row = db.execute('SELECT r.* FROM request_resolutions r JOIN requests q USING(command_id) '
                             'WHERE r.command_id=? AND r.request_hash=q.request_hash',
                             (checked_id(command_id),)).fetchone()
        if row is None:
            return None
        contract = json.loads(row['contract_json']) if row['contract_json'] is not None else {}
        return {'resolved_command': row['resolved_command'], **contract}

    def bind_resolution(self, command_id, *, resolved_command=None, goals=None,
                        unsupported_capabilities=None, supported_scope=None):
        """Bind resolution before planning and a complete contract before submit.

        The first binding wins. Replays may confirm the same binding but cannot
        replace a room, goal, value, or an unsupported part after a crash.
        """
        command_id = checked_id(command_id)
        if resolved_command is not None and (not isinstance(resolved_command, str) or
                not resolved_command.strip() or len(resolved_command) > 4096):
            raise ValueError('INVALID_REQUEST_RESOLUTION')
        contract = None
        if goals is not None:
            from target_contract import action_goal
            if (not isinstance(goals, list) or not 1 <= len(goals) <= 16 or
                    any(action_goal(goal) is None for goal in goals)):
                raise ValueError('INVALID_REQUEST_GOAL_CONTRACT')
            unsupported = [] if unsupported_capabilities is None else unsupported_capabilities
            supported = [] if supported_scope is None else supported_scope
            if any(not isinstance(values, list) or len(values) > 32 or
                   any(not isinstance(value, str) or not value or len(value) > 128 for value in values)
                   for values in (unsupported, supported)):
                raise ValueError('INVALID_REQUEST_GOAL_SCOPE')
            contract = canonical({'schema': 'sensehome.request-goals.v1', 'goals': goals,
                                  'unsupported_capabilities': unsupported, 'supported_scope': supported})
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            request = db.execute('SELECT request_hash,command FROM requests WHERE command_id=?',
                                 (command_id,)).fetchone()
            if request is None:
                raise ValueError('REQUEST_NOT_FOUND')
            previous = db.execute('SELECT * FROM request_resolutions WHERE command_id=?',
                                  (command_id,)).fetchone()
            if previous is not None:
                if (previous['request_hash'] != request['request_hash'] or
                        (resolved_command is not None and resolved_command != previous['resolved_command'])):
                    raise ValueError('REQUEST_RESOLUTION_CONFLICT')
                if contract is not None and previous['contract_json'] is not None and contract != previous['contract_json']:
                    raise ValueError('REQUEST_GOAL_CONTRACT_CONFLICT')
                if contract is not None and previous['contract_json'] is None:
                    db.execute('UPDATE request_resolutions SET contract_json=?,updated_at=? WHERE command_id=?',
                               (contract, time.time(), command_id))
            else:
                now = time.time()
                db.execute('INSERT INTO request_resolutions VALUES (?,?,?,?,?,?)',
                           (command_id, request['request_hash'], resolved_command or request['command'], contract, now, now))
        return self.resolution(command_id)

    def reconcile(self, command_id, result):
        """Commit only against the result revision observed before device I/O.

        A completed command remains historical fact. Later observations are
        stored separately, including a genuine subsequent device state change.
        """
        command_id = checked_id(command_id)
        with closing(self._connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM requests WHERE command_id=?', (command_id,)).fetchone()
            if row is None:
                return self._unconfirmed(command_id, 'not_found', 'REQUEST_NOT_FOUND')
            if result.get('result_revision') == row['result_revision']:
                current = json.loads(row['result_json']) if row['result_json'] is not None else {}
                completed = (current.get('request_state') == 'finished' and
                             current.get('execution_state') == 'applied' and
                             (current.get('task_complete') or current.get('supported_scope_complete')))
                if completed:
                    result = {**current, **{key: result[key] for key in
                              ('current_observation', 'current_goal_acceptance') if key in result}}
                updated = {**result, 'command_id': command_id, 'replayed': True}
                db.execute('UPDATE requests SET result_json=?,updated_at=?,result_revision=result_revision+1 '
                           'WHERE command_id=? AND result_revision=?',
                           (canonical(updated), time.time(), command_id, row['result_revision']))
        # _result may take _guard for an interrupted request. Do not hold a
        # SQLite writer while taking it: execute acquires them in reverse order.
        return self.lookup(command_id)
