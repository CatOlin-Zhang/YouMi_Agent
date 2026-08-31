// 会话 / 通讯录 / 成员面板 — 左侧边栏与右侧成员列表
//
// 包含:
// - 会话列表渲染与操作（打开、新建、删除）
// - 通讯录渲染（Agent 列表，可拉入群聊）
// - 成员列表渲染
// - 数据加载（loadAgents, loadSessions）
//
// 依赖: initSidebar() 注入 DOM 引用后可使用。

import { Store, colorForAgent } from "./state.js";
import { sendCommand } from "./ws.js";
import { renderChat, startTurn, addSegment, scrollToBottom } from "./chat-renderer.js";
import { renderWorkflow } from "./panels.js";
import { showModal } from "./modal.js";

let _side = {};

/** 注入侧边栏 / 成员面板 DOM 引用（由 ui.js 的 initUI 调用） */
export function initSidebar(els) {
  _side = els;
}

// ---------------------------------------------------------------------------
// 数据加载
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Tab 切换
// ---------------------------------------------------------------------------

export function switchTab(tab) {
  _side.tabChats.classList.toggle("active", tab === "chats");
  _side.tabContacts.classList.toggle("active", tab === "contacts");
  _side.tabChatsBody.classList.toggle("hidden", tab !== "chats");
  _side.tabContactsBody.classList.toggle("hidden", tab !== "contacts");
}

// ---------------------------------------------------------------------------
// 会话列表
// ---------------------------------------------------------------------------

function renderSessions() {
  _side.sessionList.innerHTML = "";
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
    _side.sessionList.appendChild(li);
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

// ---------------------------------------------------------------------------
// 通讯录
// ---------------------------------------------------------------------------

function renderContacts() {
  _side.contactList.innerHTML = "";
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
    _side.contactList.appendChild(li);
  }
}

// ---------------------------------------------------------------------------
// 成员列表
// ---------------------------------------------------------------------------

function renderMembers() {
  _side.memberList.innerHTML = "";
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
    const role = document.createElement("div");
    role.className = "role";
    role.textContent = m.role + (m.bio ? " · " + m.bio : "");
    role.title = m.bio || m.role;
    meta.appendChild(name);
    meta.appendChild(role);

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
    _side.memberList.appendChild(li);
  }

  const sess = Store.sessions.find((s) => s.session_id === Store.activeSessionId);
  _side.btnAddMember.classList.toggle("hidden", !(sess && sess.type === "group"));

  if (sess) {
    setHeader(sess.name, sess.type === "group" ? `${Store.members.length} 位成员` : "单聊");
  }
}

function setHeader(title, subtitle) {
  _side.header.querySelector(".title").textContent = title;
  _side.header.querySelector(".subtitle").textContent = subtitle || "";
}

// ---------------------------------------------------------------------------
// 会话操作
// ---------------------------------------------------------------------------

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
    // 同步会话列表中的成员计数，避免左侧预览显示旧数据
    if (data.session) {
      const idx = Store.sessions.findIndex((s) => s.session_id === sid);
      if (idx >= 0) {
        Store.sessions[idx] = data.session;
      }
    }
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

export async function createSession(type, name) {
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

export function doSend() {
  const text = _side.input.value.trim();
  if (!text) return;
  if (!Store.activeSessionId) {
    alert("请先在左侧选择或新建一个会话");
    return;
  }
  sendCommand({ type: "send_message", session_id: Store.activeSessionId, text });
  _side.input.value = "";
}

// ---------------------------------------------------------------------------
// WebSocket 实时事件 → 侧边栏/成员 相关的刷新
// ---------------------------------------------------------------------------

export function onSessionCreated(ev) {
  if (!Store.sessions.find((s) => s.session_id === ev.session.session_id)) {
    Store.sessions.push(ev.session);
  }
  renderSessions();
}

export function onSessionDeleted(ev) {
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
}

export function onAgentJoin(ev) {
  if (ev.session_id === Store.activeSessionId &&
      !Store.members.find((m) => m.agent_id === ev.agent.agent_id)) {
    Store.members.push(ev.agent);
    renderMembers();
  }
  // 同步更新左侧会话列表中的成员计数
  const sess = Store.sessions.find((s) => s.session_id === ev.session_id);
  if (sess && !sess.member_ids.includes(ev.agent.agent_id)) {
    sess.member_ids.push(ev.agent.agent_id);
    renderSessions();
  }
}

export function onAgentUpdate(ev) {
  if (ev.session_id === Store.activeSessionId) {
    const m = Store.members.find((x) => x.agent_id === ev.agent_id);
    if (m) {
      m.status = ev.status;
      renderMembers();
    }
  }
}

export function onHistory(ev) {
  if (ev.session_id === Store.activeSessionId) {
    Store.messages = ev.messages || [];
    Store.members = ev.members || [];
    Store.openMsgs = {};
    renderChat();
    renderMembers();
  }
}
