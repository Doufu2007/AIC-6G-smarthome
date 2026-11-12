from __future__ import annotations
import hashlib, json, os, subprocess, sys, zipfile
from pathlib import Path
from xml.sax.saxutils import escape
from materials import *
from docx import Document
from docx.shared import Cm, Pt, RGBColor
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from pptx import Presentation
from pptx.util import Inches, Pt as PPt
from pptx.dml.color import RGBColor as PRGB
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION
from pptx.chart.data import CategoryChartData
from pptx.enum.shapes import MSO_SHAPE, MSO_CONNECTOR
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
import fitz

OUT=ROOT/'dist/final/20261111'; OUT.mkdir(parents=True,exist_ok=True)
WORK=ROOT/'_work/submission_20261111'; WORK.mkdir(parents=True,exist_ok=True)
SOFFICE='C:/Program Files/LibreOffice/program/soffice.exe'
CN='Microsoft YaHei'; NAVY='172D3A'; GREEN='287366'; GRAY='55636A'; BG='F5F5F0'
MODEL_DATA=json.loads((ROOT/MODEL/'eval.formal.json').read_text(encoding='utf-8'))
assert MODEL_DATA['summary']['n_action_correct']==17
assert len(ABSTRACT)>=300 and len(ABSTRACT)<=500

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def run(args):
    p=subprocess.run(list(map(str,args)),capture_output=True,text=True,encoding='utf-8',errors='replace')
    if p.returncode:raise RuntimeError(str(args)+'\n'+p.stdout+'\n'+p.stderr)
    return p.stdout

def doc_font(run,name='宋体',size=12,bold=False):
    run.font.name=name; run.font.size=Pt(size); run.bold=bold
    run._element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'),name)

def make_docx():
    d=Document(); sec=d.sections[0]
    sec.page_width=Cm(21);sec.page_height=Cm(29.7)
    sec.top_margin=Cm(2.5);sec.bottom_margin=Cm(2.5);sec.left_margin=Cm(3);sec.right_margin=Cm(3)
    sec.header_distance=Cm(1.5);sec.footer_distance=Cm(1.5)
    n=d.styles['Normal']; n.font.name='宋体';n.font.size=Pt(12); n._element.rPr.rFonts.set(qn('w:eastAsia'),'宋体')
    n.paragraph_format.line_spacing=1.5;n.paragraph_format.space_after=Pt(7)
    for sty,sz in [('Title',22),('Heading 1',16),('Heading 2',14)]:
        s=d.styles[sty];s.font.name='宋体';s.font.size=Pt(sz);s.font.bold=True;s.font.color.rgb=RGBColor(0,0,0);s._element.rPr.rFonts.set(qn('w:eastAsia'),'宋体')
        s.paragraph_format.space_before=Pt(0);s.paragraph_format.space_after=Pt(10)
    for s in d.styles:
        for border in s.element.xpath('.//w:pBdr'):border.getparent().remove(border)
    h=sec.header.paragraphs[0];r=h.add_run('SenseHome  场景设计方案');doc_font(r,size=9)
    f=sec.footer.paragraphs[0];f.alignment=1
    r=f.add_run();fld=OxmlElement('w:fldSimple');fld.set(qn('w:instr'),'PAGE');r._r.addnext(fld)
    for i,p in enumerate(PLAN):
        if i:d.add_page_break()
        d.add_paragraph(p['title'],'Title' if i==0 else 'Heading 1')
        for b in p['blocks']:
            if b['kind']=='h2':d.add_paragraph(b['text'],'Heading 2')
            elif b['kind']=='p':d.add_paragraph(b['text'])
            elif b['kind']=='image':
                d.add_picture(str(ROOT/b['path']),width=Cm(15))
                pp=d.add_paragraph(b['caption']);pp.alignment=1
                for rr in pp.runs:doc_font(rr,size=10)
            else:
                t=d.add_table(rows=1,cols=len(b['headers'])); t.style='Table Grid';t.autofit=False
                for j,s in enumerate(b['headers']):t.rows[0].cells[j].text=s
                for row in b['rows']:
                    cells=t.add_row().cells
                    for j,s in enumerate(row):cells[j].text=s
                for ri,row in enumerate(t.rows):
                    for j,cell in enumerate(row.cells):
                        if b.get('widths'):cell.width=Cm(b['widths'][j]/10)
                        for pp in cell.paragraphs:
                            pp.paragraph_format.line_spacing=1.15;pp.paragraph_format.space_after=Pt(4);pp.paragraph_format.space_before=Pt(3)
                            for rr in pp.runs:doc_font(rr,size=10,bold=ri==0)
                        if ri==0:
                            sh=OxmlElement('w:shd');sh.set(qn('w:fill'),'EEEEEB');cell._tc.get_or_add_tcPr().append(sh)
                d.add_paragraph().paragraph_format.space_after=Pt(0)
    d.core_properties.title=NAME+' 场景设计方案';d.core_properties.author='';d.core_properties.last_modified_by=''
    path=OUT/'SenseHome_场景设计方案_20261111.docx';d.save(path)
    return path

