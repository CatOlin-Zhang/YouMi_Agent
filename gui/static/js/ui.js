// 所有 DOM 渲染与实时事件处理都在这里。app.js 负责启动并把 WS 事件转发到这里。
//
// 后端事件协议（server.py / hub/events.py）：
//   hello / session_created / session_deleted / agent_join / agent_update
//   message_start / message_chunk / message_replace / message_end / history / error
// 前端发出的 WS 命令（server.py 的 _dispatch_ws_command）：
//   send_message / get_history / create_session / delete_session / add_member / ping / typing
// REST：GET /api/agents、GET /api/sessions、POST /api/sessions、
//      GET /api/sessions/{id}、DELETE /api/sessions/{id}
//
// 聊天区渲染模型 —— 以「轮次（Turn）」为单位：
//   用户的一条消息开启一轮；随后 Master / 子 Agent 在这一轮里的全部产出
//   （深度思考、工具调用、协作提示、最终回复）按到达时间线性地追加进
//   同一张大卡片。不同内容用不同的字号 / 颜色 / 粗细区分：
//   - 深度思考：小号、细体、斜体、紫灰（被工具调用跟随的文本段自动降级）
//   - 工具调用：更小号、等宽、冷灰，完成后折叠为一行摘要
//   - 最终回复：正常偏大、中等粗细、近黑，始终展开
//   - 系统协作提示：居中小字

import { Store, colorForAgent } from "./state.js";
import { sendCommand } from "./ws.js";

// ---------------------------------------------------------------------------
// DOM 引用（在 initUI 中填充）
// ---------------------------------------------------------------------------
let el = {};
let pendingSubmit = null;

// ===========================================================================
// 初始化 & 数据加载
// ===========================================================================
export function initUI() {
  el = {
    sessionList: document.getElementById("session-list"),
    contactList: document.getElementById("contact-list"),
    memberList: document.getElementById("member-list"),
    messages: document.getElementById("messages"),
    header: document.getElementById("chat-header"),
    input: document.getElementById("input"),
    send: document.getElementById("send"),
    btnNewChat: document.getElementById("btn-new-chat"),
    btnNewGroup: document.getElementById("btn-new-group"),
    btnAddMember: document.getElementById("btn-add-member"),
    tabChats: document.querySelector('.tab[data-tab="chats"]'),
    tabContacts: document.querySelector('.tab[data-tab="contacts"]'),
    tabChatsBody: document.getElementById("tab-chats"),
    tabContactsBody: document.getElementById("tab-contacts"),
    modal: document.getElementById("modal"),
    modalTitle: document.getElementById("modal-title"),
    modalBody: document.getElementById("modal-body"),
    modalCancel: document.getElementById("modal-cancel"),
    modalOk: document.getElementById("modal-ok"),
    // 工作流面板
    wfPanel: document.getElementById("workflow-panel"),
    wfSteps: document.getElementById("workflow-steps"),
    wfProgress: document.getElementById("workflow-progress"),
    // MCP 工具面板
    toolsPanel: document.getElementById("tools-panel"),
    toolsList: document.getElementById("tools-list"),
    toolsStats: document.getElementById("tools-stats"),
  };

  el.tabChats.onclick = () => switchTab("chats");
  el.tabContacts.onclick = () => switchTab("contacts");

  el.btnNewChat.onclick = () => createSession("single", "单聊");
  el.btnNewGroup.onclick = () =>
    showModal(
      "新建群聊",
      [{ key: "name", label: "群名称", placeholder: "例如：调研小组" }],
      (v) => createSession("group", v.name || "群聊")
    );

  el.btnAddMember.onclick = () =>
    showModal(
      "添加群成员",
      [
        { key: "role", label: "角色（对应 youmi/agents 下的目录名，如 coder）", placeholder: "coder" },
        { key: "task", label: "任务描述", placeholder: "负责完成…" },
      ],
      (v) => {
        if (Store.activeSessionId && v.role) {
          sendCommand({
            type: "add_member",
            session_id: Store.activeSessionId,
            role: v.role,
            task: v.task,
          });
        }
      }
    );

  el.send.onclick = doSend;
  el.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      doSend();
    }
  });

  el.modalCancel.onclick = closeModal;
  el.modalOk.onclick = () => {
    if (pendingSubmit) pendingSubmit();
    closeModal();
  };
  el.modal.addEventListener("click", (e) => {
    if (e.target === el.modal) closeModal();
  });
}

