"""Record post-run file inspection and the primary agent's browser check."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import psutil

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for part in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    files = [
        ('llama-server.exe', '../创新赛道/_work/local_llm_v6/bin/llama-server.exe', 'f156c6fcfb61c7dd259e2c577ccbab214185083650f18a9cdfdfb9c38d6cf3b3'),
        ('small', '../创新赛道/_work/local_llm_v5/qwen2.5-1.5b-instruct-q4_k_m.gguf', '6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e'),
        ('large', '../创新赛道/_work/local_llm_v6/Qwen3-4B-Q4_K_M.gguf', '7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5')]
    rows = []
    for label, relative, expected in files:
        path = (ROOT / relative).resolve()
        actual = digest(path)
        assert actual == expected
        rows.append({'label': label, 'path_relative_to_project': relative, 'bytes': path.stat().st_size,
                     'post_run_sha256': actual, 'matches_prior_tool_inspection_digest': True})
    processes = [p.pid for p in psutil.process_iter(['name'])
                 if p.info['name'] and 'llama-server' in p.info['name'].lower()]
    write(ROOT / 'docs/experiments/20261111_real_fault_v2/post_run_runtime_inventory.json', {
        'schema': 'sensehome.post-run-runtime-file-inventory.v2',
        'checked_at_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'post-run file hashes, not a launch-time process or argv snapshot',
        'files': rows, 'current_llama_process_pids': processes,
        'launch_snapshot_available': False,
        'prior_agent_tool_record': {'runtime': 'llama.cpp b11010 Vulkan', 'ports': [63452, 63454],
            'reported_options': ['context 2048', 'parallel 1', 'gpu layers 99', 'flash-attn on',
                                 'no web UI', 'metrics', 'large model reasoning-budget 0'],
            'evidence_kind': 'prior primary-agent tool execution record summarized in session handoff; not independently recovered argv'},
        'limit': 'Current files match previously inspected digests; this does not reconstruct exact historical launch argv or prove process-to-file identity.'})
    run = ROOT / '_work/trial_v2/run_20261110T202323_027dd9/run_manifest.json'
    manifest = json.loads(run.read_text(encoding='utf-8'))
    after = {}
    for relative, before in manifest['protected_before_sha256'].items():
        p = ROOT / relative
        after[relative] = digest(p) if p.is_file() else None
        assert after[relative] == before
    data_dir = ROOT / 'docs/trial_v2/data'
    data_files = list(data_dir.rglob('*.json')) if data_dir.exists() else []
    assert len(data_files) == 0
    write(ROOT / 'docs/trial_v2/verification/browser_acceptance.json', {
        'schema': 'sensehome.browser-acceptance.v2',
        'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'kind': 'automation_test',
        'operator_role': 'developer_self_test', 'tool': 'unified-computer-use cua_repl',
        'scope': 'primary-agent observed localhost UI interaction; not human participant feedback',
        'observed_checks': [
            {'action': 'Start without consent', 'observed': 'Rejected; consent page retained'},
            {'action': 'Start automation session with developer role', 'observed': 'Task UI shown; quotation permission remained unselected'},
            {'action': 'Preset T1: open living-room light', 'observed': 'Actual localhost gateway request; preset action and simulated state confirmed'},
            {'action': 'Preset T2: set two rooms brightness', 'observed': 'Actual localhost gateway request; complete preset confirmation failed; failure retained in memory'},
            {'action': 'Cancel without saving', 'observed': 'UI returned to consent; discarded, not retained; external feedback 0 and connected external sessions 0'}],
        'persisted_trial_json_files_after_cancel': len(data_files), 'external_feedback_count': 0,
        'protected_before_sha256': manifest['protected_before_sha256'], 'protected_after_sha256': after,
        'protected_files_unchanged': True,
        'process_stop': 'tool-managed foreground process stopped after UI check; post-stop hashes independently compared here',
        'source_sha256': manifest['source_sha256'],
        'run_manifest_sha256': digest(run),
        'evidence_limit': 'This authored observation log is not an independent recording of a human trial; no positive feedback was submitted.'})
    print(json.dumps({'post_run_hashes_verified': len(rows), 'llama_process_count': len(processes),
                      'browser_checks': 5, 'persisted_trial_files': len(data_files), 'protected_files_unchanged': True}))


if __name__ == '__main__':
    main()
