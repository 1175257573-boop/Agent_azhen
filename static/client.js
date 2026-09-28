/* Atlas 控制台 —— 原生 JS，无构建步骤。
 *
 * 与 static/app.js 的分工：
 *     app.js     基础界面（原样保留，不动）
 *     client.js  控制台版：聊天行为与基础版一致，额外提供密钥管理面板
 *
 * 安全约定 —— 改这个文件时请一并遵守：
 *     1. 密钥只走 POST 请求体，绝不放进 URL / query string；
 *     2. 任何密钥都不写入 localStorage / sessionStorage / cookie，刷新即散；
 *     3. 输入框在提交成功后立刻清空，不在 DOM 里留明文；
 *     4. 显式查看的明文限时自动抹除，刷新密钥状态时也一并清掉。
 */

const S = {
  threadId: 'atlas-main',
  mode: 'chat',
  userId: 'demo',
  role: 'admin',
  busy: false,
  modes: {},
  enableMcp: false,
  queue: [],
  provider: 'fake',        // 本次对话使用的 provider
  providerTouched: false,  // 用户是否手动改过（改过就不再跟随服务端自动切换）
  creds: null,
  revealTimers: {},        // provider -> intervalId，用于"限时显示"倒计时
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/* ---------------------------------------------------------------- 基础工具 */

async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }

  if (!res.ok) {
    const detail = data && data.detail;
    const msg = typeof detail === 'string' ? detail : (detail ? JSON.stringify(detail) : `HTTP ${res.status}`);
    throw new Error(msg);
  }
  return data;
}

function toast(text, kind = '', ms = 3000) {
  const node = document.createElement('div');
  node.className = 'toast ' + kind;
  node.textContent = text;
  $('toasts').appendChild(node);
  setTimeout(() => node.remove(), ms);
}

let hintTimer = null;
function hint(text, ms = 2800) {
  const el = $('composerHint');
  if (!el.dataset.origin) el.dataset.origin = el.textContent.trim();
  el.textContent = text;
  clearTimeout(hintTimer);
  hintTimer = setTimeout(() => { el.textContent = el.dataset.origin; }, ms);
}

/* ---------------------------------------------------------------- 启动 */

async function boot() {
  const modes = await api('/api/modes');
  S.modes = modes;
  $('mode').innerHTML = Object.keys(modes).map((k) => `<option value="${k}">${k}</option>`).join('');
  $('mode').value = S.mode;
  $('modeDesc').textContent = modes[S.mode] || '';

  await Promise.all([loadInfo(), loadCreds(), loadMemory(), loadThreads(), loadHistory(), loadQueue()]);
  loadMcpTools(false);
}

/* ---------------------------------------------------------------- 系统信息 */

async function loadInfo() {
  const info = await api('/api/info');

  const modelPill = $('pillModel');
  modelPill.className = 'pill ' + (info.has_key || info.provider === 'fake' ? 'ok' : 'warn');
  modelPill.querySelector('.val').textContent = `${info.provider} / ${info.model}`;

  const keyPill = $('pillKey');
  const configured = (S.creds?.items || []).filter((i) => i.configured).length;
  keyPill.className = 'pill ' + (info.has_key ? 'ok' : 'warn');
  keyPill.querySelector('.val').textContent = info.has_key ? `已配置 ${configured} 个` : '未配置';

  $('envInfo').innerHTML = Object.entries(info.report || {})
    .map(([k, v]) => `<div class="kv"><span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`)
    .join('') || '<span class="hint">—</span>';
}

async function loadMemory() {
  try {
    const st = await api('/api/memory/status');
    const short = st.resolved?.short || st.short_term;
    const long = st.resolved?.long || st.long_term;
    const pill = $('pillMem');
    pill.className = 'pill ok';
    pill.querySelector('.val').textContent = `${short} / ${long}`;
  } catch {
    const pill = $('pillMem');
    pill.className = 'pill err';
    pill.querySelector('.val').textContent = '不可用';
  }
}

