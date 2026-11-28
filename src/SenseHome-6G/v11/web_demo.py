"""Serve the SenseHome competition demo and its restricted AgentArts tools."""

import argparse
from contextlib import closing, contextmanager
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from urllib.parse import parse_qs, unquote, urlsplit

from agentarts import AgentArtsWorkflow
from care import CareStore
from catalog_planner import request_privacy_mode
from checklist import (
    build_care_advisory,
    evaluate_cloud_candidate,
    get_rules_catalog,
    plan_local_checklist,
)
from cli import DEFAULT_CATALOG, DEFAULT_STATES, load
from controller import Controller, SoftwareClient
from inventory import DeviceInventory, demo_household
from mcp_server import SenseHomeMCPServer
from native_ha_adapter import NativeHAAdapter
from platform_execution import HAClient, HAError
from scheduler import LightScheduler
from sensing import observe_csi, run_demo


LOG = logging.getLogger(__name__)
STATIC = {'/': ('index.html', 'text/html'), '/style.css': ('style.css', 'text/css'),
          '/app.js': ('app.js', 'text/javascript')}
SESSION = re.compile(r'[A-Za-z0-9_-]{12,128}\Z')
STUDY_SCENE = frozenset(('我想在书房看书', '我要在书房看书', '我想在书房阅读', '开启书房阅读模式'))


