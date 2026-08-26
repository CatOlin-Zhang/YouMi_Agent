"""GUI 事件协议（服务端 → 客户端 / 客户端 → 服务端）。

所有事件都是 JSON 对象，统一带 ``type`` 字段。这里只负责构造事件字典，
不涉及网络。事件通过 WebSocketHub 广播给前端。
"""

from __future__ import annotations

import time


def _ev(type_: str, **kw) -> dict:
    ev = {"type": type_, "ts": time.time()}
    ev.update(kw)
    return ev


# ---- 连接生命周期 ----
def hello(master_id: str = "") -> dict:
    return _ev("hello", master_id=master_id)


def pong() -> dict:
    return _ev("pong")


# ---- 会话 / 联系人 ----
def session_created(session: dict) -> dict:
    return _ev("session_created", session=session)


def session_deleted(session_id: str) -> dict:
    return _ev("session_deleted", session_id=session_id)


def agent_join(session_id: str, agent: dict) -> dict:
    return _ev("agent_join", session_id=session_id, agent=agent)


def agent_update(session_id: str, agent_id: str, status: str) -> dict:
    return _ev("agent_update", session_id=session_id, agent_id=agent_id, status=status)


# ---- 消息（气泡）----
def message_start(
    msg_id: str,
    session_id: str,
    agent_id: str,
    agent_name: str,
    role: str,
    kind: str,
    text: str = "",
    color: str = "",
) -> dict:
    return _ev(
        "message_start",
        msg_id=msg_id,
        session_id=session_id,
        agent_id=agent_id,
        agent_name=agent_name,
        role=role,
        kind=kind,
        text=text,
        color=color,
    )


def message_chunk(msg_id: str, text: str) -> dict:
    return _ev("message_chunk", msg_id=msg_id, text=text)


def message_replace(msg_id: str, text: str) -> dict:
    return _ev("message_replace", msg_id=msg_id, text=text)


def message_end(msg_id: str, text: str = "", meta: dict | None = None) -> dict:
    return _ev("message_end", msg_id=msg_id, text=text, meta=meta or {})


def typing(session_id: str, agent_id: str, on: bool) -> dict:
    return _ev("typing", session_id=session_id, agent_id=agent_id, on=on)


def error(message: str, session_id: str | None = None) -> dict:
    return _ev("error", message=message, session_id=session_id)


def history(session_id: str, messages: list, members: list) -> dict:
    return _ev("history", session_id=session_id, messages=messages, members=members)


# ---- MCP 工具面板 ----
def tool_list(tools: list[dict], stats: dict) -> dict:
    """MCP 工具列表更新（首次连接或工具变更时广播）。"""
    return _ev("tool_list", tools=tools, stats=stats)


# ---- 工作流追踪 ----
def workflow_step(session_id: str, step: dict) -> dict:
    """工作流步骤状态变更（新增 / 开始运行 / 完成 / 失败）。"""
    return _ev("workflow_step", session_id=session_id, step=step)


def workflow_complete(
    session_id: str,
    total: int,
    done: int,
    failed: int,
    steps: list[dict],
) -> dict:
    """所有工作流步骤已完成。"""
    return _ev(
        "workflow_complete",
        session_id=session_id,
        total=total,
        done=done,
        failed=failed,
        steps=steps,
    )
