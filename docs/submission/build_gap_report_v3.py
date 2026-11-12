"""Build the current gap/repair report without changing sealed v1/v2 artifacts."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle, PageBreak, Image
import fitz

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'dist/final/20261111_v3'
QA = ROOT / '_work/submission_20261111_v3'
SOURCES = ['docs/competition_v3/整改与冲刺路线.md', 'docs/competition_v3/官方评分逐项差距.md',
           'docs/competition_v3/获奖案例与差距参照.md', 'docs/competition_v3/答辩20问证据卡.md']
PDF = OUT / 'SenseHome_国一竞争差距与整改报告_20261111_v3.pdf'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inline(value):
    value = value.replace('\u2011', '-').replace('\u2014', '-').replace('\u2013', '-')
    links = []
    def link(match):
        links.append((match[1], match[2])); return '@@LINK' + str(len(links) - 1) + '@@'
    value = re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)', link, value)
    value = escape(value.replace('`', ''))
    value = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', value)
    for index, (label, url) in enumerate(links):
        value = value.replace('@@LINK' + str(index) + '@@', '<link color="#23635C" href="' + escape(url, {'"':'&quot;'}) + '">' + escape(label) + '</link>')
    return value


def build():
    OUT.mkdir(parents=True, exist_ok=True); QA.mkdir(parents=True, exist_ok=True)
    pdfmetrics.registerFont(TTFont('GapCN', 'C:/Windows/Fonts/simhei.ttf'))
    pdfmetrics.registerFontFamily('GapCN', normal='GapCN', bold='GapCN', italic='GapCN', boldItalic='GapCN')
    styles = {key: ParagraphStyle(key, fontName='GapCN', fontSize=size, leading=leading,
              wordWrap='CJK', textColor=colors.HexColor('#172D3A'), spaceAfter=after,
              keepWithNext=key in ('h1','h2','h3'))
              for key,size,leading,after in [('h1',20,29,15),('h2',14,21,11),('h3',11.5,18,8),
                                            ('body',10.3,17,8),('cell',8.4,13.3,0),('header',8.7,13.5,0)]}
    story = []
    sections = []
    for index, source in enumerate(SOURCES):
        if index: story.append(PageBreak())
        raw = (ROOT / source).read_text(encoding='utf-8')
        sections.append({'source':source,'sha256':sha(ROOT / source)})
        lines = raw.splitlines(); cursor = 0
        while cursor < len(lines):
            line = lines[cursor].strip()
            if not line: cursor += 1; continue
            if '官方评分逐项差距' in source and line.startswith('官方原件：'):
                # The same official sources are listed at the front and in
                # criteria.json; avoid a nearly empty trailing source page.
                cursor += 1; continue
            if line.startswith('|'):
                table_lines = []
                while cursor < len(lines) and lines[cursor].strip().startswith('|'):
                    parts = [x.strip() for x in lines[cursor].strip().strip('|').split('|')]
                    if not all(re.fullmatch(r':?-+:?', p) for p in parts): table_lines.append(parts)
                    cursor += 1
                n = len(table_lines[0]); assert all(len(row)==n for row in table_lines)
                ratios = {3:[.22,.39,.39],4:[.20,.26,.27,.27],5:[.17,.20,.21,.21,.21],6:[.14,.14,.18,.18,.18,.18]}.get(n,[1/n]*n)
                # Three-column Q&A requires more room for the answer and evidence.
                if '20问' in source and n==3: ratios=[.19,.44,.37]
                if '获奖案例' in source and n==5: ratios=[.22,.15,.23,.20,.20]
                data = [[Paragraph(inline(cell), styles['header' if i==0 else 'cell']) for cell in row]
                        for i,row in enumerate(table_lines)]
                table = LongTable(data, colWidths=[182*mm*r for r in ratios], repeatRows=1, hAlign='LEFT')
                table.setStyle(TableStyle([
                    ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#E9EFEC')),
                    ('VALIGN',(0,0),(-1,-1),'TOP'), ('GRID',(0,0),(-1,-1),.35,colors.HexColor('#C8D2CE')),
                    ('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),
                    ('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7),
                    ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#F6F8F6')])]))
                story += [table, Spacer(1,10)]
                continue
            match = re.match(r'^(#{1,3})\s+(.+)',line)
            if match:
                story.append(Paragraph(inline(match[2]), styles['h'+str(len(match[1]))]))
            else:
                story.append(Paragraph(inline(line),styles['body']))
            cursor += 1
    story += [PageBreak(),Paragraph('复核入口与版本说明',styles['h1'])]
    for text in [
        '正文第一部分为本轮整改；官方差距表以v2证据作基线，新增v3事实以前部及对应原始记录为准。获奖案例来自其他赛事，不是本专项赛评分基准。',
        '真实模型矩阵：docs/experiments/20261111_race_v3_portfix/；启动失败保留：docs/experiments/20261111_race_v3/。',
        '独立复算：python -X utf8 docs/demo_v3/verify_race.py；审查：docs/reviews/2026-11-11-v3-results-review.md。',
        '默认演示：python -X utf8 docs/demo_v3/run_live.py；真实模型演示加--models。操作：docs/demo_v3/README_live.md。',
        '设备合同：docs/demo_v3/device_receipts/README.md。浏览验收：docs/demo_v3/browser/。本次自动化不是外部用户反馈。',
        '方案和答辩等上一版封存材料在dist/final/20261111_v2/，不会自动变为新增功能的现场演示。此报告不是已上传的赛事提交件。',
    ]: story.append(Paragraph(inline(text),styles['body']))
    story.append(Paragraph('源文件版本',styles['h2']))
    for item in sections:
        story.append(Paragraph(inline(item['source']+' / SHA256 '+item['sha256']),styles['body']))
    def page(canvas, doc):
        canvas.setTitle('SenseHome 国一竞争差距与第三轮整改'); canvas.setAuthor('')
        canvas.setFont('GapCN',8); canvas.setFillColor(colors.HexColor('#52615B'))
        canvas.drawString(14*mm,286*mm,'SenseHome / 场景设计 / 2026-11-11 v3 / 本机审阅版')
        canvas.drawRightString(196*mm,10*mm,str(doc.page))
    doc = SimpleDocTemplate(str(PDF), pagesize=A4, leftMargin=14*mm, rightMargin=14*mm,
                            topMargin=20*mm, bottomMargin=19*mm)
    doc.build(story,onFirstPage=page,onLaterPages=page)
    combined = '\n\n---\n\n'.join((ROOT / p).read_text(encoding='utf-8') for p in SOURCES)
    (OUT / 'SenseHome_国一竞争差距与整改报告_20261111_v3.md').write_text(combined,encoding='utf-8')
    document = fitz.open(PDF)
    renders = QA / 'renders'; renders.mkdir(exist_ok=True)
    text = []
    for index,p in enumerate(document):
        p.get_pixmap(matrix=fitz.Matrix(1.1,1.1),alpha=False).save(renders/f'page-{index+1:02}.png')
        text.append(p.get_text())
    (QA / 'extracted.txt').write_text('\n'.join(text),encoding='utf-8')
    meta={'created_at_utc':datetime.now(timezone.utc).isoformat(),'pdf':PDF.relative_to(ROOT).as_posix(),
          'bytes':PDF.stat().st_size,'sha256':sha(PDF),'n_pages':len(document),'sources':sections,
          'rendered_all_pages':True,'visual_review':'pending'}
    (OUT / 'report_manifest.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(meta,ensure_ascii=True,indent=2))


if __name__=='__main__': build()
