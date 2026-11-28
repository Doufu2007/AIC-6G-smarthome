'use strict';

const el = (id) => document.getElementById(id);
const node = (tagName, content = '', className = '') => {
  const result = document.createElement(tagName);
  result.textContent = content;
  if (className) result.className = className;
  return result;
};
const button = (content, action, className = 'secondary') => {
  const result = node('button', content, className);
  result.type = 'button';
  result.addEventListener('click', action);
  return result;
};
const randomId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)),
  (value) => value.toString(16).padStart(2, '0')).join('');

const session = sessionStorage.getItem('sensehome-tab') || randomId();
sessionStorage.setItem('sensehome-tab', session);
let token = sessionStorage.getItem('sensehome-operator') || '';
let devices = [];
let deviceStates = {};
let appMode = 'software-demo';
let room = '';
let proposal = null;
let lastConfirmedPayload = null;
let view = 'home';
let confirming = false;
const careAdvisories = new Map();

const statuses = {
  awaiting_confirmation: '待人工确认',
  read_only_report: '只读核查完成（无需执行）',
  clarification: '待补充信息 / 已拦截',
  needs_clarification: '待补充信息',
  succeeded: '执行成功',
  verified: '独立读回已核验',
  verified_after_unknown: '状态已核验',
  failed: '执行失败',
  rejected: '未派发',
  execution_unknown: '执行结果待核实',
  unknown: '未知 / 待核实',
  scheduled: '等待定时执行',
  running: '执行中',
  expired: '已过期',
  cancelled: '已取消',
  pending_ack: '等待家属确认',
  acked: '家属已确认',
  escalated: '已超时升级',
  delivered: '已投递',
  received: '已收到',
  unavailable: '不可用',
  online: '在线',
  on: '开启',
  off: '关闭',
  open: '打开',
  closed: '关闭',
  opening: '正在打开',
  closing: '正在关闭',
  motion: '检测到运动',
  quiet: '静止',
  missing: '未收到数据',
  state: '状态已读取',
  device_state: '状态已读取',
  status: '状态已读取',
  read_only: '只读',
  passed: '通过',
  completed: '已完成',
  hit: '已命中',
  none: '未触发',
  skipped: '旁路跳过',
  bypassed_local: '本地隐私旁路',
  verified_for_confirmation: '护栏放行 · 待确认',
  verified_read_only: '只读放行',
  verified_local_privacy: '本地隐私放行',
  blocked_negation: '否定红线拦截',
  blocked_exclusion: '排除保护拦截',
  blocked_readonly: '只读权限拦截',
  blocked_privacy: '隐私拦截',
  blocked_offline: '离线拦截',
  high: '高优先级',
  critical: '紧急升级',
  resolved: '已闭环',
};

const meanings = {
  device_not_found: '目录中未找到对应设备，请补充房间或设备名。',
  device_offline: '设备离线或状态不可读，请检查连接后重试。',
  unsupported_action: '该动作尚未接入或目标为只读设备。',
  model_unavailable: 'AgentArts 云端工作流暂不可用，请检查部署与网络配置。',
  needs_clarification: '请补充房间、设备或明确动作。',
  execution_unknown: '设备执行结果暂无法核实。',
  state_drift_blocked: '边缘安全护栏拦截：确认前检测到检查范围内设备状态已变化，旧提案已作废。',
  guardrail_negation_blocked: '边缘安全护栏拦截：候选动作与用户否定约束冲突。',
  guardrail_exclusion_blocked: '边缘安全护栏拦截：候选动作触碰了保留/排除对象。',
};

const origins = {
  agentarts: '华为云 AgentArts 多Agent',
  'agentarts-workflow': '华为云 AgentArts',
  'agentarts-http': '华为云 AgentArts',
  'local-copilot': '本地协同仿真（未接云端Key）',
  'local-guardrail': '边缘安全护栏',
  'local-rules': '本地确定性规则',
  'local-scene': '本地场景引擎',
  'local-care-copilot': '照护应急协同引擎',
  scheduler: '本地定时调度器',
  'software-demo': '软件模拟环境',
  'home-assistant': 'Home Assistant 实物',
  simulated: '模拟来源',
  offline_fixture: '合成数据',
  synthetic_fixture: '合成数据',
  esp_csi_csv: 'ESP-CSI 实采回放',
};