def text(slide,s,x,y,w,h,size=22,bold=False,color=NAVY):
    tb=slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h)); tf=tb.text_frame;tf.word_wrap=True
    tf.margin_left=0;tf.margin_right=0;tf.margin_top=0;tf.margin_bottom=0
    for k,line in enumerate(s.split('\n')):
        p=tf.paragraphs[0] if k==0 else tf.add_paragraph();p.text=line;p.space_after=PPt(12)
        for r in p.runs:r.font.name=CN;r.font.size=PPt(size);r.font.bold=bold;r.font.color.rgb=PRGB.from_string(color)
    return tb

def ppt_table(slide,rows,x=0.65,y=1.65,w=12,h=None,size=19,widths=None):
    if h is None:h=.68*len(rows)
    shape=slide.shapes.add_table(len(rows),len(rows[0]), Inches(x),Inches(y), Inches(w),Inches(h));t=shape.table
    if widths:
        for c,v in zip(t.columns,widths):c.width=Inches(v)
    for i,row in enumerate(rows):
        for j,v in enumerate(row):
            c=t.cell(i,j);c.text=str(v);c.margin_left=Inches(.13);c.margin_right=Inches(.1);c.margin_top=Inches(.1);c.margin_bottom=Inches(.08)
            c.fill.solid();c.fill.fore_color.rgb=PRGB.from_string(NAVY if i==0 else ('F1F3F1' if i%2 else 'FFFFFF'))
            for p in c.text_frame.paragraphs:
                for r in p.runs:r.font.name=CN;r.font.size=PPt(size);r.font.bold=i==0;r.font.color.rgb=PRGB.from_string('FFFFFF' if i==0 else NAVY)
    return shape

