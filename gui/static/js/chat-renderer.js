// 聊天区渲染器 — 以「轮次（Turn）」为单位的消息渲染
//
// 聊天区渲染模型：
//   用户的一条消息开启一轮；随后 Master / 子 Agent 在这一轮里的全部产出
//   （深度思考、工具调用、协作提示、最终回复）按到达时间线性地追加进
//   同一张大卡片。不同内容用不同的字号 / 颜色 / 粗细区分：
//   - 深度思考：小号、细体、斜体、紫灰（被工具调用跟随的文本段自动降级）
//   - 工具调用：更小号、等宽、冷灰，完成后折叠为一行摘要
//   - 最终回复：正常偏大、中等粗细、近黑，始终展开
//   - 系统协作提示：居中小字
//
// 依赖: initChat() 注入 DOM 引用后可使用。

import { Store, colorForAgent } from "./state.js";

let _chat = {};

/** 注入聊天区 DOM 引用（由 ui.js 的 initUI 调用） */
export function initChat(els) {
  _chat = els;
}

// ---------------------------------------------------------------------------
// Markdown 轻量渲染（先整体转义，再注入有限标签，避免 XSS）
// ---------------------------------------------------------------------------

export function renderMarkdown(text) {
  const escaped = escapeHtml(text ?? "");
  const blocks = [];
  // 抽取代码块，避免其内部被后续规则破坏
  let work = escaped.replace(/```(?:[a-zA-Z0-9_-]*)\n([\s\S]*?)```/g, (_, code) => {
    const idx = blocks.push(`<pre><code>${code.replace(/\n$/, "")}</code></pre>`) - 1;
    return ` B${idx} `;
  });
  work = work.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  work = work.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  work = work.replace(/\n/g, "<br>");
  work = work.replace(/ B(\d+) /g, (_, i) => blocks[Number(i)]);
  return work;
}

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// ---------------------------------------------------------------------------
// 聊天区完整渲染（打开会话 / 加载历史时）
// ---------------------------------------------------------------------------

export function renderChat() {
  _chat.messages.innerHTML = "";
  Store.openMsgs = {};
  Store.activeTurn = null;

  if (!Store.messages.length) {
    const hint = document.createElement("div");
    hint.className = "empty-hint";
    hint.textContent = "还没有消息，发一条试试吧";
    _chat.messages.appendChild(hint);
    return;
  }

  // 从成员列表构建 agent_id -> color 映射，确保历史消息颜色稳定
  const memberColorMap = {};
  for (const m of Store.members) {
    if (m.color) memberColorMap[m.agent_id] = m.color;
  }

  let turn = null;
  for (const rec of Store.messages) {
    rec.color = memberColorMap[rec.agent_id] || "";
    if (rec.role === "user") {
      turn = startTurn(rec);
    } else {
      if (!turn) turn = startTurn(null); // 兜底：没有用户消息开头的轮次
      addSegment(turn, rec, { live: false });
    }
  }
  // 若该会话还有进行中的输出，后续事件继续追加到最后一轮
  Store.activeTurn = turn;
  scrollToBottom();
}

// ---------------------------------------------------------------------------
// 轮次构建
// ---------------------------------------------------------------------------

// 开启新一轮：渲染用户气泡（若有），返回轮次上下文。
// Agent 大卡片延迟到第一条 Agent 内容到达时才创建，避免空卡片。
export function startTurn(userRec) {
  const turn = {
    el: document.createElement("div"), // .turn 容器
    card: null,        // .turn-card 大卡片 DOM
    bodyEl: null,      // .turn-body（所有 segment 的容器）
    ownerId: null,     // 本轮第一个发言的 Agent
    lastTextSeg: null, // 最近的文本段（被工具调用跟随时降级为深度思考）
  };
  turn.el.className = "turn";
  if (userRec) {
    turn.el.appendChild(buildUserRow(userRec));
  }
  _chat.messages.appendChild(turn.el);
  return turn;
}

