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