def make_pptx():
    prs=Presentation();prs.slide_width=Inches(13.333333);prs.slide_height=Inches(7.5)
    for i,s in enumerate(SLIDES):
        slide=prs.slides.add_slide(prs.slide_layouts[6]);slide.background.fill.solid();slide.background.fill.fore_color.rgb=PRGB.from_string('FFFFFF')
        text(slide,s['title'],.65,.45,12.0,1.0,31 if i else 37,True)
        if i==0:
            text(slide,s['subtitle'],.7,2.02,11.9,.7,24,color=GREEN)
            text(slide,'\n'.join(s['lines']),.7,3.3,11.9,2,27)
        elif s.get('diagram'):
            nodes=[(.7,2.35,2.4,1.25,'设备规则\nPC模拟状态'),(4.15,2.1,4.0,1.8,'统一网关与C策略\n观测  调用  校验  事件'),(9.2,1.7,3.2,1.4,'远程模型候选\nHTTP模型适配'),(9.2,4.1,3.2,1.4,'网关模型候选\nHTTP模型适配')]
            for x,y,w,h,label in nodes:
                sh=slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,Inches(x),Inches(y),Inches(w),Inches(h));sh.fill.solid();sh.fill.fore_color.rgb=PRGB.from_string('F1F3F1');sh.line.color.rgb=PRGB.from_string('A8B6B2')
                text(slide,label,x+.16,y+.2,w-.3,h-.2,20,True)
            for x1,y1,x2,y2 in [(3.1,2.97,4.1,2.97),(8.15,2.6,9.15,2.4),(8.15,3.4,9.15,4.7)]:
                sh=slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT,Inches(x1),Inches(y1),Inches(x2),Inches(y2));sh.line.color.rgb=PRGB.from_string(GREEN);sh.line.width=PPt(2)
            text(slide,'WAN与LAN分别观测，模型网关与设备MCU分别核算',.7,6.03,12,.6,20,color=GREEN)
        elif s.get('chart'):
            data=CategoryChartData();data.categories=['联合','仅网络','仅算力','无迟滞'];data.add_series('时限内合同完成率',[84.40,62.98,61.90,84.52])
            ch=slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED,Inches(.9),Inches(1.65),Inches(11.7),Inches(3.9),data).chart
            ch.has_legend=False;ch.value_axis.minimum_scale=0;ch.value_axis.maximum_scale=100;ch.value_axis.major_unit=20
            for ax in [ch.category_axis,ch.value_axis]:ax.tick_labels.font.name=CN;ax.tick_labels.font.size=PPt(18)
            ch.plots[0].has_data_labels=True;ch.plots[0].data_labels.position=XL_LABEL_POSITION.OUTSIDE_END;ch.plots[0].data_labels.number_format='0.00"%"';ch.plots[0].data_labels.font.size=PPt(20)
            for point,col in zip(ch.series[0].points,[GREEN,'9BAAA8','B4BFBD','687F7B']):point.format.fill.solid();point.format.fill.fore_color.rgb=PRGB.from_string(col)
            text(slide,'迟滞没有提高完成率，联合仍有786条未解析',.9,5.85,11.6,.65,22,True)
        else:
            y=1.75
            if s.get('table'):
                rr=s['table'];ppt_table(slide,rr,y=y,size=18 if len(rr[0])==3 else 20);y+=.68*len(rr)+.35
            if s.get('lines'):
                for line in s['lines']:
                    text(slide,line,.8,y,11.9,.76,23 if not s.get('table') else 19);y+=.94 if not s.get('table') else .65
        text(slide,s.get('foot','面向实际需求的场景设计方案  2026年11月11日'),.7,6.98,11.8,.35,12,color=GRAY)
        text(slide,f'{i+1:02d}',12.25,6.98,.5,.35,12,color=GRAY)
        slide.notes_slide.notes_text_frame.text=s['notes']
    prs.core_properties.title=NAME;prs.core_properties.author='';prs.core_properties.last_modified_by=''
    p=OUT/'SenseHome_场景设计答辩_20261111.pptx';prs.save(p);return p