function buildUserRow(rec) {
  const row = document.createElement("div");
  row.className = "msg right";
  row.dataset.msgId = rec.msg_id;

  const av = document.createElement("div");
  av.className = "avatar me";
  av.textContent = "我";

  const wrap = document.createElement("div");
  wrap.className = "bubble-wrap";
  const body = document.createElement("div");
  body.className = "bubble";
  body.innerHTML = renderMarkdown(rec.text || "");
  wrap.appendChild(body);

  row.appendChild(av);
  row.appendChild(wrap);
  return row;
}

// 确保 Agent 大卡片存在（以本轮当前发言者命名并显示头像）
// 群聊场景下：不同 Agent 的发言各自拥有独立气泡，而不是全部折叠到第一个发言者卡片里
function ensureTurnCard(turn, rec) {
  if (turn.card && turn.ownerId === rec.agent_id) return;
  turn.ownerId = rec.agent_id;
  // 换 Agent 发言时，清空上一个 Agent 的文本段引用，避免新 Agent 的工具调用
  // 错误地降级上一个 Agent 的最终回复为“深度思考”
  turn.lastTextSeg = null;

  const row = document.createElement("div");
  row.className = "msg left turn-card";
  row.dataset.msgId = rec.msg_id;

  const av = document.createElement("div");
  av.className = "avatar";
  av.style.background = colorForAgent(rec.agent_id, rec.agent_name, rec.color);
  av.textContent = (rec.agent_name || "?").trim().charAt(0).toUpperCase();

  const wrap = document.createElement("div");
  wrap.className = "turn-wrap";
  const name = document.createElement("div");
  name.className = "name";
  name.textContent = rec.agent_name || "";
  const body = document.createElement("div");
  body.className = "turn-body";
  wrap.appendChild(name);
  wrap.appendChild(body);

  row.appendChild(av);
  row.appendChild(wrap);
  turn.el.appendChild(row);
  turn.card = row;
  turn.bodyEl = body;
}

// ---------------------------------------------------------------------------
// Segment 类型
// ---------------------------------------------------------------------------

// 把一条消息作为 segment 追加到轮次卡片
export function addSegment(turn, rec, opts = {}) {
  ensureTurnCard(turn, rec);
  if (rec.kind === "system") return addSystemSegment(turn, rec);
  if (rec.kind === "tool") return addToolSegment(turn, rec, opts);
  if (_isSubResult(rec)) return addResultSegment(turn, rec, opts);
  return addTextSegment(turn, rec, opts);
}

// 判断是否是子 Agent 任务结果（run_sub_agent 完成后的广播消息）
function _isSubResult(rec) {
  return rec.kind === "text" && rec.role === "assistant" &&
    /^#{1,4}\s*✅/.test(rec.text || "");
}

