"""Full-model local trial with pre-task needs capture; no automatic human claims."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sys
import threading

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'docs/demo_v3'))
sys.path.insert(0, str(ROOT / 'docs/trial_v2'))
sys.path.insert(0, str(ROOT / 'docs/demo_v4'))
from collector import TrialStore, KINDS, make_server, text_value, utc_now
from live_v4 import make_live_server
from run_live import digest

BASELINE_FIELDS = ('prior_usage', 'recent_incident_or_none', 'impact',
                   'desired_improvement', 'acceptable_wait')


class DemandTrialStore(TrialStore):
    def __init__(self, directory, record_kind='developer_self_test', gateway=None, provenance=None, on_cancel=None):
        super().__init__(directory, record_kind, gateway)
        self.provenance = deepcopy(provenance or {})
        self.on_cancel = on_cancel
        self.started_once = False
        self.saved = False

    def start(self, payload):
        with self.lock:
            if self.started_once:
                raise ValueError('ONE_SESSION_PER_RUN_RESTART_FOR_NEXT_PARTICIPANT')
            result = super().start(payload)
            self.started_once = True
            session = self._session(result['session_id'])
            session.update(schema='sensehome.anonymous-trial.v4',
                           system_provenance=deepcopy(self.provenance), baseline=None)
            return deepcopy(session)

    def submit(self, session_id, feedback):
        with self.lock:
            value = super().submit(session_id, feedback)
            self.saved = True
            return value

    def cancel(self, session_id):
        value = super().cancel(session_id)
        self.saved = False
        if self.on_cancel:
            self.on_cancel()
            value['runtime_cleanup'] = 'scheduled_after_services_stop'
        return value

    def baseline(self, session_id, payload):
        with self.lock:
            session = self._session(session_id)
            if session['submitted'] or session['tasks']:
                raise ValueError('BASELINE_FROZEN_AFTER_FIRST_TASK')
            clean = {key: text_value(payload.get(key, '')) for key in BASELINE_FIELDS}
            if not any(clean.values()) and payload.get('skipped') is not True:
                raise ValueError('BASELINE_ANSWER_OR_EXPLICIT_SKIP_REQUIRED')
            session['baseline'] = {**clean, 'explicitly_skipped': payload.get('skipped') is True,
                'recorded_at_utc': utc_now(), 'phase': 'before_first_task',
                'source': 'participant_verbatim_input; operator must verify actual recruitment'}
            return deepcopy(session['baseline'])

    def execute(self, session_id, payload):
        with self.lock:
            if self._session(session_id).get('baseline') is None:
                raise ValueError('BASELINE_REQUIRED_BEFORE_TASK')
        return super().execute(session_id, payload)


def erase_private_runs(paths):
    """Delete only exact owned children after all state-producing services stop."""
    roots = [(ROOT / '_work/demo_v3').resolve(), (ROOT / '_work/demo_v4').resolve()]
    resolved = [Path(p).resolve() for p in paths]
    for path in resolved:
        if not any(path != root and path.is_relative_to(root) for root in roots):
            raise ValueError('REFUSE_NON_PRIVATE_RUN_ERASURE')
        if path.is_symlink():
            raise ValueError('REFUSE_LINKED_RUN_ERASURE')
    for path in resolved:
        if path.exists():
            shutil.rmtree(path)
    return {'schema': 'sensehome.trial-erasure.v4', 'completed_at_utc': utc_now(),
            'owned_directories_removed': len(resolved), 'all_absent': all(not p.exists() for p in resolved),
            'method': 'ordinary file deletion; no disk overwrite guarantee', 'participant_commands_retained': False}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--record-kind', choices=KINDS, default='developer_self_test')
    p.add_argument('--port', type=int, default=8877)
    p.add_argument('--gateway-port', type=int, default=8879)
    p.add_argument('--rules-only', action='store_true', help='Explicit reduced-capability mode; no models')
    p.add_argument('--data-dir', type=Path, default=HERE / 'data')
    return p


def main():
    args = parser().parse_args()
    session = make_live_server(port=args.gateway_port, models=not args.rules_only)
    thread = threading.Thread(target=session.server.serve_forever, daemon=True)
    thread.start()
    server = None
    store = None
    stopped = threading.Event()
    try:
        provenance = {'models_enabled': not args.rules_only,
            'mode': session.manifest['mode'],
            'run_manifest': (session.run_dir / 'integrated_manifest.json').relative_to(ROOT).as_posix(),
            'runtime_manifest': ((session.session.run_dir / 'runtime_manifest.json').relative_to(ROOT).as_posix()
                                 if not args.rules_only else None),
            'dashboard_sha256': digest(ROOT / 'openvela_smart_home/dashboard/web_panel.html'),
            'gateway_url': session.manifest['gateway_url'],
            'scope': 'same-PC model slots; isolated software state, not physical devices or 6G'}
        def cancel_runtime():
            stopped.set()
            # shutdown must run outside the request thread.
            threading.Thread(target=server.shutdown, daemon=True).start()
        store = DemandTrialStore(args.data_dir, args.record_kind, session.manifest['gateway_url'], provenance, cancel_runtime)
        server = make_server(store, args.port, page_path=HERE / 'index.html')
        info = {'url': f'http://127.0.0.1:{server.server_port}', 'record_kind': args.record_kind,
                'models_enabled': not args.rules_only, 'model_startup_failure_is_fatal': True,
                'gateway_url': session.manifest['gateway_url'],
                'data_directory': str(args.data_dir), 'external_feedback': store.summary(),
                'scope': provenance['scope']}
        print(json.dumps(info, ensure_ascii=True), flush=True)
        try:
            server.serve_forever(poll_interval=.1)
        except KeyboardInterrupt:
            pass
    finally:
        if server is not None:
            server.server_close()
        session.close()
        thread.join(timeout=5)
        if store is None or not store.saved:
            if store is not None:
                for sid in list(store.sessions):
                    path = store._path(sid)
                    if path.is_file():
                        path.unlink()
                store.sessions.clear()
            receipt = erase_private_runs([session.run_dir, session.session.run_dir])
            # No path, command, quote or participant identity in this receipt.
            print(json.dumps({'private_run_cleanup': receipt}, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