def evidence_pages():
    pages=[
    page('证据范围与阅读索引',para('SenseHome场景设计佐证材料  '+VERSION),para('本附件汇总可复核的软件、模型和数据证据。不同来源不混合统计；尚无专利、软著、客户反馈或家庭试点证明，不把模板或设计计划作为该类证明。'),table(['层级','证据','边界'],[['融合版r3','328个原始运行文件','合成轨迹与规划桩'],['独立真实模型','20条原始真实响应','模型规划，无物理执行'],['PC软件闭环','160请求及4步历史回放','模拟JSON设备状态'],['真实部署','尚未开展','真实设备、家庭和6G空口未验证']],[33,65,64]),para('材料匿名化：不附含参赛者所在单位、LOGO或指导教师信息的原证书。历史原型获奖不等同于本次融合功能已经获奖或落地。')),
    page('融合版r3构建与回归',*PLAN[5]['blocks'][:2],para('verification.json：passed=true；quick_mode=false；sources_unchanged_during_run=true。'),para('完成UTC：2026-11-10 16:53:16；换算为新加坡日期2026-11-11。工具为Zig 0.17.1 cc前端。'),para('归档根：'+FUSED),para('运行命令、编译器哈希、检查日志与各实验audit.json随同保存。328个文件逐路径、逐SHA256比对一致，新增README为归档索引，不计入328。')),
    page('合成回放结果',*PLAN[5]['blocks'][2:],para('合同完成并在配置时限内才计达标；合同未完成或超时均计失败。58,800是全部服务类型与实验单元合计，5,040是每个策略臂的interactive_inference子集，不能混用分母。')),
    page('HTTP网关独立墙钟',*PLAN[6]['blocks'][:3],para('客户端原始行与持久化事件一一核验，保留trace_id、输入摘要、路由和错误。2,940矩阵请求加6,144压力请求等于9,084。679个合同未完成包含383与296两类，不把外层HTTP 200写作业务100%成功。')),
    page('安全与本地模拟状态',*PLAN[6]['blocks'][3:],para('旧版Dashboard本地路径：四档每类20次，两类合计160次。支持的开关灯路径p95最大4.434 ms，只读安防状态查询1.711 ms；模拟状态与查询结果均确认。来源：SenseHome-6G/evidence/current/dashboard/dashboard_results.json。'),para('这些数字既不是MCU时延，也不是物理执行器确认。QEMU与Dashboard白名单实现分开，禁止跨通道推导统一安全认证。')),
    page('真实模型与标签来源',*PLAN[7]['blocks'][:4],para('原始响应留在eval.draft.json，正式标签核对保存在eval.formal.json。动作归一化字段与raw_model_actions分开保存，便于区分模型计划与规则修正。'),para('来源：'+MODEL)),
    page('真实模型逐条核对摘要',para('下表直接从eval.formal.json读取。动作一致以冻结参考动作核对；HTTP成功与动作一致分别统计。'),table(['ID','类别','HTTP','动作一致'],[[r['id'],r['category'],str(r.get('http_status','')), '是' if r.get('action_correct') else '否'] for r in MODEL_DATA['rows']],[19,85,22,36]),para('A01/A02为未提及房间动作，A04为白名单外阳台动作；保护层422拒绝，不计成功。')),
    page('软件状态历史回放',para('演示视频使用docs/video/v2/software_replay.json中的4步实际软件记录，采集于2026年11月3日，WAN=false/LAN=false为注入状态。它展示当时Dashboard、C策略和隔离PC状态通道，不宣称是r3现场录屏。'),table(['步骤','实际命令','结果边界'],[[str(r['step']),r['command'],r['result']['backend']+' / '+str(r['result']['semantic_complete'])] for r in json.loads((ROOT/'docs/video/v2/software_replay.json').read_text(encoding='utf-8'))['rows']],[16,91,55]),para('视频播放节奏由编辑安排，不能从镜头停留时间推导执行耗时。原JSON保留state_before/state_after、event_id、墙钟和结果；原状态未被改动。')),
    page('原始文件与SHA256索引',para('完整哈希保存在同版source_manifest.json。下表列出核验入口与前16位摘要，完整摘要以JSON为准。'),table(['相对路径','SHA256前16位'],[[p,sha(ROOT/p)[:16]] for p in SOURCE_PATHS],[115,47]),para('文件哈希用于标识材料，不自动证明内容正确。运行期源码、实验配置、原始行和报告同时保留，复算范围以各audit结果为准。')),
    page('资源 原创性与未完成项',*TEAM_PAGE['blocks'][2:],para('尚需真实外部输入：报名团队名称和编号、需求访谈或试用回答、目标设备与真实网络条件、采购或电价等实际成本信息。没有这些输入不伪造完成证明，也不代替参赛者上传赛事系统。'),para('补充开发回归与注入观测实验索引：docs/experiments/20261111_fused_real/sealed_results.md、sealed_evidence_index.json。开发回归使用程序派生命令且已用于修复，不称未见泛化测试；故障条件为显式注入观测，不是真实网络或设备故障。三套矩阵分别统计。'))
    ]
    # Keep supplementary source URLs visible in the combined evidence PDF.
    public=json.loads((ROOT/'docs/value/public_sources.json').read_text(encoding='utf-8'))['records']
    pages.insert(-1,page('公开技术来源',para('以下官方来源于2026-11-11读取，快照、摘录、哈希和允许支持的内容保存在docs/value/public_sources.json。公开报价仅用于可替换参数，不是本项目账单。'),table(['编号与来源','官方URL'],[[r['id']+' '+r['publisher']+' '+r['title'],r['url']] for r in public],[63,99]),para('本项目需求尚无访谈或用户确认。竞品文档用于准确说明已有能力，未开展同硬件、同任务的竞品性能对照。')))
    pages[-1]['blocks'][-1] = para('四套实验的原始记录与哈希见docs/experiments/20261111_fused_real/sealed_evidence_index.json。开发回归已用于修复，留出检查是预注册程序标签组合，均非真实用户测试；受控故障仅注入策略观测，不能视为真实网络故障。')
    pages.insert(6,LOCAL_PAGE)
    pages.insert(-1,HOLDOUT_PAGE)
    return pages

