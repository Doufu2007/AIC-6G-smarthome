"""Rebuild anonymous v4 competition materials from explicitly locked evidence.

No inference, live service, archive mutation, commit, or upload is performed here.
The project has no configured Codex artifact runtime. The existing local Office
fallback is recorded in the manifest, and every final PDF is rendered for QA.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from xml.sax.saxutils import escape
from lxml import etree

import fitz
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from pptx import Presentation
from pptx.dml.color import RGBColor as PPTColor
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt as PPTPt
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (PageBreak, Paragraph, SimpleDocTemplate, Spacer,
                               Table, TableStyle, KeepTogether, Image)

ROOT = Path(__file__).resolve().parents[2]
CONTENT = ROOT / 'docs/competition_v4'
OUT = ROOT / 'dist/final/20261111_v4'
WORK = ROOT / '_work/submission_20261111_v4'
NAME = '6G链路自适应家庭AI推理系统'
VERSION = '20261111_v4'
DATE = '2026年11月11日'
SOFFICE = Path('C:/Program Files/LibreOffice/program/soffice.exe')
FONT = 'Microsoft YaHei'
INK = '172D3A'
ACCENT = '287366'
GRAY = '55636A'
PALE = 'EEF3F0'
INPUTS = CONTENT / 'results_inputs.json'


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rel(path):
    return Path(path).relative_to(ROOT).as_posix()


def source(path, required=False):
    p = ROOT / path if path else None
    if p and p.is_file():
        return load(p)
    if required:
        raise FileNotFoundError(path)
    return None


def fmt(value):
    return f'{value:.1f}' if isinstance(value, (float, int)) else '未采集'


def p(text):
    return {'kind': 'p', 'text': text}


def h(text):
    return {'kind': 'h2', 'text': text}


def t(headers, rows, widths=None):
    return {'kind': 'table', 'headers': headers, 'rows': [[str(v) for v in r] for r in rows], 'widths': widths}


def page(id, title, *blocks):
    return {'id': id, 'title': title, 'blocks': list(blocks)}


def all_text(pages):
    return '\n'.join([pp['title'] + '\n' + '\n'.join(
        b.get('text', b.get('caption', '')) if b['kind'] != 'table' else '\n'.join(' '.join(r) for r in [b['headers']] + b['rows'])
        for b in pp['blocks']) for pp in pages])


def extract_rows(path):
    if not path or not (ROOT / path).is_file():
        return []
    return [json.loads(line) for line in (ROOT / path).read_text(encoding='utf-8').splitlines() if line.strip()]


def numeric_summary(values):
    values = sorted(v for v in values if isinstance(v, (float, int)) and math.isfinite(v))
    if not values:
        return '未采集'
    return f'{median(values):.1f} / {values[max(0, math.ceil(.95 * len(values)) - 1)]:.1f} / {len(values)}'


def build_content(cfg):
    criteria = source('docs/competition_v3/criteria.json', True)
    baseline = source(cfg.get('baseline_summary'))
    integration = source(cfg.get('integration_summary'))
    operations = source(cfg.get('operations_results'))
    video = source(cfg.get('video_manifest'))
    raw_rows = extract_rows(cfg.get('baseline_rows'))
    public = source('docs/value/public_sources.json', True)
    arms = [('local-first', '本地优先'), ('second-first', '第二端优先'), ('joint', '联合初选')]
    conds = [('healthy', '健康'), ('local_wait700', '本地等待700ms'), ('second_wait700', '第二端等待700ms'),
             ('local_post503', '本地POST503'), ('second_post503', '第二端POST503'), ('both_post503', '两端POST503')]
    baseline_ready = bool(baseline and baseline.get('by_condition'))
    if baseline_ready:
        condition_rows = [[label] + [f"{baseline['by_condition'][key][arm]['completed']}/{baseline['by_condition'][key][arm]['n']}" for arm, _ in arms] for key, label in conds]
        aggregate_rows = [[label, f"{baseline['by_arm'][arm]['completed']}/{baseline['by_arm'][arm]['n']}", fmt(baseline['by_arm'][arm]['median_total_wall_ms_all_requests']), str(baseline['by_arm'][arm]['proxy_post']), str(baseline['by_arm'][arm]['model_post'])] for arm, label in arms]
        ancillary_rows = [[label, str(baseline.get('ancillary_empty_plan', {}).get(arm, {}).get('n', '未采集')), str(baseline.get('ancillary_empty_plan', {}).get(arm, {}).get('safe_no_action_structure', '未采集'))] for arm, label in arms]
        rec = baseline.get('recommendation', {})
        recommendation = ('三臂复杂完成均20/24。按预设“完成数优先，其次全请求中位时延”选择本地优先为当前默认。'
                          '本地优先955.506ms，联合976.297ms，第二端1265.095ms；本地与联合相差20.791ms，不能解释为显著优势。')
    else:
        condition_rows = [[label, '待封存', '待封存', '待封存'] for _, label in conds]
        aggregate_rows = [[label, '待封存', '待封存', '待封存', '待封存'] for _, label in arms]
        ancillary_rows = [[label, '待封存', '待封存'] for _, label in arms]
        recommendation = '默认选择须等三臂同条件运行与独立复算后确定；不根据历史合成收益预先宣布联合更好。'
    timing_keys = [('c_decision_ipc', 'C决策含IPC'), ('observation_health', '健康观测'), ('first_model', '首端规划'),
                   ('recovery_health', '恢复前健康'), ('backup_model', '备用规划'), ('executor', '执行与确认'), ('total_wall', '请求总墙钟')]
    primary_rows = [r for r in raw_rows if r.get('cohort') == 'reviewed_complex_regression']
    timing_rows = [[label, numeric_summary([r.get('timing_ms', {}).get(key) for r in primary_rows])] for key, label in timing_keys]
    n_external = cfg.get('external_feedback_count', 0)
    assert n_external == 0, '接入真实反馈须先增加匿名原始证据和授权记录，不能只修改计数'
    assert cfg.get('actual_total_cost') is None, '接入实际成本须先核验凭据与分项账本'
    abstract = ('家庭自然语言控制依赖模型和服务可用性，收到接口响应仍可能没有完成指令。SenseHome面向存量住宅集成服务，提供设备规则、网关模型与第二模型候选，由统一网关完成任务分类、初选、最多一次规划恢复、动作白名单及状态核验。同机双模型上比较本地优先、第二端优先与联合初选，三臂共用恢复规则、模型、提示、超时和初态。六类条件下复杂任务各完成20/24，预设规则推荐本地优先，未证明联合具有完成率优势。正式执行通道接入持久命令身份、冲突检查、回执和状态回查，实际MQTT软件实体六类故障核验通过，未知结果只回查，不盲目重发。软件状态、真实模型、代理故障和历史合成结果分层保存，原250毫秒目标未达记录完整保留。当前为设计验证阶段，两个模型共享本机GPU，MQTT实体和JSON状态不代表实物完成，尚无真实6G空口、独立主机及外部用户反馈。后续以授权试用、低风险设备、独立网络和实际成本逐层验收。')
    assert 300 <= len(abstract) <= 500, len(abstract)
    integration_status = ('正式网关已接入持久回执适配器。实际本机MQTT服务、客户端与SQLite软件实体完成六类故障核验，均PASS；逐项记录见佐证。'
                          if integration else '本轮接线结果尚未锁定，以下描述为实现与验收结构。')
    operation_status = (f"本轮从新目录复制{operations['copied_source_file_count']}个源码文件，自动启动至就绪{operations['automated_launch_to_ready_ms']:.1f}ms，首任务{operations['automated_first_task_wall_ms']:.1f}ms。"
                        f"规则路径以1请求/秒运行约{operations['observed_duration_s']:.0f}秒，共{operations['dispatched_requests']}请求，{operations['expected_behavior_observed']}条行为符合预期、传输错误{operations['transport_errors']}。"
                        if operations else '本轮自动部署与固定负载结果尚未锁定，保留采集入口。')

    plan = [
        page('overview', '场景设计方案', h(NAME), p('SenseHome  '+DATE+'  v4'),
             p('场景类型  面向实际需求的应用场景设计方案'), h('作品简介'), p(abstract),
             p('主线  家庭复杂指令在后端异常时的恢复与执行核验。当前成熟度为软件原型与分层验证。')),
        page('demand', '一 场景概述与需求来源', h('目标角色与业务流程'),
             p('设计面向为存量住宅接入灯光、窗帘和环境设备的集成服务者，最终使用者为住户。使用者发出自然语言指令，集成服务者负责配置权限、定位异常与维护系统。该客户画像仍是设计定位；真实反馈保存前，项目外部反馈数保持0。'),
             h('需要解决的问题'), p('网络可连接时，模型调用仍可能超时或返回不合规动作。自动重试又可能重复控制设备。操作者需要区分请求收到、计划生成、已提交与状态确认，并知道哪一部分没有完成。'),
             t(['当前环节', '失败后果', '方案响应'], [['自然语言交给单个模型', '服务异常后复杂指令中断', '受约束的一次备用规划'], ['模型计划直接作为成功', '越权动作或空计划被误报', '整项动作校验和状态核验'], ['执行超时后再次发送', '同一动作可能重复', '持久命令身份与回查']], [36, 55, 67]),
             h('需求证据状态'), p('公开平台文档用于解释技术背景，不能代替本项目住户访谈。中性提纲与完整模型版隔离试用入口已准备；尚未保存的授权、原话、用户任务与等待阈值均保持待采集。')),
        page('kpi', '二 需求指标与验收口径',
             t(['指标', '定义和分母', '判定边界'], [['完整任务完成', '冻结参考动作一致且状态满足目标 / 非空任务数', '软件状态层，失败全保留'], ['安全无动作', '空目标下无动作且状态不变 / 空目标任务数', '不自动等价澄清话术正确'], ['总时延', '从客户端发起到响应结束的墙钟', '全部请求和成功请求分开'], ['调用开销', '代理POST、模型POST、恢复次数', 'GET健康检查单列'], ['执行可追溯', 'command_id、payload摘要、平台身份与逐项状态', '确认、部分、未知分别报告'], ['需求与成本', '真实授权记录、采购和能耗工时凭据', '反馈0；总成本null']], [33, 78, 47]),
             p('原250ms是旧应用配置，未经客户确认，也不是官方要求。历史v3成功请求250ms内完成0/18，最短841.612ms，继续保留。界面先显示“收到”不计为任务完成，不能通过修改口径追认达标。'),
             p('官方评分维度为场景真实性20、创新10、技术落地30、产业推广20、答辩10、协作10。本文依据场景设计大纲组织，不设臆测获奖分数线。')),
        page('architecture', '三 系统架构与业务流程',
             {'kind': 'image', 'path': '_work/submission_20261111_v4/architecture.png', 'caption': '图1 当前软件角色与接口结构'},
             h('一次请求'), p('明确支持的简单命令与只读查询使用规则。复杂指令先读取带来源和年龄的观测，再选择首个候选。合格传输失败最多发起一次备用规划；两个候选均失败时报告未完成。模型只产出计划，统一网关在白名单校验后执行。'),
             p('执行提交前持久化命令与动作。回执应绑定动作、command_id及平台身份；确认丢失时查询状态，同ID异payload视为冲突。无法确认时保留unknown，并向操作者报告。'),
             p('图1的规则、模型、执行路径均由网关统一记录。所有“第二端”均指逻辑角色，当前不称云端独立节点；部署清单为Python网关、C策略、llama.cpp、两个GGUF、SQLite及MQTT服务与客户端。')),
        page('mechanism', '三 关键机制与已有方案差异',
             h('初选与恢复分开验收'), p('初选决定先调用哪一候选，恢复器决定合格失败后是否尝试备用。三臂共享恢复器，避免把恢复能力只给联合策略。本地优先是合理的强基线；若它在本轮更好，就采用它作为默认，C联合策略保留为未来真实资源观测的扩展。'),
             h('执行核验'), p('动作合同约束房间、设备与允许值，越权或畸形计划整项拒绝。持久命令与状态回查补上跨请求、ACK丢失和重启后的对账；逐项状态支持部分完成，不承诺多设备物理原子回滚。'),
             t(['对标', '已有能力', '本作品验收重点'], [['Home Assistant Assist', '本地自然语言与模型接入', '异常后的来源与逐项执行状态'], ['Home Assistant AI agent', '本地意图优先和实体授权', '三臂同款恢复比较与事件证据'], ['RouteLLM', '成本质量驱动模型路由', '家庭动作合同及网络算力观测适配']], [37, 59, 62]),
             p('不将规则优先、动作白名单或模型路由单独宣称首创。尚未开展竞品同硬件同任务性能对照；差异是具体工程组合与可复核验证范围。')),
        page('ai6g', '三 AI与6G的接口位置',
             t(['通信计算协同方向', '已提供的软件接口', '外部验收条件'], [['网络支持推理位置选择', 'WAN与LAN分开；值、单位、来源、时间及年龄', '接真实链路遥测和尾时延'], ['算力支持模型候选检查', '健康、队列、内存、并发与known字段', '真实设备容量和负载校准'], ['任务预算约束协同', '规则快速路径和复杂规划路径分开', '需求方确认任务预算与损失'], ['事件贯通处理环节', '初选、实际模型、计划、执行及确认绑定trace', '授权平台与独立节点接入']], [43, 68, 47]),
             p('ITU-R M.2160-0将AI与通信列入IMT-2030愿景；ETSI MEC提供边缘计算与网络信息接口方向。这些来源支持方案定位，不证明本系统已接入6G。'),
             p('当前未实现6G空口、切片、确定性承载或无线资源分配。700ms等待和HTTP503由软件代理控制，不是无线RTT、真实GPU队列或事故发生率。两个模型共享同一GPU，不能外推到独立端云。')),
        page('baseline', '四 三臂公平比较',
             p('本轮从既有人工冻结命令中选C01—C04四条复杂可执行任务，三臂、六条件各一次，共72行；健康条件另有澄清和拒绝24行。它是旧人工标签回归，不是新独立人工留出，也不是96位用户。'),
             p('三臂共用模型、提示、动作白名单、超时、初态和最多一次恢复，只改变初选。每块轮换顺序，执行前注册源码、任务、条件和顺序摘要。'),
             t(['条件', '本地优先', '第二端优先', '联合初选'], condition_rows, [53, 35, 35, 35]),
             p(recommendation), p('单元内只有4条既有任务，不把小样本完成率和分位数当作总体可靠性或SLA。全部错误与保护拒绝保留；独立复算入口见佐证。')),
        page('timing', '四 时延与调用开销',
             t(['初选策略', '复杂完成', '全请求p50 ms', '代理POST', '模型POST'], aggregate_rows, [36, 29, 37, 28, 28]),
             t(['分段', 'p50 / p95 ms / 有效n'], timing_rows, [62, 96]),
             p('分段表汇总72条复杂请求，未发生的分段记0。C决策含子进程IPC，C内部耗时未知时保持null；执行器分段可能含提交及软件状态确认。并发与嵌套段不直接相加解释为总墙钟。'),
             p('本轮60条复杂成功请求250ms内完成0/60，最短341.576ms，历史0/18单列。未开展独立冻结的提示优化前后实验，不能宣称已完成时延优化或无损提速。模型常驻与规则快速路径为现有机制。')),
        page('receipt', '四 持久回执与MQTT软件实体', p(integration_status),
             t(['情形', '合同要求', '可验收观察'], [['重复命令', '相同ID相同payload只复用记录', '不新增动作提交'], ['同ID异payload', '拒绝冲突', '冲突记录且状态不变'], ['ACK丢失', '结果未知后只查询', '不因超时盲目重发'], ['网关重启', '从持久库恢复待确认记录', '回查并保留原命令身份'], ['部分或矛盾回执', '逐项报告；不接受不一致身份或动作', '已确认、未执行、未知分别保存']], [33, 66, 59]),
             p('MQTT使用实际AMQTT 0.12.1、paho 2.2.0、协议消息与软件实体。正常控制与回查走正式网关；部分完成的二动作场景使用固定contract probe，不称真实模型推理。软件实体状态不证明实物灯具或执行器动作。'),
             p('数据库与事件记录用于回放和核对。当前不承诺物理exactly-once、断电后硬件位置可靠、生产鉴权或安全认证；这些须在授权低风险设备上独立验收。')),
        page('history', '四 历史证据与失败保留',
             t(['独立证据层', '规模与关键事实', '支持范围'], [['旧封存四矩阵', '600行；人工、开发、受控观测、程序留出分开', '程序留出joint21/24，第二端24/24'], ['旧HTTP代理故障', '120行；两条命令、五条件、四臂', '决策前故障与700ms代理等待'], ['v3健康后调用竞态', '36行；本地POST503原0/6、恢复6/6', '健康均6/6；双503均0/6'], ['v3原时限', '成功250ms内0/18，最短841.612ms', '未达到旧应用配置'], ['r3合成回放', '复杂每臂5,040；joint84.40%、仅网络62.98%', '固定规划桩；不能当语言准确率']], [41, 75, 42]),
             p('H05中小模型将窗帘close写成off，保护层拒绝；失败保留，未据此调优后再追称留出。旧数据不修改、不与v4合并成一个准确率。'),
             p('历史单模型AutoDL动作一致17/20是独立环境证据，本轮没有连接该服务。当前主张以同机软件范围成立；历史合成优势未在正常健康真实模型上证明联合更优。')),
        page('deployment', '四 部署复制与稳定性', p(operation_status),
             h('复制路径'), p('复制到新目录，核验运行时、DLL、模型和策略哈希，实际绑定端口后再启动。默认规则版与完整模型版隔离状态；模型版采集器应记录同意、用户自拟任务、实际结果与反馈。回滚只停止本轮拥有的进程，保留记录，再切回已验收规则路径。'),
             t(['验收项', '本轮可自动完成', '需要真实外部输入'], [['新目录启动', '哈希、启动、首个任务和错误定位日志', '同学独立操作及真实人工工时'], ['固定负载', '固定请求率、故障序列、资源和事件计数', '长期真实家庭负载'], ['成本', '分项账本与未知传播', '采购、整机能耗积分、电价和维护工时'], ['迁移', '协议与接口合同', '独立主机、实网、授权设备']], [32, 70, 56]),
             p('固定负载仅为规则路径加MQTT软件实体，未做模型浸泡测试。自动部署耗时是自动化墙钟，不称同学人工安装工时。两分钟负载不等于长期生产稳定；独立主机与真人独立部署仍待实测。')),
        page('value', '五 产业价值与推广路径',
             h('先验收可量化的服务价值'), p('目标价值是减少受支持任务在模型异常时的中断，并让维护者查清失败环节。采用同任务比较完整完成数、全请求等待、人工重试、调用开销和部署维护工时，避免用接口200或演示流畅度代替业务效果。当前尚无客户采用、收入或市场规模实证。'),
             t(['阶段', '验收内容', '退出或扩展条件'], [['授权探索', '3—5位自愿者先谈现有方式、具体困扰与影响', '没有痛点也照实保存'], ['任务试用', '参与者自拟任务、实际过程、反向反馈及等待阈值', '开发者和自动化不计外部用户'], ['低风险试点', '一类灯光设备、独立网络、回执和回滚', '只扩展已验收权限'], ['复制推广', '独立操作、实际成本、维护负担', '有净价值证据后再谈规模']], [30, 82, 46]),
             p('月成本可按硬件摊销、电费、推理、网络与维护分项，单位成功请求成本以成功数作分母。当前采购、电价、整机能耗及工时缺凭据，总成本为null；本机无外部API收费不等于系统零成本。')),
        page('team', '六 团队与资源保障',
             p('当前由一名学生承担需求建模、策略与网关实现、实验和材料。使用任务清单、预注册、版本摘要、原始事件与独立复算管理依赖；AI辅助工具不计作真人成员或外部参与者。'),
             t(['工作包', '交付与校核', '资源边界'], [['需求与任务', '知情同意、匿名原话、任务和负面反馈', '真实授权与原话待采集'], ['实现与协议', 'C策略、统一网关、MQTT和持久回执', '普通PC；无实物设备'], ['实验与复核', '锁定输入、轮换顺序、失败、逐行复算', '同机共享GPU；无真实6G'], ['材料与发布', '同一主张表生成方案、答辩、佐证及哈希', '最终人工审阅和上传待执行']], [32, 78, 48]),
             h('原创性与生成式AI说明'), p('复用既有SenseHome/openvela原型与第三方运行时，新增策略适配、恢复、执行核验和证据工具。上游许可、模型许可及修改范围以compliance/、NOTICE与依赖清单为准。暂无专利、软著或客户证明，不将工具文档作为此类证书。'),
             p('使用生成式AI辅助代码、测试、文档、图表和视频脚本；本版视频使用Windows合成旁白，来源与编辑边界见视频清单。参赛者负责核实生成内容、数据、引用和原创性。匿名材料不包含单位名称、LOGO、指导教师或私人联系信息。')),
        page('roadmap', '七 验证计划与复核索引',
             t(['未完成项', '来源和验收', '当前状态'], [['真实需求与用户任务', '授权记录、匿名原话、过程与成功失败', '外部反馈0，待真实保存'], ['物理设备', '命令、平台ACK、传感器或设备状态逐项核对', '无实物验证'], ['独立节点与网络', '两台资产、模型哈希、实链路和资源', '当前同机'], ['实际成本', '凭据、整机能耗、电价与工时分摊', '总成本null'], ['永久材料链接', '百度网盘永久有效、提取码和官方命名', '待创建并核验']], [39, 79, 40]),
             p('正式材料使用同一主张索引，核心数字均可回到注册、原始行、摘要和复算文件。佐证为一个PDF；源码与完整事件放入其他材料目录。上传前核对方案不超过15MB、视频3—5分钟且不超过300MB、答辩PDF、300—500字简介、匿名及永久链接。'),
             p('依据：官方附件1作品提交要求、附件2场景设计大纲与评分标准；ITU-R M.2160-0；ETSI MEC；Home Assistant与RouteLLM官方文档。访问快照、URL和哈希见公开来源页。')),
    ]

    evidence = [
        page('scope', '佐证范围与阅读索引', p(NAME+'  '+DATE+'  v4'),
             p('本附件只把可核验的软件、模型与过程事实作为证据。正文围绕三臂公平比较、执行回执与复制验收展开；历史矩阵单列，不逐轮混算。'),
             t(['证据层', '当前含义', '不能推导'], [['真实模型', '实际GGUF推理响应与计划', '真实用户泛化或云端独立性'], ['受控代理', '实际HTTP等待与503返回', '自然网络故障率或6G性能'], ['JSON / MQTT软件状态', '软件实体提交、回执和回查', '物理设备动作'], ['自动部署与固定负载', '脚本墙钟、请求与资源记录', '真人工时或长期可靠性'], ['需求与成本', '采集入口与未知账本', '未取得的反馈与效益']], [45, 66, 47]),
             p('每项证据的完整相对路径和SHA256写入source_manifest.json。材料页码索引同时定位方案、答辩和本附件。')),
        page('registration', '三臂实验注册与计量', p('正式数据路径由results_inputs.json显式锁定。无路径或文件不存在时，不将该项标为已完成。'),
             p('任务为既有冻结人工标签的回归，C01—C04用于复杂可执行比较；澄清/拒绝另列。程序生成任务若未人工核验，仅能作开发集。注册必须早于推理；未修改旧提示、标签、H05或历史日志。'),
             t(['控制要素', '共同配置'], [['初选外条件', '同模型、提示、白名单、超时、恢复和初态'], ['六条件', '健康、单端各等待700ms、单端各POST503、双503'], ['样本含义', '72复杂行与24空目标行分别报告；相关输入不视为独立用户'], ['计时', 'C/适配、健康、首端、恢复、备用、执行与总墙钟'], ['失败', '原始错误、保护拒绝、双失败和执行次数完整保留']], [40, 118]),
             p('注册入口：'+str(cfg.get('baseline_registration') or '待锁定')+'；复算入口：'+str(cfg.get('baseline_verification') or '待锁定'))),
        page('conditions', '三臂分条件完整完成结果', t(['条件', '本地优先', '第二端优先', '联合初选'], condition_rows, [53, 35, 35, 35]),
             p('完成要求动作满足冻结参考且软件状态达到目标；不能仅依据上游HTTP200。每条件每臂4条已知复杂任务，不发布总体可靠性置信保证。'),
             p(recommendation), p('摘要：'+str(cfg.get('baseline_summary') or '待锁定')+'；逐行：'+str(cfg.get('baseline_rows') or '待锁定'))),
        page('overhead', '调用与空目标分项', t(['初选策略', '复杂完成', '总墙钟p50 ms', '代理POST', '模型POST'], aggregate_rows, [36, 29, 37, 28, 28]),
             t(['初选策略', '空目标n', '无动作且状态不变'], ancillary_rows, [50, 38, 70]),
             p('空目标下没有动作只证明结构与状态保护，澄清是否表达正确仍需人工语义核验。代理POST含被503拦截的尝试，模型POST是实际到达模型的推理，GET健康不计入两者。')),
        page('timing', '分段时延及旧目标', t(['计时段', 'p50 / p95 ms / 有效n'], timing_rows, [62, 96]),
             p('p95使用最近秩描述统计；本表含72条复杂请求，未发生分段记录0。以本地优先为例，备用实际发生8次，中位601.065ms；总体备用中位0不代表备用无代价。未做同输入优化前后对照，C内部保持null，嵌套段不能简单求和。'),
             p('本轮复杂成功250ms内0/60，最短341.576ms。102次评测代理POST含66次真实模型200和36次代理503，另12预热单列。评测usage为38898输入、4173输出token。'),
             p('旧v3成功18条中250ms内0/18，最短841.612ms。该目标为应用旧配置，非官方门槛与客户SLA；原始记录保留。')),
        page('integration', '正式执行通道与持久命令', p(integration_status),
             p('接口结构：动作合同通过后写入command_id、规范payload摘要、平台身份、逐动作目标和待确认状态；适配器发送；接收回执或回查；数据库记录确认、部分或未知。相同ID且payload不同必须冲突。'),
             t(['字段或状态', '核验作用'], [['command_id / payload_hash', '将重试与新命令区分；阻止ID复用混淆'], ['platform identity / action identity', '拒绝来源、设备或动作不匹配的回执'], ['submitted / confirmed / partial / unknown', '避免把提交成功解释为全部完成'], ['SQLite与事件', '跨网关进程保存待确认记录并回查']], [58, 100]),
             p('锁定结果：'+str(cfg.get('integration_summary') or '待锁定')+'；事件：'+str(cfg.get('integration_events') or '待锁定'))),
        page('mqtt', 'MQTT软件平台故障核验',
             t(['故障或重放', '必须观察的结果'], [['重复相同命令', '复用命令记录，不再次提交动作'], ['相同ID不同payload', '冲突拒绝，既有记录保持一致'], ['ACK丢失', '未知后只回查，不盲重发'], ['网关重启', '重新加载持久记录，原ID对账'], ['部分动作完成', '逐项列已执行、未执行和未知'], ['矛盾或伪身份回执', '拒绝确认，保留错误与原始事件']], [52, 106]),
             p('六类本轮核验均PASS，离线重数确认各命令实际submit恰好1次，自有进程已停止。网关155项、Dashboard49项回归通过；根目录pytest仍有36项历史模块重名与导入收集错误，不能称整个工作区全绿。'),
             p('AMQTT 0.12.1与paho 2.2.0构成真实本机软件协议过程，部分二动作故障用固定contract probe，不是模型推理结果。未接物理执行器，不宣称实物完成或物理exactly-once。'),
             p('复核：'+str(cfg.get('integration_verification') or '待锁定'))),
        page('operations', '新目录部署与固定负载', p(operation_status),
             p('负载含116条可执行和4条无效请求，规则路径与MQTT软件实体运行，非模型浸泡。含首任务共121条网关事件，原状态和原事件未变；本轮自有进程已停止。进程树RSS求和峰值147460096 B，可能重复计共享页，不是整机内存或能耗；事件增长622894 B。'),
             p('结果入口：'+str(cfg.get('operations_results') or '待锁定')),
             t(['事实边界', '材料表达'], [['自动化脚本墙钟', '不称同学人工独立部署工时'], ['本机复制目录', '不称第二独立主机'], ['短时稳定运行', '不称全天或生产可靠'], ['GPU遥测', '不等同整机电表能耗']], [55, 103])),
        page('trial', '需求采集与真实成本状态',
             p('先询问现有方式、最近一次具体困扰及影响，再问期望与可接受等待。演示后请参与者自拟3—5个任务，保存原话、实际过程、成功失败、是否理解结果及负向反馈。匿名引用需单独同意；没有困扰可照实记录。'),
             p('当前项目真实外部反馈数为0。预定参与、开发自测和自动化均不计作已收反馈。真实记录需完成授权并保存后才能更新计数；3—5人探索也不能推断市场比例。'),
             t(['成本项', '当前'], [['硬件摊销', '未知'], ['整机电量与电价', '未知'], ['维护与部署工时', '未知'], ['网络分摊', '未知'], ['实际总成本', 'null']], [68, 90]),
             p('历史415次本机POST的外部API直接费用分项为0，仅限该运行。成本账本见docs/value_v2/results/ledger_current_result.json，不将0分项写作总成本。')),
        page('history', '历史模型结果与失败',
             t(['矩阵', '分母', '保留结论'], [['旧人工回归', '240行，复杂每臂21请求', '健康条件未证明联合优于固定策略'], ['旧程序留出', '144行；非空每臂24', 'joint21/24；第二端24/24'], ['H05', '3重复×3小模型路径', 'close写off被拒，未据此调优'], ['旧受控观测与开发', '72行与144行', '程序开发与观测注入单列']], [41, 56, 61]),
             p('四矩阵共600行；同一任务和条件重复，不能视为600独立语言样本。索引：docs/experiments/20261111_fused_real/sealed_evidence_index.json。'),
             p('历史AutoDL单模型20条动作一致17/20；其GPU、运行时与模型环境不同，不合并到本轮同机矩阵。原始响应与后续标签重标分别保留。')),
        page('v3', '历史代理与恢复机制',
             t(['历史运行', '结果', '范围'], [['v2受控HTTP', '120行；两命令、五条件、四臂', '故障在决策前已知'], ['v3健康', '原网关6/6；恢复6/6', '两命令各三重复'], ['v3本地POST503', '原0/6；恢复6/6', '健康成功后调用竞态'], ['v3双POST503', '原0/6；恢复0/6', '失败不执行'], ['v3时限', '250ms内成功0/18', '最短841.612ms']], [42, 68, 48]),
             p('v3共48次代理POST，其中18次模型200与30次代理503；另2次预热不计正式行。成功请求各执行1次，失败各0次。usage为10575输入和1227输出token。'),
             p('入口：docs/experiments/20261111_real_fault_v2/与docs/experiments/20261111_race_v3_portfix/。这两套与v4独立，不混为总体准确率。')),
        page('public', '公开技术来源与支持范围',
             t(['来源', '官方URL'], [[r['publisher']+' '+r['title'], r['url']] for r in public['records'] if r['id'] in ('S1', 'S3', 'S4', 'S6', 'S7', 'S8')], [58, 100]),
             p('访问日期为2026-11-11。摘录、支持范围和快照SHA256见docs/value/public_sources.json。Home Assistant证明本地意图与模型接入已有；RouteLLM证明模型路由已有；ITU与ETSI提供方向映射。它们不证明本项目客户痛点、性能优势或实际收益。')),
        page('sources', '证据文件与摘要', p('本页列复核入口，完整SHA256见同版source_manifest.json。文件哈希证明内容标识，不能自动证明内容真实。'),
             t(['证据入口', '用途'], [[str(cfg.get('baseline_summary') or '三臂结果待锁定'), '三臂、六条件与调用'], [str(cfg.get('integration_summary') or 'MQTT结果待锁定'), '正式回执通道'], [str(cfg.get('operations_results') or '部署结果待锁定'), '复制与固定负载'], ['docs/experiments/20261111_fused_real/sealed_evidence_index.json', '历史600行与H05'], ['docs/experiments/20261111_race_v3_portfix/summary.json', '旧恢复与时限'], ['docs/competition_v3/criteria.json', '官方要求与事实红线']], [111, 47]),
             p('本轮独立审查发现的3项P1和2项P2已修复并复核，当前已审范围无未修复P1/P2。报告：docs/reviews/2026-11-11-v4-independent-review.md；该结论不替代最终PDF/PPT逐页、视频或提交包封版检查。'),
             p('所有入口均为工程内相对路径，正式材料不含本机个人目录、联系方式或口令。')),
        page('checks', '官方要求与待提交项',
             t(['要求', '本版核验方式', '待人工完成'], [['名称≤20字；简介300—500字', '生成器断言并输出简介.txt', '报名名称一致'], ['方案PDF≤15MB', '最终文件大小与页面提取', '上传核对'], ['视频MP4 3—5分钟≤300MB', '视频清单与ffprobe', '真实内容与剪辑确认'], ['答辩PDF；一个佐证PDF', '生成器输出，逐页渲染', '上传文件选择'], ['匿名及AI说明', '文本和OOXML扫描、人工视觉检查', '承诺与责任核实'], ['其他材料永久链接', '当前缺项不伪造', '网盘永久有效、提取码及编号命名']], [50, 65, 43]),
             p('本版视频为当前软件运行记录与注册原始行的编辑回放，非连续现场录屏；画面驻留为剪辑时长，不作运行时延。Windows合成旁白，软件实体非实物。来源与剪辑清单：'+str(cfg.get('video_manifest') or '待锁定')+'。'),
             p('演示实际策略为local-first/static-local；原始清单继承的joint字段不作执行证据。更正：docs/video/v4/metadata_correction.json。'),
             p('没有永久链接前，正式提交流程仍有形式缺项。原始代码包与其他材料命名需使用真实参赛团队编号；内部审阅包不代表已向赛事系统提交。')),
    ]

    slides = [
        {'id': 'overview', 'title': NAME, 'lead': '家庭复杂指令的恢复与执行核验', 'lines': ['SenseHome  场景设计方案  v4', '同机真实模型与MQTT软件验证'], 'notes': abstract},
        {'id': 'demand', 'title': '家庭模型控制的业务问题', 'lead': '收到响应以后，任务仍可能没有完成', 'lines': ['单模型异常会中断复杂指令', '自动重发可能造成重复执行', '操作者需要看到来源与逐项状态'], 'foot': '目标客户为设计定位，真实外部反馈仍为0', 'notes': '方案需求页。当前无客户访谈或已落地证明。'},
        {'id': 'architecture', 'title': '系统处理流程', 'flow': True, 'notes': '统一网关分类与选择，最多一次备用规划，动作校验后执行与确认。两个模型共享本机GPU；JSON/MQTT软件实体。'},
        {'id': 'mechanism', 'title': '初选与恢复分别比较', 'table': [['三臂', '唯一变化', '共同配置'], ['本地优先', '先请求本地槽位', '同模型与提示'], ['第二端优先', '先请求第二槽位', '同一次恢复'], ['联合初选', 'C策略选择', '同动作与初态']], 'lines': ['简单策略更好时采用简单默认'], 'notes': 'Home Assistant已有规则优先与模型能力，不宣称本项目首创。来源docs/value/public_sources.json。'},
        {'id': 'ai6g', 'title': 'AI与通信计算的衔接', 'lines': ['WAN与LAN分别观测，保留来源与年龄', '算力字段未观测时保持unknown', '任务预算约束候选与恢复', '真实链路和独立节点逐层验收'], 'foot': 'ITU与ETSI方向映射；当前无6G空口和无线资源实测', 'notes': 'ITU-R M.2160-0；ETSI MEC，URL见公开来源。'},
        {'id': 'baseline', 'title': '三臂公平比较结果', 'table': [['条件', '本地优先', '第二端优先', '联合']] + condition_rows, 'foot': '4条旧人工任务×6条件×3臂，共72行；非独立新测试', 'notes': str(cfg.get('baseline_summary'))+'。三臂共用恢复器，只变初选。'},
        {'id': 'timing', 'title': '等待与调用开销', 'table': [['策略', '复杂完成', '总墙钟p50 ms', '模型POST']] + [[r[0], r[1], r[2], r[4]] for r in aggregate_rows], 'lines': ['分段记录健康、规划、备用、执行与总时延', '旧250ms达标0/18继续保留'], 'foot': '小样本描述统计，未完成独立优化前后对照', 'notes': '分段数据见rows.timing_ms。C内部unknown保留。'},
        {'id': 'receipt', 'title': '执行结果可回查', 'lead': '持久命令身份贯通提交与确认', 'lines': ['相同ID相同内容复用，相同ID异内容拒绝', '丢ACK与重启后只回查，不盲目重发', '部分完成与未知按动作分别报告'], 'foot': '实际MQTT软件实体，不能证明实物执行', 'notes': str(cfg.get('integration_summary'))+'；数据库、事件与故障核验见佐证。'},
        {'id': 'history', 'title': '失败与不同证据层保留', 'table': [['证据', '结果', '范围'], ['旧程序留出', 'joint21/24，第二端24/24', '程序标签组合'], ['v3本地POST503', '原0/6，恢复6/6', '两条旧命令'], ['v3双POST503', '均0/6', '失败不执行']], 'lines': ['H05合同拒绝保留，历史600/120/36分别统计'], 'notes': '封存索引与race_v3_portfix。合成收益不等于真实模型联合优势。'},
        {'id': 'deployment', 'title': '复制部署与固定负载', 'lines': ['新目录启动和哈希核验', '完整模型版隔离网关与需求采集', '固定请求率和故障序列保留资源日志', '自动化墙钟与真人工时分别记录'], 'foot': '尚无独立主机及真人独立部署工时', 'notes': str(cfg.get('operations_results'))},
        {'id': 'value', 'title': '价值通过实际任务验收', 'table': [['要证明的价值', '验收记录'], ['减少中断', '同任务完成与人工重试'], ['降低等待', '全请求墙钟与尾部'], ['便于维护', '错误定位和部署工时'], ['成本可接受', '采购、整机能耗、网络和维护']], 'foot': '外部反馈0；实际总成本null；未宣称收入或节费比例', 'notes': '真实参与者记录稍后补充，未采集不计。'},
        {'id': 'team', 'title': '实施与事实保障', 'lines': ['当前单人实现，不虚构成员与合作', '执行前注册，原始失败保留', '同一主张表生成方案答辩佐证', '第三方许可与生成式AI使用如实说明'], 'notes': '源码与compliance，生成式AI辅助代码测试文档脚本，最终参赛者负责核对。'},
        {'id': 'roadmap', 'title': '下一步验收条件', 'lines': ['真实授权访谈和用户自拟任务', '低风险物理设备与独立主机', '真实网络与实际成本凭据', '永久材料链接和最终人工上传核对'], 'foot': '本版交付为软件原型与设计验证；各层证据边界明确', 'notes': '官方场景设计赛道允许面向实际需求的应用场景设计方案。'},
    ]
    return {'plan': plan, 'evidence': evidence, 'slides': slides, 'abstract': abstract,
            'baseline': baseline, 'integration': integration, 'operations': operations, 'video': video,
            'criteria': criteria, 'recommendation': recommendation}


def word_font(run, size=11, bold=False):
    run.font.name = FONT
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = RGBColor(0, 0, 0)
    run._element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), FONT)


def make_docx(pages, target):
    d = Document()
    s = d.sections[0]
    s.page_width, s.page_height = Cm(21), Cm(29.7)
    s.top_margin, s.bottom_margin = Cm(2), Cm(1.9)
    s.left_margin, s.right_margin = Cm(2.4), Cm(2.4)
    s.header_distance, s.footer_distance = Cm(.8), Cm(.8)
    for key, size in [('Normal', 10.5), ('Title', 23), ('Heading 1', 18), ('Heading 2', 12.5)]:
        st = d.styles[key]
        st.font.name, st.font.size = FONT, Pt(size)
        st.font.color.rgb = RGBColor(0, 0, 0)
        st._element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), FONT)
        st.paragraph_format.space_before = Pt(0)
        st.paragraph_format.space_after = Pt(8)
        st.paragraph_format.line_spacing = 1.3
    for st in d.styles:
        for border in st.element.xpath('.//w:pBdr'):
            border.getparent().remove(border)
    s.header.paragraphs[0].text = 'SenseHome  场景设计方案  v4'
    for r in s.header.paragraphs[0].runs:
        word_font(r, 8)
    footer = s.footer.paragraphs[0]
    footer.alignment = 2
    field = OxmlElement('w:fldSimple')
    field.set(qn('w:instr'), 'PAGE')
    footer._p.append(field)
    for index, pg in enumerate(pages):
        if index:
            d.add_page_break()
        d.add_paragraph(pg['title'], 'Title' if index == 0 else 'Heading 1')
        for b in pg['blocks']:
            if b['kind'] == 'p':
                d.add_paragraph(b['text'])
            elif b['kind'] == 'h2':
                d.add_paragraph(b['text'], 'Heading 2')
            elif b['kind'] == 'image':
                d.add_picture(str(ROOT/b['path']), width=Cm(16))
                cap=d.add_paragraph(b['caption']); cap.alignment=1
                for r in cap.runs: word_font(r,9)
            else:
                tab = d.add_table(rows=1, cols=len(b['headers']))
                tab.autofit = False
                widths = b.get('widths') or [158 / len(b['headers'])] * len(b['headers'])
                widths = [v * 16.2 / sum(widths) for v in widths]
                for i, value in enumerate(b['headers']):
                    tab.rows[0].cells[i].text = value
                for row in b['rows']:
                    cells = tab.add_row().cells
                    for i, value in enumerate(row):
                        cells[i].text = value
                for ri, row in enumerate(tab.rows):
                    trpr = row._tr.get_or_add_trPr()
                    cant = OxmlElement('w:cantSplit'); trpr.append(cant)
                    if ri == 0:
                        repeat = OxmlElement('w:tblHeader'); trpr.append(repeat)
                    for ci, cell in enumerate(row.cells):
                        cell.width = Cm(widths[ci])
                        tcpr = cell._tc.get_or_add_tcPr()
                        sh = OxmlElement('w:shd'); sh.set(qn('w:fill'), 'E8EFEB' if ri == 0 else ('FFFFFF' if ri % 2 else 'F6F8F6')); tcpr.append(sh)
                        borders = OxmlElement('w:tcBorders')
                        for edge in ['top', 'left', 'bottom', 'right']:
                            el = OxmlElement('w:' + edge)
                            for k, v in [('val', 'single'), ('sz', '4'), ('color', 'D9D9D9')]: el.set(qn('w:' + k), v)
                            borders.append(el)
                        tcpr.append(borders)
                        marg = OxmlElement('w:tcMar')
                        for edge in ['top', 'left', 'bottom', 'right']:
                            el = OxmlElement('w:' + edge); el.set(qn('w:w'), '80'); el.set(qn('w:type'), 'dxa'); marg.append(el)
                        tcpr.append(marg)
                        for par in cell.paragraphs:
                            par.paragraph_format.line_spacing = 1.1
                            par.paragraph_format.space_after = Pt(3)
                            for r in par.runs: word_font(r, 9.3, ri == 0)
                d.add_paragraph('').paragraph_format.space_after = Pt(0)
    d.core_properties.title = NAME
    d.core_properties.author = ''
    d.core_properties.last_modified_by = ''
    d.save(target)


def slide_text(slide, value, x, y, w, height, size=23, bold=False, color=INK):
    shape = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(height))
    tf = shape.text_frame; tf.word_wrap = True
    tf.margin_left = tf.margin_right = 0
    tf.margin_top = tf.margin_bottom = 0
    tf.vertical_anchor = MSO_ANCHOR.TOP
    for i, line in enumerate(value.split('\n')):
        par = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        par.text = line
        par.font.name = FONT; par.font.size = PPTPt(size)
        ea = par.font._rPr.find('{http://schemas.openxmlformats.org/drawingml/2006/main}ea')
        if ea is None: ea = etree.SubElement(par.font._rPr, '{http://schemas.openxmlformats.org/drawingml/2006/main}ea')
        ea.set('typeface', FONT)
        par.font.bold = bold; par.font.color.rgb = PPTColor.from_string(color)
        par.space_after = PPTPt(8)
    return shape


def ppt_table(slide, rows, y, height=None, font_size=19):
    if height is None: height = .64 * len(rows)
    shape = slide.shapes.add_table(len(rows), len(rows[0]), Inches(.75), Inches(y), Inches(11.8), Inches(height))
    tab = shape.table
    for ri, row in enumerate(rows):
        for ci, value in enumerate(row):
            cell = tab.cell(ri, ci)
            cell.text = str(value)
            cell.margin_left = cell.margin_right = Inches(.12)
            cell.margin_top = cell.margin_bottom = Inches(.07)
            cell.fill.solid(); cell.fill.fore_color.rgb = PPTColor.from_string('E8EFEB' if ri == 0 else ('FFFFFF' if ri % 2 else 'F7F8F6'))
            for p0 in cell.text_frame.paragraphs:
                p0.font.name = FONT; p0.font.size = PPTPt(font_size)
                ea = p0.font._rPr.find('{http://schemas.openxmlformats.org/drawingml/2006/main}ea')
                if ea is None: ea = etree.SubElement(p0.font._rPr, '{http://schemas.openxmlformats.org/drawingml/2006/main}ea')
                ea.set('typeface', FONT)
                p0.font.bold = ri == 0; p0.font.color.rgb = PPTColor.from_string(INK)
    return shape


def make_pptx(slides, target):
    prs = Presentation(); prs.slide_width = Inches(13.333); prs.slide_height = Inches(7.5)
    for index, data in enumerate(slides):
        s = prs.slides.add_slide(prs.slide_layouts[6])
        s.background.fill.solid(); s.background.fill.fore_color.rgb = PPTColor.from_string('FFFFFF')
        slide_text(s, data['title'], .75, .48, 11.85, .9, 31 if index else 34, True)
        if index == 0:
            slide_text(s, data['lead'], .75, 2.15, 11.8, 1, 31, False, ACCENT)
            slide_text(s, '\n'.join(data['lines']), .75, 4.15, 11.8, 1.5, 24)
        elif data.get('flow'):
            # A requested editable process diagram, kept as native slide text.
            entries = [('01  任务分类', '支持的规则直接处理'), ('02  选择与规划', '同款恢复最多一次'), ('03  动作合同', '整项校验后提交'), ('04  状态核验', '确认 部分 未知')]
            for i, (label, detail) in enumerate(entries):
                y = 1.6 + i * 1.1
                slide_text(s, label, .95, y, 3.5, .5, 25, True, ACCENT)
                slide_text(s, detail, 4.8, y, 7.2, .65, 24)
            slide_text(s, '统一事件贯通初选 实际模型 动作和回执', .95, 6.2, 11.5, .55, 21, False, GRAY)
        else:
            y = 1.7
            if data.get('lead'):
                slide_text(s, data['lead'], .75, y, 11.8, .8, 27, False, ACCENT); y += 1.05
            if data.get('table'):
                rows = data['table']; height = .58 * len(rows)
                ppt_table(s, rows, y, height, 18.5 if len(rows) > 5 else 20)
                y += height + .38
            for line in data.get('lines', []):
                slide_text(s, line, .85, y, 11.65, .8, 23 if not data.get('table') else 19.5); y += .94 if not data.get('table') else .68
        slide_text(s, data.get('foot', 'SenseHome  场景设计  '+DATE+'  v4'), .75, 7.0, 11.7, .28, 11, False, GRAY)
        slide_text(s, f'{index+1:02}', 12.45, 7, .4, .28, 11, False, GRAY)
        s.notes_slide.notes_text_frame.text = data.get('notes', '')
    prs.core_properties.title = NAME
    prs.core_properties.author = ''; prs.core_properties.last_modified_by = ''
    prs.save(target)


def make_pdf(pages, target, label):
    pdfmetrics.registerFont(TTFont('V4CN', 'C:/Windows/Fonts/simhei.ttf'))
    pdfmetrics.registerFontFamily('V4CN', normal='V4CN', bold='V4CN', italic='V4CN', boldItalic='V4CN')
    styles = {key: ParagraphStyle(key, fontName='V4CN', fontSize=size, leading=leading, wordWrap='CJK',
               textColor=colors.black, spaceAfter=after, keepWithNext=key in ('h1', 'h2'))
              for key, size, leading, after in [('h1', 21, 28, 17), ('h2', 12.5, 19, 9), ('p', 10.5, 17.5, 11), ('cell', 9, 14.2, 0), ('head', 9, 14.2, 0)]}
    def P(value, key='p'):
        return Paragraph(escape(str(value)).replace('\n', '<br/>'), styles[key])
    story = []
    for index, pg in enumerate(pages):
        if index: story.append(PageBreak())
        story.append(P(pg['title'], 'h1'))
        for b in pg['blocks']:
            if b['kind'] in ('p', 'h2'):
                story.append(P(b['text'], b['kind']))
            elif b['kind'] == 'image':
                story.extend([Image(str(ROOT/b['path']), width=162*mm, height=75*mm),P(b['caption'])])
            else:
                rows = [b['headers']] + b['rows']
                widths = b.get('widths') or [158 / len(rows[0])] * len(rows[0])
                cw = [v * 162 / sum(widths) * mm for v in widths]
                tab = Table([[P(v, 'head' if ri == 0 else 'cell') for v in row] for ri, row in enumerate(rows)], colWidths=cw, repeatRows=1, hAlign='LEFT')
                tab.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#E8EFEB')),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F6F8F6')]),
                    ('GRID', (0, 0), (-1, -1), .35, colors.HexColor('#D9D9D9')), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                    ('LEFTPADDING', (0, 0), (-1, -1), 6), ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                    ('TOPPADDING', (0, 0), (-1, -1), 7), ('BOTTOMPADDING', (0, 0), (-1, -1), 7)]))
                story.extend([tab, Spacer(1, 12)])
    def footer(c, doc):
        c.setTitle(NAME+' '+label); c.setAuthor('')
        c.setFont('V4CN', 8); c.setFillColor(colors.HexColor('#55636A'))
        c.drawString(24*mm, 13*mm, 'SenseHome  '+label+'  '+DATE+'  v4')
        c.drawRightString(186*mm, 13*mm, str(doc.page))
    SimpleDocTemplate(str(target), pagesize=A4, leftMargin=24*mm, rightMargin=24*mm,
                      topMargin=22*mm, bottomMargin=24*mm).build(story, onFirstPage=footer, onLaterPages=footer)


def run(args):
    result = subprocess.run([str(x) for x in args], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError(result.stdout + '\n' + result.stderr)
    return result.stdout


def convert(path):
    profile = WORK / ('lo_' + path.suffix[1:])
    profile.mkdir(parents=True, exist_ok=True)
    run([SOFFICE, '-env:UserInstallation=' + profile.as_uri(), '--headless', '--convert-to', 'pdf', '--outdir', path.parent, path])
    pdf = path.with_suffix('.pdf')
    if not pdf.is_file(): raise RuntimeError('LibreOffice did not create PDF')
    return pdf


def render(path, label):
    dest = WORK / label
    dest.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(path)
    for index, pg in enumerate(doc):
        pg.get_pixmap(matrix=fitz.Matrix(1.3, 1.3), alpha=False).save(dest / f'page-{index+1:02}.png')
    return {'pages': len(doc), 'size_bytes': path.stat().st_size, 'sha256': sha(path), 'png_dir': rel(dest),
            'page_text_chars': [len(pg.get_text()) for pg in doc]}


def markdown(pages):
    out = []
    for pg in pages:
        out.append('# ' + pg['title'])
        for b in pg['blocks']:
            if b['kind'] == 'p': out.append(b['text'])
            elif b['kind'] == 'h2': out.append('## ' + b['text'])
            elif b['kind'] == 'image': out.append('!['+b['caption']+']('+b['path']+')')
            else:
                out.append('| ' + ' | '.join(b['headers']) + ' |')
                out.append('| ' + ' | '.join(['---'] * len(b['headers'])) + ' |')
                out.extend('| ' + ' | '.join(row) + ' |' for row in b['rows'])
    return '\n\n'.join(out) + '\n'


def page_map(pdf, pages):
    doc = fitz.open(pdf)
    result = {}
    for pg in pages:
        title = re.sub(r'\s+', '', pg['title'])
        hits = []
        for i, actual in enumerate(doc):
            # LibreOffice may split fallback CJK glyphs into reordered spans.
            # Check the heading region as a character bag as a second check.
            heading = re.sub(r'\s+', '', actual.get_text(clip=fitz.Rect(0,25,actual.rect.width,140),sort=True))
            if title in heading or not (Counter(title)-Counter(heading)):
                hits.append(i+1)
        if not hits: raise AssertionError('Missing page title: ' + pg['title'])
        result[pg['id']] = hits[0]
    return result


def write_indexes(data, cfg, maps):
    plan_map, slide_map, evidence_map = maps
    claims = [
        ('C01', '复杂指令有受约束的一次规划恢复', '完整动作且软件目标状态满足 / 非空请求', '按六条件与三臂分列', 'baseline', 'conditions', cfg.get('baseline_summary')),
        ('C02', '三臂仅改变初选且共用恢复', '配置、源码、任务与顺序摘要一致', '注册与逐行复算', 'baseline', 'registration', cfg.get('baseline_registration')),
        ('C03', '调用与总时延同时报告', '全请求墙钟与代理/模型POST', '本轮分段与调用表', 'timing', 'timing', cfg.get('baseline_rows')),
        ('C04', '正式执行通道可持久对账', 'ID/内容/身份绑定，逐动作状态', '以锁定MQTT结果为准', 'receipt', 'integration', cfg.get('integration_summary')),
        ('C05', 'MQTT为真实软件协议实体', '消息、ACK、状态与数据库事件', '无物理执行证据', 'receipt', 'mqtt', cfg.get('integration_events')),
        ('C06', '历史250ms目标未达', '成功且≤250ms / v3成功请求18', '0/18，最短841.612ms', 'kpi', 'v3', 'docs/experiments/20261111_race_v3_portfix/rows.jsonl'),
        ('C07', '旧留出和失败完整保留', '非空完成 / 各臂24', 'joint21/24；第二端24/24；H05被拒', 'history', 'history', 'docs/experiments/20261111_fused_real/sealed_evidence_index.json'),
        ('C08', '自动部署与工时分开', '自动化墙钟、请求、资源、清理', '非真人独立部署', 'deployment', 'operations', cfg.get('operations_results')),
        ('C09', '真实外部反馈仍为0', '有授权并保存实际任务和回答的人数', '0；待真实记录', 'demand', 'trial', 'docs/competition_v4/results_inputs.json'),
        ('C10', '实际总成本未知', '硬件、电力、调用、维护与网络总额', 'null', 'value', 'trial', 'docs/value_v2/results/ledger_current_result.json'),
        ('C11', 'AI与6G为应用侧接口衔接', '来源、单位、时间、年龄、known', '无6G空口/独立端云实测', 'ai6g', 'public', 'docs/value/public_sources.json'),
        ('C12', '同版正式材料可追溯', '主张、指标、分母、环境、来源与页码', '当前索引', 'roadmap', 'sources', 'docs/competition_v4/claims_index.json'),
    ]
    output = []
    for id, claim, metric, value, pi, ei, path in claims:
        output.append({'id': id, 'claim': claim, 'metric_definition': metric, 'value_and_denominator': value,
                       'environment': '同机共享GPU；受控软件代理；JSON/MQTT软件状态；无真实6G或实物',
                       'raw_path': path, 'plan_pdf_page': plan_map[pi], 'defense_pdf_page': slide_map.get('timing' if id == 'C06' else pi),
                       'evidence_pdf_page': evidence_map[ei], 'evidence_status': '已锁定' if path and ((ROOT/path).is_file() or id == 'C12') else '待锁定'})
    (CONTENT / 'claims_index.json').write_text(json.dumps({'schema': 'sensehome.claim-index.v4', 'claims': output}, ensure_ascii=False, indent=2), encoding='utf-8')
    rows = [[c['id'], c['claim'], c['metric_definition'], c['value_and_denominator'], str(c['plan_pdf_page']), str(c['defense_pdf_page'] or '—'), str(c['evidence_pdf_page']), str(c['raw_path'] or '待锁定')] for c in output]
    index_pages = [page('index', '主张指标证据页码索引', p('页码为同版最终PDF实际页序，含首页。环境统一为同机共享GPU及软件状态，特殊边界在主张中说明。'), t(['ID', '主张', '指标定义', '数值与分母', '方案', '答辩', '佐证', '原始入口'], rows))]
    (CONTENT / '主张指标证据页码索引.md').write_text(markdown(index_pages), encoding='utf-8')
    (OUT / '主张指标证据页码索引.md').write_text(markdown(index_pages), encoding='utf-8')
    return output


def collect_sources(cfg):
    paths = ['docs/competition_v3/criteria.json', '附件1-作品提交要求.pdf', '附件2-作品方案大纲与评分标准.pdf',
             'docs/execution/2026-11-11-evidence-v3.md', 'docs/experiments/20261111_fused_real/sealed_evidence_index.json',
             'docs/experiments/20261111_real_fault_v2/summary.json', 'docs/experiments/20261111_race_v3_portfix/summary.json',
             'docs/experiments/20261111_race_v3_portfix/rows.jsonl', 'docs/value/public_sources.json',
             'docs/value_v2/results/ledger_current_result.json', 'docs/competition_v4/results_inputs.json',
             'docs/submission/build_submission_v4.py', 'docs/experiments_v4/post_analysis.json',
             'docs/experiments_v4/recommendation.json', 'docs/experiments_v4/final_manifest.json',
             'docs/operations_v4/registration.json', 'docs/operations_v4/rows.jsonl',
             'docs/operations_v4/resource_samples.json', 'docs/integration_v4/LATEST.json',
             'docs/integration_v4/verification/final_verification.json',
             'docs/reviews/2026-11-11-v4-independent-review.md',
             'docs/reviews/2026-11-11-v4-independent-review.json',
             'docs/video/v4/metadata_correction.json', 'docs/video/v4/visual_review.json']
    paths.extend(value for key, value in cfg.items() if key.endswith(('_summary', '_verification', '_rows', '_registration', '_results', '_manifest', '_events')) and isinstance(value, str))
    video = source(cfg.get('video_manifest'))
    if video:
        paths.extend(video.get('source_sha256', {}).keys())
    return [{'path': path, 'sha256': sha(ROOT/path), 'bytes': (ROOT/path).stat().st_size} for path in dict.fromkeys(paths) if (ROOT/path).is_file()]


def inspect_video(cfg, manifest):
    if not manifest:
        return {'passed': None, 'evidence': cfg.get('video_manifest'), 'note': '当前视频清单未锁定'}
    path = manifest.get('video_path') or manifest.get('path') or manifest.get('output_path')
    if not path or not (ROOT/path).is_file():
        return {'passed': False, 'evidence': cfg.get('video_manifest'), 'note': '清单未指向可读视频'}
    probe_tool = shutil.which('ffprobe') or 'C:/Program Files/EVCapture/ffprobe.exe'
    if not Path(probe_tool).is_file():
        raise FileNotFoundError('ffprobe is required for final video validation')
    probe = json.loads(run([probe_tool,'-v','error','-show_format','-show_streams','-of','json',ROOT/path]))
    duration=float(probe['format']['duration']);size=(ROOT/path).stat().st_size
    actual_sha=sha(ROOT/path)
    metadata_matches=(manifest.get('sha256')==actual_sha and manifest.get('size_bytes')==size
                      and abs(float(manifest.get('duration_seconds', -1))-duration)<0.02)
    passed=Path(path).suffix.lower()=='.mp4' and 180 <= duration <= 300 and size <= 300*1024*1024 and metadata_matches
    return {'passed':passed,'evidence':cfg.get('video_manifest'),'video_path':path,
            'duration_seconds':duration,'size_bytes':size,'sha256':actual_sha,'metadata_matches_actual_file':metadata_matches,
            'recording_kind':manifest.get('recording_kind'),'editing_boundaries':manifest.get('editing_boundaries'),
            'synthetic_voice':manifest.get('synthetic_voice'),'runtime_metadata_correction':'docs/video/v4/metadata_correction.json',
            'claim_boundary':'时长与格式核验；实际操作和剪辑来源另按视频清单核查'}


def make_architecture():
    """Required technical architecture diagram, never a decorative asset."""
    pdfmetrics.registerFont(TTFont('V4Diagram', 'C:/Windows/Fonts/simhei.ttf'))
    f=WORK/'architecture.pdf'; c=canvas.Canvas(str(f),pagesize=(980,440))
    def box(x,y,w,height,title,detail):
        c.setFillColor(colors.HexColor('#F3F6F3')); c.setStrokeColor(colors.HexColor('#9CAEA4'))
        c.roundRect(x,y,w,height,5,fill=1,stroke=1)
        c.setFillColor(colors.HexColor('#172D3A')); c.setFont('V4Diagram',18)
        c.drawCentredString(x+w/2,y+height-29,title)
        c.setFont('V4Diagram',14)
        for i,line in enumerate(detail.split('\n')): c.drawCentredString(x+w/2,y+height-55-i*21,line)
    def line(x1,y1,x2,y2):
        c.setStrokeColor(colors.HexColor('#287366')); c.setLineWidth(2); c.line(x1,y1,x2,y2)
        c.setFillColor(colors.HexColor('#287366')); p0=c.beginPath();p0.moveTo(x2,y2)
        if x1==x2: p0.lineTo(x2-5,y2+9);p0.lineTo(x2+5,y2+9)
        else: p0.lineTo(x2-9,y2-5);p0.lineTo(x2-9,y2+5)
        p0.close();c.drawPath(p0,fill=1,stroke=0)
    box(10,175,170,100,'命令入口','规则与复杂任务\n状态和原因展示')
    box(235,160,245,135,'统一网关','观测适配与初选\n最多一次规划恢复\n动作白名单与事件')
    box(540,285,205,100,'本地模型候选','Qwen2.5 1.5B\n实际GGUF推理')
    box(770,285,200,100,'第二模型候选','Qwen3 4B\n同机共享GPU')
    box(540,100,205,115,'持久命令与回执','SQLite命令身份\n提交 回查 逐项状态')
    box(770,100,200,115,'MQTT软件实体','真实本机MQTT\n软件ACK与状态')
    box(235,10,245,90,'初选与观测','C策略或简单优先\n来源 年龄 known')
    line(180,225,235,225);line(480,260,540,325)
    c.setStrokeColor(colors.HexColor('#287366'));c.setLineWidth(2)
    c.line(480,280,510,410);c.line(510,410,870,410);line(870,410,870,385)
    line(480,185,540,155);line(745,155,770,155)
    line(355,160,355,100)
    c.setFont('V4Diagram',14);c.setFillColor(colors.HexColor('#55636A'))
    c.drawString(540,45,'软件协议与状态核验 不证明物理设备动作')
    c.save();d=fitz.open(f);d[0].get_pixmap(matrix=fitz.Matrix(1.8,1.8),alpha=False).save(WORK/'architecture.png')


def scan_anonymous(files):
    findings = []
    bad = [r'C:[\\/]+Users[\\/]', r'郑傅韬', r'大学生活', r'华为openjiuwen', r'指导教师[：:]\s*\S+', r'sshpass', r'password\s*[=:]\s*[^n\s][^\s]*', r'1[3-9]\d{9}', r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}']
    for file in files:
        if file.suffix == '.pdf':
            text = '\n'.join(pg.get_text() for pg in fitz.open(file))
        elif file.suffix in ('.docx', '.pptx'):
            with zipfile.ZipFile(file) as z:
                text = '\n'.join(z.read(name).decode('utf-8', errors='replace') for name in z.namelist() if name.endswith('.xml'))
        else: text = file.read_text(encoding='utf-8')
        for pattern in bad:
            if re.search(pattern, text, re.I): findings.append({'file': file.name, 'pattern': pattern})
    if findings: raise AssertionError('Anonymous scan failed: ' + json.dumps(findings, ensure_ascii=False))
    return {'passed': True, 'files': [f.name for f in files], 'patterns_checked': len(bad)}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--content-only', action='store_true'); args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True); WORK.mkdir(parents=True, exist_ok=True); CONTENT.mkdir(parents=True, exist_ok=True)
    cfg = load(INPUTS); data = build_content(cfg)
    (CONTENT/'方案正文.md').write_text(markdown(data['plan']), encoding='utf-8')
    (CONTENT/'佐证正文.md').write_text(markdown(data['evidence']), encoding='utf-8')
    (CONTENT/'答辩讲稿.md').write_text('\n\n'.join(f"## {i+1} {s['title']}\n\n{s.get('notes','')}" for i,s in enumerate(data['slides'])), encoding='utf-8')
    (OUT/'作品简介.txt').write_text(data['abstract']+'\n', encoding='utf-8')
    (CONTENT/'content_model.json').write_text(json.dumps({k:v for k,v in data.items() if k in ('plan','evidence','slides','abstract')}, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.content_only:
        print(json.dumps({'abstract_chars': len(data['abstract']), 'plan_sections': len(data['plan']), 'slides': len(data['slides']), 'evidence_sections': len(data['evidence'])})); return
    docx = OUT/'SenseHome_场景设计方案_20261111_v4.docx'
    pptx = OUT/'SenseHome_场景设计答辩_20261111_v4.pptx'
    make_architecture();make_docx(data['plan'], docx); make_pptx(data['slides'], pptx)
    plan_pdf = convert(docx); deck_pdf = convert(pptx)
    evidence_pdf = OUT/'SenseHome_合并佐证材料_20261111_v4.pdf'
    make_pdf(data['evidence'], evidence_pdf, '佐证材料')
    maps = (page_map(plan_pdf, data['plan']), {s['id']: i+1 for i,s in enumerate(data['slides'])}, page_map(evidence_pdf, data['evidence']))
    claims = write_indexes(data, cfg, maps)
    qa = {f.name: render(f, label) for f,label in [(plan_pdf,'plan'),(deck_pdf,'slides'),(evidence_pdf,'evidence')]}
    video_check = inspect_video(cfg, data['video'])
    checks = {'schema': 'sensehome.submission-checks.v4', 'version': VERSION, 'title': NAME, 'name_chars': len(NAME),
              'abstract_chars': len(data['abstract']), 'plan_pdf_bytes': plan_pdf.stat().st_size,
              'checks': [{'item':'作品名称≤20字','passed':len(NAME)<=20}, {'item':'简介300—500字','passed':300<=len(data['abstract'])<=500},
                         {'item':'方案PDF≤15MB','passed':plan_pdf.stat().st_size<=15*1024*1024},
                         {'item':'答辩PDF','passed':deck_pdf.is_file()}, {'item':'单一合并佐证PDF','passed':evidence_pdf.is_file()},
                         {'item':'视频3—5分钟≤300MB',**video_check},
                         {'item':'其他材料永久分享链接与提取码','passed':bool(cfg.get('permanent_share_url') and cfg.get('extraction_code'))},
                         {'item':'真人需求/独立部署/物理设备','passed':False,'note':'事实缺项；非所有六项均已完成'}],
              'external_feedback_count':0,'actual_total_cost':None,'final_human_review_and_upload':'pending'}
    final_files = [docx,pptx,plan_pdf,deck_pdf,evidence_pdf,OUT/'作品简介.txt']
    checks['anonymous_scan'] = scan_anonymous(final_files)
    output_files = final_files + ([ROOT/video_check['video_path']] if video_check.get('video_path') else [])
    (OUT/'提交检查表.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
    check_lines = ['# v4 提交检查表', '', f'作品名称 {NAME}（{len(NAME)}字）；简介{len(data["abstract"])}字。', '', '| 项目 | 结果 |', '|---|---|']
    for c in checks['checks']: check_lines.append('| '+c['item']+' | '+('已核验' if c['passed'] else ('待核验' if c['passed'] is None else '待完成'))+' |')
    check_lines += ['', '真实外部反馈0；实际总成本null；当前无独立主机、真实6G空口或实物完成。最终人工核对、永久链接和上传尚未完成。', '', '所有PDF已生成逐页PNG供视觉审查；审查记录以material_qa.json为准。']
    (OUT/'提交检查表.md').write_text('\n'.join(check_lines)+'\n',encoding='utf-8')
    manifest = {'schema':'sensehome.material-manifest.v4','version':VERSION,'created_at_utc':datetime.now(timezone.utc).isoformat(),
                'inputs':cfg,'sources':collect_sources(cfg),'outputs':[{'path':rel(f),'bytes':f.stat().st_size,'sha256':sha(f)} for f in output_files],
                'authoring':'Fallback: existing local python-docx/python-pptx/reportlab and LibreOffice; load_workspace_dependencies unavailable in enabled tool catalog.',
                'skills_read':['documents','pdf','presentations'],'qa':qa,'visual_review':'pending','claims':len(claims)}
    (OUT/'source_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    (WORK/'material_qa.json').write_text(json.dumps({'qa':qa,'anonymous_scan':checks['anonymous_scan'],'visual_review':'pending'},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'qa':qa,'abstract_chars':len(data['abstract']),'claims':len(claims)},ensure_ascii=True,indent=2))


if __name__ == '__main__':
    main()
