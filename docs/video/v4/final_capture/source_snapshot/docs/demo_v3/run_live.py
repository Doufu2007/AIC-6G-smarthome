"""Serve the repaired dashboard on private PC state and literal loopback.

Default rules-only needs no model process. --models owns two pinned local
model children and adds once-only alternate planning after the original C
selection. Both candidates share this PC; devices remain JSON simulation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import secrets
import sys
import threading

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
WORK = ROOT / '_work/demo_v3'
DASHBOARD = ROOT / 'openvela_smart_home/dashboard'
CALIBRATION = ROOT / 'docs/experiments/20261111_fused_real/reviewed_20_sealed_repeat3/calibration.json'
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / 'docs/trial_v2'))
from run_isolated_gateway import production_bridge, make_isolated_server  # noqa: E402
from runtime import DEFAULT, LocalRuntime  # noqa: E402


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def private_directory(value=None):
    directory = Path(value) if value is not None else WORK / ('live_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + secrets.token_hex(4))
    directory = directory.resolve()
    if not directory.is_relative_to(WORK.resolve()) or directory == WORK.resolve():
        raise ValueError('RUN_DIRECTORY_MUST_BE_NEW_CHILD_OF_WORK_DEMO_V3')
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def direct_backend_class(bridge):
    """Reuse the existing HTTP adapter without initializing a second bridge."""
    alias = '_sensehome_live_fused_adapter'
    module_name = 'bridge_server'
    previous = sys.modules.get(module_name)
    try:
        sys.modules[module_name] = bridge
        spec = importlib.util.spec_from_file_location(alias, ROOT / 'docs/deployment/fused_eval.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.DirectModelBackend
    finally:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous


def serialize_recording(backend):
    """Keep this adapter's GET and POST slices separate during live polling.

    DirectModelBackend was written for sequential experiment calls. Its raw
    exchange list is shared by health and inference, so a live monitor must
    not insert GET rows into an in-progress inference attempt's evidence.
    """
    lock = threading.RLock()
    def guarded(call):
        def invoke(*args, **kwargs):
            with lock:
                return call(*args, **kwargs)
        return invoke
    backend.health = guarded(backend.health)
    backend.infer = guarded(backend.infer)
    return backend


class LiveSession:
    """Own only this run's server, monitor, C policy and optional model children."""
    def __init__(self, run_dir, models):
        self.run_dir = run_dir
        self.bridge = self.runtime = self.server = self.monitor = self.policy = None
        self.shutdown_token = None
        self.raw_backends = {}
        self._closed = False
        self._serving = threading.Event()
        self._protected = [ROOT / 'openvela_smart_home/device_state.json', ROOT / 'openvela_smart_home/offload_events.jsonl']
        self._before = {p.relative_to(ROOT).as_posix(): digest(p) for p in self._protected}
        sources = [DASHBOARD / 'bridge_server.py', DASHBOARD / 'device_actions.py', DASHBOARD / 'backend_monitor.py',
                   DASHBOARD / 'web_panel.html', ROOT / 'SenseHome-6G/net_offload/offload_policy.c',
                   ROOT / 'SenseHome-6G/net_offload/policy_cli.exe', HERE / 'runtime.py',
                   HERE / 'planning_recovery.py', HERE / 'run_live.py', ROOT / 'docs/deployment/fused_eval.py']
        self.manifest = {
            'schema': 'sensehome.isolated-live-dashboard.v3', 'started_at_utc': now(), 'status': 'starting',
            'binding': '127.0.0.1', 'mode': 'models-with-planning-recovery' if models else 'rules-only',
            'model_endpoints_configured': bool(models), 'patrol_interval_s': 0,
            'run_dir': run_dir.relative_to(ROOT).as_posix(), 'record_kind': 'developer_self_test',
            'scope': 'Original C selection and gateway executor; optional Python alternate planner; PC JSON devices. Two model slots share one local PC/GPU. No physical devices, external users, real WAN or 6G measurement.',
            'protected_before_sha256': self._before,
            'source_sha256': {p.relative_to(ROOT).as_posix(): digest(p) for p in sources},
            'health_source': 'actual backend health HTTP requests, cached by production BackendMonitor; unobserved queue, memory, jitter, loss and bandwidth remain unknown',
        }
        self.save()

    def save(self):
        write(self.run_dir / 'run_manifest.json', self.manifest)

    def initialize(self, port, models, model_config):
        self.bridge = bridge = production_bridge(self.run_dir)
        self.policy = bridge.gateway.policy
        calibration = json.loads(CALIBRATION.read_text(encoding='utf-8'))
        costs = calibration['inference_cost_ms']
        if costs != {'local': 1116, 'cloud': 1361}:
            raise ValueError('FROZEN_1116_1361_CALIBRATION_CHANGED')
        # This config belongs to the private C client, not a source-file edit.
        self.policy.config = {**self.policy.config, 'local_infer_ms': costs['local'], 'cloud_infer_ms': costs['cloud']}
        self.policy.arm = 'joint' if models else 'rules-only'
        self.manifest.update(policy_config=dict(self.policy.config), policy_arm=self.policy.arm,
            owned_policy_pid=self.policy.proc.pid,
            calibration_source=CALIBRATION.relative_to(ROOT).as_posix(), calibration_sha256=digest(CALIBRATION))

        if models:
            self.runtime = LocalRuntime(self.run_dir, config_path=model_config)
            self.runtime.__enter__()
            endpoints = self.runtime.endpoints()
            Backend = direct_backend_class(bridge)
            from planning_recovery import PlanningRecoveryBackend
            self.raw_backends = {
                tier: serialize_recording(Backend(endpoint['url'], endpoint['model'], tier, timeout_s=15,
                              revision=self.runtime.inventory.get('models', {}).get(tier, {}).get('sha256', 'unknown')))
                for tier, endpoint in endpoints.items()
            }
            for tier, other in (('local', 'cloud'), ('cloud', 'local')):
                bridge.gateway_backends[tier + '-model'] = PlanningRecoveryBackend(
                    self.raw_backends[tier], self.raw_backends[other], primary_name=tier + '-model',
                    secondary_name=other + '-model', primary_url=endpoints[tier]['url'],
                    secondary_url=endpoints[other]['url'], launch_budget_ms=10000)
            self.manifest.update(model_endpoints=endpoints, runtime_inventory=self.runtime.inventory,
                                 recovery_launch_budget_ms=10000,
                                 recovery_budget_scope='secondary launch only; HTTP calls retain their own timeouts')

        # Keep all three production references consistent. The old private
        # monitor has not been started by production_bridge.
        if bridge.backend_monitor is not None:
            bridge.backend_monitor.close()
        bridge.gateway.backends = dict(bridge.gateway_backends)
        self.monitor = bridge.BackendMonitor(bridge.gateway_backends, interval_s=2.0)
        bridge.backend_monitor = self.monitor
        health_sink = bridge.append_jsonl(str(self.run_dir / 'health_snapshots.jsonl'))
        refresh = self.monitor.refresh_once
        def record_health():
            refresh()
            health_sink({'recorded_at_utc': now(), 'scope': 'backend health; loopback model slots and PC simulation',
                         'snapshot': self.monitor.snapshot()})
        self.monitor.refresh_once = record_health
        self.monitor.refresh_once()
        self.monitor.start()

        self.server, self.shutdown_token = make_isolated_server(bridge, port)
        isolated_handler = self.server.RequestHandlerClass
        page = DASHBOARD / 'web_panel.html'
        class Handler(isolated_handler):
            def do_GET(self):
                if self.path in ('/', '/web_panel.html'):
                    if not self.allowed():
                        return self.send_json({'error': 'LOCAL_ORIGIN_REQUIRED'}, 403)
                    raw = page.read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(raw)))
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                return super().do_GET()
        self.server.RequestHandlerClass = Handler
        serve = self.server.serve_forever
        def serve_forever(poll_interval=.1):
            self._serving.set()
            try:
                return serve(poll_interval=poll_interval)
            finally:
                self._serving.clear()
        self.server.serve_forever = serve_forever
        self.manifest.update(status='ready', port=self.server.server_port,
                             gateway_url=f'http://127.0.0.1:{self.server.server_port}')
        self.save()

    def receipt(self):
        return {**self.manifest, 'shutdown_token': self.shutdown_token}

    def close(self, failure=None):
        if self._closed:
            return
        self._closed = True
        errors = []
        if self.server is not None:
            if self._serving.is_set():
                self.server.shutdown()
            self.server.server_close()
        if self.monitor is not None:
            self.monitor.close()
            thread = getattr(self.monitor, '_thread', None)
            if thread is not None:
                thread.join(timeout=35)
            self.manifest['monitor_thread_stopped'] = thread is None or not thread.is_alive()
            if not self.manifest['monitor_thread_stopped']:
                errors.append('OWN_MONITOR_THREAD_DID_NOT_STOP')
        else:
            self.manifest['monitor_thread_stopped'] = True
        if self.bridge is not None:
            self.bridge.close_gateway()
        if self.policy is not None:
            self.manifest['policy_process_exit_code'] = self.policy.proc.poll()
        if self.runtime is not None:
            try:
                self.runtime.close()
            except Exception as exc:
                errors.append('OWN_RUNTIME_CLOSE_' + type(exc).__name__)
        if self.raw_backends:
            write(self.run_dir / 'model_backend_records.json', {
                tier: {'metadata': backend.metadata, 'attempts': backend.attempts, 'probes': backend.probes}
                for tier, backend in self.raw_backends.items()})
            write(self.run_dir / 'planning_recovery_records.json', {
                name: backend.recovery_records for name, backend in self.bridge.gateway_backends.items()
                if hasattr(backend, 'recovery_records')})
        after = {p.relative_to(ROOT).as_posix(): digest(p) for p in self._protected}
        self.manifest.update(finished_at_utc=now(), protected_after_sha256=after,
            original_state_and_events_unchanged=after == self._before,
            status='failed' if failure or errors else 'stopped')
        if failure:
            self.manifest['failure'] = type(failure).__name__ + ':' + str(failure)
        if after != self._before:
            errors.append('ORIGINAL_STATE_OR_EVENTS_CHANGED')
            self.manifest['status'] = 'failed'
        if errors:
            self.manifest['cleanup_errors'] = errors
        self.save()
        if errors:
            raise RuntimeError(';'.join(errors))

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        self.close(failure=error)


def make_live_server(port=8879, models=False, run_dir=None, model_config=DEFAULT):
    """Return a bound LiveSession; the caller explicitly starts serve_forever."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('INVALID_PORT')
    session = LiveSession(private_directory(run_dir), models)
    try:
        session.initialize(port, models, model_config)
        return session
    except BaseException as exc:
        session.close(failure=exc)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8879)
    parser.add_argument('--models', action='store_true')
    parser.add_argument('--run-dir', type=Path)
    parser.add_argument('--model-config', type=Path, default=DEFAULT)
    args = parser.parse_args()
    with make_live_server(args.port, args.models, args.run_dir, args.model_config) as session:
        print(json.dumps(session.receipt(), ensure_ascii=True), flush=True)
        try:
            session.server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