/* ================================================================ 密钥管理
 * 这一整块是整个界面的重心：用户填进去的东西，得能看清状态、能改、能删。
 * 但"能看清状态"和"能看到明文"是两件事——默认只给掩码，看明文要额外确认一次。
 */

async function loadCreds() {
  try {
    renderCreds(await api('/api/credentials'));
  } catch (e) {
    $('activeState').textContent = '读取失败';
    toast('密钥状态读取失败：' + e.message, 'err');
  }
}

function renderCreds(state) {
  S.creds = state;

  if (!S.providerTouched) S.provider = state.active_provider;

  // ---- 本次对话使用的模型
  const sel = $('providerSelect');
  const choices = [['fake', 'fake · 离线脚本模型（无需密钥）']].concat(
    state.items.map((i) => [i.provider, `${i.label}${i.configured ? '' : '（未配置密钥）'}`])
  );
  sel.innerHTML = choices.map(([v, t]) => `<option value="${v}">${esc(t)}</option>`).join('');
  sel.value = choices.some(([v]) => v === S.provider) ? S.provider : 'fake';

  // ---- 状态摘要
  const configured = state.items.filter((i) => i.configured);
  const stateEl = $('activeState');
  if (configured.length) {
    stateEl.textContent = `已配置 ${configured.length} 个 provider`;
    stateEl.className = 'v';
  } else {
    stateEl.textContent = '未配置（当前只能跑离线脚本模型）';
    stateEl.className = 'v';
    stateEl.style.color = 'var(--red)';
  }
  if (configured.length) stateEl.style.color = '';

  $('storageNote').textContent = state.storage_note || '—';
  $('storagePath').textContent = state.file_exists ? state.storage_path : `${state.storage_path}（尚未创建）`;

  // ---- 保密说明（文案由服务端统一维护，避免两边口径打架）
  $('secretNotes').innerHTML = (state.notes || []).map((n) => `<li>${esc(n)}</li>`).join('');

  // ---- 新增/更新表单的 provider 选项（记住当前选择，别把用户填到一半的选择重置掉）
  const keySel = $('keyProvider');
  const keep = keySel.value;
  keySel.innerHTML = state.items
    .map((i) => `<option value="${i.provider}">${esc(i.label)}${i.configured ? ' · 已配置' : ''}</option>`)
    .join('');
  if (keep && state.items.some((i) => i.provider === keep)) keySel.value = keep;

  renderCredList(state);
  loadInfo();
}

function originBadge(item) {
  if (!item.configured) return '<span class="badge err">未配置</span>';
  if (item.origin === 'env') return '<span class="badge accent">环境变量</span>';
  if (item.persistent) return '<span class="badge ok">已记住到本机</span>';
  return '<span class="badge warn">本次运行</span>';
}

function renderCredList(state) {
  const box = $('credList');
  box.innerHTML = '';

  state.items.forEach((item) => {
    const row = document.createElement('div');
    row.className = 'cred-row' + (item.provider === S.provider ? ' on' : '');

    const top = document.createElement('div');
    top.className = 'cred-top';
    top.innerHTML = `<span class="name">${esc(item.label)}</span>${originBadge(item)}`;

    const value = document.createElement('div');
    value.className = 'cred-value';
    value.dataset.provider = item.provider;
    value.textContent = item.configured ? item.masked : '—';

    const acts = document.createElement('div');
    acts.className = 'cred-acts';

    const meta = document.createElement('span');
    meta.className = 'cred-meta';
    meta.textContent = item.configured
      ? `${item.env_name} · ${item.length} 字符${item.shadowed ? ' · 覆盖了环境变量' : ''}`
      : item.env_name;
    acts.appendChild(meta);

    if (item.configured) {
      const revealBtn = document.createElement('button');
      revealBtn.className = 'btn tiny';
      revealBtn.textContent = '显示';
      revealBtn.onclick = () => onRevealClick(item, revealBtn, value);
      acts.appendChild(revealBtn);
    }

    const fillBtn = document.createElement('button');
    fillBtn.className = 'btn tiny';
    fillBtn.textContent = item.configured ? '更新' : '填入';
    fillBtn.onclick = () => {
      $('keyProvider').value = item.provider;
      $('keyInput').value = '';
      $('keyInput').focus();
      $('inspector').querySelector('.ins-scroll').scrollTop = 9999;
    };
    acts.appendChild(fillBtn);

    if (item.configured) {
      const delBtn = document.createElement('button');
      delBtn.className = 'btn tiny danger';
      delBtn.textContent = '清除';
      delBtn.onclick = () => removeKey(item);
      acts.appendChild(delBtn);
    }

    row.append(top, value, acts);
    box.appendChild(row);
  });
}