function switchTab(tab) {
  el.tabChats.classList.toggle("active", tab === "chats");
  el.tabContacts.classList.toggle("active", tab === "contacts");
  el.tabChatsBody.classList.toggle("hidden", tab !== "chats");
  el.tabContactsBody.classList.toggle("hidden", tab !== "contacts");
}

export async function loadAgents() {
  try {
    const res = await fetch("/api/agents");
    const data = await res.json();
    Store.contacts = data.agents || [];
    const master = Store.contacts.find((a) => a.role === "master" || a.name === "master");
    if (master) Store.masterId = master.name; // 固定品牌蓝
  } catch (e) {
    console.warn("加载 Agents 失败", e);
  }
  renderContacts();
}

export async function loadSessions() {
  try {
    const res = await fetch("/api/sessions");
    const data = await res.json();
    Store.sessions = data.sessions || [];
  } catch (e) {
    console.warn("加载会话失败", e);
  }
  renderSessions();
}

// ===========================================================================
// 渲染：左侧会话 / 通讯录
// ===========================================================================
function renderSessions() {
  el.sessionList.innerHTML = "";
  for (const s of Store.sessions) {
    const li = document.createElement("li");
    li.className = "session-item" + (s.session_id === Store.activeSessionId ? " active" : "");
    li.dataset.sid = s.session_id;

    const av = document.createElement("div");
    av.className = "avatar";
    av.style.background = s.type === "group" ? "#9c36b5" : "#2f7cf6";
    av.textContent = s.type === "group" ? "👥" : "💬";

    const meta = document.createElement("div");
    meta.className = "meta";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = s.name;
    const preview = document.createElement("div");
    preview.className = "preview";
    preview.textContent = s.type === "group" ? `${s.member_ids.length} 位成员` : "单聊会话";
    meta.appendChild(name);
    meta.appendChild(preview);

    // 删除会话入口（hover 时显示）
    const del = document.createElement("button");
    del.className = "session-del";
    del.title = "删除会话";
    del.textContent = "✕";
    del.onclick = (e) => {
      e.stopPropagation();
      confirmDeleteSession(s);
    };

    li.appendChild(av);
    li.appendChild(meta);
    li.appendChild(del);
    li.onclick = () => openSession(s.session_id);
    el.sessionList.appendChild(li);
  }
}

function confirmDeleteSession(sess) {
  showModal(
    `删除会话「${sess.name}」？`,
    [],
    async () => {
      try {
        await fetch(`/api/sessions/${sess.session_id}`, { method: "DELETE" });
      } catch (e) {
        console.warn("删除会话失败", e);
      }
      // 成功后后端会广播 session_deleted 事件刷新界面；这里兜底再拉一次列表
      loadSessions();
    },
    "删除"
  );
}

function renderContacts() {
  el.contactList.innerHTML = "";
  for (const c of Store.contacts) {
    const li = document.createElement("li");
    li.className = "contact-item";
    li.dataset.role = c.name;

    const av = document.createElement("div");
    av.className = "avatar";
    av.style.background = colorForAgent(c.name, c.display_name);
    av.textContent = (c.display_name || c.name).trim().charAt(0).toUpperCase();

    const meta = document.createElement("div");
    meta.className = "meta";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = c.display_name || c.name;
    const desc = document.createElement("div");
    desc.className = "desc";
    desc.textContent = c.description || c.role;
    meta.appendChild(name);
    meta.appendChild(desc);

    li.appendChild(av);
    li.appendChild(meta);
    li.title = "在群聊中点击可把该角色拉进群";
    li.onclick = () => {
      const sess = Store.sessions.find((s) => s.session_id === Store.activeSessionId);
      if (sess && sess.type === "group") {
        sendCommand({ type: "add_member", session_id: Store.activeSessionId, role: c.name, task: "" });
      } else {
        alert("请先打开一个群聊，再点击通讯录成员将其拉入");
      }
    };
    el.contactList.appendChild(li);
  }
}

