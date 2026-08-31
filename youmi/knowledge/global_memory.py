"""
GlobalMemory — 全局记忆核心

跨任务的工具使用经验知识库，基于 SQLite 持久化 + 向量语义检索。
经验专供工具管理 Agent（如 ToolGuardian）诊断和修复工具问题，
修复完成后通过 mark_resolved() 标记解决并记录修复方案。

数据表:
- knowledge_entries: 知识条目主表
- knowledge_vectors: 向量索引 (embedding 序列化为 JSON)

用法::

    from youmi.knowledge import GlobalMemory, KnowledgeCategory
    from youmi.llm.embeddings import EmbeddingClient

    embedder = EmbeddingClient(base_url="http://localhost:11434/v1",
                               model="nomic-embed-text")
    memory = GlobalMemory(db_path="global_memory.db", embedding_client=embedder)
    await memory.initialize()

    # 记录经验
    await memory.add_experience(
        tool_name="file_read",
        content="路径参数必须使用绝对路径，相对路径会因 cwd 不同而失败",
        category=KnowledgeCategory.TOOL_EXPERIENCE,
        source_task_id="task_001",
    )

    # 语义检索
    results = await memory.search("file_read 工具路径问题")

    # 聚合查询
    knowledge = await memory.get_tool_knowledge("file_read")

    # 修复完成
    await memory.mark_resolved(entry.entry_id, "v0.0.2 修复了路径解析")
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING

from youmi.knowledge.models import (
    KnowledgeCategory,
    KnowledgeEntry,
    ToolKnowledge,
)

if TYPE_CHECKING:
    from youmi.llm.embeddings import EmbeddingClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 建表 SQL
# ---------------------------------------------------------------------------

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS knowledge_entries (
    entry_id TEXT PRIMARY KEY,
    category TEXT NOT NULL DEFAULT 'tool_experience',
    tool_name TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    source_task_id TEXT DEFAULT '',
    source_agent_id TEXT DEFAULT '',
    success_rate REAL DEFAULT 0.0,
    resolved INTEGER DEFAULT 0,
    resolution TEXT DEFAULT '',
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_tool ON knowledge_entries(tool_name);
CREATE INDEX IF NOT EXISTS idx_knowledge_category ON knowledge_entries(category);
CREATE INDEX IF NOT EXISTS idx_knowledge_updated ON knowledge_entries(updated_at DESC);

CREATE TABLE IF NOT EXISTS knowledge_vectors (
    entry_id TEXT PRIMARY KEY REFERENCES knowledge_entries(entry_id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL DEFAULT '',
    embedding_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kvec_tool ON knowledge_vectors(tool_name);
"""


