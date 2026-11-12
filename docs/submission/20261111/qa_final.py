from __future__ import annotations
import hashlib,json,subprocess,sys
from pathlib import Path
from zipfile import ZipFile
import fitz
from materials import ROOT,NAME,ABSTRACT,PLAN,render_markdown
from build_submission import OUT,WORK,SOURCE_PATHS,sha

# Keep the editable Markdown figure references inside the distributable tree.
for name in ['architecture','workflow']:
    (ROOT/f'SenseHome-6G/figures/submission_20261111_{name}.png').write_bytes((WORK/f'{name}.png').read_bytes())
for path in ['SenseHome-6G/作品方案_场景设计_v1.md','SenseHome-6G/作品方案_场景设计_20261111.md']:
    (ROOT/path).write_text(render_markdown(PLAN),encoding='utf-8')

pdfs=list(OUT.glob('*.pdf'));read={p.name:fitz.open(p) for p in pdfs}
texts={k:'\n'.join(page.get_text() for page in d) for k,d in read.items()}
for k,t in texts.items():
    assert not any(x in t for x in ['待填','郑傅韬','大学生活','依据用户允许','Codex依赖加载器','指导教师姓名'])
    assert all(len(page.get_text().strip())>(50 if '答辩' in k else 100) for page in read[k]),(k,'short/orphan page')
    assert '�' not in t
    if '方案' in k or '佐证' in k:
        assert all(x in t for x in ['617.349','5995.330','635.609','1402.547','627.756','4097.427','632.459','1284.334'])
        assert all(x not in t for x in ['581.496','1238.693','614.036','957.479','619.873','1033.887','600.715','1043.262'])
assert len(NAME)<=20 and 300<=len(ABSTRACT)<=500
assert (OUT/'SenseHome_场景设计方案_20261111.pdf').stat().st_size<15_000_000
video=json.loads((OUT/'video_manifest.json').read_text(encoding='utf-8'))
assert 180<=video['duration_s']<=300 and video['size_bytes']<300_000_000
assert sha(OUT/'SenseHome_演示视频_20261111.mp4')==video['sha256']
assert (ROOT/'docs/experiments/20261111_fused_real/reviewed_20_sealed_repeat3/report.json').is_file()
assert sha(ROOT/'docs/experiments/20261111_fused_real/reviewed_20_sealed_repeat3/report.json')=='d695368596da69fe400cea10991584932742cd43bd60bd85ab16290693725322'
sources=json.loads((OUT/'source_manifest.json').read_text(encoding='utf-8'))
assert all(sha(ROOT/x['path'])==x['sha256'] for x in sources['sources'])

skill=Path('C:/Users/郑傅韬/.codex/plugins/cache/openai-primary-runtime/presentations/26.909.12148/skills/presentations/container_tools')
checks=[]
for program,args in [('inspect_presentation_package_integrity.py',['--fail-on-findings']),('inspect_presentation_layout_geometry.py',['--expected-slide-count','14','--expected-aspect','16:9','--validate-heading-fit','--fail-on-findings'])]:
    p=subprocess.run([sys.executable,str(skill/program),str(OUT/'SenseHome_场景设计答辩_20261111.pptx'),*args],capture_output=True,text=True)
    checks.append({'tool':program,'exit_code':p.returncode,'result':json.loads(p.stdout)})
    assert p.returncode==0,p.stdout+p.stderr

receipt={'status':'passed','title_chars':len(NAME),'abstract_chars':len(ABSTRACT),'pdf_pages':{k:len(v) for k,v in read.items()},'pptx_validation':checks,'anonymous_text_scan':'passed; no known personal identity, unit name, placeholders or internal tooling explanation in PDFs','scope':'four final matrices remain separate: 240 human-frozen regression, 144 development, 72 injected observations, 144 preregistered programmatic holdout; r3 synthetic, historical AutoDL and PC state replay separate','render_review':'Earlier plan/slide/evidence pages visually inspected from LibreOffice/PyMuPDF PNGs; latest plan holdout/diagrams and evidence holdout/resource pages rechecked by primary agent; no clipped text, overlap, orphan page or replacement glyph observed','runtime_fallback':'Codex dependency loader unconfigured and artifact-tool absent. Authorized existing local Python libraries, LibreOffice and ffmpeg; no global installs. Skill render_docx.py attempted but missing pdf2image; native LibreOffice PDF and PyMuPDF PNG rendering used and inspected.','video':video,'deliverables':[{'path':p.name,'bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(OUT.iterdir()) if p.suffix.lower() in ['.pdf','.docx','.pptx','.mp4']]}
(ROOT/'docs/submission/20261111/qa_final.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'status':receipt['status'],'pages':receipt['pdf_pages'],'video_duration':video['duration_s'],'video_bytes':video['size_bytes'],'title_chars':len(NAME),'abstract_chars':len(ABSTRACT)},ensure_ascii=True,indent=2))
