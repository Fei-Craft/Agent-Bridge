'use strict';
const $ = id => document.getElementById(id);
const labels = {running: '运行中', queued: '排队中', completed: '已完成', cancelled: '已取消', failed: '失败'};
let snapshot = {tasks: [], remote: {workspaces: {}}, approvals: [], audit: []};
let agents = [], selected = null, busy = false, page = 'overview', transcriptOffset = 0, resultCursor = 0, authenticated = false;

function node(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (className) item.className = className;
  return item;
}
function notice(text, error = false) { $('notice').textContent = text; $('notice').className = error ? 'error' : ''; }
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
  document.querySelectorAll('nav button').forEach(item => item.classList.toggle('active', item.dataset.page === name));
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
  return '通知: ' + (task.caller_delivery?.status || '—') + ' / 回调: ' + (task.callback_delivery?.status || '—');
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
    row.append(title, node('td', task.agent), state, node('td', delivery(task)), actions);
    return row;
  });
  if (!rows.length) { const cell = node('td', undefined, 'empty'); cell.colSpan = 5; cell.append(node('p', '还没有匹配的任务'), node('p', '从一个清晰的目标开始，让代理替你完成工作。', 'muted small'), button('派发第一个任务', () => show('dispatch'))); const row = node('tr'); row.append(cell); rows.push(row); }
  $('tasks').replaceChildren(...rows);
}
function renderGateway() {
  const remote = snapshot.remote;
  $('gateway-status').textContent = '网关: ' + (remote.enabled ? '已启用（独立回环端口）' : '已关闭') + '\n公共地址: ' + (remote.public_url || '未配置') +
    '\n允许代理: ' + (remote.agents.join('、') || '无') + '\n命名工作区: ' + JSON.stringify(remote.workspaces) + '\n本机审批: ' + (remote.require_approval ? '开启' : '关闭');
  $('approvals').replaceChildren(...snapshot.approvals.map(item => {
    const card = node('div', undefined, 'card approval');
    card.append(node('strong', item.agent + ' → ' + item.workspace), node('p', item.message), node('p', item.approval_id, 'muted small'));
    for (const [action, text] of [['approve', '批准执行'], ['deny', '拒绝']]) card.append(button(text, async () => {
      await api('/v1/approvals/' + item.approval_id + '/' + action, {}); await refresh();
    }));
    return card;
  }));
  if (!snapshot.approvals.length) $('approvals').textContent = '暂无待审批调用';
  $('audit').textContent = snapshot.audit.map(item => JSON.stringify(item)).join('\n') || '暂无审计记录';
}
async function loadTranscript() {
  if (!selected) return;
  if (selected.native_search) {
    $('transcript').textContent = '原生搜索的内部会话不显示记录。';
    $('more-transcript').hidden = true;
    return;
  }
  const taskId = selected.task_id;
  const data = await api('/v1/sessions/' + selected.session_id + '/transcript?offset=' + transcriptOffset + '&limit=100');
  if (selected?.task_id !== taskId) return;
  const events = data.events || [];
  if (events.length) {
    $('transcript').textContent += events.map(event => JSON.stringify(event)).join('\n') + '\n';
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
  $('detail-meta').textContent = '代理: ' + data.agent + ' · 状态: ' + (labels[data.status] || data.status) + '\n' + delivery(data) +
    '\n远程: ' + JSON.stringify(data.remote || {}) + '\n文件: ' + JSON.stringify(data.files_changed || []);
  if (append) $('result').textContent += data.result_text || '';
  else $('result').textContent = data.result_text || data.error || '任务尚未产生结果';
  resultCursor = data.next_cursor || 0;
  $('more-result').hidden = !data.has_more;
}
async function openTask(task) {
  selected = task; transcriptOffset = 0; resultCursor = 0;
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
    $('agent-summary').replaceChildren(...agents.map(agent => node('span', (agent.available ? '● ' : '○ ') + agent.agent, 'badge ' + (agent.available ? 'completed' : ''))));
    $('agent-select').replaceChildren(...agents.map(agent => { const option = node('option', agent.agent + ' · ' + agent.protocol); option.value = agent.agent; return option; }));
    $('sessions').replaceChildren(...sessionData.sessions.map(session => node('p', session.session_id + ' · ' + session.agent + ' · ' + session.proc_state + ' · ' + session.cwd)));
    if (!sessionData.sessions.length) $('sessions').textContent = '暂无会话';
    await refresh();
    for (const [name] of Object.entries(snapshot.remote.workspaces)) { const option = node('option', name); option.value = name; $('workspace').append(option); }
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
  const body = {agent, message: fields.get('message')};
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
