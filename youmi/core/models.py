"""
Agent 数据模型

从 youmi/core/agent.py 提取，包含 Agent 生命周期所需的全部数据结构：
- AgentStatus  — 生命周期状态枚举
- AgentConfig  — 配置模型（纯数据，可序列化）
- TaskResult   — 任务执行结果
- _Observation / _Thought / _ActionResult / _Reflection — ReAct 循环中间数据
- _TaskSelfCheck — 任务自检结果
- _ToolRequest   — 工具权限申请
- _text_similarity — 文本相似度工具函数

设计约定:
- 所有模型均为 Pydantic BaseModel（可序列化、可校验）
- AgentConfig 是纯数据配置，可序列化、可从 YAML 加载
- 以 _ 前缀开头的类为框架内部使用，不建议外部直接引用
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from youmi.core.types import (
    AgentMetadata,
    HandoffConfig,
    LLMConfig,
    MemoryConfig,
    RetryPolicy,
    ToolsConfig,
)


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------

class AgentStatus(str, Enum):
    """Agent 生命周期状态

    状态流转:
        CREATED → IDLE → RUNNING → COMPLETED
                        ↘         ↗
                         WAITING
                        ↘         ↗
                         FAILED
                                 → DESTROYED
    """

    CREATED = "created"          # 已创建，未初始化
    IDLE = "idle"                # 初始化完成，等待任务
    RUNNING = "running"          # 正在执行 ReAct 循环
    WAITING = "waiting"          # 等待外部资源 / 其他 Agent
    COMPLETED = "completed"      # 任务成功完成
    FAILED = "failed"            # 任务失败
    DESTROYED = "destroyed"      # 已销毁，不可复用

    @property
    def is_terminal(self) -> bool:
        return self in (AgentStatus.COMPLETED, AgentStatus.FAILED, AgentStatus.DESTROYED)

    @property
    def is_active(self) -> bool:
        return self in (AgentStatus.RUNNING, AgentStatus.WAITING)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

class AgentConfig(BaseModel):
    """Agent 配置 — 纯数据，可序列化，可从 YAML/JSON 加载

    涵盖本地运行与远程 API 两种场景:
    - llm_config.base_url 指向远程服务 → API 模式
    - llm_config.provider=local + base_url=localhost → 本地模式
    """

    agent_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    name: str = Field(default="Agent", description="Agent 实例名称")
    system_prompt: str = Field(default="", description="系统提示词")

    # LLM
    llm_config: LLMConfig = Field(default_factory=LLMConfig)

    # 记忆
    memory_config: MemoryConfig = Field(default_factory=MemoryConfig)

    # 行为控制
    max_iterations: int = Field(default=20, gt=0, description="ReAct 最大迭代次数")
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)

    # 授权范围 (空列表表示不限制，由上层工厂填充)
    allowed_skills: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)

    # 工具装配（声明式注册，为空时保持向后兼容）
    tools: ToolsConfig = Field(
        default_factory=ToolsConfig,
        description="工具装配配置：声明 Agent 需要注册哪些工具",
    )

    # Handoff / 任务委派 (P1: OC-4)
    handoff: HandoffConfig = Field(
        default_factory=HandoffConfig,
        description="Agent 间任务委派配置",
    )

    # 对外标签
    metadata: AgentMetadata = Field(default_factory=AgentMetadata)

    # 自定义扩展
    extra: dict[str, Any] = Field(default_factory=dict)

    # 运行环境路径（逻辑工作目录，默认继承项目根目录）
    env: str = Field(
        default="",
        description="Agent 运行环境路径（逻辑工作目录），为空时自动检测项目根目录",
    )

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# 执行结果
# ---------------------------------------------------------------------------

class TaskResult(BaseModel):
    """Agent 任务执行结果"""

    agent_id: str
    task_id: str = ""
    status: AgentStatus = AgentStatus.COMPLETED
    output: Any = None                     # 主输出 (文本 / dict / list)
    iterations: int = 0                    # 实际执行的 ReAct 迭代次数
    error: str | None = None               # 失败时的错误信息
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def success(self) -> bool:
        return self.status == AgentStatus.COMPLETED


# ---------------------------------------------------------------------------
# ReAct 循环中间数据
# ---------------------------------------------------------------------------

class _Observation(BaseModel):
    """Observe 阶段输出 — 收集到的上下文"""
    messages: list[dict[str, Any]] = Field(default_factory=list)
    memories: list[dict[str, Any]] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class _Thought(BaseModel):
    """Think 阶段输出 — LLM 的推理结果"""
    reasoning: str = ""
    action_type: str = "respond"           # "tool_call" | "skill_call" | "respond" | "delegate"
    action_payload: dict[str, Any] = Field(default_factory=dict)
    should_continue: bool = True


class _ActionResult(BaseModel):
    """Act 阶段输出 — 行动执行结果"""
    success: bool = True
    output: Any = None
    error: str | None = None


class _Reflection(BaseModel):
    """Reflect 阶段输出 — 对结果的评估"""
    is_goal_met: bool = False
    summary: str = ""
    should_continue: bool = False
    next_hint: str = ""


class _TaskSelfCheck(BaseModel):
    """任务自检结果 — SubAgent 在 run() 前检查工具是否充足"""
    is_sufficient: bool = True
    missing_capabilities: list[str] = Field(default_factory=list)
    suggestion: str = ""
    request_tools: bool = False  # 是否需要申请更多工具


class _ToolRequest(BaseModel):
    """工具权限申请"""
    tool_description: str = ""
    reason: str = ""
    approved: bool = False


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _text_similarity(a: str, b: str) -> float:
    """简单的文本相似度检测（基于字符集合 Jaccard 相似度）。

    将两段文本各自拆成字符 3-gram 集合，计算 Jaccard 系数。
    返回值在 [0, 1] 之间，越高表示越相似。
    """
    if not a or not b:
        return 0.0

    def _ngrams(text: str, n: int = 3) -> set[str]:
        text = text.strip()
        if len(text) < n:
            return {text}
        return {text[i:i + n] for i in range(len(text) - n + 1)}

    set_a = _ngrams(a)
    set_b = _ngrams(b)
    if not set_a or not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0
