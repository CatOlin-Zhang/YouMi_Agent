"""
网关服务门面 (gateway.service)

``GatewayService`` 组装队列 / 注册表 / 事件中心 / worker 池，
为 API 层（FastAPI / GUI）提供统一入口：

- ``submit`` — 提交任务（返回 TaskRecord 快照）
- ``get`` / ``list_tasks`` — 查询任务状态
- ``subscribe`` — 订阅任务事件流（SSE / WebSocket 的底层）
- ``stats`` — 队列与任务统计
- ``start`` / ``stop`` — 生命周期（worker 池 + 执行器）

执行器可选：``GatewayService(executor=None)`` 时仅提供提交/查询能力
（任务停留在队列中，适合 API 层联调或纯存储场景）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from youmi.gateway.events import TaskEventHub, task_update_event
from youmi.gateway.executor import TaskExecutor
from youmi.gateway.models import TaskRecord, TaskStatus
from youmi.gateway.queue import InMemoryTaskQueue, TaskQueue
from youmi.gateway.registry import TaskRegistry
from youmi.gateway.worker import WorkerPool

logger = logging.getLogger(__name__)


class GatewayService:
    """任务网关服务门面。

    Args:
        executor: 任务执行器（如 ``MasterTaskExecutor``）；None 时不启动 worker
        queue / registry / events: 可注入的队列 / 记录表 / 事件中心
        size: worker 数量
        worker_prefix: worker 标识前缀
    """

    def __init__(
        self,
        executor: TaskExecutor | None = None,
        *,
        queue: TaskQueue | None = None,
        registry: TaskRegistry | None = None,
        events: TaskEventHub | None = None,
        size: int = 2,
        worker_prefix: str = "worker",
    ) -> None:
        self.queue = queue or InMemoryTaskQueue()
        self.registry = registry or TaskRegistry()
        self.events = events or TaskEventHub()
        self._executor = executor
        self._pool: WorkerPool | None = None
        if executor is not None:
            self._pool = WorkerPool(
                executor,
                queue=self.queue,
                registry=self.registry,
                events=self.events,
                size=size,
                worker_prefix=worker_prefix,
            )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动 worker 池（无执行器时为 no-op）"""
        if self._pool is not None:
            await self._pool.start()

    async def stop(self, *, drain: bool = True, timeout: float = 30.0) -> None:
        """停止 worker 池并释放执行器

        Args:
            drain: True 先等待队列清空（最多 ``timeout`` 秒）
            timeout: 排水等待上限（秒）
        """
        if self._pool is not None:
            await self._pool.stop(drain=drain, timeout=timeout)
        if self._executor is not None:
            await self._executor.aclose()

    # ------------------------------------------------------------------
    # 任务 API
    # ------------------------------------------------------------------

    async def submit(
        self,
        task: str,
        *,
        tenant: str = "default",
        metadata: dict[str, Any] | None = None,
        task_id: str | None = None,
    ) -> TaskRecord:
        """提交任务：登记记录 → 发布 queued 事件 → 入队

        Returns:
            任务记录快照（可在队列/worker 处理过程中通过 ``get`` 轮询最新状态）
        """
        record = TaskRecord(
            task=task,
            tenant=tenant or "default",
            metadata=dict(metadata or {}),
        )
        if task_id:
            record.task_id = task_id
        self.registry.put(record)
        self.events.publish(record.task_id, task_update_event(record))
        await self.queue.put(record)
        return record

    def get(self, task_id: str) -> TaskRecord | None:
        """按 ID 查询任务记录"""
        return self.registry.get(task_id)

    def list_tasks(
        self,
        *,
        tenant: str | None = None,
        status: TaskStatus | None = None,
        limit: int = 100,
    ) -> list[TaskRecord]:
        """列出任务记录（新→旧，可按租户/状态过滤）"""
        return self.registry.list(tenant=tenant, status=status, limit=limit)

    def subscribe(self, task_id: str) -> AsyncIterator[dict]:
        """订阅任务事件流（任务已终结时返回空流）"""
        return self.events.subscribe(task_id)

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """队列 + 任务 + worker 统计"""
        data = self.registry.stats()
        data["queue_size"] = self.queue.qsize()
        data["workers"] = self._pool.size if self._pool else 0
        data["workers_running"] = self._pool.running if self._pool else False
        return data

    @property
    def pool(self) -> WorkerPool | None:
        """worker 池（无执行器时为 None）"""
        return self._pool


__all__ = ["GatewayService"]
