"""Seal distinct anonymous materials and internal reproducibility snapshots."""
from __future__ import annotations

from datetime import datetime, timezone
import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'dist/final/20261111_v4'
spec = importlib.util.spec_from_file_location('v3_package', Path(__file__).with_name('build_review_package_v3.py'))
v3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v3)
NEW = ['docs/competition_v4', 'docs/demo_v4', 'docs/experiments_v4', 'docs/integration_v4',
       'docs/operations_v4', 'docs/trial_v4', 'docs/video/v4', 'dist/final/20261111_v4']
EXTRAS = ['docs/execution/2026-11-11-evidence-v4.md', 'docs/reviews/2026-11-11-v4-independent-review.md',
          'docs/reviews/2026-11-11-v4-independent-review.json',
          'docs/superpowers/plans/2026-11-11-competition-v4.md']
EXCLUDE_DIRS = {'data', 'exports', '__pycache__', '.pytest_cache', '_work',
                'qa', 'renders', 'frames', 'audio', '.git', '.venv', 'node_modules'}
SECRET_PATTERNS = {
    'private_key': re.compile(rb'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'),
    'openai_key': re.compile(rb'\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}\b'),
    'github_token': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
    'aws_access_key': re.compile(rb'\bAKIA[A-Z0-9]{16}\b'),
}
PRIVATE_TEXT = [re.compile(p, re.I) for p in [r'[A-Z]:[\\/]+Users[\\/]', r'郑傅韬', r'大学生活',
                                                        r'华为openjiuwen', r'Doufu2007']]


def video_build_references():
    manifest_path = ROOT / 'docs/video/v4/manifest.json'
    if not manifest_path.is_file():
        return set()
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    references = {item['path'] for item in manifest.get('frames', [])}
    references.update(item['frame'] for item in manifest.get('encoded_frame_checks', []))
    references.add(manifest['synthetic_voice']['narration'])
    references.add(manifest['full_decode_check']['log'])
    probe = manifest['build_directory'] + '/ffprobe.json'
    if (ROOT / probe).is_file():
        references.add(probe)
    return references


VIDEO_BUILD_REFERENCES = video_build_references()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def allowed(path):
    if not path.is_file() or not path.resolve().is_relative_to(ROOT):
        return False
    rel = path.relative_to(ROOT)
    # Preserve only the exact image/narration/decode evidence referenced by the
    # final manifest, excluding raw audio and obsolete or duplicated builds.
    if 'builds' in rel.parts and rel.as_posix() not in VIDEO_BUILD_REFERENCES:
        return False
    if EXCLUDE_DIRS.intersection(rel.parts) or path.name.lower().endswith(('.zip', '.sha256.json')):
        return False
    if path.name in {'package_audit.json', 'package_seal_report.json'} or 'v4-seal-review' in path.name:
        return False
    if rel.as_posix() == 'docs/experiments_v4/frozen_source/SenseHome-6G/net_offload/policy_cli.exe':
        return True
    if path.suffix.lower() in {'.sqlite3', '.sqlite', '.cjs'} and rel.parts[0] == 'docs':
        return True
    return v3.allowed(path)


def text_parts(path, raw):
    if path.suffix in {'.docx', '.pptx'}:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            return '\n'.join(z.read(n).decode('utf-8', errors='replace') for n in z.namelist() if n.endswith('.xml'))
    if path.suffix == '.pdf':
        import fitz
        with fitz.open(stream=raw, filetype='pdf') as doc:
            return '\n'.join(p.get_text() for p in doc)
    if path.suffix in {'.mp4', '.png', '.exe', '.sqlite3', '.sqlite'}:
        return ''
    return raw.decode('utf-8', errors='replace')


def scan(paths, anonymous):
    path_count = 0
    findings = []
    for path in paths:
        raw = path.read_bytes()
        rel = path.relative_to(ROOT).as_posix()
        content = text_parts(path, raw)
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(raw) or pattern.search(content.encode('utf-8')):
                findings.append({'path': rel, 'kind': label})
        if any(p.search(content) for p in PRIVATE_TEXT):
            path_count += 1
            if anonymous:
                findings.append({'path': rel, 'kind': 'private_identity_or_path'})
    if findings:
        raise AssertionError(json.dumps({'scan_findings': findings}, ensure_ascii=True))
    return {'status': 'PASS', 'credential_patterns': list(SECRET_PATTERNS),
            'private_identity_or_path_file_count': path_count, 'anonymous': anonymous,
            'scope': 'finite pattern scan; not a guarantee of exhaustive privacy review'}