const actions = {
  'light.turn_on': '开灯',
  'light.turn_off': '关灯',
  'cover.open_cover': '打开窗帘',
  'cover.close_cover': '关闭窗帘',
  'switch.turn_on': '打开插座',
  'switch.turn_off': '关闭插座',
  'climate.set_temperature': '设定温度',
};

const stateLabel = (value) => statuses[value] || value || '未记录';
const originLabel = (value) => origins[value] || value || '';
const displayTime = (value) => {
  if (value === null || value === undefined || value === 'initial') return value === 'initial' ? '初始态' : '';
  if (typeof value === 'string' && /^\d{15,}$/.test(value)) {
    const ms = Number(value.slice(0, 13));
    return Number.isNaN(ms) ? value : new Date(ms).toLocaleTimeString('zh-CN', { hour12: false });
  }
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleTimeString('zh-CN', { hour12: false });
};

const tone = (status) => {
  if (['succeeded', 'verified', 'verified_after_unknown', 'acked', 'online', 'delivered', 'state',
    'status', 'device_state', 'quiet', 'read_only_report', 'passed', 'completed', 'hit',
    'verified_for_confirmation', 'verified_read_only', 'verified_local_privacy', 'resolved'].includes(status)) {
    return 'good';
  }
  if (['failed', 'rejected', 'escalated', 'unavailable', 'blocked_negation', 'blocked_exclusion',
    'blocked_readonly', 'blocked_privacy', 'blocked_offline', 'critical'].includes(status)) {
    return 'bad';
  }
  if (['pending_ack', 'awaiting_confirmation', 'unknown', 'expired', 'needs_clarification',
    'clarification', 'execution_unknown', 'missing', 'motion', 'bypassed_local', 'high'].includes(status)) {
    return 'warn';
  }
  return '';
};

const tag = (status, text = stateLabel(status)) => node('span', text, ('tag ' + tone(status)).trim());
const deviceName = (id) => devices.find((item) => item.entity_id === id)?.name || id || '设备';
const descriptions = (id, text, error = false) => {
  const target = el(id);
  if (!target) return;
  target.textContent = text;
  target.classList.toggle('error', error);
};
const empty = (target, text) => target.replaceChildren(node('p', text, 'empty'));

async function api(path, body) {
  const headers = { Authorization: 'Bearer ' + token, 'X-SenseHome-Session': session };
  const options = { headers, cache: 'no-store' };
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    options.method = 'POST';
    options.body = JSON.stringify(body);
  }
  const response = await fetch('/api/v1' + path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || meanings[data.code] || data.reason || data.message
      || (response.status === 401 ? '令牌无效，请重新登录。' : '请求失败（' + response.status + '）'));
  }
  return data;
}

async function busy(control, target, call) {
  if (control && control.disabled) return;
  if (control) control.disabled = true;
  if (target) descriptions(target, '');
  try {
    await call();
  } catch (error) {
    if (target) descriptions(target, error.message || '请求失败，请重试。', true);
  } finally {
    if (control) control.disabled = false;
  }
}

function actionLabel(action) {
  const brightness = action.data?.brightness_pct;
  const temperature = action.data?.temperature;
  return [
    action.name || deviceName(action.entity_id),
    actions[action.service] || action.service,
    typeof brightness === 'number' ? '亮度 ' + brightness + '%' : '',
    typeof temperature === 'number' ? temperature + ' °C' : '',
  ].filter(Boolean).join(' · ');
}

function updateCountdown() {
  const confirm = el('confirm-button');
  const drift = el('drift-button');
  const remain = !proposal?.expires_at ? 0 : Math.max(0, Math.ceil(proposal.expires_at - Date.now() / 1000));
  confirm.disabled = confirming || !remain;
  if (drift) drift.disabled = confirming || !remain;
  el('countdown').textContent = !proposal ? '' : remain ? remain + ' 秒内确认' : '已过期';
}

