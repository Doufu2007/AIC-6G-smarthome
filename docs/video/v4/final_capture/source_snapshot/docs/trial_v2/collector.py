"""Local anonymous trial collection. No record is written before final consented submit."""
from __future__ import annotations

import argparse
from copy import deepcopy
import csv
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

HERE = Path(__file__).resolve().parent
KINDS = ('developer_self_test', 'automation_test', 'participant')
FEEDBACK_FIELDS = ('prior_usage', 'recent_incident_or_none', 'clear_or_confusing',
                   'expected_failure_message', 'local_cloud_preference', 'willing_to_retry',
                   'reason', 'degradation_restatement_verbatim')
PRESETS = {
    'T1': {'command': '打开客厅的灯', 'targets': [('lights', '客厅', 'is_on', True)]},
    'T2': {'command': '把客厅灯光设为60%，再把卧室灯光设为30%',
           'targets': [('lights', '客厅', 'brightness', 60), ('lights', '卧室', 'brightness', 30)]},
    'T4': {'command': '把阳台灯打开', 'targets': []},
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def validate_gateway(value):
    if not value:
        return None
    parsed = urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', '::1')
            or parsed.username or parsed.password or parsed.path not in ('', '/')
            or parsed.query or parsed.fragment):
        raise ValueError('GATEWAY_MUST_BE_LOOPBACK_HTTP_ORIGIN')
    try:
        if parsed.port is None or not 1 <= parsed.port <= 65535:
            raise ValueError('INVALID_GATEWAY_PORT')
    except ValueError:
        raise ValueError('INVALID_GATEWAY_PORT') from None
    return value.rstrip('/')


def text_value(value, maximum=1200):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError('INVALID_TEXT_LENGTH')
    return value.strip()


def csv_text(value):
    value = '' if value is None else str(value)
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('GATEWAY_REDIRECT_REFUSED')


def gateway_request(base, path, payload=None):
    """Fixed loopback paths only; system proxies and redirects are never followed."""
    if path not in ('/api/command', '/api/state'):
        raise ValueError('INVALID_GATEWAY_PATH')
    raw = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = Request(base + path, data=raw, headers={'Content-Type': 'application/json'})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    start = time.perf_counter()
    status = None
    error = None
    try:
        try:
            response = opener.open(request, timeout=25)
        except HTTPError as exc:
            response = exc
        with response:
            status = response.status
            content = response.read(2 * 1024 * 1024 + 1)
        if len(content) > 2 * 1024 * 1024:
            raise ValueError('UPSTREAM_RESPONSE_TOO_LARGE')
        decoded = json.loads(content.decode('utf-8'))
        if not isinstance(decoded, dict):
            raise ValueError('UPSTREAM_RESPONSE_NOT_OBJECT')
    except (OSError, ValueError, URLError) as exc:
        decoded = None
        error = type(exc).__name__
    return {'http_status': status, 'body': decoded, 'error': error,
            'wall_ms': round((time.perf_counter() - start) * 1000, 3)}


def validated_state_response(snapshot):
    """Return authoritative state only for a successful, expected device schema."""
    if snapshot.get('http_status') != 200 or snapshot.get('error'):
        return None
    state = snapshot.get('body')
    if not isinstance(state, dict) or state.get('error'):
        return None
    for group, boolean_key, numeric_key in (
            ('lights', 'is_on', 'brightness'), ('curtains', 'is_open', 'position')):
        devices = state.get(group)
        if not isinstance(devices, dict) or not devices:
            return None
        for room, device in devices.items():
            if (not isinstance(room, str) or not isinstance(device, dict)
                    or type(device.get(boolean_key)) is not bool
                    or type(device.get(numeric_key)) is not int
                    or not 0 <= device[numeric_key] <= 100):
                return None
    readings = state.get('temperature')
    if not isinstance(readings, dict) or not readings:
        return None
    for reading in readings.values():
        if not isinstance(reading, dict):
            return None
        for key in ('temp', 'humidity'):
            value = reading.get(key)
            if type(value) not in (int, float) or not math.isfinite(value):
                return None
        if not 0 <= reading['humidity'] <= 100:
            return None
    return state


