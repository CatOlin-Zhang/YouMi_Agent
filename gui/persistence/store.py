"""基于 JSON 文件的轻量持久化。

存储布局（全部位于 gui/data/ 下，符合「代码局限在 gui/」的约束）：
- state.json          ：所有会话 + Agent 卡片
- messages/<sid>.json ：每个会话的消息记录列表

采用「写临时文件 + 原子替换」避免写入中途崩溃导致文件损坏。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from gui.engine.models import AgentCard, MessageRecord, Session

logger = logging.getLogger(__name__)


class Store:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.state_file = os.path.join(data_dir, "state.json")
        self.msg_dir = os.path.join(data_dir, "messages")
        os.makedirs(self.msg_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # 通用原子写
    # ------------------------------------------------------------------
    def _atomic_write(self, path: str, obj: Any) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    # ------------------------------------------------------------------
    # 状态（会话 + 卡片）
    # ------------------------------------------------------------------
    def load_state(self) -> tuple[dict, dict]:
        sessions: dict[str, Session] = {}
        cards: dict[str, AgentCard] = {}
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for s in data.get("sessions", []):
                    sessions[s["session_id"]] = Session.from_dict(s)
                for cid, c in data.get("cards", {}).items():
                    cards[cid] = AgentCard.from_dict(c)
            except Exception as exc:  # pragma: no cover - 损坏文件不致命
                logger.warning("加载 state.json 失败: %s", exc)
        return sessions, cards

    def save_state(self, sessions: dict, cards: dict) -> None:
        data = {
            "sessions": [s.to_dict() for s in sessions.values()],
            "cards": {cid: c.to_dict() for cid, c in cards.items()},
        }
        self._atomic_write(self.state_file, data)

    # ------------------------------------------------------------------
    # 消息
    # ------------------------------------------------------------------
    def load_messages(self, session_id: str) -> list[dict]:
        path = os.path.join(self.msg_dir, f"{session_id}.json")
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def append_message(self, rec: MessageRecord) -> None:
        msgs = self.load_messages(rec.session_id)
        msgs.append(rec.to_dict())
        self._atomic_write(
            os.path.join(self.msg_dir, f"{rec.session_id}.json"), msgs
        )

    def delete_session(self, session_id: str) -> None:
        path = os.path.join(self.msg_dir, f"{session_id}.json")
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
