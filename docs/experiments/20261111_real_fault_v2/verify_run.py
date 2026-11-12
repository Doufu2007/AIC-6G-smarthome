"""Offline audit only: no model requests, label edits or inference reruns."""
from collections import defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics
import sys

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]
sys.path.insert(0, str(ROOT / 'docs/deployment'))
import fused_eval as frozen


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


registration = read(BASE / 'registration.json')
overall = read(BASE / 'summary.json')
registered = datetime.fromisoformat(registration['registered_at'])
assert overall['status'] == 'finished' and overall['source_unchanged']
assert sha(BASE / 'registration.json') == overall['registration_sha256']
assert sha(BASE / 'calibration.json') == registration['calibration_sha256']
assert sha(BASE / 'cases.human_subset.json') == registration['selected_cases_sha256']
assert sha(ROOT / 'SenseHome-6G/net_offload/backends/eval_commands.reviewed.jsonl') == registration['dataset_sha256']
for relative, expected in registration['source_sha256'].items():
    assert sha(ROOT / relative) == expected, relative
    assert sha(BASE / 'source_snapshot' / Path(relative).name) == expected, relative

audit = {'status': 'passed', 'reviewer': 'author offline audit; no fresh independent review',
         'no_model_requests': True, 'conditions': {}, 'n_rows': 0,
         'paired_proxy_post_attempts': 0, 'paired_actual_model_post_attempts': 0,
         'paired_actual_model_post_http200': 0, 'warmup_proxy_post_attempts': 0,
         'warmup_actual_model_post_attempts': 0, 'model_inventory_gets': 0,
         'proxy_telemetry_gets': 0}
