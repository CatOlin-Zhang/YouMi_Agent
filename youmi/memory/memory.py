"""
记忆系统统一调用接口

面向 Agent 的唯一入口。Agent 通过 MemoryManager 与记忆策略交互，
无需关心底层使用的是哪种策略。

用法::

    from youmi.memory import MemoryManager

    # 方式1: 使用预置策略名称
    manager = MemoryManager(agent_id="a1", strategy="full")

    # 方式2: 使用带 LLM 的策略
    manager = MemoryManager(agent_id="a1", strategy="summary", llm_call=my_llm)

    # 方式3: 使用自定义策略文件
    manager = MemoryManager(agent_id="a1", strategy="/path/to/my_strategy.py")

    # 方式4: 直接传入策略实例
    manager = MemoryManager(agent_id="a1", strategy=FullMemoryStrategy(agent_id="a1"))

    # 方式5: 启用 Session 持久化
    from youmi.memory.backends import SQLiteBackend
    backend = SQLiteBackend(db_path="sessions.db")
    manager = MemoryManager(agent_id="a1", strategy="full", persistence_backend=backend)

    # 统一调用
    await manager.initialize()
    await manager.on_message("user", "你好")
    context = await manager.get_context()
    await manager.on_session_end()
"""

from __future__ import annotations

import uuid
from typing import Any

from youmi.memory.strategies.base import MemoryStrategy
from youmi.memory.strategies import (
    FullMemoryStrategy,
    SummaryMemoryStrategy,
    LSTMMemoryStrategy,
    create_strategy,
    list_strategies,
    register_strategy,
    LLMCallFn,
)

# 延迟导入避免循环依赖
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from youmi.memory.backends.base import PersistenceBackend

# 运行时导入 (供 __all__ 重新导出)
from youmi.memory.backends.base import PersistenceBackend as _PersistenceBackend  # noqa: E402
from youmi.memory.backends.sqlite_backend import SQLiteBackend as _SQLiteBackend  # noqa: E402
from youmi.memory.backends.file_backend import FileBackend as _FileBackend  # noqa: E402
from youmi.memory.compaction import ContextCompactor as _ContextCompactor  # noqa: E402

# 公开名称绑定
PersistenceBackend = _PersistenceBackend
SQLiteBackend = _SQLiteBackend
FileBackend = _FileBackend
ContextCompactor = _ContextCompactor


