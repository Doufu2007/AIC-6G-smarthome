#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the audit appendix from local evidence; never synthesize measurements."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "dist" / "final" / "SenseHome_佐证材料_20261103.pdf"
QA = ROOT / "_work" / "evidence_pdf_qa"
NET = ROOT / "SenseHome-6G" / "net_offload"
CURRENT = ROOT / "SenseHome-6G" / "evidence" / "current"

SOURCES = [
    ROOT / "_work" / "evidence_final" / "results.json",
    ROOT / "_work" / "evidence_final" / "results.corrected.json",
    NET / "results" / "results.json",
    CURRENT / "dashboard" / "dashboard_results.json",
    CURRENT / "dashboard" / "events.jsonl",
    CURRENT / "validation" / "verification.json",
    CURRENT / "validation" / "build_manifest.json",
    CURRENT / "validation" / "provenance.json",
    ROOT / "docs" / "reviews" / "2026-11-03-final-review.md",
    ROOT / "SenseHome-6G" / "compliance" / "CURRENT_DEPENDENCIES.md",
    ROOT / "SenseHome-6G" / "compliance" / "README.md",
    ROOT / "docs" / "real_model_attempt_20261103.md",
]

for path in SOURCES:
    if not path.is_file():
        raise FileNotFoundError(path)

data = json.loads((NET / "results" / "results.json").read_text(encoding="utf-8"))
dashboard = json.loads((CURRENT / "dashboard" / "dashboard_results.json").read_text(encoding="utf-8"))
provenance = json.loads((CURRENT / "validation" / "provenance.json").read_text(encoding="utf-8"))
verification = json.loads((CURRENT / "validation" / "verification.json").read_text(encoding="utf-8"))

assert sum(s["overall"]["n"] for s in data["summaries"]) == 480
assert sum(s["n"] for s in dashboard["summaries"]) == 160
assert all(x["exit_code"] == 0 for x in verification["checks"])

pdfmetrics.registerFont(TTFont("EvidenceCN", "C:/Windows/Fonts/simhei.ttf"))
FONT = "EvidenceCN"
INK = colors.HexColor("#172D3A")
MUTED = colors.HexColor("#52626A")
GREEN = colors.HexColor("#397D6E")
LINE = colors.HexColor("#CBD4D6")
BG = colors.HexColor("#F2F5F3")
styles = {
    "title": ParagraphStyle("EvidenceTitle", fontName=FONT, fontSize=26, leading=34, textColor=INK, spaceAfter=16, wordWrap="CJK"),
    "h1": ParagraphStyle("EvidenceH1", fontName=FONT, fontSize=19, leading=26, textColor=INK, spaceAfter=14, wordWrap="CJK"),
    "h2": ParagraphStyle("EvidenceH2", fontName=FONT, fontSize=12.5, leading=19, textColor=GREEN, spaceBefore=14, spaceAfter=7, wordWrap="CJK"),
    "body": ParagraphStyle("EvidenceBody", fontName=FONT, fontSize=10.5, leading=17, textColor=INK, spaceAfter=9, wordWrap="CJK"),
    "small": ParagraphStyle("EvidenceSmall", fontName=FONT, fontSize=8.4, leading=13, textColor=MUTED, spaceAfter=6, wordWrap="CJK"),
    "cell": ParagraphStyle("EvidenceCell", fontName=FONT, fontSize=8.8, leading=13, textColor=INK, wordWrap="CJK"),
    "head": ParagraphStyle("EvidenceHead", fontName=FONT, fontSize=8.8, leading=13, textColor=colors.white, wordWrap="CJK"),
    "mono": ParagraphStyle("EvidenceMono", fontName="Courier", fontSize=7.4, leading=11, textColor=MUTED, wordWrap="CJK", spaceAfter=7),
}

story = []
def p(text, style="body"):
    return Paragraph(escape(str(text)).replace("\n", "<br/>"), styles[style])

def add(text, style="body"):
    story.append(p(text, style))