for condition in registration['conditions']:
    name = condition['id']
    directory = BASE / 'conditions' / name
    report = read(directory / 'report.json')
    gateway_raw = read(directory / 'report.gateway_raw.json')
    rows = [json.loads(line) for line in (directory / 'rows.jsonl').read_text(encoding='utf-8').splitlines()]
    blocks = [json.loads(line) for line in (directory / 'blocks.jsonl').read_text(encoding='utf-8').splitlines()]
    assert registered < datetime.fromisoformat(report['started_at'])
    assert report['status'] == 'finished' and not report['failures']
    assert report['rows'] == rows and report['blocks'] == blocks
    assert len(rows) == 24 and len(blocks) == 6
    assert report['policy_config'] == registration['policy_config']
    assert frozen.summarize(rows) == report['summary'] == read(directory / 'summary.json')
    assert frozen.summarize(gateway_raw['rows']) == report['summary']
    assert read(directory / 'condition_summary.json') == overall['conditions'][name]
    for relative, expected in report['source_sha256'].items():
        assert registration['source_sha256'][Path(relative).as_posix()] == expected
        assert sha(directory / 'source_snapshot' / Path(relative).name) == expected
    by_pair = defaultdict(list)
    records = read(directory / 'proxy_records.json')
    traces = {entry['trace_id']: entry for entries in records.values() for entry in entries}
    assert len(traces) == sum(len(entries) for entries in records.values())
    used = set()
    for row in rows:
        assert row['status'] == 'finished'
        by_pair[row['pair_id']].append(row)
        scored = frozen.score_outcome(row['gateway_event'], row['state_before'], row['state_after'],
                                      row['expected_actions'], row['simulated_executor_results'])
        assert all(row[key] == value for key, value in scored.items())
        client_calls = [exchange for attempt in row['backend_attempts']
                        for exchange in attempt['upstream_exchanges'] if exchange['method'] == 'POST']
        assert len(client_calls) == row['proxy_post_attempts'] == row['model_calls']
        assert [entry['response']['_sensehome_proxy_trace']['trace_id'] for entry in client_calls] == row['proxy_trace_ids']
        proxied = [traces[trace] for trace in row['proxy_trace_ids']]
        assert not used.intersection(row['proxy_trace_ids'])
        used.update(row['proxy_trace_ids'])
        assert sum(bool(entry['upstream_forwarded']) for entry in proxied) == row['actual_upstream_model_post_attempts']
        assert sum(entry['upstream_forwarded'] and entry['upstream_status'] == 200 for entry in proxied) == row['actual_upstream_model_post_http200']
        audit['paired_proxy_post_attempts'] += len(client_calls)
        audit['paired_actual_model_post_attempts'] += row['actual_upstream_model_post_attempts']
        audit['paired_actual_model_post_http200'] += row['actual_upstream_model_post_http200']
    for block in blocks:
        paired = by_pair[block['pair_id']]
        assert [row['arm'] for row in paired] == block['order']
        assert set(block['order']) == set(frozen.ARMS)
        for key in ('command', 'input_hash', 'observation_hash', 'state_before', 'expected_actions'):
            assert all(row[key] == paired[0][key] for row in paired)
        for tier in ('local', 'cloud'):
            observed = block['health'][tier]
            assert len(observed['exchanges']) == 2
            audit['model_inventory_gets'] += 1
            audit['proxy_telemetry_gets'] += 1
            assert observed['ready'] == (condition[tier + '_mode'] != 'http_503')
            assert observed['reachable'] is True
            assert block['observation']['compute'][tier]['model_ready'] == observed['ready']
            assert block['observation']['compute'][tier]['queue_ms'] == observed['queue_ms']
    warmups = read(directory / 'warmups.json')
    warmup_traces = {exchange['response']['_sensehome_proxy_trace']['trace_id']
                    for entry in warmups for attempt in entry['attempts']
                    for exchange in attempt['upstream_exchanges'] if exchange['method'] == 'POST'}
    assert len(warmup_traces) == 2 and not used.intersection(warmup_traces)
    assert {entry['trace_id'] for entry in traces.values() if entry['method'] == 'POST'} == used | warmup_traces
    audit['warmup_proxy_post_attempts'] += len(warmup_traces)
    audit['warmup_actual_model_post_attempts'] += sum(traces[trace]['upstream_forwarded'] for trace in warmup_traces)
    delay_ranges = {}
    for tier, entries in records.items():
        posts = [entry for entry in entries if entry['method'] == 'POST']
        assert all(entry['configured_admission_wait_ms'] == condition[tier + '_delay_ms'] for entry in posts)
        if condition[tier + '_mode'] == 'http_503':
            assert all(entry['http_status'] == 503 and not entry['upstream_forwarded'] for entry in posts)
        else:
            assert all(entry['upstream_forwarded'] for entry in posts)
            waits = [entry['actual_admission_wait_ms'] for entry in posts]
            if condition[tier + '_delay_ms']:
                assert min(waits) >= condition[tier + '_delay_ms'] - 10
            delay_ranges[tier] = {'n_posts_including_warmup': len(waits), 'min_ms': min(waits),
                                  'median_ms': statistics.median(waits), 'max_ms': max(waits)}
    audit['n_rows'] += len(rows)
    audit['conditions'][name] = {'n_rows': len(rows), 'n_pair_blocks': len(blocks),
        'n_runner_failed': 0, 'n_skipped': 0, 'source_snapshot_and_current_match': True,
        'registration_before_run': True, 'all_scores_recomputed': True,
        'paired_inputs_match': True, 'measured_proxy_waits': delay_ranges,
        'report_sha256': sha(directory / 'report.json'), 'rows_sha256': sha(directory / 'rows.jsonl'),
        'proxy_records_sha256': sha(directory / 'proxy_records.json')}
assert audit['n_rows'] == registration['planned_rows'] == 120
audit['registered_at'] = registration['registered_at']
audit['registration_sha256'] = sha(BASE / 'registration.json')
audit['summary_sha256'] = sha(BASE / 'summary.json')
audit['checked_at'] = frozen.now()
frozen.write_json(BASE / 'audit.json', audit)
print(json.dumps({key: value for key, value in audit.items() if key != 'conditions'}, indent=2))
