"""Build a bounded local review snapshot, verify every archived hash and CRC."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
DIRECTORIES = (
    'SenseHome-6G/net_offload', 'SenseHome-6G/compliance',
    'SenseHome-6G/evidence/network_compute/20261111_fused',
    'openvela_smart_home/dashboard', 'docs/deployment', 'docs/value',
    'docs/submission', 'docs/experiments/20261111_fused_real',
    'docs/experiments/20261111_local_resources',
    'docs/experiments/autodl_20261103_normalized_v2', 'dist/final/20261111',
)
ROOT_FILES = ('README.md', 'LICENSE', '答辩PPT_页面大纲.md', '提交材料差距审计.md',
              'SenseHome-6G/作品方案_场景设计_v1.md',
              'SenseHome-6G/作品方案_场景设计_20261111.md',
              'SenseHome-6G/figures/submission_20261111_architecture.png',
              'SenseHome-6G/figures/submission_20261111_workflow.png',
              'SenseHome-6G/evidence/current/dashboard/dashboard_results.json',
              'docs/video/v2/software_replay.json',
              'docs/execution/2026-11-11-evidence-completion.md',
              'docs/reviews/2026-11-11-evidence-review.md',
              'docs/superpowers/specs/2026-11-11-evidence-completion-design.md',
              'docs/superpowers/plans/2026-11-11-evidence-completion.md')
EXTENSIONS = {'.py', '.c', '.h', '.ps1', '.sh', '.md', '.txt', '.json', '.jsonl',
              '.csv', '.html', '.css', '.pdf', '.docx', '.pptx', '.mp4', '.png', '.log'}
EXCLUDED_DIRS = {'_work', '__pycache__', '.pytest_cache', '.git', '.venv',
                 'node_modules', 'qa', 'renders', 'frames', 'audio', 'quarantine_untrusted_reviewed'}


def safe_file(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    lower = path.name.lower()
    own_policy_binary = lower == 'policy_cli.exe' and ('source_snapshot' in relative.parts or
                          relative.as_posix() == 'SenseHome-6G/net_offload/policy_cli.exe')
    license_notice = lower in {'license', 'notice', 'copying'} or lower.endswith('-license')
    return (path.is_file() and path.resolve().is_relative_to(root.resolve())
            and not EXCLUDED_DIRS.intersection(relative.parts)
            and not lower.startswith('.env') and '.private.' not in lower
            and 'password' not in lower and 'access-token' not in lower
            and (path.suffix.lower() in EXTENSIONS or own_policy_binary or license_notice)
            and lower not in {'device_state.json', 'state.json'})


def select_files(root: Path) -> list[Path]:
    root = Path(root).resolve()
    selected = {root / name for name in ROOT_FILES if (root / name).is_file()}
    for relative in DIRECTORIES:
        directory = root / relative
        if directory.is_dir():
            selected.update(p for p in directory.rglob('*') if safe_file(root, p))
    return sorted(selected, key=lambda p: p.relative_to(root).as_posix())


def file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def build(root: Path, output: Path) -> dict:
    root, output = Path(root).resolve(), Path(output).resolve()
    sidecar = output.with_suffix('.sha256.json')
    if output.exists() or sidecar.exists():
        raise FileExistsError('Refusing to overwrite a review snapshot')
    paths = select_files(root)
    manifest = {p.relative_to(root).as_posix(): {'bytes': p.stat().st_size, 'sha256': file_digest(p)} for p in paths}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in paths:
            archive.write(path, 'SenseHome-review/' + path.relative_to(root).as_posix())
        archive.writestr('SenseHome-review/SHA256SUMS.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr('SenseHome-review/REVIEW_SCOPE.md',
                         '# Local review snapshot\n\nCurrent local code, evidence and deliverables. '
                         'Not uploaded to the competition. No model weights, credentials or private files. '
                         'Pre-fix records remain historical; use the final run index. '
                         'See docs/execution/2026-11-11-evidence-completion.md for completion status.\n')
    with zipfile.ZipFile(output) as archive:
        failed = archive.testzip()
        if failed is not None:
            raise RuntimeError('ZIP CRC failed: ' + failed)
        for name, expected in manifest.items():
            raw = archive.read('SenseHome-review/' + name)
            if len(raw) != expected['bytes'] or hashlib.sha256(raw).hexdigest() != expected['sha256']:
                raise RuntimeError('Archived hash mismatch: ' + name)
    report = {'created_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'package': output.name,
              'bytes': output.stat().st_size, 'sha256': file_digest(output),
              'n_files': len(paths) + 2, 'zip_crc_and_hashes_passed': True, 'files': manifest}
    sidecar.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    report = build(ROOT, args.output)
    print(json.dumps({key: value for key, value in report.items() if key != 'files'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