def title(n, text):
    if story:
        story.append(PageBreak())
    add(f"{n:02d}  {text}", "h1")

def table(rows, widths, font=8.8):
    cells = [[p(x, "head" if i == 0 else "cell") for x in row] for i, row in enumerate(rows)]
    t = Table(cells, colWidths=[v * mm for v in widths], repeatRows=1, hAlign="LEFT")
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
        ("LINEBELOW", (0, 1), (-1, -1), 0.35, LINE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, BG]),
    ]))
    story.append(t)
    story.append(Spacer(1, 9))

def ref(text):
    add("核验入口：" + text, "small")

def fmt(x):
    return f"{float(x):g}"

add("SenseHome", "title")
add("AI + 6G 场景设计\n本地软件与证据佐证材料", "h1")
add("版本：2026-11-03  /  本地审阅附件", "h2")
add("本附件将作品的实现、测试与数据对应到可查阅的文件。当前证据支持：PC 软件链路中的自适应路由、白名单动作执行、故障回退、逐请求记录和复算。")
table([
    ["证据层", "已保存的可复核内容", "适用范围"],
    ["网络策略台架", "四档链路、两臂对照、三类合成任务；每单元 n=20，共 480 请求", "TCP 软件回放；模型时间和语义为桩合同"],
    ["Dashboard 闭环", "四档链路、两类命令；每单元 n=20，共 160 请求", "真实 C/Python 路径与 PC JSON 设备状态"],
    ["当前源码核验", "87 条 C 断言、83 项 Python 测试、3 项数字门回归；n=1 入口烟测", "当前构建快照；不回填历史实验源码"],
], [32, 83, 57])
add("证据边界", "h2")
add("当前没有真实模型动作准确率、GPU 推理 p50/p95、物理设备执行、家庭试点、真实 5G/6G 空口或网络切片实测结果。场景设计、模拟验证与实际部署按层级分别表述。")
add("阅读顺序：软件闭环与防护（第 2 页）→ 台架设计和结果（第 3-4 页）→ 独立设备模拟证据（第 5 页）→ 测试复算与资源披露（第 6-7 页）→ 文件哈希（第 8 页）。", "small")

title(2, "可运行的软件闭环")
add("统一入口与动作确认", "h2")
add("Dashboard /api/command → Python 网关 → 唯一 C 策略进程 → 模型后端或本地规则 → 权威动作白名单 → PC JSON 状态持久化 → 结构化事件。模型给出的计划与本地执行结果分别记录。")
table([
    ["机制", "当前行为", "代码与回归入口"],
    ["唯一策略实现", "链路与已知算力观测进入 C；Python 不另写一套路由决策。", "offload_policy.c；policy_cli.c；test_policy_cli_compute.py"],
    ["动作边界", "仅允许灯光、窗帘、温湿度查询。危险、未知或畸形动作先拒绝，失败不会标作成功。", "dashboard/device_actions.py；test_device_actions.py；test_remote_execution.py"],
    ["故障回退", "超时、无效 JSON、后端未配置及策略进程异常写入事件，再按边界回退。", "offload_gateway.py；test_gateway_resilience.py；test_policy_cli_timeout.py"],
    ["部分健康观测", "ready/inflight 可独立驱动 C；未知显存和队列保持 null / -1，不伪造测量。", "backend_monitor.py；test_gateway_bridge.py；test_compute_policy.c"],
    ["复杂指令保护", "复合、条件、否定指令要求规划；回退不只执行一部分关键词命中。", "test_offload_gateway.py；test_remote_execution.py"],
], [30, 83, 59])
add("可观察事件", "h2")
add("每次请求保留命令类别、路由原因、后端、观测来源、已知/未知字段、执行结果与耗时分量。真实 wrapper 的模型 ID、GPU 环境声明及上游 HTTP 墙钟可传入事件；协议自测并不构成真实模型成绩。")
ref("SenseHome-6G/net_offload/tests/；openvela_smart_home/dashboard/tests/；docs/reviews/2026-11-03-final-review.md")

