let currentSession = null;
let sending = false;

const $ = (sel) => document.querySelector(sel);
const chatScroll = $('#chat-scroll');
const sessionList = $('#session-list');
const wbBody = $('#wb-body');
const saPanel = $('#subagent-panel');
const input = $('#input');
const sendBtn = $('#send');
const emptyState = $('#empty-state');
const tokenRing = $('#token-ring');
const ringProgress = $('#ring-progress');

let abortController = null;

const SEND_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none"><path d="M4.5 12L20 4l-3.5 16-4.5-5.5L4.5 12z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>';
const STOP_ICON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/></svg>';

function setSendBtnState(active) {
  if (active) {
    sendBtn.innerHTML = STOP_ICON;
    sendBtn.title = '停止生成';
    sendBtn.classList.add('btn-stop');
  } else {
    sendBtn.innerHTML = SEND_ICON;
    sendBtn.title = '发送';
    sendBtn.classList.remove('btn-stop');
  }
}

async function stopGeneration() {
  if (abortController) abortController.abort();
  if (currentSession) {
    try { await fetch('/api/chat/stop/' + currentSession, { method: 'POST' }); } catch (e) {}
  }
}

function newSessionId() {
  return 'sess_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 8);
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function makeTyping() {
  const typing = el('span', 'typing');
  for (let i = 0; i < 3; i++) typing.appendChild(el('span', 'dot'));
  return typing;
}

function mdRender(text) {
  if (typeof marked !== 'undefined' && marked.parse) {
    return marked.parse(text, { breaks: true, gfm: true });
  }
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

/* ===== 会话列表 ===== */
async function loadSessions() {
  const res = await fetch('/api/sessions');
  const sessions = await res.json();
  sessionList.innerHTML = '';
  if (sessions.length === 0) {
    sessionList.appendChild(el('div', 'session-meta', '暂无会话'));
  }
  sessions.forEach((s) => {
    const item = el('div', 'session-item' + (s.session_id === currentSession ? ' active' : ''));
    item.appendChild(el('div', 'session-title', s.title || s.session_id.slice(0, 16) + '…'));
    const d = new Date(s.mtime * 1000);
    item.appendChild(el('div', 'session-meta', d.toLocaleString()));

    const delBtn = el('button', 'session-del', '×');
    delBtn.title = '删除会话';
    delBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      deleteSession(s.session_id);
    });
    item.appendChild(delBtn);

    item.addEventListener('click', () => switchSession(s.session_id));
    sessionList.appendChild(item);
  });
}

async function deleteSession(id) {
  if (!confirm('确定删除这个会话吗？删除后无法恢复。')) return;
  const res = await fetch('/api/session/' + id, { method: 'DELETE' });
  if (!res.ok) return;
  if (currentSession === id) {
    currentSession = null;
    chatScroll.innerHTML = '';
    chatScroll.appendChild(emptyState);
    wbBody.innerHTML = '<div class="wb-empty">工具调用将在这里实时显示</div>';
  }
  await loadSessions();
}

function formatTokens(n) {
  if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
  return '' + n;
}

function ratioToColor(ratio) {
  const r = Math.max(0, Math.min(1, ratio));
  const hue = 120 * (1 - r);  // 120 绿 → 0 红
  return `hsl(${hue}, 72%, 42%)`;
}

function setRing(ratio, used, window) {
  const C = 94.2;
  ringProgress.style.strokeDashoffset = (C * (1 - ratio)).toFixed(2);
  ringProgress.style.stroke = ratioToColor(ratio);
  const pct = Math.round(ratio * 100);
  const detail = (used !== undefined && window !== undefined)
    ? `${formatTokens(used)} / ${formatTokens(window)} · ${pct}%`
    : `${pct}%`;
  tokenRing.title = '上下文用量 ' + detail + '，点击压缩';
}

async function updateTokenUsage() {
  if (!currentSession) {
    setRing(0);
    return null;
  }
  try {
    const res = await fetch('/api/token-usage/' + currentSession);
    const usage = await res.json();
    setRing(usage.ratio, usage.used, usage.window);
    return usage;
  } catch (e) {
    return null;
  }
}