/* 「显示」两步走：第一次点只是把按钮变成「确认显示」，5 秒内再点一次才真的去取。
   这不是流程仪式——明文一旦回到浏览器，就等于多了一份副本，
   多一道确认能让"手滑点到"不至于变成"明文暴露在屏幕上"。 */
async function onRevealClick(item, btn, valueEl) {
  if (btn.dataset.stage !== 'confirm') {
    btn.dataset.stage = 'confirm';
    btn.textContent = '确认显示';
    btn.classList.add('danger');
    setTimeout(() => {
      if (btn.dataset.stage === 'confirm') {
        btn.dataset.stage = '';
        btn.textContent = '显示';
        btn.classList.remove('danger');
      }
    }, 5000);
    return;
  }

  btn.dataset.stage = '';
  btn.textContent = '显示';
  btn.classList.remove('danger');

  try {
    const data = await api('/api/credentials/reveal', { method: 'POST', body: { provider: item.provider } });
    showPlain(item.provider, data.api_key, data.expires_in || 15, valueEl);
  } catch (e) {
    toast('读取失败：' + e.message, 'err');
  }
}

function showPlain(provider, plain, seconds, valueEl) {
  stopRevealTimer(provider);
  let left = seconds;

  const paint = () => {
    valueEl.textContent = `${plain}（${left}s 后自动隐藏）`;
    valueEl.classList.add('revealed');
  };
  paint();

  S.revealTimers[provider] = setInterval(() => {
    left -= 1;
    if (left <= 0) {
      stopRevealTimer(provider);
      renderCredList(S.creds);   // 整体重渲染 = 明文从 DOM 里彻底消失
      return;
    }
    paint();
  }, 1000);
}

function stopRevealTimer(provider) {
  const id = S.revealTimers[provider];
  if (id) {
    clearInterval(id);
    delete S.revealTimers[provider];
  }
}

function clearAllRevealed() {
  Object.keys(S.revealTimers).forEach(stopRevealTimer);
}

async function saveKey() {
  const provider = $('keyProvider').value;
  const raw = $('keyInput').value;
  const remember = $('keyRemember').checked;

  if (!raw.trim()) {
    setResult('请先粘贴密钥', 'err');
    $('keyInput').focus();
    return;
  }

  setResult('正在保存…', 'info');
  try {
    const state = await api('/api/credentials', {
      method: 'POST',
      body: { provider, api_key: raw, remember },
    });

    $('keyInput').value = '';          // 立刻清空：明文不留在输入框里
    clearAllRevealed();
    S.providerTouched = true;
    S.provider = provider;
    renderCreds(state);

    setResult(
      `已保存并启用。${
        remember
          ? '已写入本机，服务重启后仍生效。'
          : '仅本次运行有效——服务重启后失效（勾选「记住到本机」可持久化）。'
      }建议点「测试连接」确认密钥真的可用。`,
      'ok'
    );
    toast('密钥已保存并启用', 'ok');
  } catch (e) {
    setResult('保存失败：' + e.message, 'err');
    toast('保存失败', 'err');
  }
}

async function verifyKey() {
  const provider = $('keyProvider').value;
  setResult('正在向模型发一次最小请求…', 'info');
  try {
    const out = await api('/api/credentials/verify', { method: 'POST', body: { provider } });
    if (out.ok) {
      setResult(`连接正常 · ${out.model} · ${out.latency_ms}ms${out.sample ? ` · 回显：${out.sample}` : ''}`, 'ok');
    } else {
      setResult(`连接失败：${out.message}`, 'err');
    }
  } catch (e) {
    setResult('测试失败：' + e.message, 'err');
  }
}