function renderTrace(trace, reply) {
  const container = el('trace-output');
  const timingEl = el('trace-timing');
  const sessionEl = el('session-trace-id');
  container.replaceChildren();
  const effectiveTrace = trace || reply?.metadata?.trace;
  if (!effectiveTrace || !Array.isArray(effectiveTrace.steps) || !effectiveTrace.steps.length) {
    timingEl.textContent = '暂无调用轨迹';
    empty(container, '发起指令后，此处实时展示全链路轨迹与耗时拆解。');
    return;
  }
  const sid = effectiveTrace.agentarts_session_id || reply?.proposal_id || reply?.request_id || 'local-session';
  sessionEl.textContent = sid.slice(0, 18);
  const t = effectiveTrace.timing_ms || {};
  timingEl.textContent = [
    effectiveTrace.agent_role && ('角色: ' + effectiveTrace.agent_role),
    typeof t.cloud_ms === 'number' && ('云端 ' + t.cloud_ms + 'ms'),
    typeof t.guard_ms === 'number' && ('护栏 ' + t.guard_ms + 'ms'),
    typeof t.total_ms === 'number' && ('总计 ' + t.total_ms + 'ms'),
  ].filter(Boolean).join(' · ');

  for (const step of effectiveTrace.steps) {
    const card = node('article', '', 'trace-card');
    const head = node('div', '', 'trace-card-head');
    head.append(node('span', step.label || step.stage, 'trace-step-name'), tag(step.status));
    card.append(head, node('p', step.detail || '', 'detail'));
    container.append(card);
  }
}

function buildChecklistGroup(title, kind, items, renderItem, countLabel) {
  const box = node('div', '', 'checklist-group ' + kind);
  const heading = node('h4');
  heading.append(node('span', title), node('span', countLabel || (items.length + ' 项'), 'mono'));
  box.append(heading);
  const list = node('ul', '', 'checklist-list');
  items.forEach((item) => list.append(renderItem(item)));
  box.append(list);
  return box;
}

function renderReply(reply) {
  const output = el('proposal-output');
  output.replaceChildren();
  const confirm = el('confirm-button');
  const drift = el('drift-button');
  const replay = el('replay-button');

  const actionList = reply.actions || reply.payload?.actions || reply.steps
    || (reply.schedule_id && reply.entity_id ? [reply] : []);
  proposal = reply.status === 'awaiting_confirmation' && (reply.proposal_id || reply.schedule_id)
    ? { ...reply, requestId: randomId() } : null;
  confirm.hidden = !proposal;
  confirm.textContent = proposal?.schedule_id ? '确认定时任务' : '确认执行检查单';
  drift.hidden = !(proposal && proposal.proposal_id && appMode === 'software-demo');
  replay.hidden = !(reply.status === 'succeeded' && lastConfirmedPayload);
  updateCountdown();

  const meta = node('div', '', 'proposal-meta');
  meta.append(tag(reply.status));
  const origin = originLabel(reply.source || reply.planner || reply.mode);
  if (origin) meta.append(node('span', origin, 'tag info'));
  if (reply.duplicate) meta.append(node('span', '幂等命中原回执 · 零重复派发', 'tag good'));
  for (const ruleId of (reply.rule_refs || reply.checklist?.rule_refs || [])) {
    meta.append(node('span', ruleId, 'rule-chip'));
  }
  output.append(meta);

  const reason = meanings[reply.reason] || reply.reason || reply.payload?.reason || meanings[reply.code];
  if (reason) output.append(node('p', reason));

  const checklist = reply.checklist || reply.metadata?.checklist;
  if (checklist) {
    if (checklist.to_execute?.length && reply.status === 'awaiting_confirmation') {
      output.append(buildChecklistGroup('拟执行动作（确认后派发）', 'exec', checklist.to_execute, (item) => {
        const li = node('li');
        li.textContent = [
          actionLabel(item),
          item.room && ('[' + item.room + ']'),
          item.current_state && ('当前: ' + stateLabel(item.current_state)),
        ].filter(Boolean).join(' · ');
        return li;
      }));
    }
    if (checklist.skipped?.length) {
      output.append(buildChecklistGroup('自动跳过（已纳入漂移监控）', 'skip', checklist.skipped, (item) => {
        const li = node('li');
        li.textContent = `${item.name}（${item.room}）· 当前 ${stateLabel(item.state)} · ${item.reason}`;
        return li;
      }));
    }
    if (checklist.preserved?.length) {
      output.append(buildChecklistGroup('按规则 / 口语保留（禁止动作）', 'keep', checklist.preserved, (item) => {
        const li = node('li');
        const prefix = item.rule_id ? `[${item.rule_id}] ` : '';
        li.textContent = `${prefix}${item.name}（${item.room}）· 当前 ${stateLabel(item.state)} · ${item.reason}`;
        return li;
      }));
    }
    if (checklist.observations?.length) {
      output.append(buildChecklistGroup('只读实时观测（边缘直采）', 'obs', checklist.observations, (item) => {
        const li = node('li');
        const ts = displayTime(item.last_updated);
        li.textContent = `${item.name}（${item.room}）· ${item.display_state || stateLabel(item.state)}`
          + (ts ? ` · 采样 ${ts}` : '') + (item.note ? ` · ${item.note}` : '');
        if (item.warning) {
          li.append(node('div', item.warning, 'checklist-warn'));
        }
        return li;
      }));
    }
  }

  if (actionList.length && (!checklist || reply.status !== 'awaiting_confirmation')) {
    const title = reply.steps ? '执行与独立读回核验结果' : '拟执行动作';
    output.append(buildChecklistGroup(title, 'exec', actionList, (action) => {
      const state = action.verification?.state ?? action.actual_state ?? action.state;
      const detail = [
        actionLabel(action),
        action.status && stateLabel(action.status),
        state != null && ('独立读回: ' + stateLabel(state)),
      ].filter(Boolean).join(' · ');
      return node('li', detail);
    }));
  }

  const device = (reply.state && typeof reply.state === 'object' ? reply.state : null)
    || reply.device_state || reply.device || reply;
  if (device.state != null && typeof device.state !== 'object' && !checklist) {
    output.append(node('p', [deviceName(device.entity_id), stateLabel(device.state),
      displayTime(device.last_updated)].filter(Boolean).join(' · ')));
  }
  if (reply.delay_seconds != null) output.append(node('p', '确认后 ' + reply.delay_seconds + ' 秒执行'));
  if (reply.execute_at) output.append(node('p', '计划执行时间 ' + displayTime(reply.execute_at), 'detail'));
  if (reply.proposal_id) {
    output.append(node('p', 'Proposal ID: ' + reply.proposal_id.slice(0, 16)
      + (reply.request_id ? ' · Request ID: ' + reply.request_id.slice(0, 16) : ''), 'detail mono'));
  }

  renderTrace(reply.trace, reply);
}

