"""Run active source tests with private default state, never archived snapshots."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def main():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = ROOT / '_work/intrinsic_fix' / ('checks_' + stamp)
    out.mkdir(parents=True, exist_ok=False)
    protected = [ROOT / 'openvela_smart_home/device_state.json', ROOT / 'openvela_smart_home/offload_events.jsonl']
    def hashes(): return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    before = hashes()
    env = {**os.environ, 'PYTHONUTF8': '1', 'SENSEHOME_STATE_FILE': str(out / 'default_state.json'),
           'SENSEHOME_EVENT_LOG': str(out / 'default_events.jsonl'),
           'SENSEHOME_REQUEST_DB': str(out / 'default_requests.sqlite3'), 'SENSEHOME_RECEIPT_DB': '',
           'SENSEHOME_LOCAL_URL': '', 'SENSEHOME_CLOUD_URL': '',
           'SENSEHOME_POLICY_CLI': str(ROOT / 'SenseHome-6G/net_offload/policy_cli.exe')}
    files = ['SenseHome-6G/net_offload/tests', 'openvela_smart_home/dashboard/tests',
        'docs/demo_v4', 'docs/trial_v2', 'docs/trial_v4', 'docs/intrinsic_v5',
        'docs/deployment/fused_eval_tests.py', 'docs/deployment/test_real_fault_eval_v2.py']
    for folder in ('docs/demo_v3', 'docs/demo_v3/device_receipts', 'docs/operations_v4'):
        files.extend(str(p.relative_to(ROOT)) for p in (ROOT / folder).glob('test_*.py'))
    files = [p for p in files if (ROOT / p).exists()]
    command = [sys.executable, '-X', 'utf8', '-m', 'pytest', '--import-mode=importlib', '-q', *files]
    print(json.dumps({'output': str(out), 'command': command}, ensure_ascii=True), flush=True)
    with (out / 'python_tests.txt').open('w', encoding='utf-8') as log:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    # The registered v4 runner deliberately imports its frozen fused_eval
    # under the same module name. Keep that historical compatibility check in
    # a separate interpreter, so it cannot replace the active-source module.
    compatibility_command = [sys.executable, '-X', 'utf8', '-m', 'pytest', '--import-mode=importlib', '-q',
                             'docs/experiments_v4/test_comparison.py']
    with (out / 'historical_compatibility_tests.txt').open('w', encoding='utf-8') as log:
        compatibility = subprocess.run(compatibility_command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    after = hashes()
    receipt = {'command': command, 'returncode': result.returncode, 'protected_before': before,
               'compatibility_command': compatibility_command, 'compatibility_returncode': compatibility.returncode,
               'protected_after': after, 'original_files_unchanged': before == after}
    (out / 'result.json').write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print((out / 'python_tests.txt').read_text(encoding='utf-8')[-18000:], flush=True)
    print((out / 'historical_compatibility_tests.txt').read_text(encoding='utf-8')[-5000:], flush=True)
    print(json.dumps(receipt, ensure_ascii=True), flush=True)
    return (result.returncode or compatibility.returncode) if before == after else 99


if __name__ == '__main__':
    raise SystemExit(main())
