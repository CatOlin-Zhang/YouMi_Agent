"""
网关任务模型 (gateway.models)

- ``TaskStatus`` — 任务生命周期状态
- ``TaskRecord`` — 任务全量记录（提交信息 + 运行状态 + 结果 + 成本观测），
  网关内部与 API 层统一使用该模型
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """任务生命周期状态"""

    QUEUED = "queued"        # 已入队等待执行
    RUNNING = "running"      # 正在执行
    COMPLETED = "completed"  # 执行完成
    FAILED = "failed"        # 执行失败
    CANCELLED = "cancelled"  # 已取消（预留）

    @property
    def finished(self) -> bool:
        """是否为终态"""
        return self in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)


def new_task_id() -> str:
    """生成任务 ID（短 uuid，便于日志与 API 使用）"""
    return f"task_{uuid.uuid4().hex[:12]}"


class TaskRecord(BaseModel):
    """网关任务记录。

    Args:
        task_id: 任务唯一 ID（缺省自动生成）
        task: 任务描述（用户文本）
        tenant: 租户标识（多租户隔离）
        metadata: 调用方附加信息（如来源 / 优先级 / session_id）
        status: 生命周期状态
        result: 最终输出（完成时）
        error: 错误信息（失败时）
        iterations: ReAct 迭代次数
        tool_calls: 本轮任务调用的工具名列表
        worker_id: 处理该任务的 worker 标识
        created_at / started_at / finished_at: 时间戳（epoch 秒）
    """

    task_id: str = Field(default_factory=new_task_id)
    task: str = ""
    tenant: str = "default"
    metadata: dict[str, Any] = Field(default_factory=dict)

    status: TaskStatus = TaskStatus.QUEUED
    result: str = ""
    error: str = ""
    iterations: int = 0
    tool_calls: list[str] = Field(default_factory=list)
    worker_id: str = ""

    created_at: float = Field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None

    # ------------------------------------------------------------------
    # 派生属性
    # ------------------------------------------------------------------

    @property
    def duration_ms(self) -> float | None:
        """执行耗时（毫秒）；未开始为 None，运行中为已流逝时间"""
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else time.time()
        return round((end - self.started_at) * 1000.0, 2)

    def to_dict(self) -> dict[str, Any]:
        """序列化（status 转字符串，附 duration_ms）"""
        return {
            "task_id": self.task_id,
            "task": self.task,
            "tenant": self.tenant,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "iterations": self.iterations,
            "tool_calls": self.tool_calls,
            "worker_id": self.worker_id,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
        }


__all__ = ["TaskStatus", "TaskRecord", "new_task_id"]