SOURCE_PATHS=[FUSED+'README.md',FUSED+'validation/verification.json',FUSED+'replay/summary.json',FUSED+'http_gateway/summary.json',FUSED+'safety/summary.json',MODEL+'README.md',MODEL+'eval.formal.json',MODEL+'eval.draft.json','docs/deployment/20条动作标签冻结记录_20261103.json','SenseHome-6G/evidence/current/dashboard/dashboard_results.json','docs/video/v2/software_replay.json','docs/value/public_sources.json','docs/value/host_resources_summary.json','docs/value/cost_results/current_unknown.json']
SOURCE_PATHS.extend([LOCAL+'summary.json',LOCAL+'report.json'])
SOURCE_PATHS.extend(['docs/experiments/20261111_fused_real/sealed_results.md','docs/experiments/20261111_fused_real/sealed_evidence_index.json'])

def make_pdf(pages,path):
    pdfmetrics.registerFont(TTFont('SubmissionCN','C:/Windows/Fonts/simhei.ttf'))
    st={k:ParagraphStyle(k,fontName='SubmissionCN',fontSize=sz,leading=le,textColor=colors.HexColor('#'+NAVY),spaceAfter=sp,wordWrap='CJK') for k,sz,le,sp in [('h1',21,29,18),('h2',13,20,10),('p',10.5,17,10),('cell',9,14,0),('head',9,14,0)]}
    story=[]
    def P(s,k='p'):return Paragraph(escape(str(s)).replace('\n','<br/>'),st[k])
    for i,p in enumerate(pages):
        if i:story.append(PageBreak())
        story.append(P(p['title'],'h1'))
        for b in p['blocks']:
            if b['kind'] in ['p','h2']:story.append(P(b['text'],b['kind']))
            else:
                rows=[b['headers']]+b['rows'];cw=b.get('widths') or [162/len(rows[0])]*len(rows[0]);cw=[x*162/sum(cw)*mm for x in cw]
                t=Table([[P(x,'head' if n==0 else 'cell') for x in r] for n,r in enumerate(rows)],colWidths=cw,repeatRows=1)
                pad=3 if len(rows)>15 else 6
                t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#E7ECE9')),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#F7F8F6')]),('VALIGN',(0,0),(-1,-1),'TOP'),('LINEBELOW',(0,0),(-1,-1),.35,colors.HexColor('#CDD6D0')),('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),('TOPPADDING',(0,0),(-1,-1),pad),('BOTTOMPADDING',(0,0),(-1,-1),pad)]));story.extend([t,Spacer(1,10)])
    def foot(c,d):
        c.setFont('SubmissionCN',8);c.setFillColor(colors.HexColor('#55636A'));c.drawString(24*mm,15*mm,'SenseHome  佐证材料  2026-11-11');c.drawRightString(186*mm,15*mm,str(d.page))
    SimpleDocTemplate(str(path),pagesize=(210*mm,297*mm),rightMargin=24*mm,leftMargin=24*mm,topMargin=22*mm,bottomMargin=24*mm,title=NAME+'佐证材料',author='').build(story,onFirstPage=foot,onLaterPages=foot)

def convert(path):
    profile=WORK/('lo_'+path.suffix[1:]);profile.mkdir(exist_ok=True)
    run([SOFFICE,'-env:UserInstallation='+profile.as_uri(),'--headless','--convert-to','pdf','--outdir',OUT,path])
    pp=path.with_suffix('.pdf');assert pp.is_file();return pp

def render(path,label):
    dest=WORK/label;dest.mkdir(exist_ok=True)
    for old in dest.glob('page-*.png'):old.unlink()
    doc=fitz.open(path)
    for i,page in enumerate(doc):page.get_pixmap(matrix=fitz.Matrix(1.35,1.35),alpha=False).save(dest/f'page-{i+1:02d}.png')
    return {'pages':len(doc),'size_bytes':path.stat().st_size,'sha256':sha(path),'png_dir':str(dest.relative_to(ROOT)),'text_chars':[len(p.get_text()) for p in doc]}

