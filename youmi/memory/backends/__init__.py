"""
Session 持久化后端

参考 OpenClaw 的 SQLite session transcript 机制，
为 Agent 提供跨重启的对话持久化能力。

架构:
- PersistenceBackend (ABC): 统一接口
- SQLiteBackend: SQLite 异步实现 (推荐，支持并发安全)
- FileBackend: JSON 文件实现 (轻量，适合调试)

数据模型:
- Session: session_id + agent_id + created_at + updated_at
- Message: session_id + role + content + raw_data (JSON) + timestamp
"""

from youmi.memory.backends.base import (
    PersistenceBackend,
    SessionRecord,
    MessageRecord,
)
from youmi.memory.backends.sqlite_backend import SQLiteBackend
from youmi.memory.backends.file_backend import FileBackend

__all__ = [
    "PersistenceBackend",
    "SessionRecord",
    "MessageRecord",
    "SQLiteBackend",
    "FileBackend",
]