title(3, "锁定 n=20 台架的实验设计")
add("480 = 4 档链路 × 2 个策略臂 × 3 类任务 × 20 次请求。adaptive 与 static-cloud 在每一档共用 trace_id，逐连接实际 TCP 结果保存于 paired_traces。")
rows = [["链路档位", "WAN RTT / 抖动 / 丢包", "LAN 与预期路由"]]
seen = set()
names = {"P1-normal":"P1 正常", "P2-degraded":"P2 WAN 劣化", "P3-wan-down":"P3 WAN 断链", "P4-all-down":"P4 全网断链"}
for s in data["summaries"]:
    if s["phase"] in seen: continue
    seen.add(s["phase"])
    w,l=s["conditions"]["wan"],s["conditions"]["lan"]
    wan="断链" if w["blackout"] else f"{fmt(w['rtt_ms'])} / {fmt(w['jitter_ms'])} ms / {fmt(w['loss_pct'])}%"
    lan="断链；回到本地规则" if l["blackout"] else ("可用；复杂任务降到 local-model" if w["blackout"] or s["phase"]=="P2-degraded" else "可用；复杂任务可选 cloud-model")
    rows.append([names[s["phase"]],wan,lan])
table(rows,[34,66,72])
table([
    ["设计项", "固定约定"],
    ["对照策略", "adaptive 按任务和观测选择；static-cloud 强制走云后端。"],
    ["任务类别", "simple 结构化、complex 合成复杂 NLU、safety 合成安全任务。"],
    ["模型桩假设", f"cloud compute={fmt(data['stub_parameters']['cloud_compute_ms'])} ms；gateway compute={fmt(data['stub_parameters']['gateway_compute_ms'])} ms。两者均为注入值。"],
    ["时延定义", "用户总时延 = 实际探测耗时 + C 策略耗时 + 后端耗时。各分量中位数通常不能相加得到总中位数。"],
    ["正确性口径", "interface_ok 只表示收到响应；semantic_complete 是合成任务合同，不是自然语言准确率。"],
], [34,138])
add("不纳入样本的推论", "h2")
add("本台架不模拟无线物理层，不产生 6G 空口结论；全网断链时 complex 的接口仍可响应，但语义合同完成率为 0。不得据此表述“断网下所有复杂任务都能完成”。")
ref("net_offload/results/results.json：harness、summaries、raw_runs、paired_traces；results.md 给出完整 24 单元表。")

title(4, "复杂任务结果与已暴露的代价")
add("下表逐单元 n=20，仅报告合成 complex。时延为该单元全部请求的用户总墙钟中位数；失败请求的快速返回不代表任务完成。")
rows=[["档位 / 策略", "实际后端", "接口成功", "语义合同", "总时延中位 ms"]]
for s in data["summaries"]:
    c=s["by_task"]["complex"]
    b=next(iter(s["by_backend"])) if s["arm"]=="static-cloud" else {"P1-normal":"cloud-model","P2-degraded":"local-model","P3-wan-down":"local-model","P4-all-down":"local-rules"}[s["phase"]]
    rows.append([names[s["phase"]]+"\n"+s["arm"],b,fmt(c["completion_pct"])+"%",fmt(c["semantic_pct"])+"%",fmt(c["total_user_median_ms"])])
table(rows,[43,39,27,27,36])
add("结果支持的结论", "h2")
add("WAN 劣化与 WAN 断链而 LAN 尚存时，自适应路径选择网关逻辑后端并保持桩合同可用；全网断链退回规则后，复杂任务合同不再完成。结构化和安全合成任务保持本地路径，具体命令另由第 5 页验证。")
add("需要继续改进的成本", "h2")
add("P2 自适应复杂路径总时延中位数 596.2 ms，其中实际探测中位数 425.5 ms。原 K1≤350 ms、K2≤200 ms 目标仍未达到；下一轮应测有有效期的观测缓存，并用真实模型重新跑对照。")
ref("results.json 的 by_task.complex；探测聚合仅取 wan_probe_ok 非 null 的实际探测请求，不将本地快路径的 0 纳入中位数。")

