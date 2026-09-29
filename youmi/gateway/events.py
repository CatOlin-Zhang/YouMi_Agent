"""
任务事件中心 (gateway.events)

内存发布-订阅：worker / service 在每个任务状态变更时发布事件，
API 层（SSE / WebSocket）通过 ``subscribe(task_id)`` 消费事件流。

语义约定：
- 事件为普通 dict，统一含 ``type`` / ``task_id`` / ``ts`` 字段
- ``close(task_id)`` 后订阅流自然结束；对已关闭任务的新订阅得到空流
  （不会永久挂起）
- 同一任务支持多个并发订阅者
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import AsyncIterator

from youmi.gateway.models import TaskRecord


def task_update_event(record: TaskRecord) -> dict:
    """构造任务状态更新事件（status / result / error / 耗时等全量快照）"""
    return {
        "type": "task_update",
        "ts": time.time(),
        **record.to_dict(),
    }


class TaskEventHub:
    """任务事件中心（内存 pub-sub）。"""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._closed: set[str] = set()

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------

    def publish(self, task_id: str, event: dict) -> None:
        """向某任务的所有订阅者推送一个事件（非阻塞）"""
        for queue in list(self._subscribers.get(task_id, [])):
            queue.put_nowait(event)

    def close(self, task_id: str) -> None:
        """任务终结：向订阅者发送终止信号并清理订阅表"""
        self._closed.add(task_id)
        for queue in self._subscribers.pop(task_id, []):
            queue.put_nowait(None)

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------

    def subscribe(self, task_id: str) -> AsyncIterator[dict]:
        """订阅任务事件流（异步迭代）。

        任务已关闭（``close`` 之后）时返回空流；迭代自然结束（收到终止
        信号）或显式 ``aclose()`` 时清理订阅。注意：``async for`` 中
        ``break`` 不会立即关闭 async generator，提前退出时订阅方应调用
        ``aclose()``。
        """
        if task_id in self._closed:
            return _empty_stream()

        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers[task_id].append(queue)

        async def _iter() -> AsyncIterator[dict]:
            try:
                while True:
                    event = await queue.get()
                    if event is None:  # 终止信号
                        break
                    yield event
            finally:
                self._remove(task_id, queue)

        return _iter()

    def _remove(self, task_id: str, queue: asyncio.Queue) -> None:
        subs = self._subscribers.get(task_id)
        if not subs:
            return
        try:
            subs.remove(queue)
        except ValueError:
            pass
        if not subs:
            self._subscribers.pop(task_id, None)

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------

    def subscriber_count(self, task_id: str) -> int:
        return len(self._subscribers.get(task_id, []))

    @property
    def closed_count(self) -> int:
        return len(self._closed)


async def _empty_stream() -> AsyncIterator[dict]:
    """空事件流（终止任务订阅）"""
    return
    yield  # pragma: no cover - 使其成为 async generator


__all__ = ["TaskEventHub", "task_update_event"]
