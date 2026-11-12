"""Offline audit of HTTP, MQTT wire logs, both databases and owned cleanup."""
from __future__ import annotations
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]


def audit(directory):
    summary = read(directory / 'summary.json')
    assert summary['schema'] == 'sensehome.integration-v4.summary.v1'
    if (directory / 'protected_files.json').exists():
        protected = read(directory / 'protected_files.json')
        assert protected['unchanged'] is True and protected['before'] == protected['after']
    checks = []
    for scenario in ('normal', 'ack_loss', 'partial', 'contradictory'):
        base = directory / scenario
        mqtt = base / 'mqtt'
        cleanup = read(mqtt / 'cleanup.json')
        assert cleanup['all_stopped'] is True
        assert all(p['exit_code'] == 0 and not p['forced_termination'] for p in cleanup['owned_processes'])
        launches = lines(mqtt / 'process_launches.jsonl')
        assert {p['pid'] for p in launches} == {p['pid'] for p in cleanup['owned_processes']}
        wire = lines(mqtt / 'entity_events.jsonl')
        submissions = [x['envelope']['payload'] for x in wire if x.get('envelope', {}).get('operation') == 'submit']
        counts = {}
        for payload in submissions:
            cid = payload['command_id']
            counts[cid] = counts.get(cid, 0) + 1
            raw = json.dumps({k: v for k, v in payload.items() if k != 'request_sha256'}, ensure_ascii=False,
                             sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
            assert hashlib.sha256(raw).hexdigest() == payload['request_sha256']
        assert counts and all(n == 1 for n in counts.values()), counts
        queries = sum(x.get('envelope', {}).get('operation') == 'lookup' for x in wire)
        gateway_db = base / 'gateway.sqlite3' if scenario == 'contradictory' else mqtt / 'gateway.sqlite3'
        with closing(sqlite3.connect(gateway_db)) as db:
            db.row_factory = sqlite3.Row
            commands = [dict(row) for row in db.execute('select * from commands')]
        with closing(sqlite3.connect(mqtt / 'entity.sqlite3')) as db:
            entities = db.execute('select command_id, request_sha256, receipt_json from entity_commands').fetchall()
            current_state_json, current_version = db.execute('select state_json,version from entity_state').fetchone()
        assert len(entities) == len(commands) == len(counts)
        for cid, sha, receipt_json in entities:
            receipt = json.loads(receipt_json)
            assert receipt['command_id'] == cid and receipt['request_sha256'] == sha
            assert receipt['physical_execution_confirmed'] is False
            gateway_row = next(row for row in commands if row['command_id'] == cid)
            assert gateway_row['request_sha256'] == sha
        if scenario == 'contradictory':
            assert all(row['status'] == 'unknown' and row['receipt_json'] is None for row in commands)
            assert queries == 1
            original = read(base / 'received_original_receipt.json')
            injected = read(base / 'injected_receipt.json')
            assert original['platform_id'] != injected['platform_id']
        else:
            rows = {row['case']: row['body'] for row in read(base / 'http_results.json')}
            if scenario == 'normal':
                for case in ('first', 'duplicate', 'after_restart', 'newer_command_off', 'historical_receipt_after_newer_command'):
                    assert rows[case]['execution_state'] == 'applied'
                assert rows['conflict']['execution_state'] == 'conflict'
                assert rows['final_dashboard_state']['lights']['客厅']['brightness'] == 0
                assert json.loads(current_state_json)['lights']['客厅']['brightness'] == 0
                assert current_version == 2
            elif scenario == 'ack_loss':
                assert rows['ack_loss']['execution_state'] == 'unknown'
                assert rows['query_after_ack_loss']['execution_state'] == rows['after_restart']['execution_state'] == 'applied'
                assert queries >= 1 and current_version == 1
                assert any('ack_dropped_after_commit' in item for item in wire)
            else:
                assert rows['partial_batch']['execution_state'] == rows['partial_duplicate']['execution_state'] == 'partial'
                assert rows['partial_duplicate']['receipt']['completed_indices'] == [0]
                assert not rows['partial_batch']['semantic_complete']
                assert json.loads(current_state_json)['curtains']['客厅']['position'] == 0
                assert current_version == 1
        checks.append({'scenario': scenario, 'submit_counts': counts, 'lookup_messages': queries,
                       'entity_commits': current_version, 'gateway_status': {r['command_id']: r['status'] for r in commands},
                       'owned_processes_stopped': True})
    return {'schema': 'sensehome.integration-v4.offline-audit.v1', 'pass': True, 'checks': checks,
            'scope': 'independent raw log/database recount; software entities only, no physical-device assertion'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = audit(args.run.resolve())
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text, encoding='utf-8')
    print(text)


if __name__ == '__main__':
    main()
