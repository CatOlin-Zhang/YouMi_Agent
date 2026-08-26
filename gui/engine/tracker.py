"""工作流追踪器 — 自动从 MasterAgent 的工具调用中提取并追踪任务步骤。

职责：
1. 监听 ``create_sub_agent`` / ``run_sub_agent`` 工具调用，自动维护步骤清单
2. 通过 ``workflow_step`` / ``workflow_complete`` 事件把进度推送到前端
3. 实施子 Agent 创建数量硬限制，防止 MasterAgent 无限循环验证
4. 所有步骤完成后发出停止信号，让 Bridge 强制终止 ReAct 循环

设计原则：
- Tracker 是无状态的（每次用户消息重置），不持久化
- 它只关心 *GUI 展示* 和 *防循环*，不影响 YouMi 核心逻辑
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# 子 Agent 创建数量硬限制（防止 MasterAgent 无限验证循环）
MAX_SUB_AGENTS = 6


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class _Step:
    step_id: str
    role: str
    task: str
    status: StepStatus = StepStatus.PENDING
    agent_id: str = ""
    output_preview: str = ""
    started_at: float = 0
    finished_at: float = 0


class WorkflowTracker:
    """工作流进度追踪器。

    Bridge 在每次用户消息开始时创建（或重置）一个实例，
    然后把引用交给 HookBridge 和 patched coordinator_ops 使用。
    """

    def __init__(self, bridge: Any, max_sub_agents: int = MAX_SUB_AGENTS) -> None:
        self._bridge = bridge
        self._max = max_sub_agents
        self._steps: list[_Step] = []
        self._step_map: dict[str, _Step] = {}  # agent_id -> step
        self._next_seq: int = 1
        self._active: bool = True
        self._all_done_emitted: bool = False

    # ------------------------------------------------------------------
    # 公开属性
    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self._active

    @property
    def steps(self) -> list[dict]:
        return [self._step_dict(s) for s in self._steps]

    @property
    def sub_agent_count(self) -> int:
        return len(self._steps)

    @property
    def limit_reached(self) -> bool:
        return self.sub_agent_count >= self._max

    @property
    def all_done(self) -> bool:
        if not self._steps:
            return False
        return all(s.status in (StepStatus.DONE, StepStatus.FAILED) for s in self._steps)

    # ------------------------------------------------------------------
    # 工具调用钩子（由 patched coordinator_ops 调用）
    # ------------------------------------------------------------------
    def on_create_sub_agent(self, role: str, task: str, agent_id: str) -> dict:
        """记录一个新的 create_sub_agent 调用（幂等：相同 agent_id 不会重复创建）。

        Returns:
            步骤字典（用于回传给工具函数的 meta）
        """
        # 幂等：如果已有该 agent_id 的步骤，直接返回
        existing = self._step_map.get(agent_id)
        if existing is not None:
            logger.debug("[Tracker] 步骤 %s 已存在，跳过重复记录", existing.step_id)
            return self._step_dict(existing)

        step_id = f"step_{self._next_seq}"
        self._next_seq += 1
        step = _Step(step_id=step_id, role=role, task=task, agent_id=agent_id)
        self._steps.append(step)
        self._step_map[agent_id] = step
        logger.info("[Tracker] 添加步骤 %s: role=%s task=%s", step_id, role, task[:50])
        self._emit_step(step)
        return self._step_dict(step)

    def on_run_sub_agent_start(self, agent_id: str) -> None:
        step = self._step_map.get(agent_id)
        if step and step.status == StepStatus.PENDING:
            step.status = StepStatus.RUNNING
            step.started_at = time.time()
            logger.info("[Tracker] 步骤 %s 开始运行", step.step_id)
            self._emit_step(step)

    def on_run_sub_agent_done(
        self, agent_id: str, status: str, output: str = ""
    ) -> None:
        step = self._step_map.get(agent_id)
        if not step:
            return
        step.status = StepStatus.DONE if status == "completed" else StepStatus.FAILED
        step.finished_at = time.time()
        step.output_preview = (output[:120] + "...") if len(output) > 120 else output
        logger.info("[Tracker] 步骤 %s 完成: %s", step.step_id, step.status.value)
        self._emit_step(step)
        self._check_all_done()

    def can_create_more(self) -> bool:
        """是否还能创建更多子 Agent。"""
        return not self.limit_reached

    def get_limit_message(self) -> str:
        """达到限制时注入给 MasterAgent 的系统提醒。"""
        done = sum(1 for s in self._steps if s.status == StepStatus.DONE)
        total = len(self._steps)
        return (
            f"【系统限制】你已经创建了 {total} 个子Agent（上限 {self._max}），"
            f"其中 {done} 个已完成。请不要再创建新的子Agent，"
            f"直接汇总已有结果并回复用户。"
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _step_dict(self, s: _Step) -> dict:
        return {
            "step_id": s.step_id,
            "role": s.role,
            "task": s.task,
            "status": s.status.value,
            "agent_id": s.agent_id,
            "output_preview": s.output_preview,
        }

    def _emit_step(self, step: _Step) -> None:
        from gui.hub.events import workflow_step
        session_id = self._bridge.active_session_id
        if not session_id:
            return
        self._bridge._emit(workflow_step(session_id, self._step_dict(step)))

    def _check_all_done(self) -> None:
        if self._all_done_emitted:
            return
        if not self.all_done:
            return
        self._all_done_emitted = True
        logger.info("[Tracker] 所有步骤已完成，发送 workflow_complete")
        from gui.hub.events import workflow_complete
        session_id = self._bridge.active_session_id
        if session_id:
            done = sum(1 for s in self._steps if s.status == StepStatus.DONE)
            failed = sum(1 for s in self._steps if s.status == StepStatus.FAILED)
            self._bridge._emit(workflow_complete(
                session_id,
                total=len(self._steps),
                done=done,
                failed=failed,
                steps=self.steps,
            ))