async function compactSession(auto = false) {
  if (!currentSession) return;
  if (!auto && !confirm('确定压缩当前会话的历史记忆吗？压缩后旧消息会折叠成摘要。')) return;
  tokenRing.classList.add('compacting');
  try {
    const res = await fetch('/api/compact/' + currentSession, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'part', rate: 0.5 }),
    });
    const data = await res.json().catch(() => null);
    if (!res.ok || !data || data.ok === false) {
      throw new Error((data && data.error) || ('HTTP ' + res.status));
    }
    await updateTokenUsage();
  } catch (e) {
    alert('压缩失败：' + e.message);
  } finally {
    tokenRing.classList.remove('compacting');
  }
}

async function switchSession(id) {
  currentSession = id;
  await loadSessions();
  updateTokenUsage();
  chatScroll.innerHTML = '';
  const msgs = await fetch('/api/messages/all/' + id).then((r) => r.json());
  if (msgs.length === 0) {
    chatScroll.appendChild(emptyState);
    return;
  }
  let pendingTools = [];
  let pendingReasoning = null;
  msgs.forEach((m) => {
    if (m.role === 'async') {
      // 异步子代理的内部消息（外层 role=async，本质是喂给模型的 user 输入），不在页面上渲染
      return;
    } else if (m.role === 'user') {
      addUserMsg(m.content);
    } else if (m.role === 'assistant') {
      // 带工具调用的 assistant：先暂存工具名，等正文出现一起渲染
      if (m.tool_calls) {
        // 累加（而非覆盖）：模型可能串行多轮各调一个工具
        pendingTools = pendingTools.concat(m.tool_calls.map((tc) => tc.function.name));
      }
      if (m.reasoning_content) {
        pendingReasoning = (pendingReasoning ? pendingReasoning + '\n' : '') + m.reasoning_content;
      }
      if (m.content) {
        addAssistantMsg(m.content, pendingTools, pendingReasoning);
        pendingTools = [];
        pendingReasoning = null;
      }
    }
    // role === 'tool' 的结果消息跳过（工具结果已在右侧工作台体现）
  });
  scrollBottom();
}

/* ===== 消息渲染 ===== */
function removeEmptyState() {
  if (emptyState.parentNode) emptyState.remove();
}

function addUserMsg(text) {
  removeEmptyState();
  const wrap = el('div', 'msg user');
  const bubble = el('div', 'msg-bubble', text);
  wrap.appendChild(bubble);
  chatScroll.appendChild(wrap);
  scrollBottom();
}

function addAssistantMsg(text, tools, reasoning) {
  removeEmptyState();
  const wrap = el('div', 'msg assistant');
  const header = el('div', 'ai-header');
  header.appendChild(el('span', 'ai-name', 'AEGIS'));
  wrap.appendChild(header);
  if (reasoning || (tools && tools.length)) {
    const status = el('div', 'ai-status');
    if (reasoning) {
      status.appendChild(makeReasoningCollapse(reasoning));
    }
    if (tools && tools.length) {
      if (tools.length > 1) {
        status.appendChild(makeToolCollapse(tools));
      } else {
        status.appendChild(addToolDone(tools[0]));
      }
    }
    wrap.appendChild(status);
  }
  const content = el('div', 'ai-content');
  content.innerHTML = mdRender(text);
  wrap.appendChild(content);
  chatScroll.appendChild(wrap);
  scrollBottom();
  return wrap;
}

// 历史消息里的工具调用（已完成）折叠显示为静态灰白小字
function addToolDone(name) {
  const line = el('div', 'status-line tool-done');
  line.appendChild(document.createTextNode('✓ ' + name));
  return line;
}

// 将多条已完成工具折叠成一个可点击展开的块
function makeToolCollapse(names) {
  const box = el('div', 'tool-collapse');
  const head = el('div', 'tool-collapse-head');
  const arrow = el('span', 'tool-collapse-arrow', '▸');
  head.appendChild(arrow);
  head.appendChild(document.createTextNode(' 调用了 ' + names.length + ' 个工具'));
  const body = el('div', 'tool-collapse-body');
  body.style.display = 'none';
  names.forEach((n) => body.appendChild(addToolDone(n)));
  head.addEventListener('click', () => {
    const collapsed = body.style.display === 'none';
    body.style.display = collapsed ? 'flex' : 'none';
    arrow.textContent = collapsed ? '▾' : '▸';
  });
  box.appendChild(head);
  box.appendChild(body);
  return box;
}