async function removeKey(item) {
  const ok = window.confirm(
    `确定清除「${item.label}」的密钥？\n\n` +
    '· 会从服务进程内存中移除；\n' +
    '· 若之前勾选过「记住到本机」，磁盘上的记录也会一并删除；\n' +
    '· 如果这个 provider 原本来自系统环境变量，会恢复成原值。'
  );
  if (!ok) return;

  try {
    const state = await api(`/api/credentials/${encodeURIComponent(item.provider)}?forget=true`, { method: 'DELETE' });
    clearAllRevealed();
    renderCreds(state);
    setResult(`已清除「${item.label}」的密钥。`, 'info');
    toast('已清除', 'ok');
  } catch (e) {
    toast('清除失败：' + e.message, 'err');
  }
}

function setResult(text, kind) {
  const box = $('keyResult');
  box.className = 'result ' + kind;
  box.textContent = text;
  box.hidden = false;
}

/* ================================================================ 聊天区 */

function scrollBottom() {
  const box = $('messages');
  box.scrollTop = box.scrollHeight;
}

function appendMessage(role, content, extra = {}) {
  const wrap = document.createElement('div');
  wrap.className = 'msg ' + (role === 'user' ? 'user' : 'assistant');

  const avatar = document.createElement('div');
  avatar.className = 'avatar';
  avatar.textContent = role === 'user' ? 'U' : 'A';

  const col = document.createElement('div');
  col.className = 'main-col';

  const who = document.createElement('div');
  who.className = 'who';
  who.textContent = role === 'user' ? '你' : (extra.name ? `工具 · ${extra.name}` : 'Atlas');
  col.appendChild(who);

  const text = document.createElement('div');
  text.className = 'text';
  text.textContent = content || '';
  col.appendChild(text);

  (extra.tool_calls || []).forEach((c) => col.appendChild(toolChip(c.name, true)));

  wrap.append(avatar, col);
  $('messages').appendChild(wrap);
  return { node: wrap, textEl: text, col };
}

function toolChip(name, done) {
  const chip = document.createElement('span');
  chip.className = 'tool-chip' + (done ? ' done' : '');
  chip.textContent = `${done ? '✓' : '⚙'} ${name}`;
  return chip;
}

function appendToolOutput(col, name, output) {
  const d = document.createElement('details');
  d.className = 'tool-out';
  const s = document.createElement('summary');
  s.textContent = `${name} 返回`;
  const pre = document.createElement('pre');
  pre.textContent = output;
  d.append(s, pre);
  col.appendChild(d);
}

function appendError(text) {
  const box = document.createElement('div');
  box.className = 'err-box';
  box.textContent = text;
  $('messages').appendChild(box);
  scrollBottom();
}

function appendHitl(interrupts) {
  const div = document.createElement('div');
  div.className = 'hitl';
  const items = interrupts.flatMap((i) => (i && i.action_requests) || []);
  div.innerHTML =
    '<div class="hitl-title">⏸ 需要你确认</div>' +
    items.map((a) => `<div class="req">${esc(a.name)} — <code>${esc(JSON.stringify(a.args))}</code></div>`).join('') +
    '<div class="acts">' +
      '<button class="btn tiny primary" data-act="approve">同意</button>' +
      '<button class="btn tiny danger" data-act="reject">拒绝</button>' +
    '</div>';
  $('messages').appendChild(div);
  div.querySelectorAll('button').forEach((b) => {
    b.onclick = () => {
      div.querySelectorAll('button').forEach((x) => { x.disabled = true; });
      resume(items.map(() => ({ type: b.dataset.act, message: b.dataset.act === 'reject' ? '用户在控制台拒绝了该操作' : undefined })));
    };
  });
  scrollBottom();
}

/* ---------------------------------------------------------------- 串流消费 */

