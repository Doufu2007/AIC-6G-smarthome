"""Offline reconstruction of the registered rules/MQTT load; starts no service."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def read(name):
    return json.loads((HERE / name).read_text(encoding='utf-8'))


def lines(name):
    return [json.loads(line) for line in (HERE / name).read_text(encoding='utf-8').splitlines() if line]


def require(condition, detail):
    if not condition:
        raise AssertionError(detail)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(snapshot=False):
    registration, result = read('registration.json'), read('results.json')
    require(result['registration_sha256'] == digest(HERE / 'registration.json'), 'registration digest')
    require(datetime.fromisoformat(registration['registered_at_utc']) <
            datetime.fromisoformat(read('run_manifest.json')['started_at_utc']), 'registration ordering')
    stored = HERE / 'source_snapshot'
    copied = ROOT / '_work/operations_v4/20261111T064349'
    if snapshot:
        require(not stored.exists(), 'snapshot already exists; never replace')
        require(copied.is_dir(), 'original copied deployment missing')
        require(all(digest(copied / rel) == expected for rel, expected in registration['source_sha256'].items()),
                'copied registered source mismatch')
        stored.mkdir()
        for rel in registration['source_sha256']:
            target = stored / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(copied / rel, target)
    source = stored if stored.is_dir() else copied
    for rel, expected in registration['source_sha256'].items():
        require(digest(source / rel) == expected, 'source mismatch: ' + rel)
    rows = lines('rows.jsonl')
    require(len(rows) == registration['planned_requests'] == 120, 'request count')
    successful, rejected, expected_ids = 0, 0, set()
    for i, row in enumerate(rows):
        require(row['index'] == i and row['scheduled_s'] == i * registration['interval_s'], 'schedule index')
        invalid = (i + 1) % registration['invalid_every'] == 0
        expected_command = registration['invalid_command'] if invalid else registration['commands'][i % 2]
        require(row['command'] == expected_command, 'registered command')
        require('error' not in row, 'transport error')
        response = row['response']
        body = response['body']
        if invalid:
            require(not body['actions'], 'invalid command executed')
            require(not body['semantic_complete'], 'invalid command marked complete')
            rejected += 1
        else:
            require(response['status'] == 200 and body['semantic_complete'] is True, 'valid completion')
            require(len(body['actions']) == 1, 'valid action count')
            require(row['state_after']['body']['lights']['客厅']['is_on'] == (i % 2 == 0), 'actual light state')
            receipt = body['receipt']
            require(receipt['status'] == 'applied' and receipt['completed_indices'] == [0], 'receipt completion')
            require(receipt['command_id'] == 'ops-v4-' + format(i, '04d'), 'receipt identity')
            require(receipt['physical_execution_confirmed'] is False, 'physical boundary')
            require(receipt['request_sha256'] == body['request_sha256'], 'receipt request binding')
            expected_ids.add(receipt['command_id'])
            successful += 1
    first = read('first_task.json')['response']['body']
    expected_ids.add(first['receipt']['command_id'])
    events = lines('gateway_events.jsonl')
    require(len(events) == len(rows) + 1 == result['gateway_event_count_including_first_task'], 'gateway events')
    submits = [r['envelope']['payload']['command_id'] for r in lines('mqtt_gateway_mqtt.jsonl')
               if r.get('direction') == 'publish' and r.get('envelope', {}).get('operation') == 'submit']
    require(set(submits) == expected_ids and all(n == 1 for n in Counter(submits).values()), 'duplicate/missing submit')
    with sqlite3.connect((HERE / 'mqtt_entity.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        entity_ids = {r[0] for r in db.execute('SELECT command_id FROM entity_commands')}
        version = db.execute('SELECT version FROM entity_state WHERE id=1').fetchone()[0]
    with sqlite3.connect((HERE / 'mqtt_gateway.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        gateway = list(db.execute('SELECT command_id,status FROM commands'))
    require(entity_ids == expected_ids == {r[0] for r in gateway}, 'database identity agreement')
    require(all(r[1] == 'applied' for r in gateway) and version == len(expected_ids), 'database status/version')
    samples = read('resource_samples.json')
    require(all(s['rss_sum_bytes'] == sum(p['rss_bytes'] for p in s['processes']) for s in samples), 'RSS sums')
    max_rss = max(s['rss_sum_bytes'] for s in samples)
    require(len(samples) == result['sample_count'] and max_rss == result['max_process_tree_rss_sum_bytes'], 'RSS summary')
    require(successful == result['executable_requests'] == 116 and rejected == result['invalid_requests'] == 4, 'summary counts')
    require(result['expected_behavior_observed'] == 120 and result['transport_errors'] == 0, 'summary behavior')
    require(result['observed_duration_s'] >= registration['duration_s'], 'observation duration')
    require(result['owned_process_exit_code'] == 0 and read('mqtt_cleanup.json')['all_stopped'], 'runtime cleanup')
    require(result['original_state_and_events_unchanged'] is True, 'protected files')
    require(result['human_deployment_time_s'] is None and result['actual_total_cost_cny'] is None and
            result['participant_feedback_count'] == 0, 'external facts boundary')
    return {'schema': 'sensehome.operations-offline-verification.v4', 'status': 'PASS',
            'source_files_verified': len(registration['source_sha256']), 'source_kind': 'frozen copied deployment',
            'rows': len(rows), 'executed': successful, 'rejected': rejected, 'gateway_events': len(events),
            'unique_submits': len(expected_ids), 'entity_version': version,
            'resource_samples': len(samples), 'max_rss_sum_bytes': max_rss,
            'scope': 'rules-only, automated, short loopback MQTT software load; no human, model-soak or physical-device claim'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preserve-sources', action='store_true')
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    report = verify(args.preserve_sources)
    if args.write:
        target = HERE / 'offline_verification.json'
        require(not target.exists(), 'verification output exists; use stdout without --write')
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2))
