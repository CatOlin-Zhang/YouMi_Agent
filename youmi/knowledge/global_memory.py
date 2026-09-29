"""
GlobalMemory — 全局记忆核心

跨任务的工具经验知识库，基于 SQLite 持久化 + sqlite-vec 向量语义检索。
经验专供工具管理 Agent（如 ToolGuardian）诊断和修复工具问题，
修复完成后通过 mark_resolved() 标记解决并记录修复方案。

多租户: 每个实例绑定一个 tenant（默认 "default"），写入自动归属该 tenant，
查询仅返回同 tenant 的数据；同进程内可用 clone_for_tenant() 获取其他租户视图
（共享数据库连接）。旧库在 initialize() 时自动补 tenant 列，旧数据归 default。

数据表:
- knowledge_entries: 知识条目主表
- vec_knowledge_idx: sqlite-vec vec0 虚拟表 (归一化向量, KNN 查询)
- knowledge_vectors: 降级用 JSON 向量列 (sqlite-vec 不可用时)

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

from youmi._vec_utils import (
    cosine_similarity_python,
    l2_to_cosine,
    normalize_vector,
    try_load_sqlite_vec,
    vec_to_json,
)
from youmi.knowledge.models import (
    KnowledgeCategory,
    KnowledgeEntry,
    ToolKnowledge,
)

if TYPE_CHECKING:
    from youmi.llm.embeddings import EmbeddingClient

logger = logging.getLogger(__name__)

# 向后兼容
_cosine_similarity = cosine_similarity_python

# ---------------------------------------------------------------------------
# 建表 SQL
# ---------------------------------------------------------------------------

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS knowledge_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id TEXT UNIQUE NOT NULL,
    category TEXT NOT NULL DEFAULT 'tool_experience',
    tool_name TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    source_task_id TEXT DEFAULT '',
    source_agent_id TEXT DEFAULT '',
    tenant TEXT NOT NULL DEFAULT 'default',
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

-- 降级用 JSON 向量表
CREATE TABLE IF NOT EXISTS knowledge_vectors (
    entry_id TEXT PRIMARY KEY REFERENCES knowledge_entries(entry_id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL DEFAULT '',
    embedding_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_kvec_tool ON knowledge_vectors(tool_name);
"""


# ---------------------------------------------------------------------------
# GlobalMemory 核心
# ---------------------------------------------------------------------------

