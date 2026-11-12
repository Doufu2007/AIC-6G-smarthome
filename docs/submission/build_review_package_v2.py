"""Build a verified v2 review snapshot; explicitly exclude private trial data."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('base_package', Path(__file__).with_name('build_review_package.py'))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
ADDITIONAL = ['docs/experiments/20261111_real_fault_v2', 'docs/competition_v2',
              'docs/trial_v2', 'docs/value_v2', 'dist/final/20261111_v2']
FILES = ['docs/execution/2026-11-11-evidence-v2.md',
         'docs/reviews/2026-11-11-v2-integration-review.md',
         'docs/reviews/2026-11-11-real-fault-v2-review.md',
         'docs/reviews/2026-11-11-real-fault-v2-independent-counts.json',
         'docs/reviews/2026-11-11-trial-v2-review.md',
         'docs/reviews/2026-11-11-value-v2-review.md',
         'docs/superpowers/specs/2026-11-11-evidence-v2-design.md',
         'docs/superpowers/plans/2026-11-11-evidence-v2.md']


def allowed(path):
    relative = path.relative_to(ROOT)
    if relative.parts[:2] == ('docs', 'trial_v2') and set(relative.parts[2:]) & {'data', 'exports'}:
        return False
    # A repeated build must not embed a stale receipt or another package.
    if relative.parts[:3] == ('dist', 'final', '20261111_v2') and path.name.endswith(('.sha256.json', '.zip')):
        return False
    return base.safe_file(ROOT, path)


def build(output):
    receipt = output.with_suffix('.sha256.json')
    if output.exists() or receipt.exists():
        raise FileExistsError('Refusing to replace a review snapshot')
    paths = {p for p in base.select_files(ROOT) if allowed(p)}
    paths.update(ROOT / name for name in FILES if (ROOT / name).is_file())
    for relative in ADDITIONAL:
        paths.update(p for p in (ROOT / relative).rglob('*') if allowed(p))
    paths = sorted(paths, key=lambda p: p.relative_to(ROOT).as_posix())
    manifest = {p.relative_to(ROOT).as_posix(): {'bytes': p.stat().st_size, 'sha256': base.file_digest(p)} for p in paths}
    assert not any('/trial_v2/data/' in p or '/trial_v2/exports/' in p or '/_work/' in p for p in manifest)
    assert all((ROOT / p).is_file() for p in FILES)
    output.parent.mkdir(parents=True, exist_ok=True)
    scope = ('# SenseHome 第二版本地审阅包\n\n'
             '先读 dist/final/20261111_v2/交付说明.md 与第二版补证报告。方案、答辩、佐证均已整合本轮补证。\n\n'
             '新实验为同机两个模型、受控HTTP代理与PC模拟状态。外部试用0、实际总成本未知。'
             '旧600行及留出失败保留；新增120行独立统计。不含模型权重、凭据、私人试用记录或原始整页抓取。'
             '尚未上传赛事或GitHub。SHA256SUMS.json逐文件核验。\n')
    with zipfile.ZipFile(output, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in paths:
            archive.write(path, 'SenseHome-review-v2/' + path.relative_to(ROOT).as_posix())
        archive.writestr('SenseHome-review-v2/SHA256SUMS.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr('SenseHome-review-v2/先读.md', scope)
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        for name, expected in manifest.items():
            raw = archive.read('SenseHome-review-v2/' + name)
            assert len(raw) == expected['bytes'] and hashlib.sha256(raw).hexdigest() == expected['sha256'], name
    result = {'created_at_utc': datetime.now(timezone.utc).isoformat(), 'package': output.name,
              'bytes': output.stat().st_size, 'sha256': base.file_digest(output), 'n_files': len(paths) + 2,
              'zip_crc_and_hashes_passed': True, 'private_trial_data_excluded': True, 'files': manifest}
    receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items() if k != 'files'}, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/final/20261111_v2/SenseHome_本地审阅包_20261111_v2.zip')
    build(parser.parse_args().output.resolve())
