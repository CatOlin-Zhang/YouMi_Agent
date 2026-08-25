"""
核心类型定义

包含 Agent 所需的所有基础数据结构:
- LLMConfig: 大语言模型连接配置
- MemoryConfig: 记忆系统配置
- RetryPolicy: 重试策略
- AgentMetadata: Agent 对外标签/元数据
- AgentMessage: Agent 间消息协议
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# LLM 相关
# ---------------------------------------------------------------------------

class LLMProvider(str, Enum):
    """支持的 LLM 服务提供商"""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    LOCAL = "local"          # 本地部署模型 (Ollama / vLLM 等)
    CUSTOM = "custom"        # 自定义兼容接口


class LLMConfig(BaseModel):
    """LLM 连接与推理参数配置

    兼容本地运行与远程 API:
    - provider=openai/anthropic 时，api_key + base_url 指向远程服务
    - provider=local 时，base_url 指向本地推理服务 (如 http://localhost:11434)
    - provider=custom 时，可对接任意兼容 OpenAI Chat Completions 格式的 API
    """

    provider: LLMProvider = LLMProvider.OPENAI
    model: str = "gpt-4o"
    api_key: str = Field(default="", description="API 密钥，建议从环境变量读取")
    base_url: str | None = Field(default=None, description="自定义 API 地址，本地部署时填入")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, gt=0)
    timeout_s: int = Field(default=120, gt=0, description="单次请求超时(秒)")
    extra_headers: dict[str, str] = Field(default_factory=dict)
    extra_params: dict[str, Any] = Field(default_factory=dict)

    # 上下文窗口预算 (P0: Compaction)
    max_context_tokens: int = Field(
        default=8000, gt=0,
        description="上下文 token 预算。Compactor 据此决定何时触发压缩。"
                    "设为模型实际 context window 的 80% 较安全。",
    )

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Memory 相关
# ---------------------------------------------------------------------------

class MemoryBackendType(str, Enum):
    """记忆后端类型"""

    SQLITE = "sqlite"          # 轻量本地存储 (短期记忆)
    CHROMADB = "chromadb"      # 向量检索 (长期记忆)
    REDIS = "redis"            # 高速缓存 (可选)
    MEMORY = "memory"          # 纯内存，测试/轻量场景使用


class ShortTermMemoryConfig(BaseModel):
    """短期记忆配置"""

    backend: MemoryBackendType = MemoryBackendType.MEMORY
    max_messages: int = Field(default=100, gt=0, description="保留的最大消息数")
    db_path: str | None = Field(default=None, description="SQLite 数据库路径")


class LongTermMemoryConfig(BaseModel):
    """长期记忆配置"""

    enabled: bool = False
    backend: MemoryBackendType = MemoryBackendType.CHROMADB
    embedding_model: str = Field(
        default="all-MiniLM-L6-v2",
        description="向量化模型名称 (sentence-transformers)",
    )
    persist_dir: str | None = Field(default=None, description="持久化目录")
    collection_name: str | None = Field(default=None, description="向量集合名称，默认按 agent_id 自动生成")


# ---------------------------------------------------------------------------
# 上下文压缩配置 (P0: Compaction)
# ---------------------------------------------------------------------------

class CompactionConfig(BaseModel):
    """上下文压缩配置

    参考 OpenClaw compaction 机制，控制 Agent 在对话过长时自动压缩上下文。
    """

    enabled: bool = Field(
        default=True,
        description="是否启用自动上下文压缩",
    )
    reserve_ratio: float = Field(
        default=0.8, ge=0.1, le=1.0,
        description="触发压缩的阈值比例。当已用 token 达到 max_context_tokens * ratio 时触发。",
    )
    keep_recent: int = Field(
        default=10, gt=0,
        description="压缩时保留的最近消息条数 (不参与压缩)",
    )

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Session 持久化配置 (P0: Persistence)
# ---------------------------------------------------------------------------

class SessionPersistenceConfig(BaseModel):
    """Session 持久化配置

    参考 OpenClaw SQLite session transcript，控制 Agent 对话的跨重启持久化。
    """

    enabled: bool = Field(
        default=False,
        description="是否启用 session 持久化",
    )
    backend: str = Field(
        default="sqlite",
        description="持久化后端类型: 'sqlite' 或 'file'",
    )
    db_path: str = Field(
        default=".youmi_sessions.db",
        description="SQLite 数据库文件路径 (backend='sqlite' 时使用)",
    )
    base_dir: str = Field(
        default=".youmi_sessions",
        description="JSON 文件存储根目录 (backend='file' 时使用)",
    )
    auto_restore: bool = Field(
        default=True,
        description="Agent 初始化时是否自动恢复最近的 session",
    )

    model_config = {"frozen": True}


class MemoryConfig(BaseModel):
    """Agent 记忆系统总配置

    支持两种配置方式:
    1. 策略模式 (推荐): 通过 strategy 字段指定记忆管理方案
       - "full" / "summary" / "lstm" — 预置策略
       - "/path/to/custom_strategy.py" — 自定义策略文件
    2. 后端模式 (兼容旧版): 通过 short_term / long_term 直接配置存储后端
    """

    # 策略模式 (新)
    strategy: str = Field(
        default="full",
        description="记忆策略名称 ('full'/'summary'/'lstm') 或自定义策略 .py 文件路径",
    )
    strategy_config: dict[str, Any] = Field(
        default_factory=dict,
        description="传给策略的配置参数 (如 buffer_size, keywords 等)",
    )

    # 上下文压缩 (P0)
    compaction: CompactionConfig = Field(
        default_factory=CompactionConfig,
        description="上下文压缩配置",
    )

    # Session 持久化 (P0)
    persistence: SessionPersistenceConfig = Field(
        default_factory=SessionPersistenceConfig,
        description="Session 持久化配置",
    )

    # 后端模式 (旧版兼容)
    short_term: ShortTermMemoryConfig = Field(default_factory=ShortTermMemoryConfig)
    long_term: LongTermMemoryConfig = Field(default_factory=LongTermMemoryConfig)

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# 重试策略
# ---------------------------------------------------------------------------

class BackoffStrategy(str, Enum):
    """退避策略"""

    FIXED = "fixed"            # 固定间隔
    LINEAR = "linear"          # 线性递增
    EXPONENTIAL = "exponential"  # 指数递增


class RetryPolicy(BaseModel):
    """重试策略配置"""

    max_retries: int = Field(default=3, ge=0)
    base_delay_s: float = Field(default=1.0, ge=0.0, description="基础等待时间(秒)")
    max_delay_s: float = Field(default=60.0, ge=0.0, description="最大等待时间(秒)")
    backoff: BackoffStrategy = BackoffStrategy.EXPONENTIAL
    retryable_exceptions: list[str] = Field(
        default_factory=lambda: ["TimeoutError", "ConnectionError"],
        description="可重试的异常类名列表",
    )

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Agent 对外标签 / 元数据
# ---------------------------------------------------------------------------

class AgentMetadata(BaseModel):
    """Agent 对外可见的标签与描述信息

    用于 Registry 检索、Agent 间发现、用户界面展示。
    """

    display_name: str = Field(default="", description="人类可读的显示名称")
    role: str = Field(default="general", description="角色标识 (coder / reviewer / researcher / ...)")
    description: str = Field(default="", description="角色能力描述")
    tags: list[str] = Field(default_factory=list, description="检索标签")
    version: str = Field(default="0.1.0", description="Agent 版本")
    capabilities: list[str] = Field(
        default_factory=list,
        description="能力声明列表，如 ['code_generation', 'code_review']",
    )
    custom: dict[str, Any] = Field(
        default_factory=dict,
        description="用户自定义扩展字段",
    )


# ---------------------------------------------------------------------------
# 工具装配配置
# ---------------------------------------------------------------------------

class BuiltinToolsConfig(BaseModel):
    """内置工具装配配置

    支持两种互斥模式:
    - include (白名单): 只注册列出的工具，其余全部排除
    - exclude (黑名单): 注册所有内置工具，排除列出的工具

    两者同时配置时，include 优先；两者均为空时等价于全量注册。
    """

    include: list[str] = Field(
        default_factory=list,
        description="白名单：只注册这些内置工具（为空则不限制，配合 exclude 使用）",
    )
    exclude: list[str] = Field(
        default_factory=list,
        description="黑名单：排除这些内置工具（include 非空时此字段被忽略）",
    )

    model_config = {"frozen": True}

    def resolve(self, all_tool_names: list[str]) -> list[str]:
        """根据 include/exclude 计算最终要注册的工具名称列表

        Args:
            all_tool_names: 所有可用内置工具的名称列表

        Returns:
            最终应注册的工具名称列表
        """
        if self.include:
            # 白名单模式：只保留在 all_tool_names 中存在的
            valid = set(all_tool_names)
            return [n for n in self.include if n in valid]
        if self.exclude:
            # 黑名单模式：排除指定的
            excluded = set(self.exclude)
            return [n for n in all_tool_names if n not in excluded]
        # 都为空：全量注册
        return list(all_tool_names)


class CustomToolRef(BaseModel):
    """自定义工具引用（预留扩展）

    通过 module + function 定位一个 Python 函数，
    Agent 初始化时动态导入并注册为工具。
    """

    name: str = Field(description="工具名称")
    module: str = Field(description="Python 模块路径，如 youmi.tools.code_ops")
    function: str = Field(description="模块内的函数名")
    description: str = Field(default="", description="工具功能描述（可选覆盖）")

    model_config = {"frozen": True}


class ToolsConfig(BaseModel):
    """Agent 工具装配总配置

    声明式定义 Agent 需要注册哪些工具，支持：
    - builtin: 内置工具（file_read / shell_exec 等）的 include/exclude
    - custom: 自定义工具函数引用（未来扩展）

    为空时保持向后兼容（全量注册内置工具）。
    """

    builtin: BuiltinToolsConfig = Field(
        default_factory=BuiltinToolsConfig,
        description="内置工具装配规则",
    )
    custom: list[CustomToolRef] = Field(
        default_factory=list,
        description="自定义工具引用列表（预留）",
    )

    model_config = {"frozen": True}

    @property
    def is_empty(self) -> bool:
        """是否未做任何工具声明（向后兼容标志）"""
        return (
            not self.builtin.include
            and not self.builtin.exclude
            and not self.custom
        )


# ---------------------------------------------------------------------------
# 工具发现配置 (ToolVault 向量搜索)
# ---------------------------------------------------------------------------

class ToolDiscoveryConfig(BaseModel):
    """工具发现配置 — 向量搜索 + 动态上下文加载

    启用后，MCPServer 将创建 ToolVault 管理工具的三级上下文:
    - HOT: 完整 schema 在 LLM 上下文中
    - WARM: 仅摘要可见，可快速重载
    - COLD: 仅在 Vault 中，需语义搜索发现

    Args:
        enabled: 是否启用工具发现
        embedding_model: Embedding 模型名称
        max_hot_tools: 热态工具上限
        recycle_after_turns: N 轮未使用则从 HOT 降级为 WARM
        search_top_k: 语义搜索结果数量
        min_similarity: 最低相似度阈值
    """

    enabled: bool = Field(
        default=False,
        description="是否启用工具发现 (向量搜索 + 动态加载)",
    )
    embedding_model: str = Field(
        default="nomic-embed-text",
        description="Embedding 模型名称 (OpenAI/Ollama 兼容)",
    )
    max_hot_tools: int = Field(
        default=15, gt=0,
        description="热态工具数量上限",
    )
    recycle_after_turns: int = Field(
        default=3, ge=1,
        description="连续 N 轮未使用的非必备热态工具降级为温态",
    )
    search_top_k: int = Field(
        default=5, gt=0,
        description="语义搜索返回结果数量",
    )
    min_similarity: float = Field(
        default=0.3, ge=0.0, le=1.0,
        description="语义搜索最低相似度阈值",
    )

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Handoff / 任务委派配置 (P1: OC-4)
# ---------------------------------------------------------------------------

class HandoffRule(BaseModel):
    """任务委派规则

    定义 Agent 在何种条件下将任务委派给其他 Agent。
    参考 OpenClaw 的 SOUL.md ## Handoffs 声明式委派。

    Args:
        name: 规则名称 (日志/标识用)
        target_agent_id: 目标 Agent ID (接收委任务的 Agent)
        trigger_keywords: 触发关键词列表 (用户消息中包含任一关键词时触发)
        trigger_description: 触发描述 (LLM 根据此描述判断是否委派)
        message_template: 委派消息模板 ({task} 会被替换为实际任务内容)
        max_depth: 最大委派链深度 (防止循环委派)
        enabled: 是否启用
    """

    name: str = Field(description="规则名称")
    target_agent_id: str = Field(description="目标 Agent ID")
    trigger_keywords: list[str] = Field(
        default_factory=list,
        description="触发关键词列表",
    )
    trigger_description: str = Field(
        default="",
        description="LLM 判断是否委派的描述文本",
    )
    message_template: str = Field(
        default="请将以下任务完成:\n{task}",
        description="委派消息模板",
    )
    max_depth: int = Field(
        default=3, ge=1, le=10,
        description="最大委派链深度",
    )
    enabled: bool = Field(default=True)

    model_config = {"frozen": True}


class HandoffConfig(BaseModel):
    """Handoff 总配置"""

    enabled: bool = Field(
        default=False,
        description="是否启用 Agent 间任务委派",
    )
    rules: list[HandoffRule] = Field(
        default_factory=list,
        description="委派规则列表",
    )
    default_max_depth: int = Field(
        default=3, ge=1, le=10,
        description="默认最大委派链深度",
    )
    timeout_seconds: float = Field(
        default=120, gt=0,
        description="等待委派结果的超时秒数",
    )

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Agent 间消息
# ---------------------------------------------------------------------------

class MessageRole(str, Enum):
    """消息角色"""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    AGENT = "agent"            # Agent 间通信
    TOOL = "tool"              # 工具调用结果


class AgentMessage(BaseModel):
    """Agent 间通信消息

    支持点对点 (指定 to_agent_id) 与广播 (to_agent_id 为 None 或 "*")。
    """

    message_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    from_agent_id: str
    to_agent_id: str | None = Field(default=None, description="目标 Agent ID，None 表示广播")
    role: MessageRole = MessageRole.AGENT
    content: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    @property
    def is_broadcast(self) -> bool:
        return self.to_agent_id is None or self.to_agent_id == "*"