def assess_response(status, payload, before, after, targets=None):
    """A claim or HTTP200 alone never proves a simulated state execution."""
    payload = payload or {}
    event = payload.get('gateway_event') or {}
    actions = event.get('actions', payload.get('actions', []))
    explicit_confirmation = (payload.get('simulated_execution_confirmed') is True
        or (event.get('response') or {}).get('simulated_execution_confirmed') is True
        or bool(event.get('execution', {}).get('semantic_complete')))
    # The gateway's semantic/executor confirmation and authoritative state are
    # separate evidence. We do not infer execution from initial target equality.
    state_changed = isinstance(before, dict) and isinstance(after, dict) and before != after
    complete_claim = payload.get('semantic_complete') is True and payload.get('interface_ok') is True
    confirmed = bool(status == 200 and actions and complete_claim and
                     (explicit_confirmation or state_changed) and before is not None and after is not None)
    match = None
    completed = None
    no_action = None
    if targets is not None and not targets:
        no_action = bool(before == after and not actions) if before is not None and after is not None else None
    elif targets:
        match = bool(after is not None and all(after.get(group, {}).get(room, {}).get(key) == target
                     for group, room, key, target in targets))
        remaining = list(actions) if isinstance(actions, list) else []
        plan_matches = len(remaining) == len(targets)
        for group, room, key, target in targets:
            matching = []
            for action in remaining:
                if not isinstance(action, dict) or action.get('name') != 'light_control' or group != 'lights':
                    continue
                params = action.get('params') or {}
                if params.get('location') != room:
                    continue
                if key == 'brightness' and params.get('brightness') == target:
                    matching.append(action)
                elif key == 'is_on' and ((target is True and params.get('action') == 'on')
                                         or (target is False and params.get('action') == 'off')):
                    matching.append(action)
            if not matching:
                plan_matches = False
            else:
                remaining.remove(matching[0])
        completed = bool(match and plan_matches and confirmed and not payload.get('error') and not payload.get('fallback_from'))
    return {'task_completed': completed, 'target_state_matches': match,
            'state_execution_confirmed': confirmed, 'empty_target_no_action_no_mutation': no_action,
            'scope': 'isolated PC simulated state; not physical device confirmation; self-authored tasks have no automatic oracle'}


