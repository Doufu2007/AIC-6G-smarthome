"""Run the unchanged production bridge on private PC state and loopback only.

No model endpoints are inherited. Complex tasks can therefore fail honestly.
The optional collector uses this local gateway without physical devices.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sys
import threading

from collector import TrialStore, make_server

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / '_work/trial_v2'
DASHBOARD = ROOT / 'openvela_smart_home/dashboard'
sys.path.insert(0, str(DASHBOARD))
from bounded_http import BoundedHTTPServer


def digest_if_present(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def production_bridge(run_dir, state_dir=None):
    """Override process-local configuration before module initialization."""
    sys.path.insert(0, str(DASHBOARD))
    changes = {'SENSEHOME_LOCAL_URL': '', 'SENSEHOME_CLOUD_URL': '',
               'SENSEHOME_EVENT_LOG': str(run_dir / 'offload_events.jsonl'),
               'SENSEHOME_POLICY_CLI': str(ROOT / 'SenseHome-6G/net_offload/policy_cli.exe'),
               'SENSEHOME_SERVICE_PROFILES': str(ROOT / 'SenseHome-6G/net_offload/experiments/service_profiles.json'),
               'SENSEHOME_POLICY_CONFIG': str(ROOT / 'SenseHome-6G/net_offload/experiments/policy_config.json'),
               'SENSEHOME_POLICY_ARM': 'joint'}
    previous = {key: os.environ.get(key) for key in changes}
    try:
        os.environ.update(changes)
        spec = importlib.util.spec_from_file_location('isolated_trial_bridge', DASHBOARD / 'bridge_server.py')
        bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bridge)
        bridge.close_gateway()
        state_directory = Path(state_dir) if state_dir is not None else run_dir
        state_directory.mkdir(parents=True, exist_ok=True)
        bridge.STATE_FILE = str(state_directory / 'device_state.json')
        bridge.REQUEST_DB = str(state_directory / 'command_requests.sqlite3')
        bridge.PATROL_INTERVAL = 0
        bridge.patrol_running = False
        bridge._init_gateway(start_monitor=False)
        # No background polling, network injection, patrol or preset handler is
        # added. These are the existing C policy, local rules and executor.
        if bridge.gateway is None:
            raise RuntimeError('PRODUCTION_GATEWAY_UNAVAILABLE:' + str(bridge.gateway_init_error))
        if not Path(bridge.STATE_FILE).exists():
            bridge.save_state(bridge.default_state())
        return bridge
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def make_isolated_server(bridge, port):
    shutdown_token = secrets.token_urlsafe(32)

    class Handler(bridge.BridgeHandler):
        def log_message(self, *args):
            pass

        def send_json(self, data, status=200):
            raw = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def allowed(self):
            expected = f'127.0.0.1:{self.server.server_port}'
            return (self.headers.get('Host') in (expected, f'localhost:{self.server.server_port}')
                    and self.headers.get('Origin') in (None, 'http://' + expected,
                        f'http://localhost:{self.server.server_port}'))

        def do_GET(self):
            if not self.allowed():
                return self.send_json({'error': 'LOCAL_ORIGIN_REQUIRED'}, 403)
            if (self.path not in ('/api/state', '/api/health', '/api/patrol/log', '/api/patrol/trigger')
                    and not self.path.startswith(('/api/receipts/', '/api/requests/'))):
                return self.send_json({'error': 'ROUTE_DISABLED_IN_ISOLATED_TRIAL'}, 404)
            return super().do_GET()

        def do_POST(self):
            if not self.allowed():
                return self.send_json({'error': 'LOCAL_ORIGIN_REQUIRED'}, 403)
            if self.path == '/__shutdown':
                if not secrets.compare_digest(self.headers.get('X-Isolated-Shutdown', ''), shutdown_token):
                    return self.send_json({'error': 'SHUTDOWN_TOKEN_REQUIRED'}, 403)
                self.send_json({'status': 'closing'}, 202)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path not in ('/api/command', '/api/patrol/trigger'):
                return self.send_json({'error': 'ROUTE_DISABLED_IN_ISOLATED_TRIAL'}, 404)
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.send_json({'error': 'JSON_REQUIRED'}, 400)
            return super().do_POST()

    server = BoundedHTTPServer(('127.0.0.1', port), Handler)
    return server, shutdown_token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8878)
    parser.add_argument('--with-collector', action='store_true')
    parser.add_argument('--collector-port', type=int, default=8877)
    parser.add_argument('--record-kind', choices=('developer_self_test', 'automation_test', 'participant'), default='developer_self_test')
    parser.add_argument('--run-dir', type=Path)
    args = parser.parse_args()
    run_dir = (args.run_dir or WORK / ('run_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + secrets.token_hex(3))).resolve()
    if not run_dir.is_relative_to(WORK.resolve()) or run_dir == WORK.resolve():
        raise ValueError('RUN_DIRECTORY_MUST_BE_NEW_CHILD_OF_WORK_TRIAL_V2')
    run_dir.mkdir(parents=True, exist_ok=False)
    protected = [ROOT / 'openvela_smart_home/device_state.json', ROOT / 'openvela_smart_home/offload_events.jsonl']
    protected_before = {str(path.relative_to(ROOT)): digest_if_present(path) for path in protected}
    bridge = production_bridge(run_dir)
    server, shutdown_token = make_isolated_server(bridge, args.port)
    collector_server = None
    receipt = {'schema': 'sensehome.isolated-production-trial.v2',
               'started_at_utc': datetime.now(timezone.utc).isoformat(),
               'binding': '127.0.0.1', 'port': server.server_port,
               'gateway_url': f'http://127.0.0.1:{server.server_port}',
               'model_endpoints_configured': False, 'patrol_interval_s': 0,
               'scope': 'unchanged production C policy, rules and executor; isolated PC simulation; complex T2 may fail without models',
               'run_dir': str(run_dir.relative_to(ROOT)), 'protected_before_sha256': protected_before,
               'source_sha256': {str(path.relative_to(ROOT)): digest_if_present(path) for path in
                   (DASHBOARD / 'bridge_server.py', DASHBOARD / 'device_actions.py',
                    ROOT / 'SenseHome-6G/net_offload/offload_policy.c', ROOT / 'SenseHome-6G/net_offload/policy_cli.exe')}}
    try:
        if args.with_collector:
            store = TrialStore(Path(__file__).with_name('data'), args.record_kind, receipt['gateway_url'])
            collector_server = make_server(store, args.collector_port)
            receipt['collector_url'] = f'http://127.0.0.1:{collector_server.server_port}'
            receipt['collector_record_kind'] = args.record_kind
            threading.Thread(target=server.serve_forever, daemon=True).start()
        (run_dir / 'run_manifest.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({**receipt, 'shutdown_token': shutdown_token}, ensure_ascii=True), flush=True)
        if collector_server:
            collector_server.serve_forever()
        else:
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if collector_server:
            collector_server.server_close()
            server.shutdown()
        server.server_close()
        bridge.close_gateway()
        receipt['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        receipt['protected_after_sha256'] = {str(path.relative_to(ROOT)): digest_if_present(path) for path in protected}
        receipt['original_state_and_events_unchanged'] = receipt['protected_after_sha256'] == protected_before
        (run_dir / 'run_manifest.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        if not receipt['original_state_and_events_unchanged']:
            raise RuntimeError('ORIGINAL_FILES_CHANGED_DURING_ISOLATED_RUN')


if __name__ == '__main__':
    main()
