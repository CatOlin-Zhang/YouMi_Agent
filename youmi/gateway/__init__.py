"""
YouMi Gateway — 任务网关（P1 里程碑）

把 MasterAgent 的对话执行封装为「提交任务 → worker 池异步执行 →
状态查询 / 事件流」的生产可用形态：

- ``models`` — TaskStatus / TaskRecord（任务全量记录）
- ``queue`` — TaskQueue / InMemoryTaskQueue（队列抽象，可外接 Redis 等）
- ``registry`` — TaskRegistry（内存记录表 + 容量保护）
- ``events`` — TaskEventHub（内存 pub-sub，SSE / WebSocket 的底层）
- ``executor`` — TaskExecutor / MasterTaskExecutor（驱动 MasterAgent 执行）
- ``worker`` — WorkerPool（asyncio worker 池 + 状态流转）
- ``service`` — GatewayService（门面：submit / get / list / subscribe / stats）
- ``api`` — FastAPI 应用工厂（可选依赖，需 ``pip install -e .[gateway]``）

快速开始::

    from youmi.gateway import GatewayService, MasterTaskExecutor

    executor = MasterTaskExecutor(master)          # master 须已 initialize
    service = GatewayService(executor, size=2)
    await service.start()
    record = await service.submit("帮我查下北京天气", tenant="team-a")
    async for event in service.subscribe(record.task_id):
        ...
    await service.stop()
"""

from typing import Any

from youmi.gateway.events import TaskEventHub, task_update_event
from youmi.gateway.executor import MasterTaskExecutor, TaskExecutor, TaskOutcome
from youmi.gateway.models import TaskRecord, TaskStatus, new_task_id
from youmi.gateway.queue import InMemoryTaskQueue, TaskQueue
from youmi.gateway.registry import TaskRegistry
from youmi.gateway.service import GatewayService
from youmi.gateway.worker import WorkerPool

__all__ = [
    # models
    "TaskStatus",
    "TaskRecord",
    "new_task_id",
    # queue
    "TaskQueue",
    "InMemoryTaskQueue",
    # registry
    "TaskRegistry",
    # events
    "TaskEventHub",
    "task_update_event",
    # executor
    "TaskExecutor",
    "TaskOutcome",
    "MasterTaskExecutor",
    # worker
    "WorkerPool",
    # service
    "GatewayService",
    # api（可选依赖，延迟导入）
    "create_app",
]


def __getattr__(name: str) -> Any:
    """延迟导入 API 模块（fastapi 为可选依赖，仅访问时才要求安装）"""
    if name in ("create_app", "api"):
        from youmi.gateway import api
        return api if name == "api" else api.create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
