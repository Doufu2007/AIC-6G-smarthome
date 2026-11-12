"""Independently audit v3 files without inference or mutations of run evidence."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUN = ROOT / 'docs/experiments/20261111_race_v3_portfix'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def canonical_actions(actions):
    """Independent implementation of semantic equivalence for frozen C01/C04."""
    values = []
    for action in actions:
        name, p = action['name'], action['params']
        result = {'name': name, 'location': p['location']}
        if name == 'light_control':
            operation = p.get('action', 'set' if 'brightness' in p else None)
            result['brightness'] = 0 if operation == 'off' else p.get('brightness', 100)
        elif name == 'curtain_control':
            operation = p.get('action', 'set' if 'position' in p else None)
            result['position'] = 0 if operation == 'close' else p.get('position', 100)
        else:
            raise ValueError('audit limited to registered mutation-only cases')
        values.append(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return sorted(values)


def expected_state(before, actions):
    state = deepcopy(before)
    for action in canonical_actions(actions):
        item = json.loads(action)
        room = item['location']
        if item['name'] == 'light_control':
            brightness = item['brightness']
            state['lights'][room] = {'is_on': brightness > 0, 'brightness': brightness}
        else:
            position = item['position']
            state['curtains'][room] = {'is_open': position > 0, 'position': position}
    return state


def completed(row):
    event = row.get('gateway_event', {})
    actions = event.get('actions', [])
    try:
        correct = bool(event.get('interface_ok') and canonical_actions(actions) ==
                       canonical_actions(row['expected_actions']))
        state_ok = expected_state(row['state_before'], row['expected_actions']) == row.get('state_after')
    except (KeyError, TypeError, ValueError):
        correct = state_ok = False
    executor_results = row.get('simulated_executor_results', [])
    remote_confirmed = bool(executor_results and all(x.get('semantic_complete') for x in executor_results))
    response = event.get('response', {})
    local_confirmed = bool(event.get('source') == 'local-rules' and event.get('semantic_complete') and actions and
                           (response.get('execution_confirmed') is True or
                            response.get('simulated_execution_confirmed') is True or
                            row['state_before'] != row.get('state_after') or response.get('observation')))
    simulated = bool(event.get('interface_ok') and (remote_confirmed or local_confirmed))
    return bool(row['expected_actions'] and correct and state_ok and simulated and
                event.get('semantic_complete') and not event.get('error') and not event.get('fallback_from'))


def posts(records):
    return [x for x in records if x.get('method') == 'POST']


def count_calls(records):
    values = posts(records)
    return {'proxy_post': len(values),
            'model_post': sum(x.get('upstream_forwarded') is True for x in values),
            'proxy_503': sum(x.get('upstream_forwarded') is False and x.get('http_status') == 503 for x in values)}


def usage(records):
    total = Counter(prompt_tokens=0, completion_tokens=0, total_tokens=0)
    seen, missing = 0, 0
    for record in posts(records):
        if record.get('upstream_forwarded') is not True:
            continue
        value = record.get('upstream_exchange', {}).get('response', {}).get('usage')
        seen += 1
        if not isinstance(value, dict):
            missing += 1
            continue
        for key in total:
            if type(value.get(key)) is int:
                total[key] += value[key]
    return {'forwarded_model_post': seen, 'missing_usage': missing, **total}


def audit(run_dir):
    run_dir = Path(run_dir).resolve()
    issues, checks = [], {}
    def check(name, condition, detail=None):
        checks[name] = {'pass': bool(condition), 'detail': detail}
        if not condition:
            issues.append(name)

    registration = read(run_dir / 'registration.json')
    manifest = read(run_dir / 'runtime_manifest.json')
    check('runtime_stopped', manifest.get('status') == 'stopped')
    check('two_model_children_started_ready_then_exited', len(manifest.get('children', [])) == 2 and
          all(x.get('ready_at_utc') and x.get('exit_code') is not None for x in manifest.get('children', [])))
    old_registration = read(ROOT / 'docs/experiments/20261111_real_fault_v2/registration.json')
    old_hashes = {name: sha(ROOT / name) == expected for name, expected in old_registration['source_sha256'].items()}
    check('all_12_old_source_sha256_unchanged', len(old_hashes) == 12 and all(old_hashes.values()), old_hashes)
    source_checks = {}
    for name, expected in registration['source_sha256'].items():
        source_checks[name] = {'current': sha(ROOT / name) == expected,
                               'snapshot': sha(run_dir / 'source_snapshot' / name) == expected}
    check('all_registered_sources_and_snapshots_match', all(all(x.values()) for x in source_checks.values()), source_checks)
    check('runtime_config_hash_matches_registered_config', manifest.get('config_sha256') ==
          registration['source_sha256'].get('docs/demo_v3/local_models.json'))
    check('runtime_30_dll_hashes_current', len(manifest.get('runtime_dependencies', {})) == 30 and
          all(sha(x['path']) == x['sha256'] for x in manifest.get('runtime_dependencies', {}).values()))
    check('runtime_executable_and_two_model_hashes_current',
          sha(manifest['runtime']['path']) == manifest['runtime']['sha256'] and
          len(manifest['models']) == 2 and
          all(sha(x['path']) == x['sha256'] for x in manifest['models'].values()))
    startup_failure = ROOT / 'docs/experiments/20261111_race_v3'
    failed_registration = read(startup_failure / 'registration.json') if startup_failure.exists() else None
    failure_lineage = None
    if failed_registration:
        failure_manifest = read(startup_failure / 'runtime_manifest.json')
        failure_lineage = {'directory': str(startup_failure.relative_to(ROOT)),
                           'registration_sha256': sha(startup_failure / 'registration.json'),
                           'status': failure_manifest.get('status'), 'children': failure_manifest.get('children'),
                           'has_rows': (startup_failure / 'rows.jsonl').exists(),
                           'reason': 'local HTTP bind failed before models became ready; retain startup failure'}
        check('startup_amendment_links_preserved_failed_registration',
              registration.get('startup_amendment', {}).get('previous_registration_sha256') ==
              failure_lineage['registration_sha256'] and not failure_lineage['has_rows'])
    required = ['rows.jsonl', 'report.json', 'summary.json', 'warmups.json', 'initial_state.json']
    missing = [name for name in required if not (run_dir / name).exists()]
    check('all_result_files_exist', not missing, missing)
    result = {'schema': 'sensehome.v3.independent-audit.v1', 'audited_at_utc': datetime.now(timezone.utc).isoformat(),
              'run_directory': str(run_dir.relative_to(ROOT)), 'scope': 'read-only independent recomputation; no model calls',
              'startup_failure_preserved': failure_lineage, 'checks': checks, 'issues': issues}
    if missing:
        result['verdict'] = 'INCOMPLETE'
        return result

    rows = [json.loads(line) for line in (run_dir / 'rows.jsonl').read_text(encoding='utf-8').splitlines() if line]
    report, summary = read(run_dir / 'report.json'), read(run_dir / 'summary.json')
    warmups, initial = read(run_dir / 'warmups.json'), read(run_dir / 'initial_state.json')
    check('rows_jsonl_matches_report', rows == report['rows'])
    check('36_rows_18_blocks', len(rows) == 36 and len(report['blocks']) == 18 and
          len({x['pair_id'] for x in rows}) == 18)
    check('zero_runner_failures', all(x['status'] == 'finished' for x in rows))
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['pair_id']].append(row)
    check('pairs_have_two_arms_same_command_input_observation_initial', all(
        len(pair) == 2 and {x['arm'] for x in pair} == {'baseline', 'recovery'} and
        len({x['command'] for x in pair}) == len({x['input_hash'] for x in pair}) ==
        len({x['observation_hash'] for x in pair}) == 1 and
        all(x['state_before'] == initial for x in pair) for pair in grouped.values()))
    check('registered_order_matches_rows', all([x['arm'] for x in grouped[b['pair_id']]] == b['order']
                                             for b in report['blocks']))
    expected_pairs = [f"{x['condition']}:{x['case_id']}:r{x['repeat']}" for x in registration['schedule']]
    check('schedule_matches_block_order', [b['pair_id'] for b in report['blocks']] == expected_pairs)
    health_ok, health_before_fault, observation_hash_ok = True, True, True
    for block in report['blocks']:
        fault_time = datetime.fromisoformat(block['fault_activated_at_utc'])
        for tier in ('local', 'cloud'):
            health = block['health'][tier]
            exchanges = health.get('exchanges', [])
            health_ok &= health.get('ready') is True and health.get('reachable') is True and bool(exchanges)
            for exchange in exchanges:
                health_ok &= exchange['method'] == 'GET' and exchange['path'] == 'models' and exchange['http_status'] == 200
                end = datetime.fromisoformat(exchange['started_at']) + timedelta(milliseconds=exchange['wall_ms'])
                health_before_fault &= end <= fault_time + timedelta(milliseconds=2)
        observation_hash_ok &= all(x['observation_hash'] == canonical_hash(block['observation'])
                                  for x in grouped[block['pair_id']])
    check('all_block_health_real_get_inventory_ready_reachable', health_ok)
    check('paired_observation_hashes_recomputed', observation_hash_ok)
    registered_cases = {x['id']: x for x in registration['cases']}
    check('input_hashes_recomputed', all(row['input_hash'] == canonical_hash({
        'command': row['command'], 'context': registered_cases[row['case_id']].get('context', {}),
        'initial': initial}) for row in rows))
    check('fault_activated_after_health_gets', health_before_fault, '2ms tolerance for separate wall and monotonic clocks')
    check('registration_before_launch_before_evaluation',
          datetime.fromisoformat(registration['registered_at_utc']) <= datetime.fromisoformat(manifest['started_at_utc']) <=
          datetime.fromisoformat(report['started_at_utc']) <= datetime.fromisoformat(report['finished_at_utc']) <=
          datetime.fromisoformat(manifest['finished_at_utc']))
    check('actual_child_argv_and_readiness_match_manifest', all(
        child['argv'][0] == manifest['runtime']['path'] and
        child['argv'][child['argv'].index('-m') + 1] == manifest['models'][child['tier']]['path'] and
        child['argv'][child['argv'].index('--port') + 1] == str(manifest['models'][child['tier']]['port']) and
        child['argv'][child['argv'].index('--host') + 1] == '127.0.0.1' and
        child['health']['http_status'] == 200 and
        datetime.fromisoformat(child['launched_at_utc']) < datetime.fromisoformat(child['ready_at_utc']) <=
        datetime.fromisoformat(report['started_at_utc']) for child in manifest['children']))

    check('completion_independently_recomputed_matches', all(completed(x) == x['task_completed'] for x in rows))
    check('per_row_call_counts_match', all(count_calls(x['proxy_records']) == x['calls'] for x in rows))
    row_posts = [p for row in rows for p in posts(row['proxy_records'])]
    all_posts = posts(report['proxy_records'])
    check('no_missing_or_duplicate_post_records', Counter(x['trace_id'] for x in row_posts) ==
          Counter(x['trace_id'] for x in all_posts) and all(v == 1 for v in Counter(x['trace_id'] for x in all_posts).values()))
    check('all_forwarded_model_posts_returned_200', all(x['upstream_status'] == 200 and
          x.get('upstream_exchange', {}).get('error') is None for x in all_posts if x['upstream_forwarded']))
    check('actual_post_fault_receipts_match_registered_condition', all(
        (p['http_status'] == 503 and p['upstream_forwarded'] is False)
        if row['condition'] == 'both_post503' or (row['condition'] == 'local_post503' and p['tier'] == 'local')
        else (p['upstream_status'] == 200 and p['upstream_forwarded'] is True)
        for row in rows for p in posts(row['proxy_records'])))
    check('one_executor_call_on_completed_requests', all(x['executor_calls'] == 1 for x in rows if completed(x)))
    check('executor_records_count_consistent', all(x['executor_calls'] == len(x['simulated_executor_results']) for x in rows))
    check('zero_executor_on_transport_terminal_failures', all(x['executor_calls'] == 0 for x in rows
          if not x['gateway_event'].get('interface_ok') or x['gateway_event'].get('error') or
          x['gateway_event'].get('fallback_from')))
    recovery_rows = [x for x in rows if x['arm'] == 'recovery']
    source_ok = True
    recovered = []
    for row in recovery_rows:
        records = row['recovery_records']
        source_ok &= len(records) == 1
        if not records:
            continue
        meta = records[0]
        if meta['attempted']:
            source_ok &= meta['actual_backend'] == meta['to'] and meta['from'] != meta['to']
            source_ok &= len(row['backend_attempts']) == 2
            if completed(row):
                source_ok &= row['gateway_event']['response']['actual_backend'] == meta['to']
                source_ok &= row['gateway_event']['source'].startswith(meta['to'] + ':')
                recovered.append(row['pair_id'])
        else:
            source_ok &= len(row['backend_attempts']) == 1
    check('recovery_audit_actual_source_and_max_two_attempts', source_ok)
    check('at_most_one_executor_every_request', all(x['executor_calls'] <= 1 for x in rows))
    check('warmup_is_two_separate_direct_posts', len(warmups) == 2 and
          sum(len(posts(x['upstream_exchanges'])) for x in warmups) == 2)
    tables = {}
    for condition in ('healthy', 'local_post503', 'both_post503'):
        tables[condition] = {}
        for arm in ('baseline', 'recovery'):
            subset = [x for x in rows if x['condition'] == condition and x['arm'] == arm]
            tables[condition][arm] = {'n': len(subset), 'completed': sum(completed(x) for x in subset),
                'median_all_request_ms': statistics.median(x['gateway_wall_ms'] for x in subset),
                'executor_calls': sum(x['executor_calls'] for x in subset),
                **count_calls([p for x in subset for p in x['proxy_records']])}
    check('independent_condition_summary_matches_runner', tables == summary['conditions'])
    check('runner_source_unchanged_flag_true', summary.get('source_sha256_unchanged') is True)
    result.update(verdict='PASS' if not issues else 'FAIL', conditions=tables,
                  n_rows=len(rows), n_paired_blocks=len(grouped), experimental_calls=count_calls(report['proxy_records']),
                  experimental_usage=usage(report['proxy_records']), warmup_model_posts=2,
                  recovered_pair_ids=recovered,
                  completed_latency={'n': sum(completed(x) for x in rows),
                                     'within_profile_250_ms': sum(completed(x) and x['gateway_wall_ms'] <= 250 for x in rows),
                                     'min_ms': min(x['gateway_wall_ms'] for x in rows if completed(x)),
                                     'max_ms': max(x['gateway_wall_ms'] for x in rows if completed(x))},
                  evidence_files_sha256={name: sha(run_dir / name) for name in required + ['registration.json', 'runtime_manifest.json']},
                  limitations=['two previously reviewed commands repeated three times, not 36 independent tasks',
                               'same local PC/GPU, controlled proxy POST503, JSON simulation',
                               'once-only executor within one gateway request, not cross-request device idempotency',
                               'paired frozen health observation, not fresh measurement for every arm',
                               'no physical devices, WAN/6G, user trial or total cost claim'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, default=DEFAULT_RUN)
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/reviews/2026-11-11-v3-independent-counts.json')
    args = parser.parse_args()
    result = audit(args.run_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True, indent=2))
    raise SystemExit(0 if result['verdict'] == 'PASS' else 1)


if __name__ == '__main__':
    main()
