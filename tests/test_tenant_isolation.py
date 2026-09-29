"""多租户隔离测试 (tenant02)

测试覆盖:
1. KnowledgeEntry tenant 字段默认值
2. GlobalMemory 租户隔离 — clone_for_tenant / 查询过滤 / 聚合统计 / 跨租户保护
3. GlobalMemory 旧库迁移 — 缺 tenant 列自动补齐, 旧数据归 default
4. SQLiteBackend 租户隔离 — 读写过滤 / 归属保留 / 旧库迁移
5. FileBackend 租户隔离 — 读写过滤 / 旧数据兼容
6. MemoryManager 租户传递 — tenant 属性 / save_session / restore_session
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from youmi.knowledge import GlobalMemory, KnowledgeEntry
from youmi.memory import MemoryManager
from youmi.memory.backends import FileBackend, SQLiteBackend, SessionRecord


# =========================================================================
# 辅助工具
# =========================================================================

class MockEmbeddingClient:
    """Mock Embedding 客户端 — 基于字符哈希的伪向量 (32 维)"""

    def __init__(self, dim: int = 32) -> None:
        self._dim = dim

    def _embed_text(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for i, ch in enumerate(text):
            vec[i % self._dim] += ord(ch) % 97
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_text(t) for t in texts]

    async def embed_one(self, text: str) -> list[float]:
        return self._embed_text(text)


def make_legacy_gm_db(path: Path) -> None:
    """构造旧版 GlobalMemory 库 (knowledge_entries 无 tenant 列)"""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE knowledge_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id TEXT UNIQUE NOT NULL,
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
    """)
    now = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO knowledge_entries "
        "(entry_id, category, tool_name, content, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("old_entry_1", "tool_experience", "legacy_tool", "旧库经验", now, now),
    )
    conn.commit()
    conn.close()


