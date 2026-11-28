"""Dynamic auditable checklist engine, household rules, and layered edge guardrails.

Cloud agents (e.g., Huawei Cloud AgentArts) may propose structured checklists and
actions based on natural language and household knowledge bases. This module
enforces deterministic edge guardrails before any proposal card is minted:

1. Local privacy requests never accept cloud-originated candidates.
2. Explicit negations and room/device exclusions in the user's original utterance
   are deterministically verified and cannot be bypassed by cloud candidates.
3. Read-only observations (e.g., door contact, temperature) are always refreshed
   from the local inventory rather than trusted from cloud prose.
4. All watched controllable entities in scope (including initially-off skipped
   lights) are bound as proposal state dependencies so pre-confirm state drift
   invalidates stale proposals.
5. Care advisories cite structured SOP rules and remain strictly advisory; local
   CareStore state machines remain the authoritative escalation engine.
"""
from __future__ import annotations

import copy
import re
import time
import unicodedata
from typing import Any

from catalog_planner import request_privacy_mode


HOUSEHOLD_RULES: list[dict[str, Any]] = [
    {
        'rule_id': 'RULE-001',
        'title': '日常离家灯光检查范围',
        'category': 'leave_home',
        'version': '2026.11',
        'scope': ['书房', '阳台', '厨房', '卫生间', '卧室', '门口', '客厅'],
        'summary': '离家检查默认扫描全屋已授权灯光，仅对当前处于开启(on)状态的灯生成关灯动作；已关闭(off)的灯自动跳过并纳入确认前状态依赖监控。',
        'exceptions': ['当有家庭成员或室友留在某房间，或用户口语明确排除某房间/设备时，按 RULE-002 保留。'],
    },
    {
        'rule_id': 'RULE-002',
        'title': '客厅有人与成员保留例外',
        'category': 'leave_home',
        'version': '2026.11',
        'scope': ['客厅'],
        'summary': '当指令提及室友/家人在客厅，或明确要求“客厅别动/不要动某房间”时，保留该房间内所有灯光与空调，严禁列入拟执行动作。',
        'exceptions': ['用户显式撤销保留声明并发出直接控制指令时除外。'],
    },
    {
        'rule_id': 'RULE-003',
        'title': '入户门磁与环境传感器只读核查',
        'category': 'inspection',
        'version': '2026.11',
        'scope': ['门口', '客厅'],
        'summary': '离家检查默认附带读取入户门磁(binary_sensor.front_door)实时状态并标注采样时间；离线或状态未知时必须提示无法核实，绝不生成门磁/摄像头控制动作。',
        'exceptions': ['无例外；只读实体永久禁止写操作。'],
    },
    {
        'rule_id': 'RULE-004',
        'title': '书房阅读场景与房间澄清约定',
        'category': 'scene',
        'version': '2026.11',
        'scope': ['书房'],
        'summary': '明确指定在书房阅读/看书时，可将书房阅读灯设为 40% 亮度并拉开书房窗帘；未指明房间时必须发起追问，禁止猜测房间。',
        'exceptions': ['设备离线或处于不可控模式时中止提案并提示原因。'],
    },
    {
        'rule_id': 'RULE-005',
        'title': '否定句式与越权动作硬红线',
        'category': 'safety',
        'version': '2026.11',
        'scope': ['全屋'],
        'summary': '包含“不要/别/禁止/取消”对某设备或房间的否定约束时，边缘护栏强制拦截冲突候选动作；云端 Agent 永不拥有 confirm 或直接执行权限。',
        'exceptions': ['无例外；边缘护栏硬约束高于一切云端提案。'],
    },
    {
        'rule_id': 'RULE-006',
        'title': '隐私分层本地旁路策略',
        'category': 'privacy',
        'version': '2026.11',
        'scope': ['全屋'],
        'summary': '包含“不要上传/仅在本地/不要联网/离线运行”的指令由边缘直接旁路处理，不向云端发送文本、设备目录或状态快照；CSI 原始 IQ 永远留在本地。',
        'exceptions': ['无例外。'],
    },
]

CARE_SOPS: list[dict[str, Any]] = [
    {
        'rule_id': 'SOP-001',
        'title': 'SOS 求助触发与初始分级通知',
        'category': 'care',
        'version': '2026.11',
        'summary': '收到居家 SOS 触发后，本地 CareStore 立即原子持久化事件并投递初始通知至家属收件箱，启动 30 秒待确认倒计时；云端协同 Agent 仅可只读拉取事件上下文生成摘要。',
        'actions': ['核对求助房间位置与触发时间', '立即尝试语音/电话联系居家老人', '同步核查该房间灯光与入户门磁状态'],
    },
    {
        'rule_id': 'SOP-002',
        'title': '超时未确认自动升级处置',
        'category': 'care',
        'version': '2026.11',
        'summary': '超过确认截止时间仍未收到家属确认时，本地状态机自动原子升级为 escalated 并投递二次升级通知（仅升级一次，服务重启后持续生效）。',
        'actions': ['立即联系第二紧急联系人或物业/社区值班人员', '核对入户门磁开闭及非侵入式 CSI 运动状态', '必要时直接拨打 120/110 急救热线'],
    },
    {
        'rule_id': 'SOP-003',
        'title': '家属确认闭环与安全免责边界',
        'category': 'care',
        'version': '2026.11',
        'summary': '家属完成确认(acked)后停止后续超时升级并保留完整时间线审计记录。系统研判摘要仅作辅助参考，不替代专业医疗诊断或紧急救援通信。',
        'actions': ['记录现场核实结果与后续跟进事项', '确认居家环境安全后恢复常态监测'],
    },
]

