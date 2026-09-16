/* Atlas Web UI —— 原生 JS，无构建步骤。
   服务端用 SSE 推事件：token / tool_start / tool_end / custom / interrupt / done / error */

const state = {
  threadId: 'atlas-main',
  mode: 'chat',
  userId: 'demo',
  role: 'admin',
  busy: false,
  modes: {},
  enableMcp: false,   // MCP 开关：打开后本地工具 + MCP 工具一起交给模型
  queue: [],          // 排队中的输入（Agent 忙碌时先缓存，本轮结束后自动发出）
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/* ---------------------------------------------------------------- 初始化 */
async function boot() {
  const modes = await fetch('/api/modes').then((r) => r.json());
  state.modes = modes;
  $('mode').innerHTML = Object.entries(modes)
    .map(([k, v]) => `<option value="${k}">${k}</option>`)
    .join('');
  $('mode').value = state.mode;
  $('modeDesc').textContent = modes[state.mode] || '';

  await Promise.all([loadInfo(), loadMemory(), loadThreads(), loadHistory(), loadQueue()]);
  loadMcpTools(false);   // 后端若已连过 MCP，这里会直接显示工具清单
}

/* ---------------------------------------------------------------- 信息区 */
async function loadInfo() {
  const info = await fetch('/api/info').then((r) => r.json());
  $('modelMeta').textContent = `${info.provider} / ${info.model}`;
  const badge = $('memBadge');
  badge.textContent = `Key ${info.has_key ? '已配置' : '未配置'}`;
  badge.className = 'badge ' + (info.has_key ? 'ok' : 'warn');
  $('envInfo').innerHTML = Object.entries(info.report || {})
    .map(([k, v]) => `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`)
    .join('');
}

async function loadMemory() {
  const st = await fetch('/api/memory/status').then((r) => r.json());
  const rows = [
    ['短期记忆后端', st.resolved?.short || st.short_term],
    ['长期记忆后端', st.resolved?.long || st.long_term],
    ['消息窗口', st.window + ' 条'],
  ];
  $('memStatus').innerHTML = rows
    .map(([k, v]) => `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`)
    .join('');
  await loadPrefs();
}

async function loadPrefs() {
  const prefs = await fetch(`/api/memory/preferences?user_id=${encodeURIComponent(state.userId)}`).then((r) => r.json());
  $('prefs').innerHTML = prefs.length
    ? prefs.map((p) => `<div class="pref-item"><span class="pk">${esc(p.key)}</span><span class="pv">${esc(p.value)}</span><span class="rm" data-k="${esc(p.key)}">×</span></div>`).join('')
    : '<div class="empty">暂无偏好</div>';
  $('prefs').querySelectorAll('.rm').forEach((el) => {
    el.onclick = async () => {
      await fetch(`/api/memory/preferences?user_id=${encodeURIComponent(state.userId)}&key=${encodeURIComponent(el.dataset.k)}`, { method: 'DELETE' });
      loadPrefs();
    };
  });
}

/* ---------------------------------------------------------------- MCP */
async function loadMcpTools(connect) {
  const box = $('mcpTools');
  if (!state.enableMcp) {
    box.className = 'empty';
    box.textContent = '未连接（勾选后自动连接）';
    return;
  }
  box.className = 'empty';
  box.textContent = '连接中…';
  try {
    const r = await fetch(`/api/chat/mcp-tools?connect=${connect ? 'true' : 'false'}&mode=${state.mode}&user_id=${encodeURIComponent(state.userId)}`);
    if (!r.ok) throw new Error((await r.json()).detail || r.status);
    const d = await r.json();
    const tools = d.tools || [];
    if (!tools.length) { box.className = 'empty'; box.textContent = 'MCP Server 未提供工具'; return; }
    box.className = 'mcp-list';
    box.innerHTML = tools.map((t) => `<span class="mcp-chip">${esc(t)}</span>`).join('');
  } catch (e) {
    box.className = 'empty';
    box.textContent = 'MCP 连接失败：' + e.message;
  }
}

/* ---------------------------------------------------------------- 会话 */
async function loadThreads() {
  let list = [];
  try { list = await fetch('/api/chat/threads').then((r) => r.json()); } catch { list = []; }
  if (!list.some((t) => t.thread_id === state.threadId)) {
    list.unshift({ thread_id: state.threadId, message_count: 0 });
  }
  $('threads').innerHTML = list
    .map((t) => `<div class="thread-item ${t.thread_id === state.threadId ? 'active' : ''}" data-t="${esc(t.thread_id)}">
        <span class="tid">${esc(t.thread_id)}</span>
        <span class="cnt">${t.message_count}</span>
        <span class="del" data-del="${esc(t.thread_id)}">×</span>
      </div>`)
    .join('');
  $('threads').querySelectorAll('.thread-item').forEach((el) => {
    el.onclick = (e) => {
      if (e.target.dataset.del) { removeThread(e.target.dataset.del); return; }
      switchThread(el.dataset.t);
    };
  });
}

async function removeThread(tid) {
  await fetch(`/api/chat/threads/${encodeURIComponent(tid)}`, { method: 'DELETE' });
  if (tid === state.threadId) { state.threadId = 'atlas-main'; await loadHistory(); }
  loadThreads();
}

async function switchThread(tid) {
  state.threadId = tid;
  $('threadLabel').textContent = tid;
  await loadThreads();
  await loadHistory();
  // 排队是会话级的，切会话要跟着换（顺便清掉本地缓存，避免串台）
  state.queue = [];
  await loadQueue();
}

async function loadHistory() {
  const url = `/api/chat/history?thread_id=${encodeURIComponent(state.threadId)}&mode=${state.mode}&user_id=${encodeURIComponent(state.userId)}&role=${state.role}`;
  const msgs = await fetch(url).then((r) => r.json()).catch(() => []);
  $('messages').innerHTML = '';
  msgs.forEach((m) => appendMessage(m.role, m.content, m));
  scrollBottom();
}

/* ---------------------------------------------------------------- 渲染 */
function scrollBottom() {
  const box = $('messages');
  box.scrollTop = box.scrollHeight;
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function appendMessage(role, content, extra = {}) {
  const wrap = el('div', 'msg ' + (role === 'user' ? 'user' : 'assistant'));
  wrap.appendChild(el('div', 'avatar', role === 'user' ? 'U' : 'A'));

  const body = el('div', 'body');
  body.appendChild(el('div', 'who', role === 'user' ? '你' : 'Atlas'));

  if (extra.name) {
    // 工具回执：用小字等宽块展示，不占正文
    body.appendChild(el('div', 'tool-out', content || ''));
  } else {
    body.appendChild(el('div', 'text', content || ''));
  }

  (extra.tool_calls || []).forEach((c) => {
    const row = el('div');
    row.appendChild(el('span', 'tool-chip', '⚙ ' + c.name));
    body.appendChild(row);
  });

  wrap.appendChild(body);
  $('messages').appendChild(wrap);
  return wrap;
}

function appendTool(name, phase, output) {
  const box = $('messages');
  let last = box.querySelector('.msg.assistant:last-child');
  if (!last) { last = appendMessage('assistant', '', {}); }
  const chip = document.createElement('div');
  chip.innerHTML = `<span class="tool-chip ${phase === 'end' ? 'done' : ''}">${phase === 'end' ? '✓' : '⚙'} ${esc(name)}</span>`;
  last.querySelector('.body').appendChild(chip);
  if (output) {
    const out = document.createElement('div');
    out.className = 'tool-out';
    out.textContent = output;
    last.querySelector('.body').appendChild(out);
  }
  scrollBottom();
}

function appendHitl(interrupts) {
  const box = $('messages');
  const div = document.createElement('div');
  div.className = 'hitl';
  const items = interrupts.flatMap((i) => (i && i.action_requests) || []);
  div.innerHTML = `<div><strong>⏸ 需要人工确认</strong></div>` +
    items.map((a) => `<div style="font-size:12px;margin-top:6px">${esc(a.name)} — <code>${esc(JSON.stringify(a.args))}</code></div>`).join('') +
    `<div class="acts">
       <button class="btn tiny" data-act="approve">同意</button>
       <button class="btn tiny" data-act="reject">拒绝</button>
     </div>`;
  box.appendChild(div);
  div.querySelectorAll('button').forEach((b) => {
    b.onclick = () => resume(items.map(() => ({ type: b.dataset.act, message: b.dataset.act === 'reject' ? '用户在网页端拒绝' : undefined })));
  });
  scrollBottom();
}

/* ---------------------------------------------------------------- 排队消息
   Agent 一轮要跑几十秒，这期间用户的输入不能丢、也不能并发打断当前轮。
   做法：本地 + 服务端各存一份（服务端是真相源），本轮 done 后由服务端
   按先进先出自动执行，事件流里会给出 queued_start 通知前端「这条开始发了」。 */

async function loadQueue() {
  const items = await fetch(`/api/chat/queue?thread_id=${encodeURIComponent(state.threadId)}`).then((r) => r.json());
  state.queue = items;
  renderQueue();
}

function renderQueue() {
  const box = $('queue');
  if (!state.queue.length) {
    box.hidden = true;
    box.innerHTML = '';
    return;
  }
  box.hidden = false;
  box.innerHTML =
    `<div class="queue-head"><span>待发送 ${state.queue.length} 条 · 当前回复结束后自动依次发送</span>` +
    `<span class="queue-clear" id="queueClear">全部撤回</span></div>` +
    state.queue.map((q, i) =>
      `<div class="queue-item">
         <span class="qi-seq">${i + 1}</span>
         <span class="qi-text">${esc(q.text)}</span>
         <span class="qi-edit" data-id="${q.id}" title="取回输入框修改">✎</span>
         <span class="qi-del" data-id="${q.id}" title="撤回">×</span>
       </div>`).join('');

  box.querySelectorAll('.qi-del').forEach((n) => {
    n.onclick = async () => {
      await fetch(`/api/chat/queue/${n.dataset.id}?thread_id=${encodeURIComponent(state.threadId)}`, { method: 'DELETE' });
      loadQueue();
    };
  });
  box.querySelectorAll('.qi-edit').forEach((n) => {
    n.onclick = async () => {
      const item = state.queue.find((q) => q.id === n.dataset.id);
      if (!item) return;
      // 取回输入框 = 删除排队项 + 内容回填，改完再回车即重新入队
      await fetch(`/api/chat/queue/${n.dataset.id}?thread_id=${encodeURIComponent(state.threadId)}`, { method: 'DELETE' });
      $('input').value = item.text;
      $('input').focus();
      loadQueue();
    };
  });
  $('queueClear').onclick = async () => {
    await fetch(`/api/chat/queue?thread_id=${encodeURIComponent(state.threadId)}`, { method: 'DELETE' });
    loadQueue();
  };
}

async function enqueue(text) {
  const r = await fetch('/api/chat/queue', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ thread_id: state.threadId, message: text }),
  });
  if (r.status === 429) {
    hint('排队已满（20 条），等当前回复结束后再发');
    return false;
  }
  if (!r.ok) return false;
  state.queue.push(await r.json());
  renderQueue();
  hint('已加入队列，当前回复结束后自动发送');
  return true;
}