title(5, "独立 Dashboard 动作与状态证据")
add("160 次请求来自真实 C 策略与 Python Dashboard 规则，覆盖开关灯和只读安防查询。每一档/任务 n=20，逐条 state_confirmed 对 PC JSON 模拟状态进行确认。")
rows=[["档位", "命令类型", "状态确认", "p50 ms", "p95 ms"]]
for s in dashboard["summaries"]:
    rows.append([names[s["phase"]],"开关灯" if s["task"]=="simple" else "只读安防查询",f"{s['n_state_confirmed']}/{s['n']}",fmt(s["p50_wall_ms"]),fmt(s["p95_wall_ms"])])
table(rows,[40,45,29,29,29])
add("可核验对象", "h2")
add("dashboard_results.json 保存分档汇总和逐条 wall_ms、event_id、state_confirmed。events.jsonl 保存同一请求的网关事件，可通过 event_id 对应。此处衡量的是 Python 规则路径和 PC JSON 状态，不是灯具、门锁或传感器。")
add("适用的具体边界", "h2")
add("本轮安全类数据仅为只读查询，不能外推为真实安防控制的授权、联锁或安全认证。旧联动场景与多写者状态事务不在这 160 条证据的覆盖范围。")
ref("SenseHome-6G/evidence/current/dashboard/dashboard_results.json；events.jsonl；_work/capture_dashboard_evidence.py")

title(6, "源码核验、数字复算与重现入口")
table([
    ["检查", "保存结果", "检查入口"],
    ["C 策略 / 算力", "70 + 17 条断言通过", "test_offload_policy.c；test_compute_policy.c"],
    ["Python 功能回归", "net_offload 68 项 + Dashboard 15 项通过", "python -m unittest discover -s tests -v（各模块目录）"],
    ["数字门回归", "3 项通过；含篡改恢复指标的拒绝测试", "_work/test_verify_numbers.py；verify_numbers.py"],
    ["一键入口烟测", "n=1，24 请求；四档两臂；跳过恢复过程", "run_evidence.ps1；validation/check_01.log"],
    ["方案检查", "23 页；30 目录项；8 嵌入字体；已保存脱敏与字数门结果", "validation/check_03.log；check_05.log"],
], [35,65,72])
add("最短复现命令（项目根目录）", "h2")
for text in [
    "./SenseHome-6G/net_offload/build.ps1",
    "python _work/verify_numbers.py",
    "python _work/check_draft.py",
    "python _work/verify_pdf.py",
    "python openvela_smart_home/dashboard/bridge_server.py --patrol-interval 0",
]: add(text,"mono")
add("历史实验与当前源码的对应", "h2")
add("n=20 运行当时没有保存完整源码清单。后续 build_manifest 证明当前交付源码和二进制，n=1 烟测证明当前流水线；两者均不能追认 n=20 发生时全部源码版本。probe_overheads 派生修正仅重算实际探测请求，raw_runs 和任务汇总保持原样。")
ref("evidence/current/validation/verification.json；provenance.json；docs/reviews/2026-11-03-final-review.md")