_LEAVE_KEYWORDS = ('出门', '离家', '离开家', '出去了', '外出了', '离开一下', '出发了')
_RULE_HINTS = ('平时', '约定', '习惯', '惯例', '规矩', '离家约定', '规则')
_DOOR_KEYWORDS = ('入户门', '大门', '门磁', '门关', '锁门', '门开', '门口')
_INSPECT_KEYWORDS = ('巡检', '检查一下全屋', '全屋状态', '看看家里', '检查门窗', '看看门')
_CARE_KEYWORDS = ('sos', 'SOS', '照护', '求助', '应急预案', '老人求助')
_PRESERVE_SUFFIXES = r'(?:别动|不要动|不用动|别关|不要关|不用关|保留|留着|不用管|先别关|别碰)'
_DIRECT_NEGATION_PREFIXES = ('不要打开', '别打开', '不要开', '别开', '不要关闭', '别关闭', '不要关', '别关', '禁止打开', '禁止关闭')


def _norm(text: str) -> str:
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text or ''))


def get_rules_catalog(query: str | None = None, category: str | None = None) -> dict[str, Any]:
    """Return structured household rules and care SOPs with optional filtering."""
    all_rules = [*HOUSEHOLD_RULES, *CARE_SOPS]
    results = []
    q = _norm(query).casefold() if query else ''
    cat = (category or '').strip().casefold()
    for item in all_rules:
        if cat and item['category'].casefold() != cat:
            continue
        if q:
            haystack = _norm(' '.join([
                item['rule_id'], item['title'], item['category'], item['summary'],
                ' '.join(item.get('scope', [])),
                ' '.join(item.get('exceptions', [])),
                ' '.join(item.get('actions', [])),
            ])).casefold()
            if q not in haystack:
                continue
        results.append(copy.deepcopy(item))
    return {
        'rules': results,
        'total': len(results),
        'household_count': sum(1 for r in results if r['rule_id'].startswith('RULE-')),
        'care_sop_count': sum(1 for r in results if r['rule_id'].startswith('SOP-')),
    }


def extract_exclusions(text: str, devices: list[dict[str, Any]]) -> tuple[set[str], set[str], list[str]]:
    """Deterministically extract excluded rooms and entities from the original utterance."""
    normalized = _norm(text)
    rooms = sorted({d['room'] for d in devices if d.get('room')}, key=len, reverse=True)
    excluded_rooms: set[str] = set()
    excluded_entities: set[str] = set()
    reasons: list[str] = []

    for room in rooms:
        room_pat = re.escape(room)
        if re.search(room_pat + r'(?:的?(?:灯|灯光|空调|设备|照明))?' + _PRESERVE_SUFFIXES, normalized):
            excluded_rooms.add(room)
            reasons.append(f'口语明确排除：{room}')
        elif re.search(r'(?:除|除了)' + room_pat + r'(?:之外|以外|外)?', normalized):
            excluded_rooms.add(room)
            reasons.append(f'排除条件：{room}')
        elif re.search(r'(?:室友|家人|有人|爸妈|父母|朋友|孩子)(?:还)?在' + room_pat, normalized):
            excluded_rooms.add(room)
            reasons.append(f'成员仍在{room}（依据 RULE-002 保留）')
        elif re.search(room_pat + r'(?:里|内)?(?:还)?有(?:人|室友|家人)', normalized):
            excluded_rooms.add(room)
            reasons.append(f'{room}内有人（依据 RULE-002 保留）')

    for device in devices:
        names = [device['name'], *device.get('aliases', [])]
        for name in names:
            name_norm = _norm(name)
            if not name_norm:
                continue
            if re.search(re.escape(name_norm) + _PRESERVE_SUFFIXES, normalized):
                excluded_entities.add(device['entity_id'])
                reasons.append(f'口语排除设备：{device["name"]}')
            for prefix in _DIRECT_NEGATION_PREFIXES:
                if prefix + name_norm in normalized or prefix + _norm(device.get('room', '')) + name_norm in normalized:
                    excluded_entities.add(device['entity_id'])
                    reasons.append(f'否定指令保护：{device["name"]}')

    return excluded_rooms, excluded_entities, list(dict.fromkeys(reasons))


def _is_direct_full_negation(text: str, devices: list[dict[str, Any]]) -> bool:
    """Detect pure negative commands like '不要打开阅读灯' that must never execute."""
    normalized = _norm(text).rstrip('。.!?')
    for prefix in _DIRECT_NEGATION_PREFIXES:
        if normalized.startswith(prefix):
            return True
        if re.fullmatch(r'(?:请|帮我|千万)?' + re.escape(prefix) + r'.+', normalized):
            return True
    return False