let hintTimer = null;
function hint(text, ms = 2600) {
  const el = $('composerHint');
  if (!el.dataset.origin) el.dataset.origin = el.textContent;
  el.textContent = text;
  clearTimeout(hintTimer);
  hintTimer = setTimeout(() => { el.textContent = el.dataset.origin; }, ms);
}

/* ---------------------------------------------------------------- 发送 */
function startAssistant() {
  const node = appendMessage('assistant', '', {});
  return { node, textEl: node.querySelector('.text'), pending: '' };
}

/** 消费一条 SSE 流；ctx 是当前 assistant 气泡，queued_start 会换成新气泡 */
async function consume(res, ctx) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const parts = buf.split('\n\n');
    buf = parts.pop();
    for (const part of parts) {
      if (!part.startsWith('data: ')) continue;
      const ev = JSON.parse(part.slice(6));
      ctx = applyEvent(ev, ctx);
    }
  }
  if (!ctx.pending) ctx.textEl.textContent = '（无输出）';
}

function applyEvent(ev, ctx) {
  if (ev.type === 'token') {
    ctx.pending += ev.data;
    ctx.textEl.textContent = ctx.pending;
    scrollBottom();
  } else if (ev.type === 'tool_start') {
    appendTool(ev.data.name, 'start');
  } else if (ev.type === 'tool_end') {
    appendTool(ev.data.name, 'end', ev.data.output);
  } else if (ev.type === 'custom') {
    appendTool(ev.data, 'start');
  } else if (ev.type === 'interrupt') {
    appendHitl(ev.data);
  } else if (ev.type === 'error') {
    ctx.textEl.textContent = `[出错] ${ev.data}`;
  } else if (ev.type === 'queued_start') {
    // 排队消息开始执行：补完上一条回复，插入正式的用户气泡，再开一条新的 assistant
    if (!ctx.pending) ctx.textEl.textContent = '（无输出）';
    appendMessage('user', ev.data.text, {});
    state.queue = state.queue.filter((q) => q.id !== ev.data.id);
    renderQueue();
    return startAssistant();
  } else if (ev.type === 'done') {
    loadQueue();
  }
  return ctx;
}

