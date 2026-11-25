"""Durable target-state execution over the official Home Assistant REST API.

This layer provides persistent request identity, dispatch-before-send journaling,
and independent state verification. It does not provide exactly-once delivery,
atomic multi-entity actions, or physical-device verification. Only trusted code
may pass ``authorized=True`` after validating the user's confirmation contract.
An unknown result is queried; it is never automatically dispatched again.
"""
from pathlib import Path
from contextlib import contextmanager
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from urllib.parse import quote, urlparse
from urllib.request import Request
from capabilities import DEVICE_SERVICES, temperature_data_valid, temperature_target_supported
from deadline import request_response


_HA_AUDIT_LOCK = threading.RLock()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class HAError(RuntimeError):
    """An official API request failed without exposing credentials."""


class UnknownEntity(HAError):
    pass


class NotDispatched(HAError, ValueError):
    """The client rejected locally, before any service request was sent."""


class HAClient:
    def __init__(self, base_url, token, timeout=5, audit_path=None):
        parsed = urlparse(base_url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('invalid Home Assistant base URL')
        if not isinstance(token, str) or not token:
            raise ValueError('a private HA token is required')
        self.base_url = base_url.rstrip('/')
        self._token = token
        self.timeout = timeout
        self.audit_path = Path(audit_path) if audit_path else None
        self._audit_local = threading.local()

    @property
    def last_audit_status(self):
        """This thread's latest HTTP audit outcome, separate from API success.

        Callers running requests in workers must carry this telemetry back with
        the worker result. Reading it in another thread does not aggregate the
        worker's audit status.
        """
        return dict(getattr(self._audit_local, 'status',
            dict(status='not_attempted' if self.audit_path else 'disabled',
                 scope='supplemental HA JSONL')))

    def _record_audit(self, record):
        path = self.audit_path
        if path is None:
            self._audit_local.status = dict(status='disabled', scope='supplemental HA JSONL')
            return
        try:
            # This lock is independent of execution/state locks and covers all
            # HAClient instances in this process, including a shared log file.
            with _HA_AUDIT_LOCK:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('a', encoding='utf8') as stream:
                    stream.write(_json(record).replace(self._token, '<REDACTED>') + '\n')
            self._audit_local.status = dict(status='written', scope='supplemental HA JSONL')
        except Exception as exc:
            # Never replace a completed API result or the original API error
            # with a supplemental filesystem/serialization failure. Only the
            # exception type is public; paths/messages may contain credentials.
            self._audit_local.status = dict(status='failed', error_type=type(exc).__name__,
                                           scope='supplemental HA JSONL')

    def _request(self, path, body=None, *, timeout=None):
        raw_body = _json(body).encode('utf8') if body is not None else None
        headers = {'Authorization': 'Bearer ' + self._token, 'Accept': 'application/json'}
        if raw_body is not None:
            headers['Content-Type'] = 'application/json'
        request = Request(self.base_url + path, data=raw_body, headers=headers)
        started = time.time()
        record = {'at': started, 'method': request.get_method(), 'path': path,
                  'request_headers': {**headers, 'Authorization': '<REDACTED>'}, 'request_body': body}
        try:
            budget = self.timeout if timeout is None else min(self.timeout, timeout)
            status, _, _, data = request_response(request, timeout=budget, max_bytes=4 * 1024 * 1024)
            try:
                value = json.loads(data)
            except ValueError:
                value = data.decode('utf8', errors='replace')
            record.update(status=status, response='<omitted>' if path == '/api/config' else value)
            if status == 404 and path.startswith('/api/states/'):
                raise UnknownEntity('unknown_entity')
            if not 200 <= status < 300:
                raise HAError('HA HTTP ' + str(status))
            return value
        except Exception as exc:
            if isinstance(exc, TimeoutError) and type(exc).__name__ != 'TimeoutError':
                record['error_type'] = 'TimeoutError'
                raise TimeoutError('Home Assistant request deadline exceeded') from exc
            record['error_type'] = type(exc).__name__
            # Network errors can contain userinfo from a URL. Only type is public.
            raise
        finally:
            record['elapsed_ms'] = round((time.time() - started) * 1000, 3)
            self._record_audit(record)

    def get_state(self, entity_id):
        value = self._request('/api/states/' + quote(entity_id, safe=''))
        if not isinstance(value, dict) or value.get('entity_id') != entity_id or 'state' not in value:
            raise HAError('invalid_state_response')
        return value

    def get_state_with_timeout(self, entity_id, timeout):
        value = self._request('/api/states/' + quote(entity_id, safe=''), timeout=timeout)
        if not isinstance(value, dict) or value.get('entity_id') != entity_id or 'state' not in value:
            raise HAError('invalid_state_response')
        return value

    def call_service(self, entity_id, service, data):
        domain, action = service.split('.', 1)
        return self._request('/api/services/' + domain + '/' + action, {**data, 'entity_id': entity_id})

    def get_states(self):
        value = self._request('/api/states')
        if not isinstance(value, list) or any(not isinstance(s, dict) for s in value):
            raise HAError('invalid_state_list_response')
        return value

    def get_config(self, *, timeout=None):
        value = self._request('/api/config', timeout=timeout)
        if not isinstance(value, dict):
            raise HAError('invalid_config_response')
        return value


class ExecutionStore:
    """SQLite journal. Separate processes may share a local journal file.

    ``snapshot`` produces the complete precondition map accepted by ``execute``.
    ``execute`` checks current state and last_updated before any POST. The HTTP
    API cannot atomically compare-and-set HA state, so external changes between
    the preflight GET and POST remain a documented race.
    """
    def __init__(self, db_path, client, *, max_age_seconds=30, now=time.time,
                 verify_timeout_seconds=0.25, poll_interval_seconds=0.02):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.client = client
        self.max_age_seconds = max_age_seconds
        self.now = now
        if type(max_age_seconds) not in (int, float) or not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
            raise ValueError('max age must be finite and positive')
        if (type(verify_timeout_seconds) not in (int, float) or
                not math.isfinite(verify_timeout_seconds) or not 0 <= verify_timeout_seconds <= 30):
            raise ValueError('verification timeout must be within 0..30 seconds')
        if (type(poll_interval_seconds) not in (int, float) or
                not math.isfinite(poll_interval_seconds) or not 0 < poll_interval_seconds <= 1):
            raise ValueError('poll interval must be within 0..1 seconds')
        self.verify_timeout_seconds = verify_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.lock_directory = self.db_path.parent / ('.' + self.db_path.name + '.locks')
        self.lock_directory.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, plan TEXT NOT NULL,
                    expected TEXT NOT NULL, status TEXT NOT NULL, result TEXT, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS steps (
                    request_id TEXT NOT NULL, ordinal INTEGER NOT NULL, entity_id TEXT NOT NULL,
                    service TEXT NOT NULL, data TEXT NOT NULL, status TEXT NOT NULL,
                    verification TEXT, error_type TEXT,
                    PRIMARY KEY (request_id, ordinal),
                    FOREIGN KEY (request_id) REFERENCES requests(request_id)
                );
                CREATE TABLE IF NOT EXISTS entity_owners (
                    entity_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                    FOREIGN KEY (request_id) REFERENCES requests(request_id)
                );
            ''')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(requests)')}
            for name, declaration in [('owner', 'TEXT'), ('revision', 'INTEGER NOT NULL DEFAULT 0'),
                                      ('authorization_expires_at', 'REAL'),
                                      ('operator_resolution', 'TEXT')]:
                if name not in columns:
                    db.execute('ALTER TABLE requests ADD COLUMN ' + name + ' ' + declaration)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def snapshot(self, entity_ids):
        result = {}
        for entity in entity_ids:
            value = self.client.get_state(entity)
            result[entity] = {**value, 'observed_at': self.now()}
        return result

    @staticmethod
    def _validate(plan, expected):
        if not isinstance(plan, list) or not plan or len(plan) > 32:
            return 'invalid_plan'
        entities = []
        for step in plan:
            if not isinstance(step, dict) or set(step) != {'entity_id', 'service', 'data'}:
                return 'invalid_step'
            entity, service, data = step['entity_id'], step['service'], step['data']
            if not isinstance(entity, str) or not re.fullmatch(r'[a-z_]+\.[a-z0-9_]+', entity):
                return 'invalid_entity_id'
            if not isinstance(service, str) or not isinstance(data, dict):
                return 'invalid_service'
            domain = entity.split('.')[0]
            allowed = {**DEVICE_SERVICES, 'input_boolean': ('input_boolean.turn_on', 'input_boolean.turn_off'),
                       'input_number': ('input_number.set_value',)}
            if service not in allowed.get(domain, ()):
                return 'unsupported_target_action'
            if service == 'light.turn_on':
                if not set(data) <= {'brightness_pct'}:
                    return 'invalid_service_data'
                if 'brightness_pct' in data:
                    value = data['brightness_pct']
                    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
                        return 'invalid_brightness'
            elif service == 'input_number.set_value':
                if set(data) != {'value'} or type(data['value']) not in (int, float) or not math.isfinite(data['value']):
                    return 'invalid_number'
            elif service == 'climate.set_temperature':
                if not temperature_data_valid(data):
                    return 'invalid_temperature'
            elif data:
                return 'invalid_service_data'
            entities.append(entity)
        if len(entities) != len(set(entities)):
            return 'repeated_entity'
        if not isinstance(expected, dict) or set(expected) != set(entities):
            return 'precondition_entity_set_mismatch'
        return None

    @staticmethod
    def _matches(step, state, expected=None):
        service, data = step['service'], step['data']
        value = state.get('state')
        if service in ('input_boolean.turn_on', 'input_boolean.turn_off', 'light.turn_off',
                       'switch.turn_on', 'switch.turn_off'):
            return value == ('on' if service.endswith('turn_on') else 'off')
        if service == 'light.turn_on':
            pct = data.get('brightness_pct')
            if pct == 0:
                return value == 'off'
            if value != 'on':
                return False
            if pct is None:
                return True
            brightness = state.get('attributes', {}).get('brightness')
            return type(brightness) in (int, float) and abs(brightness - pct * 255 / 100) <= 1
        if service in ('cover.open_cover', 'cover.close_cover'):
            target = 'open' if service.endswith('open_cover') else 'closed'
            position = state.get('attributes', {}).get('current_position')
            return value == target and (position is None or position == (100 if target == 'open' else 0))
        if service == 'input_number.set_value':
            try:
                return abs(float(value) - data['value']) < 1e-7
            except (TypeError, ValueError):
                return False
        if service == 'climate.set_temperature':
            attrs = state.get('attributes', {})
            actual = attrs.get('temperature')
            return (expected is not None and value == expected.get('state') and
                    attrs.get('temperature_unit') == '°C' and temperature_data_valid({'temperature': actual}) and
                    abs(actual - data['temperature']) <= 1e-7)
        return False

    @contextmanager
    def _request_guard(self, request_id):
        """An OS-owned lock is released on process death, unlike a time lease.

        The SQLite owner token fences state updates. OS locks prevent an active
        sender being mistaken for a dead sender while its HTTP call is slow.
        """
        path = self.lock_directory / (hashlib.sha256(request_id.encode('utf8')).hexdigest() + '.lock')
        stream = path.open('a+b')
        acquired = False
        try:
            if stream.seek(0, 2) == 0:
                stream.write(b'\0'); stream.flush()
            stream.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (OSError, BlockingIOError):
                pass
            yield acquired
        finally:
            if acquired:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()

    @staticmethod
    def _receipt_from_row(row):
        result = json.loads(row['result']) if row['result'] else dict(
            request_id=row['request_id'], status='unknown', duplicate=True,
            reason='dispatch_in_progress_or_interrupted', steps=[])
        if row['operator_resolution']:
            result['operator_resolution'] = json.loads(row['operator_resolution'])
        return result

    def get_receipt(self, request_id):
        """Pure durable read; no platform traffic, dispatch, or reconciliation."""
        with self._db() as db:
            row = db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
        return None if row is None else self._receipt_from_row(row)

    def resolve_unknown(self, request_id, *, operator, reason, disposition,
                        acknowledge_late_risk=False, platform_settled=False):
        """Record a trusted local operator's decision and release its reservations.

        ``verified_settled`` requires the operator's explicit statement that the
        platform has finished processing the old request. ``accept_late_risk``
        instead requires explicit acceptance that a late old action may still
        occur. Neither disposition cancels a remote action or proves the old
        target succeeded. The original unknown result and step history remain
        intact; even reconciliation cannot continue this request afterwards.
        An interrupted preparing/dispatching row is also observationally
        unknown, and can be settled directly after its OS-owned guard is free;
        no reconciliation or platform read is required first.

        Callers must restrict this API to trusted local operators. The supplied
        operator name is an audit statement, not an authentication mechanism.
        """
        def reject(why):
            return dict(request_id=request_id, status='rejected', reason=why,
                        steps=[], duplicate=False)

        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            return reject('invalid_request_id')
        if not isinstance(operator, str) or not operator.strip() or len(operator) > 200:
            return reject('invalid_operator')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
            return reject('invalid_resolution_reason')
        if disposition not in ('verified_settled', 'accept_late_risk'):
            return reject('invalid_disposition')
        if disposition == 'verified_settled' and platform_settled is not True:
            return reject('platform_settled_confirmation_required')
        if disposition == 'accept_late_risk' and acknowledge_late_risk is not True:
            return reject('late_risk_confirmation_required')

        # The same OS-owned guard covers execution and reconciliation. A slow
        # active sender cannot be mistaken for an abandoned unknown request.
        with self._request_guard(request_id) as acquired:
            if not acquired:
                return reject('execution_in_progress')
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
                if row is None:
                    return reject('unknown_request_id')
                if row['operator_resolution']:
                    # First decision wins. A replay cannot change its audit
                    # record or release an entity now owned by another request.
                    return {**self._receipt_from_row(row), 'duplicate': True}
                if row['status'] not in ('unknown', 'preparing', 'dispatching'):
                    return reject('request_not_unknown')
                entities = [r['entity_id'] for r in db.execute(
                    'SELECT entity_id FROM entity_owners WHERE request_id=? ORDER BY entity_id',
                    (request_id,))]
                resolution = dict(operator=operator.strip(), reason=reason.strip(),
                    disposition=disposition, resolved_at=self.now(),
                    platform_settled=platform_settled is True,
                    acknowledge_late_risk=acknowledge_late_risk is True,
                    released_entities=entities, scope='local_reservations_only',
                    remote_cancelled=False)
                # Preserve status/result/steps verbatim. Audit and removal of
                # only this request's reservations commit atomically.
                db.execute('UPDATE requests SET operator_resolution=?,owner=NULL,revision=revision+1 '
                           'WHERE request_id=? AND revision=?',
                           (_json(resolution), request_id, row['revision']))
                db.execute('DELETE FROM entity_owners WHERE request_id=?', (request_id,))
                updated = db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
                return self._receipt_from_row(updated)

    def _finish(self, request_id, status, reason=None, *, duplicate=False, owner=None):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            request = db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
            if request['operator_resolution'] or request['status'] in ('succeeded', 'verified_after_unknown', 'rejected'):
                return {**self._receipt_from_row(request), 'duplicate': True}
            if owner is not None and request['owner'] != owner:
                return {**self._receipt_from_row(request), 'duplicate': True}
            rows = db.execute('SELECT * FROM steps WHERE request_id=? ORDER BY ordinal', (request_id,)).fetchall()
            result = dict(request_id=request_id, status=status, duplicate=duplicate,
                steps=[{**dict(row), 'data': json.loads(row['data']),
                        'verification': json.loads(row['verification']) if row['verification'] else None} for row in rows])
            if reason: result['reason'] = reason
            db.execute('UPDATE requests SET status=?,result=?,revision=revision+1 WHERE request_id=? AND revision=?',
                       (status, _json(result), request_id, request['revision']))
            if status in ('succeeded', 'verified_after_unknown', 'rejected'):
                db.execute('DELETE FROM entity_owners WHERE request_id=?', (request_id,))
            else:
                # At rest, only an unresolved send needs an indefinite
                # reservation. Verified and never-sent entities can be used by
                # a new consent; a later continuation must reacquire and check
                # their original state before it can send anything.
                db.execute("DELETE FROM entity_owners WHERE request_id=? AND entity_id IN "
                           "(SELECT entity_id FROM steps WHERE request_id=? AND status IN ('verified','not_dispatched'))",
                           (request_id, request_id))
        return result

    def _precondition(self, entity, expected):
        if not isinstance(expected, dict) or expected.get('entity_id') != entity:
            return 'invalid_precondition'
        observed = expected.get('observed_at')
        if type(observed) not in (int, float) or not math.isfinite(observed) or not 0 <= self.now() - observed <= self.max_age_seconds:
            return 'stale_or_future_precondition'
        current = self.client.get_state(entity)
        if not 0 <= self.now() - observed <= self.max_age_seconds:
            return 'stale_or_future_precondition'
        if current.get('state') in ('unavailable', 'unknown'):
            return 'entity_unavailable'
        if not expected.get('last_updated') or any(current.get(key) != expected.get(key) for key in ('state', 'last_updated', 'attributes')):
            return 'precondition_changed'
        return None

    @staticmethod
    def _capability_reason(step, state):
        attrs = state.get('attributes', {})
        if not isinstance(attrs, dict): return 'invalid_capability_state'
        services = attrs.get('supported_services')
        if services is not None and (not isinstance(services, list) or step['service'] not in services):
            return 'unsupported_capability'
        if 'brightness_pct' in step['data'] and attrs.get('brightness_supported') is False:
            return 'unsupported_capability'
        if step['service'] == 'input_number.set_value':
            target = step['data']['value']
            if 'min' not in attrs or 'max' not in attrs or not attrs['min'] <= target <= attrs['max']:
                return 'number_out_of_range'
        if step['service'] == 'climate.set_temperature' and not temperature_target_supported(step['data'], state):
            return 'temperature_out_of_range_or_unsupported'
        return None

    def _verify(self, step, expected=None):
        deadline = time.monotonic() + self.verify_timeout_seconds
        actual, error = None, None
        first_read = True
        while True:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0 and not first_read:
                    return False, actual, error
                bounded_read = getattr(self.client, 'get_state_with_timeout', None)
                if callable(bounded_read) and remaining > 0:
                    actual = bounded_read(step['entity_id'], remaining)
                else:
                    actual = self.client.get_state(step['entity_id'])
                first_read = False
                if self._matches(step, actual, expected): return True, actual, error
            except Exception as exc:
                error = type(exc).__name__
            remaining = deadline - time.monotonic()
            if remaining <= 0: return False, actual, error
            time.sleep(min(self.poll_interval_seconds, remaining))

    def _claim(self, request_id):
        owner = uuid.uuid4().hex
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
            if row is None: return None
            db.execute('UPDATE requests SET owner=?,revision=revision+1 WHERE request_id=?', (owner, request_id))
        return owner

    def _release(self, request_id, owner):
        with self._db() as db:
            db.execute('UPDATE requests SET owner=NULL WHERE request_id=? AND owner=?', (request_id, owner))

    def _run_owned(self, request_id, owner, *, recovering=False):
        with self._db() as db:
            request = db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
            rows = db.execute('SELECT * FROM steps WHERE request_id=? ORDER BY ordinal', (request_id,)).fetchall()
        if request['operator_resolution']:
            return {**self._receipt_from_row(request), 'duplicate': True}
        expected = json.loads(request['expected'])
        stored_plan = json.loads(request['plan'])
        reason = self._validate(stored_plan, expected)
        if reason:
            return self._finish(request_id, 'rejected', reason, owner=owner, duplicate=recovering)
        if len(rows) != len(stored_plan) or any(
                (row['ordinal'], row['entity_id'], row['service'], json.loads(row['data'])) !=
                (i, step['entity_id'], step['service'], step['data'])
                for i, (row, step) in enumerate(zip(rows, stored_plan))):
            return self._finish(request_id, 'unknown', 'journal_incomplete_no_dispatch', owner=owner, duplicate=recovering)
        any_sent = any(row['status'] != 'not_dispatched' for row in rows)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            conflicts = [db.execute('SELECT request_id FROM entity_owners WHERE entity_id=?',
                         (s['entity_id'],)).fetchone() for s in stored_plan]
            busy = any(r is not None and r['request_id'] != request_id for r in conflicts)
            if not busy:
                for step, prior in zip(stored_plan, conflicts):
                    if prior is None:
                        db.execute('INSERT INTO entity_owners VALUES (?,?)', (step['entity_id'], request_id))
        if busy:
            return self._finish(request_id, 'unknown' if any_sent else 'rejected', 'entity_busy',
                                owner=owner, duplicate=recovering)
        uncertain = recovering or any(row['error_type'] for row in rows)
        for row in rows:
            if row['status'] == 'verified': continue
            step = {**dict(row), 'data': json.loads(row['data'])}
            if row['status'] == 'not_dispatched':
                # Both the user's original consent and state snapshot must still
                # be fresh before a never-sent step can be resumed.
                expires = request['authorization_expires_at']
                reason = 'authorization_expired' if expires is not None and self.now() >= expires else None
                try:
                    reason = reason or self._precondition(row['entity_id'], expected[row['entity_id']])
                    reason = reason or self._capability_reason(step, expected[row['entity_id']])
                    if reason is None and hasattr(self.client, 'validate_action'):
                        self.client.validate_action(row['entity_id'], row['service'], step['data'],
                                                    state=expected[row['entity_id']])
                except NotDispatched:
                    reason = 'unsupported_capability'
                except Exception as exc:
                    reason = 'preflight_unavailable:' + type(exc).__name__
                if reason:
                    return self._finish(request_id, 'unknown' if any_sent else 'rejected',
                        'predispatch_' + reason + ('_partial_plan' if any_sent else ''),
                        owner=owner, duplicate=recovering)
                with self._db() as db:
                    db.execute('UPDATE requests SET status=?,revision=revision+1 WHERE request_id=? AND owner=?',
                               ('dispatching', request_id, owner))
                    db.execute('UPDATE steps SET status=? WHERE request_id=? AND ordinal=?',
                               ('dispatching', request_id, row['ordinal']))
                error = None
                try:
                    if expires is not None and self.now() >= expires:
                        raise NotDispatched('authorization_expired_before_send')
                    if hasattr(self.client, 'call_service_checked'):
                        self.client.call_service_checked(row['entity_id'], row['service'], step['data'],
                            state=expected[row['entity_id']], authorization_expires_at=expires, now=self.now)
                    else:
                        self.client.call_service(row['entity_id'], row['service'], step['data'])
                    any_sent = True
                except NotDispatched as exc:
                    # Typed local adapter rejection is proven not to have sent.
                    with self._db() as db:
                        db.execute('UPDATE steps SET status=?,error_type=? WHERE request_id=? AND ordinal=?',
                                   ('not_dispatched', type(exc).__name__, request_id, row['ordinal']))
                    return self._finish(request_id, 'unknown' if any_sent else 'rejected',
                                        'service_not_dispatched', owner=owner, duplicate=recovering)
                except Exception as exc:
                    any_sent = True
                    uncertain = True
                    error = type(exc).__name__
            else:
                # A persisted dispatch intent may already have reached HA.
                # Reconciliation only GETs it; no second POST is issued.
                error = row['error_type']
            verified, actual, read_error = self._verify(step, expected[row['entity_id']])
            error = error or read_error
            with self._db() as db:
                db.execute('UPDATE steps SET status=?,verification=?,error_type=? WHERE request_id=? AND ordinal=?',
                           ('verified' if verified else 'unknown', _json(actual) if actual else None,
                            error, request_id, row['ordinal']))
            if not verified:
                return self._finish(request_id, 'unknown', 'target_not_verified_no_automatic_retry',
                                    owner=owner, duplicate=recovering)
        return self._finish(request_id, 'verified_after_unknown' if uncertain else 'succeeded',
                            owner=owner, duplicate=recovering)

    def execute(self, request_id, plan, expected_states, *, authorized=False, authorization_expires_at=None):
        def reject(reason, duplicate=False):
            return dict(request_id=request_id, status='rejected', reason=reason, steps=[], duplicate=duplicate)
        if authorized is not True: return reject('authorization_required')
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            return reject('invalid_request_id')
        if authorization_expires_at is not None and (type(authorization_expires_at) not in (int, float) or
                not math.isfinite(authorization_expires_at)):
            return reject('invalid_authorization_expiry')
        try:
            payload = _json(dict(plan=plan, expected_states=expected_states))
        except (TypeError, ValueError): return reject('invalid_json')
        digest = hashlib.sha256(payload.encode('utf8')).hexdigest()
        frozen = json.loads(payload)
        plan, expected_states = frozen['plan'], frozen['expected_states']
        with self._request_guard(request_id) as acquired:
            with self._db() as db:
                old = db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
            if old:
                if old['digest'] != digest: return reject('request_id_conflict', True)
                return {**self._receipt_from_row(old), 'duplicate': True}
            if not acquired:
                return dict(request_id=request_id, status='unknown', reason='execution_in_progress', steps=[], duplicate=True)
            reason = self._validate(plan, expected_states)
            expiries = [] if reason else [s.get('observed_at') + self.max_age_seconds for s in expected_states.values()
                if isinstance(s, dict) and type(s.get('observed_at')) in (int, float)]
            expires = min(expiries + ([authorization_expires_at] if authorization_expires_at is not None else [])) if expiries else self.now()
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT INTO requests(request_id,digest,plan,expected,status,result,created,authorization_expires_at) VALUES (?,?,?,?,?,?,?,?)',
                    (request_id, digest, _json(plan), _json(expected_states), 'preparing', None, self.now(), expires))
                busy = False
                if reason is None:
                    busy = any(db.execute('SELECT request_id FROM entity_owners WHERE entity_id=?',
                        (step['entity_id'],)).fetchone() is not None for step in plan)
                    # Request and complete step list are one transaction. A
                    # process cannot leave a valid empty plan that looks done.
                    for i, step in enumerate(plan):
                        db.execute('INSERT INTO steps(request_id,ordinal,entity_id,service,data,status) VALUES (?,?,?,?,?,?)',
                            (request_id, i, step['entity_id'], step['service'], _json(step['data']), 'not_dispatched'))
                        if not busy:
                            db.execute('INSERT INTO entity_owners VALUES (?,?)', (step['entity_id'], request_id))
            owner = self._claim(request_id)
            try:
                if reason: return self._finish(request_id, 'rejected', reason, owner=owner)
                if busy: return self._finish(request_id, 'rejected', 'entity_busy', owner=owner)
                # Reject all known impossible/precondition failures before the
                # first service, rather than discover them halfway through.
                for step in plan:
                    try:
                        reason = self._precondition(step['entity_id'], expected_states[step['entity_id']])
                        reason = reason or self._capability_reason(step, expected_states[step['entity_id']])
                    except UnknownEntity: reason = 'unknown_entity'
                    except Exception as exc: reason = 'preflight_unavailable:' + type(exc).__name__
                    if reason: return self._finish(request_id, 'rejected', reason, owner=owner)
                return self._run_owned(request_id, owner)
            finally:
                self._release(request_id, owner)

    def reconcile(self, request_id):
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            return dict(request_id=request_id, status='rejected', reason='invalid_request_id', steps=[], duplicate=False)
        with self._request_guard(request_id) as acquired:
            with self._db() as db:
                request = db.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
            if request is None:
                return dict(request_id=request_id, status='rejected', reason='unknown_request_id', steps=[], duplicate=False)
            if request['operator_resolution'] or request['status'] in ('succeeded', 'verified_after_unknown', 'rejected'):
                return {**self._receipt_from_row(request), 'duplicate': True}
            if not acquired:
                return dict(request_id=request_id, status='unknown', reason='execution_in_progress', steps=[], duplicate=True)
            owner = self._claim(request_id)
            try:
                return self._run_owned(request_id, owner, recovering=True)
            finally:
                self._release(request_id, owner)