// 子 Agent 任务结果：可能长达数千字，默认折叠为一行摘要，点击展开
function addResultSegment(turn, rec, opts = {}) {
  const seg = document.createElement("div");
  seg.className = "seg seg-result collapsed";
  seg.dataset.msgId = rec.msg_id;

  // 群聊中非卡片主人（其他 Agent）的结果：加彩色小徽标
  if (rec.agent_id && rec.agent_id !== turn.ownerId) {
    seg.appendChild(buildAgentBadge(rec));
  }

  const m = (rec.text || "").match(/^#{1,4}\s*✅\s*(.+?)(?:\n|$)/);
  const title = m ? m[1] : "任务结果";
  const head = document.createElement("div");
  head.className = "seg-head";
  head.innerHTML =
    `<span>✅ ${escapeHtml(title)}</span><span class="collapse-arrow">▾</span>`;
  head.onclick = () => seg.classList.toggle("collapsed");

  const content = document.createElement("div");
  content.className = "seg-content";
  if (rec.text) content.innerHTML = renderMarkdown(rec.text);
  seg.appendChild(head);
  seg.appendChild(content);
  turn.bodyEl.appendChild(seg);

  return { seg, contentEl: content, rec, turn, kind: "text" };
}

// 文本段：流式期间按「最终回复」样式渲染；一旦被工具调用跟随，
// 由 addToolSegment 降级为「深度思考」样式并折叠
export function addTextSegment(turn, rec, opts = {}) {
  const seg = document.createElement("div");
  seg.className = "seg seg-text" + (opts.live ? " streaming" : "");
  seg.dataset.msgId = rec.msg_id;

  // 群聊中非卡片主人（其他 Agent）的发言：加彩色小徽标
  if (rec.agent_id && rec.agent_id !== turn.ownerId) {
    seg.appendChild(buildAgentBadge(rec));
  }

  const content = document.createElement("div");
  content.className = "seg-content";
  if (rec.text) content.innerHTML = renderMarkdown(rec.text);
  seg.appendChild(content);
  turn.bodyEl.appendChild(seg);

  const ref = { seg, contentEl: content, rec, turn, kind: "text", downgraded: false };
  turn.lastTextSeg = ref;
  return ref;
}

function buildAgentBadge(rec) {
  const badge = document.createElement("div");
  badge.className = "seg-agent-badge";
  const dot = document.createElement("span");
  dot.className = "seg-agent-dot";
  dot.style.background = colorForAgent(rec.agent_id, rec.agent_name, rec.color);
  const label = document.createElement("span");
  label.textContent = rec.agent_name || rec.agent_id;
  badge.appendChild(dot);
  badge.appendChild(label);
  return badge;
}

// ---------------------------------------------------------------------------
// 深度思考降级
// ---------------------------------------------------------------------------

// 工具调用会把它前面最近的文本段降级为「深度思考」：
// 换小号斜体紫灰样式、加标签并折叠，点击标签可重新展开
function downgradeToThinking(ref) {
  if (!ref || ref.downgraded) return;
  ref.downgraded = true;
  ref.seg.classList.remove("seg-text", "streaming");
  ref.seg.classList.add("seg-thinking");

  const n = (ref.rec.text || "").length;
  const head = document.createElement("div");
  head.className = "seg-head";
  head.innerHTML =
    `<span>💭 深度思考 · ${n} 字</span><span class="collapse-arrow">▾</span>`;
  head.onclick = () => ref.seg.classList.toggle("collapsed");

  const badge = ref.seg.querySelector(".seg-agent-badge");
  if (badge) badge.after(head);
  else ref.seg.insertBefore(head, ref.seg.firstChild);

  ref.seg.classList.add("collapsed");
}

// ---------------------------------------------------------------------------
// 工具调用段
// ---------------------------------------------------------------------------

// 工具调用会把它前面最近的文本段降级为「深度思考」
export function addToolSegment(turn, rec, opts = {}) {
  downgradeToThinking(turn.lastTextSeg);

  const seg = document.createElement("div");
  seg.className = "seg seg-tool running";
  seg.dataset.msgId = rec.msg_id;

  const info = parseToolInfo(rec.text || "", rec.meta);
  const head = document.createElement("div");
  head.className = "tool-head";
  head.innerHTML =
    `<span class="tool-icon">🔧</span>` +
    `<span class="tool-name">${escapeHtml(info.name)}</span>` +
    `<span class="tool-status"><span class="spinner"></span>调用中…</span>` +
    `<span class="tool-arrow">▾</span>`;
  head.onclick = () => seg.classList.toggle("collapsed");

  const body = document.createElement("div");
  body.className = "tool-body";
  seg.appendChild(head);
  seg.appendChild(body);
  turn.bodyEl.appendChild(seg);

  const ref = { seg, contentEl: body, rec, turn, kind: "tool" };
  // 历史消息已经完成，直接填好参数/结果并折叠
  if (!opts.live) fillToolBody(ref, info);
  return ref;
}

// 从工具消息的 meta（新版）或文本（旧数据兜底）解析结构化信息
export function parseToolInfo(text, meta) {
  if (meta && meta.tool_name) {
    return {
      name: meta.tool_name,
      args: meta.arguments || "",
      result: meta.result || "",
    };
  }
  const name =
    (text.match(/\*\*🔧\s*(.+?)\*\*/) || [])[1] ||
    (text.match(/正在调用工具\s*`(.+?)`/) || [])[1] ||
    "工具调用";
  const args = (text.match(/参数：`([\s\S]*?)`/) || [])[1] || "";
  const result = (text.match(/结果：\n?([\s\S]*)$/) || [])[1] || "";
  return { name, args, result };
}

// 用结构化信息填充工具段主体，并标记完成 + 折叠
export function fillToolBody(ref, info) {
  ref.seg.classList.remove("running");
  ref.seg.classList.add("collapsed");
  const status = ref.seg.querySelector(".tool-status");
  if (status) status.textContent = "完成";

  const body = ref.contentEl;
  body.innerHTML = "";

  if (info.args) {
    const block = document.createElement("div");
    block.className = "tool-block";
    block.innerHTML =
      `<div class="tool-label">参数</div><code>${escapeHtml(info.args)}</code>`;
    body.appendChild(block);
  }
  const rb = document.createElement("div");
  rb.className = "tool-block";
  rb.innerHTML =
    `<div class="tool-label">结果</div>` +
    `<div class="tool-result-text">${escapeHtml(info.result || "（无输出）")}</div>`;
  body.appendChild(rb);
}

// 系统协作提示：居中小字一行
function addSystemSegment(turn, rec) {
  const seg = document.createElement("div");
  seg.className = "seg seg-system";
  seg.dataset.msgId = rec.msg_id;
  const content = document.createElement("div");
  content.className = "seg-content";
  content.innerHTML = renderMarkdown(rec.text || "");
  seg.appendChild(content);
  turn.bodyEl.appendChild(seg);
  return { seg, contentEl: content, rec, turn, kind: "system" };
}

// ---------------------------------------------------------------------------
// 流式消息事件处理
// ---------------------------------------------------------------------------

export function onMessageStart(ev) {
  if (ev.session_id !== Store.activeSessionId) return;

  const rec = {
    msg_id: ev.msg_id,
    session_id: ev.session_id,
    agent_id: ev.agent_id,
    agent_name: ev.agent_name,
    role: ev.role,
    kind: ev.kind,
    text: ev.text || "",
    color: ev.color || "",
  };

  if (rec.role === "user") {
    Store.activeTurn = startTurn(rec);
    scrollToBottom();
    return;
  }
  if (!Store.activeTurn) Store.activeTurn = startTurn(null);
  Store.openMsgs[ev.msg_id] = addSegment(Store.activeTurn, rec, { live: true });
  scrollToBottom();
}

export function onMessageChunk(ev) {
  const ref = Store.openMsgs[ev.msg_id];
  if (!ref) return;
  ref.rec.text = (ref.rec.text || "") + ev.text;
  if (ref.kind === "text") {
    ref.contentEl.textContent += ev.text; // textContent 自动转义，安全
  }
  scrollToBottom();
}

export function onMessageReplace(ev) {
  const ref = Store.openMsgs[ev.msg_id];
  if (!ref) return;
  ref.rec.text = ev.text;
  if (ref.kind === "text") {
    ref.contentEl.innerHTML = renderMarkdown(ev.text);
  }
  scrollToBottom();
}

export function onMessageEnd(ev) {
  const ref = Store.openMsgs[ev.msg_id];
  if (!ref) return;
  // 优先使用后端回传的最终文本（覆盖 system 等无 chunk/replace 的消息）
  if (ev.text) ref.rec.text = ev.text;

  if (ref.kind === "text") {
    ref.contentEl.innerHTML = renderMarkdown(ref.rec.text || "");
  } else if (ref.kind === "tool") {
    fillToolBody(ref, parseToolInfo(ref.rec.text || "", ev.meta || {}));
  }
  ref.seg.classList.remove("streaming");
  delete Store.openMsgs[ev.msg_id];
  scrollToBottom();
}

// ---------------------------------------------------------------------------
// 辅助
// ---------------------------------------------------------------------------

export function scrollToBottom() {
  _chat.messages.scrollTop = _chat.messages.scrollHeight;
}
