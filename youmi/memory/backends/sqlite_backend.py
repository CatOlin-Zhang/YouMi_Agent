"""
SQLite 持久化后端

使用 Python 内置 sqlite3 模块，通过 asyncio.to_thread 实现异步操作。
无需额外依赖 (不依赖 aiosqlite)。

数据表:
- sessions: session_id, agent_id, created_at, updated_at, metadata
- messages: id, session_id, role, content, raw_data, timestamp
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from youmi.memory.backends.base import (
    PersistenceBackend,
    SessionRecord,
    MessageRecord,
)

logger = logging.getLogger(__name__)

# 建表 SQL
_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_sessions_agent ON sessions(agent_id);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    raw_data TEXT DEFAULT '{}',
    timestamp TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
"""


class SQLiteBackend(PersistenceBackend):
    """SQLite 持久化后端

    使用 Python 内置 sqlite3，通过 asyncio.to_thread 实现异步操作。

    Args:
        db_path: SQLite 数据库文件路径。
            默认 ".youmi_sessions.db" (当前工作目录)。
            设为 ":memory:" 可使用内存数据库 (测试用)。

    用法::

        backend = SQLiteBackend(db_path="data/sessions.db")
        await backend.initialize()
        await backend.save_session("s1", "a1", [{"role": "user", "content": "hi"}])
    """

    def __init__(self, db_path: str = ".youmi_sessions.db") -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None

    async def initialize(self) -> None:
        """建库建表 (幂等: 已初始化时跳过)"""
        if self._conn is not None:
            return  # 已初始化, 跳过

        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        self._conn = await asyncio.to_thread(
            sqlite3.connect, self._db_path, check_same_thread=False,
        )
        # 启用外键约束
        await asyncio.to_thread(self._conn.execute, "PRAGMA foreign_keys = ON;")
        # 建表
        await asyncio.to_thread(self._conn.executescript, _CREATE_TABLES_SQL)
        await asyncio.to_thread(self._conn.commit)
        logger.debug("SQLiteBackend initialized: %s", self._db_path)

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteBackend not initialized. Call initialize() first.")
        return self._conn

    async def save_session(
        self,
        session_id: str,
        agent_id: str,
        messages: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        conn = self._ensure_conn()
        now = datetime.utcnow().isoformat()

        def _save():
            cursor = conn.cursor()
            try:
                # upsert session 记录
                cursor.execute(
                    """INSERT INTO sessions (session_id, agent_id, created_at, updated_at, metadata)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(session_id) DO UPDATE SET
                           updated_at = excluded.updated_at,
                           metadata = excluded.metadata
                    """,
                    (session_id, agent_id, now, now, json.dumps(metadata or {})),
                )

                # 删除旧消息，写入新消息
                cursor.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
                for msg in messages:
                    record = MessageRecord.from_openai_message(msg)
                    cursor.execute(
                        """INSERT INTO messages (session_id, role, content, raw_data, timestamp)
                           VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            record.role,
                            record.content,
                            json.dumps(record.raw_data, ensure_ascii=False),
                            record.timestamp.isoformat(),
                        ),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        await asyncio.to_thread(_save)

    async def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        conn = self._ensure_conn()

        def _load():
            cursor = conn.execute(
                """SELECT role, content, raw_data, timestamp
                   FROM messages WHERE session_id = ? ORDER BY id ASC""",
                (session_id,),
            )
            rows = cursor.fetchall()
            result = []
            for role, content, raw_data_str, _ts in rows:
                raw_data = json.loads(raw_data_str) if raw_data_str else {}
                record = MessageRecord(
                    role=role, content=content, raw_data=raw_data,
                )
                result.append(record.to_openai_message())
            return result

        return await asyncio.to_thread(_load)

    async def list_sessions(self, agent_id: str) -> list[SessionRecord]:
        conn = self._ensure_conn()

        def _list():
            cursor = conn.execute(
                """SELECT session_id, agent_id, created_at, updated_at, metadata
                   FROM sessions WHERE agent_id = ?
                   ORDER BY updated_at DESC""",
                (agent_id,),
            )
            rows = cursor.fetchall()
            records = []
            for sid, aid, created, updated, meta_str in rows:
                records.append(SessionRecord(
                    session_id=sid,
                    agent_id=aid,
                    created_at=datetime.fromisoformat(created),
                    updated_at=datetime.fromisoformat(updated),
                    metadata=json.loads(meta_str) if meta_str else {},
                ))
            return records

        return await asyncio.to_thread(_list)

    async def delete_session(self, session_id: str) -> None:
        conn = self._ensure_conn()

        def _delete():
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            conn.commit()

        await asyncio.to_thread(_delete)

    async def get_latest_session(self, agent_id: str) -> SessionRecord | None:
        conn = self._ensure_conn()

        def _get():
            cursor = conn.execute(
                """SELECT session_id, agent_id, created_at, updated_at, metadata
                   FROM sessions WHERE agent_id = ?
                   ORDER BY updated_at DESC LIMIT 1""",
                (agent_id,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            sid, aid, created, updated, meta_str = row
            return SessionRecord(
                session_id=sid,
                agent_id=aid,
                created_at=datetime.fromisoformat(created),
                updated_at=datetime.fromisoformat(updated),
                metadata=json.loads(meta_str) if meta_str else {},
            )

        return await asyncio.to_thread(_get)

    async def close(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None
            logger.debug("SQLiteBackend closed: %s", self._db_path)
