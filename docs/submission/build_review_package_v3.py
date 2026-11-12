"""Create a fresh v3 review ZIP, excluding private trials, weights and credentials."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('v2_package', Path(__file__).with_name('build_review_package_v2.py'))
v2 = importlib.util.module_from_spec(spec); spec.loader.exec_module(v2)
MORE = ['docs/competition_v3','docs/demo_v3','docs/experiments/20261111_race_v3',
        'docs/experiments/20261111_race_v3_portfix','dist/final/20261111_v3']
FILES = v2.FILES + ['docs/execution/2026-11-11-context-v3.md',
         'docs/execution/2026-11-11-evidence-v3.md',
         'docs/reviews/2026-11-11-v3-preflight-review.md',
         'docs/reviews/2026-11-11-v3-results-review.md',
         'docs/reviews/2026-11-11-v3-independent-counts.json',
         'docs/reviews/2026-11-11-v3-integration-review.md',
         'docs/superpowers/specs/2026-11-11-competition-v3-design.md',
         'docs/superpowers/plans/2026-11-11-competition-v3.md']


def allowed(path):
    rel=path.relative_to(ROOT)
    if rel.parts[:3] in [('dist','final','20261111_v2'),('dist','final','20261111_v3')]:
        if path.name.endswith(('.zip','.sha256.json')): return False
    if 'trial_v2' in rel.parts and set(rel.parts) & {'data','exports'}: return False
    # The browser JS behavior tests are part of the evidence delivery.
    if path.suffix=='.cjs':
        return (path.is_file() and path.resolve().is_relative_to(ROOT.resolve())
                and not v2.base.EXCLUDED_DIRS.intersection(rel.parts))
    return v2.base.safe_file(ROOT,path)


def build():
    out=ROOT/'dist/final/20261111_v3/SenseHome_本地审阅包_20261111_v3.zip'
    receipt=out.with_suffix('.sha256.json')
    if out.exists() or receipt.exists(): raise FileExistsError('Never overwrite a review snapshot')
    paths={p for p in v2.base.select_files(ROOT) if allowed(p)}
    for directory in v2.ADDITIONAL+MORE:
        paths.update(p for p in (ROOT/directory).rglob('*') if allowed(p))
    assert all((ROOT/p).is_file() for p in FILES)
    paths.update(ROOT/p for p in FILES)
    paths=sorted(paths,key=lambda p:p.relative_to(ROOT).as_posix())
    manifest={p.relative_to(ROOT).as_posix():{'bytes':p.stat().st_size,'sha256':v2.base.file_digest(p)} for p in paths}
    assert not any('/trial_v2/data/' in n or '/trial_v2/exports/' in n or '/_work/' in n for n in manifest)
    assert not any(n.lower().endswith(('.gguf','.dll')) or '/.firecrawl/' in n for n in manifest)
    prefix='SenseHome-review-v3/'
    with zipfile.ZipFile(out,'x',zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for p in paths: archive.write(p,prefix+p.relative_to(ROOT).as_posix())
        archive.writestr(prefix+'SHA256SUMS.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        archive.writestr(prefix+'先读.md',
            '# SenseHome 第三轮本地审阅包\n\n先读dist/final/20261111_v3/交付说明.md与19页差距整改报告。'
            'v3是增量整改，v2方案/答辩/视频保留为历史材料。正式赛事材料需统一后由参赛者上传。\n\n'
            '新增36行独立报告，先前600/120行不改；含首次端口启动失败。全部设备仍为PC软件状态，'
            '双模型同机共享GPU；外部反馈0、总成本未知、无6G空口或实物认证。'
            '不含权重、DLL、凭据、私有试用记录或整页网页抓取。未提交Git或赛事。\n')
    with zipfile.ZipFile(out) as archive:
        assert archive.testzip() is None
        for name,meta in manifest.items():
            raw=archive.read(prefix+name)
            assert len(raw)==meta['bytes'] and hashlib.sha256(raw).hexdigest()==meta['sha256'],name
    report={'created_at_utc':datetime.now(timezone.utc).isoformat(),'package':out.name,
            'bytes':out.stat().st_size,'sha256':v2.base.file_digest(out),'n_files':len(paths)+2,
            'zip_crc_and_hashes_passed':True,'private_trial_data_excluded':True,'files':manifest}
    receipt.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k!='files'},ensure_ascii=True,indent=2))


if __name__=='__main__': build()
