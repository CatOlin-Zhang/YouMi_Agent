"""
ToolVault — 工具数据库与动态上下文管理

基于向量语义匹配的工具发现与三级状态管理:
- HOT (热态): 完整 schema 在 LLM 上下文中，Agent 可直接调用
- WARM (温态): 仅摘要可见，再次使用时从 Vault 直接加载，跳过发现
- COLD (冷态): 仅在 Vault 中存在向量头，需通过语义搜索发现

回收策略:
- 必备工具 (essential=True) 永不回收
- 连续 N 轮未使用的热态工具自动降级为温态
- 温态工具再次需要时直接提升到热态

持久化:
- 可选集成 ToolStore (SQLite 持久化存储层)
- 内存字典 _entries 作为一级缓存，ToolStore 作为持久化层
- store=None 时保持纯内存行为 (向后兼容)

用法::

    from youmi.mcp.vault import ToolVault, ToolEntry, ToolContextTier
    from youmi.mcp.tool_store import ToolStore
    from youmi.llm.embeddings import EmbeddingClient

    # 纯内存模式 (向后兼容)
    vault = ToolVault(embedding_client=EmbeddingClient(...))

    # 持久化模式
    store = ToolStore(db_path="tools.db")
    await store.initialize()
    vault = ToolVault(embedding_client=EmbeddingClient(...), store=store)

    # 注册工具
    await vault.add_tool(ToolEntry(
        tool_name="send_email",
        definition=tool_def,
        summary="发送电子邮件",
        essential=False,
    ))

    # 从数据库加载
    await vault.load_from_store()

    # 语义搜索
    results = await vault.search("我需要一个能发通知的工具", top_k=3)
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from youmi.core.tool import ToolDefinition, ToolHandler

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 三级状态枚举
# ---------------------------------------------------------------------------

class ToolContextTier(str, Enum):
    """工具上下文层级

    状态流转:
        COLD → HOT  (通过语义搜索发现后加载)
        HOT  → WARM (LRU 回收: 连续 N 轮未使用)
        WARM → HOT  (再次需要时直接加载, 跳过发现)
    """

    HOT = "hot"      # 完整 schema 在 LLM 上下文中
    WARM = "warm"    # 仅摘要可见, 可快速重载
    COLD = "cold"    # 仅在 Vault 中, 需搜索发现


# ---------------------------------------------------------------------------
# 工具条目
# ---------------------------------------------------------------------------

class ToolEntry(BaseModel):
    """ToolVault 中的工具条目

    包含工具完整定义、语义向量、上下文状态和使用追踪。

    Args:
        tool_name: 工具名称 (唯一标识)
        definition: 完整工具定义 (ToolDefinition)
        handler: 执行函数引用 (Any 类型, 因 BaseModel 不接受 Callable)
        provider_id: 来源 Provider 标识
        essential: 是否必备 (永不回收)
        embedding: 语义向量
        summary: 一句话摘要 (温态显示)
        tier: 当前上下文状态
        last_used_turn: 上次使用的对话轮次 (-1 = 从未使用)
        use_count: 总使用次数
    """

    tool_name: str = Field(description="工具名称 (唯一标识)")
    definition: ToolDefinition = Field(description="完整工具定义")
    handler: Any = Field(default=None, description="执行函数引用")
    provider_id: str = Field(default="", description="来源 Provider")
    essential: bool = Field(default=False, description="是否必备 (永不回收)")
    embedding: list[float] = Field(default_factory=list, description="语义向量")
    summary: str = Field(default="", description="一句话摘要 (温态显示)")
    tier: ToolContextTier = Field(default=ToolContextTier.COLD, description="当前上下文状态")
    last_used_turn: int = Field(default=-1, description="上次使用轮次")
    use_count: int = Field(default=0, description="总使用次数")
    version: str = Field(default="0.0.1", description="工具版本号")
    language: str = Field(default="python", description="工具实现语言")

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# 搜索结果
# ---------------------------------------------------------------------------

class ToolSearchResult(BaseModel):
    """工具语义搜索结果

    Args:
        tool_name: 工具名称
        definition: 工具定义 (可选, 取决于调用方)
        score: 相似度分数 (0~1)
        summary: 工具摘要
    """

    tool_name: str
    definition: ToolDefinition | None = None
    score: float = Field(ge=0.0, le=1.0, description="相似度分数")
    summary: str = ""


# ---------------------------------------------------------------------------
# ToolVault 核心
# ---------------------------------------------------------------------------

class ToolVault:
    """工具数据库 — 向量存储 + 语义搜索 + 三级上下文管理

    管理所有已注册工具的完整定义、语义向量和上下文状态。
    与 MCPServer / ToolBridge 配合使用:
    - MCPServer.register_provider() → Vault.add_tools_from_provider()
    - ToolBridge.discover_tools() → Vault.search()
    - ToolBridge.call_tool() → Vault.record_usage()
    - Agent._act() 完成后 → Vault.recycle()

    Args:
        embedding_client: Embedding 客户端实例 (None = 不启用向量搜索)
    """

    def __init__(
        self,
        embedding_client: Any = None,
        store: Any = None,
    ) -> None:
        from youmi.llm.embeddings import EmbeddingClient  # 延迟导入避免循环
        self._entries: dict[str, ToolEntry] = {}
        self._embedding_client: EmbeddingClient | None = embedding_client
        self._store: Any = store  # ToolStore 实例 (可选)
        self._current_turn: int = 0

    @property
    def store(self) -> Any:
        """ToolStore 持久化存储层 (可选)"""
        return self._store

    # ==================================================================
    # 注册
    # ==================================================================

    async def add_tool(self, entry: ToolEntry) -> None:
        """添加工具到 Vault

        如果 embedding_client 可用且条目无向量，自动生成 embedding。
        如果启用了 ToolStore，同步写入持久化层。

        Args:
            entry: 工具条目
        """
        if not entry.summary:
            # 自动从定义生成摘要
            entry = entry.model_copy(update={
                "summary": entry.definition.description[:80],
            })

        # 同步版本号到 ToolDefinition
        if entry.version and entry.version != entry.definition.version:
            entry = entry.model_copy(update={
                "definition": entry.definition.model_copy(update={"version": entry.version}),
            })

        self._entries[entry.tool_name] = entry

        # 自动生成向量 (如有 embedding client 且无现有向量)
        if self._embedding_client and not entry.embedding:
            await self._generate_embedding(entry.tool_name)

        # 同步写入持久化层
        if self._store is not None:
            try:
                await self._store.upsert_tool(entry)
            except Exception as exc:
                logger.warning("Vault: failed to persist '%s' to store: %s",
                               entry.tool_name, exc)

        logger.debug("Vault: added tool '%s' (tier=%s, essential=%s, version=%s)",
                      entry.tool_name, entry.tier.value, entry.essential, entry.version)

    async def add_tools_from_provider(
        self,
        provider: Any,
        essential_names: set[str] | None = None,
    ) -> None:
        """从 Provider 批量导入工具

        Args:
            provider: ToolProvider 实例 (需有 _definitions 和 _handlers)
            essential_names: 必备工具名称集合 (None = 全部必备)
        """
        essential = essential_names or set()
        definitions = getattr(provider, "_definitions", {})
        handlers = getattr(provider, "_handlers", {})
        provider_id = getattr(provider, "provider_id", "")

        for name, defn in definitions.items():
            entry = ToolEntry(
                tool_name=name,
                definition=defn,
                handler=handlers.get(name),
                provider_id=provider_id,
                essential=(name in essential) if essential_names is not None else True,
                summary=defn.description[:80],
                tier=ToolContextTier.HOT if (name in essential or essential_names is None) else ToolContextTier.COLD,
            )
            self._entries[name] = entry

        # 批量生成向量
        if self._embedding_client:
            await self.build_embeddings()

        logger.info("Vault: imported %d tools from provider '%s'",
                     len(definitions), provider_id)

    # ==================================================================
    # 搜索
    # ==================================================================

    async def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.3,
        exclude: set[str] | None = None,
    ) -> list[ToolSearchResult]:
        """语义搜索工具

        如果启用了 ToolStore，委托给 ToolStore.search()（支持持久化向量搜索和 exclude）。
        否则使用内存搜索。

        流程:
        1. 将 query 通过 embedding_client 生成向量
        2. 与所有非 HOT 条目的向量计算余弦相似度
        3. 过滤 min_score 以下和 exclude 中的结果
        4. 按分数降序返回 Top-K

        Args:
            query: 自然语言查询 (如 "我需要一个能发送邮件的工具")
            top_k: 返回结果数量
            min_score: 最低相似度阈值
            exclude: 排除的工具名称集合 (用于召回确认闭环)

        Returns:
            ToolSearchResult 列表 (按分数降序)
        """
        # 委托给 ToolStore (如有)
        if self._store is not None:
            try:
                return await self._store.search(
                    query, top_k=top_k, min_score=min_score, exclude=exclude,
                )
            except Exception as exc:
                logger.warning("Vault: store search failed, falling back to memory: %s", exc)

        # 内存搜索
        if not self._embedding_client:
            return self._keyword_search(query, top_k, exclude)

        # 生成查询向量
        try:
            query_vec = await self._embedding_client.embed_one(query)
        except Exception as exc:
            logger.warning("Vault: embedding failed, falling back to keyword search: %s", exc)
            return self._keyword_search(query, top_k, exclude)

        # 收集候选 (COLD 和 WARM 状态的工具)
        candidates: list[tuple[str, list[float]]] = []
        for name, entry in self._entries.items():
            if exclude and name in exclude:
                continue
            if entry.embedding and entry.tier != ToolContextTier.HOT:
                candidates.append((name, entry.embedding))

        if not candidates:
            return []

        # 计算相似度
        names = [c[0] for c in candidates]
        vectors = [c[1] for c in candidates]
        scores = await self._embedding_client.similarity(query_vec, vectors)

        # 排序 + 过滤
        results: list[ToolSearchResult] = []
        for name, score in zip(names, scores):
            if score >= min_score:
                entry = self._entries[name]
                results.append(ToolSearchResult(
                    tool_name=name,
                    definition=entry.definition,
                    score=score,
                    summary=entry.summary,
                ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    def _keyword_search(
        self, query: str, top_k: int, exclude: set[str] | None = None,
    ) -> list[ToolSearchResult]:
        """关键词匹配 fallback (无 embedding 时使用)

        简单的词频匹配: 查询词与工具名称+描述的词交集。
        支持中英文混合: 同时使用空格分词和子串匹配。
        """
        query_lower = query.lower()
        query_tokens = set(query_lower.split())
        # 添加单字符 token (支持中文子串匹配)
        query_tokens.update(c for c in query_lower if not c.isspace())

        scored: list[ToolSearchResult] = []
        for name, entry in self._entries.items():
            if exclude and name in exclude:
                continue
            if entry.tier == ToolContextTier.HOT:
                continue

            text = f"{name} {entry.definition.description} {entry.summary}".lower()
            text_tokens = set(text.split())
            text_tokens.update(c for c in text if not c.isspace())
            overlap = query_tokens & text_tokens
            score = len(overlap) / max(len(query_tokens), 1)

            if score > 0:
                scored.append(ToolSearchResult(
                    tool_name=name,
                    definition=entry.definition,
                    score=min(score, 1.0),
                    summary=entry.summary,
                ))

        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]

    # ==================================================================
    # 上下文管理
    # ==================================================================

    async def load_tool(self, tool_name: str) -> ToolEntry | None:
        """将工具从 COLD/WARM 提升到 HOT

        Args:
            tool_name: 工具名称

        Returns:
            更新后的 ToolEntry，如果工具不存在返回 None
        """
        entry = self._entries.get(tool_name)
        if entry is None:
            return None

        if entry.tier == ToolContextTier.HOT:
            return entry

        entry = entry.model_copy(update={"tier": ToolContextTier.HOT})
        self._entries[tool_name] = entry
        logger.debug("Vault: loaded tool '%s' → HOT", tool_name)
        return entry

    def unload_tool(self, tool_name: str) -> bool:
        """将工具从 HOT 降级到 WARM (不记录使用历史)

        Args:
            tool_name: 工具名称

        Returns:
            是否成功
        """
        entry = self._entries.get(tool_name)
        if entry is None or entry.tier != ToolContextTier.HOT:
            return False
        if entry.essential:
            return False  # 必备工具不可降级

        entry = entry.model_copy(update={"tier": ToolContextTier.WARM})
        self._entries[tool_name] = entry
        logger.debug("Vault: unloaded tool '%s' → WARM", tool_name)
        return True

    def get_hot_tools(self) -> list[ToolEntry]:
        """获取所有热态工具"""
        return [e for e in self._entries.values() if e.tier == ToolContextTier.HOT]

    def get_warm_tools(self) -> list[ToolEntry]:
        """获取所有温态工具"""
        return [e for e in self._entries.values() if e.tier == ToolContextTier.WARM]

    def get_cold_tools(self) -> list[ToolEntry]:
        """获取所有冷态工具"""
        return [e for e in self._entries.values() if e.tier == ToolContextTier.COLD]

    def get_entry(self, tool_name: str) -> ToolEntry | None:
        """获取工具条目"""
        return self._entries.get(tool_name)

    # ==================================================================
    # 使用追踪
    # ==================================================================

    def record_usage(self, tool_name: str, turn: int | None = None) -> None:
        """记录工具使用

        Args:
            tool_name: 工具名称
            turn: 使用时的对话轮次 (None = 当前轮次)
        """
        entry = self._entries.get(tool_name)
        if entry is None:
            return

        actual_turn = turn if turn is not None else self._current_turn
        entry = entry.model_copy(update={
            "last_used_turn": actual_turn,
            "use_count": entry.use_count + 1,
        })
        self._entries[tool_name] = entry

    # ==================================================================
    # LRU 回收
    # ==================================================================

    def recycle(self, idle_threshold: int = 3) -> list[str]:
        """LRU 回收: 将闲置的非必备热态工具降级为温态

        规则:
        - 必备工具 (essential=True) 永不回收
        - 从未使用过的非必备热态工具，如果当前轮次 > idle_threshold，降级
        - 上次使用距今超过 idle_threshold 轮的热态工具，降级

        Args:
            idle_threshold: 闲置轮次阈值

        Returns:
            被回收 (降级) 的工具名列表
        """
        recycled: list[str] = []

        for name, entry in self._entries.items():
            if entry.tier != ToolContextTier.HOT:
                continue
            if entry.essential:
                continue

            # 判断是否闲置超过阈值
            if entry.last_used_turn < 0:
                # 从未使用过: 如果已经存在超过 threshold 轮则回收
                if self._current_turn >= idle_threshold:
                    entry = entry.model_copy(update={"tier": ToolContextTier.WARM})
                    self._entries[name] = entry
                    recycled.append(name)
            else:
                idle_turns = self._current_turn - entry.last_used_turn
                if idle_turns >= idle_threshold:
                    entry = entry.model_copy(update={"tier": ToolContextTier.WARM})
                    self._entries[name] = entry
                    recycled.append(name)

        if recycled:
            logger.info("Vault: recycled %d tools → WARM: %s",
                         len(recycled), recycled)

        return recycled

    # ==================================================================
    # 向量管理
    # ==================================================================

    async def build_embeddings(self) -> int:
        """为所有无向量的条目批量生成 embedding

        Returns:
            成功生成的条目数量
        """
        if not self._embedding_client:
            return 0

        # 收集无向量的条目
        to_embed: list[tuple[str, str]] = []
        for name, entry in self._entries.items():
            if not entry.embedding:
                # 用 "名称 + 描述" 作为嵌入文本
                text = f"{name}: {entry.definition.description}"
                to_embed.append((name, text))

        if not to_embed:
            return 0

        texts = [t[1] for t in to_embed]
        try:
            vectors = await self._embedding_client.embed(texts)
            count = 0
            for (name, _), vec in zip(to_embed, vectors):
                entry = self._entries[name]
                entry = entry.model_copy(update={"embedding": vec})
                self._entries[name] = entry
                count += 1

            logger.info("Vault: generated embeddings for %d tools", count)
            return count

        except Exception as exc:
            logger.warning("Vault: batch embedding failed: %s", exc)
            return 0

    async def _generate_embedding(self, tool_name: str) -> None:
        """为单个工具生成向量"""
        if not self._embedding_client:
            return

        entry = self._entries.get(tool_name)
        if entry is None or entry.embedding:
            return

        text = f"{tool_name}: {entry.definition.description}"
        try:
            vec = await self._embedding_client.embed_one(text)
            entry = entry.model_copy(update={"embedding": vec})
            self._entries[tool_name] = entry
        except Exception as exc:
            logger.warning("Vault: embedding failed for '%s': %s", tool_name, exc)

    # ==================================================================
    # 轮次管理
    # ==================================================================

    def advance_turn(self) -> int:
        """推进对话轮次计数器

        Returns:
            新的当前轮次
        """
        self._current_turn += 1
        return self._current_turn

    @property
    def current_turn(self) -> int:
        return self._current_turn

    # ==================================================================
    # OpenAI schema 生成
    # ==================================================================

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """生成所有热态工具的 OpenAI tools schema"""
        schemas: list[dict[str, Any]] = []
        for entry in self._entries.values():
            if entry.tier == ToolContextTier.HOT:
                schemas.append(entry.definition.to_openai_function_schema())
        return schemas

    def to_warm_summaries(self) -> list[dict[str, str]]:
        """生成所有温态工具的摘要列表

        格式: [{"name": "tool_name", "description": "一句话摘要"}]
        Agent 可将此列表注入 system prompt，让 LLM 知道还有哪些工具可用。
        """
        summaries: list[dict[str, str]] = []
        for entry in self._entries.values():
            if entry.tier == ToolContextTier.WARM:
                summaries.append({
                    "name": entry.tool_name,
                    "description": entry.summary,
                })
        return summaries

    # ==================================================================
    # 诊断
    # ==================================================================

    @property
    def tool_count(self) -> int:
        return len(self._entries)

    @property
    def hot_count(self) -> int:
        return sum(1 for e in self._entries.values() if e.tier == ToolContextTier.HOT)

    @property
    def warm_count(self) -> int:
        return sum(1 for e in self._entries.values() if e.tier == ToolContextTier.WARM)

    @property
    def cold_count(self) -> int:
        return sum(1 for e in self._entries.values() if e.tier == ToolContextTier.COLD)

    @property
    def tool_names(self) -> list[str]:
        return list(self._entries.keys())

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, name: str) -> bool:
        return name in self._entries

    # ==================================================================
    # 持久化层集成
    # ==================================================================

    async def load_from_store(self) -> int:
        """从 ToolStore 加载所有工具到内存缓存

        将 ToolStore 中的最新版本工具加载到 _entries 字典中。
        已存在于内存中的工具不会被覆盖。

        Returns:
            加载的工具数量
        """
        if self._store is None:
            return 0

        try:
            entries = await self._store.list_tools()
        except Exception as exc:
            logger.warning("Vault: failed to load from store: %s", exc)
            return 0

        loaded = 0
        for entry in entries:
            if entry.tool_name not in self._entries:
                self._entries[entry.tool_name] = entry
                loaded += 1

        if loaded:
            logger.info("Vault: loaded %d tools from store", loaded)

        return loaded

    async def sync_to_store(self) -> int:
        """将内存中的所有工具同步写入 ToolStore

        Returns:
            同步的工具数量
        """
        if self._store is None:
            return 0

        count = 0
        for entry in self._entries.values():
            try:
                await self._store.upsert_tool(entry)
                count += 1
            except Exception as exc:
                logger.warning("Vault: failed to sync '%s' to store: %s",
                               entry.tool_name, exc)

        return count

    def __repr__(self) -> str:
        store_info = " store=True" if self._store else ""
        return (
            f"<ToolVault total={self.tool_count} "
            f"hot={self.hot_count} warm={self.warm_count} cold={self.cold_count}{store_info}>"
        )
