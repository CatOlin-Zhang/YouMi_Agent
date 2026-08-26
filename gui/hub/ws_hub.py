"""WebSocket 连接中心：管理所有浏览器连接并向其广播事件。

设计要点：
- 不做会话级订阅过滤，所有事件都带 ``session_id``，由前端按需过滤。
- 发送失败的连接会被自动剔除，避免污染后续广播。
- ``send`` 用于点对点（如某个 WS 专属的历史回放），``broadcast`` 用于全局事件。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class WebSocketHub:
    """WebSocket 客户端注册表 + 广播器。"""

    def __init__(self) -> None:
        self._clients: set[Any] = set()

    def add(self, ws: Any) -> None:
        self._clients.add(ws)

    def remove(self, ws: Any) -> None:
        self._clients.discard(ws)

    @property
    def count(self) -> int:
        return len(self._clients)

    async def send(self, ws: Any, event: dict) -> None:
        """向单个连接发送事件。"""
        try:
            await ws.send_str(json.dumps(event, ensure_ascii=False))
        except Exception:
            self.remove(ws)

    async def broadcast(self, event: dict) -> None:
        """向所有连接广播事件。"""
        data = json.dumps(event, ensure_ascii=False)
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_str(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.remove(ws)