class DemoApp:
    def __init__(self, directory, operator_key, tool_key, *, workflow=None, ha_url=None, ha_token=None, csi_csv=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        if not operator_key or not tool_key or operator_key == tool_key:
            raise ValueError('Operator and tool credentials must be distinct and nonempty')
        self.operator_key, self.tool_key, self.workflow = operator_key, tool_key, workflow
        self.csi_csv = csi_csv
        self.contexts = {}
        self.context_lock = threading.Lock()
        self.db_path = self.directory / 'web.sqlite'
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS web_owners (kind TEXT, id TEXT, owner TEXT, PRIMARY KEY(kind,id))')
        catalog_file = self.directory / 'catalog.json'
        if ha_url:
            if not ha_token:
                raise ValueError('Set SENSEHOME_HA_TOKEN for Home Assistant mode')
            ha = HAClient(ha_url, ha_token)
            rooms_file = self.directory / 'rooms.json'
            rooms = load(rooms_file) if rooms_file.is_file() else {}
            adapter = NativeHAAdapter(ha, rooms={key: value if isinstance(value, str) else value['room']
                                                for key, value in rooms.items()})
            catalog = load(catalog_file) if catalog_file.is_file() else adapter.discover_catalog()
            self.controller = Controller(self.directory, catalog, adapter, mode='home-assistant')
            self.inventory = DeviceInventory(self.controller, ha_client=ha, rooms=rooms)
        else:
            states_file = self.directory / 'software_states.json'
            catalog, states, extra = demo_household(load(catalog_file) if catalog_file.is_file() else DEFAULT_CATALOG,
                                                    load(states_file) if states_file.is_file() else DEFAULT_STATES)
            self.controller = Controller(self.directory, catalog, SoftwareClient(states, states_file, seed_missing=True))
            self.inventory = DeviceInventory(self.controller, extra_states=extra)
        self.scheduler = LightScheduler(self.directory / 'schedules.sqlite', self.controller)
        self.care = CareStore(self.directory / 'care.sqlite')
        self.mcp = SenseHomeMCPServer(self)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.db_path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def bind(self, kind, key, owner):
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO web_owners VALUES (?,?,?)', (kind, key, owner))
            row = db.execute('SELECT owner FROM web_owners WHERE kind=? AND id=?', (kind, key)).fetchone()
            if row != (owner,):
                raise PermissionError('resource belongs to another session')

    def owns(self, kind, key, owner):
        with self.db() as db:
            row = db.execute('SELECT owner FROM web_owners WHERE kind=? AND id=?', (kind, key)).fetchone()
        return row == (owner,)

    @contextmanager
    def workflow_context(self, owner, text):
        key = secrets.token_urlsafe(24)
        with self.context_lock:
            self.contexts[key] = (owner, text)
        try:
            yield key
        finally:
            with self.context_lock:
                self.contexts.pop(key, None)

    def get_context(self, key):
        with self.context_lock:
            return self.contexts.get(key) if isinstance(key, str) else None

    @staticmethod
    def card(result, source):
        payload = result['payload']
        fields = ('status', 'proposal_id', 'digest', 'expires_at')
        out = {**{key: result[key] for key in fields if key in result},
               'actions': payload['actions'], 'reason': payload['reason'], 'source': source}
        metadata = result.get('metadata') if isinstance(result.get('metadata'), dict) else {}
        if metadata.get('checklist'):
            out['checklist'] = metadata['checklist']
            if metadata['checklist'].get('rule_refs'):
                out['rule_refs'] = metadata['checklist']['rule_refs']
        if metadata.get('trace'):
            out['trace'] = metadata['trace']
        return out

    @staticmethod
    def _is_structured_candidate(candidate):
        if not isinstance(candidate, dict):
            return False
        if candidate.get('result_kind') == 'read_only_report':
            return True
        return bool(set(candidate) & {'checklist', 'intent_type', 'agent_role', 'rule_refs', 'tools_used'})

    @staticmethod
    def _local_trace(text, privacy, source, role, guard_status, rule_refs=None, started=None):
        elapsed = round((time.perf_counter() - started) * 1000, 2) if started is not None else 0.0
        refs = list(rule_refs or ())
        return {
            'agentarts_session_id': None,
            'agent_role': role,
            'privacy_policy': privacy,
            'guard_status': guard_status,
            'timing_ms': {'cloud_ms': 0.0, 'guard_ms': elapsed, 'total_ms': elapsed},
            'steps': [
                {
                    'stage': 'privacy_gate',
                    'label': '边缘隐私分级',
                    'status': 'bypassed_local' if privacy != 'standard' else 'passed',
                    'detail': f'策略={privacy}' + ('（本地拦截，零云端调用）' if privacy != 'standard' else '（标准请求）'),
                },
                {
                    'stage': 'router',
                    'label': '意图路由',
                    'status': 'completed',
                    'detail': f'路由至 {role}（来源：{source}）',
                },
                {
                    'stage': 'knowledge',
                    'label': '家庭约定 / 规则命中',
                    'status': 'hit' if refs else 'none',
                    'detail': '命中：' + ', '.join(refs) if refs else '直接语法/设备目录匹配',
                },
                {
                    'stage': 'edge_guardrail',
                    'label': '边缘安全护栏校验',
                    'status': guard_status,
                    'detail': f'耗时 {elapsed} ms · 需人工显式确认',
                },
            ],
        }

    def owned_proposal(self, proposal_id, owner, *, context=None):
        if not self.owns('proposal', proposal_id, owner):
            raise PermissionError('proposal belongs to another session')
        if context is not None and not self.owns('workflow_proposal', proposal_id, context):
            raise PermissionError('proposal belongs to another workflow invocation')
        with closing(sqlite3.connect(self.controller.proposals.path)) as db:
            row = db.execute('SELECT card FROM proposals WHERE id=?', (proposal_id,)).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        result = json.loads(row[0])
        return self.card({**result, 'status': 'awaiting_confirmation'},
                         result['metadata']['route']['backend'])

    @staticmethod
    def clarify(code, reason, source='local-rules'):
        return dict(status='clarification', code=code, actions=[], reason=reason, source=source)

    @staticmethod
    def normalized(text):
        return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).strip('。.!?')

    def device_candidates(self, fragment):
        return [device for device in self.inventory.list_devices() if any(
            self.normalized(name) == fragment for name in
            [device['name'], *device['aliases'], device['room'] + device['name'],
             device['room'] + '的' + device['name']])]

    @staticmethod
    def _strip_no_upload_prefix(text):
        cleaned = re.sub(
            r'^\s*(?:请|麻烦)?(?:不要上传(?:云端|数据|设备信息)?|不上传(?:云端|数据)?|'
            r'禁止上传(?:云端|设备信息|数据)?|不得上传(?:云端|设备信息|数据)?|'
            r'不要发送设备信息|不要把设备信息发到云端|不要将设备信息发到云端|'
            r'仅在本地(?:处理|执行|运行)?|只在本地(?:处理|执行|运行)?)[,，;；:：\s]*',
            '',
            text.strip(),
        )
        return cleaned.strip()

    def operator_proposal(self, text, owner):
        started = time.perf_counter()
        if not isinstance(text, str) or not 0 < len(text.strip()) <= 2000:
            raise ValueError('请输入 1 到 2000 个字符。')
        privacy = request_privacy_mode(text)
        if privacy != 'standard':
            if privacy == 'no_upload':
                local_text = self._strip_no_upload_prefix(text)
                raw = self.controller.propose_local_no_upload(text, local_text)
                card = self.card(raw, raw['metadata']['route']['backend'])
                if card['status'] == 'awaiting_confirmation':
                    self.bind('proposal', card['proposal_id'], owner)
            else:
                card = self.propose({'text': text}, owner=owner)
            card['rule_refs'] = ['RULE-006']
            card['trace'] = self._local_trace(
                text, privacy, card.get('source', 'local-rules'), 'privacy-bypass',
                'verified_local_privacy' if card['status'] == 'awaiting_confirmation' else 'blocked_offline',
                rule_refs=['RULE-006'], started=started,
            )
            return card
        value = self.normalized(text)
        scheduled = re.fullmatch(r'10秒后(打开|关闭)(.+)', value)
        if scheduled:
            verb, name = scheduled.groups()
            devices = self.device_candidates(name)
            if len(devices) != 1:
                return self.clarify('device_not_found' if not devices else 'needs_clarification', '请指定一盏明确的灯。')
            device = devices[0]
            if device['kind'] != 'light' or device['access'] != 'control':
                return self.clarify('unsupported_action', '当前定时任务只支持灯光开关。')
            if not device['available']:
                return self.clarify('device_offline', '设备当前离线。')
            service = 'light.turn_on' if verb == '打开' else 'light.turn_off'
            task = self.scheduler.prepare(device['entity_id'], service)
            self.bind('schedule', task['schedule_id'], owner)
            return {**task, 'actions': [dict(entity_id=device['entity_id'], service=service, data={})],
                    'reason': '确认后 10 秒执行。', 'source': 'scheduler',
                    'trace': self._local_trace(text, privacy, 'scheduler', 'local-scheduler',
                                               'awaiting_schedule_confirm', started=started)}
        if self.workflow is not None:
            try:
                with self.workflow_context(owner, text) as context:
                    response = self.workflow(text, context)
                    cloud_trace = getattr(self.workflow, 'last_trace', None)
                    if response.get('status') == 'awaiting_confirmation':
                        card = {**self.owned_proposal(response['proposal_id'], owner, context=context), 'source': 'agentarts'}
                        if isinstance(cloud_trace, dict) and cloud_trace.get('session_id') and 'trace' not in card:
                            card['trace'] = {
                                'agentarts_session_id': cloud_trace['session_id'],
                                'agent_role': 'agentarts-workflow',
                                'privacy_policy': privacy,
                                'guard_status': 'verified_for_confirmation',
                                'timing_ms': {'cloud_ms': cloud_trace.get('elapsed_ms', 0.0), 'guard_ms': 0.0,
                                              'total_ms': cloud_trace.get('elapsed_ms', 0.0)},
                                'steps': [
                                    {'stage': 'privacy_gate', 'label': '边缘隐私分级', 'status': 'passed', 'detail': '标准请求'},
                                    {'stage': 'router', 'label': 'AgentArts 云端工作流', 'status': 'completed',
                                     'detail': f'Session {cloud_trace["session_id"][:12]}'},
                                    {'stage': 'edge_guardrail', 'label': '边缘护栏建卡', 'status': 'verified_for_confirmation',
                                     'detail': '已绑定原句与会话，等待人工确认'},
                                ],
                            }
                        return card
                    if response.get('result_kind') in ('proposal', 'clarification', 'read_only_report'):
                        if self._is_structured_candidate(response):
                            card = evaluate_cloud_candidate(
                                text, response, self.controller, self.inventory,
                                source='agentarts', cloud_trace=cloud_trace,
                            )
                            if card.get('status') == 'awaiting_confirmation':
                                self.bind('proposal', card['proposal_id'], owner)
                            return card
                        return self.propose({'text': text, 'candidate': response}, owner=owner)
                    if response.get('status') == 'clarification':
                        return self.clarify(response.get('code') or 'needs_clarification',
                                            str(response.get('reason') or '请明确房间和设备。'), 'agentarts')
                    raise ValueError('unexpected AgentArts workflow output')
            except (OSError, ValueError, KeyError, PermissionError, TypeError):
                return self.clarify('model_unavailable', 'AgentArts 暂不可用，请检查工作流部署和接线配置。', 'agentarts')
        local_candidate = plan_local_checklist(text, self.controller, self.inventory, self.care)
        if local_candidate is not None:
            card = evaluate_cloud_candidate(
                text, local_candidate, self.controller, self.inventory,
                source='local-copilot', cloud_trace=None,
            )
            if card.get('status') == 'awaiting_confirmation':
                self.bind('proposal', card['proposal_id'], owner)
            if local_candidate.get('care_advisory'):
                card['care_advisory'] = local_candidate['care_advisory']
            return card
        read = re.fullmatch(r'(?:帮我)?(?:看看|查看|查询)(.+?)(?:的状态)?', value)
        if read:
            matches = self.device_candidates(read.group(1))
            if len(matches) != 1:
                return self.clarify('device_not_found' if not matches else 'needs_clarification', '请指定要查看的设备。')
            device = matches[0]
            state = self.inventory.get_device_state(device['entity_id'])
            return dict(status='device_state', actions=[], source=device['source'], device=device, state=state,
                        rule_refs=['RULE-003'],
                        trace=self._local_trace(text, privacy, device['source'], 'inspection-agent',
                                                'verified_read_only', rule_refs=['RULE-003'], started=started))
        card = self.propose({'text': text}, owner=owner)
        if 'trace' not in card:
            refs = ['RULE-004'] if value in STUDY_SCENE or value in ('我想看书', '我想阅读', '我要看书') else []
            if refs:
                card['rule_refs'] = refs
            card['trace'] = self._local_trace(
                text, privacy, card.get('source', 'local-rules'),
                'scene-agent' if value in STUDY_SCENE else 'local-rules',
                'verified_for_confirmation' if card['status'] == 'awaiting_confirmation' else 'needs_clarification',
                rule_refs=refs, started=started,
            )
        return card

    def propose(self, body, *, owner):
        if set(body) - {'text', 'candidate'}:
            raise ValueError('unsupported proposal fields')
        text = body.get('text')
        if not isinstance(text, str) or not 0 < len(text.strip()) <= 2000:
            raise ValueError('请输入 1 到 2000 个字符。')
        candidate = body.get('candidate')
        privacy = request_privacy_mode(text)
        value = self.normalized(text)
        if privacy == 'standard' and value in STUDY_SCENE:
            result = self.controller.propose('把书房的阅读灯调到40%，拉开书房窗帘')
            source = 'local-scene'
        else:
            result = self.controller.propose_candidate(text, candidate) if candidate is not None and privacy == 'standard' else self.controller.propose(text)
            source = result['metadata']['route']['backend']
        card = self.card(result, source)
        if card['status'] == 'awaiting_confirmation':
            self.bind('proposal', card['proposal_id'], owner)
        elif privacy == 'standard':
            card['code'] = 'needs_clarification'
            if value in ('我想看书', '我想阅读', '我要看书'):
                card['reason'] = '你在哪个房间阅读？请在指令里带上房间。'
            match = re.fullmatch(r'(?:打开|关闭)([^，,]+)', value)
            if match:
                devices = self.device_candidates(match.group(1))
                if not devices:
                    card.update(code='device_not_found', reason='目录中没有唯一匹配的设备，请核对设备名称或房间。')
                elif len(devices) == 1 and devices[0]['access'] != 'control':
                    card.update(code='unsupported_action', reason='该设备目前仅支持查询。')
                elif len(devices) == 1 and (not devices[0]['available'] or
                        self.inventory.get_device_state(devices[0]['entity_id'])['state'] in ('unavailable', 'unknown')):
                    card.update(code='device_offline', reason='设备当前离线或状态不可读。')
        return card

    def tool_proposal(self, body):
        context = self.get_context(body.get('context'))
        provided_text = body.get('text')
        if context is None:
            if body.get('context'):
                raise PermissionError('workflow context expired')
            # Console test runs have no operator session. Their cards cannot be executed from the UI.
            owner, text = 'tool', provided_text
        else:
            owner, text = context
            if text != provided_text:
                raise PermissionError('workflow changed the original request')
        data = {key: value for key, value in body.items() if key != 'context'}
        candidate = data.get('candidate')
        if candidate is not None and self._is_structured_candidate(candidate):
            card = evaluate_cloud_candidate(text, candidate, self.controller, self.inventory, source='agentarts')
            if card.get('status') == 'awaiting_confirmation':
                self.bind('proposal', card['proposal_id'], owner)
        else:
            card = self.propose(data, owner=owner)
        if context is not None and card['status'] == 'awaiting_confirmation':
            self.bind('workflow_proposal', card['proposal_id'], body['context'])
        return card

    def advise_care_incident(self, incident_id):
        incident = self.care.get_incident(incident_id)
        if incident is None:
            raise KeyError(incident_id)
        report = run_demo()
        if self.csi_csv is not None:
            with self.csi_csv.open(encoding='utf-8-sig') as capture:
                report['csi']['replay'] = observe_csi(capture)
        base_advisory = build_care_advisory(incident, self.inventory, report, source='local-care-copilot')
        if self.workflow is not None:
            prompt = (f'请针对照护SOS事件 {incident_id}（房间：{incident["area_id"]}，状态：{incident["status"]}）'
                      '结合居家照护SOP预案生成家属研判与处置建议')
            try:
                with self.workflow_context('care-copilot', prompt) as context:
                    cloud_resp = self.workflow(prompt, context)
                if isinstance(cloud_resp, dict) and isinstance(cloud_resp.get('reason'), str) and cloud_resp['reason'].strip():
                    base_advisory['cloud_summary'] = cloud_resp['reason'].strip()
                    base_advisory['source'] = 'agentarts'
            except (OSError, ValueError, KeyError, PermissionError, TypeError):
                pass
        return base_advisory

    def apply_demo_preset(self, body):
        if self.controller.mode != 'software-demo':
            raise PermissionError('demo presets are available only in software-demo mode')
        preset = body.get('preset')
        client = self.controller.client
        catalog_map = {e['entity_id']: e for e in self.controller.catalog}
        if preset == 'leave_home_ready':
            desired = {
                'light.reading': ('light.turn_on', {'brightness_pct': 80}),
                'light.living': ('light.turn_on', {}),
                'light.patio': ('light.turn_off', {}),
                'light.bedroom': ('light.turn_off', {}),
                'light.kitchen': ('light.turn_off', {}),
                'light.bathroom': ('light.turn_off', {}),
                'light.entry': ('light.turn_off', {}),
            }
            for eid, (service, data) in desired.items():
                if eid in catalog_map:
                    client.call_service(eid, service, data)
            if 'binary_sensor.front_door' in self.inventory.extra_states:
                self.inventory.set_extra_state('binary_sensor.front_door', 'on', str(time.time_ns()))
            return {'preset': preset, 'summary': '已准备离家演示初始态：书房灯亮、客厅灯亮、阳台灯灭、入户门磁打开。'}
        if preset == 'leave_home_all_off':
            for eid in ('light.reading', 'light.patio', 'light.bedroom', 'light.kitchen', 'light.bathroom', 'light.entry'):
                if eid in catalog_map:
                    client.call_service(eid, 'light.turn_off', {})
            if 'light.living' in catalog_map:
                client.call_service('light.living', 'light.turn_on', {})
            if 'binary_sensor.front_door' in self.inventory.extra_states:
                self.inventory.set_extra_state('binary_sensor.front_door', 'off', str(time.time_ns()))
            return {'preset': preset, 'summary': '已切换为全灭态：仅客厅灯亮、其余灯均已关闭、入户门磁已关闭。'}
        if preset == 'inject_state_drift':
            target = body.get('entity_id') or 'light.patio'
            if not isinstance(target, str) or target not in catalog_map or catalog_map[target]['kind'] != 'light':
                raise ValueError('state drift target must be a known catalog light')
            current = self.inventory.get_device_state(target)['state']
            service = 'light.turn_off' if current == 'on' else 'light.turn_on'
            client.call_service(target, service, {})
            new_state = self.inventory.get_device_state(target)['state']
            return {
                'preset': preset,
                'entity_id': target,
                'previous_state': current,
                'new_state': new_state,
                'summary': f'已注入确认前状态漂移：{target} 由 {current} 变为 {new_state}。此时点击“确认执行”将被边缘护栏拦截。',
            }
        if preset == 'toggle_device':
            eid = body.get('entity_id')
            if not isinstance(eid, str) or not eid:
                raise ValueError('entity_id is required')
            if eid == 'binary_sensor.front_door' and eid in self.inventory.extra_states:
                current = self.inventory.get_device_state(eid)['state']
                updated = self.inventory.set_extra_state(eid, 'off' if current == 'on' else 'on', str(time.time_ns()))
                return {'preset': preset, 'entity_id': eid, 'new_state': updated['state']}
            if eid not in catalog_map:
                raise ValueError('unknown controllable entity_id')
            current = self.inventory.get_device_state(eid)['state']
            if eid.startswith('light.'):
                client.call_service(eid, 'light.turn_off' if current == 'on' else 'light.turn_on', {})
            elif eid.startswith('switch.'):
                client.call_service(eid, 'switch.turn_off' if current == 'on' else 'switch.turn_on', {})
            elif eid.startswith('cover.'):
                client.call_service(eid, 'cover.close_cover' if current == 'open' else 'cover.open_cover', {})
            else:
                raise ValueError('unsupported toggle entity')
            return {'preset': preset, 'entity_id': eid, 'new_state': self.inventory.get_device_state(eid)['state']}
        raise ValueError('unknown demo preset')

    def confirm(self, body, *, owner):
        if owner == 'tool' or not isinstance(body.get('proposal_id'), str) or not self.owns('proposal', body['proposal_id'], owner):
            raise PermissionError('proposal belongs to another session')
        request_id = body.get('request_id')
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise ValueError('invalid request ID')
        if set(body) != {'proposal_id', 'digest', 'request_id', 'confirmed'} or body['confirmed'] is not True:
            raise ValueError('explicit confirmation required')
        self.bind('receipt', request_id, owner)
        return self.controller.confirm(body)

    def dispatch(self, method, path, query, body, role, owner):
        if method == 'POST' and path == '/mcp':
            if role != 'tool':
                raise PermissionError('tool credential required for MCP endpoint')
            return self.mcp.handle_jsonrpc(body)
        if method == 'GET' and path == '/api/v1/mcp/manifest':
            return self.mcp.manifest()
        if method == 'GET' and path == '/api/v1/rules':
            return get_rules_catalog(query=query.get('q', [None])[0], category=query.get('category', [None])[0])
        if method == 'GET' and path == '/api/v1/devices':
            items = self.inventory.list_devices(query.get('room', [None])[0])
            payload = dict(devices=items, mode=self.controller.mode,
                           workflow='agentarts' if self.workflow else 'local',
                           mcp_tools=len(self.mcp.manifest()['tools']), guardrail='edge-firewall-active')
            if query.get('include_states', ['0'])[0] == '1':
                payload['states'] = {item['entity_id']: self.inventory.get_device_state(item['entity_id']) for item in items}
            return payload
        state = re.fullmatch(r'/api/v1/devices/([^/]+)/state', path)
        if method == 'GET' and state:
            return self.inventory.get_device_state(state.group(1))
        if method == 'POST' and path == '/api/v1/proposals':
            if role == 'tool':
                return self.tool_proposal(body)
            if set(body) != {'text'}:
                raise ValueError('operator proposal accepts text only')
            return self.operator_proposal(body['text'], owner)
        incident = re.fullmatch(r'/api/v1/care/incidents/([A-Za-z0-9_-]+)', path)
        if method == 'GET' and incident:
            result = self.care.get_incident(incident.group(1))
            if result is None:
                raise KeyError(path)
            return result
        if role != 'operator':
            raise PermissionError('operator permission required')
        if method == 'POST' and path == '/api/v1/demo/preset':
            return self.apply_demo_preset(body)
        if method == 'POST' and path == '/api/v1/confirm':
            return self.confirm(body, owner=owner)
        receipt = re.fullmatch(r'/api/v1/receipts/([^/]+)', path)
        if method == 'GET' and receipt:
            request_id = receipt.group(1)
            if not self.owns('receipt', request_id, owner):
                raise KeyError(path)
            result = self.controller.receipt(request_id)
            if result is None:
                raise KeyError(path)
            return result
        if method == 'GET' and path == '/api/v1/schedules':
            return dict(schedules=[task for task in self.scheduler.list_schedules()
                                   if self.owns('schedule', task['schedule_id'], owner)])
        schedule = re.fullmatch(r'/api/v1/schedules/([a-z0-9]+)/(?P<action>confirm|cancel)', path)
        if method == 'POST' and schedule:
            key = schedule.group(1)
            if not self.owns('schedule', key, owner):
                raise KeyError(path)
            if schedule['action'] == 'confirm':
                if set(body) != {'digest'}:
                    raise ValueError('schedule digest is required')
                return self.scheduler.confirm(key, body['digest'])
            return self.scheduler.cancel(key)
        if method == 'GET' and path == '/api/v1/care/incidents':
            return dict(incidents=self.care.list_incidents())
        if method == 'POST' and path == '/api/v1/care/events':
            if set(body) - {'event_id', 'area_id'} or 'event_id' not in body:
                raise ValueError('provide an event ID and optional area')
            return self.care.create_event(body['event_id'], area_id=body.get('area_id', 'bedroom'))
        advise = re.fullmatch(r'/api/v1/care/incidents/([A-Za-z0-9_-]+)/advise', path)
        if method == 'POST' and advise:
            return self.advise_care_incident(advise.group(1))
        acknowledgment = re.fullmatch(r'/api/v1/care/incidents/([A-Za-z0-9_-]+)/ack', path)
        if method == 'POST' and acknowledgment:
            result = self.care.acknowledge(acknowledgment.group(1))
            if result is None:
                raise KeyError(path)
            return result
        if method == 'GET' and path == '/api/v1/sensing':
            report = run_demo()
            if self.csi_csv is not None:
                with self.csi_csv.open(encoding='utf-8-sig') as capture:
                    report['csi']['replay'] = observe_csi(capture)
            return report
        raise KeyError(path)

    def background(self, stop):
        while not stop.wait(0.5):
            try:
                self.scheduler.run_due()
                self.care.expire_due()
            except Exception as exc:
                LOG.error('Background task failed: %s', type(exc).__name__)


class DemoHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        pass

    def send_data(self, status, value, content_type='application/json'):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type + '; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(data)

    def read_body(self):
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 <= length <= 32768:
            raise ValueError('request body too large')
        value = json.loads(self.rfile.read(length)) if length else {}
        if not isinstance(value, dict):
            raise ValueError('request body must be a JSON object')
        return value

    def identity(self, app):
        raw = self.headers.get('Authorization', '')
        token = raw.removeprefix('Bearer ') if raw.startswith('Bearer ') else ''
        if hmac.compare_digest(token.encode(), app.operator_key.encode()):
            session = self.headers.get('X-SenseHome-Session', '')
            if not SESSION.fullmatch(session):
                raise PermissionError('operator session required')
            return 'operator', session
        if hmac.compare_digest(token.encode(), app.tool_key.encode()):
            return 'tool', 'tool'
        return None, None

    def handle_request(self, method):
        app = self.server.app
        url = urlsplit(self.path)
        path = unquote(url.path)
        try:
            if method == 'GET' and path == '/healthz':
                self.send_data(200, {'status': 'ok'})
                return
            if method == 'GET' and path in STATIC:
                filename, content_type = STATIC[path]
                self.send_data(200, (Path(__file__).with_name('web') / filename).read_bytes(), content_type)
                return
            if method == 'GET' and path == '/mcp':
                self.send_response(405)
                self.send_header('Allow', 'POST')
                self.end_headers()
                return
            role, owner = self.identity(app)
            if role is None:
                self.send_data(401, {'error': '请输入有效的访问凭据。'})
                return
            body = self.read_body() if method == 'POST' else {}
            value = app.dispatch(method, path, parse_qs(url.query), body, role, owner)
            if value is None:
                self.send_response(202)
                self.end_headers()
                return
            self.send_data(200, value)
        except PermissionError:
            self.send_data(403, {'error': '该凭据或会话没有此操作的权限。'})
        except KeyError:
            self.send_data(404, {'error': '未找到设备、事件或回执。'})
        except (ValueError, TypeError) as exc:
            message = str(exc)
            if 'dependency changed' in message or 'state changed' in message:
                self.send_data(400, {
                    'error': '边缘安全护栏拦截：确认前检测到检查范围内设备状态已发生变化，旧提案已自动作废，请重新生成检查单。',
                    'code': 'state_drift_blocked',
                    'detail': message,
                })
            elif 'proposal expired' in message:
                self.send_data(400, {
                    'error': '提案已超过 30 秒确认有效期，请重新生成检查单。',
                    'code': 'proposal_expired',
                    'detail': message,
                })
            else:
                self.send_data(400, {'error': message})
        except (BrokenPipeError, ConnectionResetError):
            return
        except (HAError, OSError):
            self.send_data(503, {'error': '设备平台或本机服务暂不可用。'})
        except Exception as exc:
            LOG.error('Request failed: %s', type(exc).__name__)
            self.send_data(500, {'error': '请求处理失败。'})

    def do_GET(self):
        self.handle_request('GET')

    def do_POST(self):
        self.handle_request('POST')