class GlobalMemory:
    """全局记忆 — 跨任务的工具经验知识库

    职责:
    - 持久化 KnowledgeEntry (SQLite)
    - 向量语义检索 (sqlite-vec KNN; 未接入时降级为关键词匹配)
    - 聚合单个工具的经验 (ToolKnowledge)
    - 修复闭环 (mark_resolved / 记录 fix_history)

    Args:
        db_path: SQLite 数据库文件路径。
            ":memory:" 使用内存数据库 (测试用)。
            默认 ".youmi_knowledge.db" (当前工作目录)。
        embedding_client: EmbeddingClient 实例 (None = 关键词检索降级)
        embedding_dim: Embedding 向量维度 (默认 768)
        tenant: 租户标识 (多租户隔离, 默认 "default")
    """

    def __init__(
        self,
        db_path: str = ".youmi_knowledge.db",
        embedding_client: EmbeddingClient | None = None,
        embedding_dim: int = 768,
        tenant: str = "default",
    ) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._embedding_client = embedding_client
        self._embedding_dim = embedding_dim
        self._tenant = tenant or "default"
        self._vec_available: bool = False

    @property
    def tenant(self) -> str:
        """当前实例绑定的租户标识"""
        return self._tenant

    def clone_for_tenant(self, tenant: str) -> "GlobalMemory":
        """获取同一数据库上的其他租户视图 (共享连接)

        返回的新实例复用当前连接/向量能力/Embedding 客户端，
        仅改变读写归属的租户，避免重复 initialize。

        Args:
            tenant: 目标租户标识

        Returns:
            绑定目标租户的 GlobalMemory 视图 (未初始化连接时为独立实例)
        """
        clone = GlobalMemory(
            db_path=self._db_path,
            embedding_client=self._embedding_client,
            embedding_dim=self._embedding_dim,
            tenant=tenant,
        )
        clone._conn = self._conn
        clone._vec_available = self._vec_available
        return clone

    # ==================================================================
    # 生命周期
    # ==================================================================

    async def initialize(self) -> None:
        """建库建表 (幂等: 已初始化时跳过)，并尝试加载 sqlite-vec"""
        if self._conn is not None:
            return

        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = await asyncio.to_thread(
            sqlite3.connect, self._db_path, check_same_thread=False,
        )
        await asyncio.to_thread(self._conn.execute, "PRAGMA foreign_keys = ON;")
        await asyncio.to_thread(self._conn.executescript, _CREATE_TABLES_SQL)
        # 旧库迁移: 补 tenant 列 (旧数据归 default)
        await asyncio.to_thread(self._migrate_tenant_column)
        await asyncio.to_thread(self._conn.commit)

        # 尝试加载 sqlite-vec
        self._vec_available = await asyncio.to_thread(
            try_load_sqlite_vec, self._conn,
        )
        if self._vec_available:
            dim = self._embedding_dim
            vec_sql = (
                f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_knowledge_idx "
                f"USING vec0(embedding float[{dim}])"
            )
            await asyncio.to_thread(self._conn.execute, vec_sql)
            await asyncio.to_thread(self._conn.commit)
            logger.info(
                "GlobalMemory initialized with sqlite-vec: %s (dim=%d)",
                self._db_path, dim,
            )
        else:
            logger.info("GlobalMemory initialized (fallback mode): %s", self._db_path)

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("GlobalMemory not initialized. Call initialize() first.")
        return self._conn

    def _migrate_tenant_column(self) -> None:
        """旧库迁移: knowledge_entries 缺 tenant 列时补齐并建索引"""
        conn = self._ensure_conn()
        cols = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_entries)")}
        if "tenant" not in cols:
            conn.execute(
                "ALTER TABLE knowledge_entries "
                "ADD COLUMN tenant TEXT NOT NULL DEFAULT 'default'"
            )
            logger.info("GlobalMemory: migrated knowledge_entries with tenant column")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_knowledge_tenant "
            "ON knowledge_entries(tenant)"
        )

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
            tenant=self._tenant,
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

    async def add_feedback(
        self,
        tool_name: str,
        feedback: str,
        rating: str = "positive",
        source_task_id: str = "",
        source_agent_id: str = "",
    ) -> KnowledgeEntry:
        """记录一条人工反馈 (HUMAN_FEEDBACK)

        人工反馈是工具经验的最高可信度来源:
        - 正反馈沉淀为该工具的最佳实践
        - 负反馈作为未解决问题进入 known_issues，
          累计达到阈值后可触发 ToolGuardian 修复流程
          (见 youmi.knowledge.feedback.FeedbackCollector)

        Args:
            tool_name: 关联工具名
            feedback: 反馈内容 (用户对工具使用的评价/纠正)
            rating: "positive" | "negative"
            source_task_id: 来源任务 ID
            source_agent_id: 来源 Agent ID

        Returns:
            写入后的 KnowledgeEntry
        """
        return await self.add_experience(
            tool_name=tool_name,
            content=feedback,
            category=KnowledgeCategory.HUMAN_FEEDBACK,
            source_task_id=source_task_id,
            source_agent_id=source_agent_id,
            metadata={"rating": rating},
        )

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

        # 未指定租户的条目自动归属当前实例租户
        for e in entries:
            if e.tenant == "default":
                e.tenant = self._tenant

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
                # ON CONFLICT DO UPDATE 保持 id 稳定
                cursor.execute(
                    """INSERT INTO knowledge_entries
                       (entry_id, category, tool_name, content, source_task_id,
                        source_agent_id, tenant, success_rate, resolved, resolution,
                        metadata, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(entry_id) DO UPDATE SET
                           content = excluded.content,
                           resolved = excluded.resolved,
                           resolution = excluded.resolution,
                           success_rate = excluded.success_rate,
                           metadata = excluded.metadata,
                           updated_at = excluded.updated_at""",
                    (
                        entry.entry_id,
                        entry.category.value,
                        entry.tool_name,
                        entry.content,
                        entry.source_task_id,
                        entry.source_agent_id,
                        entry.tenant,
                        entry.success_rate,
                        int(entry.resolved),
                        entry.resolution,
                        json.dumps(entry.metadata, ensure_ascii=False),
                        entry.created_at.isoformat(),
                        entry.updated_at.isoformat(),
                    ),
                )

                # 获取稳定的 id
                row = cursor.execute(
                    "SELECT id FROM knowledge_entries WHERE entry_id = ?",
                    (entry.entry_id,),
                ).fetchone()
                row_id = row[0]

                # 写入向量
                if entry.embedding is not None:
                    self._write_vec(cursor, row_id, entry)

                conn.commit()
            except Exception:
                conn.rollback()
                raise

        await asyncio.to_thread(_write)

    def _write_vec(
        self,
        cursor: sqlite3.Cursor,
        row_id: int,
        entry: KnowledgeEntry,
    ) -> None:
        """写入向量到 vec0 (优先) 或 knowledge_vectors (降级)"""
        now = entry.updated_at.isoformat()
        if self._vec_available:
            norm = normalize_vector(entry.embedding)
            cursor.execute(
                "DELETE FROM vec_knowledge_idx WHERE rowid = ?", (row_id,),
            )
            cursor.execute(
                "INSERT INTO vec_knowledge_idx(rowid, embedding) VALUES (?, ?)",
                (row_id, vec_to_json(norm)),
            )
        else:
            cursor.execute(
                """INSERT INTO knowledge_vectors
                   (entry_id, tool_name, embedding_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(entry_id) DO UPDATE SET
                       embedding_json = excluded.embedding_json,
                       updated_at = excluded.updated_at""",
                (
                    entry.entry_id,
                    entry.tool_name,
                    json.dumps(entry.embedding),
                    now,
                ),
            )

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
                       WHERE entry_id = ? AND tenant = ?""",
                    (fix_description, now, entry_id, self._tenant),
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
        """按 ID 获取单条条目 (限当前租户)"""
        conn = self._ensure_conn()
        cursor = await asyncio.to_thread(
            conn.execute,
            """SELECT entry_id, category, tool_name, content, source_task_id,
                      source_agent_id, tenant, success_rate, resolved, resolution,
                      metadata, created_at, updated_at
               FROM knowledge_entries WHERE entry_id = ? AND tenant = ?""",
            (entry_id, self._tenant),
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
        """列出条目 (按更新时间倒序, 限当前租户)

        Args:
            tool_name: 按工具名过滤 (None = 不过滤)
            category: 按类别过滤 (None = 不过滤)
            unresolved_only: 仅返回未解决的 bug 条目
            limit: 最多返回条数
        """
        conn = self._ensure_conn()

        conditions: list[str] = ["tenant = ?"]
        params: list[Any] = [self._tenant]
        if tool_name is not None:
            conditions.append("tool_name = ?")
            params.append(tool_name)
        if category is not None:
            conditions.append("category = ?")
            params.append(category.value)
        if unresolved_only:
            conditions.append("resolved = 0")

        where = "WHERE " + " AND ".join(conditions)
        sql = (
            "SELECT entry_id, category, tool_name, content, source_task_id, "
            "source_agent_id, tenant, success_rate, resolved, resolution, "
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

        接入 EmbeddingClient 时使用 sqlite-vec KNN 或 Python 余弦相似度排序；
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

        # 尝试向量检索
        if self._embedding_client is not None:
            try:
                query_vec = await self._embedding_client.embed_one(query)

                if self._vec_available:
                    results = await self._vec0_search(query_vec, tool_name, top_k)
                    if results:
                        return results
                else:
                    results = await self._json_search(query_vec, tool_name, top_k)
                    if results:
                        return results
                # 向量召回为空 → 继续尝试关键词
            except Exception as exc:
                logger.warning(
                    "GlobalMemory: vector search failed (fallback to keyword): %s", exc,
                )

        # 关键词降级检索
        entries = await self.list_entries(
            tool_name=tool_name, limit=1000,
        )
        if not entries:
            return []
        return self._keyword_search(entries, query, top_k)

    async def _vec0_search(
        self,
        query_vec: list[float],
        tool_name: str | None,
        top_k: int,
    ) -> list[KnowledgeEntry]:
        """sqlite-vec vec0 KNN 搜索"""
        conn = self._ensure_conn()
        norm_query = normalize_vector(query_vec)
        fetch_k = top_k * 3

        # 构建 SQL: 可选 tool_name 过滤通过 JOIN 实现; tenant 必须过滤
        if tool_name is not None:
            sql = """SELECT ke.entry_id, ke.category, ke.tool_name, ke.content,
                            ke.source_task_id, ke.source_agent_id, ke.tenant,
                            ke.success_rate, ke.resolved, ke.resolution, ke.metadata,
                            ke.created_at, ke.updated_at, v.distance
                     FROM vec_knowledge_idx v
                     JOIN knowledge_entries ke ON ke.id = v.rowid
                     WHERE v.embedding MATCH ? AND k = ?
                       AND ke.tool_name = ?
                       AND ke.tenant = ?
                     ORDER BY v.distance"""
            params: list[Any] = [
                vec_to_json(norm_query), fetch_k, tool_name, self._tenant,
            ]
        else:
            sql = """SELECT ke.entry_id, ke.category, ke.tool_name, ke.content,
                            ke.source_task_id, ke.source_agent_id, ke.tenant,
                            ke.success_rate, ke.resolved, ke.resolution, ke.metadata,
                            ke.created_at, ke.updated_at, v.distance
                     FROM vec_knowledge_idx v
                     JOIN knowledge_entries ke ON ke.id = v.rowid
                     WHERE v.embedding MATCH ? AND k = ?
                       AND ke.tenant = ?
                     ORDER BY v.distance"""
            params = [vec_to_json(norm_query), fetch_k, self._tenant]

        def _search():
            return conn.execute(sql, params).fetchall()

        rows = await asyncio.to_thread(_search)
        results: list[KnowledgeEntry] = []
        for row in rows:
            distance = row[-1]
            score = l2_to_cosine(distance)
            if score > 0.1:
                entry = self._row_to_entry(row[:-1])  # 去掉 distance 列
                results.append(entry)

        return results[:top_k]

    async def _json_search(
        self,
        query_vec: list[float],
        tool_name: str | None,
        top_k: int,
    ) -> list[KnowledgeEntry]:
        """降级: JSON 向量表 + Python 余弦相似度"""
        entries = await self.list_entries(tool_name=tool_name, limit=1000)
        if not entries:
            return []

        # 加载向量
        conn = self._ensure_conn()

        def _load_vecs():
            rows = conn.execute(
                "SELECT entry_id, embedding_json FROM knowledge_vectors"
            ).fetchall()
            return {r[0]: json.loads(r[1]) for r in rows}

        vecs = await asyncio.to_thread(_load_vecs)

        scored = [
            (entry, cosine_similarity_python(query_vec, vecs.get(entry.entry_id, [])))
            for entry in entries
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [e for e, score in scored[:top_k] if score > 0.1]

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

            if entry.category == KnowledgeCategory.HUMAN_FEEDBACK:
                rating = (entry.metadata or {}).get("rating", "")
                tag = "正反馈" if rating == "positive" else "负反馈"
                knowledge.human_feedback.append(f"[{tag}] {entry.content}")
                continue

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
                # 校验条目归属当前租户 (不存在则不动任何数据)
                row = cursor.execute(
                    "SELECT id FROM knowledge_entries "
                    "WHERE entry_id = ? AND tenant = ?",
                    (entry_id, self._tenant),
                ).fetchone()
                if row is None:
                    return False
                # 删除 vec0 行
                if self._vec_available:
                    cursor.execute(
                        "DELETE FROM vec_knowledge_idx WHERE rowid = ?",
                        (row[0],),
                    )
                # 删除降级表
                cursor.execute(
                    "DELETE FROM knowledge_vectors WHERE entry_id = ?", (entry_id,),
                )
                cursor.execute(
                    "DELETE FROM knowledge_entries "
                    "WHERE entry_id = ? AND tenant = ?",
                    (entry_id, self._tenant),
                )
                conn.commit()
                return True
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
                "SELECT COUNT(*), SUM(resolved) FROM knowledge_entries "
                "WHERE tenant = ?",
                (self._tenant,),
            )
            total, resolved = cursor.fetchone()
            if self._vec_available:
                (vec_count,) = conn.execute(
                    "SELECT COUNT(*) FROM vec_knowledge_idx v "
                    "JOIN knowledge_entries ke ON ke.id = v.rowid "
                    "WHERE ke.tenant = ?",
                    (self._tenant,),
                ).fetchone()
            else:
                (vec_count,) = conn.execute(
                    "SELECT COUNT(*) FROM knowledge_vectors kv "
                    "JOIN knowledge_entries ke ON ke.entry_id = kv.entry_id "
                    "WHERE ke.tenant = ?",
                    (self._tenant,),
                ).fetchone()
            cursor = conn.execute(
                "SELECT tool_name, COUNT(*) FROM knowledge_entries "
                "WHERE tool_name != '' AND tenant = ? GROUP BY tool_name "
                "ORDER BY COUNT(*) DESC LIMIT 10",
                (self._tenant,),
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
            source_agent_id, tenant, success_rate, resolved, resolution,
            metadata_str, created_at, updated_at,
        ) = row
        return KnowledgeEntry(
            entry_id=entry_id,
            category=KnowledgeCategory(category),
            tool_name=tool_name,
            content=content,
            source_task_id=source_task_id,
            source_agent_id=source_agent_id,
            tenant=tenant or "default",
            success_rate=success_rate,
            resolved=bool(resolved),
            resolution=resolution,
            metadata=json.loads(metadata_str) if metadata_str else {},
            created_at=datetime.fromisoformat(created_at),
            updated_at=datetime.fromisoformat(updated_at),
        )

    def __repr__(self) -> str:
        return (
            f"<GlobalMemory db={self._db_path!r} tenant={self._tenant!r} "
            f"embedded={self._embedding_client is not None}>"
        )


__all__ = ["GlobalMemory"]