// 将思考内容折叠成一个可点击展开的块
function makeReasoningCollapse(text) {
  const box = el('div', 'tool-collapse');
  const head = el('div', 'tool-collapse-head');
  const arrow = el('span', 'tool-collapse-arrow', '▸');
  head.appendChild(arrow);
  head.appendChild(document.createTextNode(' 思考过程'));
  const body = el('div', 'reasoning-collapse-body');
  body.style.display = 'none';
  body.appendChild(el('div', 'reasoning-full', text));
  head.addEventListener('click', () => {
    const collapsed = body.style.display === 'none';
    body.style.display = collapsed ? 'block' : 'none';
    arrow.textContent = collapsed ? '▾' : '▸';
  });
  box.appendChild(head);
  box.appendChild(body);
  return box;
}

// 把流式展示的思考内容折叠成一个块（正文开始输出时调用）
function collapseReasoning(statusBox, reasoningBox) {
  if (!reasoningBox || !reasoningBox.parentNode) return;
  const textSpan = reasoningBox.querySelector('.reasoning-text');
  const full = textSpan ? textSpan.textContent : '';
  reasoningBox.remove();
  if (full.trim()) {
    statusBox.appendChild(makeReasoningCollapse(full));
  }
}

// 滚动到底：用 requestAnimationFrame 合并，避免每个事件都同步 reflow（读 scrollHeight 会强制重排）
let _scrollRaf = null;
function scrollBottom() {
  if (_scrollRaf) return;
  _scrollRaf = requestAnimationFrame(() => {
    _scrollRaf = null;
    chatScroll.scrollTop = chatScroll.scrollHeight;
  });
}

/* ===== 工作台 ===== */
function addWbItem(name, args) {
  removeWbEmpty();
  const item = el('div', 'wb-item');
  const head = el('div', 'wb-item-head');
  head.appendChild(el('span', 'fn', name));
  const status = el('span', 'wb-status running', '运行中…');
  head.appendChild(status);
  item.appendChild(head);
  if (args) item.appendChild(el('div', 'wb-item-args', args));
  wbBody.appendChild(item);
  wbBody.scrollTop = wbBody.scrollHeight;
  return item;
}

function removeWbEmpty() {
  const e = wbBody.querySelector('.wb-empty');
  if (e) e.remove();
}

function finishWbItem(item, content, ok = true) {
  const status = item.querySelector('.wb-status');
  status.textContent = ok ? '完成' : '出错';
  status.className = 'wb-status ' + (ok ? 'done' : 'error');
  // 工具结果过长时截断显示，避免把全文塞进 DOM 卡死页面（模型侧拿到的仍是完整内容，不受影响）
  const MAX_WB_CHARS = 100;
  let shown = content || '';
  if (shown.length > MAX_WB_CHARS) {
    const total = shown.length;
    shown = shown.slice(0, MAX_WB_CHARS) + '…（共 ' + total + ' 字符，已省略）';
  }
  const result = el('div', 'wb-item-result', shown);
  item.appendChild(result);
  wbBody.scrollTop = wbBody.scrollHeight;
}

/* ===== 子代理面板 =====
   后端状态表是全局的；sid 形如 "<session_id>:<uuid>"，所以按前缀过滤出当前会话的。
   用轮询而不是 SSE：主循环等子代理时会卡在 await q.get()，SSE 流是静默的，推不出状态。 */
const saCards = new Map();   // sid -> 卡片 DOM（按 id 增量更新，避免全量重写导致闪烁）
let saPolling = false;       // 防止请求重叠堆积

const SA_STATUS_TEXT = { running: '运行中', done: '完成', failed: '失败', cancelled: '已取消' };

function saCreateCard(sid) {
  const card = el('div', 'sa-card');
  const head = el('div', 'sa-head');
  head.appendChild(el('span', 'sa-dot'));
  head.appendChild(el('span', 'sa-label'));
  head.appendChild(el('span', 'sa-status'));
  card.appendChild(head);
  const bar = el('div', 'sa-bar');
  bar.appendChild(el('div', 'sa-bar-fill'));
  card.appendChild(bar);
  card.appendChild(el('div', 'sa-meta'));
  card.appendChild(el('div', 'sa-tool'));
  card.appendChild(el('div', 'sa-text'));
  saCards.set(sid, card);
  return card;
}

