"""
记忆存储抽象基类与内存实现

设计原则:
- 短期记忆 (ShortTermBackend): 有序消息流，FIFO 淘汰，快速读写
- 长期记忆 (LongTermBackend):  语义检索，向量化存储，跨会话持久化
- MemoryAdapter: 面向 Agent 的统一记忆 API，屏蔽后端差异
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 记忆条目
# ---------------------------------------------------------------------------

class MemoryEntry(BaseModel):
    """单条记忆"""

    entry_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    agent_id: str
    memory_type: str = "short_term"     # "short_term" | "long_term" | "shared"
    role: str = "user"                  # "user" | "assistant" | "system" | "tool" | "agent"
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    ttl_seconds: int | None = None      # None 表示永不过期


# ---------------------------------------------------------------------------
# 抽象后端接口
# ---------------------------------------------------------------------------

class ShortTermBackend(ABC):
    """短期记忆后端 — 面向当前会话的消息存储"""

    @abstractmethod
    async def put(self, entry: MemoryEntry) -> None:
        """写入一条消息"""
        ...

    @abstractmethod
    async def get_latest(self, agent_id: str, limit: int = 50) -> list[MemoryEntry]:
        """获取最近的 N 条消息 (按时间正序)"""
        ...

    @abstractmethod
    async def clear(self, agent_id: str) -> None:
        """清空指定 Agent 的短期记忆"""
        ...

    @abstractmethod
    async def count(self, agent_id: str) -> int:
        """当前消息数"""
        ...


class LongTermBackend(ABC):
    """长期记忆后端 — 面向跨会话知识的语义存储"""

    @abstractmethod
    async def store(self, entry: MemoryEntry) -> None:
        """持久化一条长期记忆"""
        ...

    @abstractmethod
    async def search(
        self,
        agent_id: str,
        query: str,
        query_embedding: list[float] | None = None,
        top_k: int = 5,
    ) -> list[MemoryEntry]:
        """语义检索 — query_embedding 由上层 Embedding 模型生成"""
        ...

    @abstractmethod
    async def update(self, entry: MemoryEntry) -> None:
        """更新一条已有记忆"""
        ...

    @abstractmethod
    async def delete(self, entry_id: str) -> None:
        """删除一条记忆"""
        ...

    @abstractmethod
    async def list_all(self, agent_id: str, limit: int = 100) -> list[MemoryEntry]:
        """列出所有长期记忆 (按时间倒序)"""
        ...


# ---------------------------------------------------------------------------
# 内存实现 (默认后端，适用于开发/测试/轻量场景)
# ---------------------------------------------------------------------------

class InMemoryShortTermBackend(ShortTermBackend):
    """纯内存短期记忆 — 按 agent_id 隔离的消息列表"""

    def __init__(self, max_messages: int = 100) -> None:
        self._max_messages = max_messages
        self._store: dict[str, list[MemoryEntry]] = {}

    async def put(self, entry: MemoryEntry) -> None:
        bucket = self._store.setdefault(entry.agent_id, [])
        bucket.append(entry)
        # FIFO 淘汰
        if len(bucket) > self._max_messages:
            self._store[entry.agent_id] = bucket[-self._max_messages:]

    async def get_latest(self, agent_id: str, limit: int = 50) -> list[MemoryEntry]:
        bucket = self._store.get(agent_id, [])
        return list(bucket[-limit:])

    async def clear(self, agent_id: str) -> None:
        self._store.pop(agent_id, None)

    async def count(self, agent_id: str) -> int:
        return len(self._store.get(agent_id, []))


class InMemoryLongTermBackend(LongTermBackend):
    """纯内存长期记忆 — 基于关键词匹配的简易检索 (无向量依赖)"""

    def __init__(self) -> None:
        self._store: dict[str, MemoryEntry] = {}  # entry_id -> entry

    async def store(self, entry: MemoryEntry) -> None:
        self._store[entry.entry_id] = entry

    async def search(
        self,
        agent_id: str,
        query: str,
        query_embedding: list[float] | None = None,
        top_k: int = 5,
    ) -> list[MemoryEntry]:
        # 简易关键词匹配；正式实现应接入向量库
        candidates = [e for e in self._store.values() if e.agent_id == agent_id]
        query_lower = query.lower()
        scored = [
            (e, sum(1 for word in query_lower.split() if word in e.content.lower()))
            for e in candidates
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [e for e, _ in scored[:top_k] if _ > 0]

    async def update(self, entry: MemoryEntry) -> None:
        if entry.entry_id in self._store:
            entry.updated_at = datetime.utcnow()
            self._store[entry.entry_id] = entry

    async def delete(self, entry_id: str) -> None:
        self._store.pop(entry_id, None)

    async def list_all(self, agent_id: str, limit: int = 100) -> list[MemoryEntry]:
        entries = [e for e in self._store.values() if e.agent_id == agent_id]
        entries.sort(key=lambda e: e.created_at, reverse=True)
        return entries[:limit]


# ---------------------------------------------------------------------------
# MemoryAdapter — 面向 Agent 的统一记忆 API
# ---------------------------------------------------------------------------

class MemoryAdapter:
    """Agent 记忆适配器

    每个 Agent 实例持有一个 MemoryAdapter，提供:
    - 短期消息读写 (对话上下文)
    - 长期知识存取 (语义检索)
    - 后端隔离 (agent_id 分区)

    用法::

        adapter = MemoryAdapter(agent_id="a1", short_term=InMemoryShortTermBackend())
        await adapter.add_message("user", "你好")
        history = await adapter.get_conversation()
    """

    def __init__(
        self,
        agent_id: str,
        short_term: ShortTermBackend | None = None,
        long_term: LongTermBackend | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._short_term: ShortTermBackend = short_term or InMemoryShortTermBackend()
        self._long_term: LongTermBackend | None = long_term

    # -- 短期记忆 --

    async def add_message(
        self,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """写入一条短期记忆消息，返回 entry_id"""
        entry = MemoryEntry(
            agent_id=self._agent_id,
            memory_type="short_term",
            role=role,
            content=content,
            metadata=metadata or {},
        )
        await self._short_term.put(entry)
        return entry.entry_id

    async def get_conversation(self, limit: int = 50) -> list[MemoryEntry]:
        """获取对话历史 (时间正序)"""
        return await self._short_term.get_latest(self._agent_id, limit)

    async def clear_conversation(self) -> None:
        """清空当前对话上下文"""
        await self._short_term.clear(self._agent_id)

    async def message_count(self) -> int:
        """当前短期消息数量"""
        return await self._short_term.count(self._agent_id)

    # -- 长期记忆 --

    @property
    def has_long_term(self) -> bool:
        return self._long_term is not None

    async def store_knowledge(
        self,
        content: str,
        metadata: dict[str, Any] | None = None,
        embedding: list[float] | None = None,
    ) -> str:
        """存入一条长期知识，返回 entry_id"""
        if self._long_term is None:
            raise RuntimeError(f"Agent '{self._agent_id}' 未配置长期记忆后端")
        entry = MemoryEntry(
            agent_id=self._agent_id,
            memory_type="long_term",
            content=content,
            metadata=metadata or {},
            embedding=embedding,
        )
        await self._long_term.store(entry)
        return entry.entry_id

    async def search_knowledge(
        self,
        query: str,
        query_embedding: list[float] | None = None,
        top_k: int = 5,
    ) -> list[MemoryEntry]:
        """语义检索长期记忆"""
        if self._long_term is None:
            return []
        return await self._long_term.search(
            self._agent_id, query, query_embedding, top_k
        )

    async def delete_knowledge(self, entry_id: str) -> None:
        if self._long_term is None:
            raise RuntimeError(f"Agent '{self._agent_id}' 未配置长期记忆后端")
        await self._long_term.delete(entry_id)

    # -- 归档 --

    async def archive_session(self, summary: str, metadata: dict[str, Any] | None = None) -> str | None:
        """将本次会话摘要归档到长期记忆

        返回归档的 entry_id，若未配置长期记忆则返回 None。
        """
        if self._long_term is None:
            return None
        return await self.store_knowledge(
            content=summary,
            metadata={"source": "session_archive", **(metadata or {})},
        )

    # -- 诊断 --

    async def snapshot(self) -> dict[str, Any]:
        """返回当前记忆状态快照 (用于调试/可观测)"""
        result: dict[str, Any] = {
            "agent_id": self._agent_id,
            "short_term_count": await self.message_count(),
            "has_long_term": self.has_long_term,
        }
        if self._long_term:
            entries = await self._long_term.list_all(self._agent_id)
            result["long_term_count"] = len(entries)
        return result
