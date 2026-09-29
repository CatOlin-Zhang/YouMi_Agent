"""
任务队列抽象 (gateway.queue)

``TaskQueue`` 定义网关对任务队列的最小契约；``InMemoryTaskQueue``
为默认的进程内实现（基于 ``asyncio.Queue``）。

替换指引：外部队列（Redis / RabbitMQ 等）只需实现同一接口，
即可通过 ``GatewayService(queue=...)`` 无缝替换。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from youmi.gateway.models import TaskRecord


class TaskQueue(ABC):
    """任务队列抽象。

    语义约定：
    - ``put`` 由提交方调用（不阻塞，除非实现有界背压）
    - ``get`` 由 worker 调用（阻塞等待）
    - worker 处理完成后必须调用 ``task_done``（配合 ``join`` 实现排水）
    """

    @abstractmethod
    async def put(self, record: TaskRecord) -> None:
        """入队一个任务"""

    @abstractmethod
    async def get(self) -> TaskRecord:
        """取出一个任务（阻塞）"""

    @abstractmethod
    def task_done(self) -> None:
        """标记当前任务处理完成"""

    @abstractmethod
    async def join(self) -> None:
        """等待队列中所有任务被处理完成"""

    @abstractmethod
    def qsize(self) -> int:
        """当前排队任务数"""


class InMemoryTaskQueue(TaskQueue):
    """进程内任务队列（asyncio.Queue 封装）。

    Args:
        maxsize: 队列上限；0 = 无界。有界时 ``put`` 在队满时阻塞（背压）
    """

    def __init__(self, maxsize: int = 0) -> None:
        self._queue: asyncio.Queue[TaskRecord] = asyncio.Queue(maxsize=maxsize)

    async def put(self, record: TaskRecord) -> None:
        await self._queue.put(record)

    async def get(self) -> TaskRecord:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()

    def qsize(self) -> int:
        return self._queue.qsize()


__all__ = ["TaskQueue", "InMemoryTaskQueue"]
