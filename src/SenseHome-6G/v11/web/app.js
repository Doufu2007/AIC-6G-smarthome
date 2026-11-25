'use strict';

const el = (id) => document.getElementById(id);
const node = (tag, content = '', className = '') => {
  const result = document.createElement(tag);
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
let room = '';
let proposal = null;
let view = 'home';
let confirming = false;

const statuses = {
  awaiting_confirmation: '待确认', clarification: '待补充信息', needs_clarification: '待补充信息',
  succeeded: '执行成功', verified: '状态已核验', verified_after_unknown: '状态已核验',
  failed: '执行失败', rejected: '未派发', execution_unknown: '执行结果待核实', unknown: '结果待核实',
  scheduled: '等待执行', running: '执行中', expired: '已过期', cancelled: '已取消',
  pending_ack: '等待家属确认', acked: '家属已确认', escalated: '已升级',
  delivered: '已投递', received: '已收到', unavailable: '不可用', online: '在线',
  on: '开启', off: '关闭', open: '打开', closed: '关闭', opening: '正在打开', closing: '正在关闭',
  motion: '检测到运动', quiet: '静止', missing: '未收到数据',
  state: '状态已读取', device_state: '状态已读取', status: '状态已读取', read_only: '只读',
};
const meanings = {
  device_not_found: '设备未找到，请补充房间或设备名。',
  device_offline: '设备离线，请检查连接。',
  unsupported_action: '该动作尚未接入或设备不支持。',
  model_unavailable: '云端工作流当前不可用。',
  needs_clarification: '请补充房间、设备或动作。',
  execution_unknown: '设备执行结果暂无法核实。',
};
const origins = {
  'agentarts': 'AgentArts', 'agentarts-workflow': 'AgentArts', 'agentarts-http': 'AgentArts',
  'agentarts_workflow': 'AgentArts', 'local': '本地运行时', 'local-rules': '本地规则',
  'local_rules': '本地规则', 'local-scene': '本地场景', scheduler: '定时任务',
  'software-demo': '软件模拟', 'native-ha': 'Home Assistant',
  'home-assistant': 'Home Assistant', 'simulated': '模拟', 'offline_fixture': '合成数据',
  'synthetic_fixture': '合成数据', 'esp_csi_csv': 'ESP-CSI 回放',
  'disabled': '本地规则', 'off': '本地规则', 'local-only': '本地规则',
  'configured': 'AgentArts 已配置', 'demo': '软件模拟',
};
const actions = {
  'light.turn_on': '开灯', 'light.turn_off': '关灯', 'cover.open_cover': '打开窗帘',
  'cover.close_cover': '关闭窗帘', 'switch.turn_on': '打开开关', 'switch.turn_off': '关闭开关',
  'climate.set_temperature': '设置温度', 'climate.set_hvac_mode': '设置运行模式',
};
const stateLabel = (value) => statuses[value] || value || '未记录';
const originLabel = (value) => origins[value] || value || '';
const displayTime = (value) => {
  if (value === null || value === undefined) return '';
  const date = new Date(typeof value === 'number' ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleTimeString('zh-CN', { hour12: false });
};
const tone = (status) => {
  if (['succeeded', 'verified', 'verified_after_unknown', 'acked', 'online', 'delivered', 'state',
    'status', 'device_state', 'quiet'].includes(status)) return 'good';
  if (['failed', 'rejected', 'escalated', 'unavailable'].includes(status)) return 'bad';
  if (['pending_ack', 'awaiting_confirmation', 'unknown', 'expired', 'needs_clarification',
    'clarification', 'execution_unknown', 'missing', 'motion'].includes(status)) return 'warn';
  return '';
};
const tag = (status, text = stateLabel(status)) => node('span', text, ('tag ' + tone(status)).trim());
const deviceName = (id) => devices.find((item) => item.entity_id === id)?.name || id || '设备';
const descriptions = (id, text, error = false) => {
  const target = el(id);
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
    throw new Error(meanings[data.code] || data.reason || data.message || data.error ||
      (response.status === 401 ? '令牌无效，请重新登录。' : '请求失败（' + response.status + '）'));
  }
  return data;
}

async function busy(control, target, call) {
  if (control.disabled) return;
  control.disabled = true;
  descriptions(target, '');
  try {
    await call();
  } catch (error) {
    descriptions(target, error.message || '请求失败，请重试。', true);
  } finally {
    control.disabled = false;
  }
}

function actionLabel(action) {
  const brightness = action.data?.brightness_pct;
  const temperature = action.data?.temperature;
  return [
    deviceName(action.entity_id),
    actions[action.service] || action.service,
    typeof brightness === 'number' ? '亮度 ' + brightness + '%' : '',
    typeof temperature === 'number' ? temperature + ' °C' : '',
  ].filter(Boolean).join(' · ');
}

function updateCountdown() {
  const confirm = el('confirm-button');
  const remain = !proposal?.expires_at ? 0 : Math.max(0, Math.ceil(proposal.expires_at - Date.now() / 1000));
  confirm.disabled = confirming || !remain;
  el('countdown').textContent = !proposal ? '' : remain ? remain + ' 秒内确认' : '已过期';
}

function renderReply(reply) {
  const output = el('proposal-output');
  output.replaceChildren();
  const confirm = el('confirm-button');
  const actionList = reply.actions || reply.payload?.actions || reply.steps
    || (reply.schedule_id && reply.entity_id ? [reply] : []);
  proposal = reply.status === 'awaiting_confirmation' && (reply.proposal_id || reply.schedule_id)
    ? { ...reply, requestId: randomId() } : null;
  confirm.hidden = !proposal;
  confirm.textContent = proposal?.schedule_id ? '确认定时' : '确认执行';
  updateCountdown();

  const origin = originLabel(reply.source || reply.planner || reply.mode);
  output.append(tag(reply.status));
  if (origin) output.append(node('p', '来源 · ' + origin, 'descriptor'));
  const reason = meanings[reply.reason] || reply.reason || reply.payload?.reason || meanings[reply.code];
  if (reason) output.append(node('p', reason));
  if (actionList.length) {
    const list = node('ul');
    actionList.forEach((action) => {
      const state = action.verification?.state ?? action.actual_state ?? action.state;
      const detail = [actionLabel(action), action.status && stateLabel(action.status),
        state != null && '读回 ' + stateLabel(state)].filter(Boolean).join(' · ');
      list.append(node('li', detail));
    });
    output.append(list);
  }
  const device = (reply.state && typeof reply.state === 'object' ? reply.state : null)
    || reply.device_state || reply.device || reply;
  if (device.state != null && typeof device.state !== 'object') {
    output.append(node('p', [deviceName(device.entity_id), stateLabel(device.state)].join(' · ')));
  }
  if (reply.delay_seconds != null) output.append(node('p', '确认后 ' + reply.delay_seconds + ' 秒执行'));
  if (reply.execute_at) output.append(node('p', '执行时间 ' + displayTime(reply.execute_at), 'detail'));
  if (reply.duplicate) output.append(node('p', '已返回原回执', 'detail'));
}

async function confirmCurrent() {
  if (!proposal || confirming || el('confirm-button').disabled) return;
  const card = proposal;
  confirming = true;
  updateCountdown();
  descriptions('command-feedback', '正在提交确认…');
  try {
    const reply = card.schedule_id
      ? await api('/schedules/' + encodeURIComponent(card.schedule_id) + '/confirm', { digest: card.digest })
      : await api('/confirm', {
        proposal_id: card.proposal_id, digest: card.digest, request_id: card.requestId, confirmed: true,
      });
    if (proposal === card) {
      renderReply(reply);
      descriptions('command-feedback', '');
    }
    if (card.schedule_id) await loadSchedules();
  } catch (error) {
    if (proposal === card) descriptions('command-feedback', error.message, true);
  } finally {
    confirming = false;
    updateCountdown();
  }
}

function renderDevices() {
  const rooms = [...new Set(devices.map((device) => device.room || '未分组'))];
  if (room && !rooms.includes(room)) room = '';
  const filters = el('room-filters');
  filters.replaceChildren();
  for (const option of ['', ...rooms]) {
    const filter = button(option || '全部', () => {
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
    const detail = node('p', '', 'detail');
    const show = button('读取状态', async () => {
      if (show.disabled) return;
      show.disabled = true;
      detail.textContent = '正在读取…';
      try {
        const value = await api('/devices/' + encodeURIComponent(device.entity_id) + '/state');
        const data = value.device || (typeof value.state === 'object' ? value.state : value);
        const attributes = data.attributes || {};
        detail.textContent = [stateLabel(data.state), attributes.unit_of_measurement,
          typeof attributes.current_position === 'number' && attributes.current_position + '%',
          displayTime(data.last_updated)].filter(Boolean).join(' · ');
      } catch (error) {
        detail.textContent = error.message;
      } finally {
        show.disabled = false;
      }
    });
    detail.setAttribute('role', 'status');
    card.append(title, node('p', (device.room || '未分组') + ' · ' + (access[device.access] || device.kind), 'descriptor'),
      show, detail);
    list.append(card);
  });
}

async function loadDevices() {
  const data = await api('/devices');
  devices = data.devices || [];
  const workflow = data.workflow;
  let label = '本地规则';
  if (workflow && typeof workflow === 'object') {
    label = workflow.enabled === false ? label : 'AgentArts 已配置';
  } else if (workflow === true) {
    label = 'AgentArts 已配置';
  } else if (typeof workflow === 'string') {
    label = workflow === 'local' ? label : originLabel(workflow) || label;
  }
  el('workflow-label').textContent = label;
  el('connection-status').textContent = originLabel(data.mode) || '已连接';
  el('connection-status').className = 'tag good';
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
    const details = [item.execute_at && '安排于 ' + displayTime(item.execute_at),
      item.receipt?.status && '执行回执 · ' + stateLabel(item.receipt.status)].filter(Boolean).join(' · ');
    if (details) article.append(node('p', details, 'detail'));
    list.append(article);
  }
}

async function loadCare() {
  const { incidents = [] } = await api('/care/incidents');
  const list = el('care-list');
  list.replaceChildren();
  if (!incidents.length) return empty(list, '暂无照护事件');
  const steps = { sos_received: '收到模拟 SOS', initial_notice: '通知已投递至演示收件箱',
    acknowledged: '测试家属已确认', escalation_notice: '已投递升级通知' };
  for (const incident of incidents) {
    const { article, header } = record((incident.area_id || '未分组') + ' · SOS', incident.status);
    if (incident.status === 'pending_ack') {
      const ack = button('家属确认', () => busy(ack, 'care-feedback', async () => {
        await api('/care/incidents/' + encodeURIComponent(incident.id || incident.event_id) + '/ack', {});
        await loadCare();
      }));
      header.append(ack);
      article.append(node('p', '确认截止 ' + displayTime(incident.deadline_at), 'detail'));
    }
    const history = node('ol');
    for (const step of incident.timeline || []) {
      history.append(node('li', [displayTime(step.at), steps[step.kind] || step.kind].filter(Boolean).join(' · ')));
    }
    article.append(history);
    list.append(article);
  }
}

async function loadSensing() {
  const report = await api('/sensing');
  const container = el('sensing-output');
  container.replaceChildren();
  el('sensing-mode').textContent = originLabel(report.mode) || '离线演示';
  const cards = node('div', '', 'csi-grid');
  for (const output of Object.values(report.csi || {})) {
    const card = node('article', '', 'card');
    card.append(tag(output.status));
    const frames = output.quality?.valid_frames;
    card.append(node('p', [originLabel(output.source), typeof frames === 'number' && frames + ' 帧']
      .filter(Boolean).join(' · '), 'descriptor'));
    cards.append(card);
  }
  const readings = node('section', '', 'sensing-section');
  readings.append(node('h3', 'CSI 窗口'), cards);
  const predictions = node('section', '', 'sensing-section');
  const wm = node('div');
  for (const prediction of report.wm?.predictions || []) {
    const name = { open: '开窗帘', close: '关窗帘', hold: '保持' };
    const { article } = record((name[prediction.action] || prediction.action)
      + ' · ' + stateLabel(prediction.csi_status), prediction.predicted_state);
    if (typeof prediction.confidence === 'number') {
      article.append(node('p', '合成样本置信度 ' + Math.round(prediction.confidence * 100) + '%', 'detail'));
    }
    wm.append(article);
  }
  predictions.append(node('h3', '世界模型预测'), wm);
  const versions = node('section', '', 'sensing-section');
  const versionList = node('ol');
  const operations = { evaluate: '验证候选', promote: '启用', rollback: '回滚' };
  for (const row of report.rsi?.history || []) {
    versionList.append(node('li', (operations[row.operation] || row.operation) + ' · ' + row.version));
  }
  versions.append(node('h3', '模型更新记录'),
    node('p', '当前版本 · ' + (report.rsi?.active_version || '未记录'), 'detail'), versionList);
  container.append(readings, predictions, versions);
}

function showView(name) {
  view = ['home', 'care', 'sensing'].includes(name) ? name : 'home';
  document.querySelectorAll('[data-panel]').forEach((section) => { section.hidden = section.id !== view; });
  document.querySelectorAll('[data-view]').forEach((item) => {
    if (item.dataset.view === view) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
  if (view === 'care') busy(el('refresh-care'), 'care-feedback', loadCare);
  if (view === 'sensing') busy(el('refresh-sensing'), 'sensing-feedback', loadSensing);
  if (view === 'home') busy(el('refresh-schedules'), 'schedule-feedback', loadSchedules);
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
    if (!token) throw new Error('请输入操作者令牌。');
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
document.querySelectorAll('[data-command]').forEach((item) => item.addEventListener('click', () => {
  el('command-text').value = item.dataset.command;
  el('command-text').focus();
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
    updateCountdown();
    descriptions('command-feedback', '正在处理指令…');
    renderReply(await api('/proposals', { text }));
    descriptions('command-feedback', '');
  });
});
el('confirm-button').addEventListener('click', confirmCurrent);
el('care-form').addEventListener('submit', (event) => {
  event.preventDefault();
  busy(event.currentTarget.querySelector('button'), 'care-feedback', async () => {
    await api('/care/events', { event_id: randomId(), area_id: el('care-room').value });
    await loadCare();
  });
});
for (const [id, feedback, loader] of [
  ['refresh-devices', 'device-feedback', loadDevices], ['refresh-schedules', 'schedule-feedback', loadSchedules],
  ['refresh-care', 'care-feedback', loadCare], ['refresh-sensing', 'sensing-feedback', loadSensing],
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