function startAssistant() {
  return appendMessage('assistant', '', {});
}

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
      if (!part.startsWith('data: ')) continue;   // 心跳（": ping"）走到这里被忽略
      let ev;
      try { ev = JSON.parse(part.slice(6)); } catch { continue; }
      ctx = applyEvent(ev, ctx);
    }
  }

  if (!ctx.pending && !ctx.failed) ctx.textEl.textContent = '（无输出）';
}

function applyEvent(ev, ctx) {
  if (ev.type === 'token') {
    ctx.pending += ev.data;
    ctx.textEl.textContent = ctx.pending;
    scrollBottom();
  } else if (ev.type === 'tool_start') {
    ctx.col.appendChild(toolChip(ev.data.name, false));
    scrollBottom();
  } else if (ev.type === 'tool_end') {
    ctx.col.appendChild(toolChip(ev.data.name, true));
    if (ev.data.output) appendToolOutput(ctx.col, ev.data.name, ev.data.output);
    scrollBottom();
  } else if (ev.type === 'custom') {
    ctx.col.appendChild(toolChip(String(ev.data), false));
  } else if (ev.type === 'interrupt') {
    appendHitl(ev.data);
  } else if (ev.type === 'error') {
    ctx.failed = true;
    appendError('执行失败：' + ev.data);
  } else if (ev.type === 'queued_start') {
    if (!ctx.pending && !ctx.failed) ctx.textEl.textContent = '（无输出）';
    appendMessage('user', ev.data.text, {});
    S.queue = S.queue.filter((q) => q.id !== ev.data.id);
    renderQueue();
    return startAssistant();
  } else if (ev.type === 'done') {
    loadQueue();
  }
  return ctx;
}

function setBusy(on) {
  S.busy = on;
  // 忙碌时按钮不禁用 —— 点它是「排队」而不是「丢弃输入」
  $('btnSend').textContent = on ? '排队' : '发送';
  $('btnSend').classList.toggle('queuing', on);
}

async function send() {
  const text = $('input').value.trim();
  if (!text) return;
  $('input').value = '';
  $('input').style.height = 'auto';

  if (S.busy) {
    const ok = await enqueue(text);
    if (!ok) $('input').value = text;   // 入队失败就把内容还给用户，别吞掉
    return;
  }

  appendMessage('user', text, {});
  setBusy(true);

  try {
    const res = await fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message: text,
        thread_id: S.threadId,
        mode: S.mode,
        provider: S.provider,
        user_id: S.userId,
        role: S.role,
        enable_mcp: S.enableMcp,
      }),
    });

    if (!res.ok) {
      // 非流式错误（422 / 403 之类）：把服务端给的 detail 原样显示出来
      const body = await res.text();
      let msg = `HTTP ${res.status}`;
      try { msg = JSON.parse(body).detail || msg; } catch { /* 保持默认 */ }
      appendError('请求被拒绝：' + msg);
    } else {
      await consume(res, startAssistant());
    }
  } catch (e) {
    appendError('网络异常：' + e.message);
  }

  setBusy(false);
  loadThreads();
  loadMemory();
  loadCreds();
}

async function resume(decisions) {
  setBusy(true);
  try {
    const res = await fetch('/api/chat/resume', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        thread_id: S.threadId,
        mode: S.mode,
        provider: S.provider,
        user_id: S.userId,
        role: S.role,
        enable_mcp: S.enableMcp,
        decisions,
      }),
    });
    if (!res.ok) {
      const body = await res.text();
      let msg = `HTTP ${res.status}`;
      try { msg = JSON.parse(body).detail || msg; } catch { /* 保持默认 */ }
      appendError('恢复执行被拒绝：' + msg);
    } else {
      await consume(res, startAssistant());
    }
  } catch (e) {
    appendError('网络异常：' + e.message);
  }
  setBusy(false);
  loadThreads();
}

/* ---------------------------------------------------------------- 会话 */

