"""Recover the hash-confirmed original event prefix, retaining all test appends."""
from pathlib import Path
import hashlib
import json

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs/operations_v4/event_isolation_repair'
EXPECTED = 'f8871ff4c70a5c03e5fbd7e6fc32ea224a61c6cae34279545c674ae3da3abc54'


def main():
    OUT.mkdir(exist_ok=False)
    target = ROOT / 'openvela_smart_home/offload_events.jsonl'
    raw = target.read_bytes()
    (OUT / 'full_before_restore.jsonl').write_bytes(raw)
    offsets = []
    for i, value in enumerate(raw):
        if value == 10 and hashlib.sha256(raw[:i + 1]).hexdigest() == EXPECTED:
            offsets.append(i + 1)
    if len(offsets) != 1:
        raise RuntimeError('NO_UNIQUE_HASH_CONFIRMED_ORIGINAL_PREFIX_NO_RESTORE')
    boundary = offsets[0]
    prefix, added = raw[:boundary], raw[boundary:]
    (OUT / 'isolated_test_appends.jsonl').write_bytes(added)
    target.write_bytes(prefix)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == EXPECTED
    (OUT / 'repair.json').write_text(json.dumps({'schema': 'sensehome.confirmed-test-log-repair.v4',
        'before_sha256': hashlib.sha256(raw).hexdigest(), 'original_sha256': EXPECTED,
        'original_prefix_bytes': boundary, 'appended_test_bytes': len(added),
        'appended_test_rows': len(added.splitlines()), 'restored_prefix_exactly': True,
        'evidence_preserved': ['full_before_restore.jsonl', 'isolated_test_appends.jsonl'],
        'cause': 'new dashboard integration tests used original default event sink; fixed test fixture separately',
        'not_a_successful_isolated_run': '_work/demo_v3/live_20261111T064929_d0e32a33/run_manifest.json records failure; not overwritten'},
        indent=2), encoding='utf-8')
    print('RESTORED_HASH_CONFIRMED_ORIGINAL_PREFIX_AND_PRESERVED_TEST_APPENDS')


if __name__ == '__main__':
    main()
