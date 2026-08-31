// 工作流面板 & MCP 工具面板渲染
//
// 工作流面板: 展示 workflow_step / workflow_complete 事件的步骤列表
// MCP 工具面板: 展示 tool_list 事件的工具清单和统计
//
// 依赖: initPanels() 注入 DOM 引用后可使用。

import { Store } from "./state.js";

let _panel = {};

/** 注入面板相关 DOM 引用（由 ui.js 的 initUI 调用） */
export function initPanels(els) {
  _panel = els;
}

// ---------------------------------------------------------------------------
// 工作流 TODO 面板
// ---------------------------------------------------------------------------

export function onWorkflowStep(ev) {
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

export function onWorkflowComplete(ev) {
  if (ev.session_id !== Store.activeSessionId) return;
  Store.workflowComplete = true;
  // 同步最终步骤状态
  if (ev.steps) Store.workflowSteps = ev.steps;
  renderWorkflow();
  appendSystemLine(`✅ 工作流完成：${ev.done}/${ev.total} 个步骤成功` + (ev.failed ? `，${ev.failed} 个失败` : ""));
}

export function renderWorkflow() {
  if (!Store.workflowSteps.length) {
    _panel.wfPanel.classList.add("hidden");
    return;
  }
  _panel.wfPanel.classList.remove("hidden");

  // 进度文字
  const done = Store.workflowSteps.filter((s) => s.status === "done").length;
  const total = Store.workflowSteps.length;
  _panel.wfProgress.textContent = `${done}/${total}`;
  if (Store.workflowComplete) {
    _panel.wfProgress.classList.add("complete");
  } else {
    _panel.wfProgress.classList.remove("complete");
  }

  // 渲染步骤列表
  _panel.wfSteps.innerHTML = "";
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
    _panel.wfSteps.appendChild(li);
  }

  // 滚动到可见
  _panel.wfPanel.scrollTop = _panel.wfPanel.scrollHeight;
}

// ---------------------------------------------------------------------------
// MCP 工具面板
// ---------------------------------------------------------------------------

export function onToolList(ev) {
  Store.toolsList = ev.tools || [];
  Store.toolStats = ev.stats || {};
  renderTools();
}

function renderTools() {
  if (!Store.toolsList.length) {
    _panel.toolsPanel.classList.add("hidden");
    return;
  }
  _panel.toolsPanel.classList.remove("hidden");

  // 统计信息
  const s = Store.toolStats;
  _panel.toolsStats.textContent = `${s.tools || Store.toolsList.length} 个工具`;

  // 渲染工具列表
  _panel.toolsList.innerHTML = "";
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
    _panel.toolsList.appendChild(li);
  }
}

// ---------------------------------------------------------------------------
// 辅助
// ---------------------------------------------------------------------------

/** 在聊天区追加一行系统提示（也被 ui.js 的 error 事件使用） */
export function appendSystemLine(text) {
  const row = document.createElement("div");
  row.className = "msg system";
  const line = document.createElement("div");
  line.className = "system-line";
  line.textContent = text;
  row.appendChild(line);
  _panel.messages.appendChild(row);
  _panel.messages.scrollTop = _panel.messages.scrollHeight;
}