def main():
    parser = argparse.ArgumentParser(description='SenseHome Web Demo')
    parser.add_argument('--home', default='demo/web')
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--ha-url')
    parser.add_argument('--csi-csv', type=Path, help='Read one local ESP-CSI capture window')
    args = parser.parse_args()
    operator_key = os.environ.get('SENSEHOME_OPERATOR_KEY')
    tool_key = os.environ.get('SENSEHOME_TOOL_KEY')
    workflow_url = os.environ.get('AGENTARTS_WORKFLOW_URL')
    runtime_key = os.environ.get('AGENTARTS_RUNTIME_KEY')
    if bool(workflow_url) != bool(runtime_key):
        parser.error('Set both AGENTARTS_WORKFLOW_URL and AGENTARTS_RUNTIME_KEY')
    try:
        workflow_timeout = float(os.environ.get('AGENTARTS_TIMEOUT', '60'))
        if not 0 < workflow_timeout <= 300:
            raise ValueError
    except ValueError:
        parser.error('AGENTARTS_TIMEOUT must be a number within (0, 300]')
    workflow = AgentArtsWorkflow(workflow_url, runtime_key,
                result_path=os.environ.get('AGENTARTS_RESULT_PATH', 'proposal'),
                timeout=workflow_timeout) if workflow_url else None
    if args.bind not in ('127.0.0.1', 'localhost') and (not operator_key or not tool_key or args.ha_url):
        parser.error('Non-loopback software demo requires both configured keys. Home Assistant mode must bind locally.')
    if not operator_key:
        operator_key = secrets.token_urlsafe(18)
        print('Local operator key: ' + operator_key, flush=True)
    app = DemoApp(args.home, operator_key, tool_key or secrets.token_urlsafe(24), workflow=workflow,
                  ha_url=args.ha_url, ha_token=os.environ.get('SENSEHOME_HA_TOKEN'), csi_csv=args.csi_csv)
    stop = threading.Event()
    with ThreadingHTTPServer((args.bind, args.port), DemoHandler) as server:
        server.app = app
        worker = threading.Thread(target=app.background, args=(stop,), daemon=True)
        worker.start()
        print(f'SenseHome demo: http://{args.bind}:{server.server_port}/', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            worker.join()


if __name__ == '__main__':
    main()
