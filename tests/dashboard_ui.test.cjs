const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/agent_bridge/share/ui/app.js'), 'utf8');

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.value = ''; this._text = ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  get childElementCount() { return this.children.length; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  addEventListener() {}
}
function setup() {
  const elements = new Map();
  const context = vm.createContext({Node: Element, URLSearchParams, Date,
    document: {createElement: tag => new Element(tag), querySelectorAll: () => [], getElementById: id => {
      if (!elements.has(id)) elements.set(id, new Element('div'));
      return elements.get(id);
    }}, location: {hash: ''}, history: {replaceState() {}}, setInterval() {}, fetch: () => new Promise(() => {})});
  vm.runInContext(source, context);
  return {run: code => vm.runInContext(code, context), get: id => elements.get(id), context};
}

test('timeline groups adjacent chunks across polls, preserving text and compact tool rows', () => {
  const {run, get} = setup();
  run(`appendTranscript([{ts:'2026-06-05T09:10:11Z',type:'prompt_sent',data:{text:'检查项目'}},
    {ts:'2026-06-05T09:10:12Z',type:'message_chunk',data:{text:''}},
    {ts:'2026-06-05T09:10:12Z',type:'message_chunk',data:{text:'已经'}}]);
    appendTranscript([{ts:'2026-06-05T09:10:13Z',type:'message_chunk',data:{text:'完成。'}},
    {ts:'2026-06-05T09:10:14Z',type:'tool_call',data:{title:'读取 README.md',status:'completed'}}]);`);
  const rows = get('transcript').children;
  assert.equal(rows.length, 3);
  assert.match(rows[0].textContent, /任务指令检查项目/);
  assert.match(rows[1].textContent, /代理回复已经完成。/);
  assert.match(rows[2].textContent, /工具调用读取 README.md · 已完成/);
  assert.match(rows[2].className, /tool-event/);
  assert.doesNotMatch(get('transcript').textContent, /"data"|"type"/);
});
test('timeline reads nested provider text, error events and hostile text as text only', () => {
  const {run, get} = setup();
  run(`appendTranscript([{type:'message_chunk',data:{payload:{item:{text:'<img src=x onerror=alert(1)>'}}}},
    {type:'error',data:{text:'请求失败'}},{type:'unknown_event',data:{}}]);`);
  assert.match(get('transcript').textContent, /<img src=x onerror=alert\(1\)>/);
  assert.equal(get('transcript').children[0].children[1].children.length, 0);
  assert.match(get('transcript').textContent, /错误请求失败/);
  assert.match(get('transcript').textContent, /unknown_event事件已记录/);
});
test('metadata renders labeled values and pills, hiding empty remote/file fields', () => {
  const {run, get} = setup();
  run(`renderMetadata({agent:'fake',status:'completed',task_id:'task_1',session_id:'session_1',
    model:'big-pickle',elapsed_sec:0,remote:{},files_changed:[]});`);
  assert.match(get('detail-meta').textContent, /模型big-pickle/);
  assert.match(get('detail-meta').textContent, /耗时0 秒/);
  assert.doesNotMatch(get('detail-meta').textContent, /远程|变更文件|交付状态|\{\}|\[\]/);
  run(`renderMetadata({status:'failed',error:'执行失败',remote:{url:'https://example.invalid/run',branches:['fix/ui']},
    files_changed:['app.js'],callback_delivery:{status:'failed',error:'拒绝连接'}});`);
  assert.match(get('detail-meta').textContent, /回调错误拒绝连接/);
  assert.match(get('detail-meta').textContent, /远程地址https:\/\/example.invalid\/run/);
  assert.match(get('detail-meta').textContent, /变更文件app.js/);
  assert.match(get('detail-meta').textContent, /错误执行失败/);
});
test('native search detail shows partial results and errors without reading internal transcripts', async () => {
  const {run, get, context} = setup();
  const result = {ok: true, task_id: 'task_search', agent: 'codex', status: 'failed',
    error: 'Search requires attributable sources.'};
  const requests = [];
  context.fetch = async url => {
    requests.push(url);
    const body = {
      '/v1/tasks/task_search/result?cursor=0': {...result, result_text: 'Partial findings', next_cursor: 16, has_more: true},
      '/v1/tasks/task_search/result?cursor=16': {...result, result_text: ' with details'},
      '/ui/data': {ok: true, tasks: [], audit: [], approvals: [], remote: {agents: [], workspaces: {}}},
      '/v1/sessions': {ok: true, sessions: []},
    }[url];
    assert.ok(body, 'Unexpected request: ' + url);
    return {ok: true, json: async () => body};
  };
  await run("openTask({task_id:'task_search',session_id:'sess_search',native_search:true})");
  assert.equal(get('result').textContent, 'Partial findings');
  assert.match(get('detail-meta').textContent, /Search requires attributable sources/);
  assert.equal(get('more-transcript').hidden, true);
  await run('loadResult(true)');
  assert.equal(get('result').textContent, 'Partial findings with details');
  assert.match(get('detail-meta').textContent, /Search requires attributable sources/);
  await run('authenticated = true; refresh()');
  assert.equal(requests.filter(url => url.endsWith('cursor=0')).length, 2);
  assert.equal(requests.some(url => url.includes('/transcript')), false);
});
test('gateway uses workspace list, audit columns and outcome pills instead of JSON', () => {
  const {run, get} = setup();
  run(`snapshot.remote={enabled:true,require_approval:true,public_url:'',agents:['fake'],workspaces:{project:'/repo'}};
    snapshot.audit=[{timestamp:'2026-06-05T09:10:11Z',token_id:'123abc',remote_ip:'127.0.0.1',
      route:'/v1/tasks',agent:'fake',workspace:'project',outcome:'awaiting_approval'}]; renderGateway();`);
  assert.match(get('gateway-status').textContent, /网关状态已启用/);
  assert.match(get('gateway-status').textContent, /project→\/repo/);
  const table = get('audit').children[0];
  assert.equal(table.tagName, 'table');
  assert.equal(table.children[0].children[0].children.length, 7);
  assert.match(table.textContent, /123abc127.0.0.1\/v1\/tasksfakeproject等待审批/);
  assert.doesNotMatch(table.textContent, /"outcome"|"workspace"/);
  run(`snapshot.audit=[{timestamp:'2026-06-05T09:10:11Z',outcome:'invalid or expired token'}]; renderAudit();`);
  assert.equal(get('audit').children[0].children[0].children[0].children.length, 2);
  assert.match(get('audit').textContent, /凭据无效或过期/);
});
test('empty gateway, approvals, audit and task list have quiet designed states', () => {
  const {run, get} = setup();
  run(`snapshot.remote={enabled:false,require_approval:false,public_url:'',agents:[],workspaces:{}};
    renderGateway(); renderTasks();`);
  assert.match(get('gateway-status').textContent, /未启用/);
  assert.match(get('gateway-status').textContent, /命名工作区未配置/);
  assert.equal(get('audit').textContent, '暂无审计记录');
  assert.equal(get('approvals').textContent, '暂无待审批调用');
  assert.match(get('tasks').textContent, /暂无匹配的任务/);
  assert.equal(run('delivery({})'), '');
  assert.equal(run("delivery({caller_delivery:{status:'delivered'}})"), '通知 · 已送达');
});
test('API errors remain visible and expired authentication stops refresh', async () => {
  const {run, get, context} = setup();
  context.fetch = async () => ({ok: false, status: 401, json: async () => ({ok:false,error:'登录已过期'})});
  run('authenticated = true');
  const error = await run("api('/ui/data').catch(error => { notice(error.message, true); return error.message; })");
  assert.equal(error, '登录已过期');
  assert.equal(get('notice').className, 'error');
  assert.equal(run('authenticated'), false);
});