function saUpdateCard(card, st) {
  const status = st.status || 'running';
  card.className = 'sa-card sa-' + status;

  card.querySelector('.sa-label').textContent =
    (st.label || st.id || '').trim() || '(未命名子任务)';
  card.querySelector('.sa-status').textContent = SA_STATUS_TEXT[status] || status;

  const cur = st.round || 0;
  const max = st.max_rounds || 0;
  card.querySelector('.sa-bar-fill').style.width =
    (max > 0 ? Math.min(100, Math.round((cur / max) * 100)) : 0) + '%';

  // updated_at 目前不会在 update() 里刷新，所以结束后的总耗时不可信 —— 只对运行中显示
  let meta = cur + '/' + max + ' 轮';
  if (status === 'running' && st.started_at) {
    meta += ' · ' + Math.max(0, Math.round(Date.now() / 1000 - st.started_at)) + 's';
  }
  card.querySelector('.sa-meta').textContent = meta;

  card.querySelector('.sa-tool').textContent = st.last_tool ? '正在调用 ' + st.last_tool : '';
  card.querySelector('.sa-text').textContent = (st.last_text || '').trim();
}

function renderSubagents(list) {
  const mine = (list || []).filter((s) => {
    const sid = String(s.id || '');
    const i = sid.indexOf(':');
    return (i >= 0 ? sid.slice(0, i) : sid) === currentSession;
  });

  // 清掉已经不在快照里的卡片（含切换会话后属于别的会话的）
  const alive = new Set(mine.map((s) => s.id));
  saCards.forEach((card, sid) => {
    if (!alive.has(sid)) { card.remove(); saCards.delete(sid); }
  });

  if (mine.length === 0) { saPanel.hidden = true; return; }
  saPanel.hidden = false;

  mine.forEach((st) => {
    const card = saCards.get(st.id) || saCreateCard(st.id);
    if (!card.parentNode) saPanel.appendChild(card);
    saUpdateCard(card, st);
  });
}

async function pollSubagents() {
  if (saPolling) return;                 // 上一轮还没回来就跳过
  saPolling = true;
  try {
    const res = await fetch('/api/subagents');
    if (res.ok) renderSubagents(await res.json());
  } catch (e) {
    /* 轮询失败静默忽略，下一秒会重试 */
  } finally {
    saPolling = false;
  }
}

