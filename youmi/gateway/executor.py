"""
任务执行器 (gateway.executor)

``TaskExecutor`` 定义「把一个 TaskRecord 执行出结果」的契约；
``MasterTaskExecutor`` 是默认实现：驱动 ``MasterAgent`` 完成单个任务。

并发语义（重要）：
- MasterAgent 的对话/记忆为实例态，多 worker 并发执行同一实例会竞态；
  ``MasterTaskExecutor`` 实例内用 asyncio.Lock 串行执行任务
- 需要并行吞吐时，创建多个 ``MasterTaskExecutor``（各自独立 MasterAgent，
  或按租户隔离的 factory），分别绑定到不同的 WorkerPool

多租户：
- ``MasterTaskExecutor(factory=...)`` 模式下，按任务 tenant 懒创建/复用
  独立 MasterAgent（factory 负责装配与 initialize），实现租户级执行隔离
- 不传 factory（单 master 模式）时所有租户共享一个 MasterAgent
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TaskOutcome:
    """任务执行结果（执行器输出，worker 写入 TaskRecord）"""

    output: str = ""
    error: str = ""
    iterations: int = 0
    tool_calls: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.error


class TaskExecutor(ABC):
    """任务执行器抽象。"""

    @abstractmethod
    async def execute(self, task: "TaskRecordLike") -> TaskOutcome:
        """执行任务并返回结果（不应抛异常；失败以 ``TaskOutcome.error`` 表达）"""

    async def aclose(self) -> None:
        """释放资源（默认无操作）"""


# 说明：仅为类型提示用途，避免循环导入
TaskRecordLike = Any


class MasterTaskExecutor(TaskExecutor):
    """默认执行器 — 驱动 MasterAgent 完成单个任务。

    执行契约：``master.chat_turn(task.task)``（返回
    ``{response, iterations, tool_calls, error}``）。

    Args:
        master: 单实例模式使用的 MasterAgent（须已 initialize）
        factory: 多租户模式；``factory(tenant) -> MasterAgent``（须已 initialize
            并完成装配；可为同步或异步函数），执行器按租户懒创建并缓存实例
        reset_between_tasks: 是否在任务之间调用 ``master.reset_for_new_task()``
            隔离上下文（默认 True；首个任务不额外重置）
        master 与 factory 至少提供其一（同时提供时 factory 优先）
    """

    def __init__(
        self,
        master: Any | None = None,
        *,
        factory: Callable[[str], Any] | None = None,
        reset_between_tasks: bool = True,
    ) -> None:
        if master is None and factory is None:
            raise ValueError("MasterTaskExecutor 需要 master 或 factory 之一")
        self._master = master
        self._factory = factory
        self._reset = reset_between_tasks
        self._per_tenant: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._executed_any = False

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    async def execute(self, task: TaskRecordLike) -> TaskOutcome:
        tenant = getattr(task, "tenant", "") or "default"
        try:
            master = await self._master_for(tenant)
        except Exception as exc:
            logger.exception("MasterTaskExecutor: 获取 MasterAgent 失败")
            return TaskOutcome(error=f"获取 MasterAgent 失败: {type(exc).__name__}: {exc}")

        if master is None:
            return TaskOutcome(error="MasterAgent 未配置")

        # 实例内串行：MasterAgent 对话/记忆为实例态
        async with self._lock:
            if self._reset and self._executed_any:
                await self._safe_reset(master)
            self._executed_any = True

            try:
                result = await master.chat_turn(task.task)
            except Exception as exc:
                logger.exception("MasterTaskExecutor: 任务 %s 执行异常", getattr(task, "task_id", "?"))
                return TaskOutcome(error=f"{type(exc).__name__}: {exc}")

        return self._to_outcome(result)

    async def _master_for(self, tenant: str) -> Any:
        """按租户获取 MasterAgent（单实例模式直返；factory 模式懒创建缓存）

        factory 可为同步或异步函数（异步适用于需要 ``await master.initialize()``
        的装配场景）。
        """
        if self._factory is None:
            return self._master
        master = self._per_tenant.get(tenant)
        if master is None:
            master = self._factory(tenant)
            if inspect.isawaitable(master):
                master = await master
            self._per_tenant[tenant] = master
            logger.info("MasterTaskExecutor: 已为租户 '%s' 创建 MasterAgent", tenant)
        return master

    @staticmethod
    async def _safe_reset(master: Any) -> None:
        """重置 MasterAgent 上下文（失败仅告警，不阻断任务）"""
        reset = getattr(master, "reset_for_new_task", None)
        if reset is None:
            return
        try:
            await reset()
        except Exception as exc:
            logger.warning("MasterTaskExecutor: reset_for_new_task 失败（忽略）: %s", exc)

    @staticmethod
    def _to_outcome(result: Any) -> TaskOutcome:
        """把 chat_turn 返回 dict 映射为 TaskOutcome"""
        if not isinstance(result, dict):
            return TaskOutcome(output=str(result or ""))
        return TaskOutcome(
            output=str(result.get("response") or ""),
            error=str(result.get("error") or ""),
            iterations=int(result.get("iterations") or 0),
            tool_calls=[str(t) for t in (result.get("tool_calls") or [])],
        )

    # ------------------------------------------------------------------
    # 观测 / 生命周期
    # ------------------------------------------------------------------

    @property
    def tenants(self) -> list[str]:
        """factory 模式下已创建租户 MasterAgent 的租户列表"""
        return list(self._per_tenant.keys())

    async def aclose(self) -> None:
        """关闭所有租户 MasterAgent（若支持 destroy）"""
        masters = list(self._per_tenant.values())
        if self._master is not None:
            masters.append(self._master)
        seen: set[int] = set()
        for master in masters:
            if id(master) in seen:
                continue
            seen.add(id(master))
            destroy = getattr(master, "destroy", None)
            if destroy is None:
                continue
            try:
                await destroy()
            except Exception as exc:
                logger.warning("MasterTaskExecutor: destroy 失败: %s", exc)


__all__ = ["TaskExecutor", "TaskOutcome", "MasterTaskExecutor"]