class TrialStore:
    def __init__(self, directory, record_kind='developer_self_test', gateway=None):
        if record_kind not in KINDS:
            raise ValueError('INVALID_RECORD_KIND')
        self.directory = Path(directory).resolve()
        self.record_kind = record_kind
        self.gateway = validate_gateway(gateway)
        self.sessions = {}
        self.lock = threading.RLock()

    def _session(self, session_id):
        if not isinstance(session_id, str) or not re.fullmatch(r'[0-9a-f]{32}', session_id):
            raise ValueError('INVALID_SESSION_ID')
        if session_id not in self.sessions:
            raise ValueError('UNKNOWN_OR_CANCELLED_SESSION')
        return self.sessions[session_id]

    def _path(self, session_id):
        return self.directory / self.record_kind / (session_id + '.json')

    def start(self, payload):
        if payload.get('consent') is not True:
            raise ValueError('CONSENT_REQUIRED')
        role = payload.get('role', 'user')
        if role not in ('user', 'integrator', 'developer_self_test'):
            raise ValueError('INVALID_ROLE')
        with self.lock:
            if len(self.sessions) >= 128:
                raise ValueError('SESSION_LIMIT_REACHED')
            session_id = secrets.token_hex(16)
            session = {'schema': 'sensehome.anonymous-trial.v2', 'session_id': session_id,
                       'record_kind': self.record_kind, 'participant_role': role,
                       'consent': True, 'consent_at_utc': utc_now(),
                       'allow_anonymous_quote': payload.get('allow_quote') is True,
                       'mode': 'connected_pc_simulation' if self.gateway else 'practice_no_gateway',
                       'tasks': [], 'submitted': False}
            if self.record_kind != 'participant':
                session['participant_role'] = self.record_kind
            self.sessions[session_id] = session
            return deepcopy(session)

    def execute(self, session_id, payload):
        with self.lock:
            session = self._session(session_id)
            if session['submitted']:
                raise ValueError('SESSION_ALREADY_SUBMITTED')
            if len(session['tasks']) >= 20:
                raise ValueError('TASK_LIMIT_REACHED')
            command = text_value(payload.get('command'), 500)
            origin = payload.get('origin')
            if not command or origin not in ('preset', 'self_authored'):
                raise ValueError('COMMAND_AND_ORIGIN_REQUIRED')
            preset = None
            if origin == 'preset':
                preset = PRESETS.get(payload.get('preset_id'))
                if not preset or command != preset['command']:
                    raise ValueError('PRESET_COMMAND_MISMATCH')
            task = {'command': command, 'origin': origin, 'preset_id': payload.get('preset_id') if preset else None,
                    'started_at_utc': utc_now(), 'transport': 'practice_no_gateway', 'task_completed': None,
                    'state_execution_confirmed': False,
                    'warning': '未连接网关，仅练习输入；没有执行设备动作，不计技术验证。'}
            if self.gateway:
                start = time.perf_counter()
                before = gateway_request(self.gateway, '/api/state')
                response = gateway_request(self.gateway, '/api/command', {'command': command, 'context': {}})
                after = gateway_request(self.gateway, '/api/state')
                task.update(transport='actual_loopback_gateway_http', warning='仅PC模拟设备；不是物理执行器。',
                            state_before=before, gateway_response=response, state_after=after,
                            total_user_ms=round((time.perf_counter() - start) * 1000, 3))
                before_state, after_state = validated_state_response(before), validated_state_response(after)
                task['state_validation'] = {'before_valid': before_state is not None,
                    'after_valid': after_state is not None,
                    'rule': 'HTTP200, no transport/body error, nonempty lights/curtains/temperature device schema'}
                task.update(assess_response(response['http_status'], response['body'],
                    before_state, after_state, preset['targets'] if preset else None))
            session['tasks'].append(task)
            return deepcopy(task)

    def submit(self, session_id, feedback):
        with self.lock:
            session = self._session(session_id)
            if session['submitted']:
                raise ValueError('SESSION_ALREADY_SUBMITTED')
            clean = {key: text_value(feedback.get(key, '')) for key in FEEDBACK_FIELDS}
            if not any(clean.values()):
                raise ValueError('AT_LEAST_ONE_VERBATIM_ANSWER_REQUIRED')
            if feedback.get('confirm_submit') is False:
                raise ValueError('FINAL_SUBMIT_NOT_CONFIRMED')
            record = deepcopy(session)
            record.update(feedback=clean, submitted=True, submitted_at_utc=utc_now(),
                          submission_source='explicit_submit_request; operator record_kind controls cohort; not proof of human identity',
                          valid_connected_trial=bool(self.record_kind == 'participant'
                              and session['participant_role'] in ('user', 'integrator')
                              and any(task['transport'] == 'actual_loopback_gateway_http' for task in session['tasks'])))
            path = self._path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('x', encoding='utf-8') as stream:
                json.dump(record, stream, ensure_ascii=False, indent=2)
            session['submitted'] = True
            return record

    def cancel(self, session_id):
        with self.lock:
            self._session(session_id)
            path = self._path(session_id)
            if path.exists():
                path.unlink()
            del self.sessions[session_id]
            return {'status': 'discarded', 'session_id': session_id}

    def records(self):
        records = []
        if self.directory.is_dir():
            for kind in KINDS:
                for path in sorted((self.directory / kind).glob('*.json')):
                    try:
                        record = json.loads(path.read_text(encoding='utf-8'))
                        if record.get('record_kind') == kind and record.get('submitted') and record.get('consent'):
                            records.append(record)
                    except (OSError, ValueError):
                        continue
        return records

    def summary(self):
        records = self.records()
        participant = [record for record in records if record['record_kind'] == 'participant'
                       and record['participant_role'] in ('user', 'integrator')]
        return {'participant_feedback_count': len(participant),
                'valid_connected_trial_count': sum(bool(record.get('valid_connected_trial')) for record in participant),
                'developer_self_test_count': sum(record['record_kind'] == 'developer_self_test' for record in records),
                'automation_test_count': sum(record['record_kind'] == 'automation_test' for record in records),
                'human_identity_verified': False,
                'scope': 'anonymous explicit submissions; cohort is operator-declared; no inferred population statistics'}

    def export_json(self):
        records = self.records()
        return json.dumps({'schema': 'sensehome.trial-export.v2', 'summary': self.summary(),
                           'records': records}, ensure_ascii=False, indent=2)

    def export_csv(self):
        output = io.StringIO(newline='')
        fields = ('session_id', 'record_kind', 'participant_role', 'mode', 'valid_connected_trial',
                  'allow_anonymous_quote', 'submitted_at_utc', 'task_count', *FEEDBACK_FIELDS)
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for record in self.records():
            row = {key: record.get(key, '') for key in fields}
            row.update(record['feedback'], task_count=len(record['tasks']))
            writer.writerow({key: csv_text(value) for key, value in row.items()})
        return output.getvalue()