async function loadThreads() {
  let list = [];
  try { list = await api('/api/chat/threads'); } catch { list = []; }
  if (!list.some((t) => t.thread_id === S.threadId)) {
    list.unshift({ thread_id: S.threadId, message_count: 0 });
  }
  $('threads').innerHTML = list.map((t) => `
    <div class="thread-item ${t.thread_id === S.threadId ? 'active' : ''}" data-t="${esc(t.thread_id)}">
      <span class="tid">${esc(t.thread_id)}</span>
      <span class="cnt">${t.message_count}</span>
      <span class="del" data-del="${esc(t.thread_id)}" title="删除会话">×</span>
    </div>`).join('');

  $('threads').querySelectorAll('.thread-item').forEach((el) => {
    el.onclick = (e) => {
      if (e.target.dataset.del) { removeThread(e.target.dataset.del); return; }
      switchThread(el.dataset.t);
    };
  });
}

async function removeThread(tid) {
  clearAllRevealed();
  await api(`/api/chat/threads/${encodeURIComponent(tid)}`, { method: 'DELETE' });
  if (tid === S.threadId) {
    S.threadId = 'atlas-main';
    $('threadLabel').textContent = S.threadId;
    await loadHistory();
  }
  loadThreads();
}

async function switchThread(tid) {
  clearAllRevealed();          // 切会话顺手把可能显示着的明文抹掉
  S.threadId = tid;
  $('threadLabel').textContent = tid;
  S.queue = [];                // 排队是会话级的，先清本地缓存避免串台
  await Promise.all([loadThreads(), loadHistory(), loadQueue()]);
}

async function loadHistory() {
  const url = `/api/chat/history?thread_id=${encodeURIComponent(S.threadId)}&mode=${S.mode}`
    + `&user_id=${encodeURIComponent(S.userId)}&role=${S.role}`;
  let msgs = [];
  try { msgs = await api(url); } catch { msgs = []; }
  $('messages').innerHTML = '';
  msgs.forEach((m) => appendMessage(m.role, m.content, m));
  scrollBottom();
}

/* ---------------------------------------------------------------- 排队 */

async function loadQueue() {
  try {
    S.queue = await api(`/api/chat/queue?thread_id=${encodeURIComponent(S.threadId)}`);
  } catch {
    S.queue = [];
  }
  renderQueue();
}

function renderQueue() {
  const box = $('queue');
  if (!S.queue.length) {
    box.hidden = true;
    box.innerHTML = '';
    return;
  }
  box.hidden = false;
  box.innerHTML =
    `<div class="queue-head"><span>待发送 ${S.queue.length} 条 · 当前回复结束后自动依次发送</span>` +
    '<span class="clear" id="queueClear">全部撤回</span></div>' +
    S.queue.map((q, i) => `
      <div class="queue-item">
        <span class="seq">${i + 1}</span>
        <span class="qi-text">${esc(q.text)}</span>
        <span class="act" data-edit="${q.id}" title="取回输入框修改">✎</span>
        <span class="act" data-del="${q.id}" title="撤回">×</span>
      </div>`).join('');

  box.querySelectorAll('[data-del]').forEach((n) => {
    n.onclick = async () => {
      await api(`/api/chat/queue/${n.dataset.del}?thread_id=${encodeURIComponent(S.threadId)}`, { method: 'DELETE' });
      loadQueue();
    };
  });
  box.querySelectorAll('[data-edit]').forEach((n) => {
    n.onclick = async () => {
      const item = S.queue.find((q) => q.id === n.dataset.edit);
      if (!item) return;
      await api(`/api/chat/queue/${n.dataset.edit}?thread_id=${encodeURIComponent(S.threadId)}`, { method: 'DELETE' });
      $('input').value = item.text;
      $('input').focus();
      loadQueue();
    };
  });
  $('queueClear').onclick = async () => {
    await api(`/api/chat/queue?thread_id=${encodeURIComponent(S.threadId)}`, { method: 'DELETE' });
    loadQueue();
  };
}

async function enqueue(text) {
  try {
    const res = await fetch('/api/chat/queue', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ thread_id: S.threadId, message: text }),
    });
    if (res.status === 429) {
      hint('排队已满（20 条），等当前回复结束后再发');
      return false;
    }
    if (!res.ok) return false;
    S.queue.push(await res.json());
    renderQueue();
    hint('已加入队列，当前回复结束后自动发送');
    return true;
  } catch {
    return false;
  }
}