def make_legacy_sessions_db(path: Path) -> None:
    """构造旧版 SQLiteBackend 库 (sessions 无 tenant 列)"""
    conn = sqlite3.connect(str(path))
    conn.executescript("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata TEXT DEFAULT '{}'
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            raw_data TEXT DEFAULT '{}',
            timestamp TEXT NOT NULL
        );
    """)
    now = datetime.utcnow().isoformat()
    conn.execute(
        "INSERT INTO sessions (session_id, agent_id, created_at, updated_at, metadata) "
        "VALUES (?, ?, ?, ?, '{}')",
        ("legacy_s1", "a1", now, now),
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, raw_data, timestamp) "
        "VALUES (?, ?, ?, '{}', ?)",
        ("legacy_s1", "user", "旧库消息", now),
    )
    conn.commit()
    conn.close()


def make_legacy_file_backend(base_dir: Path) -> None:
    """构造旧版 FileBackend 数据 (session 无 tenant 字段)"""
    agent_dir = base_dir / "a1"
    agent_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.utcnow().isoformat()
    data = {
        "sessions": {
            "legacy_f1": {
                "session_id": "legacy_f1",
                "agent_id": "a1",
                "created_at": now,
                "updated_at": now,
                "metadata": {},
                "messages": [
                    {"role": "user", "content": "旧文件消息",
                     "raw_data": {}, "timestamp": now},
                ],
            },
        },
    }
    (agent_dir / "sessions.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8",
    )


MSGS = [{"role": "user", "content": "hello"}]


# =========================================================================
# 测试1: KnowledgeEntry tenant 字段
# =========================================================================

async def test_knowledge_entry_tenant_default():
    entry = KnowledgeEntry(tool_name="t", content="c")
    assert entry.tenant == "default"

    entry2 = KnowledgeEntry(tool_name="t", content="c", tenant="acme")
    assert entry2.tenant == "acme"


# =========================================================================
# 测试2: GlobalMemory 租户隔离
# =========================================================================

async def test_global_memory_tenant_isolation():
    memory = GlobalMemory(db_path=":memory:")
    await memory.initialize()
    try:
        assert memory.tenant == "default"

        t1 = memory.clone_for_tenant("t1")
        t2 = memory.clone_for_tenant("t2")

        # clone 共享连接与配置
        assert t1._conn is memory._conn
        assert t1.tenant == "t1"

        await memory.add_experience("tool_a", "default 租户的经验")
        e1 = await t1.add_experience("tool_a", "t1 租户的经验")
        await t2.add_experience("tool_b", "t2 租户的经验")

        # 写入归属正确
        assert e1.tenant == "t1"

        # 列表查询隔离
        default_entries = await memory.list_entries()
        t1_entries = await t1.list_entries()
        t2_entries = await t2.list_entries()
        assert len(default_entries) == 1
        assert default_entries[0].content == "default 租户的经验"
        assert len(t1_entries) == 1
        assert t1_entries[0].content == "t1 租户的经验"
        assert len(t2_entries) == 1

        # get_entry 跨租户不可见
        assert await memory.get_entry(e1.entry_id) is None
        assert await t1.get_entry(e1.entry_id) is not None

        # 聚合查询隔离
        tk_default = await memory.get_tool_knowledge("tool_a")
        assert tk_default.entry_ids == [default_entries[0].entry_id]
        tk_t2 = await t2.get_tool_knowledge("tool_a")
        assert tk_t2.is_empty

        # 关键词检索隔离: default 查询不会召回 t1 的数据
        hits = await memory.search("t1 租户的经验")
        assert all(h.tenant == "default" for h in hits)
        assert all("t1 租户" not in h.content for h in hits)

        # stats 按租户统计
        stats_default = await memory.stats()
        stats_t1 = await t1.stats()
        assert stats_default["total_entries"] == 1
        assert stats_t1["total_entries"] == 1
        assert stats_default["top_tools"] == {"tool_a": 1}
        assert stats_t1["top_tools"] == {"tool_a": 1}
        assert stats_t1["vectorized_entries"] == 0
    finally:
        await memory.close()


async def test_global_memory_tenant_isolation_with_embeddings():
    embedder = MockEmbeddingClient(dim=32)
    memory = GlobalMemory(db_path=":memory:", embedding_client=embedder, embedding_dim=32)
    await memory.initialize()
    try:
        t1 = memory.clone_for_tenant("t1")

        await memory.add_experience("tool_a", "alpha 经验内容")
        await t1.add_experience("tool_b", "beta 经验内容")

        # t1 的向量检索不应召回 default 租户数据
        hits = await t1.search("alpha 经验内容", top_k=5)
        assert all(h.tenant == "t1" for h in hits)
        assert all("alpha" not in h.content for h in hits)

        # default 检索只召回 default
        hits_default = await memory.search("beta 经验内容", top_k=5)
        assert all(h.tenant == "default" for h in hits_default)
    finally:
        await memory.close()


async def test_global_memory_cross_tenant_protection():
    memory = GlobalMemory(db_path=":memory:")
    await memory.initialize()
    try:
        t1 = memory.clone_for_tenant("t1")
        entry = await memory.add_experience("tool_a", "待修复问题")

        # 跨租户 mark_resolved 不可见
        assert await t1.mark_resolved(entry.entry_id, "t1 越权修复") is None
        reloaded = await memory.get_entry(entry.entry_id)
        assert reloaded is not None and reloaded.resolved is False

        # 跨租户 delete 不可删
        assert await t1.delete_entry(entry.entry_id) is False
        assert await memory.get_entry(entry.entry_id) is not None

        # 本租户操作正常
        fixed = await memory.mark_resolved(entry.entry_id, "本租户修复")
        assert fixed is not None and fixed.resolved is True
        assert await memory.delete_entry(entry.entry_id) is True
    finally:
        await memory.close()


async def test_global_memory_batch_add_tenant():
    memory = GlobalMemory(db_path=":memory:")
    await memory.initialize()
    try:
        t3 = memory.clone_for_tenant("t3")

        # 未指定 tenant 的条目自动归属实例租户
        entries = [
            KnowledgeEntry(tool_name="tool_x", content="条目1"),
            KnowledgeEntry(tool_name="tool_x", content="条目2"),
        ]
        await t3.batch_add(entries)
        assert all(e.tenant == "t3" for e in entries)

        assert len(await t3.list_entries()) == 2
        assert len(await memory.list_entries()) == 0
    finally:
        await memory.close()


async def test_global_memory_file_db_multiple_tenants(tmp_path):
    """同一文件库上的两个独立实例 (不同连接) 互不干扰"""
    db_path = str(tmp_path / "gm.db")

    m_default = GlobalMemory(db_path=db_path)
    m_sec = GlobalMemory(db_path=db_path, tenant="sec")
    await m_default.initialize()
    await m_sec.initialize()
    try:
        await m_default.add_experience("tool_a", "default 数据")
        await m_sec.add_experience("tool_a", "sec 数据")

        assert len(await m_default.list_entries()) == 1
        assert (await m_default.list_entries())[0].tenant == "default"
        assert len(await m_sec.list_entries()) == 1
        assert (await m_sec.list_entries())[0].tenant == "sec"
    finally:
        await m_default.close()
        await m_sec.close()


# =========================================================================
# 测试3: GlobalMemory 旧库迁移
# =========================================================================

async def test_global_memory_legacy_migration(tmp_path):
    db_path = tmp_path / "legacy_gm.db"
    make_legacy_gm_db(db_path)

    memory = GlobalMemory(db_path=str(db_path))
    await memory.initialize()
    try:
        # 迁移后旧数据可被 default 租户查询到 (list_entries 引用 tenant 列)
        entries = await memory.list_entries()
        assert len(entries) == 1
        assert entries[0].entry_id == "old_entry_1"
        assert entries[0].tenant == "default"

        # 其他租户不可见
        t9 = memory.clone_for_tenant("t9")
        assert await t9.list_entries() == []

        # tenant 列已补齐
        cols = {
            row[1]
            for row in memory._ensure_conn().execute(
                "PRAGMA table_info(knowledge_entries)"
            )
        }
        assert "tenant" in cols
    finally:
        await memory.close()


# =========================================================================
# 测试4: SQLiteBackend 租户隔离
# =========================================================================

async def test_sqlite_backend_tenant_isolation():
    backend = SQLiteBackend(db_path=":memory:")
    await backend.initialize()
    try:
        await backend.save_session("s1", "a1", MSGS, tenant="t1")
        await backend.save_session("s2", "a1", MSGS, tenant="t2")
        await backend.save_session("s3", "a1", MSGS)  # default

        # 按租户过滤
        t1_sessions = await backend.list_sessions("a1", tenant="t1")
        assert [s.session_id for s in t1_sessions] == ["s1"]
        assert t1_sessions[0].tenant == "t1"

        t2_sessions = await backend.list_sessions("a1", tenant="t2")
        assert [s.session_id for s in t2_sessions] == ["s2"]

        default_sessions = await backend.list_sessions("a1", tenant="default")
        assert [s.session_id for s in default_sessions] == ["s3"]

        # 不过滤 → 全部
        assert len(await backend.list_sessions("a1")) == 3

        # get_latest_session 按租户
        latest_t1 = await backend.get_latest_session("a1", tenant="t1")
        assert latest_t1 is not None and latest_t1.session_id == "s1"
        latest_none = await backend.get_latest_session("a1", tenant="ghost")
        assert latest_none is None

        # 消息加载不受影响
        msgs = await backend.load_messages("s1")
        assert msgs[0]["content"] == "hello"
    finally:
        await backend.close()


async def test_sqlite_backend_tenant_owner_preserved():
    """同一 session 再次保存时保留原租户归属"""
    backend = SQLiteBackend(db_path=":memory:")
    await backend.initialize()
    try:
        await backend.save_session("sx", "a1", MSGS, tenant="t1")
        await backend.save_session("sx", "a1", MSGS, tenant="t2")

        s = await backend.get_latest_session("a1", tenant="t1")
        assert s is not None and s.session_id == "sx"
        assert await backend.get_latest_session("a1", tenant="t2") is None
    finally:
        await backend.close()


async def test_sqlite_backend_legacy_migration(tmp_path):
    db_path = tmp_path / "legacy_sessions.db"
    make_legacy_sessions_db(db_path)

    backend = SQLiteBackend(db_path=str(db_path))
    await backend.initialize()
    try:
        sessions = await backend.list_sessions("a1", tenant="default")
        assert len(sessions) == 1
        assert sessions[0].session_id == "legacy_s1"
        assert sessions[0].tenant == "default"

        assert await backend.list_sessions("a1", tenant="t1") == []

        # 旧消息仍可加载
        msgs = await backend.load_messages("legacy_s1")
        assert msgs[0]["content"] == "旧库消息"

        cols = {
            row[1]
            for row in backend._ensure_conn().execute("PRAGMA table_info(sessions)")
        }
        assert "tenant" in cols
    finally:
        await backend.close()


# =========================================================================
# 测试5: FileBackend 租户隔离
# =========================================================================

async def test_file_backend_tenant_isolation(tmp_path):
    backend = FileBackend(base_dir=str(tmp_path / "fb"))
    await backend.initialize()

    await backend.save_session("f1", "a1", MSGS, tenant="t1")
    await backend.save_session("f2", "a1", MSGS, tenant="t2")

    t1_sessions = await backend.list_sessions("a1", tenant="t1")
    assert [s.session_id for s in t1_sessions] == ["f1"]
    assert t1_sessions[0].tenant == "t1"

    assert len(await backend.list_sessions("a1")) == 2
    assert await backend.list_sessions("a1", tenant="ghost") == []

    latest = await backend.get_latest_session("a1", tenant="t2")
    assert latest is not None and latest.session_id == "f2"

    # 持久化文件中包含 tenant 字段
    raw = json.loads((tmp_path / "fb" / "a1" / "sessions.json").read_text(encoding="utf-8"))
    assert raw["sessions"]["f1"]["tenant"] == "t1"


async def test_file_backend_legacy_compat(tmp_path):
    base = tmp_path / "fb_legacy"
    make_legacy_file_backend(base)

    backend = FileBackend(base_dir=str(base))
    await backend.initialize()

    # 旧数据归 default
    default_sessions = await backend.list_sessions("a1", tenant="default")
    assert len(default_sessions) == 1
    assert default_sessions[0].session_id == "legacy_f1"
    assert default_sessions[0].tenant == "default"

    assert await backend.list_sessions("a1", tenant="t1") == []

    # 旧消息可加载
    msgs = await backend.load_messages("legacy_f1")
    assert msgs[0]["content"] == "旧文件消息"


# =========================================================================
# 测试6: MemoryManager 租户传递
# =========================================================================

async def test_memory_manager_tenant_property():
    mgr = MemoryManager(agent_id="a1", strategy="full", tenant="acme")
    assert mgr.tenant == "acme"

    mgr_default = MemoryManager(agent_id="a1", strategy="full")
    assert mgr_default.tenant == "default"


async def test_memory_manager_save_and_restore_by_tenant():
    backend = SQLiteBackend(db_path=":memory:")

    mgr_t1 = MemoryManager(
        agent_id="a1", strategy="full",
        persistence_backend=backend, tenant="t1",
    )
    await mgr_t1.initialize()
    mgr_t1.start_session("s_t1")
    await mgr_t1.save_session([{"role": "user", "content": "t1 的消息"}])

    # default 租户恢复 → 看不到 t1 的会话
    mgr_default = MemoryManager(
        agent_id="a1", strategy="full",
        persistence_backend=backend, tenant="default",
    )
    assert await mgr_default.restore_session() is None

    # t1 租户恢复 → 命中
    mgr_t1b = MemoryManager(
        agent_id="a1", strategy="full",
        persistence_backend=backend, tenant="t1",
    )
    restored = await mgr_t1b.restore_session()
    assert restored is not None
    assert restored[0]["content"] == "t1 的消息"
    assert mgr_t1b.current_session_id == "s_t1"

    # 持久化记录归属正确
    sessions = await backend.list_sessions("a1", tenant="t1")
    assert [s.session_id for s in sessions] == ["s_t1"]


async def test_memory_manager_session_end_persists_tenant():
    backend = SQLiteBackend(db_path=":memory:")

    mgr = MemoryManager(
        agent_id="a2", strategy="full",
        persistence_backend=backend, tenant="t5",
    )
    await mgr.initialize()
    mgr.start_session("s_t5")
    await mgr.on_message("user", "内容A")
    await mgr.on_session_end()

    sessions = await backend.list_sessions("a2", tenant="t5")
    assert len(sessions) == 1
    assert sessions[0].session_id == "s_t5"

    msgs = await backend.load_messages("s_t5")
    assert any(m["content"] == "内容A" for m in msgs)

    # 其他租户不可见
    assert await backend.list_sessions("a2", tenant="default") == []


async def test_session_record_tenant_default():
    record = SessionRecord(session_id="s", agent_id="a")
    assert record.tenant == "default"
