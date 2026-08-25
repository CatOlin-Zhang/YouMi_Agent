"""
Coordinator 模块 — 任务编排与多 Agent 协调

提供:
- MasterAgent: 主协调 Agent，负责任务分析、子 Agent 实例化与工作流编排
- ToolGuardianAgent: 工具记忆守护 Agent，收集工具问题汇报并修正工具描述
- WorkflowPlan / WorkflowExecutor: 工作流计划与执行器 (P1)
- HandoffProtocol: Agent 间任务委派协议 (P1)
- PostTaskPipeline: 任务完成后后台流水线 (P1)
- SubProcessAgentRunner: 子 Agent 进程隔离运行器 (P1)
"""

from youmi.coordinator.master import MasterAgent, SubAgentRecord
from youmi.coordinator.tool_guardian import ToolGuardianAgent, ToolModification
from youmi.coordinator.plan import (
    WorkflowPlan,
    WorkflowStep,
    WorkflowExecutor,
    StepResult,
    StepStatus,
)
from youmi.coordinator.handoff import HandoffProtocol
from youmi.coordinator.post_task import PostTaskPipeline, ToolExperience, TaskOutcomeSummary
from youmi.coordinator.subprocess_agent import (
    SubProcessAgentRunner,
    SubProcessHandle,
    SubProcessResult,
)

__all__ = [
    "MasterAgent",
    "SubAgentRecord",
    "ToolGuardianAgent",
    "ToolModification",
    # P1: WorkflowPlan + Executor
    "WorkflowPlan",
    "WorkflowStep",
    "WorkflowExecutor",
    "StepResult",
    "StepStatus",
    # P1: Handoff
    "HandoffProtocol",
    # P1: PostTask Pipeline
    "PostTaskPipeline",
    "ToolExperience",
    "TaskOutcomeSummary",
    # P1: Subprocess Isolation
    "SubProcessAgentRunner",
    "SubProcessHandle",
    "SubProcessResult",
]