def _format_observation(device: dict[str, Any], state_obj: dict[str, Any], *, note: str = '') -> dict[str, Any]:
    raw_state = state_obj.get('state', 'unavailable')
    unknown = raw_state in ('unknown', 'unavailable', '', None) or bool(state_obj.get('read_error_type'))
    label = raw_state
    if device.get('kind') == 'binary_sensor' and state_obj.get('attributes', {}).get('device_class') == 'door':
        if raw_state == 'on':
            label = '打开（未关闭）'
        elif raw_state == 'off':
            label = '已关闭'
        else:
            label = '未知（无法核实）'
    elif device.get('kind') == 'sensor':
        unit = state_obj.get('attributes', {}).get('unit_of_measurement', '')
        label = f'{raw_state}{unit}' if not unknown else '未知（不可读）'
    elif device.get('kind') == 'camera':
        label = '在线（仅状态，不回传画面）' if not unknown else '离线/未知'
    elif unknown:
        label = '未知（设备离线或不可读）'
    return {
        'entity_id': device['entity_id'],
        'name': device['name'],
        'room': device['room'],
        'kind': device['kind'],
        'access': device.get('access', 'read_only'),
        'state': 'unknown' if unknown else raw_state,
        'display_state': label,
        'last_updated': state_obj.get('last_updated') or 'unknown',
        'source': state_obj.get('source', device.get('source', 'software-demo')),
        'warning': '传感器离线或数据不可读，请人工核实。' if unknown else '',
        'note': note or ('依据 RULE-003 只读核查' if device['entity_id'] == 'binary_sensor.front_door' else '实时只读采样'),
    }


def _extract_target_rooms(text: str, devices: list[dict[str, Any]], excluded_rooms: set[str]) -> list[str]:
    normalized = _norm(text)
    # Remove read-only inspection clauses (e.g. "顺便看看客厅温度和入户门") so sensor rooms
    # are not mistaken for lighting control targets.
    control_clause = re.split(r'(?:顺便|另外|再)?(?:看看|查看|查询|核查|检查一下)(?:入户门|大门|门磁|客厅温度|温度|门口监控)', normalized)[0]
    for dev in devices:
        if dev.get('access') in ('read_only', 'event_only'):
            for name in [dev['name'], *dev.get('aliases', [])]:
                control_clause = control_clause.replace(_norm(name), '')
    all_rooms = list(dict.fromkeys(d['room'] for d in devices if d.get('room')))
    mentioned = [room for room in all_rooms if room in control_clause and room not in excluded_rooms]
    return mentioned


def _is_leave_home_request(text: str) -> bool:
    normalized = _norm(text)
    return any(k in normalized for k in _LEAVE_KEYWORDS)


def _is_inspection_request(text: str) -> bool:
    normalized = _norm(text)
    return any(k in normalized for k in _INSPECT_KEYWORDS)


def _is_care_query(text: str) -> bool:
    normalized = _norm(text)
    return any(k in normalized for k in _CARE_KEYWORDS)


def build_care_advisory(incident: dict[str, Any], inventory: Any, sensing_report: dict[str, Any] | None = None,
                        *, source: str = 'local-care-copilot') -> dict[str, Any]:
    """Build a structured, SOP-cited care advisory from read-only local context."""
    area = incident.get('area_id', '未分组')
    status = incident.get('status', 'pending_ack')
    devices = inventory.list_devices()
    room_devices = [d for d in devices if d.get('room') == area]
    observations = []
    for dev in room_devices:
        observations.append(_format_observation(dev, inventory.get_device_state(dev['entity_id']), note=f'{area}环境联查'))
    door_dev = next((d for d in devices if d['entity_id'] == 'binary_sensor.front_door'), None)
    if door_dev and all(o['entity_id'] != door_dev['entity_id'] for o in observations):
        observations.append(_format_observation(door_dev, inventory.get_device_state(door_dev['entity_id']), note='入户安全联查'))

    csi_summary = None
    if sensing_report and isinstance(sensing_report.get('csi'), dict):
        first_csi = next(iter(sensing_report['csi'].values()), None)
        if first_csi:
            csi_summary = {
                'status': first_csi.get('status', 'missing'),
                'source': first_csi.get('source', 'synthetic_fixture'),
                'note': '非侵入式 Wi-Fi CSI 感知复核（原始 IQ 仅留本地）',
            }

    if status == 'escalated':
        severity = 'critical'
        sop_refs = ['SOP-001', 'SOP-002', 'SOP-003']
        headline = f'{area} SOS 事件已超时升级，请立即启动二次应急联动'
        recommended_steps = [
            f'立即呼叫紧急联系人或社区值班人员核实{area}人员状况（依据 SOP-002）。',
            '核对下方入户门磁状态，确认救援人员入户条件。',
            '如 2 分钟内无法取得语音联系，请直接拨打 120/110。',
        ]
    elif status == 'acked':
        severity = 'resolved'
        sop_refs = ['SOP-001', 'SOP-003']
        headline = f'{area} SOS 事件已由测试家属确认，超时升级已中止'
        recommended_steps = [
            '核实居家老人身体与环境状况并做好随访记录（依据 SOP-003）。',
            '检查相关房间照明与门磁状态是否恢复正常。',
        ]
    else:
        severity = 'high'
        sop_refs = ['SOP-001', 'SOP-003']
        headline = f'{area} 触发模拟 SOS，等待家属在截止时间前确认'
        recommended_steps = [
            f'立即通过电话/对讲联系{area}内的居家成员（依据 SOP-001）。',
            '核实无误后在控制台点击“家属确认”以停止自动升级。',
            '若超时未确认，本地 CareStore 状态机将自动升级并触发二次通知（依据 SOP-002）。',
        ]

    return {
        'incident_id': incident.get('id') or incident.get('event_id'),
        'area_id': area,
        'incident_status': status,
        'severity': severity,
        'headline': headline,
        'sop_refs': sop_refs,
        'recommended_steps': recommended_steps,
        'observations': observations,
        'csi_summary': csi_summary,
        'safety_boundary': '本研判卡片仅作辅助参考，不替代专业医疗诊断或 120 急救；SOS 触发与超时升级始终由本地确定性状态机兜底执行。',
        'source': source,
        'generated_at': time.time(),
    }


