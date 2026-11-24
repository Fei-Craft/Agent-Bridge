'use strict';
const $ = id => document.getElementById(id);
const labels = {running: '运行中', queued: '排队中', completed: '已完成', cancelled: '已取消', failed: '失败',
  pending: '待交付', delivered: '已送达', retrying: '重试中', in_progress: '执行中', end_turn: '正常结束'};
const eventLabels = {prompt_sent: '任务指令', message_chunk: '代理回复', thought_chunk: '思考', tool_call: '工具调用',
  tool_call_update: '工具进展', turn_end: '本轮结束', cloud_status: '云端状态', error: '错误', plan: '执行计划', raw: '运行日志'};
let snapshot = {tasks: [], remote: {workspaces: {}}, approvals: [], audit: []};
let agents = [], selected = null, busy = false, page = 'overview', transcriptOffset = 0, resultCursor = 0, authenticated = false;
let lastChunk = null;

function node(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (className) item.className = className;
  return item;
}
function notice(text, error = false) { $('notice').textContent = text; $('notice').className = error ? 'error' : ''; }
function badge(status, text) { return node('span', text || labels[status] || status, 'badge ' + status); }
function time(value, date = false) {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString('zh-CN', date ?
    {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false} :
    {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
}
function values(value) {
  if (Array.isArray(value)) return value.map(values).filter(Boolean).join(' · ');
  if (value && typeof value === 'object') return Object.entries(value).map(([key, item]) => key + ': ' + values(item)).join(' · ');
  return value === undefined || value === null ? '' : String(value);
}
function field(grid, label, value, className = '') {
  if (value === undefined || value === null || value === '' || (Array.isArray(value) && !value.length)) return;
  const item = node('div', undefined, className), content = node('dd');
  content.append(value instanceof Node ? value : node('span', values(value)));
  item.append(node('dt', label), content); grid.append(item);
}
async function api(path, body, method) {
  const response = await fetch(path, {method: method || (body === undefined ? 'GET' : 'POST'), credentials: 'same-origin',
    headers: {'X-Agent-Bridge-UI': '1', 'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok || !data.ok) {
    if (response.status === 401) authenticated = false;
    throw new Error(data.error || '请求失败，请重新运行 agent-bridge ui 登录');
  }
  return data;
}
function show(name) {
  page = name;
  document.querySelectorAll('.page').forEach(item => { item.hidden = item.id !== name; });
  document.querySelectorAll('nav button').forEach(item => item.classList.toggle('active', item.dataset.page === name ||
    (name === 'detail' && item.dataset.page === 'overview')));
}
function button(text, action) {
  const item = node('button', text);
  item.addEventListener('click', async () => {
    item.disabled = true;
    try { await action(); } catch (error) { notice(error.message, true); } finally { item.disabled = false; }
  });
  return item;
}
function delivery(task) {
  return [['通知', task.caller_delivery], ['回调', task.callback_delivery]].filter(([, item]) => item?.status)
    .map(([label, item]) => label + ' · ' + (labels[item.status] || item.status)).join(' / ');
}
function renderTasks() {
  $('metrics').replaceChildren(...[['全部任务', snapshot.tasks.length], ['进行中', snapshot.tasks.filter(task => ['running', 'queued'].includes(task.status)).length], ['已完成', snapshot.tasks.filter(task => task.status === 'completed').length]].map(([label, value]) => {
    const card = node('div', undefined, 'metric'); card.append(node('span', label, 'muted small'), node('strong', value)); return card;
  }));
  const rows = snapshot.tasks.filter(task => !$('filter').value || task.status === $('filter').value).map(task => {
    const row = node('tr'), title = node('td', task.task_id);
    title.append(node('small', task.message.slice(0, 110)));
    const state = node('td'); state.append(node('span', labels[task.status] || task.status, 'badge ' + task.status));
    const actions = node('td'); actions.append(button('详情', () => openTask(task)));
    if (['running', 'queued'].includes(task.status)) actions.append(button('取消', async () => {
      if (confirm('确认取消任务 ' + task.task_id + '？')) { await api('/v1/tasks/' + task.task_id + '/cancel', {}); await refresh(); }
    }));
    if (delivery(task)) state.append(node('small', delivery(task), 'delivery'));
    row.append(title, node('td', task.agent, 'task-agent'), state, actions);
    return row;
  });
  if (!rows.length) { const cell = node('td', undefined, 'empty'); cell.colSpan = 4; cell.append(node('p', '暂无匹配的任务'), node('p', '派发新任务，或选择其他状态。', 'muted small'), button('派发任务', () => show('dispatch'))); const row = node('tr'); row.className = 'empty-row'; row.append(cell); rows.push(row); }
  $('tasks').replaceChildren(...rows);
}
function renderGateway() {
  const remote = snapshot.remote, grid = node('dl', undefined, 'key-grid gateway-grid');
  field(grid, '网关状态', badge(remote.enabled ? 'completed' : 'cancelled', remote.enabled ? '已启用 · 独立回环端口' : '未启用'));
  field(grid, '本机审批', badge(remote.require_approval ? 'queued' : 'cancelled', remote.require_approval ? '需本机批准' : '无需审批'));
  field(grid, '公共地址', remote.public_url || '未配置', 'mono-value');
  field(grid, '允许调用的代理', remote.agents.length ? remote.agents.join(' · ') : '无');
  const workspaces = node('ul', undefined, 'workspace-list');
  for (const [name, path] of Object.entries(remote.workspaces)) {
    const row = node('li'); row.append(node('span', name, 'workspace-name'), node('span', '→', 'muted'),
      node('span', path || '路径不可用（无法解析）', path ? 'mono' : 'muted')); workspaces.append(row);
  }
  field(grid, '命名工作区', workspaces.childElementCount ? workspaces : '未配置', 'wide');
  $('gateway-status').replaceChildren(grid);
  $('approval-count').textContent = snapshot.approvals.length;
  $('approvals').replaceChildren(...snapshot.approvals.map(item => {
    const card = node('div', undefined, 'card approval');
    const heading = node('div', undefined, 'heading'), actions = node('div', undefined, 'approval-actions');
    heading.append(node('strong', item.agent + ' → ' + item.workspace), badge('queued', '等待批准'));
    card.append(heading, node('p', item.message, 'approval-message'), node('p', item.approval_id, 'muted small mono'));
    for (const [action, text] of [['approve', '批准执行'], ['deny', '拒绝']]) {
      const control = button(text, async () => { await api('/v1/approvals/' + item.approval_id + '/' + action, {}); await refresh(); });
      if (action === 'approve') control.className = 'primary'; actions.append(control);
    }
    card.append(actions);
    return card;
  }));
  if (!snapshot.approvals.length) $('approvals').replaceChildren(node('p', '暂无待审批调用', 'empty muted'));
  renderAudit();
}
function renderAudit() {
  const records = snapshot.audit.slice().reverse();
  if (!records.length) { $('audit').replaceChildren(node('p', '暂无审计记录', 'empty muted')); return; }
  const columns = [['timestamp', '时间'], ['token_id', 'Token 编号'], ['remote_ip', '来源 IP'], ['route', '路由'],
    ['agent', 'Agent'], ['workspace', '工作区'], ['outcome', '结果']].filter(([key]) => records.some(item => item[key]));
  const table = node('table'), head = node('thead'), header = node('tr'), body = node('tbody');
  columns.forEach(([, label]) => header.append(node('th', label))); head.append(header);
  for (const record of records) {
    const row = node('tr');
    for (const [key] of columns) {
      const cell = node('td', undefined, ['route', 'token_id', 'remote_ip'].includes(key) ? 'mono' : '');
      if (key === 'outcome' && record[key]) {
        const status = record[key] === 'accepted' ? 'completed' : record[key] === 'awaiting_approval' ? 'queued' : 'cancelled';
        cell.append(badge(status, {accepted: '已接受', awaiting_approval: '等待审批', 'not found': '路由不存在',
          'invalid or expired token': '凭据无效或过期', 'remote call denied or expired': '已拒绝或过期'}[record[key]] || record[key]));
      } else cell.textContent = key === 'timestamp' ? time(record[key], true) : record[key] || '—';
      row.append(cell);
    }
    body.append(row);
  }
  table.append(head, body); $('audit').replaceChildren(table);
}
function eventText(event) {
  const data = event.data || {}, item = data.payload?.item || data.payload || data.value || {};
  if (event.type.startsWith('tool_call')) return [data.title || item.command || data.path || data.kind || item.type,
    labels[data.status] || data.status].filter(Boolean).join(' · ');
  return values(data.text || data.title || data.stderr || data.path || item.step_update?.text_delta || item.text_delta || item.delta || item.text || item.message ||
    data.status_detail || data.status || data.stop_reason || data.error || item.type || data.update_type || '');
}
function appendTranscript(events) {
  for (const event of events) {
    const type = event.type || 'raw', text = eventText(event), chunk = ['message_chunk', 'thought_chunk'].includes(type);
    if (chunk && !text) continue;
    if (chunk && lastChunk?.type === type) { lastChunk.content.textContent += text; continue; }
    const row = node('article', undefined, 'timeline-event ' + (type.startsWith('tool_call') ? 'tool-event' : type));
    const heading = node('div', undefined, 'event-heading');
    heading.append(node('time', time(event.ts)), node('span', eventLabels[type] || type));
    const content = node('div', (chunk ? text : labels[text] || text) || '事件已记录', 'event-content');
    row.append(heading, content); $('transcript').append(row);
    lastChunk = chunk ? {type, content} : null;
  }
}
function renderMetadata(data) {
  const grid = node('dl', undefined, 'key-grid');
  field(grid, '代理', data.agent); field(grid, '任务状态', badge(data.status));
  field(grid, '模型', data.observed_model || data.model); field(grid, '推理强度', data.observed_effort || data.effort);
  field(grid, '开始时间', data.started_at ? time(data.started_at, true) : null);
  field(grid, '耗时', data.elapsed_sec === undefined ? null : data.elapsed_sec + ' 秒');
  field(grid, '会话编号', data.session_id, 'mono-value'); field(grid, '任务编号', data.task_id, 'mono-value');
  field(grid, '交付状态', delivery(data), 'wide');
  for (const [label, item] of [['通知错误', data.caller_delivery], ['回调错误', data.callback_delivery]]) field(grid, label, item?.error, 'wide');
  const remoteLabels = {url: '远程地址', pr_urls: 'Pull Requests', branches: '分支', status: '远程状态', status_detail: '状态详情',
    agent_id: '远程代理编号', run_id: '运行编号', session_id: '远程会话编号'};
  for (const [key, value] of Object.entries(data.remote || {})) field(grid, remoteLabels[key] || key, values(value), 'wide mono-value');
  field(grid, '变更文件', data.files_changed, 'wide mono-value');
  field(grid, '错误', data.error, 'wide error'); field(grid, '警告', data.warnings, 'wide');
  $('detail-meta').replaceChildren(grid);
}
async function loadTranscript() {
  if (!selected) return;
  const taskId = selected.task_id;
  const data = await api('/v1/sessions/' + selected.session_id + '/transcript?offset=' + transcriptOffset + '&limit=100');
  if (selected?.task_id !== taskId) return;
  const events = data.events || [];
  if (events.length) {
    appendTranscript(events);
    transcriptOffset += events.length;
  }
  $('more-transcript').hidden = !data.has_more;
  if (!$('transcript').textContent) $('transcript').textContent = '';
}
async function loadResult(append = false) {
  if (!selected) return;
  const taskId = selected.task_id;
  const data = await api('/v1/tasks/' + taskId + '/result?cursor=' + (append ? resultCursor : 0));
  if (selected?.task_id !== taskId) return;
  renderMetadata(data);
  if (append) $('result').textContent += data.result_text || '';
  else $('result').textContent = data.result_text || data.error || '任务尚未产生结果';
  resultCursor = data.next_cursor || 0;
  $('more-result').hidden = !data.has_more;
}
async function openTask(task) {
  selected = task; transcriptOffset = 0; resultCursor = 0; lastChunk = null;
  $('transcript').textContent = ''; $('result').textContent = ''; $('detail-id').textContent = task.task_id;
  show('detail'); await loadTranscript(); await loadResult();
}
async function refresh() {
  if (busy || !authenticated) return;
  busy = true;
  try {
    snapshot = await api('/ui/data'); renderTasks(); renderGateway();
    const sessionData = await api('/v1/sessions');
    $('sessions').replaceChildren(...sessionData.sessions.map(session => node('p', session.session_id + ' · ' + session.agent + ' · ' + session.proc_state + ' · ' + session.cwd)));
    if (!sessionData.sessions.length) $('sessions').textContent = '暂无会话 · 派发任务后，会话会显示在这里。';
    if (page === 'detail') { await loadTranscript(); if (!resultCursor) await loadResult(); }
    notice('已连接 · 自动刷新中 · ' + new Date().toLocaleTimeString('zh-CN'));
  } catch (error) { notice(error.message, true); } finally { busy = false; }
}
async function initialize() {
  const code = new URLSearchParams(location.hash.slice(1)).get('code');
  history.replaceState(null, '', '/ui');
  try {
    if (code) await api('/ui/login', {code});
    await api('/ui/data'); authenticated = true;
    const [agentData, sessionData] = await Promise.all([api('/v1/agents'), api('/v1/sessions')]);
    agents = agentData.agents;
    $('agents').replaceChildren(...agents.map(agent => {
      const card = node('div', undefined, 'agent');
      card.append(node('strong', agent.agent), node('span', agent.available ? '● 可用' : '○ 不可用', agent.available ? 'local' : 'muted'), node('span', agent.protocol, 'muted'));
      return card;
    }));
    $('agent-summary').replaceChildren(...agents.map(agent => {
      const chip = button(agent.agent, () => show('agents-page'));
      chip.className = 'agent-chip ' + (agent.available ? 'available' : 'unavailable');
      chip.title = agent.agent + ' · ' + (agent.available ? '可用' : '不可用') + ' · 查看 Agents'; return chip;
    }));
    $('agent-select').replaceChildren(...agents.map(agent => { const option = node('option', agent.agent + ' · ' + agent.protocol); option.value = agent.agent; return option; }));
    $('sessions').replaceChildren(...sessionData.sessions.map(session => node('p', session.session_id + ' · ' + session.agent + ' · ' + session.proc_state + ' · ' + session.cwd)));
    if (!sessionData.sessions.length) $('sessions').textContent = '暂无会话';
    await refresh();
    for (const [name, path] of Object.entries(snapshot.remote.workspaces)) {
      const option = node('option', name + (path ? '' : ' · 路径不可用')); option.value = name; option.disabled = !path; $('workspace').append(option);
    }
  } catch (error) { notice('登录未完成：' + error.message + '。请运行 agent-bridge ui。', true); }
}
document.querySelectorAll('nav button').forEach(item => item.addEventListener('click', () => show(item.dataset.page)));
$('filter').addEventListener('change', renderTasks);
$('refresh').addEventListener('click', refresh);
$('back').addEventListener('click', () => show('overview'));
$('more-transcript').addEventListener('click', () => loadTranscript().catch(error => notice(error.message, true)));
$('more-result').addEventListener('click', () => loadResult(true).catch(error => notice(error.message, true)));
$('workspace').addEventListener('change', () => { $('cwd').value = snapshot.remote.workspaces[$('workspace').value] || ''; });
$('logout').addEventListener('click', async () => {
  try { await api('/ui/logout', {}); authenticated = false; location.reload(); } catch (error) { notice(error.message, true); }
});
$('dispatch-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.target, submit = form.querySelector('[type=submit]');
  const fields = new FormData(form), agent = fields.get('agent');
  const body = {agent, message: fields.get('message'), user_requested: true};
  for (const key of ['model', 'effort', 'cwd']) if (fields.get(key)?.trim()) body[key] = fields.get(key).trim();
  submit.disabled = true; $('dispatch-status').textContent = '正在派发…';
  try {
    const isAsync = fields.has('async');
    if (!isAsync) delete body.agent;
    const result = await api(isAsync ? '/v1/tasks' : '/v1/agents/' + encodeURIComponent(agent) + '/invoke', body);
    $('dispatch-status').textContent = '已派发：' + result.task_id;
    await refresh(); await openTask({task_id: result.task_id, session_id: result.session_id});
  } catch (error) { $('dispatch-status').textContent = error.message; } finally { submit.disabled = false; }
});
initialize();
setInterval(refresh, 3000);
