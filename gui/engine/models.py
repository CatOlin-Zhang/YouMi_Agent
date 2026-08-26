"""GUI 侧的数据模型：会话、Agent 卡片、消息记录。

这些模型只描述「展示与持久化」所需的信息，不直接依赖 YouMi 引擎类型，
便于 JSON 序列化与前端消费。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


# 主 Agent 固定使用品牌蓝；其余 Agent 按 id 哈希取色，保证刷新后颜色稳定。
MASTER_COLOR = "#2f7cf6"
USER_COLOR = "#07c160"  # 微信绿（用户自己）
PALETTE = [
    "#e8590c", "#2b8a3e", "#9c36b5", "#c2255c", "#1864ab",
    "#0b7285", "#5f3dc4", "#d6336c", "#364fc7", "#5c940d",
]


def color_for(agent_id: str) -> str:
    if agent_id == "__user__":
        return USER_COLOR
    if agent_id == "master":
        return MASTER_COLOR
    h = abs(hash(agent_id))
    return PALETTE[h % len(PALETTE)]


def avatar_letter(name: str) -> str:
    for ch in name:
        if ch.strip():
            return ch.upper()
    return "?"


def new_id(prefix: str = "id") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class AgentCard:
    """Agent 的展示卡片（头像底色、名称、角色、状态）。"""

    agent_id: str
    name: str
    role: str = "agent"
    color: str = ""
    status: str = "idle"
    bio: str = ""      # 角色简要定义（来自 agents/<role>/config.yaml 的 metadata.description）
    task: str = ""     # Master 发给该子 Agent 的任务消息（不属于角色定义）

    def __post_init__(self) -> None:
        if not self.color:
            self.color = color_for(self.agent_id)

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "role": self.role,
            "color": self.color,
            "status": self.status,
            "bio": self.bio,
            "task": self.task,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AgentCard":
        return cls(
            agent_id=d["agent_id"],
            name=d.get("name", d["agent_id"]),
            role=d.get("role", "agent"),
            color=d.get("color", ""),
            status=d.get("status", "idle"),
            bio=d.get("bio", ""),
            task=d.get("task", ""),
        )


@dataclass
class MessageRecord:
    """一条聊天消息（气泡 / 工具卡片 / 系统行）。"""

    msg_id: str
    session_id: str
    agent_id: str
    agent_name: str
    role: str          # user | assistant | system | tool
    kind: str          # text | tool | system
    text: str = ""
    ts: float = field(default_factory=time.time)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "role": self.role,
            "kind": self.kind,
            "text": self.text,
            "ts": self.ts,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MessageRecord":
        return cls(
            msg_id=d["msg_id"],
            session_id=d["session_id"],
            agent_id=d["agent_id"],
            agent_name=d.get("agent_name", ""),
            role=d.get("role", "assistant"),
            kind=d.get("kind", "text"),
            text=d.get("text", ""),
            ts=d.get("ts", time.time()),
            meta=d.get("meta", {}),
        )


@dataclass
class Session:
    """一个聊天会话：单聊（1 个 Agent）或群聊（Master + 多个成员）。"""

    session_id: str
    type: str                 # single | group
    name: str
    owner_agent_id: str = ""
    member_ids: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "type": self.type,
            "name": self.name,
            "owner_agent_id": self.owner_agent_id,
            "member_ids": list(self.member_ids),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Session":
        return cls(
            session_id=d["session_id"],
            type=d.get("type", "single"),
            name=d.get("name", ""),
            owner_agent_id=d.get("owner_agent_id", ""),
            member_ids=list(d.get("member_ids", [])),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
        )
