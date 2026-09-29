"""
任务工作者 (gateway.worker)

``WorkerPool`` 管理一组 asyncio worker，循环从队列取任务并驱动
``TaskExecutor`` 执行，期间维护 TaskRecord 状态流转与事件发布：

    QUEUED --(取出)--> RUNNING --(执行完毕)--> COMPLETED / FAILED

状态流转时序：
1. 取出任务：置 RUNNING / started_at / worker_id → write registry → publish
2. 执行：``executor.execute(record) -> TaskOutcome``（异常防御性兜底）
3. 终结：写入 result / error / iterations / tool_calls / finished_at，
   状态置 COMPLETED（无 error）或 FAILED → write registry → publish → close

停止语义：
- ``stop(drain=True)`` 先等待队列清空（超时仅告警），再取消 worker
- ``stop(drain=False)`` 立即取消 worker（未完成任务停留在当前状态）
"""

from __future__ import annotations

import asyncio
import logging
import time

from youmi.gateway.events import TaskEventHub, task_update_event
from youmi.gateway.executor import TaskExecutor, TaskOutcome
from youmi.gateway.models import TaskRecord, TaskStatus
from youmi.gateway.queue import InMemoryTaskQueue, TaskQueue
from youmi.gateway.registry import TaskRegistry

logger = logging.getLogger(__name__)


class WorkerPool:
    """asyncio worker 池。

    Args:
        executor: 任务执行器（worker 调用 ``executor.execute(record)``）
        queue / registry / events: 任务队列 / 记录表 / 事件中心
            （缺省各自新建；传入同一实例即可与其他组件共享）
        size: worker 数量（>=1）
        worker_prefix: worker 标识前缀（生成 ``worker-0`` / ``worker-1`` ...）
    """

    def __init__(
        self,
        executor: TaskExecutor,
        *,
        queue: TaskQueue | None = None,
        registry: TaskRegistry | None = None,
        events: TaskEventHub | None = None,
        size: int = 2,
        worker_prefix: str = "worker",
    ) -> None:
        if size < 1:
            raise ValueError("WorkerPool size 必须 >= 1")
        self.queue = queue or InMemoryTaskQueue()
        self.registry = registry or TaskRegistry()
        self.events = events or TaskEventHub()
        self._executor = executor
        self._size = size
        self._prefix = worker_prefix
        self._tasks: list[asyncio.Task] = []
        self._worker_ids: list[str] = []

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动 worker（幂等：已启动时直接返回）"""
        if self._tasks:
            return
        for i in range(self._size):
            worker_id = f"{self._prefix}-{i}"
            self._worker_ids.append(worker_id)
            self._tasks.append(
                asyncio.create_task(self._run_worker(worker_id), name=worker_id)
            )
        logger.info("WorkerPool: 已启动 %d 个 worker", self._size)

    async def stop(self, *, drain: bool = True, timeout: float = 30.0) -> None:
        """停止 worker。

        Args:
            drain: True 时先等待队列清空（最多 ``timeout`` 秒，超时仅告警）；
                False 时立即取消
            timeout: 排水等待上限（秒）
        """
        if drain and self._tasks:
            try:
                await asyncio.wait_for(self.queue.join(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("WorkerPool: 排水超时（%.1fs），仍有任务未完成", timeout)

        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._worker_ids.clear()
        logger.info("WorkerPool: 已停止")

    # ------------------------------------------------------------------
    # worker 主循环
    # ------------------------------------------------------------------

    async def _run_worker(self, worker_id: str) -> None:
        """单个 worker 的主循环（被取消时退出）"""
        while True:
            try:
                record = await self.queue.get()
            except asyncio.CancelledError:
                break
            try:
                await self._execute(record, worker_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # 防御：单任务异常不得杀死 worker
                logger.exception("WorkerPool: worker %s 处理任务异常", worker_id)
            finally:
                self.queue.task_done()

    async def _execute(self, record: TaskRecord, worker_id: str) -> None:
        """执行单个任务并维护状态流转与事件发布"""
        record.status = TaskStatus.RUNNING
        record.started_at = time.time()
        record.worker_id = worker_id
        self._publish(record)

        try:
            outcome = await self._executor.execute(record)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 防御：执行器违反「不抛异常」契约
            logger.exception("WorkerPool: 执行器异常 task=%s", record.task_id)
            outcome = TaskOutcome(error=f"{type(exc).__name__}: {exc}")

        record.result = outcome.output
        record.error = outcome.error
        record.iterations = outcome.iterations
        record.tool_calls = list(outcome.tool_calls)
        record.finished_at = time.time()
        record.status = TaskStatus.COMPLETED if outcome.success else TaskStatus.FAILED
        self._publish(record)
        self.events.close(record.task_id)  # 任务终结：结束订阅流

    def _publish(self, record: TaskRecord) -> None:
        """写注册表并向订阅者广播任务快照"""
        self.registry.put(record)
        self.events.publish(record.task_id, task_update_event(record))

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------

    @property
    def size(self) -> int:
        return self._size

    @property
    def running(self) -> bool:
        return bool(self._tasks)

    @property
    def worker_ids(self) -> list[str]:
        return list(self._worker_ids)


__all__ = ["WorkerPool"]