function setBusy(on) {
  state.busy = on;
  // 忙碌时按钮不禁用——点它是「排队」而不是「丢弃输入」
  $('send').textContent = on ? '排队' : '发送';
  $('send').classList.toggle('queuing', on);
}

async function send() {
  const text = $('input').value.trim();
  if (!text) return;
  $('input').value = '';
  $('input').style.height = 'auto';

  // Agent 正在工作：入队，等本轮结束自动发出（WorkBuddy 的排队行为）
  if (state.busy) {
    const ok = await enqueue(text);
    if (!ok) $('input').value = text;   // 入队失败就把内容还给用户，别吞掉
    return;
  }

  appendMessage('user', text, {});
  setBusy(true);

  const res = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message: text,
      thread_id: state.threadId,
      mode: state.mode,
      user_id: state.userId,
      role: state.role,
      enable_mcp: state.enableMcp,
    }),
  });
  await consume(res, startAssistant());

  setBusy(false);
  loadThreads();
  loadMemory();
}

async function resume(decisions) {
  const res = await fetch('/api/chat/resume', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      thread_id: state.threadId,
      mode: state.mode,
      user_id: state.userId,
      role: state.role,
      enable_mcp: state.enableMcp,
      decisions,
    }),
  });
  setBusy(true);
  await consume(res, startAssistant());
  setBusy(false);
  loadThreads();
}

