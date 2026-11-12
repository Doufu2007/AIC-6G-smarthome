"""A single registered comparison of model failure after inventory checks."""
from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
import dataclasses
import json
from pathlib import Path
import random
import shutil
import statistics
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'docs/deployment'))
import fused_eval as f
from real_fault_eval_v2 import ControlledProxy
from runtime import LocalRuntime, now, write, digest

ARMS = ('baseline', 'recovery')
CONDITIONS = ('healthy', 'local_post503', 'both_post503')
OUT = ROOT / 'docs/experiments/20261111_race_v3'


def schedule():
    jobs = []
    for condition in CONDITIONS:
        for repeat in range(3):
            for j, case_id in enumerate(('C01', 'C04')):
                jobs.append({'condition': condition, 'repeat': repeat, 'case_id': case_id,
                             'arms': list(ARMS if (repeat + j) % 2 == 0 else ARMS[::-1])})
    return jobs


def count_calls(rows):
    posts = [r for r in rows if r['method'] == 'POST']
    return {'proxy_post': len(posts), 'model_post': sum(bool(r['upstream_forwarded']) for r in posts),
            'proxy_503': sum(not r['upstream_forwarded'] and r.get('http_status') == 503 for r in posts)}


def register(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    cases_path = f.NET / 'backends/eval_commands.reviewed.jsonl'
    cases = [c for c in f.read_dataset(cases_path)['cases'] if c['id'] in ('C01', 'C04')]
    config = f.load_json(f.NET / 'experiments/policy_config.json')
    calibration = f.load_json(ROOT / 'docs/experiments/20261111_fused_real/reviewed_20_sealed_repeat3/calibration.json')
    for tier, value in calibration['inference_cost_ms'].items():
        config[tier + '_infer_ms'] = value
    sources = [HERE / n for n in ('planning_recovery.py', 'race_matrix.py', 'runtime.py', 'local_models.json')]
    sources += [ROOT / p for p in f.load_json(ROOT / 'docs/experiments/20261111_real_fault_v2/registration.json')['source_sha256']]
    sources += [cases_path, f.NET / 'experiments/policy_config.json',
                f.NET / 'experiments/service_profiles.json',
                ROOT / 'docs/experiments/20261111_fused_real/reviewed_20_sealed_repeat3/calibration.json']
    registration = {'schema': 'sensehome.post-health-failure-registration.v3', 'registered_at_utc': now(),
        'planned_rows': 36, 'paired_blocks': 18, 'schedule': schedule(), 'cases': cases,
        'case_source_sha256': digest(cases_path), 'policy_config': config, 'calibration_source': 'unchanged v1 1116/1361ms',
        'recovery_launch_budget_ms': 10000,
        'source_sha256': {p.relative_to(ROOT).as_posix(): digest(p) for p in sources},
        'manipulation': 'For each block, both proxies healthy during actual GET /models; only afterward switch local or both proxies to POST503. GET /models continues forwarding inventory; no health or C observation override.',
        'comparison': 'Original C joint selection and original gateway in both arms. Recovery arm adds one eligible alternate planner before the gateway execution contract. Arm order alternates; state resets each arm; independent C instances per arm and condition.',
        'primary_outcome': 'frozen nonempty task completion + simulation state confirmation without gateway fallback/error',
        'secondary': ['model POST/503 attempts', 'once-only executor call count', 'all-request gateway wall time', 'actual planning source'],
        'warmup': 'one model POST per actual model before matrix, using a fixed non-evaluation command; separate file',
        'stop_rule': 'one registered run; preserve all failures; no post-result config/prompt/model/label optimization',
        'scope': 'Two previously reviewed commands repeated, same PC/GPU, software proxy POST failures, JSON devices; not user generalization, real WAN, service crash or 6G.'}
    write(out / 'registration.json', registration)
    snapshot = out / 'source_snapshot'
    snapshot.mkdir()
    for p in sources:
        dest = snapshot / p.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dest)
    return registration


