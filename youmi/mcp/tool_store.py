"""
ToolStore — 工具持久化存储层

基于 SQLite + sqlite-vec 向量搜索的工具持久化与语义检索:
- tools 表: 工具元数据 + 版本链 (version, parent_version_id)
  + 锥形检索列 (risk_level, required_permissions, lineage_id)
- vec_tools_idx 表: sqlite-vec vec0 虚拟表 (归一化向量, KNN 查询)
- vec_tools 表: 降级用 JSON 向量列 (sqlite-vec 不可用时)
- tool_changelogs 表: 同版本内的 bug 修复记录
- tool_aliases 表: 别名映射 (Skill 引用旧版本)
- tool_tags 表: 工具标签
- tool_dependencies 表: 工具依赖关系

使用 Python 内置 sqlite3 + asyncio.to_thread 实现异步操作。
向量搜索优先使用 sqlite-vec 的 vec0 KNN 查询，
扩展不可用时降级为纯 Python 余弦相似度计算。

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

from youmi._vec_utils import (
    cosine_similarity_python,
    cosine_to_l2,
    l2_to_cosine,
    normalize_vector,
    try_load_sqlite_vec,
    vec_to_json,
)
from youmi.core.tool import (
    RiskLevel,
    ToolDefinition,
    ToolVersion,
    bump_version,
    risk_rank,
)
from youmi.mcp.summary import SummaryGenerator

if TYPE_CHECKING:
    from youmi.llm.embeddings import EmbeddingClient
    from youmi.mcp.vault import ToolEntry, ToolSearchResult

logger = logging.getLogger(__name__)

# 向后兼容: 保留旧名称供测试和其他模块引用
_cosine_similarity = cosine_similarity_python


class VersionConflictError(Exception):
    """三向合并字段级冲突 — conflicts 属性列出冲突字段路径"""

    def __init__(self, tool_name: str, conflicts: list[str]) -> None:
        self.tool_name = tool_name
        self.conflicts = conflicts
        super().__init__(
            f"merge conflict on '{tool_name}': {', '.join(conflicts)}"
        )


# dict diff/merge 的"键不存在"哨兵
_MISSING = object()


def _join_path(prefix: str, key: str) -> str:
    return f"{prefix}.{key}" if prefix else key


def _dict_diff(old: dict, new: dict, path: str = "") -> dict[str, dict]:
    """递归结构化 diff — added/removed/changed 三段 (不引入新依赖)

    Returns:
        {"added": {path: value}, "removed": {path: value},
         "changed": {path: [old, new]}} — path 为点分字段路径
    """
    added: dict[str, Any] = {}
    removed: dict[str, Any] = {}
    changed: dict[str, Any] = {}

    for key in old:
        if key not in new:
            removed[_join_path(path, key)] = old[key]
        elif old[key] != new[key]:
            if isinstance(old[key], dict) and isinstance(new[key], dict):
                sub = _dict_diff(old[key], new[key], _join_path(path, key))
                added.update(sub["added"])
                removed.update(sub["removed"])
                changed.update(sub["changed"])
            else:
                changed[_join_path(path, key)] = [old[key], new[key]]

    for key in new:
        if key not in old:
            added[_join_path(path, key)] = new[key]

    return {"added": added, "removed": removed, "changed": changed}


def _merge_dicts(base: dict, ours: dict, theirs: dict) -> tuple[dict, list[str]]:
    """三向合并 (Git merge 语义) — 返回 (merged, conflicts)

    - 双方一致 (含同时增删) → 取该值
    - 仅一方修改 → 取修改方
    - 双方都改且不同 → 记入 conflicts (字段点分路径, 不自动合并);
      dict×dict×dict 时深入递归
    """
    merged = dict(ours)
    conflicts: list[str] = []

    for key in sorted(base.keys() | ours.keys() | theirs.keys()):
        b = base.get(key, _MISSING)
        o = ours.get(key, _MISSING)
        t = theirs.get(key, _MISSING)

        if o == t:
            if o is _MISSING:
                merged.pop(key, None)
            else:
                merged[key] = o
            continue
        if b == o:
            # ours 未改 → 采用 theirs
            if t is _MISSING:
                merged.pop(key, None)
            else:
                merged[key] = t
            continue
        if b == t:
            # theirs 未改 → 保留 ours
            merged[key] = o
            continue

        # 双方都改且不同
        if isinstance(b, dict) and isinstance(o, dict) and isinstance(t, dict):
            sub_merged, sub_conflicts = _merge_dicts(b, o, t)
            merged[key] = sub_merged
            conflicts.extend(f"{key}.{c}" for c in sub_conflicts)
            continue
        conflicts.append(key)

    return merged, conflicts

# ---------------------------------------------------------------------------
# 建表 SQL
# ---------------------------------------------------------------------------

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS tools (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_id TEXT UNIQUE NOT NULL,
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
    risk_level TEXT DEFAULT 'low',
    required_permissions TEXT DEFAULT '[]',
    lineage_id TEXT DEFAULT '',
    summary_l2 TEXT DEFAULT '',
    branch TEXT DEFAULT 'main',
    is_head INTEGER DEFAULT 1,
    diff_patch TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tools_name ON tools(tool_name);
CREATE INDEX IF NOT EXISTS idx_tools_provider ON tools(provider_id);
CREATE INDEX IF NOT EXISTS idx_tools_lineage ON tools(lineage_id);

-- 降级用 JSON 向量表 (仅当 sqlite-vec 不可用时使用)
-- level 区分 L1 (摘要向量) / L2 (摘要的摘要向量) 双层索引
CREATE TABLE IF NOT EXISTS vec_tools (
    tool_id TEXT NOT NULL REFERENCES tools(tool_id),
    tool_name TEXT NOT NULL,
    embedding_json TEXT NOT NULL,
    level TEXT DEFAULT 'l1',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tool_id, level)
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
# ToolStore 核心
# ---------------------------------------------------------------------------

class ToolStore:
    """工具持久化存储层 — SQLite + sqlite-vec 向量搜索

    管理工具的完整定义、语义向量、版本链、变更日志和元数据。
    与 ToolVault 配合: ToolVault 作为内存缓存层，ToolStore 作为持久化层。

    向量搜索优先使用 sqlite-vec 的 vec0 KNN 查询（高效），
    扩展不可用时降级为纯 Python 余弦相似度计算。

    Args:
        db_path: SQLite 数据库文件路径。
            ":memory:" 使用内存数据库 (测试用)。
            默认 ".youmi_tools.db" (当前工作目录)。
        embedding_client: EmbeddingClient 实例 (None = 不启用向量搜索)
        embedding_dim: Embedding 向量维度 (默认 768, 对应 nomic-embed-text)
        summary_generator: 三级摘要生成器 (可选; 默认启发式截断,
            注入 LLM 时生成式摘要, 见 youmi/mcp/summary.py)
    """

    def __init__(
        self,
        db_path: str = ".youmi_tools.db",
        embedding_client: EmbeddingClient | None = None,
        embedding_dim: int = 768,
        summary_generator: SummaryGenerator | None = None,
    ) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._embedding_client = embedding_client
        self._embedding_dim = embedding_dim
        self._vec_available: bool = False
        self._summary_generator = summary_generator or SummaryGenerator()

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
        # 旧库升级必须先于 executescript: _CREATE_TABLES_SQL 含
        # idx_tools_lineage 索引, 旧库缺 lineage_id 列时先建索引会失败
        await asyncio.to_thread(self._migrate_schema, self._conn)
        await asyncio.to_thread(self._conn.executescript, _CREATE_TABLES_SQL)
        await asyncio.to_thread(self._conn.commit)

        # 尝试加载 sqlite-vec 扩展
        self._vec_available = await asyncio.to_thread(
            try_load_sqlite_vec, self._conn,
        )
        if self._vec_available:
            dim = self._embedding_dim
            # 双层向量索引: L1 摘要向量 + L2 摘要的摘要向量 (薄层初筛)
            for vec_table in ("vec_tools_idx", "vec_tools_l2_idx"):
                vec_sql = (
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS {vec_table} "
                    f"USING vec0(embedding float[{dim}])"
                )
                await asyncio.to_thread(self._conn.execute, vec_sql)
            await asyncio.to_thread(self._conn.commit)
            logger.info(
                "ToolStore initialized with sqlite-vec: %s (dim=%d, dual-level)",
                self._db_path, dim,
            )
        else:
            logger.info(
                "ToolStore initialized (fallback mode): %s", self._db_path,
            )

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("ToolStore not initialized. Call initialize() first.")
        return self._conn

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        """旧库幂等升级 — 补齐锥形检索列并回填 lineage_id

        - PRAGMA table_info 检测缺列 → ALTER TABLE ADD COLUMN
        - 存量行回填 lineage_id = tool_name (版本链标识)
        - 建索引 idx_tools_lineage

        新建库由 _CREATE_TABLES_SQL 直接建齐，此方法检测到无缺列时为空操作。
        """
        columns = {row[1] for row in conn.execute("PRAGMA table_info(tools)").fetchall()}
        if not columns:
            return  # tools 表不存在 (异常情况, 不处理)

        migrations = [
            ("risk_level", "ALTER TABLE tools ADD COLUMN risk_level TEXT DEFAULT 'low'"),
            (
                "required_permissions",
                "ALTER TABLE tools ADD COLUMN required_permissions TEXT DEFAULT '[]'",
            ),
            ("lineage_id", "ALTER TABLE tools ADD COLUMN lineage_id TEXT DEFAULT ''"),
            # P2: 三级摘要索引 + Git 版本树列
            ("summary_l2", "ALTER TABLE tools ADD COLUMN summary_l2 TEXT DEFAULT ''"),
            ("branch", "ALTER TABLE tools ADD COLUMN branch TEXT DEFAULT 'main'"),
            ("is_head", "ALTER TABLE tools ADD COLUMN is_head INTEGER DEFAULT 1"),
            ("diff_patch", "ALTER TABLE tools ADD COLUMN diff_patch TEXT DEFAULT ''"),
        ]
        changed = False
        for col_name, ddl in migrations:
            if col_name not in columns:
                conn.execute(ddl)
                changed = True
                logger.info("ToolStore: migrated tools table, added column '%s'", col_name)

        if changed:
            conn.commit()

        # vec_tools 降级表加 level 列区分 L1/L2 向量层:
        # 主键需变为 (tool_id, level) 复合主键, ALTER 无法实现 → 重建迁移
        vec_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(vec_tools)").fetchall()
        }
        if vec_cols and "level" not in vec_cols:
            conn.executescript("""
                ALTER TABLE vec_tools RENAME TO vec_tools_old;
                CREATE TABLE vec_tools (
                    tool_id TEXT NOT NULL REFERENCES tools(tool_id),
                    tool_name TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    level TEXT DEFAULT 'l1',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tool_id, level)
                );
                INSERT INTO vec_tools (tool_id, tool_name, embedding_json, level, updated_at)
                    SELECT tool_id, tool_name, embedding_json, 'l1', updated_at
                    FROM vec_tools_old;
                DROP TABLE vec_tools_old;
            """)
            logger.info("ToolStore: migrated vec_tools table, added 'level' column")

        # 回填 lineage_id (含新建库时 CREATE TABLE IF NOT EXISTS 未覆盖的旧行)
        backfilled = conn.execute(
            "UPDATE tools SET lineage_id = tool_name WHERE lineage_id = '' OR lineage_id IS NULL"
        ).rowcount
        if backfilled:
            conn.commit()
            logger.info("ToolStore: backfilled lineage_id for %d rows", backfilled)

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tools_lineage ON tools(lineage_id)"
        )
        conn.commit()

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
        # 锥形检索列: risk/permissions 以 definition 为权威源 (随版本化)
        risk_level = getattr(entry.definition, 'risk_level', RiskLevel.LOW) or RiskLevel.LOW
        req_perms = getattr(entry.definition, 'required_permissions', None) or []
        # lineage_id: 同名工具全部版本共享, 默认 = tool_name
        lineage_id = entry.lineage_id or entry.tool_name
        # L2 摘要: entry 显式提供优先, 否则 SummaryGenerator 生成 (可插拔)
        summary_l2 = (
            getattr(entry, 'summary_l2', '')
            or await self._summary_generator.generate_l2(entry.tool_name, summary)
        )

        def _upsert():
            cursor = conn.cursor()
            try:
                # 检查是否已存在 (rowid 次级排序消除同微秒碰撞)
                existing = cursor.execute(
                    """SELECT id, tool_id, version FROM tools WHERE tool_name = ?
                       ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                    (entry.tool_name,),
                ).fetchone()

                parent_id = None
                if existing:
                    parent_id = existing[1]  # tool_id of previous version

                # INSERT ... ON CONFLICT DO UPDATE 保持 id 稳定
                cursor.execute(
                    """INSERT INTO tools (tool_id, tool_name, version, parent_version_id,
                                          provider_id, summary, definition_json,
                                          language, runtime, essential,
                                          risk_level, required_permissions, lineage_id,
                                          summary_l2, branch, is_head, diff_patch,
                                          created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(tool_id) DO UPDATE SET
                           summary = excluded.summary,
                           definition_json = excluded.definition_json,
                           risk_level = excluded.risk_level,
                           required_permissions = excluded.required_permissions,
                           lineage_id = excluded.lineage_id,
                           summary_l2 = excluded.summary_l2,
                           updated_at = excluded.updated_at
                    """,
                    (
                        tool_id, entry.tool_name, version, parent_id,
                        entry.provider_id, summary, defn_json,
                        getattr(entry.definition, 'language', 'python'),
                        getattr(entry.definition, 'runtime', 'python'),
                        1 if entry.essential else 0,
                        risk_level, json.dumps(req_perms, ensure_ascii=False), lineage_id,
                        summary_l2, "main", 1, "",
                        now, now,
                    ),
                )

                # 获取稳定的 id (rowid)
                row = cursor.execute(
                    "SELECT id FROM tools WHERE tool_id = ?", (tool_id,),
                ).fetchone()
                row_id = row[0]

                # 维护同 lineage main 分支唯一 head (版本树语义)
                cursor.execute(
                    """UPDATE tools SET is_head = 0
                       WHERE tool_name = ? AND branch = 'main' AND tool_id != ?""",
                    (entry.tool_name, tool_id),
                )

                # 更新向量 (L1 摘要向量; L2 在 update_embedding/rebuild 时生成)
                if entry.embedding:
                    self._write_vec(cursor, row_id, tool_id, entry.tool_name, entry.embedding, now)

                conn.commit()
                return row_id
            except Exception:
                conn.rollback()
                raise

        row_id = await asyncio.to_thread(_upsert)
        logger.debug("ToolStore: upserted '%s' (id=%s, row_id=%s)", entry.tool_name, tool_id, row_id)
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
                              risk_level, required_permissions, lineage_id, summary_l2,
                              created_at, updated_at
                       FROM tools WHERE tool_id = ?""",
                    (tool_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT tool_id, tool_name, version, parent_version_id, provider_id,
                              summary, definition_json, language, runtime, essential,
                              risk_level, required_permissions, lineage_id, summary_l2,
                              created_at, updated_at
                       FROM tools WHERE tool_name = ?
                       ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                    (tool_name,),
                ).fetchone()

            if row is None:
                return None

            return self._row_to_entry(row, conn)

        return await asyncio.to_thread(_get)

    async def get_latest_version(self, tool_name: str) -> ToolEntry | None:
        """获取工具的最新版本"""
        return await self.get_tool(tool_name, version=None)

    async def get_root_version(self, lineage_id: str) -> ToolEntry | None:
        """获取版本链的初始提交 (root — lineage 中最早创建的版本)

        CallPathRouter SKILL 来源解析用: Skill 绑定的 Tool 原始名称
        对应 root 提交语义 (P1 按 lineage_id 直查; P2 接入 SkillStore
        绑定关系后同样落到此方法)。

        Args:
            lineage_id: 版本链标识 (通常 = tool_name)

        Returns:
            初始提交的 ToolEntry，lineage 不存在时返回 None
        """
        conn = self._ensure_conn()

        def _get():
            row = conn.execute(
                """SELECT tool_id, tool_name, version, parent_version_id, provider_id,
                          summary, definition_json, language, runtime, essential,
                          risk_level, required_permissions, lineage_id, summary_l2,
                          created_at, updated_at
                   FROM tools WHERE lineage_id = ?
                   ORDER BY created_at ASC, rowid ASC LIMIT 1""",
                (lineage_id,),
            ).fetchone()
            if row is None:
                # 兜底: lineage_id 未回填的行按 tool_name 查最早版本
                row = conn.execute(
                    """SELECT tool_id, tool_name, version, parent_version_id, provider_id,
                              summary, definition_json, language, runtime, essential,
                              risk_level, required_permissions, lineage_id, summary_l2,
                              created_at, updated_at
                       FROM tools WHERE tool_name = ?
                       ORDER BY created_at ASC, rowid ASC LIMIT 1""",
                    (lineage_id,),
                ).fetchone()
            if row is None:
                return None
            return self._row_to_entry(row, conn)

        return await asyncio.to_thread(_get)

    async def get_head_version(self, lineage_id: str) -> ToolEntry | None:
        """获取版本链的最新版本 (head — lineage 中最晚创建的版本)

        CallPathRouter CROSS_DOMAIN 来源解析用: 跨域调用解析到
        对方域当前 head (与 get_latest_version 语义一致, 以 lineage 为键)。

        Args:
            lineage_id: 版本链标识 (通常 = tool_name)

        Returns:
            head 版本的 ToolEntry，lineage 不存在时返回 None
        """
        conn = self._ensure_conn()

        def _get():
            row = conn.execute(
                """SELECT tool_id, tool_name, version, parent_version_id, provider_id,
                          summary, definition_json, language, runtime, essential,
                          risk_level, required_permissions, lineage_id, summary_l2,
                          created_at, updated_at
                   FROM tools WHERE lineage_id = ?
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (lineage_id,),
            ).fetchone()
            if row is None:
                # 兜底: lineage_id 未回填的行按 tool_name 查最新版本
                row = conn.execute(
                    """SELECT tool_id, tool_name, version, parent_version_id, provider_id,
                              summary, definition_json, language, runtime, essential,
                              risk_level, required_permissions, lineage_id, summary_l2,
                              created_at, updated_at
                       FROM tools WHERE tool_name = ?
                       ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                    (lineage_id,),
                ).fetchone()
            if row is None:
                return None
            return self._row_to_entry(row, conn)

        return await asyncio.to_thread(_get)

    async def list_tools(self) -> list[ToolEntry]:
        """列出所有工具 (每个工具仅返回最新版本)"""
        conn = self._ensure_conn()

        def _list():
            # 使用子查询获取每个工具的最新版本
            rows = conn.execute(
                """SELECT t.tool_id, t.tool_name, t.version, t.parent_version_id,
                          t.provider_id, t.summary, t.definition_json,
                          t.language, t.runtime, t.essential,
                          t.risk_level, t.required_permissions, t.lineage_id, t.summary_l2,
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
                    # version 列精确匹配: 覆盖 main ("{name}@{v}")
                    # 与分支 ("{name}@{branch}/{v}") 两种 tool_id
                    tool_ids = [r[0] for r in conn.execute(
                        """SELECT tool_id FROM tools
                           WHERE tool_name = ? AND version = ?""",
                        (tool_name, version),
                    ).fetchall()]
                    if not tool_ids:
                        return False
                else:
                    # 删除所有版本 (含全部分支)
                    tool_ids = [r[0] for r in conn.execute(
                        "SELECT tool_id FROM tools WHERE tool_name = ?", (tool_name,),
                    ).fetchall()]

                for tid in tool_ids:
                    self._delete_vec_for_tool(conn, tid)
                    conn.execute("DELETE FROM tool_changelogs WHERE tool_id = ?", (tid,))
                    conn.execute("DELETE FROM tool_tags WHERE tool_id = ?", (tid,))
                    conn.execute("DELETE FROM tool_aliases WHERE tool_id = ?", (tid,))
                    conn.execute(
                        "DELETE FROM tool_dependencies WHERE tool_id = ? OR depends_on_tool_id = ?",
                        (tid, tid),
                    )
                    conn.execute("DELETE FROM tools WHERE tool_id = ?", (tid,))

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
        branch: str = "main",
    ) -> str:
        """创建工具新版本 (从指定分支 head 分叉)

        自增版本号，写入新 tools 行 (含与 parent 的结构化 diff_patch)
        + 双层向量; 维护同 branch 唯一 is_head。

        tool_id 格式: main 分支 "{tool}@{version}" (兼容旧格式);
        非 main 分支 "{tool}@{branch}/{version}" 避免撞号。

        Args:
            tool_name: 工具名称
            new_definition: 新的工具定义
            changelog: 版本变更说明 (自动挂到新版本行)
            bump: 自增类型 "patch" | "minor" | "major"
            branch: 目标分支 (从该分支 head 分叉; 默认 "main")

        Returns:
            新版本 tool_id
        """
        conn = self._ensure_conn()
        now = datetime.utcnow().isoformat()

        # 预计算新版本 L1/L2 摘要 (SummaryGenerator 可插拔)
        new_l1 = new_definition.description[:80]
        new_l2 = await self._summary_generator.generate_l2(tool_name, new_l1)

        def _create():
            # 获取指定分支的 head 版本
            row = conn.execute(
                """SELECT tool_id, version, definition_json FROM tools
                   WHERE tool_name = ? AND branch = ?
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (tool_name, branch),
            ).fetchone()

            if row is None:
                raise ValueError(f"Tool '{tool_name}' not found in store")

            old_tool_id, old_version, old_defn_json = row
            new_version = bump_version(old_version, bump)
            if branch == "main":
                new_tool_id = f"{tool_name}@{new_version}"
            else:
                new_tool_id = f"{tool_name}@{branch}/{new_version}"
            defn_json = new_definition.model_dump_json()
            # 与 parent 的结构化 diff (added/removed/changed 三段)
            try:
                diff_patch = json.dumps(
                    _dict_diff(json.loads(old_defn_json), json.loads(defn_json)),
                    ensure_ascii=False,
                )
            except (json.JSONDecodeError, TypeError):
                diff_patch = ""
            # 新版本风险位随 definition 版本化; lineage_id 继承父版本行
            new_risk = getattr(new_definition, 'risk_level', RiskLevel.LOW) or RiskLevel.LOW
            new_perms = getattr(new_definition, 'required_permissions', None) or []

            cursor = conn.cursor()
            try:
                cursor.execute(
                    """INSERT INTO tools (tool_id, tool_name, version, parent_version_id,
                                          provider_id, summary, definition_json,
                                          language, runtime, essential,
                                          risk_level, required_permissions, lineage_id,
                                          summary_l2, branch, is_head, diff_patch,
                                          created_at, updated_at)
                       SELECT ?, ?, ?, ?, provider_id, ?, ?,
                              ?, ?, essential, ?, ?, lineage_id, ?, ?, 1, ?, ?, ?
                       FROM tools WHERE tool_id = ?
                    """,
                    (
                        new_tool_id, tool_name, new_version, old_tool_id,
                        new_l1, defn_json,
                        getattr(new_definition, 'language', 'python'),
                        getattr(new_definition, 'runtime', 'python'),
                        new_risk, json.dumps(new_perms, ensure_ascii=False),
                        new_l2, branch, diff_patch, now, now, old_tool_id,
                    ),
                )

                # 新版本成为该分支唯一 head (版本树语义)
                cursor.execute(
                    """UPDATE tools SET is_head = 0
                       WHERE tool_name = ? AND branch = ? AND tool_id != ?""",
                    (tool_name, branch, new_tool_id),
                )

                conn.commit()
                # 获取新行的稳定 id
                row = cursor.execute(
                    "SELECT id FROM tools WHERE tool_id = ?", (new_tool_id,),
                ).fetchone()
                new_row_id = row[0] if row else None
            except Exception:
                conn.rollback()
                raise

            return new_tool_id, new_version, old_tool_id, new_row_id

        new_tool_id, new_version, old_tool_id, new_row_id = await asyncio.to_thread(_create)

        # 异步生成双层向量 (L1 摘要 + L2 摘要的摘要)
        await self._embed_tool_row(new_row_id, new_tool_id, tool_name, new_l1, new_l2)

        # 写入 changelog (直接挂到新版本行, 避免跨分支错挂)
        if changelog:
            def _log():
                conn.execute(
                    """INSERT INTO tool_changelogs (tool_id, change_type, description,
                                                    created_at, source)
                       VALUES (?, 'version_update', ?, ?, 'system')""",
                    (new_tool_id, changelog, now),
                )
                conn.commit()
            await asyncio.to_thread(_log)

        logger.info("ToolStore: created version %s for '%s' (id=%s, branch=%s)",
                     new_version, tool_name, new_tool_id, branch)
        return new_tool_id

    async def create_branch(
        self, tool_name: str, branch_name: str, from_version: str | None = None,
    ) -> str:
        """创建分支 — 从指定版本分叉 (Git 版本树)

        在分支起点插入一行快照 (definition = 分叉点内容, parent = 分叉点
        tool_id, branch = branch_name); 后续 create_version(branch=...) 从
        该快照派生。非 main 分支的 tool_id 格式为 "{tool}@{branch}/{version}"。

        Args:
            tool_name: 工具名称
            branch_name: 新分支名 (不可为 "main", 不可含 "/" 或 "@")
            from_version: 分叉点版本号 (None = main 分支当前 head)

        Returns:
            分支起点行的 tool_id

        Raises:
            ValueError: 工具/分叉点不存在 / 分支名非法 / 分支已存在
        """
        if branch_name == "main":
            raise ValueError("branch name 'main' is reserved")
        if "/" in branch_name or "@" in branch_name:
            raise ValueError(f"invalid branch name: {branch_name!r}")

        conn = self._ensure_conn()
        now = datetime.utcnow().isoformat()

        # 快照内容与分叉点一致 → 空 diff
        empty_diff = json.dumps(
            {"added": {}, "removed": {}, "changed": {}}, ensure_ascii=False,
        )

        def _fork():
            if from_version:
                row = conn.execute(
                    """SELECT tool_id, version, summary, summary_l2 FROM tools
                       WHERE tool_name = ? AND version = ? AND branch = 'main'""",
                    (tool_name, from_version),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT tool_id, version, summary, summary_l2 FROM tools
                       WHERE tool_name = ? AND branch = 'main'
                       ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                    (tool_name,),
                ).fetchone()
            if row is None:
                raise ValueError(
                    f"fork point for '{tool_name}'"
                    f"{'@' + from_version if from_version else ''} not found"
                )
            base_tool_id, base_version, base_l1, base_l2 = row

            branch_tool_id = f"{tool_name}@{branch_name}/{base_version}"
            exists = conn.execute(
                "SELECT 1 FROM tools WHERE tool_id = ?", (branch_tool_id,),
            ).fetchone()
            if exists:
                raise ValueError(
                    f"branch '{branch_name}' already exists for '{tool_name}'"
                )

            cursor = conn.cursor()
            try:
                cursor.execute(
                    """INSERT INTO tools (tool_id, tool_name, version, parent_version_id,
                                          provider_id, summary, definition_json,
                                          language, runtime, essential,
                                          risk_level, required_permissions, lineage_id,
                                          summary_l2, branch, is_head, diff_patch,
                                          created_at, updated_at)
                       SELECT ?, tool_name, version, ?, provider_id, summary,
                              definition_json, language, runtime, essential,
                              risk_level, required_permissions, lineage_id,
                              summary_l2, ?, 1, ?, ?, ?
                       FROM tools WHERE tool_id = ?
                    """,
                    (branch_tool_id, base_tool_id, branch_name,
                     empty_diff, now, now, base_tool_id),
                )
                # 分支起点成为该分支唯一 head
                cursor.execute(
                    """UPDATE tools SET is_head = 0
                       WHERE tool_name = ? AND branch = ? AND tool_id != ?""",
                    (tool_name, branch_name, branch_tool_id),
                )
                conn.commit()
                row2 = cursor.execute(
                    "SELECT id FROM tools WHERE tool_id = ?", (branch_tool_id,),
                ).fetchone()
                return branch_tool_id, (row2[0] if row2 else None), base_l1, base_l2
            except Exception:
                conn.rollback()
                raise

        branch_tool_id, new_row_id, base_l1, base_l2 = await asyncio.to_thread(_fork)

        # 快照复用分叉点的双层摘要生成向量
        await self._embed_tool_row(
            new_row_id, branch_tool_id, tool_name, base_l1, base_l2,
        )
        logger.info("ToolStore: branched '%s' -> '%s' (id=%s)",
                     tool_name, branch_name, branch_tool_id)
        return branch_tool_id

    async def merge_branch(
        self, tool_name: str, source_branch: str, target_branch: str = "main",
    ) -> str:
        """三向合并 source 分支到 target 分支 (Git 版本树)

        base = source 分支首行 parent 指向的分叉点; 仅一方修改的字段
        取修改方, 双方都改且不同的字段抛 VersionConflictError
        (列出冲突路径, 不做自动合并)。合并结果作为 target 分支新版本
        (minor bump) 提交。

        Args:
            tool_name: 工具名称
            source_branch: 被合并分支
            target_branch: 合入分支 (默认 "main")

        Returns:
            合并产生的新版本 tool_id

        Raises:
            ValueError: 分支不存在 / 无法确定 merge base
            VersionConflictError: 字段级冲突
        """
        conn = self._ensure_conn()

        def _collect():
            def _branch_head(branch: str) -> tuple:
                row = conn.execute(
                    """SELECT tool_id, definition_json FROM tools
                       WHERE tool_name = ? AND branch = ?
                       ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                    (tool_name, branch),
                ).fetchone()
                if row is None:
                    raise ValueError(
                        f"branch '{branch}' not found for '{tool_name}'"
                    )
                return row

            source_head_id, source_defn_json = _branch_head(source_branch)
            _target_head_id, target_defn_json = _branch_head(target_branch)

            # merge base = source 分支首行 (分叉快照) 的 parent
            base_row = conn.execute(
                """SELECT parent_version_id FROM tools
                   WHERE tool_name = ? AND branch = ?
                   ORDER BY created_at ASC, rowid ASC LIMIT 1""",
                (tool_name, source_branch),
            ).fetchone()
            base_id = base_row[0] if base_row else None
            if not base_id:
                raise ValueError(
                    f"cannot determine merge base for '{source_branch}'"
                )
            base_row = conn.execute(
                "SELECT definition_json FROM tools WHERE tool_id = ?", (base_id,),
            ).fetchone()
            if base_row is None:
                raise ValueError(f"merge base '{base_id}' not found")
            return base_row[0], target_defn_json, source_defn_json

        base_json, ours_json, theirs_json = await asyncio.to_thread(_collect)

        base = json.loads(base_json)
        ours = json.loads(ours_json)        # target 分支 (合入方)
        theirs = json.loads(theirs_json)    # source 分支 (被合方)
        merged, conflicts = _merge_dicts(base, ours, theirs)
        if conflicts:
            raise VersionConflictError(tool_name, conflicts)

        merged_defn = ToolDefinition.model_validate(merged)
        new_tool_id = await self.create_version(
            tool_name, merged_defn,
            changelog=f"merge branch '{source_branch}' into '{target_branch}'",
            bump="minor", branch=target_branch,
        )
        logger.info("ToolStore: merged '%s' into '%s' for '%s' (id=%s)",
                     source_branch, target_branch, tool_name, new_tool_id)
        return new_tool_id

    async def rollback(self, tool_name: str, to_version: str) -> str:
        """回滚到指定版本 (revert 语义 — Git 版本树)

        不删除任何历史: 以目标版本 definition 在 main 分支创建新版本
        (parent = 当前 head, diff_patch 记录回滚差异, changelog 自动生成)。

        Args:
            tool_name: 工具名称
            to_version: 回滚目标版本号 (main 分支)

        Returns:
            回滚产生的新版本 tool_id

        Raises:
            ValueError: 目标版本不存在
        """
        target = await self.get_tool(tool_name, version=to_version)
        if target is None:
            raise ValueError(
                f"rollback target '{tool_name}@{to_version}' not found"
            )
        new_tool_id = await self.create_version(
            tool_name, target.definition,
            changelog=f"rollback to {to_version}",
        )
        logger.info("ToolStore: rolled back '%s' to %s (id=%s)",
                     tool_name, to_version, new_tool_id)
        return new_tool_id

    async def get_version_tree(self, lineage_id: str) -> dict[str, list[ToolVersion]]:
        """获取版本树 — branch 分组的版本列表 (Git 版本树)

        每个分支内按时间正序; ToolVersion 携带 branch/is_head/diff_patch。
        旧 get_version_chain() 是本树 main 分支的线性视图。

        Args:
            lineage_id: 版本链标识 (通常 = tool_name)

        Returns:
            {branch: [ToolVersion 按时间正序]}; lineage 不存在时返回 {}
        """
        conn = self._ensure_conn()

        def _tree():
            rows = conn.execute(
                """SELECT tool_id, version, parent_version_id, definition_json,
                          branch, is_head, diff_patch, created_at, updated_at
                   FROM tools WHERE lineage_id = ?
                   ORDER BY created_at ASC, rowid ASC""",
                (lineage_id,),
            ).fetchall()
            if not rows:
                # 兜底: lineage_id 未回填的行按 tool_name 查
                rows = conn.execute(
                    """SELECT tool_id, version, parent_version_id, definition_json,
                              branch, is_head, diff_patch, created_at, updated_at
                       FROM tools WHERE tool_name = ?
                       ORDER BY created_at ASC, rowid ASC""",
                    (lineage_id,),
                ).fetchall()

            tree: dict[str, list[ToolVersion]] = {}
            for (tool_id, version, parent_id, defn_json,
                 branch, is_head, diff_patch, created, _updated) in rows:
                changelogs = conn.execute(
                    "SELECT description FROM tool_changelogs WHERE tool_id = ? ORDER BY created_at",
                    (tool_id,),
                ).fetchall()
                tree.setdefault(branch or "main", []).append(ToolVersion(
                    version=version,
                    parent_version_id=parent_id,
                    definition_json=defn_json,
                    created_at=created,
                    changelog="; ".join(c[0] for c in changelogs),
                    branch=branch or "main",
                    is_head=bool(is_head),
                    diff_patch=diff_patch or "",
                ))
            return tree

        return await asyncio.to_thread(_tree)

    async def get_version_chain(self, tool_name: str) -> list[ToolVersion]:
        """获取工具的完整版本链 (从最新到最旧 — main 分支线性视图)

        仅返回 main 分支的版本 (Git 版本树的 main 线性投影);
        全分支视图见 get_version_tree()。

        Args:
            tool_name: 工具名称

        Returns:
            ToolVersion 列表 (按时间倒序)
        """
        conn = self._ensure_conn()

        def _get():
            rows = conn.execute(
                """SELECT tool_id, version, parent_version_id, definition_json,
                          branch, is_head, diff_patch, created_at, updated_at
                   FROM tools WHERE tool_name = ? AND branch = 'main'
                   ORDER BY created_at DESC, rowid DESC""",
                (tool_name,),
            ).fetchall()

            chain = []
            for row in rows:
                (tool_id, version, parent_id, defn_json,
                 branch, is_head, diff_patch, created, _updated) = row
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
                    branch=branch or "main",
                    is_head=bool(is_head),
                    diff_patch=diff_patch or "",
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
                "SELECT tool_id FROM tools WHERE tool_name = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
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
        """为指定工具生成/更新双层向量 (L1 摘要 + L2 摘要的摘要)

        L1 嵌入文本 = "{name}: {summary}";
        L2 嵌入文本 = "{name}: {summary_l2}" (未生成时跳过)。

        Args:
            tool_name: 工具名称
        """
        if not self._embedding_client:
            return

        conn = self._ensure_conn()

        def _get():
            row = conn.execute(
                """SELECT id, tool_id, summary, summary_l2 FROM tools
                   WHERE tool_name = ? ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (tool_name,),
            ).fetchone()
            return row

        row = await asyncio.to_thread(_get)
        if row is None:
            return

        row_id, tool_id, summary, summary_l2 = row

        try:
            # L1: 摘要向量
            l1_vec = await self._embedding_client.embed_one(f"{tool_name}: {summary or ''}")
            await self._write_vec_async(
                row_id, tool_id, tool_name, l1_vec, level="l1",
            )
            # L2: 摘要的摘要向量 (薄层初筛索引)
            if summary_l2:
                l2_vec = await self._embedding_client.embed_one(
                    f"{tool_name}: {summary_l2}",
                )
                await self._write_vec_async(
                    row_id, tool_id, tool_name, l2_vec, level="l2",
                )
        except Exception as exc:
            logger.warning("ToolStore: embedding failed for '%s': %s", tool_name, exc)

    async def _embed_tool_row(
        self, row_id: int | None, tool_id: str, tool_name: str,
        summary_l1: str, summary_l2: str,
    ) -> None:
        """为新版本行生成双层向量 (L1 摘要 + L2 摘要的摘要; 失败仅告警)

        create_version / create_branch 共用。
        """
        if not self._embedding_client or row_id is None:
            return
        try:
            l1_vec = await self._embedding_client.embed_one(
                f"{tool_name}: {summary_l1}",
            )
            await self._write_vec_async(
                row_id, tool_id, tool_name, l1_vec, level="l1",
            )
            if summary_l2:
                l2_vec = await self._embedding_client.embed_one(
                    f"{tool_name}: {summary_l2}",
                )
                await self._write_vec_async(
                    row_id, tool_id, tool_name, l2_vec, level="l2",
                )
        except Exception as exc:
            logger.warning("ToolStore: embedding failed for new version: %s", exc)

    async def rebuild_summaries(self) -> int:
        """显式重建全部行的一级/二级摘要 (存量库迁移入口)

        遍历 tools 全部行: 由 SummaryGenerator 重新生成 L1 摘要与
        L2 摘要的摘要, 回填 summary/summary_l2 列; embedding_client
        可用时同步重建双层向量 (L1/L2 各自重嵌入)。

        与惰性迁移 (首次 upsert 时生成) 互补 — 适用于换用 LLM 生成器
        后对存量库的全量覆盖。

        Returns:
            重建的行数
        """
        conn = self._ensure_conn()
        now = datetime.utcnow().isoformat()

        def _fetch():
            return conn.execute(
                """SELECT id, tool_id, tool_name, summary, definition_json
                   FROM tools ORDER BY id""",
            ).fetchall()

        rows = await asyncio.to_thread(_fetch)
        if not rows:
            return 0

        updates: list[tuple[int, str, str, str, str]] = []
        for row_id, tool_id, tool_name, old_summary, defn_json in rows:
            try:
                defn = ToolDefinition.model_validate_json(defn_json)
                description = defn.description
            except Exception:
                description = old_summary or ""
            new_l1 = await self._summary_generator.generate_l1(tool_name, description)
            new_l2 = await self._summary_generator.generate_l2(tool_name, new_l1)
            updates.append((row_id, tool_id, tool_name, new_l1, new_l2))

        def _apply():
            cursor = conn.cursor()
            try:
                for row_id, _tool_id, _name, new_l1, new_l2 in updates:
                    cursor.execute(
                        """UPDATE tools SET summary = ?, summary_l2 = ?, updated_at = ?
                           WHERE id = ?""",
                        (new_l1, new_l2, now, row_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        await asyncio.to_thread(_apply)

        # 重建双层向量 (embedding_client 可用时)
        if self._embedding_client:
            for row_id, tool_id, tool_name, new_l1, new_l2 in updates:
                try:
                    l1_vec = await self._embedding_client.embed_one(
                        f"{tool_name}: {new_l1}",
                    )
                    await self._write_vec_async(
                        row_id, tool_id, tool_name, l1_vec, level="l1",
                    )
                    if new_l2:
                        l2_vec = await self._embedding_client.embed_one(
                            f"{tool_name}: {new_l2}",
                        )
                        await self._write_vec_async(
                            row_id, tool_id, tool_name, l2_vec, level="l2",
                        )
                except Exception as exc:
                    logger.warning(
                        "ToolStore: embedding rebuild failed for '%s': %s",
                        tool_name, exc,
                    )

        logger.info("ToolStore: rebuilt summaries for %d rows", len(updates))
        return len(updates)

    async def _write_vec_async(
        self, row_id: int, tool_id: str, tool_name: str, embedding: list[float],
        level: str = "l1",
    ) -> None:
        """异步写入向量到 vec0 或 vec_tools 表 (指定索引层)"""
        conn = self._ensure_conn()
        now = datetime.utcnow().isoformat()

        def _write():
            cursor = conn.cursor()
            self._write_vec(cursor, row_id, tool_id, tool_name, embedding, now, level=level)
            conn.commit()

        await asyncio.to_thread(_write)

    def _write_vec(
        self,
        cursor: sqlite3.Cursor,
        row_id: int,
        tool_id: str,
        tool_name: str,
        embedding: list[float],
        now: str,
        level: str = "l1",
    ) -> None:
        """写入向量到 vec0 (优先) 或 vec_tools (降级)，指定索引层 l1/l2。

        必须在事务内调用。
        """
        if self._vec_available:
            # vec0 不支持 UPSERT，使用 DELETE + INSERT
            table = "vec_tools_l2_idx" if level == "l2" else "vec_tools_idx"
            norm = normalize_vector(embedding)
            cursor.execute(
                f"DELETE FROM {table} WHERE rowid = ?", (row_id,),
            )
            cursor.execute(
                f"INSERT INTO {table}(rowid, embedding) VALUES (?, ?)",
                (row_id, vec_to_json(norm)),
            )
        else:
            # 降级: 写入 JSON 向量表 (level 区分双层)
            cursor.execute(
                """INSERT INTO vec_tools (tool_id, tool_name, embedding_json, level, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(tool_id, level) DO UPDATE SET
                       embedding_json = excluded.embedding_json,
                       updated_at = excluded.updated_at
                """,
                (tool_id, tool_name, json.dumps(embedding), level, now),
            )

    def _delete_vec_for_tool(self, conn: sqlite3.Connection, tool_id: str) -> None:
        """删除指定工具的所有向量 (双层 vec0 + vec_tools)"""
        if self._vec_available:
            row = conn.execute(
                "SELECT id FROM tools WHERE tool_id = ?", (tool_id,),
            ).fetchone()
            if row:
                conn.execute(
                    "DELETE FROM vec_tools_idx WHERE rowid = ?", (row[0],),
                )
                conn.execute(
                    "DELETE FROM vec_tools_l2_idx WHERE rowid = ?", (row[0],),
                )
        # 始终清理降级表 (含 l1/l2 两层)
        conn.execute("DELETE FROM vec_tools WHERE tool_id = ?", (tool_id,))

    async def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.3,
        exclude: set[str] | None = None,
    ) -> list[ToolSearchResult]:
        """语义搜索工具 (向后兼容入口 — 委托 search_cone 默认参数)

        等价于 ``search_cone(query, top_k, min_score, exclude)``，
        不做 tag/风险/lineage 裁剪 (全锥形边界默认开放)。

        Args:
            query: 自然语言查询
            top_k: 返回结果数量
            min_score: 最低相似度阈值
            exclude: 排除的工具名称集合 (用于召回确认闭环)

        Returns:
            ToolSearchResult 列表 (按分数降序)
        """
        return await self.search_cone(
            query, top_k=top_k, min_score=min_score, exclude=exclude,
        )

    async def search_cone(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.3,
        exclude: set[str] | None = None,
        tags: list[str] | None = None,
        risk_ceiling: str = RiskLevel.CRITICAL,
        exclude_lineages: set[str] | None = None,
        index_level: str = "l1",
    ) -> list[ToolSearchResult]:
        """锥形联合检索 — 语义方向 ∩ tag 边界 ∩ 风险边界 (∩ 排除边界)

        与 search() 的区别: 向量 KNN 与业务条件在同一条 SQL 内联合，
        而非先召回后过滤:

        - vec0 路径: KNN 子查询 JOIN tools 表，WHERE 中同时应用
          风险上限 (CASE 映射数值比较)、tag 边界 (tool_tags 子查询)、
          名称/lineage 排除
        - 降级路径: JSON 向量表 + Python 余弦，同一套锥形条件在 SQL 中应用
        - 关键词 fallback: 同一套锥形条件在 SQL 中应用后 Python 打分

        fetch_k 放大 (top_k*3) 补偿 SQL 内过滤损耗。

        三级摘要索引 (P2): index_level 指定检索层 —
        - "l1" (默认): L1 摘要向量层 (与历史行为一致)
        - "l2": L2 摘要的摘要薄层初筛 (大库场景更快);
          命中结果仍携带 L1 摘要文本, L3 schema 仅命中后按需读取;
          L2 层无命中时自动回退 L1 (存量行未生成 L2 向量时兼容)

        Args:
            query: 自然语言查询
            top_k: 返回结果数量
            min_score: 最低相似度阈值
            exclude: 排除的工具名称集合 (用于召回确认闭环)
            tags: tag 边界 (命中任意一个 tag 才可召回; None = 不限)
            risk_ceiling: 风险上限 (risk_level 高于此级别的工具不召回,
                见 youmi.core.tool.RISK_ORDER)
            exclude_lineages: 排除的版本链集合 (GitLineageGuard 去重用)
            index_level: 向量索引层 ("l1" 摘要层 / "l2" 摘要的摘要薄层)

        Returns:
            ToolSearchResult 列表 (按分数降序, 携带风险位/权限位/lineage_id)
        """
        if not self._embedding_client:
            return await self._keyword_search(
                query, top_k, exclude, tags=tags,
                risk_ceiling=risk_ceiling, exclude_lineages=exclude_lineages,
            )

        # 生成查询向量
        try:
            query_vec = await self._embedding_client.embed_one(query)
        except Exception as exc:
            logger.warning("ToolStore: embedding failed, falling back to keyword: %s", exc)
            return await self._keyword_search(
                query, top_k, exclude, tags=tags,
                risk_ceiling=risk_ceiling, exclude_lineages=exclude_lineages,
            )

        conn = self._ensure_conn()

        if self._vec_available:
            return await self._vec0_search(
                conn, query_vec, top_k, min_score, exclude,
                tags=tags, risk_ceiling=risk_ceiling,
                exclude_lineages=exclude_lineages, index_level=index_level,
            )
        else:
            return await self._json_search(
                conn, query_vec, top_k, min_score, exclude,
                tags=tags, risk_ceiling=risk_ceiling,
                exclude_lineages=exclude_lineages, index_level=index_level,
            )

    @staticmethod
    def _cone_where(
        exclude: set[str] | None,
        exclude_lineages: set[str] | None,
        tags: list[str] | None,
        risk_rank_ceiling: int,
    ) -> tuple[list[str], list[Any]]:
        """构造锥形过滤 SQL 片段 (供三条检索路径共用)

        Returns:
            (WHERE 子句列表, 参数列表) — 调用方用 " AND ".join() 拼接
        """
        clauses: list[str] = []
        params: list[Any] = []

        # 风险边界: 级别映射为数值后与上限比较 (未知级别按 low=0)
        clauses.append(
            "(CASE t.risk_level WHEN 'medium' THEN 1 WHEN 'high' THEN 2 "
            "WHEN 'critical' THEN 3 ELSE 0 END) <= ?"
        )
        params.append(risk_rank_ceiling)

        if exclude:
            placeholders = ",".join("?" for _ in exclude)
            clauses.append(f"t.tool_name NOT IN ({placeholders})")
            params.extend(sorted(exclude))

        if exclude_lineages:
            placeholders = ",".join("?" for _ in exclude_lineages)
            clauses.append(
                f"(t.lineage_id IS NULL OR t.lineage_id = '' "
                f"OR t.lineage_id NOT IN ({placeholders}))"
            )
            params.extend(sorted(exclude_lineages))

        if tags:
            placeholders = ",".join("?" for _ in tags)
            clauses.append(
                f"t.tool_id IN (SELECT tool_id FROM tool_tags WHERE tag IN ({placeholders}))"
            )
            params.extend(tags)

        return clauses, params

    @staticmethod
    def _make_search_result(
        tool_name: str,
        defn_json: str,
        summary: str | None,
        score: float,
        risk_level: str | None,
        required_permissions_json: str | None,
        lineage_id: str | None,
    ) -> "ToolSearchResult | None":
        """数据库行 → ToolSearchResult (携带锥形元数据, 解析失败返回 None)"""
        from youmi.mcp.vault import ToolSearchResult

        try:
            defn = ToolDefinition.model_validate_json(defn_json)
        except Exception:
            logger.warning("ToolStore: failed to parse definition for '%s'", tool_name)
            return None

        try:
            perms = json.loads(required_permissions_json or "[]")
            if not isinstance(perms, list):
                perms = []
        except (json.JSONDecodeError, TypeError):
            perms = []

        return ToolSearchResult(
            tool_name=tool_name,
            definition=defn,
            score=score,
            summary=summary or defn.description[:80],
            risk_level=risk_level or RiskLevel.LOW,
            required_permissions=perms,
            lineage_id=lineage_id or tool_name,
        )

    async def _vec0_search(
        self,
        conn: sqlite3.Connection,
        query_vec: list[float],
        top_k: int,
        min_score: float,
        exclude: set[str] | None,
        tags: list[str] | None = None,
        risk_ceiling: str = RiskLevel.CRITICAL,
        exclude_lineages: set[str] | None = None,
        index_level: str = "l1",
    ) -> list[ToolSearchResult]:
        """sqlite-vec vec0 锥形搜索 — KNN 子查询 JOIN tools + 联合 WHERE

        vec0 虚拟表的 KNN 查询要求 WHERE 仅含 MATCH 和 k 约束，
        因此 KNN 作为子查询先取 (rowid, distance)，再 JOIN 业务表
        应用锥形条件 (风险/tag/排除)，语义方向与边界在一条 SQL 内联合。

        index_level="l2" 时 KNN 走 vec_tools_l2_idx 薄层; 无命中
        (如存量行未生成 L2 向量) 自动回退 L1 层。
        """
        norm_query = normalize_vector(query_vec)
        # 多取一些以便锥形过滤后仍有足够结果
        has_filters = bool(exclude or exclude_lineages or tags or risk_ceiling != RiskLevel.CRITICAL)
        fetch_k = top_k * 3 if has_filters else top_k

        cone_clauses, cone_params = self._cone_where(
            exclude, exclude_lineages, tags, risk_rank(risk_ceiling),
        )
        cone_sql = " AND ".join(cone_clauses)

        vec_table = "vec_tools_l2_idx" if index_level == "l2" else "vec_tools_idx"

        def _search():
            rows = conn.execute(
                f"""SELECT t.tool_id, t.tool_name, t.definition_json, t.summary,
                           t.risk_level, t.required_permissions, t.lineage_id,
                           v.distance
                    FROM (SELECT rowid, distance FROM {vec_table}
                          WHERE embedding MATCH ? AND k = ?) v
                    JOIN tools t ON t.id = v.rowid
                    WHERE {cone_sql}
                    ORDER BY v.distance""",
                (vec_to_json(norm_query), fetch_k, *cone_params),
            ).fetchall()
            return rows

        rows = await asyncio.to_thread(_search)

        # L2 薄层无命中 (存量行未生成 L2 向量) → 回退 L1 层
        if not rows and index_level == "l2":
            logger.debug("ToolStore: L2 index empty, falling back to L1")
            return await self._vec0_search(
                conn, query_vec, top_k, min_score, exclude,
                tags=tags, risk_ceiling=risk_ceiling,
                exclude_lineages=exclude_lineages, index_level="l1",
            )

        results: list[ToolSearchResult] = []
        for (tool_id, tool_name, defn_json, summary,
             risk_level, perms_json, lineage_id, distance) in rows:
            score = l2_to_cosine(distance)
            if score < min_score:
                continue
            result = self._make_search_result(
                tool_name, defn_json, summary, score,
                risk_level, perms_json, lineage_id,
            )
            if result is not None:
                results.append(result)

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    async def _json_search(
        self,
        conn: sqlite3.Connection,
        query_vec: list[float],
        top_k: int,
        min_score: float,
        exclude: set[str] | None,
        tags: list[str] | None = None,
        risk_ceiling: str = RiskLevel.CRITICAL,
        exclude_lineages: set[str] | None = None,
        index_level: str = "l1",
    ) -> list[ToolSearchResult]:
        """降级: JSON 向量表 + Python 余弦 + 同一锥形条件 (SQL 内应用)

        vec_tools.level 列区分 L1/L2 双层; L2 无命中时回退 L1。
        """
        cone_clauses, cone_params = self._cone_where(
            exclude, exclude_lineages, tags, risk_rank(risk_ceiling),
        )
        cone_sql = " AND ".join(cone_clauses)
        level = "l2" if index_level == "l2" else "l1"

        def _search():
            rows = conn.execute(
                f"""SELECT v.tool_id, v.tool_name, v.embedding_json,
                           t.definition_json, t.summary,
                           t.risk_level, t.required_permissions, t.lineage_id
                    FROM vec_tools v
                    JOIN tools t ON v.tool_id = t.tool_id
                    WHERE v.level = ? AND {cone_sql}""",
                (level, *cone_params),
            ).fetchall()
            return rows

        rows = await asyncio.to_thread(_search)

        # L2 薄层无命中 → 回退 L1 层
        if not rows and level == "l2":
            logger.debug("ToolStore: L2 fallback rows empty, falling back to L1")
            return await self._json_search(
                conn, query_vec, top_k, min_score, exclude,
                tags=tags, risk_ceiling=risk_ceiling,
                exclude_lineages=exclude_lineages, index_level="l1",
            )

        if not rows:
            return []

        results: list[ToolSearchResult] = []
        for (tool_id, tool_name, emb_json, defn_json, summary,
             risk_level, perms_json, lineage_id) in rows:
            embedding = json.loads(emb_json)
            score = cosine_similarity_python(query_vec, embedding)
            if score < min_score:
                continue
            result = self._make_search_result(
                tool_name, defn_json, summary, score,
                risk_level, perms_json, lineage_id,
            )
            if result is not None:
                results.append(result)

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    async def _keyword_search(
        self,
        query: str,
        top_k: int,
        exclude: set[str] | None = None,
        tags: list[str] | None = None,
        risk_ceiling: str = RiskLevel.CRITICAL,
        exclude_lineages: set[str] | None = None,
    ) -> list[ToolSearchResult]:
        """关键词匹配 fallback (同一锥形条件在 SQL 中应用后 Python 打分)"""
        conn = self._ensure_conn()
        query_lower = query.lower()
        query_tokens = set(query_lower.split())
        query_tokens.update(c for c in query_lower if not c.isspace())

        cone_clauses, cone_params = self._cone_where(
            exclude, exclude_lineages, tags, risk_rank(risk_ceiling),
        )
        cone_sql = " AND ".join(cone_clauses)

        def _search():
            rows = conn.execute(
                f"""SELECT t.tool_name, t.definition_json, t.summary,
                           t.risk_level, t.required_permissions, t.lineage_id
                    FROM tools t
                    INNER JOIN (
                        SELECT tool_name, MAX(created_at) as max_created
                        FROM tools GROUP BY tool_name
                    ) latest ON t.tool_name = latest.tool_name
                            AND t.created_at = latest.max_created
                    WHERE {cone_sql}""",
                cone_params,
            ).fetchall()
            return rows

        rows = await asyncio.to_thread(_search)

        scored: list[ToolSearchResult] = []
        for tool_name, defn_json, summary, risk_level, perms_json, lineage_id in rows:
            defn = ToolDefinition.model_validate_json(defn_json)
            text = f"{tool_name} {defn.description} {summary or ''}".lower()
            text_tokens = set(text.split())
            text_tokens.update(c for c in text if not c.isspace())
            overlap = query_tokens & text_tokens
            score = len(overlap) / max(len(query_tokens), 1)

            if score > 0:
                result = self._make_search_result(
                    tool_name, defn_json, summary, min(score, 1.0),
                    risk_level, perms_json, lineage_id,
                )
                if result is not None:
                    scored.append(result)

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
                          risk_level, required_permissions, lineage_id, summary_l2,
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
                "SELECT tool_id FROM tools WHERE tool_name = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
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
                          t.risk_level, t.required_permissions, t.lineage_id, t.summary_l2,
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
                "SELECT tool_id FROM tools WHERE tool_name = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (tool_name,),
            ).fetchone()
            dst = conn.execute(
                "SELECT tool_id FROM tools WHERE tool_name = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
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

    def _row_to_entry(self, row: tuple, conn: sqlite3.Connection) -> ToolEntry | None:
        """将数据库行转换为 ToolEntry"""
        from youmi.mcp.vault import ToolEntry, ToolContextTier

        if row is None:
            return None

        (tool_id, tool_name, version, parent_version_id, provider_id,
         summary, defn_json, language, runtime, essential,
         risk_level, required_permissions_json, lineage_id, summary_l2,
         created_at, updated_at) = row

        try:
            defn = ToolDefinition.model_validate_json(defn_json)
        except Exception:
            logger.warning("ToolStore: failed to parse definition for '%s'", tool_name)
            return None

        try:
            required_permissions = json.loads(required_permissions_json or "[]")
            if not isinstance(required_permissions, list):
                required_permissions = []
        except (json.JSONDecodeError, TypeError):
            required_permissions = []

        # 读取向量
        embedding: list[float] = []
        if self._vec_available:
            # 从 vec0 读取: 通过 tool_id 查找主表 id，再查 vec0
            id_row = conn.execute(
                "SELECT id FROM tools WHERE tool_id = ?", (tool_id,),
            ).fetchone()
            if id_row:
                vec_row = conn.execute(
                    "SELECT vec_to_json(embedding) FROM vec_tools_idx WHERE rowid = ?",
                    (id_row[0],),
                ).fetchone()
                if vec_row:
                    try:
                        embedding = json.loads(vec_row[0])
                    except (json.JSONDecodeError, TypeError):
                        pass
        else:
            # 降级: 从 vec_tools JSON 表读取
            vec_row = conn.execute(
                "SELECT embedding_json FROM vec_tools WHERE tool_id = ?",
                (tool_id,),
            ).fetchone()
            if vec_row:
                try:
                    embedding = json.loads(vec_row[0])
                except (json.JSONDecodeError, TypeError):
                    pass

        l1_summary = summary or defn.description[:80]
        entry = ToolEntry(
            tool_name=tool_name,
            definition=defn,
            handler=None,  # handler 不可序列化，加载时需重新绑定
            provider_id=provider_id,
            essential=bool(essential),
            embedding=embedding,
            summary=l1_summary,
            summary_l2=summary_l2 or l1_summary[:30],
            tier=ToolContextTier.COLD,  # 持久化层不存上下文状态
            last_used_turn=-1,
            use_count=0,
            version=version,
            language=language,
            risk_level=risk_level or RiskLevel.LOW,
            required_permissions=required_permissions,
            lineage_id=lineage_id or tool_name,
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
            if self._vec_available:
                vectors = conn.execute("SELECT COUNT(*) FROM vec_tools_idx").fetchone()[0]
            else:
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
