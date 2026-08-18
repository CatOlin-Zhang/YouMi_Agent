"""记忆系统"""

from youmi.memory.base import (
    LongTermBackend,
    MemoryAdapter,
    MemoryEntry,
    ShortTermBackend,
    InMemoryShortTermBackend,
    InMemoryLongTermBackend,
)
from youmi.memory.memory import (
    MemoryManager,
    MemoryStrategy,
    FullMemoryStrategy,
    SummaryMemoryStrategy,
    LSTMMemoryStrategy,
    create_strategy,
    list_strategies,
    register_strategy,
    LLMCallFn,
)

# P0: Compaction + Persistence
from youmi.memory.compaction import ContextCompactor
from youmi.memory.backends.base import PersistenceBackend
from youmi.memory.backends.sqlite_backend import SQLiteBackend
from youmi.memory.backends.file_backend import FileBackend

__all__ = [
    # 底层存储 (兼容旧接口)
    "LongTermBackend",
    "MemoryAdapter",
    "MemoryEntry",
    "ShortTermBackend",
    "InMemoryShortTermBackend",
    "InMemoryLongTermBackend",
    # 策略系统 (新接口)
    "MemoryManager",
    "MemoryStrategy",
    "FullMemoryStrategy",
    "SummaryMemoryStrategy",
    "LSTMMemoryStrategy",
    "create_strategy",
    "list_strategies",
    "register_strategy",
    "LLMCallFn",
    # P0: 上下文压缩
    "ContextCompactor",
    # P0: Session 持久化后端
    "PersistenceBackend",
    "SQLiteBackend",
    "FileBackend",
]