/* ---------------------------------------------------------------- 事件绑定 */
$('send').onclick = send;
$('input').onkeydown = (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  e.target.style.height = 'auto';
  e.target.style.height = Math.min(e.target.scrollHeight, 160) + 'px';
};
$('newThread').onclick = async () => {
  const t = await fetch('/api/chat/threads', { method: 'POST' }).then((r) => r.json());
  switchThread(t.thread_id);
};
$('mode').onchange = () => {
  state.mode = $('mode').value;
  $('modeDesc').textContent = state.modes[state.mode] || '';
  $('modeBadge').textContent = state.enableMcp ? state.mode + ' + MCP' : state.mode;
  // MCP 工具按 (mode, user) 缓存装配，换模式后清单要跟着换，
  // 否则前端显示的还是上一个模式的工具列表。
  if (state.enableMcp) loadMcpTools(true);
  loadHistory();
};
$('mcpToggle').onchange = () => {
  state.enableMcp = $('mcpToggle').checked;
  $('modeBadge').textContent = state.enableMcp ? state.mode + ' + MCP' : state.mode;
  loadMcpTools(true);
};
$('userId').onchange = () => { state.userId = $('userId').value || 'demo'; loadPrefs(); };
$('role').onchange = () => { state.role = $('role').value; };
$('prefAdd').onclick = async () => {
  const k = $('prefKey').value.trim(), v = $('prefVal').value.trim();
  if (!k || !v) return;
  await fetch('/api/memory/preferences', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: state.userId, key: k, value: v }),
  });
  $('prefKey').value = ''; $('prefVal').value = '';
  loadPrefs();
};

boot();