// ===========================================================================
// 渲染：右侧成员
// ===========================================================================
function renderMembers() {
  el.memberList.innerHTML = "";
  for (const m of Store.members) {
    const li = document.createElement("li");
    li.className = "member-item";

    const av = document.createElement("div");
    av.className = "avatar sm";
    av.style.background = m.color || colorForAgent(m.agent_id, m.name);
    av.textContent = (m.name || "?").trim().charAt(0).toUpperCase();

    const meta = document.createElement("div");
    meta.className = "meta";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = m.name;
    // 角色简要定义（config.yaml 的 description），单行截断，hover 看全文
    const role = document.createElement("div");
    role.className = "role";
    role.textContent = m.role + (m.bio ? " · " + m.bio : "");
    role.title = m.bio || m.role;
    meta.appendChild(name);
    meta.appendChild(role);

    // task 是 Master 发给该子 Agent 的任务消息，与角色定义分开显示；
    // 可能很长，单行截断，hover 看全文
    if (m.task) {
      const taskEl = document.createElement("div");
      taskEl.className = "member-task";
      taskEl.textContent = "📋 " + m.task;
      taskEl.title = m.task;
      meta.appendChild(taskEl);
    }

    const dot = document.createElement("div");
    dot.className = "status-dot " + (m.status || "idle");

    li.appendChild(av);
    li.appendChild(meta);
    li.appendChild(dot);
    el.memberList.appendChild(li);
  }

  const sess = Store.sessions.find((s) => s.session_id === Store.activeSessionId);
  el.btnAddMember.classList.toggle("hidden", !(sess && sess.type === "group"));

  if (sess) {
    setHeader(sess.name, sess.type === "group" ? `${Store.members.length} 位成员` : "单聊");
  }
}

function setHeader(title, subtitle) {
  el.header.querySelector(".title").textContent = title;
  el.header.querySelector(".subtitle").textContent = subtitle || "";
}

