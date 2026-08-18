"""
定时/主动调度器 (HeartbeatScheduler)

参考 OpenClaw 的 HEARTBEAT.md 机制，让 Agent 可以主动行动而非只被动等待用户输入。

核心功能:
- 定时唤醒 Agent 执行预设任务
- 基于 asyncio 的轻量调度 (无外部依赖)
- 支持多种调度模式: 固定间隔 / 延迟执行 / 单次执行
- 任务执行结果可写入记忆，下次唤醒时参考

用法::

    from youmi.scheduler import HeartbeatScheduler, ScheduledTask

    scheduler = HeartbeatScheduler()

    # 添加定时任务
    scheduler.add_task(ScheduledTask(
        name="日志汇总",
        interval_seconds=1800,  # 每 30 分钟
        task_description="汇总最近 30 分钟的操作日志",
    ))

    # 绑定 Agent
    scheduler.bind_agent(agent)

    # 启动调度器 (后台运行)
    await scheduler.start()

    # 停止
    await scheduler.stop()
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Awaitable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 定时任务定义
# ---------------------------------------------------------------------------

class ScheduledTask(BaseModel):
    """定时任务定义

    Args:
        name: 任务名称 (标识 + 日志)
        interval_seconds: 执行间隔 (秒)。0 表示单次执行。
        task_description: 任务描述 (传给 Agent.run() 或 on_heartbeat())
        enabled: 是否启用
        max_runs: 最大执行次数 (0 表示无限制)
        delay_seconds: 首次执行延迟 (秒)
        metadata: 附加数据

    用法::

        # 每 30 分钟执行一次
        task = ScheduledTask(
            name="heartbeat",
            interval_seconds=1800,
            task_description="检查待处理任务",
        )

        # 延迟 10 秒后执行一次
        task = ScheduledTask(
            name="init_check",
            interval_seconds=0,
            task_description="初始化检查",
            delay_seconds=10,
        )
    """

    name: str = Field(description="任务名称")
    interval_seconds: float = Field(ge=0, description="执行间隔 (秒)，0 表示单次")
    task_description: str = Field(description="任务描述")
    enabled: bool = Field(default=True, description="是否启用")
    max_runs: int = Field(default=0, ge=0, description="最大执行次数，0=无限")
    delay_seconds: float = Field(default=0, ge=0, description="首次执行延迟秒数")
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# 任务运行时状态
# ---------------------------------------------------------------------------

class _TaskState:
    """任务运行时状态 (内部使用)"""

    def __init__(self, task: ScheduledTask) -> None:
        self.task = task
        self.run_count: int = 0
        self.last_run_at: float = 0
        self.last_result: Any = None
        self.last_error: str | None = None
        self._cancelled: bool = False

    @property
    def is_complete(self) -> bool:
        if self._cancelled:
            return True
        if not self.task.enabled:
            return True
        if self.task.max_runs > 0 and self.run_count >= self.task.max_runs:
            return True
        return False


# ---------------------------------------------------------------------------
# Heartbeat 回调签名
# ---------------------------------------------------------------------------

# async def(agent, task) -> Any
HeartbeatHandler = Callable[..., Awaitable[Any]]


# ---------------------------------------------------------------------------
# HeartbeatScheduler
# ---------------------------------------------------------------------------

class HeartbeatScheduler:
    """心跳调度器 — 定时唤醒 Agent 执行预设任务

    基于 asyncio 事件循环实现轻量调度，无需外部定时服务。

    特性:
    - 支持多个定时任务并行调度
    - 任务执行结果自动记录
    - 支持动态添加/移除/暂停任务
    - 支持自定义 handler (默认调用 Agent.run())
    - 优雅停止: stop() 等待当前正在执行的任务完成

    Args:
        handler: 自定义处理函数 (可选)。
            签名: ``async def handler(agent, task) -> Any``
            默认调用 ``agent.run(task.task_description)``

    用法::

        scheduler = HeartbeatScheduler()
        scheduler.add_task(ScheduledTask(name="check", interval_seconds=60, task_description="..."))
        scheduler.bind_agent(agent)
        await scheduler.start()

        # ... 运行中 ...
        await scheduler.stop()
    """

    def __init__(self, handler: HeartbeatHandler | None = None) -> None:
        self._handler = handler
        self._agent: Any = None  # Agent instance
        self._tasks: dict[str, _TaskState] = {}
        self._running: bool = False
        self._background_tasks: list[asyncio.Task] = []

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def task_names(self) -> list[str]:
        return list(self._tasks.keys())

    @property
    def agent(self) -> Any:
        return self._agent

    def bind_agent(self, agent: Any) -> None:
        """绑定目标 Agent

        Args:
            agent: Agent 实例 (需要有 run() 方法)
        """
        self._agent = agent
        logger.info("HeartbeatScheduler bound to agent '%s'", getattr(agent, 'name', '?'))

    def add_task(self, task: ScheduledTask) -> None:
        """添加定时任务

        Args:
            task: 定时任务定义

        Raises:
            ValueError: 任务名称已存在
        """
        if task.name in self._tasks:
            raise ValueError(f"任务 '{task.name}' 已存在")
        self._tasks[task.name] = _TaskState(task)
        logger.info(
            "Scheduled task added: '%s' (interval=%.0fs, enabled=%s)",
            task.name, task.interval_seconds, task.enabled,
        )

    def remove_task(self, name: str) -> bool:
        """移除定时任务

        Args:
            name: 任务名称

        Returns:
            True 表示成功移除，False 表示任务不存在
        """
        state = self._tasks.pop(name, None)
        if state:
            state._cancelled = True
            logger.info("Scheduled task removed: '%s'", name)
            return True
        return False

    def enable_task(self, name: str, enabled: bool = True) -> None:
        """启用/禁用任务"""
        state = self._tasks.get(name)
        if state:
            # 替换 task 定义 (frozen model)
            state.task = state.task.model_copy(update={"enabled": enabled})

    def get_task_state(self, name: str) -> dict[str, Any] | None:
        """获取任务运行状态"""
        state = self._tasks.get(name)
        if state is None:
            return None
        return {
            "name": state.task.name,
            "enabled": state.task.enabled,
            "run_count": state.run_count,
            "last_run_at": state.last_run_at,
            "last_error": state.last_error,
            "is_complete": state.is_complete,
            "interval_seconds": state.task.interval_seconds,
        }

    async def start(self) -> None:
        """启动调度器 (后台运行)

        为每个启用的任务创建后台 asyncio.Task。
        """
        if self._running:
            logger.warning("HeartbeatScheduler already running")
            return

        if self._agent is None:
            raise RuntimeError("No agent bound. Call bind_agent() first.")

        self._running = True

        for name, state in self._tasks.items():
            if state.task.enabled and not state.is_complete:
                task = asyncio.create_task(
                    self._run_task_loop(name),
                    name=f"heartbeat-{name}",
                )
                self._background_tasks.append(task)

        logger.info(
            "HeartbeatScheduler started: %d active tasks",
            len(self._background_tasks),
        )

    async def stop(self, timeout: float = 30.0) -> None:
        """停止调度器

        等待当前正在执行的任务完成后停止。

        Args:
            timeout: 等待超时秒数
        """
        self._running = False

        if self._background_tasks:
            done, pending = await asyncio.wait(
                self._background_tasks,
                timeout=timeout,
                return_when=asyncio.ALL_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass

            self._background_tasks.clear()

        logger.info("HeartbeatScheduler stopped")

    async def run_once(self, name: str) -> Any:
        """手动触发一次任务执行

        Args:
            name: 任务名称

        Returns:
            任务执行结果
        """
        state = self._tasks.get(name)
        if state is None:
            raise KeyError(f"任务 '{name}' 不存在")
        return await self._execute_task(state)

    async def _run_task_loop(self, name: str) -> None:
        """单个任务的调度循环"""
        state = self._tasks.get(name)
        if state is None:
            return

        # 首次延迟
        if state.task.delay_seconds > 0:
            await asyncio.sleep(state.task.delay_seconds)

        while self._running and not state.is_complete:
            try:
                await self._execute_task(state)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                state.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "Scheduled task '%s' failed: %s", name, exc,
                )

            # 间隔等待
            if state.task.interval_seconds > 0 and self._running:
                await asyncio.sleep(state.task.interval_seconds)
            else:
                # 单次执行
                break

    async def _execute_task(self, state: _TaskState) -> Any:
        """执行一次任务"""
        task = state.task
        logger.debug("Executing scheduled task: '%s'", task.name)

        state.run_count += 1
        state.last_run_at = time.time()
        state.last_error = None

        if self._handler is not None:
            result = await self._handler(self._agent, task)
        else:
            # 默认: 调用 agent.run()
            from youmi.core.agent import AgentStatus
            agent = self._agent

            # 确保 Agent 处于可执行状态
            if agent.status == AgentStatus.CREATED:
                await agent.initialize()

            # 如果 Agent 正在运行中，跳过本次
            if agent.status == AgentStatus.RUNNING:
                logger.warning(
                    "Agent '%s' is running, skipping scheduled task '%s'",
                    agent.name, task.name,
                )
                state.last_error = "Agent is busy"
                return None

            result = await agent.run(
                task=task.task_description,
                task_id=f"heartbeat-{task.name}-{state.run_count}",
            )

            # 重置 Agent 状态为 IDLE (允许下次调度)
            if agent.status in (AgentStatus.COMPLETED, AgentStatus.FAILED):
                agent._status = AgentStatus.IDLE

        state.last_result = result
        logger.info(
            "Scheduled task '%s' completed (run #%d)",
            task.name, state.run_count,
        )
        return result

    def snapshot(self) -> dict[str, Any]:
        """调度器状态快照"""
        return {
            "running": self._running,
            "agent": getattr(self._agent, 'name', None),
            "tasks": {
                name: self.get_task_state(name)
                for name in self._tasks
            },
        }