title(7, "复用、依赖与真实模型部署状态")
table([
    ["资产", "本轮复用与披露", "尚待确认"],
    ["已有 SenseHome 工程", "原 Tool/Skill、Dashboard 基础保留 LICENSE、NOTICE 与 THIRD_PARTY；新增网关与动作接线。", "历史上游精确 commit 未在本轮确认。"],
    ["新增 net_offload", "C 策略、CLI、Python 回放/网关、证据与模型验收代码；文件 hash 由 manifest 保存。", "不把外部工程、模型或工具链当作新增原创。"],
    ["PC 运行依赖", "CPython 3.13.7、GCC/MinGW-W64 15.2.0；运行模块使用 Python 标准库。", "PC 验证不能替代 RTOS 烧录或物理设备验收。"],
    ["模型适配器", "OpenAI-compatible HTTP wrapper 已完成本地假上游测试；设备动作不由模型直接执行。", "20 条标签仍为草案，不能自动标记为人工复核。"],
], [33,79,60])
add("AutoDL 的已观察状态（不是推理成绩）", "h2")
add("已登录 GPU 实例并观察 RTX 4080 SUPER、32760 MiB、驱动 595.71.05、Python 3.12.3；安装记录显示 Torch 2.14.0+cu130、vLLM 0.33.0。适配器等文件上传后已核对 hash。Qwen2.5-1.5B-Instruct 下载期间 SSH 入口持续拒绝连接，权重完整性和真实推理尚未验收。")
add("恢复后必须固定实际模型 revision、权重许可、运行时、GPU 和启动参数，人工冻结标签，保存逐条动作、失败原因与 p50/p95。环境连通或安装成功不能记为模型质量与时延结果。")
ref("compliance/README.md；CURRENT_DEPENDENCIES.md 为先前 PC 快照；最新部署状态以 docs/real_model_attempt_20261103.md 为准。模型权重与凭证不随本包分发。")

title(8, "文件索引与 SHA256")
add("下列哈希由本附件生成脚本直接读取文件计算。完整源文件、字节数与全部哈希另存 docs/evidence/source_manifest_20261103.json。", "small")
index_items = [
    ("原始 n=20 结果",SOURCES[0]),
    ("仅修正探测聚合的派生结果",SOURCES[1]),
    ("独立 Dashboard 160 请求",SOURCES[3]),
    ("Dashboard 逐请求事件",SOURCES[4]),
    ("当前核验报告",SOURCES[5]),
    ("当前源码/二进制快照",SOURCES[6]),
    ("证据版本关系",SOURCES[7]),
]
for label,path in index_items:
    add(label,"h2")
    add(path.relative_to(ROOT).as_posix(),"small")
    add(hashlib.sha256(path.read_bytes()).hexdigest(),"mono")
add("验收时同时核对：数据范围、样本分母、指标定义、代码版本和任务是否真正完成。后续真实模型与硬件结果应新增独立证据版本，不覆盖本版原始结果。", "small")

def decorate(canvas, doc):
    w,h=doc.pagesize
    canvas.saveState()
    canvas.setFillColor(GREEN)
    canvas.rect(18*mm,h-15*mm,12*mm,1.3*mm,fill=1,stroke=0)
    canvas.setFont(FONT,8)
    canvas.setFillColor(MUTED)
    canvas.drawString(18*mm,h-12*mm,"SENSEHOME  |  本地证据附件")
    canvas.setStrokeColor(LINE)
    canvas.line(18*mm,17*mm,w-18*mm,17*mm)
    canvas.drawString(18*mm,11.5*mm,"2026-11-03  /  TCP 台架与 PC 模拟设备；真实模型与 6G 空口未验证")
    canvas.drawRightString(w-18*mm,11.5*mm,f"{doc.page}")
    canvas.restoreState()

OUT.parent.mkdir(parents=True,exist_ok=True)
QA.mkdir(parents=True,exist_ok=True)
doc=SimpleDocTemplate(str(OUT),pagesize=(210*mm,297*mm),rightMargin=19*mm,leftMargin=19*mm,topMargin=25*mm,bottomMargin=24*mm,title="SenseHome AI+6G 场景设计佐证材料",author="SenseHome",subject="本地代码、测试、数据与披露来源")
doc.build(story,onFirstPage=decorate,onLaterPages=decorate)
manifest={p.relative_to(ROOT).as_posix():{"bytes":p.stat().st_size,"sha256":hashlib.sha256(p.read_bytes()).hexdigest()} for p in SOURCES}
(ROOT/"docs"/"evidence"/"source_manifest_20261103.json").write_text(json.dumps({"scope":"Source files used for the local evidence PDF; no credentials", "files":manifest},ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps({"output":str(OUT),"bytes":OUT.stat().st_size,"sha256":hashlib.sha256(OUT.read_bytes()).hexdigest()},ensure_ascii=False))