# ---------------------------------------------------------------------------
# 辅助: 余弦相似度 (纯 Python, 与 ToolStore 保持一致)
# ---------------------------------------------------------------------------

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度"""
    if not a or not b or len(a) != len(b):
        return 0.0

    import math
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))

    if norm_a == 0 or norm_b == 0:
        return 0.0

    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# GlobalMemory 核心
# ---------------------------------------------------------------------------

class GlobalMemory:
    """全局记忆 — 跨任务的工具经验知识库

    职责:
    - 持久化 KnowledgeEntry (SQLite)
    - 向量语义检索 (接入 EmbeddingClient; 未接入时降级为关键词匹配)
    - 聚合单个工具的经验 (ToolKnowledge)
    - 修复闭环 (mark_resolved / 记录 fix_history)

    Args:
        db_path: SQLite 数据库文件路径。
            ":memory:" 使用内存数据库 (测试用)。
            默认 ".youmi_knowledge.db" (当前工作目录)。
        embedding_client: EmbeddingClient 实例 (None = 关键词检索降级)
    """

    def __init__(
        self,
        db_path: str = ".youmi_knowledge.db",
        embedding_client: EmbeddingClient | None = None,
    ) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._embedding_client = embedding_client

    # ==================================================================
    # 生命周期
    # ==================================================================

    async def initialize(self) -> None:
        """建库建表 (幂等: 已初始化时跳过)"""
        if self._conn is not None:
            return

        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = await asyncio.to_thread(
            sqlite3.connect, self._db_path, check_same_thread=False,
        )
        await asyncio.to_thread(self._conn.execute, "PRAGMA foreign_keys = ON;")
        await asyncio.to_thread(self._conn.executescript, _CREATE_TABLES_SQL)
        await asyncio.to_thread(self._conn.commit)
        logger.info("GlobalMemory initialized: %s", self._db_path)

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("GlobalMemory not initialized. Call initialize() first.")
        return self._conn

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None
            logger.debug("GlobalMemory closed: %s", self._db_path)

    # ==================================================================
    # 写入
    # ==================================================================

    async def add_experience(
        self,
        tool_name: str,
        content: str,
        category: KnowledgeCategory = KnowledgeCategory.TOOL_EXPERIENCE,
        source_task_id: str = "",
        source_agent_id: str = "",
        success_rate: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> KnowledgeEntry:
        """记录一条经验并自动向量化

        Args:
            tool_name: 关联工具名
            content: 经验描述文本
            category: 知识类别
            source_task_id: 来源任务 ID
            source_agent_id: 来源 Agent ID
            success_rate: 关联的工具调用成功率
            metadata: 扩展字段

        Returns:
            写入后的 KnowledgeEntry (含向量)
        """
        entry = KnowledgeEntry(
            category=category,
            tool_name=tool_name,
            content=content,
            source_task_id=source_task_id,
            source_agent_id=source_agent_id,
            success_rate=success_rate,
            metadata=metadata or {},
        )

        # 向量化 (失败不阻塞写入, 降级为关键词检索)
        if self._embedding_client is not None:
            try:
                entry.embedding = await self._embedding_client.embed_one(content)
            except Exception as exc:
                logger.warning(
                    "GlobalMemory: embedding failed for entry '%s' "
                    "(fallback to keyword search): %s",
                    entry.entry_id, exc,
                )

        await self._insert_entry(entry)
        return entry

    async def batch_add(self, entries: list[KnowledgeEntry]) -> list[str]:
        """批量写入条目 (已构造好的 KnowledgeEntry 列表)

        对未向量化且可向量的条目批量生成向量。

        Args:
            entries: KnowledgeEntry 列表

        Returns:
            写入的 entry_id 列表
        """
        if not entries:
            return []

        # 批量向量化
        pending = [
            e for e in entries
            if e.embedding is None and self._embedding_client is not None
        ]
        if pending:
            try:
                vectors = await self._embedding_client.embed(
                    [e.content for e in pending],
                )
                for e, vec in zip(pending, vectors):
                    e.embedding = vec
            except Exception as exc:
                logger.warning(
                    "GlobalMemory: batch embedding failed (%d entries): %s",
                    len(pending), exc,
                )

        for entry in entries:
            await self._insert_entry(entry)
        return [e.entry_id for e in entries]

    async def _insert_entry(self, entry: KnowledgeEntry) -> None:
        """写入单条条目到 SQLite"""
        conn = self._ensure_conn()

        def _write():
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """INSERT OR REPLACE INTO knowledge_entries
                       (entry_id, category, tool_name, content, source_task_id,
                        source_agent_id, success_rate, resolved, resolution,
                        metadata, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry.entry_id,
                        entry.category.value,
                        entry.tool_name,
                        entry.content,
                        entry.source_task_id,
                        entry.source_agent_id,
                        entry.success_rate,
                        int(entry.resolved),
                        entry.resolution,
                        json.dumps(entry.metadata, ensure_ascii=False),
                        entry.created_at.isoformat(),
                        entry.updated_at.isoformat(),
                    ),
                )
                if entry.embedding is not None:
                    cursor.execute(
                        """INSERT OR REPLACE INTO knowledge_vectors
                           (entry_id, tool_name, embedding_json, updated_at)
                           VALUES (?, ?, ?, ?)""",
                        (
                            entry.entry_id,
                            entry.tool_name,
                            json.dumps(entry.embedding),
                            entry.updated_at.isoformat(),
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        await asyncio.to_thread(_write)

    # ==================================================================
    # 修复闭环
    # ==================================================================

    async def mark_resolved(
        self,
        entry_id: str,
        fix_description: str,
    ) -> KnowledgeEntry | None:
        """标记一条 bug 经验为已解决，并记录修复方案

        Args:
            entry_id: 条目 ID
            fix_description: 修复说明 (将记入 resolution 和 fix_history)

        Returns:
            更新后的 KnowledgeEntry; 条目不存在返回 None
        """
        conn = self._ensure_conn()
        now = datetime.utcnow().isoformat()

        def _mark():
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """UPDATE knowledge_entries
                       SET resolved = 1, resolution = ?, updated_at = ?
                       WHERE entry_id = ?""",
                    (fix_description, now, entry_id),
                )
                if cursor.rowcount == 0:
                    return None
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

        updated = await asyncio.to_thread(_mark)
        if not updated:
            logger.warning("GlobalMemory: entry '%s' not found for mark_resolved", entry_id)
            return None

        entry = await self.get_entry(entry_id)
        if entry is not None:
            logger.info(
                "GlobalMemory: entry '%s' (tool=%s) marked resolved: %s",
                entry_id, entry.tool_name, fix_description[:100],
            )
        return entry

    # ==================================================================
    # 查询
    # ==================================================================

    async def get_entry(self, entry_id: str) -> KnowledgeEntry | None:
        """按 ID 获取单条条目"""
        conn = self._ensure_conn()
        cursor = await asyncio.to_thread(
            conn.execute,
            """SELECT entry_id, category, tool_name, content, source_task_id,
                      source_agent_id, success_rate, resolved, resolution,
                      metadata, created_at, updated_at
               FROM knowledge_entries WHERE entry_id = ?""",
            (entry_id,),
        )
        row = await asyncio.to_thread(cursor.fetchone)
        if row is None:
            return None
        return self._row_to_entry(row)

    async def list_entries(
        self,
        tool_name: str | None = None,
        category: KnowledgeCategory | None = None,
        unresolved_only: bool = False,
        limit: int = 100,
    ) -> list[KnowledgeEntry]:
        """列出条目 (按更新时间倒序)

        Args:
            tool_name: 按工具名过滤 (None = 不过滤)
            category: 按类别过滤 (None = 不过滤)
            unresolved_only: 仅返回未解决的 bug 条目
            limit: 最多返回条数
        """
        conn = self._ensure_conn()

        conditions: list[str] = []
        params: list[Any] = []
        if tool_name is not None:
            conditions.append("tool_name = ?")
            params.append(tool_name)
        if category is not None:
            conditions.append("category = ?")
            params.append(category.value)
        if unresolved_only:
            conditions.append("resolved = 0")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = (
            "SELECT entry_id, category, tool_name, content, source_task_id, "
            "source_agent_id, success_rate, resolved, resolution, "
            "metadata, created_at, updated_at "
            f"FROM knowledge_entries {where} "
            "ORDER BY updated_at DESC LIMIT ?"
        )
        params.append(limit)

        cursor = await asyncio.to_thread(conn.execute, sql, params)
        rows = await asyncio.to_thread(cursor.fetchall)
        return [self._row_to_entry(row) for row in rows]

    async def search(
        self,
        query: str,
        tool_name: str | None = None,
        top_k: int = 5,
    ) -> list[KnowledgeEntry]:
        """语义检索知识条目

        接入 EmbeddingClient 时使用向量余弦相似度排序；
        未接入时降级为关键词匹配。

        Args:
            query: 查询文本
            tool_name: 限定工具名 (None = 全部)
            top_k: 返回条数

        Returns:
            按 relevance 降序的 KnowledgeEntry 列表 (results 为空时不返回)
        """
        if not query.strip():
            return []

        entries = await self.list_entries(
            tool_name=tool_name, limit=1000,
        )
        if not entries:
            return []

        # 尝试向量检索
        if self._embedding_client is not None:
            try:
                query_vec = await self._embedding_client.embed_one(query)
                scored = [
                    (entry, _cosine_similarity(query_vec, entry.embedding or []))
                    for entry in entries
                ]
                scored.sort(key=lambda x: x[1], reverse=True)
                results = [e for e, score in scored[:top_k] if score > 0.1]
                if results:
                    return results
                # 向量召回为空 → 继续尝试关键词
            except Exception as exc:
                logger.warning(
                    "GlobalMemory: vector search failed (fallback to keyword): %s", exc,
                )

        # 关键词降级检索
        return self._keyword_search(entries, query, top_k)

    @staticmethod
    def _keyword_search(
        entries: list[KnowledgeEntry],
        query: str,
        top_k: int,
    ) -> list[KnowledgeEntry]:
        """关键词匹配降级检索 (与 InMemoryLongTermBackend 逻辑一致)"""
        query_lower = query.lower()
        scored = [
            (
                entry,
                sum(1 for word in query_lower.split() if word in entry.content.lower()),
            )
            for entry in entries
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [e for e, score in scored[:top_k] if score > 0]

    async def get_tool_knowledge(self, tool_name: str) -> ToolKnowledge:
        """聚合单个工具的全部经验

        将该工具的所有 KnowledgeEntry 聚合为 ToolKnowledge:
        - 成功模式 (success_rate 高或 content 描述正确用法) → best_practices
        - 未解决的失败经验 → known_issues
        - 已解决的失败经验 → resolved_issues
        - 修复记录 (BUG_FIX 类) → fix_history

        Args:
            tool_name: 工具名称

        Returns:
            ToolKnowledge (无记录时返回空知识对象)
        """
        entries = await self.list_entries(tool_name=tool_name, limit=500)

        knowledge = ToolKnowledge(tool_name=tool_name)
        for entry in entries:
            knowledge.entry_ids.append(entry.entry_id)

            if entry.category == KnowledgeCategory.BUG_FIX:
                knowledge.fix_history.append(entry.content)
                continue

            if entry.category == KnowledgeCategory.TASK_PATTERN:
                continue  # 任务模式不属于工具知识

            # TOOL_EXPERIENCE
            if entry.resolved:
                if entry.resolution:
                    knowledge.resolved_issues.append(
                        f"{entry.content} (已修复: {entry.resolution})",
                    )
                else:
                    knowledge.resolved_issues.append(entry.content)
            elif entry.success_rate >= 0.8:
                knowledge.best_practices.append(entry.content)
            else:
                knowledge.known_issues.append(entry.content)

        return knowledge

    # ==================================================================
    # 删除
    # ==================================================================

    async def delete_entry(self, entry_id: str) -> bool:
        """删除一条条目 (含向量)

        Returns:
            是否实际删除
        """
        conn = self._ensure_conn()

        def _delete():
            cursor = conn.cursor()
            try:
                cursor.execute(
                    "DELETE FROM knowledge_vectors WHERE entry_id = ?", (entry_id,),
                )
                cursor.execute(
                    "DELETE FROM knowledge_entries WHERE entry_id = ?", (entry_id,),
                )
                deleted = cursor.rowcount > 0
                conn.commit()
                return deleted
            except Exception:
                conn.rollback()
                raise

        return await asyncio.to_thread(_delete)

    # ==================================================================
    # 诊断
    # ==================================================================

    async def stats(self) -> dict[str, Any]:
        """知识库统计信息"""
        conn = self._ensure_conn()

        def _stats():
            cursor = conn.execute(
                "SELECT COUNT(*), SUM(resolved) FROM knowledge_entries",
            )
            total, resolved = cursor.fetchone()
            cursor = conn.execute(
                "SELECT COUNT(*) FROM knowledge_vectors",
            )
            (vec_count,) = cursor.fetchone()
            cursor = conn.execute(
                "SELECT tool_name, COUNT(*) FROM knowledge_entries "
                "WHERE tool_name != '' GROUP BY tool_name "
                "ORDER BY COUNT(*) DESC LIMIT 10",
            )
            top_tools = cursor.fetchall()
            return {
                "total_entries": total or 0,
                "resolved_entries": int(resolved or 0),
                "vectorized_entries": vec_count or 0,
                "top_tools": dict(top_tools),
            }

        return await asyncio.to_thread(_stats)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_entry(row: tuple) -> KnowledgeEntry:
        """SQLite 行 → KnowledgeEntry"""
        (
            entry_id, category, tool_name, content, source_task_id,
            source_agent_id, success_rate, resolved, resolution,
            metadata_str, created_at, updated_at,
        ) = row
        return KnowledgeEntry(
            entry_id=entry_id,
            category=KnowledgeCategory(category),
            tool_name=tool_name,
            content=content,
            source_task_id=source_task_id,
            source_agent_id=source_agent_id,
            success_rate=success_rate,
            resolved=bool(resolved),
            resolution=resolution,
            metadata=json.loads(metadata_str) if metadata_str else {},
            created_at=datetime.fromisoformat(created_at),
            updated_at=datetime.fromisoformat(updated_at),
        )

    def __repr__(self) -> str:
        return f"<GlobalMemory db={self._db_path!r} embedded={self._embedding_client is not None}>"


__all__ = ["GlobalMemory"]
