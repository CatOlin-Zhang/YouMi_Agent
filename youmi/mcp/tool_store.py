"""
ToolStore — 工具持久化存储层

基于 SQLite + JSON 向量列的工具持久化与向量搜索:
- tools 表: 工具元数据 + 版本链 (version, parent_version_id)
- vec_tools 表: 向量索引 (embedding 序列化为 JSON)
- tool_changelogs 表: 同版本内的 bug 修复记录
- tool_aliases 表: 别名映射 (Skill 引用旧版本)
- tool_tags 表: 工具标签
- tool_dependencies 表: 工具依赖关系

使用 Python 内置 sqlite3 + asyncio.to_thread 实现异步操作，
无需额外依赖。向量搜索通过 Python 级余弦相似度计算实现。

用法::

    from youmi.mcp.tool_store import ToolStore

    store = ToolStore(db_path="tools.db")
    await store.initialize()

    # 添加工具
    tool_id = await store.upsert_tool(entry)

    # 版本管理
    new_id = await store.create_version("my_tool", new_def, bump="minor")
    chain = await store.get_version_chain("my_tool")

    # 向量搜索
    results = await store.search("我需要一个发送邮件的工具", top_k=3)

    # 别名
    await store.add_alias("legacy_email", "send_email", "0.0.1")
    entry = await store.resolve_alias("legacy_email")
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING

from youmi.core.tool import ToolDefinition, ToolVersion, bump_version

if TYPE_CHECKING:
    from youmi.llm.embeddings import EmbeddingClient
    from youmi.mcp.vault import ToolEntry, ToolSearchResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 建表 SQL
# ---------------------------------------------------------------------------

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS tools (
    tool_id TEXT PRIMARY KEY,
    tool_name TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '0.0.1',
    parent_version_id TEXT,
    provider_id TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    definition_json TEXT NOT NULL,
    handler_module TEXT DEFAULT '',
    language TEXT DEFAULT 'python',
    runtime TEXT DEFAULT 'python',
    essential INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tools_name ON tools(tool_name);
CREATE INDEX IF NOT EXISTS idx_tools_provider ON tools(provider_id);

CREATE TABLE IF NOT EXISTS vec_tools (
    tool_id TEXT PRIMARY KEY REFERENCES tools(tool_id),
    tool_name TEXT NOT NULL,
    embedding_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vec_name ON vec_tools(tool_name);

CREATE TABLE IF NOT EXISTS tool_changelogs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_id TEXT NOT NULL REFERENCES tools(tool_id),
    change_type TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_changelog_tool ON tool_changelogs(tool_id);

CREATE TABLE IF NOT EXISTS tool_aliases (
    alias_name TEXT PRIMARY KEY,
    tool_id TEXT NOT NULL REFERENCES tools(tool_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_tags (
    tool_id TEXT NOT NULL REFERENCES tools(tool_id),
    tag TEXT NOT NULL,
    PRIMARY KEY (tool_id, tag)
);

CREATE TABLE IF NOT EXISTS tool_dependencies (
    tool_id TEXT NOT NULL REFERENCES tools(tool_id),
    depends_on_tool_id TEXT NOT NULL REFERENCES tools(tool_id),
    dependency_type TEXT DEFAULT 'required',
    PRIMARY KEY (tool_id, depends_on_tool_id)
);
"""


# ---------------------------------------------------------------------------
# 辅助: 余弦相似度 (纯 Python)
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
# ToolStore 核心
# ---------------------------------------------------------------------------