async function confirmCurrent() {
  if (!proposal || confirming || el('confirm-button').disabled) return;
  const card = proposal;
  confirming = true;
  updateCountdown();
  descriptions('command-feedback', '正在执行边缘复核、派发与独立读回…');
  try {
    let reply;
    if (card.schedule_id) {
      reply = await api('/schedules/' + encodeURIComponent(card.schedule_id) + '/confirm', { digest: card.digest });
      lastConfirmedPayload = null;
    } else {
      const payload = {
        proposal_id: card.proposal_id,
        digest: card.digest,
        request_id: card.requestId,
        confirmed: true,
      };
      reply = await api('/confirm', payload);
      lastConfirmedPayload = payload;
    }
    if (proposal === card) {
      renderReply(reply);
      descriptions('command-feedback', reply.duplicate ? '已返回原 SQLite 回执，未重复派发。' : '执行完成并通过独立读回核验。');
    }
    await loadDevices();
    if (card.schedule_id) await loadSchedules();
  } catch (error) {
    if (proposal === card) {
      const msg = error.message || '';
      descriptions('command-feedback', msg, true);
      if (msg.includes('发生变化') || msg.includes('作废') || msg.includes('过期')) {
        proposal = null;
        el('confirm-button').hidden = true;
        el('drift-button').hidden = true;
        el('countdown').textContent = '已作废';
        el('proposal-output').prepend(node('p', msg, 'checklist-warn'));
      }
    }
  } finally {
    confirming = false;
    updateCountdown();
  }
}

async function injectStateDrift() {
  if (!proposal) return;
  const target = proposal.checklist?.skipped?.[0]?.entity_id || 'light.patio';
  await busy(el('drift-button'), 'command-feedback', async () => {
    const res = await api('/demo/preset', { preset: 'inject_state_drift', entity_id: target });
    descriptions('command-feedback', res.summary, true);
    await loadDevices();
  });
}

async function replayLastConfirm() {
  if (!lastConfirmedPayload) return;
  await busy(el('replay-button'), 'command-feedback', async () => {
    const reply = await api('/confirm', lastConfirmedPayload);
    renderReply(reply);
    descriptions('command-feedback', '幂等防重演练通过：同一 Request ID 命中原 SQLite 回执（duplicate=true），物理设备零重复派发。');
  });
}

