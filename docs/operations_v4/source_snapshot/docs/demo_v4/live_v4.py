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
    def __init__(self, port=8879, models=True, mqtt=True, strategy='auto', model_config=v3.DEFAULT):
        self.stack = ExitStack()
        self.session = None
        self.mqtt = None
        self.environment_before = {}
        self.manifest = {}
        run_parent = ROOT / '_work/demo_v4'
        run_parent.mkdir(parents=True, exist_ok=True)
        self.run_dir = run_parent / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + secrets.token_hex(4))
        self.run_dir.mkdir(exist_ok=False)
        try:
            selected, source = resolve_strategy(strategy)
            if mqtt:
                from mqtt_receipts import LocalMQTTSession
                self.mqtt = self.stack.enter_context(LocalMQTTSession(self.run_dir / 'mqtt',
                    initial_state=json.loads(INITIAL.read_text(encoding='utf-8')),
                    platform_id='software-home'))
                self.environment_before = {key: os.environ.get(key) for key in self.mqtt.environment}
                os.environ.update(self.mqtt.environment)
            # v3 owns its own private runtime and state directory, including all
            # child process cleanup. Environment applies only during startup.
            try:
                self.session = v3.make_live_server(port=port, models=models, model_config=model_config)
            finally:
                self.restore_environment()
            if models:
                self.session.policy.arm = MODES[selected]
            self.manifest = {**self.session.manifest,
                'schema': 'sensehome.integrated-live.v4', 'strategy': selected if models else 'rules-only',
                'strategy_source': source, 'mqtt_enabled': mqtt,
                'execution_scope': 'mqtt-software-entity' if mqtt else 'pc-json-simulation',
                'physical_execution_confirmed': False,
                'v4_run_dir': self.run_dir.relative_to(ROOT).as_posix(),
                'legacy_run_manifest': (self.session.run_dir / 'run_manifest.json').relative_to(ROOT).as_posix(),
                'source_sha256_v4': {p.relative_to(ROOT).as_posix(): v3.digest(p) for p in
                    [Path(__file__), ROOT / 'SenseHome-6G/net_offload/durable_receipts.py',
                     ROOT / 'SenseHome-6G/net_offload/mqtt_receipts.py']}}
            self.save()
        except BaseException:
            self.close()
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

    def close(self):
        self.restore_environment()
        try:
            if self.session is not None:
                self.session.close()
        finally:
            self.stack.close()
        self.manifest.update(status='stopped', finished_at_utc=v3.now())
        if self.session is not None:
            self.manifest['legacy_final_status'] = self.session.manifest['status']
            self.manifest['original_state_and_events_unchanged'] = self.session.manifest['original_state_and_events_unchanged']
        self.save()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def make_live_server(port=8879, models=True, **kwargs):
    return IntegratedSession(port=port, models=models, **kwargs)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--port', type=int, default=8879)
    p.add_argument('--rules-only', action='store_true')
    p.add_argument('--json-only', action='store_true')
    p.add_argument('--strategy', choices=['auto', *MODES], default='auto')
    args = p.parse_args()
    with make_live_server(args.port, not args.rules_only, mqtt=not args.json_only, strategy=args.strategy) as session:
        print(json.dumps(session.receipt(), ensure_ascii=True), flush=True)
        try:
            session.server.serve_forever(poll_interval=.1)
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