def seal(paths, filename, prefix, readme, anonymous):
    target = OUT / filename
    receipt = target.with_suffix('.sha256.json')
    if target.exists() or receipt.exists():
        raise FileExistsError('Never overwrite a sealed snapshot: ' + filename)
    paths = sorted(set(paths), key=lambda p: p.relative_to(ROOT).as_posix())
    scan_result = scan(paths, anonymous)
    manifest = {p.relative_to(ROOT).as_posix(): {'bytes': p.stat().st_size, 'sha256': sha(p.read_bytes())} for p in paths}
    with zipfile.ZipFile(target, 'x', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for p in paths:
            raw = p.read_bytes()
            rel = p.relative_to(ROOT).as_posix()
            if sha(raw) != manifest[rel]['sha256']:
                raise RuntimeError('Source changed while sealing: ' + rel)
            archive.writestr(prefix + rel, raw)
        archive.writestr(prefix + 'SHA256SUMS.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.writestr(prefix + '先读.md', readme)
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('ZIP CRC failure')
        for rel, meta in manifest.items():
            raw = archive.read(prefix + rel)
            if len(raw) != meta['bytes'] or sha(raw) != meta['sha256']:
                raise RuntimeError('Archived hash mismatch: ' + rel)
    report = {'schema': 'sensehome.package-seal.v4', 'created_at_utc': datetime.now(timezone.utc).isoformat(),
              'package': filename, 'bytes': target.stat().st_size, 'sha256': sha(target.read_bytes()),
              'n_files': len(paths) + 2, 'zip_crc_and_hashes_passed': True,
              'private_trial_data_excluded': True, 'scan': scan_result, 'files': manifest}
    receipt.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return {k: v for k, v in report.items() if k != 'files'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--internal-only', action='store_true')
    parser.add_argument('--internal-filename', default='SenseHome_内部复现审阅包_20261111_v4.zip')
    args = parser.parse_args()
    if Path(args.internal_filename).name != args.internal_filename or not args.internal_filename.endswith('.zip'):
        raise ValueError('Internal filename must be one ZIP basename')
    required = [OUT / ('SenseHome_' + stem + '_20261111_v4' + ext) for stem, ext in
                [('场景设计方案', '.pdf'), ('场景设计方案', '.docx'), ('场景设计答辩', '.pdf'),
                 ('场景设计答辩', '.pptx'), ('合并佐证材料', '.pdf'), ('当前软件证据回放', '.mp4')]]
    required += [OUT / n for n in ['作品简介.txt', '主张指标证据页码索引.md', '提交检查表.json', '提交检查表.md', '交付说明.md']]
    if not all(p.is_file() for p in required):
        raise FileNotFoundError('Final material or delivery note missing')
    checks = json.loads((OUT / '提交检查表.json').read_text(encoding='utf-8'))
    if not checks['anonymous_scan']['passed']:
        raise AssertionError('Material anonymity QA failed')
    if args.internal_only:
        public = 'Previously sealed anonymous package preserved unchanged.'
    else:
        public = seal(required, 'SenseHome_匿名材料审阅包_20261111_v4.zip', 'SenseHome-materials-v4/',
                      '# v4 匿名材料审阅包\n\n方案、答辩、佐证、简介、视频与检查表。尚未上传赛事。'
                      '视频为真实运行记录的编辑回放，非连续现场录屏。真实需求、人工独立部署、成本凭据、'
                      '永久分享链接及最终人工核对仍待补；不保证获奖。\n', True)
    paths = {p for p in v3.v2.base.select_files(ROOT) if allowed(p)}
    for directory in v3.v2.ADDITIONAL + v3.MORE + NEW:
        paths.update(p for p in (ROOT / directory).rglob('*') if allowed(p))
    paths.update(ROOT / p for p in v3.FILES + EXTRAS if (ROOT / p).is_file())
    paths.update(p for p in (ROOT / 'docs/reviews').glob('*v4*') if allowed(p))
    material_manifest = json.loads((OUT / 'source_manifest.json').read_text(encoding='utf-8'))
    for meta in material_manifest['sources']:
        source = ROOT / meta['path']
        if not allowed(source) or sha(source.read_bytes()) != meta['sha256']:
            raise AssertionError('Material source is missing/unsafe/changed: ' + meta['path'])
        paths.add(source)
    require = [p.relative_to(ROOT).as_posix() for p in paths]
    if any('/trial_v4/data/' in p or '/trial_v2/data/' in p or '/_work/' in p for p in require):
        raise AssertionError('Private runtime selection')
    internal = seal(paths, args.internal_filename, 'SenseHome-review-v4/',
                    '# v4 内部复现审阅包\n\n仅供团队内部审阅，不可直接公开上传。'
                    '原始自动化证据为保留哈希与可追溯性，含本机绝对路径和历史版本；对外使用匿名材料包。'
                    '不含真人试用私有记录、模型权重、推理DLL或账号凭据。已保留历史失败与修复证据。'
                    '先读docs/execution/2026-11-11-evidence-v4.md及dist/final/20261111_v4/交付说明.md。'
                    '两模型同机共享GPU，MQTT为软件实体，无实物或6G空口测量。\n', False)
    print(json.dumps({'anonymous_materials': public, 'internal_reproducibility': internal}, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