/* ===== 发送与流式接收 ===== */
async function send() {
  const text = input.value.trim();
  if (!text || sending) return;
  if (!currentSession) {
    currentSession = newSessionId();
    await loadSessions();
  }

  sending = true;
  setSendBtnState(true);
  input.value = '';
  autoResize();

  addUserMsg(text);

  // 创建一个正在生成的 assistant 消息占位
  removeEmptyState();
  const wrap = el('div', 'msg assistant');
  const header = el('div', 'ai-header');
  header.appendChild(el('span', 'ai-name', 'AEGIS'));
  wrap.appendChild(header);

  // 状态区：思考态 + 工具调用态
  const statusBox = el('div', 'ai-status');
  const thinkLine = el('div', 'status-line');
  thinkLine.appendChild(el('span', 'shimmer-text', '正在思考'));
  thinkLine.appendChild(makeTyping());
  statusBox.appendChild(thinkLine);
  wrap.appendChild(statusBox);

  // 正文区
  const content = el('div', 'ai-content', '');
  wrap.appendChild(content);
  chatScroll.appendChild(wrap);
  scrollBottom();

  // 用工具调用 id 关联「对话流状态行」和「工作台条目」，避免并发工具互相覆盖
  const toolLines = {};   // id -> 工具调用框里的"正在调用"行
  const wbItems = {};     // id -> 工作台里的工具条目
  let toolCallBox = null; // 工具调用框（固定约 3 行高度，超出滚动）
  let textStarted = false; // 正文是否已开始（首次 text 时折叠思考内容）
  let toolCollapsed = false; // 工具框是否已折叠（tool_call 事件可能晚于首个 text 到达，故独立判断）
  let reasoningBox = null; // 思考内容流式展示容器
  let reasoningText = null; // 思考内容的文本节点（累积内容直接写这里，不再每个 delta 一个 span）
  let reasoningBuffer = ''; // 思考内容累积
  let reasoningTimer = null; // 思考内容渲染节流定时器
  let mdBuffer = ''; // 累积正文原始文本
  let renderTimer = null; // markdown 渲染节流定时器（大文本时每个 delta 全量重渲染会卡）

  // 思考内容渲染节流：累积到 buffer，低频率写入 textContent（避免每个 delta 都触发 DOM 重排 + 动画）
  function flushReasoning() {
    reasoningTimer = null;
    if (reasoningText) {
      reasoningText.textContent = reasoningBuffer;
      // 思考窗口内滚动到底，始终显示最新内容
      if (reasoningBox) reasoningBox.scrollTop = reasoningBox.scrollHeight;
    }
  }
  function scheduleReasoning() {
    if (reasoningTimer) return;
    reasoningTimer = setTimeout(flushReasoning, 50);
  }

  // 流式正文渲染节流：短文本高频（流畅），长文本低频（避免卡死）
  function flushRender() {
    renderTimer = null;
    content.innerHTML = mdRender(mdBuffer);
    scrollBottom();
  }
  function scheduleRender() {
    if (renderTimer) return;
    // 正文越长，重渲染（marked 全量解析 + innerHTML 重建）越贵，降低频率
    const delay = mdBuffer.length > 60000 ? 300 : (mdBuffer.length > 30000 ? 200 : 60);
    renderTimer = setTimeout(flushRender, delay);
  }

  // 工具调用框：第一次调用工具时创建，固定约 3 行高度、超出滚动
  function ensureToolCallBox() {
    if (toolCallBox) return toolCallBox;
    toolCallBox = el('div', 'tool-call-box');
    statusBox.appendChild(toolCallBox);
    return toolCallBox;
  }

  // 折叠工具调用框为"调用了 N 个工具"的可展开块
  function collapseToolCallBox() {
    if (!toolCallBox || !toolCallBox.parentNode) return;
    const parent = toolCallBox.parentNode;
    const names = Array.from(toolCallBox.querySelectorAll('.status-line'))
      .map((l) => l.textContent.replace(/^✓\s*/, '').replace(/^正在调用\s*/, '').trim())
      .filter(Boolean);
    let node = null;
    if (names.length > 1) {
      node = makeToolCollapse(names);
    } else if (names.length === 1) {
      node = addToolDone(names[0]);
    }
    if (node) {
      parent.insertBefore(node, toolCallBox);
    }
    parent.removeChild(toolCallBox);
    toolCallBox = null;
  }

  abortController = new AbortController();
  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: currentSession, message: text }),
      signal: abortController.signal,
    });

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    const handleEvent = (ev) => {
      if (ev.event === 'text') {
        if (thinkLine.parentNode) thinkLine.remove();
        // 首次正文：折叠思考内容
        if (!textStarted) {
          textStarted = true;
          flushReasoning();  // 先把 buffer 里最后一段思考内容写进 DOM，再折叠
          collapseReasoning(statusBox, reasoningBox);
          reasoningBox = null;  // 思考已折叠，标记置空（下一轮 reasoning 事件会重建）
        }
        // 工具框：只要已创建且未折叠，就在正文输出时折叠。
        // 注意 tool_call 事件可能晚于首个 text（模型先吐过渡正文再调工具），
        // 所以不能只在"首次 text"折叠，必须每个 text 都检查。
        if (toolCallBox) {
          try {
            collapseToolCallBox();
          } catch (e) {
            console.error('折叠工具调用框失败:', e);
          }
        }
        mdBuffer += ev.data.delta;
        scheduleRender();
      } else if (ev.event === 'break') {
        // 异步结果回传后，新一段 AI 回复开始：在已有内容后加换行分隔
        mdBuffer += '\n\n';
        scheduleRender();
      } else if (ev.event === 'reasoning') {
        // 思考内容：首次出现时移除"正在思考"，流式展示思考文字
        if (!reasoningBox) {
          if (thinkLine.parentNode) thinkLine.remove();
          reasoningBox = el('div', 'reasoning-box');
          reasoningText = el('span', 'reasoning-text');
          reasoningBox.appendChild(reasoningText);
          statusBox.appendChild(reasoningBox);
        }
        reasoningBuffer += ev.data.delta;
        scheduleReasoning();
        scrollBottom();
      } else if (ev.event === 'tool_call') {
        // 在工具调用框里追加一条"正在调用 xxx"（框内超出 3 行自动滚动）
        const box = ensureToolCallBox();
        const toolLine = el('div', 'status-line');
        toolLine.appendChild(el('span', 'shimmer-text', '正在调用 ' + ev.data.name));
        toolLine.appendChild(makeTyping());
        box.appendChild(toolLine);
        toolLines[ev.data.id] = toolLine;
        wbItems[ev.data.id] = addWbItem(ev.data.name, ev.data.arguments);
        box.scrollTop = box.scrollHeight;
        scrollBottom();
      } else if (ev.event === 'tool_result') {
        // 折叠：把"正在调用"流光行改成静态灰白 ✓
        const line = toolLines[ev.data.id];
        if (line) {
          line.innerHTML = '';
          line.className = 'status-line tool-done';
          line.appendChild(document.createTextNode('✓ ' + ev.data.name));
          delete toolLines[ev.data.id];
        }
        const wb = wbItems[ev.data.id];
        if (wb) {
          finishWbItem(wb, ev.data.content, true);
          delete wbItems[ev.data.id];
        }
      } else if (ev.event === 'done') {
        if (thinkLine.parentNode) thinkLine.remove();
        // 兜底：结束前确保思考与工具框都已折叠
        flushReasoning();
        collapseReasoning(statusBox, reasoningBox);
        reasoningBox = null;
        if (toolCallBox) {
          try { collapseToolCallBox(); } catch (e) { console.error('折叠工具调用框失败:', e); }
        }
        if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
        if (mdBuffer) content.innerHTML = mdRender(mdBuffer);
      } else if (ev.event === 'error') {
        if (thinkLine.parentNode) thinkLine.remove();
        flushReasoning();
        collapseReasoning(statusBox, reasoningBox);
        reasoningBox = null;
        if (toolCallBox) {
          try { collapseToolCallBox(); } catch (e) { console.error('折叠工具调用框失败:', e); }
        }
        mdBuffer += '\n\n[错误] ' + ev.data.message;
        content.appendChild(document.createTextNode('[错误] ' + ev.data.message));
        Object.values(wbItems).forEach((wb) => finishWbItem(wb, ev.data.message, false));
      }
    };

    while (true) {
      const { done, value } = await reader.read();
      if (!done) {
        buffer += decoder.decode(value, { stream: true });
      }

      const events = buffer.split('\n\n');
      buffer = events.pop();

      for (const raw of events) {
        const ev = parseSSE(raw);
        if (ev) handleEvent(ev);
      }

      if (done) {
        // 流结束：buffer 里残留的最后一段（通常是 done 事件）也要处理
        const tail = buffer.trim();
        if (tail) {
          const ev = parseSSE(tail);
          if (ev) handleEvent(ev);
        }
        break;
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') {
      // 用户主动停止：移除进行中的状态，追加"已停止"提示
      if (thinkLine.parentNode) thinkLine.remove();
      Object.values(toolLines).forEach((line) => line.remove());
      Object.values(wbItems).forEach((wb) => {
        const st = wb.querySelector('.wb-status');
        if (st) { st.textContent = '已停止'; st.className = 'wb-status error'; }
      });
      wrap.appendChild(el('div', 'ai-stopped', '已停止生成'));
    } else {
      if (thinkLine.parentNode) thinkLine.remove();
      content.appendChild(document.createTextNode('[网络错误] ' + e.message));
    }
  } finally {
    sending = false;
    setSendBtnState(false);
    abortController = null;
    scrollBottom();
    loadSessions();
    const usage = await updateTokenUsage();
    if (usage && usage.ratio >= 0.7) {
      compactSession(true);
    }
  }
}

function parseSSE(raw) {
  const lines = raw.split('\n');
  let event = 'message';
  let data = '';
  for (const line of lines) {
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) data += line.slice(5).trim();
  }
  if (!data) return null;
  try {
    return { event, data: JSON.parse(data) };
  } catch {
    return { event, data: { delta: data } };
  }
}

/* ===== 输入框自适应 ===== */
function autoResize() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 160) + 'px';
}

input.addEventListener('input', autoResize);
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});

$('#send').addEventListener('click', () => {
  if (sending) stopGeneration();
  else send();
});
tokenRing.addEventListener('click', () => compactSession(false));
$('#new-session').addEventListener('click', () => {
  currentSession = newSessionId();
  chatScroll.innerHTML = '';
  chatScroll.appendChild(emptyState);
  wbBody.innerHTML = '<div class="wb-empty">工具调用将在这里实时显示</div>';
  loadSessions();
  updateTokenUsage();
});

// 子代理面板：每秒轮询一次状态快照
setInterval(pollSubagents, 1000);
pollSubagents();

loadSessions();
