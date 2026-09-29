"""
SkillStore — SOP Skill 并行持久化库 (P2 差距 7)

与 Tool 库 (.youmi_tools.db) 并行的独立数据库 (默认 .youmi_skills.db),
表结构镜像 tools 侧; Skill 通过 ``bound_tool_name`` 绑定对应的
Tool 原始名称 (等价 Tool lineage root 提交标识, CallPathRouter
SKILL 来源据此解析 root 版本)。

- skills 表: Skill 元数据 + 版本链 + 绑定关系
  (bound_tool_name, describe, summary L1/L2, content_json SOP 全文,
  tags JSON 数组, risk_level)
- vec_skills_idx / vec_skills_l2_idx: sqlite-vec vec0 双层虚拟表
  (L1 摘要向量 + L2 摘要的摘要向量, 结构同 tools 侧)
- vec_skills 表: 降级用 JSON 向量列 (sqlite-vec 不可用时)

SOP/Tool 二分: Skill 文档的 describe 与正文步骤归本库;
文档中声明的工具段落仅作为 bound_tool_name 绑定引用,
Tool 本身仍由 ToolStore 既有链路管理 (SkillIngestor 不创建 Tool)。

用法::

    from youmi.mcp.skill_store import SkillStore, SkillEntry

    store = SkillStore(db_path=".youmi_skills.db")
    await store.initialize()

    skill_id = await store.upsert_skill(SkillEntry(
        skill_name="邮件汇报", describe="每日汇总数据并发送邮件",
        bound_tool_name="send_email", content_json="...",
    ))

    # 锥形语义检索 (向量 ∩ tag ∩ 风险)
    results = await store.search_cone("发送邮件", top_k=3)

    # 绑定解析 (CallPathRouter SKILL 来源用)
    entry = await store.resolve_bound_tool("send_email")
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING

from pydantic import BaseModel, Field

from youmi._vec_utils import (
    cosine_similarity_python,
    l2_to_cosine,
    normalize_vector,
    try_load_sqlite_vec,
    vec_to_json,
)
from youmi.core.tool import RiskLevel, risk_rank

if TYPE_CHECKING:
    from youmi.llm.embeddings import EmbeddingClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------

class SkillEntry(BaseModel):
    """Skill 条目 — SOP 流程知识 (镜像 ToolEntry, 含 Tool 绑定)"""

    skill_name: str
    version: str = "0.0.1"
    parent_version_id: str | None = None
    # 绑定的 Tool 原始名称 (lineage root 提交语义; 空 = 未绑定)
    bound_tool_name: str = ""
    # 文件头 describe 原文 (L0, 保留原始信息)
    describe: str = ""
    # L1 摘要 (≤80 字, 温态显示 + 向量索引文本)
    summary: str = ""
    # L2 摘要的摘要 (≤30 字, 薄层初筛向量索引文本)
    summary_l2: str = ""
    # SOP 完整内容 (Markdown/JSON 原文)
    content_json: str = ""
    tags: list[str] = Field(default_factory=list)
    risk_level: str = RiskLevel.LOW
    created_at: str = ""
    updated_at: str = ""


class SkillSearchResult(BaseModel):
    """Skill 检索结果 (携带锥形元数据)"""

    skill_name: str
    score: float
    summary: str = ""
    summary_l2: str = ""
    version: str = ""
    bound_tool_name: str = ""
    risk_level: str = RiskLevel.LOW
    tags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 建表 SQL
# ---------------------------------------------------------------------------

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id TEXT UNIQUE NOT NULL,
    skill_name TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '0.0.1',
    parent_version_id TEXT,
    bound_tool_name TEXT DEFAULT '',
    describe TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    summary_l2 TEXT DEFAULT '',
    content_json TEXT NOT NULL DEFAULT '',
    tags TEXT DEFAULT '[]',
    risk_level TEXT DEFAULT 'low',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_skills_name ON skills(skill_name);
CREATE INDEX IF NOT EXISTS idx_skills_bound ON skills(bound_tool_name);

-- 降级用 JSON 向量表 (仅当 sqlite-vec 不可用时使用;
-- level 区分 L1/L2 双层, 同 tools 侧 vec_tools)
CREATE TABLE IF NOT EXISTS vec_skills (
    skill_id TEXT NOT NULL REFERENCES skills(skill_id),
    skill_name TEXT NOT NULL,
    embedding_json TEXT NOT NULL,
    level TEXT DEFAULT 'l1',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (skill_id, level)
);
"""