/* ---------------------------------------------------------------- MCP */

async function loadMcpTools(connect) {
  const box = $('mcpTools');
  const mcpPill = $('pillMcp');

  if (!S.enableMcp) {
    box.innerHTML = '<span class="hint">未连接（勾选后自动连接）</span>';
    mcpPill.className = 'pill';
    mcpPill.querySelector('.val').textContent = '关闭';
    return;
  }

  box.innerHTML = '<span class="hint">连接中…</span>';
  mcpPill.className = 'pill warn';
  mcpPill.querySelector('.val').textContent = '连接中';

  try {
    const d = await api(`/api/chat/mcp-tools?connect=${connect ? 'true' : 'false'}&mode=${S.mode}&user_id=${encodeURIComponent(S.userId)}`);
    const tools = d.tools || [];
    if (!tools.length) {
      box.innerHTML = '<span class="hint">MCP Server 未提供工具</span>';
      mcpPill.className = 'pill warn';
      mcpPill.querySelector('.val').textContent = '无工具';
      return;
    }
    box.innerHTML = tools.map((t) => `<span class="chip">${esc(t)}</span>`).join('');
    mcpPill.className = 'pill ok';
    mcpPill.querySelector('.val').textContent = `${tools.length} 个工具`;
  } catch (e) {
    box.innerHTML = `<span class="hint">连接失败：${esc(e.message)}</span>`;
    mcpPill.className = 'pill err';
    mcpPill.querySelector('.val').textContent = '失败';
  }
}

/* ---------------------------------------------------------------- 事件绑定 */

$('btnSend').onclick = send;
$('input').onkeydown = (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
  e.target.style.height = 'auto';
  e.target.style.height = Math.min(e.target.scrollHeight, 160) + 'px';
};

$('btnNewThread').onclick = async () => {
  const t = await api('/api/chat/threads', { method: 'POST' });
  switchThread(t.thread_id);
};

$('btnReload').onclick = () => Promise.all([loadHistory(), loadThreads(), loadQueue()]);

$('mode').onchange = () => {
  S.mode = $('mode').value;
  $('modeDesc').textContent = S.modes[S.mode] || '';
  $('modeBadge').textContent = S.enableMcp ? `${S.mode} + MCP` : S.mode;
  // MCP 工具按 (mode, user) 缓存装配，换模式后清单要跟着换
  if (S.enableMcp) loadMcpTools(true);
  loadHistory();
};

$('mcpToggle').onchange = () => {
  S.enableMcp = $('mcpToggle').checked;
  $('modeBadge').textContent = S.enableMcp ? `${S.mode} + MCP` : S.mode;
  loadMcpTools(true);
};

$('userId').onchange = () => { S.userId = $('userId').value || 'demo'; };
$('role').onchange = () => { S.role = $('role').value; };

// ---- 密钥面板
$('providerSelect').onchange = () => {
  S.provider = $('providerSelect').value;
  S.providerTouched = true;
  renderCredList(S.creds);
  toast(`本次对话改用 ${S.provider}`, '', 1800);
};

$('btnSaveKey').onclick = saveKey;
$('btnVerify').onclick = verifyKey;

$('btnEye').onclick = () => {
  const input = $('keyInput');
  input.type = input.type === 'password' ? 'text' : 'password';
  input.focus();
};

$('keyInput').onkeydown = (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    saveKey();
  }
};

$('btnKeys').onclick = () => {
  $('inspector').classList.add('open');
  $('inspector').querySelector('.ins-scroll').scrollTop = 0;
  $('keyInput').focus();
};

$('btnCloseIns').onclick = () => $('inspector').classList.remove('open');

// 离开页面时把还在显示的明文清掉（配合服务端的 no-store，不留下任何副本）
window.addEventListener('beforeunload', clearAllRevealed);

boot().catch((e) => {
  appendError('初始化失败：' + e.message);
  toast('初始化失败：' + e.message, 'err', 6000);
});