def make_server(store, port=8877, *, page_path=None):
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = 'SenseHomeTrial/2'
        sys_version = ''

        def log_message(self, *args):
            pass  # Deliberately do not retain request IP, headers or feedback in console logs.

        def reply(self, status, content, mime='application/json; charset=utf-8'):
            raw = content.encode('utf-8') if isinstance(content, str) else json.dumps(content, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(raw)

        def allowed_host(self):
            return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

        def authorized(self):
            origin = self.headers.get('Origin')
            allowed_origin = origin is None or origin in (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}')
            return self.allowed_host() and allowed_origin and secrets.compare_digest(self.headers.get('X-Collector-Token', ''), token)

        def do_GET(self):
            if not self.allowed_host():
                return self.reply(403, {'error': 'HOST_REFUSED'})
            if self.path == '/':
                html = (Path(page_path) if page_path else HERE / 'index.html').read_text(encoding='utf-8').replace('__COLLECTOR_TOKEN__', token)
                return self.reply(200, html, 'text/html; charset=utf-8')
            if not self.authorized():
                return self.reply(403, {'error': 'TOKEN_REQUIRED'})
            if self.path == '/api/config':
                return self.reply(200, {'record_kind': store.record_kind, 'gateway_connected': bool(store.gateway),
                                        'presets': {key: value['command'] for key, value in PRESETS.items()},
                                        'system_provenance': getattr(store, 'provenance', {}),
                                        'summary': store.summary()})
            if self.path == '/api/export.json':
                return self.reply(200, store.export_json())
            if self.path == '/api/export.csv':
                return self.reply(200, '\ufeff' + store.export_csv(), 'text/csv; charset=utf-8')
            return self.reply(404, {'error': 'NOT_FOUND'})

        def do_POST(self):
            if not self.authorized():
                return self.reply(403, {'error': 'LOCAL_TOKEN_AND_ORIGIN_REQUIRED'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16_384:
                    raise ValueError('INVALID_REQUEST_SIZE')
                payload = json.loads(self.rfile.read(length).decode('utf-8'))
                if not isinstance(payload, dict):
                    raise ValueError('REQUEST_MUST_BE_OBJECT')
                if self.path == '/api/start':
                    result = store.start(payload)
                elif self.path == '/api/baseline' and hasattr(store, 'baseline'):
                    result = store.baseline(payload.get('session_id'), payload)
                elif self.path == '/api/task':
                    result = store.execute(payload.get('session_id'), payload)
                elif self.path == '/api/submit':
                    if payload.get('confirm_submit') is not True:
                        raise ValueError('FINAL_SUBMIT_NOT_CONFIRMED')
                    result = store.submit(payload.get('session_id'), payload)
                elif self.path == '/api/cancel':
                    result = store.cancel(payload.get('session_id'))
                else:
                    return self.reply(404, {'error': 'NOT_FOUND'})
                return self.reply(200, result)
            except (UnicodeError, ValueError, TypeError, OSError):
                return self.reply(400, {'error': '请求无效，或会话已提交/撤回。请检查同意、字数与会话状态。'})

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.collector_token = token
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8877)
    parser.add_argument('--record-kind', choices=KINDS, default='developer_self_test')
    parser.add_argument('--gateway', help='Explicit local HTTP origin, e.g. http://127.0.0.1:8080')
    parser.add_argument('--data-dir', type=Path, default=HERE / 'data')
    args = parser.parse_args()
    store = TrialStore(args.data_dir, args.record_kind, args.gateway)
    server = make_server(store, args.port)
    print(json.dumps({'url': f'http://127.0.0.1:{server.server_port}', 'record_kind': store.record_kind,
                      'mode': 'connected_pc_simulation' if store.gateway else 'practice_no_gateway',
                      'participant_feedback_count': store.summary()['participant_feedback_count']}, ensure_ascii=True), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