def evaluate_cloud_candidate(
    text: str,
    candidate: dict[str, Any],
    controller: Any,
    inventory: Any,
    *,
    source: str = 'agentarts',
    cloud_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a structured checklist/proposal candidate from AgentArts at the edge.

    Returns a dictionary ready to return from ``DemoApp``:
    - ``awaiting_confirmation`` (created via ``controller.propose_checklist``)
    - ``read_only_report`` (0 executable actions, with skipped/preserved/observations)
    - ``clarification`` (guardrail rejection or cloud-requested clarification)
    """
    started = time.perf_counter()
    privacy = request_privacy_mode(text)
    if privacy != 'standard':
        return {
            'status': 'clarification',
            'code': 'privacy_blocked',
            'actions': [],
            'reason': '当前指令要求本地处理或离线，已拦截云端候选结果（依据 RULE-006）。',
            'source': 'local-guardrail',
            'rule_refs': ['RULE-006'],
            'trace': _compose_trace(text, privacy, source, cloud_trace, started,
                                    guard_status='blocked_privacy', agent_role='privacy-bypass'),
        }

    devices = inventory.list_devices()
    device_map = {d['entity_id']: d for d in devices}
    excluded_rooms, excluded_entities, exclusion_reasons = extract_exclusions(text, devices)

    result_kind = candidate.get('result_kind')
    agent_role = str(candidate.get('agent_role') or candidate.get('intent_type') or 'agentarts-orchestrator')
    raw_checklist = candidate.get('checklist') if isinstance(candidate.get('checklist'), dict) else {}
    raw_rules = candidate.get('rule_refs') or raw_checklist.get('rule_refs') or []
    rule_refs = [str(r) for r in raw_rules if isinstance(r, str) and r.strip()]

    if result_kind not in ('proposal', 'clarification', 'read_only_report'):
        return {
            'status': 'clarification',
            'code': 'invalid_candidate',
            'actions': [],
            'reason': '边缘护栏拦截：云端返回了未知的 result_kind 类型。',
            'source': 'local-guardrail',
            'rule_refs': ['RULE-005'],
            'trace': _compose_trace(text, privacy, source, cloud_trace, started,
                                    guard_status='blocked_schema', agent_role=agent_role),
        }

    # Hard Guardrail 1: Pure negative commands must never become executable actions.
    if _is_direct_full_negation(text, devices) and candidate.get('actions'):
        return {
            'status': 'clarification',
            'code': 'guardrail_negation_blocked',
            'actions': [],
            'reason': '边缘护栏拦截：原指令包含明确否定约束，已拒绝云端候选动作（依据 RULE-005）。',
            'source': 'local-guardrail',
            'rule_refs': list(dict.fromkeys([*rule_refs, 'RULE-005'])),
            'trace': _compose_trace(text, privacy, source, cloud_trace, started,
                                    guard_status='blocked_negation', agent_role=agent_role,
                                    rule_refs=rule_refs),
        }

    if result_kind == 'clarification':
        if candidate.get('actions'):
            return {
                'status': 'clarification',
                'code': 'invalid_candidate',
                'actions': [],
                'reason': '边缘护栏拦截：澄清类响应不得携带控制动作。',
                'source': 'local-guardrail',
                'rule_refs': ['RULE-005'],
                'trace': _compose_trace(text, privacy, source, cloud_trace, started,
                                        guard_status='blocked_schema', agent_role=agent_role),
            }
        return {
            'status': 'clarification',
            'code': str(candidate.get('code') or 'needs_clarification'),
            'actions': [],
            'reason': str(candidate.get('reason') or '请补充更明确的房间或设备信息。'),
            'source': source,
            'rule_refs': rule_refs,
            'trace': _compose_trace(text, privacy, source, cloud_trace, started,
                                    guard_status='clarification_passthrough', agent_role=agent_role,
                                    rule_refs=rule_refs),
        }

    actions = candidate.get('actions') or []
    if not isinstance(actions, list) or (result_kind == 'read_only_report' and actions):
        return {
            'status': 'clarification',
            'code': 'invalid_candidate',
            'actions': [],
            'reason': '边缘护栏拦截：云端候选动作结构非法或只读报告携带了控制动作。',
            'source': 'local-guardrail',
            'rule_refs': ['RULE-005'],
            'trace': _compose_trace(text, privacy, source, cloud_trace, started,
                                    guard_status='blocked_schema', agent_role=agent_role),
        }

    # Hard Guardrail 1b: Pure inspection / care queries must never produce executable actions.
    norm_text = _norm(text).rstrip('。.!?')
    pure_read_query = (
        _is_care_query(text)
        or (_is_inspection_request(text) and not _is_leave_home_request(text))
        or bool(re.fullmatch(r'(?:请|帮我)?(?:看看|查看|查询)[^，,；;]+(?:的状态)?', norm_text))
    )
    if pure_read_query and actions:
        return {
            'status': 'clarification',
            'code': 'guardrail_readonly_intent_violation',
            'actions': [],
            'reason': '边缘护栏拦截：只读查询/照护研判请求严禁附带设备控制动作（依据 RULE-003 / RULE-005）。',
            'source': 'local-guardrail',
            'rule_refs': list(dict.fromkeys([*rule_refs, 'RULE-003', 'RULE-005'])),
            'trace': _compose_trace(text, privacy, source, cloud_trace, started,
                                    guard_status='blocked_readonly', agent_role=agent_role),
        }

    # Hard Guardrail 1c: Pure leave-home checks cannot turn ON devices unless explicitly requested.
    if _is_leave_home_request(text) and actions and not any(v in norm_text for v in ('打开', '开启', '拉开', '调到', '设为')):
        if any(not str(a.get('service', '')).endswith(('turn_off', 'close_cover')) for a in actions if isinstance(a, dict)):
            return {
                'status': 'clarification',
                'code': 'guardrail_leave_home_direction_violation',
                'actions': [],
                'reason': '边缘护栏拦截：离家检查指令未包含开启要求，已拒绝异常开启类候选动作（依据 RULE-001 / RULE-005）。',
                'source': 'local-guardrail',
                'rule_refs': list(dict.fromkeys([*rule_refs, 'RULE-001', 'RULE-005'])),
                'trace': _compose_trace(text, privacy, source, cloud_trace, started,
                                        guard_status='blocked_schema', agent_role=agent_role),
            }

    # Hard Guardrail 2: Verify no proposed action violates exclusions or targets non-controllable devices.
    for action in actions:
        if not isinstance(action, dict):
            return {
                'status': 'clarification',
                'code': 'invalid_candidate',
                'actions': [],
                'reason': '边缘护栏拦截：候选动作格式不正确。',
                'source': 'local-guardrail',
                'rule_refs': ['RULE-005'],
                'trace': _compose_trace(text, privacy, source, cloud_trace, started,
                                        guard_status='blocked_schema', agent_role=agent_role),
            }
        eid = action.get('entity_id')
        dev = device_map.get(eid)
        if dev is None:
            return {
                'status': 'clarification',
                'code': 'device_not_found',
                'actions': [],
                'reason': f'边缘护栏拦截：候选设备 {eid} 不在本地授权目录中（依据 RULE-005）。',
                'source': 'local-guardrail',
                'rule_refs': ['RULE-005'],
                'trace': _compose_trace(text, privacy, source, cloud_trace, started,
                                        guard_status='blocked_unknown_device', agent_role=agent_role),
            }
        if dev.get('access') != 'control':
            return {
                'status': 'clarification',
                'code': 'unsupported_action',
                'actions': [],
                'reason': f'边缘护栏拦截：设备 {dev["name"]} 为只读/仅事件设备，禁止执行控制动作（依据 RULE-003）。',
                'source': 'local-guardrail',
                'rule_refs': ['RULE-003', 'RULE-005'],
                'trace': _compose_trace(text, privacy, source, cloud_trace, started,
                                        guard_status='blocked_readonly', agent_role=agent_role),
            }
        if eid in excluded_entities or dev.get('room') in excluded_rooms:
            return {
                'status': 'clarification',
                'code': 'guardrail_exclusion_blocked',
                'actions': [],
                'reason': f'边缘护栏拦截：候选动作触碰了保留/排除对象（{dev["room"]}·{dev["name"]}），已拒绝派发（依据 RULE-002 / RULE-005）。',
                'source': 'local-guardrail',
                'rule_refs': list(dict.fromkeys([*rule_refs, 'RULE-002', 'RULE-005'])),
                'trace': _compose_trace(text, privacy, source, cloud_trace, started,
                                        guard_status='blocked_exclusion', agent_role=agent_role,
                                        rule_refs=rule_refs),
            }

    # Re-verify observations locally so cloud prose can never fabricate sensor states.
    obs_ids: list[str] = []
    for item in raw_checklist.get('observations', []) if isinstance(raw_checklist.get('observations'), list) else []:
        if isinstance(item, dict) and item.get('entity_id') in device_map:
            obs_ids.append(item['entity_id'])
        elif isinstance(item, str) and item in device_map:
            obs_ids.append(item)
    if any(k in _norm(text) for k in _DOOR_KEYWORDS) and 'binary_sensor.front_door' in device_map:
        if 'binary_sensor.front_door' not in obs_ids:
            obs_ids.append('binary_sensor.front_door')

    verified_observations = [
        _format_observation(device_map[eid], inventory.get_device_state(eid))
        for eid in dict.fromkeys(obs_ids)
    ]

    # Collect skipped & preserved items and bind controllable skipped items as state dependencies!
    skipped_items: list[dict[str, Any]] = []
    preserved_items: list[dict[str, Any]] = []
    watched_dependency_ids: set[str] = {a['entity_id'] for a in actions}
    catalog_ids = {entry['entity_id'] for entry in controller.catalog}

    for item in raw_checklist.get('skipped', []) if isinstance(raw_checklist.get('skipped'), list) else []:
        if isinstance(item, dict) and item.get('entity_id') in device_map:
            eid = item['entity_id']
            dev = device_map[eid]
            live = inventory.get_device_state(eid)
            skipped_items.append({
                'entity_id': eid,
                'name': dev['name'],
                'room': dev['room'],
                'state': live.get('state'),
                'reason': str(item.get('reason') or '当前已满足目标状态，自动跳过并监控漂移'),
            })
            if eid in catalog_ids:
                watched_dependency_ids.add(eid)

    for item in raw_checklist.get('preserved', []) if isinstance(raw_checklist.get('preserved'), list) else []:
        if isinstance(item, dict) and item.get('entity_id') in device_map:
            eid = item['entity_id']
            dev = device_map[eid]
            live = inventory.get_device_state(eid)
            preserved_items.append({
                'entity_id': eid,
                'name': dev['name'],
                'room': dev['room'],
                'state': live.get('state'),
                'reason': str(item.get('reason') or '按口语排除或家庭约定保留'),
                'rule_id': str(item.get('rule_id') or ('RULE-002' if dev['room'] == '客厅' else 'RULE-005')),
            })

    # Also auto-include deterministic preserved items if cloud forgot to list them explicitly.
    listed_preserved = {p['entity_id'] for p in preserved_items}
    for dev in devices:
        if dev['access'] == 'control' and (dev['room'] in excluded_rooms or dev['entity_id'] in excluded_entities):
            if dev['entity_id'] not in listed_preserved and dev['entity_id'] not in {a['entity_id'] for a in actions}:
                live = inventory.get_device_state(dev['entity_id'])
                preserved_items.append({
                    'entity_id': dev['entity_id'],
                    'name': dev['name'],
                    'room': dev['room'],
                    'state': live.get('state'),
                    'reason': exclusion_reasons[0] if exclusion_reasons else '按排除约束保留',
                    'rule_id': 'RULE-002' if dev['room'] == '客厅' else 'RULE-005',
                })

    to_execute = []
    for action in actions:
        dev = device_map[action['entity_id']]
        live = inventory.get_device_state(action['entity_id'])
        to_execute.append({
            'entity_id': action['entity_id'],
            'name': dev['name'],
            'room': dev['room'],
            'service': action['service'],
            'data': action.get('data', {}),
            'current_state': live.get('state'),
            'last_updated': live.get('last_updated'),
        })

    checklist = {
        'intent_type': str(candidate.get('intent_type') or raw_checklist.get('intent_type') or 'checklist'),
        'to_execute': to_execute,
        'skipped': skipped_items,
        'preserved': preserved_items,
        'observations': verified_observations,
        'rule_refs': list(dict.fromkeys(rule_refs)),
        'watched_dependencies': sorted(watched_dependency_ids),
    }

    trace = _compose_trace(
        text, privacy, source, cloud_trace, started,
        guard_status='verified_for_confirmation' if actions else 'verified_read_only',
        agent_role=agent_role,
        rule_refs=checklist['rule_refs'],
        tools_used=candidate.get('tools_used') or ['sensehome_list_devices', 'sensehome_get_device_state'],
        watched_count=len(watched_dependency_ids),
    )

    if not actions:
        return {
            'status': 'read_only_report',
            'actions': [],
            'reason': str(candidate.get('reason') or '已完成全屋核查，当前无需执行控制动作。'),
            'source': source,
            'checklist': checklist,
            'rule_refs': checklist['rule_refs'],
            'trace': trace,
        }

    # Mint an immutable, state-dependency-bound proposal card via Controller.
    normalized_payload = {
        'result_kind': 'proposal',
        'actions': [{'entity_id': a['entity_id'], 'service': a['service'], 'data': a.get('data', {})} for a in actions],
        'reason': str(candidate.get('reason') or '已通过边缘安全护栏校验，请核对检查单后显式确认。'),
    }
    result = controller.propose_checklist(
        text,
        normalized_payload,
        extra_dependency_ids=watched_dependency_ids,
        checklist=checklist,
        source=source,
        trace=trace,
    )
    if result['status'] != 'awaiting_confirmation':
        return {
            'status': 'clarification',
            'code': 'guardrail_validation_failed',
            'actions': [],
            'reason': result['payload']['reason'],
            'source': 'local-guardrail',
            'checklist': checklist,
            'rule_refs': checklist['rule_refs'],
            'trace': _compose_trace(text, privacy, source, cloud_trace, started,
                                    guard_status='blocked_capability_or_state', agent_role=agent_role,
                                    rule_refs=checklist['rule_refs']),
        }

    return {
        'status': result['status'],
        'proposal_id': result['proposal_id'],
        'digest': result['digest'],
        'expires_at': result['expires_at'],
        'actions': result['payload']['actions'],
        'reason': result['payload']['reason'],
        'source': source,
        'checklist': checklist,
        'rule_refs': checklist['rule_refs'],
        'trace': trace,
    }


def _compose_trace(
    text: str,
    privacy: str,
    source: str,
    cloud_trace: dict[str, Any] | None,
    guard_started: float,
    *,
    guard_status: str,
    agent_role: str,
    rule_refs: list[str] | None = None,
    tools_used: list[str] | None = None,
    watched_count: int = 0,
) -> dict[str, Any]:
    guard_ms = round((time.perf_counter() - guard_started) * 1000, 2)
    cloud_ms = round(float((cloud_trace or {}).get('elapsed_ms', 0.0)), 2)
    session_id = (cloud_trace or {}).get('session_id') or None
    steps = [
        {
            'stage': 'privacy_gate',
            'label': '边缘隐私分级',
            'status': 'bypassed_local' if privacy != 'standard' else 'passed',
            'detail': f'策略={privacy}' + ('（零上云）' if privacy != 'standard' else '（允许安全编排）'),
        },
        {
            'stage': 'router',
            'label': '多智能体意图路由',
            'status': 'skipped' if privacy != 'standard' else 'completed',
            'detail': f'路由至 {agent_role}（来源：{source}）',
        },
        {
            'stage': 'knowledge',
            'label': '家庭约定/SOP 检索',
            'status': 'hit' if rule_refs else 'none',
            'detail': '命中规则：' + ', '.join(rule_refs) if rule_refs else '未触发知识库规则',
        },
        {
            'stage': 'tools',
            'label': '只读工具 / MCP 查询',
            'status': 'completed' if tools_used else 'none',
            'detail': ', '.join(tools_used) if tools_used else '本地目录快照',
        },
        {
            'stage': 'edge_guardrail',
            'label': '边缘安全护栏校验',
            'status': guard_status,
            'detail': f'校验耗时 {guard_ms} ms · 依赖监控实体 {watched_count} 个 · 云端无执行权',
        },
    ]
    return {
        'agentarts_session_id': session_id,
        'agent_role': agent_role,
        'privacy_policy': privacy,
        'guard_status': guard_status,
        'timing_ms': {'cloud_ms': cloud_ms, 'guard_ms': guard_ms, 'total_ms': round(cloud_ms + guard_ms, 2)},
        'steps': steps,
    }


def plan_local_checklist(text: str, controller: Any, inventory: Any, care_store: Any = None) -> dict[str, Any] | None:
    """Deterministic local copilot for leave-home checklists, inspections, and care queries.

    Returns a structured candidate dict compatible with ``evaluate_cloud_candidate``
    or ``None`` when the request should fall back to standard ``catalog_rules``.
    """
    normalized = _norm(text)
    devices = inventory.list_devices()
    device_map = {d['entity_id']: d for d in devices}

    # 1. Care SOS advisory query
    if _is_care_query(text) and care_store is not None:
        incidents = care_store.list_incidents()
        if not incidents:
            return {
                'result_kind': 'clarification',
                'code': 'no_care_incident',
                'agent_role': 'care-copilot-agent',
                'actions': [],
                'reason': '当前没有活跃或历史模拟 SOS 照护事件；可先在“照护模拟”页触发一条模拟 SOS。',
                'rule_refs': ['SOP-001'],
                'tools_used': ['sensehome_get_care_incidents'],
            }
        advisory = build_care_advisory(incidents[0], inventory, source='local-care-copilot')
        return {
            'result_kind': 'read_only_report',
            'intent_type': 'care_advisory',
            'agent_role': 'care-copilot-agent',
            'actions': [],
            'reason': advisory['headline'] + ' · ' + advisory['recommended_steps'][0],
            'rule_refs': advisory['sop_refs'],
            'tools_used': ['sensehome_get_care_incidents', 'sensehome_get_device_state', 'sensehome_get_household_rules'],
            'checklist': {
                'intent_type': 'care_advisory',
                'skipped': [],
                'preserved': [],
                'observations': advisory['observations'],
                'rule_refs': advisory['sop_refs'],
            },
            'care_advisory': advisory,
        }

    # 2. Multi-device read-only inspection
    if _is_inspection_request(text) and not _is_leave_home_request(text):
        obs_targets = [d['entity_id'] for d in devices if d['access'] in ('read_only', 'event_only')]
        if not obs_targets:
            return None
        return {
            'result_kind': 'read_only_report',
            'intent_type': 'device_inspection',
            'agent_role': 'inspection-agent',
            'actions': [],
            'reason': f'已完成 {len(obs_targets)} 项只读传感器与安防状态巡检，全部为只读采样。',
            'rule_refs': ['RULE-003'],
            'tools_used': ['sensehome_list_devices', 'sensehome_get_device_state'],
            'checklist': {
                'intent_type': 'device_inspection',
                'skipped': [],
                'preserved': [],
                'observations': obs_targets,
                'rule_refs': ['RULE-003'],
            },
        }

    # 3. Leave-home dynamic checklist
    if not _is_leave_home_request(text):
        return None

    excluded_rooms, excluded_entities, exclusion_reasons = extract_exclusions(text, devices)
    mentioned_rooms = _extract_target_rooms(text, devices, excluded_rooms)
    used_household_rule = any(h in normalized for h in _RULE_HINTS)

    # Check if user asked for vague room without household rule (e.g. "我要出门了，把那个房间的灯关了")
    if ('那个房间' in normalized or '那边' in normalized) and not mentioned_rooms:
        return {
            'result_kind': 'clarification',
            'code': 'needs_clarification',
            'agent_role': 'leave-home-agent',
            'actions': [],
            'reason': '未能明确需要检查的具体房间；请指明房间名称，或说明“按平时的离家约定检查”。',
            'rule_refs': ['RULE-001'],
            'tools_used': ['sensehome_list_devices', 'sensehome_get_household_rules'],
        }

    # Determine scope of lights to inspect
    all_lights = [d for d in devices if d['kind'] == 'light' and d['access'] == 'control']
    if mentioned_rooms:
        scope_lights = [d for d in all_lights if d['room'] in mentioned_rooms]
    else:
        scope_lights = [d for d in all_lights if d['room'] not in excluded_rooms]

    # Collect preserved items in excluded rooms (lights + climate)
    preserved = []
    for d in devices:
        if d['access'] == 'control' and (d['room'] in excluded_rooms or d['entity_id'] in excluded_entities):
            preserved.append({
                'entity_id': d['entity_id'],
                'reason': exclusion_reasons[0] if exclusion_reasons else f'{d["room"]}按约定保留',
                'rule_id': 'RULE-002' if d['room'] == '客厅' else 'RULE-005',
            })

    actions = []
    skipped = []
    unreadable_lights = []
    for light in scope_lights:
        if light['entity_id'] in excluded_entities or light['room'] in excluded_rooms:
            continue
        state = inventory.get_device_state(light['entity_id'])
        raw = state.get('state')
        if not light.get('available') or raw in ('unknown', 'unavailable', '', None) or state.get('read_error_type'):
            unreadable_lights.append(light['name'])
            continue
        if raw == 'on':
            actions.append({'entity_id': light['entity_id'], 'service': 'light.turn_off', 'data': {}})
        else:
            skipped.append({
                'entity_id': light['entity_id'],
                'reason': '当前已处于关闭状态（自动跳过，并纳入确认前状态漂移监控）',
            })

    if unreadable_lights:
        return {
            'result_kind': 'clarification',
            'code': 'device_offline',
            'agent_role': 'leave-home-agent',
            'actions': [],
            'reason': f'检查范围内设备状态不可读（{", ".join(unreadable_lights)}），无法确认是否亮灯，请恢复连接后重试。',
            'rule_refs': ['RULE-001'],
            'tools_used': ['sensehome_list_devices', 'sensehome_get_device_state'],
        }

    observations = []
    check_door = used_household_rule or any(k in normalized for k in _DOOR_KEYWORDS) or not mentioned_rooms
    if check_door and 'binary_sensor.front_door' in device_map:
        observations.append('binary_sensor.front_door')
    if '温度' in normalized and 'sensor.living_temperature' in device_map:
        observations.append('sensor.living_temperature')

    rule_refs = ['RULE-001']
    if excluded_rooms or excluded_entities:
        rule_refs.append('RULE-002')
    if observations:
        rule_refs.append('RULE-003')

    summary_parts = []
    if actions:
        summary_parts.append(f'拟关闭 {len(actions)} 盏开启的灯')
    else:
        summary_parts.append('范围内待查灯具均已关闭，无需执行动作')
    if skipped:
        summary_parts.append(f'自动跳过 {len(skipped)} 盏已关灯')
    if preserved:
        summary_parts.append(f'保留 {len(preserved)} 项排除设备')
    if observations:
        summary_parts.append('已附带入户门磁只读核查')

    return {
        'result_kind': 'proposal' if actions else 'read_only_report',
        'intent_type': 'leave_home',
        'agent_role': 'leave-home-agent',
        'actions': actions,
        'reason': '；'.join(summary_parts) + '。',
        'rule_refs': rule_refs,
        'tools_used': ['sensehome_get_household_rules', 'sensehome_list_devices', 'sensehome_get_device_state'],
        'checklist': {
            'intent_type': 'leave_home',
            'skipped': skipped,
            'preserved': preserved,
            'observations': observations,
            'rule_refs': rule_refs,
        },
    }
