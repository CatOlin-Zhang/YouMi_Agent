"""工作流计划数据模型 — 仅保留可持久化的计划骨架

执行器（WorkflowExecutor）与 Agent 运行时属于旧宿主职责，
已随 dsh-shell 迁移移除；本模块只提供 PlanMemory / sidecar 所需的
schema、DAG 校验与拓扑排序能力。

用法::

    from youmi.plan.models import WorkflowPlan, WorkflowStep

    plan = WorkflowPlan(
        name="数据分析工作流",
        steps=[
            WorkflowStep(step_id="collect", role="researcher", task="收集数据"),
            WorkflowStep(step_id="analyze", role="analyst", task="分析数据",
                         depends_on=["collect"]),
        ],
    )
    errors = plan.validate()
"""

from __future__ import annotations

from collections import defaultdict, deque
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 步骤状态
# ---------------------------------------------------------------------------

class StepStatus(str, Enum):
    """工作流步骤状态"""

    PENDING = "pending"       # 等待执行
    RUNNING = "running"       # 执行中
    COMPLETED = "completed"   # 成功完成
    FAILED = "failed"         # 执行失败
    SKIPPED = "skipped"       # 跳过 (依赖失败)


# ---------------------------------------------------------------------------
# 步骤定义
# ---------------------------------------------------------------------------

class WorkflowStep(BaseModel):
    """工作流步骤定义

    Args:
        step_id: 步骤唯一标识 (用于依赖引用)
        role: 执行角色 (DSH 侧映射为 agent preset / subagent 类型)
        task: 任务描述
        name: 实例名称 (默认使用 role)
        system_prompt: 系统提示词覆盖
        allowed_tools: 允许使用的工具列表
        depends_on: 依赖的步骤 ID 列表 (这些步骤完成后才开始)
        env: 运行环境路径 (空字符串表示继承)
        config_overrides: 额外配置覆盖
        max_iterations: 最大迭代次数
        timeout_seconds: 超时秒数 (0 表示不限制)
    """

    step_id: str = Field(description="步骤唯一标识")
    role: str = Field(description="执行角色")
    task: str = Field(description="任务描述")
    name: str = Field(default="", description="实例名称，默认使用 role")
    system_prompt: str = Field(default="")
    allowed_tools: list[str] | None = None
    depends_on: list[str] = Field(default_factory=list)
    env: str = ""
    config_overrides: dict[str, Any] = Field(default_factory=dict)
    max_iterations: int = 20
    timeout_seconds: float = 0
    retry_count: int = Field(default=2, description="步骤失败后最大重试次数")
    retry_backoff: float = Field(
        default=2.0,
        description="指数退避基础秒数（每次重试乘以 2^attempt）",
    )

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# 工作流计划
# ---------------------------------------------------------------------------

class WorkflowPlan(BaseModel):
    """工作流计划 — 定义完整的执行蓝图

    Args:
        name: 计划名称 (日志/展示用)
        steps: 步骤列表
        description: 计划描述
        metadata: 附加元数据
    """

    name: str = Field(default="workflow", description="计划名称")
    steps: list[WorkflowStep] = Field(default_factory=list)
    description: str = Field(default="")
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"frozen": True}

    def validate(self) -> list[str]:
        """验证计划的有效性

        Returns:
            错误消息列表 (空列表表示有效)
        """
        errors: list[str] = []
        step_ids = {s.step_id for s in self.steps}

        # 检查重复 ID
        if len(step_ids) != len(self.steps):
            errors.append("存在重复的 step_id")

        # 检查依赖引用
        for step in self.steps:
            for dep in step.depends_on:
                if dep not in step_ids:
                    errors.append(
                        f"步骤 '{step.step_id}' 依赖 '{dep}'，但该步骤不存在"
                    )
            if step.step_id in step.depends_on:
                errors.append(f"步骤 '{step.step_id}' 不能依赖自己")

        # 检查循环依赖
        if not errors:
            cycle = self._detect_cycle()
            if cycle:
                errors.append(f"检测到循环依赖: {' → '.join(cycle)}")

        return errors

    def _detect_cycle(self) -> list[str] | None:
        """检测循环依赖 (DFS)"""
        adj: dict[str, list[str]] = {s.step_id: list(s.depends_on) for s in self.steps}
        visited: set[str] = set()
        in_stack: set[str] = set()
        path: list[str] = []

        def dfs(node: str) -> list[str] | None:
            visited.add(node)
            in_stack.add(node)
            path.append(node)

            for dep in adj.get(node, []):
                if dep not in visited:
                    cycle = dfs(dep)
                    if cycle:
                        return cycle
                elif dep in in_stack:
                    idx = path.index(dep)
                    return path[idx:] + [dep]

            path.pop()
            in_stack.discard(node)
            return None

        for step in self.steps:
            if step.step_id not in visited:
                cycle = dfs(step.step_id)
                if cycle:
                    return cycle
        return None

    def get_execution_order(self) -> list[list[str]]:
        """计算拓扑执行顺序 (按层级分组)

        Returns:
            层级列表: [[无依赖的步骤], [依赖第一层的步骤], ...]
            同一层级的步骤可并行执行。
        """
        in_degree: dict[str, int] = {s.step_id: len(s.depends_on) for s in self.steps}
        adj: dict[str, list[str]] = defaultdict(list)
        for s in self.steps:
            for dep in s.depends_on:
                adj[dep].append(s.step_id)

        layers: list[list[str]] = []
        queue = deque(sid for sid, deg in in_degree.items() if deg == 0)

        while queue:
            layer = list(queue)
            layers.append(layer)
            next_queue: deque[str] = deque()
            for sid in layer:
                for child in adj[sid]:
                    in_degree[child] -= 1
                    if in_degree[child] == 0:
                        next_queue.append(child)
            queue = next_queue

        return layers

    def get_step(self, step_id: str) -> WorkflowStep | None:
        """按 ID 获取步骤"""
        for s in self.steps:
            if s.step_id == step_id:
                return s
        return None