class ToolStore:
    """工具持久化存储层 — SQLite + 向量搜索

    管理工具的完整定义、语义向量、版本链、变更日志和元数据。
    与 ToolVault 配合: ToolVault 作为内存缓存层，ToolStore 作为持久化层。

    Args:
        db_path: SQLite 数据库文件路径。
            ":memory:" 使用内存数据库 (测试用)。
            默认 ".youmi_tools.db" (当前工作目录)。
        embedding_client: EmbeddingClient 实例 (None = 不启用向量搜索)
    """

    def __init__(
        self,
        db_path: str = ".youmi_tools.db",
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
        logger.info("ToolStore initialized: %s", self._db_path)

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("ToolStore not initialized. Call initialize() first.")
        return self._conn

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None
            logger.debug("ToolStore closed: %s", self._db_path)

    # ==================================================================
    # 核心 CRUD
    # ==================================================================

    async def upsert_tool(self, entry: ToolEntry) -> str:
        """插入或更新工具条目

        如果同名同版本已存在则更新，否则新建。
        自动生成 tool_id (格式: "{tool_name}@{version}")。

        Args:
            entry: ToolEntry 工具条目

        Returns:
            tool_id 字符串
        """
        from youmi.mcp.vault import ToolEntry as _TE  # 延迟导入

        conn = self._ensure_conn()
        now = datetime.utcnow().isoformat()
        version = getattr(entry, 'version', '0.0.1') or '0.0.1'
        tool_id = f"{entry.tool_name}@{version}"
        defn_json = entry.definition.model_dump_json()
        summary = entry.summary or entry.definition.description[:80]

        def _upsert():
            cursor = conn.cursor()
            try:
                # 检查是否已存在
                existing = cursor.execute(
                    "SELECT tool_id, version FROM tools WHERE tool_name = ? ORDER BY created_at DESC LIMIT 1",
                    (entry.tool_name,),
                ).fetchone()

                parent_id = None
                if existing:
                    parent_id = existing[0]

                cursor.execute(
                    """INSERT INTO tools (tool_id, tool_name, version, parent_version_id,
                                          provider_id, summary, definition_json,
                                          language, runtime, essential, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(tool_id) DO UPDATE SET
                           summary = excluded.summary,
                           definition_json = excluded.definition_json,
                           updated_at = excluded.updated_at
                    """,
                    (
                        tool_id, entry.tool_name, version, parent_id,
                        entry.provider_id, summary, defn_json,
                        getattr(entry.definition, 'language', 'python'),
                        getattr(entry.definition, 'runtime', 'python'),
                        1 if entry.essential else 0,
                        now, now,
                    ),
                )

                # 更新向量 (如果有 embedding)
                if entry.embedding:
                    cursor.execute(
                        """INSERT INTO vec_tools (tool_id, tool_name, embedding_json, updated_at)
                           VALUES (?, ?, ?, ?)
                           ON CONFLICT(tool_id) DO UPDATE SET
                               embedding_json = excluded.embedding_json,
                               updated_at = excluded.updated_at
                        """,
                        (tool_id, entry.tool_name, json.dumps(entry.embedding), now),
                    )

                conn.commit()
            except Exception:
                conn.rollback()
                raise

        await asyncio.to_thread(_upsert)
        logger.debug("ToolStore: upserted '%s' (id=%s)", entry.tool_name, tool_id)
        return tool_id

    async def get_tool(self, tool_name: str, version: str | None = None) -> ToolEntry | None:
        """获取工具条目

        Args:
            tool_name: 工具名称
            version: 版本号 (None = 最新版本)

        Returns:
            ToolEntry 或 None
        """
        conn = self._ensure_conn()

        def _get():
            if version:
                tool_id = f"{tool_name}@{version}"
                row = conn.execute(
                    """SELECT tool_id, tool_name, version, parent_version_id, provider_id,
                              summary, definition_json, language, runtime, essential,
                              created_at, updated_at
                       FROM tools WHERE tool_id = ?""",
                    (tool_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT tool_id, tool_name, version, parent_version_id, provider_id,
                              summary, definition_json, language, runtime, essential,
                              created_at, updated_at
                       FROM tools WHERE tool_name = ?
                       ORDER BY created_at DESC LIMIT 1""",
                    (tool_name,),
                ).fetchone()

            if row is None:
                return None

            return self._row_to_entry(row, conn)

        return await asyncio.to_thread(_get)

    async def get_latest_version(self, tool_name: str) -> ToolEntry | None:
        """获取工具的最新版本"""
        return await self.get_tool(tool_name, version=None)

    async def list_tools(self) -> list[ToolEntry]:
        """列出所有工具 (每个工具仅返回最新版本)"""
        conn = self._ensure_conn()

        def _list():
            # 使用子查询获取每个工具的最新版本
            rows = conn.execute(
                """SELECT t.tool_id, t.tool_name, t.version, t.parent_version_id,
                          t.provider_id, t.summary, t.definition_json,
                          t.language, t.runtime, t.essential,
                          t.created_at, t.updated_at
                   FROM tools t
                   INNER JOIN (
                       SELECT tool_name, MAX(created_at) as max_created
                       FROM tools GROUP BY tool_name
                   ) latest ON t.tool_name = latest.tool_name
                           AND t.created_at = latest.max_created
                   ORDER BY t.tool_name"""
            ).fetchall()

            entries = []
            for row in rows:
                entry = self._row_to_entry(row, conn)
                if entry:
                    entries.append(entry)
            return entries

        return await asyncio.to_thread(_list)

    async def delete_tool(self, tool_name: str, version: str | None = None) -> bool:
        """删除工具

        Args:
            tool_name: 工具名称
            version: 版本号 (None = 删除所有版本)

        Returns:
            是否删除成功
        """
        conn = self._ensure_conn()

        def _delete():
            try:
                if version:
                    tool_id = f"{tool_name}@{version}"
                    conn.execute("DELETE FROM vec_tools WHERE tool_id = ?", (tool_id,))
                    conn.execute("DELETE FROM tool_changelogs WHERE tool_id = ?", (tool_id,))
                    conn.execute("DELETE FROM tool_tags WHERE tool_id = ?", (tool_id,))
                    conn.execute("DELETE FROM tool_aliases WHERE tool_id = ?", (tool_id,))
                    conn.execute("DELETE FROM tool_dependencies WHERE tool_id = ? OR depends_on_tool_id = ?",
                                 (tool_id, tool_id))
                    conn.execute("DELETE FROM tools WHERE tool_id = ?", (tool_id,))
                else:
                    # 删除所有版本
                    tool_ids = [r[0] for r in conn.execute(
                        "SELECT tool_id FROM tools WHERE tool_name = ?", (tool_name,)
                    ).fetchall()]
                    for tid in tool_ids:
                        conn.execute("DELETE FROM vec_tools WHERE tool_id = ?", (tid,))
                        conn.execute("DELETE FROM tool_changelogs WHERE tool_id = ?", (tid,))
                        conn.execute("DELETE FROM tool_tags WHERE tool_id = ?", (tid,))
                        conn.execute("DELETE FROM tool_aliases WHERE tool_id = ?", (tid,))
                        conn.execute("DELETE FROM tool_dependencies WHERE tool_id = ? OR depends_on_tool_id = ?",
                                     (tid, tid))
                    conn.execute("DELETE FROM tools WHERE tool_name = ?", (tool_name,))

                conn.commit()
                return True
            except Exception:
                conn.rollback()
                return False

        return await asyncio.to_thread(_delete)

    # ==================================================================
    # 版本管理
    # ==================================================================

    async def create_version(
        self,
        tool_name: str,
        new_definition: ToolDefinition,
        changelog: str = "",
        bump: str = "patch",
    ) -> str:
        """创建工具新版本

        自增版本号，写入新 tools 行 + 更新 vec_tools。

        Args:
            tool_name: 工具名称
            new_definition: 新的工具定义
            changelog: 版本变更说明
            bump: 自增类型 "patch" | "minor" | "major"

        Returns:
            新版本 tool_id
        """
        conn = self._ensure_conn()
        now = datetime.utcnow().isoformat()

        def _create():
            # 获取最新版本
            row = conn.execute(
                """SELECT tool_id, version, definition_json FROM tools
                   WHERE tool_name = ? ORDER BY created_at DESC LIMIT 1""",
                (tool_name,),
            ).fetchone()

            if row is None:
                raise ValueError(f"Tool '{tool_name}' not found in store")

            old_tool_id, old_version, _ = row
            new_version = bump_version(old_version, bump)
            new_tool_id = f"{tool_name}@{new_version}"
            defn_json = new_definition.model_dump_json()

            cursor = conn.cursor()
            try:
                cursor.execute(
                    """INSERT INTO tools (tool_id, tool_name, version, parent_version_id,
                                          provider_id, summary, definition_json,
                                          language, runtime, essential, created_at, updated_at)
                       SELECT ?, ?, ?, ?, provider_id, ?, ?,
                              ?, ?, essential, ?, ?
                       FROM tools WHERE tool_id = ?
                    """,
                    (
                        new_tool_id, tool_name, new_version, old_tool_id,
                        new_definition.description[:80], defn_json,
                        getattr(new_definition, 'language', 'python'),
                        getattr(new_definition, 'runtime', 'python'),
                        now, now, old_tool_id,
                    ),
                )

                # 如果有 embedding_client，生成新向量
                conn.commit()
            except Exception:
                conn.rollback()
                raise

            return new_tool_id, new_version, old_tool_id

        new_tool_id, new_version, old_tool_id = await asyncio.to_thread(_create)

        # 异步生成向量
        if self._embedding_client:
            try:
                text = f"{tool_name}: {new_definition.description}"
                vec = await self._embedding_client.embed_one(text)
                await self._update_vec_embedding(new_tool_id, tool_name, vec)
            except Exception as exc:
                logger.warning("ToolStore: embedding failed for new version: %s", exc)

        # 写入 changelog
        if changelog:
            await self.add_changelog(tool_name, "version_update", changelog, source="system")

        logger.info("ToolStore: created version %s for '%s' (id=%s)",
                     new_version, tool_name, new_tool_id)
        return new_tool_id

    async def get_version_chain(self, tool_name: str) -> list[ToolVersion]:
        """获取工具的完整版本链 (从最新到最旧)

        Args:
            tool_name: 工具名称

        Returns:
            ToolVersion 列表 (按时间倒序)
        """
        conn = self._ensure_conn()

        def _get():
            rows = conn.execute(
                """SELECT tool_id, version, parent_version_id, definition_json,
                          created_at, updated_at
                   FROM tools WHERE tool_name = ?
                   ORDER BY created_at DESC, rowid DESC""",
                (tool_name,),
            ).fetchall()

            chain = []
            for row in rows:
                tool_id, version, parent_id, defn_json, created, updated = row
                # 查找该版本的 changelog
                changelogs = conn.execute(
                    "SELECT description FROM tool_changelogs WHERE tool_id = ? ORDER BY created_at",
                    (tool_id,),
                ).fetchall()
                changelog_text = "; ".join(c[0] for c in changelogs)

                chain.append(ToolVersion(
                    version=version,
                    parent_version_id=parent_id,
                    definition_json=defn_json,
                    created_at=created,
                    changelog=changelog_text,
                ))
            return chain

        return await asyncio.to_thread(_get)

    async def get_version(self, tool_name: str, version: str) -> ToolEntry | None:
        """获取指定版本的工具条目"""
        return await self.get_tool(tool_name, version=version)

    async def add_changelog(
        self,
        tool_name: str,
        change_type: str,
        description: str,
        source: str = "",
    ) -> None:
        """添加工具内部变更日志 (同版本内的 bug 修复记录)

        Args:
            tool_name: 工具名称
            change_type: 变更类型 ('bugfix' | 'description_update' | 'param_update' | 'version_update')
            description: LLM 生成的变更说明
            source: 来源 (agent_id 或 'system')
        """
        conn = self._ensure_conn()
        now = datetime.utcnow().isoformat()

        def _add():
            # 获取最新版本的 tool_id
            row = conn.execute(
                "SELECT tool_id FROM tools WHERE tool_name = ? ORDER BY created_at DESC LIMIT 1",
                (tool_name,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Tool '{tool_name}' not found in store")

            tool_id = row[0]
            conn.execute(
                """INSERT INTO tool_changelogs (tool_id, change_type, description, created_at, source)
                   VALUES (?, ?, ?, ?, ?)""",
                (tool_id, change_type, description, now, source),
            )
            conn.commit()

        await asyncio.to_thread(_add)
        logger.debug("ToolStore: added changelog for '%s' (%s)", tool_name, change_type)

    # ==================================================================
    # 向量搜索
    # ==================================================================

    async def update_embedding(self, tool_name: str) -> None:
        """为指定工具生成/更新向量

        Args:
            tool_name: 工具名称
        """
        if not self._embedding_client:
            return

        conn = self._ensure_conn()

        def _get_defn():
            row = conn.execute(
                "SELECT tool_id, definition_json FROM tools WHERE tool_name = ? ORDER BY created_at DESC LIMIT 1",
                (tool_name,),
            ).fetchone()
            return row

        row = await asyncio.to_thread(_get_defn)
        if row is None:
            return

        tool_id, defn_json = row
        defn = ToolDefinition.model_validate_json(defn_json)
        text = f"{tool_name}: {defn.description}"

        try:
            vec = await self._embedding_client.embed_one(text)
            await self._update_vec_embedding(tool_id, tool_name, vec)
        except Exception as exc:
            logger.warning("ToolStore: embedding failed for '%s': %s", tool_name, exc)

    async def _update_vec_embedding(
        self, tool_id: str, tool_name: str, embedding: list[float],
    ) -> None:
        """写入向量到 vec_tools 表"""
        conn = self._ensure_conn()
        now = datetime.utcnow().isoformat()

        def _update():
            conn.execute(
                """INSERT INTO vec_tools (tool_id, tool_name, embedding_json, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(tool_id) DO UPDATE SET
                       embedding_json = excluded.embedding_json,
                       updated_at = excluded.updated_at
                """,
                (tool_id, tool_name, json.dumps(embedding), now),
            )
            conn.commit()

        await asyncio.to_thread(_update)

    async def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.3,
        exclude: set[str] | None = None,
    ) -> list[ToolSearchResult]:
        """语义搜索工具

        流程:
        1. 将 query 通过 embedding_client 生成向量
        2. 从 vec_tools 读取所有向量，计算余弦相似度
        3. 过滤 min_score 以下和 exclude 中的结果
        4. 按分数降序返回 Top-K

        如果无 embedding_client，回退到关键词匹配。

        Args:
            query: 自然语言查询
            top_k: 返回结果数量
            min_score: 最低相似度阈值
            exclude: 排除的工具名称集合 (用于召回确认闭环)

        Returns:
            ToolSearchResult 列表 (按分数降序)
        """
        from youmi.mcp.vault import ToolSearchResult

        if not self._embedding_client:
            return await self._keyword_search(query, top_k, exclude)

        # 生成查询向量
        try:
            query_vec = await self._embedding_client.embed_one(query)
        except Exception as exc:
            logger.warning("ToolStore: embedding failed, falling back to keyword: %s", exc)
            return await self._keyword_search(query, top_k, exclude)

        conn = self._ensure_conn()

        def _search():
            rows = conn.execute(
                """SELECT v.tool_id, v.tool_name, v.embedding_json,
                          t.definition_json, t.summary
                   FROM vec_tools v
                   JOIN tools t ON v.tool_id = t.tool_id
                """
            ).fetchall()
            return rows

        rows = await asyncio.to_thread(_search)
        if not rows:
            return []

        results: list[ToolSearchResult] = []
        for row in rows:
            tool_id, tool_name, emb_json, defn_json, summary = row

            # 排除已否决项
            if exclude and tool_name in exclude:
                continue

            embedding = json.loads(emb_json)
            score = _cosine_similarity(query_vec, embedding)
            if score >= min_score:
                defn = ToolDefinition.model_validate_json(defn_json)
                results.append(ToolSearchResult(
                    tool_name=tool_name,
                    definition=defn,
                    score=score,
                    summary=summary or defn.description[:80],
                ))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    async def _keyword_search(
        self, query: str, top_k: int, exclude: set[str] | None = None,
    ) -> list[ToolSearchResult]:
        """关键词匹配 fallback"""
        from youmi.mcp.vault import ToolSearchResult

        conn = self._ensure_conn()
        query_lower = query.lower()
        query_tokens = set(query_lower.split())
        query_tokens.update(c for c in query_lower if not c.isspace())

        def _search():
            rows = conn.execute(
                """SELECT t.tool_name, t.definition_json, t.summary
                   FROM tools t
                   INNER JOIN (
                       SELECT tool_name, MAX(created_at) as max_created
                       FROM tools GROUP BY tool_name
                   ) latest ON t.tool_name = latest.tool_name
                           AND t.created_at = latest.max_created
                """
            ).fetchall()
            return rows

        rows = await asyncio.to_thread(_search)

        scored: list[ToolSearchResult] = []
        for tool_name, defn_json, summary in rows:
            if exclude and tool_name in exclude:
                continue

            defn = ToolDefinition.model_validate_json(defn_json)
            text = f"{tool_name} {defn.description} {summary or ''}".lower()
            text_tokens = set(text.split())
            text_tokens.update(c for c in text if not c.isspace())
            overlap = query_tokens & text_tokens
            score = len(overlap) / max(len(query_tokens), 1)

            if score > 0:
                scored.append(ToolSearchResult(
                    tool_name=tool_name,
                    definition=defn,
                    score=min(score, 1.0),
                    summary=summary or defn.description[:80],
                ))

        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]

    # ==================================================================
    # 别名与标签
    # ==================================================================

    async def add_alias(self, alias_name: str, tool_name: str, version: str) -> None:
        """添加工具别名 (Skill 引用旧版本时使用)

        Args:
            alias_name: 别名
            tool_name: 工具名称
            version: 目标版本号
        """
        conn = self._ensure_conn()
        now = datetime.utcnow().isoformat()
        tool_id = f"{tool_name}@{version}"

        def _add():
            conn.execute(
                """INSERT INTO tool_aliases (alias_name, tool_id, created_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(alias_name) DO UPDATE SET
                       tool_id = excluded.tool_id,
                       created_at = excluded.created_at
                """,
                (alias_name, tool_id, now),
            )
            conn.commit()

        await asyncio.to_thread(_add)

    async def resolve_alias(self, alias_name: str) -> ToolEntry | None:
        """解析别名到工具条目

        Args:
            alias_name: 别名

        Returns:
            ToolEntry 或 None
        """
        conn = self._ensure_conn()

        def _resolve():
            row = conn.execute(
                "SELECT tool_id FROM tool_aliases WHERE alias_name = ?",
                (alias_name,),
            ).fetchone()
            if row is None:
                return None

            tool_id = row[0]
            tool_row = conn.execute(
                """SELECT tool_id, tool_name, version, parent_version_id, provider_id,
                          summary, definition_json, language, runtime, essential,
                          created_at, updated_at
                   FROM tools WHERE tool_id = ?""",
                (tool_id,),
            ).fetchone()

            if tool_row is None:
                return None

            return self._row_to_entry(tool_row, conn)

        return await asyncio.to_thread(_resolve)

    async def add_tag(self, tool_name: str, tag: str) -> None:
        """添加工具标签

        Args:
            tool_name: 工具名称
            tag: 标签
        """
        conn = self._ensure_conn()

        def _add():
            row = conn.execute(
                "SELECT tool_id FROM tools WHERE tool_name = ? ORDER BY created_at DESC LIMIT 1",
                (tool_name,),
            ).fetchone()
            if row is None:
                return

            tool_id = row[0]
            conn.execute(
                """INSERT OR IGNORE INTO tool_tags (tool_id, tag) VALUES (?, ?)""",
                (tool_id, tag),
            )
            conn.commit()

        await asyncio.to_thread(_add)

    async def search_by_tags(self, tags: list[str]) -> list[ToolEntry]:
        """按标签搜索工具 (返回包含任意指定标签的工具)

        Args:
            tags: 标签列表

        Returns:
            ToolEntry 列表
        """
        conn = self._ensure_conn()

        def _search():
            placeholders = ",".join("?" for _ in tags)
            rows = conn.execute(
                f"""SELECT DISTINCT t.tool_id, t.tool_name, t.version, t.parent_version_id,
                          t.provider_id, t.summary, t.definition_json,
                          t.language, t.runtime, t.essential,
                          t.created_at, t.updated_at
                   FROM tools t
                   JOIN tool_tags tt ON t.tool_id = tt.tool_id
                   WHERE tt.tag IN ({placeholders})
                   ORDER BY t.tool_name""",
                tags,
            ).fetchall()

            entries = []
            for row in rows:
                entry = self._row_to_entry(row, conn)
                if entry:
                    entries.append(entry)
            return entries

        return await asyncio.to_thread(_search)

    # ==================================================================
    # 依赖关系
    # ==================================================================

    async def add_dependency(
        self, tool_name: str, depends_on: str, dep_type: str = "required",
    ) -> None:
        """添加工具依赖关系

        Args:
            tool_name: 工具名称
            depends_on: 依赖的工具名称
            dep_type: 依赖类型 ('required' | 'optional')
        """
        conn = self._ensure_conn()

        def _add():
            src = conn.execute(
                "SELECT tool_id FROM tools WHERE tool_name = ? ORDER BY created_at DESC LIMIT 1",
                (tool_name,),
            ).fetchone()
            dst = conn.execute(
                "SELECT tool_id FROM tools WHERE tool_name = ? ORDER BY created_at DESC LIMIT 1",
                (depends_on,),
            ).fetchone()
            if src is None or dst is None:
                return

            conn.execute(
                """INSERT OR IGNORE INTO tool_dependencies
                   (tool_id, depends_on_tool_id, dependency_type) VALUES (?, ?, ?)""",
                (src[0], dst[0], dep_type),
            )
            conn.commit()

        await asyncio.to_thread(_add)

    # ==================================================================
    # 内部辅助
    # ==================================================================

    @staticmethod
    def _row_to_entry(row: tuple, conn: sqlite3.Connection) -> ToolEntry | None:
        """将数据库行转换为 ToolEntry"""
        from youmi.mcp.vault import ToolEntry, ToolContextTier

        if row is None:
            return None

        (tool_id, tool_name, version, parent_version_id, provider_id,
         summary, defn_json, language, runtime, essential,
         created_at, updated_at) = row

        try:
            defn = ToolDefinition.model_validate_json(defn_json)
        except Exception:
            logger.warning("ToolStore: failed to parse definition for '%s'", tool_name)
            return None

        # 读取向量 (如果有)
        embedding: list[float] = []
        vec_row = conn.execute(
            "SELECT embedding_json FROM vec_tools WHERE tool_id = ?",
            (tool_id,),
        ).fetchone()
        if vec_row:
            try:
                embedding = json.loads(vec_row[0])
            except (json.JSONDecodeError, TypeError):
                pass

        entry = ToolEntry(
            tool_name=tool_name,
            definition=defn,
            handler=None,  # handler 不可序列化，加载时需重新绑定
            provider_id=provider_id,
            essential=bool(essential),
            embedding=embedding,
            summary=summary or defn.description[:80],
            tier=ToolContextTier.COLD,  # 持久化层不存上下文状态
            last_used_turn=-1,
            use_count=0,
            version=version,
            language=language,
        )
        return entry

    # ==================================================================
    # 诊断
    # ==================================================================

    async def stats(self) -> dict[str, Any]:
        """返回存储层统计信息"""
        conn = self._ensure_conn()

        def _stats():
            tools = conn.execute("SELECT COUNT(DISTINCT tool_name) FROM tools").fetchone()[0]
            versions = conn.execute("SELECT COUNT(*) FROM tools").fetchone()[0]
            vectors = conn.execute("SELECT COUNT(*) FROM vec_tools").fetchone()[0]
            changelogs = conn.execute("SELECT COUNT(*) FROM tool_changelogs").fetchone()[0]
            aliases = conn.execute("SELECT COUNT(*) FROM tool_aliases").fetchone()[0]
            tags = conn.execute("SELECT COUNT(DISTINCT tag) FROM tool_tags").fetchone()[0]
            return {
                "tools": tools,
                "versions": versions,
                "vectors": vectors,
                "changelogs": changelogs,
                "aliases": aliases,
                "tags": tags,
            }

        return await asyncio.to_thread(_stats)

    def __repr__(self) -> str:
        return f"<ToolStore db_path={self._db_path!r}>"