def make_diagrams(deck_pdf):
    doc=fitz.open(deck_pdf);p=doc[2];clip=fitz.Rect(28,105,p.rect.width-25,p.rect.height-58)
    p.get_pixmap(matrix=fitz.Matrix(2.2,2.2),clip=clip,alpha=False).save(WORK/'architecture.png')
    pdfmetrics.registerFont(TTFont('FlowCN','C:/Windows/Fonts/simhei.ttf'))
    pdf=WORK/'workflow_diagram.pdf';c=canvas.Canvas(str(pdf),pagesize=(900,410));c.setFont('FlowCN',18)
    def box(x,y,w,h,label):
        c.setStrokeColor(colors.HexColor('#82958D'));c.setFillColor(colors.HexColor('#F1F4F1'));c.rect(x,y,w,h,fill=1,stroke=1);c.setFillColor(colors.HexColor('#172D3A'));ls=label.split('\n')
        for i,l in enumerate(ls):c.drawCentredString(x+w/2,y+h/2+(len(ls)-1)*11-i*24-6,l)
    def arrow(x1,y1,x2,y2):
        c.setStrokeColor(colors.HexColor('#287366'));c.setLineWidth(2);c.line(x1,y1,x2,y2)
        if x1==x2:c.line(x2,y2,x2-5,y2+8);c.line(x2,y2,x2+5,y2+8)
        else:
            sign=1 if x2>x1 else -1
            c.line(x2,y2,x2-sign*8,y2+5);c.line(x2,y2,x2-sign*8,y2-5)
    box(10,285,150,70,'输入命令\n任务分类');box(215,315,210,70,'简单与只读查询\n本地规则');box(215,205,210,70,'复杂指令\n两段链路 两端观测');box(480,205,180,70,'唯一C策略\n选择模型候选');box(710,205,180,70,'模型输出\nschema和白名单');box(480,65,180,70,'允许动作\nPC状态确认');box(710,65,180,70,'异常或拒绝\n边界内回退');box(215,65,210,70,'持久化事件\n原因 耗时 能力 状态')
    arrow(160,320,215,350);arrow(160,305,215,240);arrow(425,240,480,240);arrow(660,240,710,240);arrow(800,205,800,135);arrow(710,100,660,100);arrow(480,100,425,100)
    c.line(425,350,690,350);c.line(690,350,690,100);arrow(690,100,660,100)
    c.line(800,175,570,175);arrow(570,175,570,135)
    c.setFont('FlowCN',13);c.drawString(625,180,'校验通过');c.drawString(810,165,'异常或拒绝')
    c.setFillColor(colors.HexColor('#55636A'));c.setFont('FlowCN',14);c.drawString(15,20,'独立记录接口响应 规划完成 动作结构与模拟执行；真实物理回执尚未接入')
    c.save();dd=fitz.open(pdf);dd[0].get_pixmap(matrix=fitz.Matrix(1.6,1.6),alpha=False).save(WORK/'workflow.png')
    for name in ['architecture','workflow']:
        (ROOT/f'SenseHome-6G/figures/submission_20261111_{name}.png').write_bytes((WORK/f'{name}.png').read_bytes())

def main():
    (ROOT/'SenseHome-6G/作品方案_场景设计_20261111.md').write_text(render_markdown(PLAN),encoding='utf-8')
    old=ROOT/'SenseHome-6G/作品方案_场景设计_v1.md';archive=ROOT/'docs/submission/20261111/作品方案_v1_刷新前.md'
    if not archive.exists():archive.write_bytes(old.read_bytes())
    old.write_text(render_markdown(PLAN),encoding='utf-8')
    outline='\n\n'.join(f"## {i+1} {s['title']}\n\n"+'\n'.join('- '+x for x in s.get('lines',[]))+'\n\n'+s['notes'] for i,s in enumerate(SLIDES))
    (ROOT/'答辩PPT_页面大纲.md').write_text('# 答辩PPT页面大纲 20261111\n\n'+outline,encoding='utf-8')
    ppt=make_pptx();deck=convert(ppt);make_diagrams(deck);doc=make_docx();pdf=convert(doc)
    ep=OUT/'SenseHome_佐证材料_20261111.pdf';make_pdf(evidence_pages(),ep)
    qa={p.name:render(p,label) for p,label in [(pdf,'plan'),(deck,'slides'),(ep,'evidence')]}
    manifest={'version':'20261111','sources':[{'path':p,'sha256':sha(ROOT/p)} for p in SOURCE_PATHS],'authoring':'existing system python-docx/python-pptx/reportlab; LibreOffice native rendering; permitted fallback because no configured Codex runtime/artifact-tool','disclosures':['one student','no household pilot','no physical actuator','no 6G air interface','historical AutoDL model independent from r3'],'qa':qa}
    (OUT/'source_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    (WORK/'qa.json').write_text(json.dumps(qa,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(qa,ensure_ascii=True,indent=2))
if __name__=='__main__':main()