class MemoryManager:
    """记忆管理器 — Agent 与记忆策略之间的统一桥梁

    屏蔽策略差异，提供一致的调用接口。

    Args:
        agent_id: Agent 唯一 ID
        strategy: 记忆策略。支持以下形式:
            - str: 预置策略名称 ("full" / "summary" / "lstm")
            - str: 自定义策略 .py 文件路径
            - MemoryStrategy: 策略实例 (直接使用)
        config: 策略配置参数
        llm_call: LLM 调用函数 (summary / lstm 策略使用)
        persistence_backend: Session 持久化后端 (可选)
    """

    def __init__(
        self,
        agent_id: str,
        strategy: str | MemoryStrategy = "full",
        config: dict[str, Any] | None = None,
        llm_call: LLMCallFn | None = None,
        persistence_backend: PersistenceBackend | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._persistence = persistence_backend
        self._current_session_id: str = ""

        if isinstance(strategy, MemoryStrategy):
            self._strategy = strategy
        else:
            self._strategy = create_strategy(
                strategy=strategy,
                agent_id=agent_id,
                config=config,
                llm_call=llm_call,
            )

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def strategy(self) -> MemoryStrategy:
        """当前使用的记忆策略实例"""
        return self._strategy

    @property
    def strategy_name(self) -> str:
        """当前策略名称"""
        return self._strategy.strategy_name

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def persistence(self) -> PersistenceBackend | None:
        """当前持久化后端 (None 表示未启用)"""
        return self._persistence

    @property
    def current_session_id(self) -> str:
        """当前 session ID (空字符串表示未创建)"""
        return self._current_session_id

    # ------------------------------------------------------------------
    # 生命周期 (与 Agent 生命周期对齐)
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """初始化记忆策略 + 持久化后端 (建立连接、加载历史数据等)"""
        await self._strategy.initialize()
        if self._persistence is not None:
            await self._persistence.initialize()

    async def on_session_end(self) -> None:
        """会话结束钩子 (归档、持久化、生成摘要等)"""
        await self._strategy.on_session_end()
        # 持久化当前 session
        if self._persistence is not None and self._current_session_id:
            await self._save_current_session()

    # ------------------------------------------------------------------
    # 核心操作
    # ------------------------------------------------------------------

    async def on_message(self, role: str, content: str, **kwargs: Any) -> None:
        """记录一条消息

        Args:
            role: 消息角色 ("user" / "assistant" / "system" / "tool" / "agent")
            content: 消息内容
            **kwargs: 扩展参数
        """
        await self._strategy.on_message(role, content, **kwargs)

    async def get_context(self, **kwargs: Any) -> list[dict[str, str]]:
        """获取当前记忆上下文 (供 LLM 推理使用)

        Returns:
            OpenAI messages 格式的消息列表:
            [{"role": "user", "content": "..."}, ...]
        """
        return await self._strategy.get_context(**kwargs)

    async def clear(self) -> None:
        """清空记忆 (具体行为由策略决定)"""
        await self._strategy.clear()

    # ------------------------------------------------------------------
    # 记忆检索 (P6)
    # ------------------------------------------------------------------

    async def search(
        self,
        query: str,
        top_k: int = 5,
        embedding_client: Any | None = None,
    ) -> list[dict[str, str]]:
        """检索记忆

        接入 EmbeddingClient 时对当前上下文消息做向量语义检索；
        未接入时降级为策略的关键词检索 (MemoryStrategy.search)。

        用法::

            # 关键词检索
            hits = await manager.search("文件路径")

            # 向量检索
            hits = await manager.search("文件路径", embedding_client=embedder)

        Args:
            query: 查询文本
            top_k: 最多返回条数
            embedding_client: EmbeddingClient 实例 (None = 关键词检索)

        Returns:
            匹配的消息列表 [{"role": ..., "content": ...}]
        """
        if not query.strip():
            return []

        # 向量语义检索
        if embedding_client is not None:
            try:
                context = await self.get_context()
                if not context:
                    return []
                vectors = await embedding_client.embed(
                    [m.get("content", "") for m in context],
                )
                query_vec = await embedding_client.embed_one(query)
                scores = await embedding_client.similarity(query_vec, vectors)
                scored = sorted(
                    zip(context, scores), key=lambda x: x[1], reverse=True,
                )
                return [m for m, s in scored[:top_k] if s > 0.1]
            except Exception:
                pass  # 向量检索失败 → 关键词降级

        # 关键词降级检索 (委托给策略)
        return await self._strategy.search(query, top_k=top_k)

    # ------------------------------------------------------------------
    # Session 持久化
    # ------------------------------------------------------------------

    def start_session(self, session_id: str = "") -> str:
        """开始一个新的 session

        Args:
            session_id: session ID，为空则自动生成

        Returns:
            session ID
        """
        self._current_session_id = session_id or uuid.uuid4().hex[:16]
        return self._current_session_id

    async def restore_session(self, session_id: str = "") -> list[dict[str, str]] | None:
        """从持久化后端恢复 session 消息

        Args:
            session_id: 指定 session ID。为空则恢复最近的 session。

        Returns:
            恢复的消息列表 (OpenAI 格式)，无 session 或后端未启用时返回 None
        """
        if self._persistence is None:
            return None

        if session_id:
            messages = await self._persistence.load_messages(session_id)
            if messages:
                self._current_session_id = session_id
                return messages
            return None

        # 自动恢复最近的 session
        latest = await self._persistence.get_latest_session(self._agent_id)
        if latest is None:
            return None

        messages = await self._persistence.load_messages(latest.session_id)
        if messages:
            self._current_session_id = latest.session_id
            return messages
        return None

    async def save_session(self, messages: list[dict[str, Any]], session_id: str = "") -> None:
        """将消息列表保存到持久化后端

        Args:
            messages: OpenAI 格式消息列表
            session_id: session ID，为空则使用当前 session
        """
        if self._persistence is None:
            return
        sid = session_id or self._current_session_id
        if not sid:
            sid = self.start_session()
        await self._persistence.save_session(sid, self._agent_id, messages)

    async def _save_current_session(self) -> None:
        """保存当前策略中的消息到持久化后端"""
        if self._persistence is None or not self._current_session_id:
            return
        messages = await self._strategy.get_context()
        await self._persistence.save_session(
            self._current_session_id, self._agent_id, messages,
        )

    async def close(self) -> None:
        """关闭持久化后端"""
        if self._persistence is not None:
            await self._persistence.close()

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    async def snapshot(self) -> dict[str, Any]:
        """获取记忆状态快照"""
        return await self._strategy.snapshot()

    def __repr__(self) -> str:
        return (
            f"<MemoryManager agent={self._agent_id!r} "
            f"strategy={self._strategy.strategy_name!r}>"
        )


__all__ = [
    "MemoryManager",
    "MemoryStrategy",
    "FullMemoryStrategy",
    "SummaryMemoryStrategy",
    "LSTMMemoryStrategy",
    "create_strategy",
    "list_strategies",
    "register_strategy",
    "LLMCallFn",
    # 持久化后端
    "PersistenceBackend",
    "SQLiteBackend",
    "FileBackend",
    # Compactor
    "ContextCompactor",
]
