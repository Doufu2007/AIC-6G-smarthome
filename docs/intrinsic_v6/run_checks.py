"""Run the active V6 repair suite against private state; retain raw diagnostics."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def main():
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = ROOT / '_work/intrinsic_v6_fix' / ('checks_' + stamp)
    out.mkdir(parents=True, exist_ok=False)
    protected = [ROOT / 'openvela_smart_home/device_state.json', ROOT / 'openvela_smart_home/offload_events.jsonl']
    def hashes():
        return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
                for path in protected}
    before = hashes()
    sources = sorted({path for folder in ('SenseHome-6G/net_offload', 'SenseHome-6G/net_offload/backends',
                      'openvela_smart_home/dashboard', 'docs/demo_v3', 'docs/demo_v4', 'docs/trial_v2')
                      for path in (ROOT / folder).glob('*')
                      if path.is_file() and path.suffix in {'.py', '.c', '.h', '.html'}} |
                     {ROOT / 'docs/deployment/fused_eval.py'})
    def source_hashes():
        return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    sources_before = source_hashes()
    env = {**os.environ, 'PYTHONUTF8': '1',
           'SENSEHOME_STATE_FILE': str(out / 'default_state.json'),
           'SENSEHOME_EVENT_LOG': str(out / 'default_events.jsonl'),
           'SENSEHOME_REQUEST_DB': str(out / 'default_requests.sqlite3'),
           'SENSEHOME_RECEIPT_DB': '', 'SENSEHOME_LOCAL_URL': '', 'SENSEHOME_CLOUD_URL': '',
           'SENSEHOME_POLICY_CLI': str(ROOT / 'SenseHome-6G/net_offload/policy_cli.exe')}
    files = ['SenseHome-6G/net_offload/tests', 'openvela_smart_home/dashboard/tests',
             'docs/demo_v4', 'docs/trial_v2', 'docs/trial_v4', 'docs/intrinsic_v5',
             'docs/deployment/fused_eval_tests.py', 'docs/deployment/test_real_fault_eval_v2.py']
    for folder in ('docs/demo_v3', 'docs/demo_v3/device_receipts', 'docs/operations_v4'):
        files.extend(str(path.relative_to(ROOT)) for path in (ROOT / folder).glob('test_*.py'))
    files = [path for path in files if (ROOT / path).exists()]
    command = [sys.executable, '-X', 'utf8', '-m', 'pytest', '--import-mode=importlib', '-q', *files]
    print(json.dumps({'output': str(out), 'command': command}, ensure_ascii=False), flush=True)
    started = datetime.now(timezone.utc)
    with (out / 'python_tests.txt').open('w', encoding='utf-8') as log:
        active = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    # Historic frozen runners import a module with the same name as the active
    # evaluator. Keep them in an independent interpreter to avoid cross-binding.
    historic_command = [sys.executable, '-X', 'utf8', '-m', 'pytest', '--import-mode=importlib', '-q',
                        'docs/experiments_v4/test_comparison.py']
    with (out / 'historical_compatibility_tests.txt').open('w', encoding='utf-8') as log:
        historic = subprocess.run(historic_command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    node_command = ['node', '--test', 'docs/intrinsic_v5/test_dashboard_requests.cjs']
    if shutil.which('node'):
        with (out / 'dashboard_client_tests.txt').open('w', encoding='utf-8') as log:
            client = subprocess.run(node_command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
        client_code = client.returncode
    else:
        client_code = 127
        (out / 'dashboard_client_tests.txt').write_text('Node.js is required for the dashboard recovery checks.\n', encoding='utf-8')
    after = hashes()
    sources_after = source_hashes()
    report = {'schema': 'sensehome.intrinsic-v6-checks.v1', 'command': command,
              'returncode': active.returncode, 'historical_command': historic_command,
              'historical_returncode': historic.returncode, 'started_at_utc': started.isoformat(),
              'client_command': node_command, 'client_returncode': client_code,
              'finished_at_utc': datetime.now(timezone.utc).isoformat(),
              'protected_before': before, 'protected_after': after,
              'source_sha256_before': sources_before, 'source_sha256_after': sources_after,
              'source_unchanged_during_checks': sources_before == sources_after,
              'original_files_unchanged': before == after,
              'scope': 'Active Python/C tests with owned temporary MQTT software entities; no true model or physical device test.'}
    (out / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print((out / 'python_tests.txt').read_text(encoding='utf-8')[-16000:], flush=True)
    print((out / 'historical_compatibility_tests.txt').read_text(encoding='utf-8')[-4000:], flush=True)
    print((out / 'dashboard_client_tests.txt').read_text(encoding='utf-8')[-4000:], flush=True)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return (active.returncode or historic.returncode or client_code) if before == after and sources_before == sources_after else 99


if __name__ == '__main__':
    raise SystemExit(main())
