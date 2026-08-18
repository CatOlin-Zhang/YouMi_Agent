"""
JSON 文件持久化后端

每个 Agent 的 session 数据存储在独立的 JSON 文件中。
轻量实现，适合调试和小规模场景。

文件布局:
    <base_dir>/
    └── <agent_id>/
        └── sessions.json    # 所有 session 元数据 + 消息
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from youmi.memory.backends.base import (
    PersistenceBackend,
    SessionRecord,
    MessageRecord,
)

logger = logging.getLogger(__name__)


class FileBackend(PersistenceBackend):
    """JSON 文件持久化后端

    每个 Agent 对应一个 JSON 文件，存储该 Agent 的所有 session 数据。

    Args:
        base_dir: 存储根目录，默认 ".youmi_sessions"

    数据格式 (sessions.json)::

        {
            "sessions": {
                "<session_id>": {
                    "session_id": "...",
                    "agent_id": "...",
                    "created_at": "ISO-8601",
                    "updated_at": "ISO-8601",
                    "metadata": {},
                    "messages": [
                        {"role": "user", "content": "...", "raw_data": {}, "timestamp": "..."},
                        ...
                    ]
                }
            }
        }
    """

    def __init__(self, base_dir: str = ".youmi_sessions") -> None:
        self._base_dir = Path(base_dir)

    async def initialize(self) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _agent_file(self, agent_id: str) -> Path:
        return self._base_dir / agent_id / "sessions.json"

    async def _read_agent_data(self, agent_id: str) -> dict[str, Any]:
        path = self._agent_file(agent_id)

        def _read():
            if not path.exists():
                return {"sessions": {}}
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)

        return await asyncio.to_thread(_read)

    async def _write_agent_data(self, agent_id: str, data: dict[str, Any]) -> None:
        path = self._agent_file(agent_id)

        def _write():
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)

        await asyncio.to_thread(_write)

    async def save_session(
        self,
        session_id: str,
        agent_id: str,
        messages: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        data = await self._read_agent_data(agent_id)
        now = datetime.utcnow().isoformat()

        # 检查是否已有该 session
        existing = data["sessions"].get(session_id)
        created_at = existing.get("created_at", now) if existing else now

        # 序列化消息
        serialized_messages = []
        for msg in messages:
            record = MessageRecord.from_openai_message(msg)
            serialized_messages.append({
                "role": record.role,
                "content": record.content,
                "raw_data": record.raw_data,
                "timestamp": record.timestamp.isoformat(),
            })

        data["sessions"][session_id] = {
            "session_id": session_id,
            "agent_id": agent_id,
            "created_at": created_at,
            "updated_at": now,
            "metadata": metadata or {},
            "messages": serialized_messages,
        }

        await self._write_agent_data(agent_id, data)

    async def load_messages(self, session_id: str) -> list[dict[str, Any]]:
        # 需要遍历所有 agent 文件查找 session (或按 agent_id 索引)
        # 简单实现: 遍历 base_dir 下所有 agent 目录
        def _find():
            if not self._base_dir.exists():
                return []
            for agent_dir in self._base_dir.iterdir():
                if not agent_dir.is_dir():
                    continue
                sessions_file = agent_dir / "sessions.json"
                if not sessions_file.exists():
                    continue
                with open(sessions_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                sess = data.get("sessions", {}).get(session_id)
                if sess:
                    result = []
                    for m in sess.get("messages", []):
                        record = MessageRecord(
                            role=m["role"],
                            content=m.get("content", ""),
                            raw_data=m.get("raw_data", {}),
                        )
                        result.append(record.to_openai_message())
                    return result
            return []

        return await asyncio.to_thread(_find)

    async def list_sessions(self, agent_id: str) -> list[SessionRecord]:
        data = await self._read_agent_data(agent_id)
        records = []
        for sess in data.get("sessions", {}).values():
            records.append(SessionRecord(
                session_id=sess["session_id"],
                agent_id=sess["agent_id"],
                created_at=datetime.fromisoformat(sess["created_at"]),
                updated_at=datetime.fromisoformat(sess["updated_at"]),
                metadata=sess.get("metadata", {}),
            ))
        records.sort(key=lambda r: r.updated_at, reverse=True)
        return records

    async def delete_session(self, session_id: str) -> None:
        def _delete():
            if not self._base_dir.exists():
                return
            for agent_dir in self._base_dir.iterdir():
                if not agent_dir.is_dir():
                    continue
                sessions_file = agent_dir / "sessions.json"
                if not sessions_file.exists():
                    continue
                with open(sessions_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if session_id in data.get("sessions", {}):
                    del data["sessions"][session_id]
                    with open(sessions_file, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
                    return

        await asyncio.to_thread(_delete)

    async def get_latest_session(self, agent_id: str) -> SessionRecord | None:
        sessions = await self.list_sessions(agent_id)
        return sessions[0] if sessions else None
