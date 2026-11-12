"""Collect current software evidence over HTTP for a clearly labeled replay."""
from pathlib import Path
import json
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'docs/operations_v4'))
from run_acceptance import request, write, now
sys.path.insert(0, str(ROOT / 'docs/trial_v4'))
from run_trial import DemandTrialStore
from collector import PRESETS


def main(port=8879, output=None, collector_output=None):
    output = Path(output) if output else HERE / 'current_runtime_records.json'
    if output.exists():
        raise FileExistsError('KEEP_EXISTING_VIDEO_RECORDS')
    cases = {x['id']: x for x in (json.loads(line) for line in
        (ROOT / 'SenseHome-6G/net_offload/backends/eval_commands.reviewed.jsonl').read_text(encoding='utf-8').splitlines())}
    commands = [cases['S01']['command'], PRESETS['T2']['command'], cases['A01']['command'], cases['R04']['command']]
    records = []
    for i, command in enumerate(commands):
        before = request(port, 'GET', '/api/state')
        response = request(port, 'POST', '/api/command', {'command': command,
            'context': {'command_id': f'video-v4-valid-{i:02d}'}})
        after = request(port, 'GET', '/api/state')
        receipt = request(port, 'GET', f'/api/receipts/video-v4-valid-{i:02d}')
        records.append({'index': i, 'record_kind': 'automation_test', 'at_utc': now(),
            'command': command, 'before': before, 'response': response, 'after': after, 'receipt_query': receipt})
        print(json.dumps({'index': i, 'complete': response['body'].get('semantic_complete'),
            'wall_ms': response['wall_ms']}, ensure_ascii=True), flush=True)
    write(output, {'schema': 'sensehome.video-source.v4',
        'method': 'actual current gateway HTTP with real model and MQTT software entity; not continuous UI recording',
        'records': records})
    with tempfile.TemporaryDirectory() as temporary:
        store = DemandTrialStore(temporary, 'automation_test', f'http://127.0.0.1:{port}',
            {'models_enabled': True, 'source': 'current integrated v4 runtime', 'mqtt_enabled': True})
        sid = store.start({'consent': True, 'role': 'user'})['session_id']
        store.baseline(sid, {'skipped': True})
        task = store.execute(sid, {'command': PRESETS['T2']['command'], 'origin': 'preset', 'preset_id': 'T2'})
        write(collector_output or ROOT / 'docs/trial_v4/full_model_smoke.json', {'record_kind': 'automation_test',
            'baseline': 'explicit automated skip, not participant', 'task': task,
            'participant_feedback_count': store.summary()['participant_feedback_count'],
            'no_feedback_fabricated': True})
        store.cancel(sid)
        print(json.dumps({'collector_completed': task['task_completed'], 'external_feedback': 0}), flush=True)


if __name__ == '__main__':
    main()