function formatDeviceStateText(device, data) {
  if (!data || typeof data !== 'object') return '点击“读取状态”获取实时快照';
  const attributes = data.attributes || {};
  let label = stateLabel(data.state);
  if (device.kind === 'binary_sensor' && attributes.device_class === 'door') {
    label = data.state === 'on' ? '打开（未关闭）' : data.state === 'off' ? '已关闭' : '未知';
  }
  const brightnessPct = typeof attributes.brightness === 'number'
    ? ('亮度 ' + Math.round((attributes.brightness * 100) / 255) + '%') : '';
  return [
    label,
    brightnessPct,
    attributes.unit_of_measurement && (data.state + attributes.unit_of_measurement),
    typeof attributes.current_position === 'number' && (attributes.current_position + '%'),
    typeof attributes.temperature === 'number' && ('设定 ' + attributes.temperature + '°C'),
    displayTime(data.last_updated),
  ].filter(Boolean).join(' · ');
}

function renderDevices() {
  const rooms = [...new Set(devices.map((device) => device.room || '未分组'))];
  if (room && !rooms.includes(room)) room = '';
  const filters = el('room-filters');
  filters.replaceChildren();
  for (const option of ['', ...rooms]) {
    const filter = button(option || '全部房间', () => {
      room = option;
      renderDevices();
    }, '');
    filter.setAttribute('aria-pressed', String(room === option));
    filters.append(filter);
  }
  const list = el('device-list');
  list.replaceChildren();
  const shown = room ? devices.filter((device) => (device.room || '未分组') === room) : devices;
  if (!shown.length) return empty(list, '没有已登记设备');
  shown.forEach((device) => {
    const card = node('article', '', 'card');
    const title = node('div', '', 'card-title');
    const state = tag(device.available ? 'online' : 'unavailable', device.available ? '在线' : '离线');
    title.append(node('h3', device.name || device.entity_id), state);
    const access = { control: '可控制', read_only: '只读', event_only: '仅事件' };
    const initialState = deviceStates[device.entity_id];
    const detail = node('p', formatDeviceStateText(device, initialState), 'detail');
    detail.setAttribute('role', 'status');

    const readLive = async () => {
      const value = await api('/devices/' + encodeURIComponent(device.entity_id) + '/state');
      const data = value.device || (typeof value.state === 'object' ? value.state : value);
      deviceStates[device.entity_id] = data;
      detail.textContent = formatDeviceStateText(device, data);
    };

    const actionBar = node('div', '', 'card-actions');
    const show = button('读取状态', () => busy(show, null, readLive));
    actionBar.append(show);
    if (appMode === 'software-demo' && (device.kind === 'light' || device.kind === 'cover'
      || device.kind === 'switch' || device.entity_id === 'binary_sensor.front_door')) {
      const toggle = button('模拟切换', () => busy(toggle, 'device-feedback', async () => {
        await api('/demo/preset', { preset: 'toggle_device', entity_id: device.entity_id });
        await readLive();
      }));
      actionBar.append(toggle);
    }

    card.append(
      title,
      node('p', `${device.room || '未分组'} · ${access[device.access] || device.kind}`, 'descriptor'),
      node('p', device.entity_id, 'descriptor mono'),
      actionBar,
      detail,
    );
    list.append(card);
  });
}

async function loadDevices() {
  const data = await api('/devices?include_states=1');
  devices = data.devices || [];
  deviceStates = data.states || {};
  appMode = data.mode || 'software-demo';
  const workflow = data.workflow;
  const label = workflow === 'agentarts' ? '华为云 AgentArts 多Agent' : '本地协同仿真（可无缝切换AgentArts）';
  el('workflow-label').textContent = label;
  el('connection-status').textContent = originLabel(appMode) || '已连接';
  el('connection-status').className = 'tag good';
  if (typeof data.mcp_tools === 'number') {
    el('mcp-summary').textContent = `${data.mcp_tools} Read/Propose · 0 Execute`;
  }
  const presetBar = el('demo-preset-bar');
  if (presetBar) presetBar.hidden = appMode !== 'software-demo';
  renderDevices();
}

function record(title, status) {
  const article = node('article', '', 'record');
  const header = node('div', '', 'record-header');
  const group = node('div');
  group.append(node('h3', title), tag(status));
  header.append(group);
  article.append(header);
  return { article, header };
}

