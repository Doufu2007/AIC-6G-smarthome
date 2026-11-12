"""Integrated v4 local gateway: recovery, persistent MQTT receipts and traceable strategy."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'docs/demo_v3'))
sys.path.insert(0, str(ROOT / 'SenseHome-6G/net_offload'))
import run_live as v3

MODES = {'local-first': 'static-local', 'second-first': 'static-cloud', 'joint': 'joint'}
INITIAL = ROOT / 'docs/experiments/20261111_race_v3_portfix/initial_state.json'
RESULTS = ROOT / 'docs/experiments_v4/results.json'
DATA_SCHEMA = 'sensehome.integrated-data.v1'


class PersistentDataDirectory:
    """Hold the deployment lock until every owned process has stopped.

    The operating system releases the lock after a crash; a leftover lock file
    is not evidence of a live owner. Unknown schema versions fail closed.
    """
    def __init__(self, path, *, mqtt):
        self.path = Path(path).resolve()
        self.mode = 'mqtt-software-entity' if mqtt else 'pc-json-simulation'
        self.handle = None
        self.resumed = False

    def __enter__(self):
        self.path.mkdir(parents=True, exist_ok=True)
        self.handle = (self.path / '.instance.lock').open('a+b')
        try:
            self.handle.seek(0, os.SEEK_END)
            if self.handle.tell() == 0:
                self.handle.write(b'\0')
                self.handle.flush()
            self.handle.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError('DATA_DIRECTORY_ALREADY_IN_USE:' + str(self.path)) from exc
            schema_path = self.path / 'schema.json'
            self.resumed = schema_path.is_file()
            if self.resumed:
                try:
                    schema = json.loads(schema_path.read_text(encoding='utf-8'))
                except (ValueError, OSError) as exc:
                    raise ValueError('UNSUPPORTED_DATA_SCHEMA') from exc
                if (not isinstance(schema, dict) or schema.get('schema') != DATA_SCHEMA
                        or type(schema.get('version')) is not int or schema['version'] != 1):
                    raise ValueError('UNSUPPORTED_DATA_SCHEMA')
                if schema.get('execution_mode') != self.mode or schema.get('platform_id') != 'software-home':
                    raise ValueError('DATA_DIRECTORY_EXECUTION_MODE_MISMATCH')
            else:
                if any(p.name != '.instance.lock' for p in self.path.iterdir()):
                    raise ValueError('UNVERSIONED_DATA_DIRECTORY_NOT_EMPTY')
                temporary = self.path / ('schema.' + secrets.token_hex(4) + '.tmp')
                try:
                    v3.write(temporary, {'schema': DATA_SCHEMA, 'version': 1,
                        'execution_mode': self.mode, 'platform_id': 'software-home', 'created_at_utc': v3.now()})
                    os.replace(temporary, schema_path)
                finally:
                    temporary.unlink(missing_ok=True)
            return self
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def __exit__(self, *args):
        self.close()


def resolve_strategy(value='auto'):
    if value in MODES:
        return value, {'kind': 'operator_explicit', 'scope': 'same recovery wrapper; initial choice only'}
    if value != 'auto':
        raise ValueError('INVALID_STRATEGY')
    if RESULTS.is_file():
        results = json.loads(RESULTS.read_text(encoding='utf-8'))
        chosen = results.get('recommendation', {}).get('arm')
        if chosen not in MODES or results.get('runner_failed') != 0:
            raise ValueError('EXPERIMENT_RECOMMENDATION_NOT_READY')
        return chosen, {'kind': 'registered_test_provisional_default',
            'source': RESULTS.relative_to(ROOT).as_posix(), 'sha256': v3.digest(RESULTS),
            'scope': results['recommendation']['scope'],
            'rule': results['recommendation']['rule']}
    return 'local-first', {'kind': 'engineering_default_before_comparison_available',
        'scope': 'simple local-first, no measured optimality claim'}


class IntegratedSession:
    def __init__(self, port=8879, models=True, mqtt=True, strategy='auto', model_config=v3.DEFAULT,
                 data_dir=None):
        self.stack = ExitStack()
        self.session = None
        self.mqtt = None
        self.environment_before = {}
        self.manifest = {}
        self._closed = False
        self.data_dir = Path(data_dir).resolve() if data_dir is not None else None
        run_parent = ROOT / '_work/demo_v4'
        run_parent.mkdir(parents=True, exist_ok=True)
        self.run_dir = run_parent / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + secrets.token_hex(4))
        self.run_dir.mkdir(exist_ok=False)
        try:
            selected, source = resolve_strategy(strategy)
            persistence = {'enabled': self.data_dir is not None}
            if self.data_dir is not None:
                data = self.stack.enter_context(PersistentDataDirectory(self.data_dir, mqtt=mqtt))
                persistence.update(schema=DATA_SCHEMA, version=1, data_directory=str(self.data_dir),
                    resumed=data.resumed, bridge_state_directory=str(self.data_dir / 'bridge'),
                    mqtt_data_directory=str(self.data_dir / 'mqtt') if mqtt else None,
                    command_recovery='lookup-only; no automatic command submission')
            if mqtt:
                from mqtt_receipts import LocalMQTTSession
                self.mqtt = self.stack.enter_context(LocalMQTTSession(self.run_dir / 'mqtt',
                    initial_state=json.loads(INITIAL.read_text(encoding='utf-8')),
                    platform_id='software-home',
                    data_dir=self.data_dir / 'mqtt' if self.data_dir is not None else None))
                self.environment_before = {key: os.environ.get(key) for key in self.mqtt.environment}
                os.environ.update(self.mqtt.environment)
            # v3 owns its own private runtime and state directory, including all
            # child process cleanup. Environment applies only during startup.
            try:
                self.session = v3.make_live_server(port=port, models=models, model_config=model_config,
                    state_dir=self.data_dir / 'bridge' if self.data_dir is not None else None)
            finally:
                self.restore_environment()
            if mqtt:
                # The entity's current snapshot is the device fact source.
                # Historical receipts and bridge cache must not overwrite it.
                snapshot = self.session.bridge._sync_mqtt_state()
                if self.data_dir is not None and snapshot is None:
                    raise RuntimeError('PERSISTENT_MQTT_STARTUP_RECONCILIATION_FAILED')
                persistence['startup_reconciliation'] = {
                    'status': 'observed' if snapshot is not None else 'unavailable',
                    'state_version': snapshot['state_version'] if snapshot is not None else None,
                    'source': 'current MQTT software entity snapshot'}
            if models:
                self.session.policy.arm = MODES[selected]
            self.manifest = {**self.session.manifest,
                'schema': 'sensehome.integrated-live.v4', 'strategy': selected if models else 'rules-only',
                'strategy_source': source, 'mqtt_enabled': mqtt,
                'persistence': persistence,
                'execution_scope': 'mqtt-software-entity' if mqtt else 'pc-json-simulation',
                'physical_execution_confirmed': False,
                'v4_run_dir': self.run_dir.relative_to(ROOT).as_posix(),
                'legacy_run_manifest': (self.session.run_dir / 'run_manifest.json').relative_to(ROOT).as_posix(),
                'source_sha256_v4': {p.relative_to(ROOT).as_posix(): v3.digest(p) for p in
                    [Path(__file__), ROOT / 'SenseHome-6G/net_offload/durable_receipts.py',
                     ROOT / 'SenseHome-6G/net_offload/mqtt_receipts.py']}}
            self.save()
        except BaseException as exc:
            self.close(failure=exc)
            raise

    @property
    def server(self):
        return self.session.server

    @property
    def bridge(self):
        return self.session.bridge

    def save(self):
        v3.write(self.run_dir / 'integrated_manifest.json', self.manifest)

    def restore_environment(self):
        for key, previous in self.environment_before.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
        self.environment_before = {}

    def receipt(self):
        return {**self.manifest, 'shutdown_token': self.session.shutdown_token}

    def close(self, failure=None):
        if self._closed:
            return
        self._closed = True
        self.restore_environment()
        errors = []
        try:
            if self.session is not None:
                self.session.close(failure=failure)
        except BaseException as exc:
            errors.append(exc)
        finally:
            try:
                self.stack.close()
            except BaseException as exc:
                errors.append(exc)
        self.manifest.update(status='failed' if failure or errors else 'stopped', finished_at_utc=v3.now())
        if failure:
            self.manifest['failure'] = type(failure).__name__ + ':' + str(failure)
        if errors:
            self.manifest['cleanup_errors'] = [type(exc).__name__ + ':' + str(exc) for exc in errors]
        if self.session is not None:
            self.manifest['legacy_final_status'] = self.session.manifest['status']
            self.manifest['original_state_and_events_unchanged'] = self.session.manifest['original_state_and_events_unchanged']
        self.save()
        if errors:
            raise errors[0]

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        self.close(failure=error)


def make_live_server(port=8879, models=True, **kwargs):
    return IntegratedSession(port=port, models=models, **kwargs)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', type=int, default=8879)
    p.add_argument('--rules-only', action='store_true')
    p.add_argument('--json-only', action='store_true')
    p.add_argument('--strategy', choices=['auto', *MODES], default='auto')
    p.add_argument('--data-dir', type=Path,
                   help='stable deployment data directory; omitted starts a fresh isolated demonstration')
    args = p.parse_args()
    with make_live_server(args.port, not args.rules_only, mqtt=not args.json_only, strategy=args.strategy,
                          data_dir=args.data_dir) as session:
        print(json.dumps(session.receipt(), ensure_ascii=True), flush=True)
        try:
            session.server.serve_forever(poll_interval=.1)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