def evaluate(out, registration, endpoints):
    from planning_recovery import PlanningRecoveryBackend
    cases = {x['id']: x for x in registration['cases']}
    rows, blocks, proxy_rows, warmups = [], [], [], []
    original_state = f.bridge.STATE_FILE
    original_random = random.getstate()
    started = now()
    try:
        # Separate upstream warmup, before any experimental proxy request.
        for tier, endpoint in endpoints.items():
            backend = f.DirectModelBackend(endpoint['url'], endpoint['model'], tier, timeout_s=15)
            backend.infer('读取室外温度，然后把浴室窗帘位置设为22%')
            warmups += [{'tier': tier, **x} for x in backend.attempts]
        write(out / 'warmups.json', warmups)
        with tempfile.TemporaryDirectory(prefix='sensehome-v3-state-') as temporary:
            f.bridge.STATE_FILE = str(Path(temporary) / 'state.json')
            random.seed(202611111)
            initial = f.bridge.default_state()
            write(out / 'initial_state.json', initial)
            for condition in CONDITIONS:
                with ExitStack() as stack:
                    proxies = {tier: stack.enter_context(ControlledProxy(e['url'])) for tier, e in endpoints.items()}
                    raw = {tier: f.DirectModelBackend(proxies[tier].url, e['model'], tier, timeout_s=15)
                           for tier, e in endpoints.items()}
                    wrapped = {tier: PlanningRecoveryBackend(raw[tier], raw[other],
                        primary_name=tier + '-model', secondary_name=other + '-model',
                        primary_url=proxies[tier].url, secondary_url=proxies[other].url,
                        launch_budget_ms=registration['recovery_launch_budget_ms'])
                        for tier, other in [('local', 'cloud'), ('cloud', 'local')]}
                    policies = {arm: f.ResourcePolicyClient(str(f.NET / 'policy_cli.exe'), registration['policy_config'], 'joint')
                                for arm in ARMS}
                    for policy in policies.values():
                        stack.callback(policy.close)
                    for index, job in enumerate(j for j in registration['schedule'] if j['condition'] == condition):
                        case = cases[job['case_id']]
                        pair = f"{condition}:{job['case_id']}:r{job['repeat']}"
                        for proxy in proxies.values():
                            proxy.mode = 'healthy'
                        health = {tier: backend.health() for tier, backend in raw.items()}
                        observation = f.make_observation(index, health, f.default_profile())
                        # No alterations to measured health. Fail only after both checks finished.
                        fault_at = now()
                        if condition in ('local_post503', 'both_post503'):
                            proxies['local'].mode = 'http_503'
                        if condition == 'both_post503':
                            proxies['cloud'].mode = 'http_503'
                        block = {'pair_id': pair, 'health': deepcopy(health), 'observation': deepcopy(observation),
                                 'fault_activated_at_utc': fault_at, 'order': job['arms']}
                        blocks.append(block)
                        for order_index, arm in enumerate(job['arms']):
                            f.bridge.save_state(deepcopy(initial))
                            before = deepcopy(f.bridge.load_state())
                            begin_attempts = {t: len(x.attempts) for t, x in raw.items()}
                            begin_proxies = {t: len(x.records) for t, x in proxies.items()}
                            begin_recovery = {t: len(x.recovery_records) for t, x in wrapped.items()}
                            execution = []
                            def execute(actions, context=None):
                                value = f.bridge._gateway_action_executor(actions, context)
                                execution.append(deepcopy(value))
                                return value
                            models = raw if arm == 'baseline' else wrapped
                            gateway = f.OffloadGateway(policies[arm],
                                {'local-rules': f.RuleBackend(f.bridge._gateway_local_handler),
                                 **{t + '-model': backend for t, backend in models.items()}},
                                link_snapshot=f.LinkSnapshot(wan_ok=True, lan_ok=True), action_executor=execute)
                            row = {'pair_id': pair, 'condition': condition, 'case_id': case['id'], 'arm': arm,
                                   'order_index': order_index, 'command': case['command'], 'expected_actions': case['expected_actions'],
                                   'input_hash': f.canonical_hash({'command': case['command'], 'context': case.get('context', {}), 'initial': initial}),
                                   'observation_hash': f.canonical_hash(observation), 'started_at_utc': now(), 'state_before': before}
                            start = time.perf_counter()
                            try:
                                event = gateway.handle(case['command'], deepcopy(case.get('context', {})), deepcopy(observation))
                                after = deepcopy(f.bridge.load_state())
                                row.update(status='finished', gateway_event=event, state_after=after,
                                           executor_calls=len(execution), simulated_executor_results=execution,
                                           **f.score_outcome(event, before, after, case['expected_actions'], execution))
                            except Exception as exc:
                                row.update(status='runner_failed', error=type(exc).__name__, task_completed=False,
                                           executor_calls=len(execution), state_after=deepcopy(f.bridge.load_state()))
                            row['gateway_wall_ms'] = round((time.perf_counter() - start) * 1000, 3)
                            attempts = [{'tier': t, **deepcopy(x)} for t, b in raw.items() for x in b.attempts[begin_attempts[t]:]]
                            records = [{'tier': t, **deepcopy(x)} for t, p in proxies.items() for x in p.records[begin_proxies[t]:]]
                            row.update(backend_attempts=attempts, proxy_records=records, calls=count_calls(records),
                                       recovery_records=[deepcopy(x) for t, b in wrapped.items() for x in b.recovery_records[begin_recovery[t]:]])
                            rows.append(row)
                            with (out / 'rows.jsonl').open('a', encoding='utf-8') as stream:
                                stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
                        print(json.dumps({'pair': pair, 'rows_saved': len(rows)}, ensure_ascii=True), flush=True)
                    proxy_rows += [{'condition': condition, 'tier': t, **deepcopy(x)} for t, p in proxies.items() for x in p.records]
    finally:
        f.bridge.STATE_FILE = original_state
        random.setstate(original_random)
        report = {'schema': 'sensehome.post-health-failure-results.v3', 'started_at_utc': started,
                  'finished_at_utc': now(), 'rows': rows, 'blocks': blocks, 'proxy_records': proxy_rows}
        write(out / 'report.json', report)
    summary = {'n_rows': len(rows), 'n_runner_failed': sum(x['status'] != 'finished' for x in rows), 'conditions': {}}
    for condition in CONDITIONS:
        summary['conditions'][condition] = {}
        for arm in ARMS:
            subset = [r for r in rows if r['condition'] == condition and r['arm'] == arm]
            summary['conditions'][condition][arm] = {'n': len(subset), 'completed': sum(r['task_completed'] for r in subset),
                'median_all_request_ms': statistics.median(r['gateway_wall_ms'] for r in subset),
                'executor_calls': sum(r['executor_calls'] for r in subset),
                **{key: sum(r['calls'][key] for r in subset) for key in ('proxy_post', 'model_post', 'proxy_503')}}
    summary['source_sha256_unchanged'] = all(digest(ROOT / name) == expected for name, expected in registration['source_sha256'].items())
    write(out / 'summary.json', summary)
    return summary


def main():
    registration = register(OUT)
    with LocalRuntime(OUT) as runtime:
        result = evaluate(OUT, registration, runtime.endpoints())
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