async function loadSchedules() {
  const { schedules = [] } = await api('/schedules');
  const list = el('schedule-list');
  list.replaceChildren();
  const visible = schedules.filter((item) => item.status !== 'awaiting_confirmation');
  if (!visible.length) return empty(list, '暂无定时任务');
  for (const item of visible) {
    const { article, header } = record(actionLabel(item), item.status);
    if (item.status === 'scheduled') {
      const cancel = button('取消任务', () => busy(cancel, 'schedule-feedback', async () => {
        await api('/schedules/' + encodeURIComponent(item.schedule_id) + '/cancel', {});
        await loadSchedules();
      }));
      header.append(cancel);
    }
    const details = [
      item.execute_at && ('计划于 ' + displayTime(item.execute_at)),
      item.receipt?.status && ('执行回执: ' + stateLabel(item.receipt.status)),
    ].filter(Boolean).join(' · ');
    if (details) article.append(node('p', details, 'detail'));
    list.append(article);
  }
}

function renderAdvisoryBox(advisory) {
  const box = node('div', '', 'advisory-box');
  const head = node('div', '', 'proposal-meta');
  head.append(tag(advisory.severity), node('span', originLabel(advisory.source), 'tag info'));
  for (const ref of advisory.sop_refs || []) {
    head.append(node('span', ref, 'rule-chip'));
  }
  box.append(head, node('h4', advisory.headline));
  if (advisory.cloud_summary) {
    box.append(node('p', 'AgentArts 云端研判摘要：' + advisory.cloud_summary));
  }
  const steps = node('ul');
  for (const step of advisory.recommended_steps || []) {
    steps.append(node('li', step));
  }
  box.append(steps);
  if (advisory.observations?.length) {
    const obsText = advisory.observations
      .map((item) => `${item.name}(${item.room}): ${item.display_state}`)
      .join(' | ');
    box.append(node('p', '关联环境只读快照：' + obsText, 'detail'));
  }
  if (advisory.csi_summary) {
    box.append(node('p', `非侵入式 CSI 复核：${stateLabel(advisory.csi_summary.status)}（${advisory.csi_summary.note}）`, 'detail'));
  }
  box.append(node('p', advisory.safety_boundary, 'safety-note'));
  return box;
}

async function loadCare() {
  const { incidents = [] } = await api('/care/incidents');
  const list = el('care-list');
  list.replaceChildren();
  if (!incidents.length) return empty(list, '暂无照护事件。点击上方“触发模拟 SOS”即可体验本地状态机与照护协同研判。');
  const steps = {
    sos_received: '收到模拟 SOS',
    initial_notice: '初始通知已投递至演示收件箱',
    acknowledged: '测试家属已确认（超时升级已中止）',
    escalation_notice: '超时未确认 · 已投递二次升级通知',
  };
  for (const incident of incidents) {
    const incId = incident.id || incident.event_id;
    const { article, header } = record((incident.area_id || '未分组') + ' · SOS', incident.status);
    const controls = node('div', '', 'card-actions');
    const adviseBtn = button('生成照护协同研判', () => busy(adviseBtn, 'care-feedback', async () => {
      const adv = await api('/care/incidents/' + encodeURIComponent(incId) + '/advise', {});
      careAdvisories.set(incId, adv);
      await loadCare();
    }));
    controls.append(adviseBtn);
    if (incident.status === 'pending_ack') {
      const ack = button('家属确认', () => busy(ack, 'care-feedback', async () => {
        await api('/care/incidents/' + encodeURIComponent(incId) + '/ack', {});
        if (careAdvisories.has(incId)) {
          const adv = await api('/care/incidents/' + encodeURIComponent(incId) + '/advise', {});
          careAdvisories.set(incId, adv);
        }
        await loadCare();
      }), 'primary');
      controls.append(ack);
      article.append(node('p', '确认截止 ' + displayTime(incident.deadline_at) + ' · ID: ' + incId.slice(0, 12), 'detail mono'));
    }
    header.append(controls);

    const history = node('ol');
    for (const step of incident.timeline || []) {
      history.append(node('li', [displayTime(step.at), steps[step.kind] || step.kind].filter(Boolean).join(' · ')));
    }
    article.append(history);

    const cached = careAdvisories.get(incId);
    if (cached) article.append(renderAdvisoryBox(cached));
    list.append(article);
  }
}

