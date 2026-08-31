// UI 入口 — 初始化 DOM 引用、绑定按钮事件，并将事件分发给子模块。
//
// 子模块职责划分：
//   chat-renderer.js  — 聊天区轮次渲染（消息段、工具调用、深度思考）
//   session-panel.js  — 侧边栏会话/通讯录/成员 + 数据加载
//   panels.js         — 工作流面板 + MCP 工具面板
//   modal.js          — 通用弹窗
//
// app.js 只需要从本文件导入 initUI, loadAgents, loadSessions, handleEvent。

import { Store } from "./state.js";
import { sendCommand } from "./ws.js";

// 子模块
import { initChat, renderChat, startTurn, addSegment, scrollToBottom,
         onMessageStart, onMessageChunk, onMessageReplace, onMessageEnd } from "./chat-renderer.js";
import { initSidebar, loadAgents, loadSessions, switchTab,
         openSession, createSession, doSend,
         onSessionCreated, onSessionDeleted, onAgentJoin, onAgentUpdate, onHistory } from "./session-panel.js";
import { initPanels, onWorkflowStep, onWorkflowComplete, onToolList,
         appendSystemLine, renderWorkflow } from "./panels.js";
import { initModal, showModal } from "./modal.js";

// ===========================================================================
// 初始化
// ===========================================================================
export function initUI() {
  // 收集所有 DOM 引用
  const refs = {
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
    wfPanel: document.getElementById("workflow-panel"),
    wfSteps: document.getElementById("workflow-steps"),
    wfProgress: document.getElementById("workflow-progress"),
    toolsPanel: document.getElementById("tools-panel"),
    toolsList: document.getElementById("tools-list"),
    toolsStats: document.getElementById("tools-stats"),
  };

  // 分发给子模块
  initChat({ messages: refs.messages });
  initSidebar(refs);
  initPanels({
    wfPanel: refs.wfPanel, wfSteps: refs.wfSteps, wfProgress: refs.wfProgress,
    toolsPanel: refs.toolsPanel, toolsList: refs.toolsList, toolsStats: refs.toolsStats,
    messages: refs.messages,
  });
  initModal({
    modal: refs.modal, modalTitle: refs.modalTitle, modalBody: refs.modalBody,
    modalCancel: refs.modalCancel, modalOk: refs.modalOk,
  });

  // 绑定按钮事件
  refs.tabChats.onclick = () => switchTab("chats");
  refs.tabContacts.onclick = () => switchTab("contacts");

  refs.btnNewChat.onclick = () => createSession("single", "单聊");
  refs.btnNewGroup.onclick = () =>
    showModal(
      "新建群聊",
      [{ key: "name", label: "群名称", placeholder: "例如：调研小组" }],
      (v) => createSession("group", v.name || "群聊")
    );

  refs.btnAddMember.onclick = () =>
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

  refs.send.onclick = doSend;
  refs.input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      doSend();
    }
  });
}

// ===========================================================================
// 事件分发（来自 WebSocket，由 app.js 转发）
// ===========================================================================
export function handleEvent(ev) {
  switch (ev.type) {
    case "hello":
      if (ev.master_id) Store.masterId = ev.master_id;
      break;

    case "session_created":
      onSessionCreated(ev);
      break;
    case "session_deleted":
      onSessionDeleted(ev);
      break;

    case "agent_join":
      onAgentJoin(ev);
      break;
    case "agent_update":
      onAgentUpdate(ev);
      break;

    case "history":
      onHistory(ev);
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

// Re-export（供 app.js 使用）
export { loadAgents, loadSessions };