# 行读取列序 (SELECT 与 _row_to_entry 共用)
_SKILL_COLUMNS = """s.skill_id, s.skill_name, s.version, s.parent_version_id,
       s.bound_tool_name, s.describe, s.summary, s.summary_l2,
       s.content_json, s.tags, s.risk_level, s.created_at, s.updated_at"""


# ---------------------------------------------------------------------------
# SkillStore 核心
# ---------------------------------------------------------------------------

class SkillStore:
    """SOP Skill 并行持久化库 — SQLite + sqlite-vec (结构镜像 ToolStore)

    Args:
        db_path: SQLite 数据库文件路径 (默认 ".youmi_skills.db",
            与 Tool 库并行; ":memory:" 内存库供测试)
        embedding_client: EmbeddingClient 实例 (None = 不启用向量检索)
        embedding_dim: Embedding 向量维度 (默认 768)
    """

    def __init__(
        self,
        db_path: str = ".youmi_skills.db",
        embedding_client: EmbeddingClient | None = None,
        embedding_dim: int = 768,
    ) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._embedding_client = embedding_client
        self._embedding_dim = embedding_dim
        self._vec_available: bool = False

    # ==================================================================
    # 生命周期
    # ==================================================================

    async def initialize(self) -> None:
        """建库建表 (幂等) 并尝试加载 sqlite-vec"""
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

        self._vec_available = await asyncio.to_thread(
            try_load_sqlite_vec, self._conn,
        )
        if self._vec_available:
            dim = self._embedding_dim
            for vec_table in ("vec_skills_idx", "vec_skills_l2_idx"):
                vec_sql = (
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {vec_table} "
                    f"USING vec0(embedding float[{dim}])"
                )
                await asyncio.to_thread(self._conn.execute, vec_sql)
            await asyncio.to_thread(self._conn.commit)
            logger.info(
                "SkillStore initialized with sqlite-vec: %s (dim=%d, dual-level)",
                self._db_path, dim,
            )
        else:
            logger.info(
                "SkillStore initialized (fallback mode): %s", self._db_path,
            )

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SkillStore not initialized. Call initialize() first.")
        return self._conn

    async def close(self) -> None:
        """关闭数据库连接"""
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    # ==================================================================
    # 写入
    # ==================================================================

    async def upsert_skill(self, entry: SkillEntry) -> str:
        """新增/更新 Skill (skill_id = "{skill_name}@{version}")

        embedding_client 可用时自动生成双层向量
        (L1 = "{name}: {summary}", L2 = "{name}: {summary_l2}")。

        Returns:
            skill_id
        """
        conn = self._ensure_conn()
        now = datetime.utcnow().isoformat()
        skill_id = f"{entry.skill_name}@{entry.version}"
        # L1/L2 缺省时由 describe 派生 (惰性生成)
        summary = entry.summary or entry.describe[:80]
        summary_l2 = entry.summary_l2 or summary[:30]
        tags_json = json.dumps(entry.tags or [], ensure_ascii=False)

        def _upsert():
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """INSERT INTO skills (skill_id, skill_name, version,
                                          parent_version_id, bound_tool_name,
                                          describe, summary, summary_l2,
                                          content_json, tags, risk_level,
                                          created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(skill_id) DO UPDATE SET
                           parent_version_id = excluded.parent_version_id,
                           bound_tool_name = excluded.bound_tool_name,
                           describe = excluded.describe,
                           summary = excluded.summary,
                           summary_l2 = excluded.summary_l2,
                           content_json = excluded.content_json,
                           tags = excluded.tags,
                           risk_level = excluded.risk_level,
                           updated_at = excluded.updated_at""",
                    (
                        skill_id, entry.skill_name, entry.version,
                        entry.parent_version_id, entry.bound_tool_name,
                        entry.describe, summary, summary_l2,
                        entry.content_json, tags_json,
                        entry.risk_level or RiskLevel.LOW,
                        now, now,
                    ),
                )
                conn.commit()
                row = cursor.execute(
                    "SELECT id FROM skills WHERE skill_id = ?", (skill_id,),
                ).fetchone()
                return (row[0] if row else None)
            except Exception:
                conn.rollback()
                raise

        row_id = await asyncio.to_thread(_upsert)

        # 自动生成双层向量
        if self._embedding_client and row_id is not None:
            try:
                l1_vec = await self._embedding_client.embed_one(
                    f"{entry.skill_name}: {summary}",
                )
                await self._write_vec_async(
                    row_id, skill_id, entry.skill_name, l1_vec, level="l1",
                )
                if summary_l2:
                    l2_vec = await self._embedding_client.embed_one(
                        f"{entry.skill_name}: {summary_l2}",
                    )
                    await self._write_vec_async(
                        row_id, skill_id, entry.skill_name, l2_vec, level="l2",
                    )
            except Exception as exc:
                logger.warning("SkillStore: embedding failed for '%s': %s",
                               entry.skill_name, exc)

        logger.debug("SkillStore: upserted '%s' (id=%s)", entry.skill_name, skill_id)
        return skill_id

    async def _write_vec_async(
        self, row_id: int, skill_id: str, skill_name: str,
        embedding: list[float], level: str = "l1",
    ) -> None:
        """异步写入向量到 vec0 或 vec_skills 表 (指定索引层)"""
        conn = self._ensure_conn()
        now = datetime.utcnow().isoformat()

        def _write():
            cursor = conn.cursor()
            if self._vec_available:
                table = "vec_skills_l2_idx" if level == "l2" else "vec_skills_idx"
                norm = normalize_vector(embedding)
                cursor.execute(f"DELETE FROM {table} WHERE rowid = ?", (row_id,))
                cursor.execute(
                    f"INSERT INTO {table}(rowid, embedding) VALUES (?, ?)",
                    (row_id, vec_to_json(norm)),
                )
            else:
                cursor.execute(
                    """INSERT INTO vec_skills (skill_id, skill_name, embedding_json,
                                              level, updated_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(skill_id, level) DO UPDATE SET
                           embedding_json = excluded.embedding_json,
                           updated_at = excluded.updated_at""",
                    (skill_id, skill_name, json.dumps(embedding), level, now),
                )
            conn.commit()

        await asyncio.to_thread(_write)

    # ==================================================================
    # 读取
    # ==================================================================

    async def get_skill(
        self, skill_name: str, version: str | None = None,
    ) -> SkillEntry | None:
        """获取 Skill 条目 (version=None = 最新版本)"""
        conn = self._ensure_conn()

        def _get():
            if version:
                skill_id = f"{skill_name}@{version}"
                row = conn.execute(
                    f"""SELECT {_SKILL_COLUMNS}
                        FROM skills s WHERE s.skill_id = ?""",
                    (skill_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    f"""SELECT {_SKILL_COLUMNS}
                        FROM skills s WHERE s.skill_name = ?
                        ORDER BY s.created_at DESC, s.rowid DESC LIMIT 1""",
                    (skill_name,),
                ).fetchone()
            return self._row_to_entry(row) if row else None

        return await asyncio.to_thread(_get)

    async def resolve_bound_tool(self, bound_tool_name: str) -> SkillEntry | None:
        """按绑定的 Tool 原始名称解析 Skill (最新版本)

        CallPathRouter SKILL 来源接线用: 存在绑定即视为该 Tool 有
        SOP 背书 → 解析到 Tool root 版本。

        Args:
            bound_tool_name: Tool 原始名称 (lineage root 提交标识)

        Returns:
            绑定该 Tool 的最新 SkillEntry; 无绑定时返回 None
        """
        conn = self._ensure_conn()

        def _resolve():
            row = conn.execute(
                f"""SELECT {_SKILL_COLUMNS}
                    FROM skills s WHERE s.bound_tool_name = ?
                    ORDER BY s.created_at DESC, s.rowid DESC LIMIT 1""",
                (bound_tool_name,),
            ).fetchone()
            return self._row_to_entry(row) if row else None

        return await asyncio.to_thread(_resolve)

    async def get_version_chain(self, skill_name: str) -> list[SkillEntry]:
        """获取 Skill 的完整版本链 (从最新到最旧)"""
        conn = self._ensure_conn()

        def _get():
            rows = conn.execute(
                f"""SELECT {_SKILL_COLUMNS}
                    FROM skills s WHERE s.skill_name = ?
                    ORDER BY s.created_at DESC, s.rowid DESC""",
                (skill_name,),
            ).fetchall()
            return [self._row_to_entry(r) for r in rows if r]

        return await asyncio.to_thread(_get)

    @staticmethod
    def _row_to_entry(row: tuple) -> SkillEntry:
        """数据库行 → SkillEntry"""
        (skill_id, skill_name, version, parent_version_id,
         bound_tool_name, describe, summary, summary_l2,
         content_json, tags_json, risk_level, created_at, updated_at) = row

        try:
            tags = json.loads(tags_json or "[]")
            if not isinstance(tags, list):
                tags = []
        except (json.JSONDecodeError, TypeError):
            tags = []

        return SkillEntry(
            skill_name=skill_name,
            version=version,
            parent_version_id=parent_version_id,
            bound_tool_name=bound_tool_name or "",
            describe=describe or "",
            summary=summary or "",
            summary_l2=summary_l2 or "",
            content_json=content_json or "",
            tags=tags,
            risk_level=risk_level or RiskLevel.LOW,
            created_at=created_at or "",
            updated_at=updated_at or "",
        )

    # ==================================================================
    # 锥形检索 (向量 ∩ tag ∩ 风险 — 镜像 ToolStore.search_cone)
    # ==================================================================

    async def search_cone(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.3,
        tags: list[str] | None = None,
        risk_ceiling: str = RiskLevel.CRITICAL,
        index_level: str = "l1",
    ) -> list[SkillSearchResult]:
        """锥形联合检索 Skill — 语义方向 ∩ tag 边界 ∩ 风险边界

        Args:
            query: 自然语言查询
            top_k: 返回结果数量
            min_score: 最低相似度阈值
            tags: tag 边界 (tags JSON 列命中任意一个才可召回; None = 不限)
            risk_ceiling: 风险上限 (risk_level 高于此级别的 Skill 不召回)
            index_level: 向量索引层 ("l1" 摘要层 / "l2" 摘要的摘要薄层,
                L2 无命中自动回退 L1)

        Returns:
            SkillSearchResult 列表 (按分数降序)
        """
        if not self._embedding_client:
            return await self._keyword_search(
                query, top_k, tags=tags, risk_ceiling=risk_ceiling,
            )

        try:
            query_vec = await self._embedding_client.embed_one(query)
        except Exception as exc:
            logger.warning("SkillStore: embedding failed, keyword fallback: %s", exc)
            return await self._keyword_search(
                query, top_k, tags=tags, risk_ceiling=risk_ceiling,
            )

        conn = self._ensure_conn()

        if self._vec_available:
            return await self._vec0_search(
                conn, query_vec, top_k, min_score,
                tags=tags, risk_ceiling=risk_ceiling, index_level=index_level,
            )
        return await self._json_search(
            conn, query_vec, top_k, min_score,
            tags=tags, risk_ceiling=risk_ceiling, index_level=index_level,
        )

    @staticmethod
    def _cone_where(
        tags: list[str] | None,
        risk_rank_ceiling: int,
    ) -> tuple[list[str], list[Any]]:
        """构造锥形过滤 SQL 片段 (risk 列 + tags JSON 列)"""
        clauses: list[str] = [
            # 风险边界 (CASE 映射数值比较; 未知级别按 low=0)
            "(CASE s.risk_level WHEN 'medium' THEN 1 WHEN 'high' THEN 2 "
            "WHEN 'critical' THEN 3 ELSE 0 END) <= ?",
        ]
        params: list[Any] = [risk_rank_ceiling]

        if tags:
            # tags JSON 列按引号锚定 LIKE (任一命中即可召回)
            like_clauses = []
            for tag in tags:
                like_clauses.append("s.tags LIKE ?")
                params.append(f'%"{tag}"%')
            clauses.append("(" + " OR ".join(like_clauses) + ")")

        return clauses, params

    @staticmethod
    def _make_search_result(row: tuple, score: float) -> SkillSearchResult:
        """数据库行 → SkillSearchResult (尾部的 distance/embedding_json 由 *_extra 吸收)"""
        (_skill_id, skill_name, _version, _parent,
         bound_tool_name, _describe, summary, summary_l2,
         _content, tags_json, risk_level, _created, _updated, *_extra) = row

        try:
            tags = json.loads(tags_json or "[]")
            if not isinstance(tags, list):
                tags = []
        except (json.JSONDecodeError, TypeError):
            tags = []

        return SkillSearchResult(
            skill_name=skill_name,
            score=score,
            summary=summary or "",
            summary_l2=summary_l2 or "",
            version=_version or "",
            bound_tool_name=bound_tool_name or "",
            risk_level=risk_level or RiskLevel.LOW,
            tags=tags,
        )

    async def _vec0_search(
        self,
        conn: sqlite3.Connection,
        query_vec: list[float],
        top_k: int,
        min_score: float,
        tags: list[str] | None = None,
        risk_ceiling: str = RiskLevel.CRITICAL,
        index_level: str = "l1",
    ) -> list[SkillSearchResult]:
        """sqlite-vec vec0 锥形搜索 — KNN 子查询 JOIN skills + 联合 WHERE"""
        norm_query = normalize_vector(query_vec)
        fetch_k = top_k * 3 if (tags or risk_ceiling != RiskLevel.CRITICAL) else top_k

        cone_clauses, cone_params = self._cone_where(tags, risk_rank(risk_ceiling))
        cone_sql = " AND ".join(cone_clauses)
        vec_table = "vec_skills_l2_idx" if index_level == "l2" else "vec_skills_idx"

        def _search():
            return conn.execute(
                f"""SELECT {_SKILL_COLUMNS}, v.distance
                    FROM (SELECT rowid, distance FROM {vec_table}
                          WHERE embedding MATCH ? AND k = ?) v
                    JOIN skills s ON s.id = v.rowid
                    WHERE {cone_sql}
                    ORDER BY v.distance""",
                (vec_to_json(norm_query), fetch_k, *cone_params),
            ).fetchall()

        rows = await asyncio.to_thread(_search)

        # L2 薄层无命中 → 回退 L1 层 (存量行未生成 L2 向量时兼容)
        if not rows and index_level == "l2":
            logger.debug("SkillStore: L2 index empty, falling back to L1")
            return await self._vec0_search(
                conn, query_vec, top_k, min_score,
                tags=tags, risk_ceiling=risk_ceiling, index_level="l1",
            )

        results: list[SkillSearchResult] = []
        for row in rows:
            score = l2_to_cosine(row[-1])
            if score < min_score:
                continue
            results.append(self._make_search_result(row, score))
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    async def _json_search(
        self,
        conn: sqlite3.Connection,
        query_vec: list[float],
        top_k: int,
        min_score: float,
        tags: list[str] | None = None,
        risk_ceiling: str = RiskLevel.CRITICAL,
        index_level: str = "l1",
    ) -> list[SkillSearchResult]:
        """降级: JSON 向量表 + Python 余弦 + 同一锥形条件"""
        cone_clauses, cone_params = self._cone_where(tags, risk_rank(risk_ceiling))
        cone_sql = " AND ".join(cone_clauses)
        level = "l2" if index_level == "l2" else "l1"

        def _search():
            return conn.execute(
                f"""SELECT {_SKILL_COLUMNS}, v.embedding_json
                    FROM vec_skills v
                    JOIN skills s ON v.skill_id = s.skill_id
                    WHERE v.level = ? AND {cone_sql}""",
                (level, *cone_params),
            ).fetchall()

        rows = await asyncio.to_thread(_search)

        if not rows and level == "l2":
            logger.debug("SkillStore: L2 fallback rows empty, falling back to L1")
            return await self._json_search(
                conn, query_vec, top_k, min_score,
                tags=tags, risk_ceiling=risk_ceiling, index_level="l1",
            )

        results: list[SkillSearchResult] = []
        for row in rows:
            try:
                embedding = json.loads(row[-1])
            except (json.JSONDecodeError, TypeError):
                continue
            score = cosine_similarity_python(query_vec, embedding)
            if score < min_score:
                continue
            results.append(self._make_search_result(row, score))
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    async def _keyword_search(
        self,
        query: str,
        top_k: int,
        tags: list[str] | None = None,
        risk_ceiling: str = RiskLevel.CRITICAL,
    ) -> list[SkillSearchResult]:
        """关键词匹配 fallback (describe/summary LIKE + 同一锥形条件)"""
        conn = self._ensure_conn()
        query_lower = query.lower()

        cone_clauses, cone_params = self._cone_where(tags, risk_rank(risk_ceiling))
        cone_sql = " AND ".join(cone_clauses)

        def _search():
            return conn.execute(
                f"""SELECT {_SKILL_COLUMNS}
                    FROM skills s
                    WHERE (s.describe LIKE ? OR s.summary LIKE ?
                           OR s.skill_name LIKE ?) AND {cone_sql}""",
                (f"%{query}%", f"%{query}%", f"%{query}%", *cone_params),
            ).fetchall()

        rows = await asyncio.to_thread(_search)

        # 简单词频打分 (与 ToolStore._keyword_search 同风格)
        results: list[SkillSearchResult] = []
        for row in rows:
            text = " ".join([row[5] or "", row[6] or "", row[1] or ""]).lower()
            if not text:
                continue
            hits = sum(1 for tok in query_lower.split() if tok and tok in text)
            score = hits / max(len(query_lower.split()), 1)
            if score <= 0:
                continue
            results.append(self._make_search_result(row, score))
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    # ==================================================================
    # 诊断
    # ==================================================================

    async def stats(self) -> dict[str, Any]:
        """返回存储层统计信息"""
        conn = self._ensure_conn()

        def _stats():
            skills = conn.execute(
                "SELECT COUNT(DISTINCT skill_name) FROM skills",
            ).fetchone()[0]
            versions = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
            bound = conn.execute(
                "SELECT COUNT(DISTINCT bound_tool_name) FROM skills "
                "WHERE bound_tool_name != ''",
            ).fetchone()[0]
            return {"skills": skills, "versions": versions, "bound_tools": bound}

        return await asyncio.to_thread(_stats)

    def __repr__(self) -> str:
        return f"<SkillStore db_path={self._db_path!r}>"
