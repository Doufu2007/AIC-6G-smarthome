"""Offline verification of the four explicitly selected final matrices.

Never contacts a model, changes a label, or reruns inference. The optional index
refresh changes only the aggregate navigation file after every assertion passes.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from fused_eval import ARMS, ROOT, score_outcome, summarize

BASE = ROOT / 'docs/experiments/20261111_fused_real'
RUNS = ('reviewed_20_sealed_repeat3', 'challenge_12_sealed_development_repeat3',
        'controlled_faults_sealed', 'holdout_12_programmatic_repeat3')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_run(name):
    directory = BASE / name
    report = read(directory / 'report.json')
    rows = [json.loads(line) for line in (directory / 'rows.jsonl').read_text(encoding='utf-8').splitlines()]
    blocks = [json.loads(line) for line in (directory / 'blocks.jsonl').read_text(encoding='utf-8').splitlines()]
    assert report['status'] == 'finished' and not report['failures'], name
    assert rows == report['rows'] and blocks == report['blocks'], name
    assert summarize(rows) == report['summary'] == read(directory / 'summary.json'), name
    assert all(row['status'] == 'finished' for row in rows), name
    pairs = defaultdict(list)
    for row in rows:
        pairs[row['pair_id']].append(row)
        scored = score_outcome(row['gateway_event'], row['state_before'], row['state_after'],
                               row['expected_actions'], row['simulated_executor_results'])
        assert all(row[key] == value for key, value in scored.items()), (name, row['pair_id'], row['arm'])
    assert len(pairs) == len(blocks) and len(rows) == 4 * len(blocks), name
    for block in blocks:
        paired = pairs[block['pair_id']]
        assert [row['arm'] for row in paired] == block['order'], name
        assert set(block['order']) == set(ARMS), name
        for key in ('input_hash', 'observation_hash', 'command', 'state_before', 'expected_actions'):
            assert all(row[key] == paired[0][key] for row in paired), (name, key)
    for relative, expected in report['source_sha256'].items():
        assert digest(ROOT / relative) == expected, (name, relative, 'current source')
        assert digest(directory / 'source_snapshot' / Path(relative).name) == expected, (name, relative, 'snapshot')
    per_arm = {}
    for arm in ARMS:
        subset = [row for row in rows if row['arm'] == arm]
        nonempty = [row for row in subset if row['expected_actions']]
        empty = [row for row in subset if not row['expected_actions']]
        per_arm[arm] = {
            'n_rows': len(subset), 'nonempty_n': len(nonempty),
            'nonempty_completed': sum(bool(row['task_completed']) for row in nonempty),
            'empty_n': len(empty),
            'empty_no_action_no_mutation': sum(bool(row['safe_no_action_structure']) for row in empty),
            'protection_refusals': sum(bool(row['protection_refusal']) for row in subset),
            'upstream_model_calls': sum(row['model_calls'] for row in subset),
        }
    health_calls = sum(len(endpoint['exchanges']) for block in blocks for endpoint in block['health'].values())
    return {
        'status': 'finished', 'n_rows': len(rows), 'n_pair_blocks': len(blocks),
        'n_model_http_calls': sum(row['model_calls'] for row in rows),
        'n_health_http_calls': health_calls, 'n_runner_failed': 0, 'n_skipped': 0,
        'dataset': report['dataset'], 'per_arm': per_arm,
        'sha256': {file: digest(directory / file) for file in
                   ('report.json', 'rows.jsonl', 'blocks.jsonl', 'summary.json', 'summary.md', 'calibration.json')},
        'source_sha256': report['source_sha256'], 'source_current_matches': True,
    }


def verify_registration():
    directory = BASE / 'holdout_12_preregistered'
    registration = read(directory / 'registration.json')
    report = read(BASE / RUNS[-1] / 'report.json')
    registered = datetime.fromisoformat(registration['registered_at'])
    inference_start = min(datetime.fromisoformat(exchange['started_at'])
        for row in report['rows'] for attempt in row['backend_attempts']
        for exchange in attempt.get('exchanges', attempt.get('response', {}).get('exchanges', []))
        if exchange['method'] == 'POST') if any(
            attempt.get('exchanges') or attempt.get('response', {}).get('exchanges')
            for row in report['rows'] for attempt in row['backend_attempts']) else None
    # The report is initialized before any health or inference request.
    assert registered < datetime.fromisoformat(report['started_at'])
    assert inference_start is None or registered < inference_start
    assert digest(directory / registration['dataset_file']) == registration['dataset_sha256'] == report['dataset']['sha256']
    assert digest(ROOT / 'docs/deployment/fused_eval_generate_holdout.py') == registration['generator_sha256']
    assert registration['source_sha256'] == report['source_sha256']
    assert registration['calibration_sha256'] == digest(BASE / RUNS[-1] / 'calibration.json')
    assert registration['policy_config'] == report['policy_config']
    assert len(report['rows']) == registration['expected_raw_rows'] == 144
    cases = [json.loads(line) for line in (directory / registration['dataset_file']).read_text(encoding='utf-8').splitlines()]
    commands = {case['command'] for case in cases}
    assert len(cases) == len(commands) == 12
    for relative, expected in registration['previous_dataset_sha256'].items():
        path = ROOT / relative
        assert digest(path) == expected
        previous = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        assert not commands.intersection(case['command'] for case in previous)
    return {'registered_at': registration['registered_at'], 'run_started_at': report['started_at'],
            'registered_before_run': True, 'data_source_calibration_config_match': True,
            'exact_command_overlap_with_previous_sets': 0,
            'registration_sha256': digest(directory / 'registration.json'),
            'dataset_sha256': registration['dataset_sha256'],
            'scope': registration['scope']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh-index', action='store_true')
    args = parser.parse_args()
    runs = {name: verify_run(name) for name in RUNS}
    registration = verify_registration()
    old_index = read(BASE / 'sealed_evidence_index.json')
    # Existing sealed bytes must agree with the previously published index.
    for name in RUNS[:3]:
        assert old_index['runs'][name]['sha256'] == runs[name]['sha256']
    result = {'schema_version': 'sensehome.offline-sealed-audit.v1',
              'checked_at_utc': datetime.now(timezone.utc).isoformat(),
              'reviewer': 'primary agent offline verification; not a fresh independent review',
              'status': 'passed', 'no_http_or_inference_calls': True,
              'n_saved_rows': sum(run['n_rows'] for run in runs.values()),
              'n_actual_model_http_calls': sum(run['n_model_http_calls'] for run in runs.values()),
              'n_readonly_health_http_calls': sum(run['n_health_http_calls'] for run in runs.values()),
              'holdout_registration': registration, 'runs': runs}
    (BASE / 'offline_sealed_audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if args.refresh_index:
        index = {**old_index, 'runs': runs,
                 **{key: result[key] for key in ('n_saved_rows', 'n_actual_model_http_calls', 'n_readonly_health_http_calls')},
                 'source_hash_consistent': len({json.dumps(run['source_sha256'], sort_keys=True) for run in runs.values()}) == 1,
                 'holdout_registration': registration}
        index['narrative_artifacts'] = {name: digest(BASE / name) for name in old_index['narrative_artifacts']}
        (BASE / 'sealed_evidence_index.json').write_text(json.dumps(index, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key not in ('runs', 'holdout_registration')}, indent=2))


if __name__ == '__main__':
    main()