// ===========================================================================
// 渲染：聊天区（按「轮次」组织）
// ===========================================================================
function renderChat() {
  el.messages.innerHTML = "";
  Store.openMsgs = {};
  Store.activeTurn = null;

  if (!Store.messages.length) {
    const hint = document.createElement("div");
    hint.className = "empty-hint";
    hint.textContent = "还没有消息，发一条试试吧";
    el.messages.appendChild(hint);
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

// 开启新一轮：渲染用户气泡（若有），返回轮次上下文。
// Agent 大卡片延迟到第一条 Agent 内容到达时才创建，避免空卡片。
function startTurn(userRec) {
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
  el.messages.appendChild(turn.el);
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

// 确保 Agent 大卡片存在（以本轮第一个发言者命名并显示头像）
function ensureTurnCard(turn, rec) {
  if (turn.card) return;
  turn.ownerId = rec.agent_id;

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

// 把一条消息作为 segment 追加到轮次卡片，返回 { seg, contentEl, rec, turn, kind }
function addSegment(turn, rec, opts = {}) {
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

  // 注意：不更新 turn.lastTextSeg —— 子 Agent 的结果不是 Master 的中间推理，
  // 不应被后续工具调用降级为「深度思考」
  return { seg, contentEl: content, rec, turn, kind: "text" };
}

// 文本段：流式期间按「最终回复」样式渲染；一旦被工具调用跟随，
// 由 addToolSegment 降级为「深度思考」样式并折叠
function addTextSegment(turn, rec, opts = {}) {
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

// 工具调用段：小号等宽冷灰；运行中显示 spinner，完成后折叠为一行摘要
function addToolSegment(turn, rec, opts = {}) {
  // 工具调用意味着它前面最近的文本段只是中间推理（深度思考）
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
function parseToolInfo(text, meta) {
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
function fillToolBody(ref, info) {
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

// ===========================================================================
// 会话操作
// ===========================================================================
export async function openSession(sid) {
  Store.activeSessionId = sid;
  Store.openMsgs = {};
  Store.activeTurn = null;
  Store.workflowSteps = [];
  Store.workflowComplete = false;
  try {
    const res = await fetch(`/api/sessions/${sid}`);
    const data = await res.json();
    Store.messages = data.messages || [];
    Store.members = data.members || [];
  } catch (e) {
    console.warn("打开会话失败", e);
    Store.messages = [];
    Store.members = [];
  }
  renderChat();
  renderMembers();
  renderWorkflow();
  renderSessions();
}

async function createSession(type, name) {
  const res = await fetch("/api/sessions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type, name }),
  });
  const data = await res.json();
  await loadSessions();
  // 后端返回扁平的会话对象；同时兼容 { session: {... }} 包装结构
  const sess = data.session || data;
  if (sess && sess.session_id) openSession(sess.session_id);
}

function doSend() {
  const text = el.input.value.trim();
  if (!text) return;
  if (!Store.activeSessionId) {
    alert("请先在左侧选择或新建一个会话");
    return;
  }
  sendCommand({ type: "send_message", session_id: Store.activeSessionId, text });
  el.input.value = "";
}

// ===========================================================================
// 实时事件处理（来自 WebSocket）
// ===========================================================================
export function handleEvent(ev) {
  switch (ev.type) {
    case "hello":
      if (ev.master_id) Store.masterId = ev.master_id;
      break;

    case "session_created":
      if (!Store.sessions.find((s) => s.session_id === ev.session.session_id)) {
        Store.sessions.push(ev.session);
      }
      renderSessions();
      break;

    case "session_deleted": {
      Store.sessions = Store.sessions.filter((s) => s.session_id !== ev.session_id);
      if (Store.activeSessionId === ev.session_id) {
        Store.activeSessionId = null;
        Store.messages = [];
        Store.members = [];
        Store.openMsgs = {};
        Store.activeTurn = null;
        Store.workflowSteps = [];
        Store.workflowComplete = false;
        renderChat();
        renderMembers();
        renderWorkflow();
        setHeader("选择一个会话开始", "");
      }
      renderSessions();
      break;
    }

    case "agent_join":
      if (ev.session_id === Store.activeSessionId &&
          !Store.members.find((m) => m.agent_id === ev.agent.agent_id)) {
        Store.members.push(ev.agent);
        renderMembers();
      }
      break;

    case "agent_update":
      if (ev.session_id === Store.activeSessionId) {
        const m = Store.members.find((x) => x.agent_id === ev.agent_id);
        if (m) {
          m.status = ev.status;
          renderMembers();
        }
      }
      break;

    case "history":
      if (ev.session_id === Store.activeSessionId) {
        Store.messages = ev.messages || [];
        Store.members = ev.members || [];
        Store.openMsgs = {};
        renderChat();
        renderMembers();
      }
      break;

    case "message_start":
      onMessageStart(ev);
      break;
    case "message_chunk":
      onMessageChunk(ev);
      break;
    case "message_replace":
      onMessageReplace(ev);
      break;
    case "message_end":
      onMessageEnd(ev);
      break;

    case "typing":
      // 预留：打字指示器，MVP 暂不展示
      break;

    case "workflow_step":
      onWorkflowStep(ev);
      break;

    case "workflow_complete":
      onWorkflowComplete(ev);
      break;

    case "tool_list":
      onToolList(ev);
      break;

    case "error":
      appendSystemLine("⚠️ " + ev.message);
      break;

    default:
      break;
  }
}

function onMessageStart(ev) {
  // 只渲染当前打开会话的消息，其他会话的事件忽略
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

function onMessageChunk(ev) {
  const ref = Store.openMsgs[ev.msg_id];
  if (!ref) return;
  ref.rec.text = (ref.rec.text || "") + ev.text;
  if (ref.kind === "text") {
    ref.contentEl.textContent += ev.text; // textContent 自动转义，安全
  }
  scrollToBottom();
}

function onMessageReplace(ev) {
  const ref = Store.openMsgs[ev.msg_id];
  if (!ref) return;
  ref.rec.text = ev.text;
  if (ref.kind === "text") {
    ref.contentEl.innerHTML = renderMarkdown(ev.text);
  }
  scrollToBottom();
}

function onMessageEnd(ev) {
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

function appendSystemLine(text) {
  const row = document.createElement("div");
  row.className = "msg system";
  const line = document.createElement("div");
  line.className = "system-line";
  line.textContent = text;
  row.appendChild(line);
  el.messages.appendChild(row);
  scrollToBottom();
}

// ===========================================================================
// 工作流 TODO 面板
// ===========================================================================
function onWorkflowStep(ev) {
  if (ev.session_id !== Store.activeSessionId) return;
  const step = ev.step;
  if (!step) return;

  // 更新或新增步骤
  const idx = Store.workflowSteps.findIndex((s) => s.step_id === step.step_id);
  if (idx >= 0) {
    Store.workflowSteps[idx] = step;
  } else {
    Store.workflowSteps.push(step);
  }
  renderWorkflow();
}

function onWorkflowComplete(ev) {
  if (ev.session_id !== Store.activeSessionId) return;
  Store.workflowComplete = true;
  // 同步最终步骤状态
  if (ev.steps) Store.workflowSteps = ev.steps;
  renderWorkflow();
  appendSystemLine(`✅ 工作流完成：${ev.done}/${ev.total} 个步骤成功` + (ev.failed ? `，${ev.failed} 个失败` : ""));
}

function renderWorkflow() {
  if (!Store.workflowSteps.length) {
    el.wfPanel.classList.add("hidden");
    return;
  }
  el.wfPanel.classList.remove("hidden");

  // 进度文字
  const done = Store.workflowSteps.filter((s) => s.status === "done").length;
  const total = Store.workflowSteps.length;
  el.wfProgress.textContent = `${done}/${total}`;
  if (Store.workflowComplete) {
    el.wfProgress.classList.add("complete");
  } else {
    el.wfProgress.classList.remove("complete");
  }

  // 渲染步骤列表
  el.wfSteps.innerHTML = "";
  for (const step of Store.workflowSteps) {
    const li = document.createElement("li");
    li.className = "wf-step " + step.status;

    const icon = document.createElement("span");
    icon.className = "wf-icon";
    switch (step.status) {
      case "done":    icon.textContent = "✅"; break;
      case "running": icon.textContent = "⏳"; break;
      case "failed":  icon.textContent = "❌"; break;
      default:        icon.textContent = "○"; break;
    }

    const body = document.createElement("div");
    body.className = "wf-body";
    const title = document.createElement("div");
    title.className = "wf-role";
    title.textContent = step.role;
    const desc = document.createElement("div");
    desc.className = "wf-task";
    desc.textContent = step.task || "";
    desc.title = step.task || "";
    body.appendChild(title);
    body.appendChild(desc);

    li.appendChild(icon);
    li.appendChild(body);
    el.wfSteps.appendChild(li);
  }

  // 滚动到可见
  el.wfPanel.scrollTop = el.wfPanel.scrollHeight;
}

function scrollToBottom() {
  el.messages.scrollTop = el.messages.scrollHeight;
}

// ===========================================================================
// MCP 工具面板
// ===========================================================================
function onToolList(ev) {
  Store.toolsList = ev.tools || [];
  Store.toolStats = ev.stats || {};
  renderTools();
}

function renderTools() {
  if (!Store.toolsList.length) {
    el.toolsPanel.classList.add("hidden");
    return;
  }
  el.toolsPanel.classList.remove("hidden");

  // 统计信息
  const s = Store.toolStats;
  el.toolsStats.textContent = `${s.tools || Store.toolsList.length} 个工具`;

  // 渲染工具列表
  el.toolsList.innerHTML = "";
  for (const tool of Store.toolsList) {
    const li = document.createElement("li");
    li.className = "tool-item";

    const icon = document.createElement("span");
    icon.className = "tool-icon";
    icon.textContent = "🔧";

    const body = document.createElement("div");
    body.className = "tool-body";

    const name = document.createElement("div");
    name.className = "tool-name";
    name.textContent = tool.name;

    const desc = document.createElement("div");
    desc.className = "tool-desc";
    desc.textContent = tool.description || "";
    desc.title = tool.description || "";

    body.appendChild(name);
    body.appendChild(desc);

    // 参数标签
    if (tool.parameters && tool.parameters.length) {
      const params = document.createElement("div");
      params.className = "tool-params";
      for (const p of tool.parameters) {
        const span = document.createElement("span");
        span.className = "tool-param" + (p.required ? " required" : "");
        span.textContent = p.name;
        span.title = p.description || p.name;
        params.appendChild(span);
      }
      body.appendChild(params);
    }

    li.appendChild(icon);
    li.appendChild(body);
    el.toolsList.appendChild(li);
  }
}

// ===========================================================================
// Markdown 轻量渲染（先整体转义，再注入有限标签，避免 XSS）
// ===========================================================================
function renderMarkdown(text) {
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

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// ===========================================================================
// 弹窗
// ===========================================================================
function showModal(title, fields, onSubmit, okLabel) {
  el.modalTitle.textContent = title;
  el.modalBody.innerHTML = "";
  const inputs = {};
  for (const f of fields) {
    const label = document.createElement("label");
    label.textContent = f.label;
    const input = document.createElement("input");
    input.placeholder = f.placeholder || "";
    input.value = f.value || "";
    el.modalBody.appendChild(label);
    el.modalBody.appendChild(input);
    inputs[f.key] = input;
  }
  pendingSubmit = () => {
    const vals = {};
    for (const k in inputs) vals[k] = inputs[k].value.trim();
    onSubmit(vals);
  };
  el.modalOk.textContent = okLabel || "确定";
  el.modal.classList.remove("hidden");
  const first = el.modalBody.querySelector("input");
  if (first) first.focus();
}

function closeModal() {
  el.modal.classList.add("hidden");
  pendingSubmit = null;
}
