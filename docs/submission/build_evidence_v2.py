"""Author v2 materials in a new directory; never execute the v1 main()."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / 'docs/submission/20261111'
sys.path.insert(0, str(BASE))
import build_submission as b

OUT = ROOT / 'dist/final/20261111_v2'
WORK = ROOT / '_work/submission_20261111_v2'
FAULT = 'docs/experiments/20261111_real_fault_v2/'
VALUE = 'docs/value_v2/'
OLD_SHA = '90fb5b1d162400109526ad860f476b44282d0210597915ffb9a5fcc9ba98bd27'


def read(path):
    return json.loads((ROOT / path).read_text(encoding='utf-8'))


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def compact_docx_appendix(path):
    """Keep each planned appendix page intact at readable A4 sizes."""
    from docx import Document
    from docx.shared import Pt
    from docx.text.paragraph import Paragraph
    from docx.table import Table
    doc = Document(path)
    active = False
    for element in doc.element.body:
        if element.tag.endswith('}p'):
            paragraph = Paragraph(element, doc)
            if paragraph.text.startswith('补证1 '):
                active = True
            if not active:
                continue
            fmt = paragraph.paragraph_format
            fmt.line_spacing = 1.15
            fmt.space_before = Pt(0)
            fmt.space_after = Pt(7)
            for run in paragraph.runs:
                b.doc_font(run, size=16 if paragraph.text.startswith('补证') else 10.5,
                           bold=paragraph.text.startswith('补证') or paragraph.style.name.startswith('Heading'))
        elif active and element.tag.endswith('}tbl'):
            table = Table(element, doc)
            for i, row in enumerate(table.rows):
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        fmt = paragraph.paragraph_format
                        fmt.line_spacing = 1.1
                        fmt.space_after = Pt(3)
                        fmt.space_before = Pt(2)
                        for run in paragraph.runs:
                            b.doc_font(run, size=9.5, bold=i == 0)
    doc.save(path)


def supplement():
    page, p, sub, table = b.page, b.para, b.sub, b.table
    summary = read(FAULT + 'summary.json')
    assert summary['n_rows'] == 120
    results = [line.split('|')[1:-1] for line in (ROOT / FAULT / 'summary.md').read_text(encoding='utf-8').splitlines()
               if line.startswith('|') and any(line.startswith('|' + x + '|') for x in
                   ['healthy', 'cloud_http503', 'local_http503', 'cloud_admission700', 'local_admission700'])]
    assert len(results) == 20
    labels = {'healthy': '健康', 'cloud_http503': '第二端503', 'local_http503': '本地端503',
              'cloud_admission700': '第二端等待700ms', 'local_admission700': '本地端等待700ms'}
    arms = {'fixed-local': '固定本地', 'fixed-cloud': '固定第二端', 'network-only': '仅看网络', 'joint': '联合'}
    compact = [[labels[r[0]], arms[r[1]], r[2], r[4], r[5]] for r in results]
    return [
        page('补证1 故障条件下的模型路由',
             p('SenseHome 第二版补证报告 / 2026年11月11日'),
             p('本轮围绕“候选模型可用性变化时，家庭控制入口能否完成任务”补证。在同机两个真实模型前增加受控HTTP代理，保留原C策略、网关、动作合同与冻结标签，完成120条预注册配对请求。'),
             table(['观察', '结果', '可支持的主张'], [
                 ['本地端点真实503', '联合6/6；固定本地0/6；仅看网络0/6', 'HTTP可达不代表模型可用，联合策略能避开该故障候选'],
                 ['本地准入等待700ms', '联合1042.006ms；仅看网络1431.163ms', '该固定等待条件下，联合路由选另一候选并减少请求中位时间'],
                 ['健康条件', '四臂均完成6/6', '本轮不支持正常环境下普遍提高任务质量'],
                 ['可用替代端点', '第二端503时，联合与仅看网络均6/6', '单类故障结果不能泛化为所有条件的可靠性优势']
             ], [32, 56, 74]),
             p('以上为同机共享GPU、隔离JSON设备的受控回归。每臂每条件只有两条已有人工命令的三次重复；相互相关，仅描述观察，不作住户总体推断。'),
             sub('与6G场景设计的关系'),
             p('实现位置是应用侧的网络与算力协同接口：任务分类后，以链路、模型健康和准入等待选择候选，动作合同统一校验执行。现阶段验证软件机制；未测6G空口、真实广域网、独立端云或物理执行器。')),
        page('补证2 预注册与计量合同',
             table(['项目', '固定设计'], [
                 ['输入', 'C01“客厅有点暗”；C04“关闭书房的灯并打开书房窗帘”。复用2026-11-03人工冻结标签'],
                 ['矩阵', '2命令 × 4策略 × 3重复 × 5条件 = 120行，30个四臂配对块'],
                 ['条件', '健康、本地503、第二端503、本地固定700ms准入等待、第二端固定700ms准入等待'],
                 ['公平性', '配对块共用输入、初始状态和观测；每次重置设备，臂顺序轮换。每条件新建4个C进程，条件内连续重复'],
                 ['策略参数', '沿用1116/1361ms校准及原迟滞/恢复阈值；注册后单次运行，未见结果后放大延迟或修改提示词'],
                 ['成功合同', '预期非空动作完整匹配、模拟状态确认，且无错误/规则回退。503、422、空结构与快速失败分别记录']
             ], [27, 135]),
             table(['主矩阵/辅助调用', '数量', '来源'], [
                 ['客户端到代理POST', '120', '102转发到模型并收到200；18代理返回503，未调模型'],
                 ['预热POST', '10，另计', '8实际模型POST；2代理503'],
                 ['模型清单GET / 代理遥测GET', '60 / 60', '与推理POST分开，不作为业务完成'],
                 ['运行器异常 / 跳过', '0 / 0', '所有120行保留，初探2次GET单独存档']
             ], [73, 25, 64]),
             p('700ms是代理通过实际等待实施的固定准入延迟，非GPU队列或网络拥塞测量。503由自建代理实际HTTP返回，上游模型进程保持健康，非服务崩溃。大模型本轮启动时关闭推理预算，与旧环境分开统计。'),
             p('来源：'+FAULT+'registration.json；各conditions目录的report.gateway_raw.json、rows.jsonl、blocks.jsonl及proxy_records.json。')),
        page('补证3 五个条件分别报告',
             p('每格完成率分母为6；时间为所有请求的网关墙钟中位数，单位ms。固定本地503的约5ms是快速失败，不是响应收益。'),
             table(['条件', '策略', '完成', '实际模型POST', '中位ms'], compact, [38, 30, 17, 36, 29]),
             p('联合在本地503条件调用可用的第二端，在本地700ms等待条件选第二端。本地仍正常时，优先本地的对照也能完成。不要跨条件汇总“真实世界可靠性”，或把此矩阵与旧600条混算质量指标。'),
             p('独立核验：逐行重评分、配对输入、trace、POST计数与注册先于推理均通过。来源：summary.json、verify_run.py；docs/reviews/2026-11-11-v2-integration-review.md。')),
        page('补证4 需求来源与可执行验收',
             p('2份公开报告提供3项技术观察；P2/P3来自同一帖子。本项目实际外部使用者反馈仍为0。公开资料支持问题来源，不代表该发帖者认可或采用本项目。'),
             table(['公开观察', '对应系统要求', '当前验证'], [
                 ['P1：2024年历史HA问题报告实体幻觉、控制失败', '模型计划进入执行器前校验实体、动作与参数；拒绝不能部分执行', '原动作保护测试与422原始行；不宣称当前竞品仍有同样缺陷'],
                 ['P2：同一窗帘操作在LLM与local-first路径表现不同', '明确且已支持的命令保留规则路径；复杂命令报告所用模型与实际能力', '隔离生产规则、模型合同与模拟状态分开验收'],
                 ['P3：同一帖子自述LLM响应随任务变化', '分解端到端、上游模型和代理等待；记录所有失败', '受控120条原始墙钟与转发记录；他人的1至11秒不作本项目基线']
             ], [48, 57, 57]),
             p('P1：Home Assistant core issue #123316，2024.8历史版本。https://github.com/home-assistant/core/issues/123316'),
             p('P2/P3：Home Assistant社区，帖子944860，第35楼，页面显示Jan 8但未显示年份。https://community.home-assistant.io/t/my-journey-to-a-reliable-and-enjoyable-locally-hosted-voice-assistant/944860/35'),
             p('2026-11-11读取。短引语、原文行定位和原始页面SHA256见docs/competition_v2/public_user_reports.json及public_excerpts/。交付仅含短摘录，避免整页头像和临时签名链接。'),
             sub('试点验收目标'),
             p('支持命令需取得动作和目标状态；不支持命令需无越权执行且说明原因。使用者需能复述完成/未完成内容。目标均为工程验收提案，尚未经客户确认。')),
        page('补证5 真实试用采集已经可运行',
             p('一条命令启动原生产网关与采集页：'),
             p('python docs/trial_v2/run_isolated_gateway.py --with-collector --record-kind participant'),
             p('本机页面 http://127.0.0.1:8877；隔离网关8878。Ctrl+C停止。状态和事件写入新_work/trial_v2/子目录，原设备状态文件不动。仅模拟电脑设备，不连接物理家居。'),
             table(['环节', '实际行为'], [
                 ['来源与同意', 'participant、developer_self_test、automation_test独立标识；未勾选同意不创建会话；匿名原话公开引用默认关闭'],
                 ['任务', '区分使用者自拟与预设；自拟不自动断言正确。预设核对完整动作、有效前后状态和执行证据'],
                 ['保存与撤回', '执行过程暂存内存，只有明确保存才落盘；取消丢弃；保存后可撤回。无姓名、住址、联系方式与IP字段'],
                 ['默认无模型', '不继承旧模型端点。T1开灯由生产规则完成；T2多房间调光可失败。没有针对预设任务的成功特判'],
                 ['反馈边界', '自测/自动化不计外部用户；愿意再试不解释成付费。采集记录本身也不能证明操作者身份']
             ], [30, 132]),
             p('本轮浏览检查采用automation_test及开发者角色：未同意时拒绝开始；T1完成并确认状态；T2未获完整确认；取消后页面显示未保留，外部反馈仍0。没有把自动操作写作人类同意或真实访谈。'),
             p('状态读取失败时，不能推定“状态未变”。修后须前后GET均200、无错误且状态schema有效，才计算空目标保持。HTTP503夹具回归通过。'),
             p('操作、导出与撤回说明：docs/trial_v2/README.md；验证：verification/；独立复核见本轮集成审查。试用数据和导出默认不进入Git或审阅包。')),
        page('补证6 实际用量与成本缺项',
             p('对旧四矩阵逐条读取实际上游HTTP响应usage，只算模型POST一次；原始源SHA与调用索引逐条保存，不重新推理，不用估算token替代服务端计数。'),
             table(['矩阵', '模型POST', '输入token', '输出token'], [
                 ['20条人工冻结回归', '84', '49,572', '4,860'],
                 ['12条开发回归', '144', '85,644', '10,086'],
                 ['注入观测实验', '55', '32,386', '3,483'],
                 ['12条程序留出', '132', '78,900', '10,848'],
                 ['执行量合计', '415', '246,502', '29,277']
             ], [64, 26, 36, 36]),
             p('415次响应均有合法usage，响应ID互异。本表仅合计执行量，四组效果不能合并；不包含本轮120条、10次预热或历史校准。token包含服务端记录的提示处理，不能直接当计费发票或能耗。'),
             table(['成本分项', '当前依据', '结论'], [
                 ['直接模型API收费', '该415次全部loopback本机服务，无外部API账单', '该范围直接分项0；执行日志推导，非供应商发票'],
                 ['硬件折旧、电费、维护、网络', '尚无采购凭据、整机电量积分、电价和实际工时', '未知；actual_total与scenario_total均null'],
                 ['对外报价与节费', '仅有可替换假设和公开参考', '不能发布实测总成本或节费比例']
             ], [38, 77, 47]),
             p('部署检查只读访问字面量回环IP，直接HTTP连接，不读取代理环境且拒绝重定向；真实HTTP夹具验证目标与代理收包。两台主机配置仅模板，非已部署。已退出旧端口的探测失败原样保留。'),
             p('来源：docs/value_v2/results/usage_calls.jsonl、usage_summary.json、ledger_current_result.json。账本区分实际、报价、假设；任一必要实际分项缺失，总额保持未知。')),
        page('补证7 当前验收状态与复核入口',
             table(['维度', '本轮已取得', '仍需取得'], [
                 ['技术落地', '真实模型+代理503/等待、原C策略、状态合同、120条配对原始记录', '独立主机、真实WAN变化、物理回执与持续负载'],
                 ['业务需求', '2份公开使用报告、可执行匿名试用工具', '本项目自愿参与者操作与原话、目标客户KPI确认'],
                 ['量化价值', '实际415次usage，条件内失败与时间对照', '整机成本凭据、安装维护工时、客户收益'],
                 ['可复核性', '旧ZIP和600条封存未变；新实验预注册、源码/原始/计数独立复核', '异机构复现或独立设备验证'],
                 ['材料交付', '第二版整合方案、答辩、佐证和本补充报告', '报名团队编号等真实信息、参赛者最终审阅与正式提交']
             ], [28, 75, 59]),
             p('下一次实施按验收而非材料篇幅推进：先由自愿使用者操作并记录原话；再以授权设备回执核对执行；条件具备后固定输入和阈值，在独立主机重复端云对照，保留所有失败；最后补齐真实成本账本。没有现场条件时保持待验收，不由模拟值填补。'),
             sub('复核命令与边界'),
             p('python docs/experiments/20261111_real_fault_v2/verify_run.py 为离线复算入口，不需要模型。新工具测试与HTTP夹具运行见各README。受控模型实验已按注册运行一次，不为改善结果重跑。'),
             p('独立审查：docs/reviews/2026-11-11-v2-integration-review.md。采集器状态读取与部署探测两项实质问题已修复并补回归。'),
             p('旧审阅包保留：SHA256 '+OLD_SHA+'。新source_manifest.json列出补证引用文件完整哈希；新ZIP旁车逐文件校验。'),
             p('运行时局限：未能保存本轮模型启动时的完整命令快照；结束后已无活进程可核对。事后权重/运行时文件哈希另记为post_run_runtime_inventory.json，不能追称为启动快照。奖项由赛事评审决定，本报告不把完成工程检查等同于保证获奖。'))
    ]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    original = ROOT / 'dist/final/20261111/SenseHome_本地审阅包_20261111.zip'
    assert digest(original) == OLD_SHA
    b.OUT, b.WORK = OUT, WORK
    original_plan = copy.deepcopy(b.PLAN)
    original_slides = copy.deepcopy(b.SLIDES)
    original_evidence = copy.deepcopy(b.evidence_pages())
    pages = supplement()
    b.PLAN = original_plan + copy.deepcopy(pages)
    for pp in b.PLAN[:16]:
        for block in pp['blocks']:
            if block['kind'] == 'table':
                for row in block['rows']:
                    if row[0] == 'dist/final/20261111/':
                        row[1] = '上一版封存方案、答辩、佐证与历史演示视频'
                        block['rows'].append(['dist/final/20261111_v2/', '本版方案、答辩、佐证、补证与来源清单'])
                        break
    for pp in original_evidence:
        for block in pp['blocks']:
            if 'text' in block:
                block['text'] = block['text'].replace(
                    '受控故障仅注入策略观测，不能视为真实网络故障。',
                    '旧四矩阵中的受控观测实验仅注入策略观测，不能视为真实网络故障；本轮真实HTTP代理故障实验见补证1至3。')
    b.PLAN[0]['blocks'].append(b.para('第二版增加120条受控HTTP故障联测、公开需求来源、试用采集与实际用量。完整新增结果见补证1至7；原封存数据和失败保留。'))
    b.PLAN[0]['blocks'][3] = b.para('版本 2026-11-11 v2 本机受控验证')
    b.SLIDES = original_slides
    b.SLIDES[8]['foot'] = b.SLIDES[8]['foot'].replace('失败0', '运行器失败0')
    b.SLIDES[-1]['lines'] = ['新增120条受控HTTP故障与准入等待验证', '本地503：联合6/6，仅看网络0/6', '试用工具已可运行，外部真实反馈仍为0', '待取得：独立网络、物理回执与实际成本']
    b.SLIDES[-1]['notes'] = '本轮同机两个真实模型，代理注入，隔离PC设备。每条件两命令三重复，非人群泛化。正常条件无普遍优势；不保证奖项。'
    b.SLIDES.insert(-1, {
        'title': '本地模型503时，联合仍完成6/6',
        'table': [['策略', '健康', '本地503', '第二端503'], ['固定本地', '6/6', '0/6', '6/6'],
                  ['固定第二端', '6/6', '6/6', '0/6'], ['仅看网络', '6/6', '0/6', '6/6'], ['联合', '6/6', '6/6', '6/6']],
        'lines': ['120行中102次真实模型推理，18次代理503未调模型'],
        'foot': '同机共享GPU，代理受控503，PC模拟设备；每臂每条件6条重复任务',
        'notes': (ROOT / FAULT / 'summary.md').read_text(encoding='utf-8')})
    b.SLIDES.insert(-1, {
        'title': '本地准入等待下，联合选择另一端点',
        'table': [['700ms等待条件', '固定本地', '仅看网络', '联合'],
                  ['本地端等待', '1420.246', '1431.163', '1042.006'],
                  ['第二端等待', '714.990', '719.141', '712.163']],
        'lines': ['表为所有请求中位ms，各臂均完成6/6', '实际代理等待，尚未测GPU排队或WAN拥塞'],
        'foot': '阈值与1116/1361ms先前校准固定；注册后只运行一次',
        'notes': '不要把差值写成实网或6G收益。固定第二端两条件中位1046.806/1790.389ms。重复相关，无显著性推断。'})
    b.SLIDES.insert(-1, {
        'title': '可试用、可核账，外部验证仍待完成',
        'table': [['交付', '已经取得', '待取得事实'],
                  ['需求与试用', '2公开来源、匿名采集页', '本项目外部反馈0'],
                  ['用量与成本', '415次真实响应usage', '总成本未知'],
                  ['部署与执行', '同机模型、模拟状态', '独立主机与物理回执']],
        'lines': ['246,502输入token，29,277输出token，仅计旧四矩阵'],
        'foot': '采集器区分自测/自动化/参与者；未知账本分项不填零',
        'notes': '公开报告为二手需求。participant需真实人自愿操作，AI自测不计。直接API分项0不等于总成本0。'})
    doc = b.make_docx()
    compact_docx_appendix(doc)
    ppt = b.make_pptx()
    for path in (doc, ppt):
        path.replace(path.with_name(path.stem + '_v2' + path.suffix))
    doc = OUT / 'SenseHome_场景设计方案_20261111_v2.docx'
    ppt = OUT / 'SenseHome_场景设计答辩_20261111_v2.pptx'
    plan_pdf = b.convert(doc)
    slides_pdf = b.convert(ppt)
    report = OUT / 'SenseHome_第二版补证报告_20261111.pdf'
    evidence = OUT / 'SenseHome_佐证材料_20261111_v2.pdf'
    b.make_pdf(pages, report)
    b.make_pdf(original_evidence + copy.deepcopy(pages), evidence)
    (OUT / '作品方案_20261111_v2.md').write_text(b.render_markdown(b.PLAN), encoding='utf-8')
    qa = {path.name: b.render(path, label) for path, label in
          [(plan_pdf, 'plan'), (slides_pdf, 'slides'), (report, 'supplement'), (evidence, 'evidence')]}
    sources = list(b.SOURCE_PATHS) + [FAULT + name for name in
              ['registration.json', 'summary.json', 'audit.json', 'post_run_runtime_inventory.json']]
    sources += ['docs/competition_v2/public_user_reports.json', 'docs/trial_v2/verification/browser_acceptance.json',
                'docs/value_v2/results/usage_summary.json', 'docs/value_v2/results/ledger_current_result.json',
                'docs/reviews/2026-11-11-v2-integration-review.md']
    manifest = {'version': '20261111_v2', 'created_at_utc': datetime.now(timezone.utc).isoformat(),
                'sources': [{'path': p, 'sha256': digest(ROOT / p)} for p in sources], 'qa': qa,
                'old_zip_sha256_unchanged': digest(original),
                'authoring': 'existing python-docx/python-pptx/reportlab and LibreOffice; no available bundled runtime',
                'limits': ['same computer', 'software proxy conditions', 'PC device simulation',
                           'zero external project feedback', 'unknown actual total cost', 'missing startup argv snapshot']}
    (OUT / 'source_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(qa, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