async function loadSensing() {
  const report = await api('/sensing');
  const container = el('sensing-output');
  container.replaceChildren();
  el('sensing-mode').textContent = originLabel(report.mode) || '离线演示';
  const cards = node('div', '', 'csi-grid');
  for (const [key, output] of Object.entries(report.csi || {})) {
    const card = node('article', '', 'card');
    const head = node('div', '', 'card-title');
    head.append(node('h3', '窗口 · ' + key), tag(output.status));
    const frames = output.quality?.valid_frames;
    card.append(head, node('p', [originLabel(output.source), typeof frames === 'number' && (frames + ' 有效帧')]
      .filter(Boolean).join(' · '), 'descriptor'));
    cards.append(card);
  }
  const readings = node('section', '', 'sensing-section');
  readings.append(node('h3', 'Wi-Fi CSI 滑动窗口检测'), cards);

  const predictions = node('section', '', 'sensing-section');
  const wm = node('div');
  for (const prediction of report.wm?.predictions || []) {
    const name = { open: '开窗帘', close: '关窗帘', hold: '保持' };
    const { article } = record((name[prediction.action] || prediction.action)
      + ' · CSI=' + stateLabel(prediction.csi_status), prediction.predicted_state);
    if (typeof prediction.confidence === 'number') {
      article.append(node('p', '合成样本置信度 ' + Math.round(prediction.confidence * 100) + '%', 'detail'));
    }
    wm.append(article);
  }
  predictions.append(node('h3', '动作条件世界模型预测（视觉遮挡窗帘场景）'), wm);

  const versions = node('section', '', 'sensing-section');
  const versionList = node('ol');
  const operations = { evaluate: '离线回归验证', promote: '灰度启用', rollback: '异常回滚' };
  for (const row of report.rsi?.history || []) {
    versionList.append(node('li', (operations[row.operation] || row.operation) + ' · ' + row.version));
  }
  versions.append(
    node('h3', '受约束 RSI 模型版本审计'),
    node('p', '当前生效版本：' + (report.rsi?.active_version || '未记录'), 'detail mono'),
    versionList,
  );
  container.append(readings, predictions, versions);
}

async function loadGovernance() {
  const [manifest, rulesData] = await Promise.all([api('/mcp/manifest'), api('/rules')]);
  const posture = manifest.security_posture || {};
  el('mcp-posture-tag').textContent = `${posture.read_only_tools || 0} 只读 · ${posture.proposal_only_tools || 0} 提案 · ${posture.actuator_execution_tools || 0} 执行`;

  const toolList = el('mcp-tool-list');
  toolList.replaceChildren();
  for (const tool of manifest.tools || []) {
    const card = node('article', '', 'card');
    const head = node('div', '', 'card-title');
    head.append(
      node('h3', tool.name, 'mono'),
      tag(tool.annotations?.readOnlyHint ? 'verified_read_only' : 'awaiting_confirmation',
        tool.annotations?.readOnlyHint ? '只读 ReadOnly' : '仅提案 ProposeOnly'),
    );
    card.append(
      head,
      node('p', tool.description, 'detail'),
      node('p', `destructiveHint=${Boolean(tool.annotations?.destructiveHint)} · idempotentHint=${Boolean(tool.annotations?.idempotentHint)}`, 'descriptor mono'),
    );
    toolList.append(card);
  }

  const rulesList = el('rules-list');
  rulesList.replaceChildren();
  for (const rule of rulesData.rules || []) {
    const card = node('article', '', 'card');
    const head = node('div', '', 'card-title');
    head.append(node('span', rule.rule_id, 'rule-chip'), tag('info', rule.category));
    card.append(head, node('h3', rule.title), node('p', rule.summary, 'detail'));
    if (rule.scope?.length) {
      card.append(node('p', '适用范围：' + rule.scope.join(' / '), 'descriptor'));
    }
    rulesList.append(card);
  }
}

function showView(name) {
  view = ['home', 'care', 'sensing', 'governance'].includes(name) ? name : 'home';
  document.querySelectorAll('[data-panel]').forEach((section) => { section.hidden = section.id !== view; });
  document.querySelectorAll('[data-view]').forEach((item) => {
    if (item.dataset.view === view) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  if (view === 'care') busy(el('refresh-care'), 'care-feedback', loadCare);
  if (view === 'sensing') busy(el('refresh-sensing'), 'sensing-feedback', loadSensing);
  if (view === 'home') busy(el('refresh-schedules'), 'schedule-feedback', loadSchedules);
  if (view === 'governance') busy(el('refresh-governance'), 'governance-feedback', loadGovernance);
}

async function login() {
  await loadSchedules();
  await loadDevices();
  sessionStorage.setItem('sensehome-operator', token);
  el('operator-token').value = '';
  el('login').hidden = true;
  el('console').hidden = false;
  el('sign-out').hidden = false;
  showView(location.hash.slice(1));
}

el('login-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector('button');
  busy(submit, 'login-feedback', async () => {
    token = el('operator-token').value.trim();
    if (!token) throw new Error('请输入操作员令牌。');
    await login();
  });
});

el('sign-out').addEventListener('click', () => {
  sessionStorage.removeItem('sensehome-operator');
  sessionStorage.removeItem('sensehome-tab');
  location.reload();
});

window.addEventListener('hashchange', () => {
  if (!el('console').hidden) showView(location.hash.slice(1));
});

document.querySelectorAll('[data-view]').forEach((item) => item.addEventListener('click', () => {
  location.hash = item.dataset.view;
}));

function clearActiveProposal(hintText = '场景已就绪，点击「生成可核对检查单」发起本次核查。') {
  proposal = null;
  lastConfirmedPayload = null;
  el('confirm-button').hidden = true;
  el('drift-button').hidden = true;
  el('replay-button').hidden = true;
  updateCountdown();
  empty(el('proposal-output'), hintText);
}

document.querySelectorAll('[data-preset]').forEach((btn) => btn.addEventListener('click', () => {
  busy(btn, 'command-feedback', async () => {
    const res = await api('/demo/preset', { preset: btn.dataset.preset });
    clearActiveProposal();
    descriptions('command-feedback', res.summary);
    await loadDevices();
  });
}));

document.querySelectorAll('[data-command]').forEach((item) => item.addEventListener('click', () => {
  busy(item, 'command-feedback', async () => {
    if (item.dataset.presetBefore && appMode === 'software-demo') {
      const res = await api('/demo/preset', { preset: item.dataset.presetBefore });
      clearActiveProposal();
      descriptions('command-feedback', res.summary);
      await loadDevices();
    } else {
      clearActiveProposal('指令已填入，点击「生成可核对检查单」执行。');
    }
    el('command-text').value = item.dataset.command;
    el('command-text').focus();
  });
}));

el('command-form').addEventListener('submit', (event) => {
  event.preventDefault();
  if (confirming) return;
  const submit = event.currentTarget.querySelector('button');
  busy(submit, 'command-feedback', async () => {
    const text = el('command-text').value.trim();
    if (!text) return;
    proposal = null;
    el('confirm-button').hidden = true;
    el('drift-button').hidden = true;
    el('replay-button').hidden = true;
    updateCountdown();
    descriptions('command-feedback', '正在执行意图编排与边缘安全护栏校验…');
    const reply = await api('/proposals', { text });
    renderReply(reply);
    descriptions('command-feedback', '');
  });
});

el('confirm-button').addEventListener('click', confirmCurrent);
el('drift-button').addEventListener('click', injectStateDrift);
el('replay-button').addEventListener('click', replayLastConfirm);

el('care-form').addEventListener('submit', (event) => {
  event.preventDefault();
  busy(event.currentTarget.querySelector('button'), 'care-feedback', async () => {
    const created = await api('/care/events', { event_id: randomId(), area_id: el('care-room').value });
    const adv = await api('/care/incidents/' + encodeURIComponent(created.id) + '/advise', {});
    careAdvisories.set(created.id, adv);
    await loadCare();
  });
});

for (const [id, feedback, loader] of [
  ['refresh-devices', 'device-feedback', loadDevices],
  ['refresh-schedules', 'schedule-feedback', loadSchedules],
  ['refresh-care', 'care-feedback', loadCare],
  ['refresh-sensing', 'sensing-feedback', loadSensing],
  ['refresh-governance', 'governance-feedback', loadGovernance],
]) {
  el(id).addEventListener('click', () => busy(el(id), feedback, loader));
}

window.setInterval(updateCountdown, 500);
window.setInterval(() => {
  if (el('console').hidden || document.hidden) return;
  if (view === 'care' && !el('care-form').querySelector('button').disabled
    && !el('care-list').querySelector('button:disabled')) {
    busy(el('refresh-care'), 'care-feedback', loadCare);
  }
  if (view === 'home' && !el('schedule-list').querySelector('button:disabled')) {
    busy(el('refresh-schedules'), 'schedule-feedback', loadSchedules);
  }
}, 4000);

if (token) {
  const submit = el('login-form').querySelector('button');
  busy(submit, 'login-feedback', login);
}
