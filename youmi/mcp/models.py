"""
MCP 数据模型

从 youmi/mcp/vault.py 提取，包含 MCP 工具管理层的数据结构：
- ToolContextTier  — 工具上下文层级枚举 (HOT/WARM/COLD)
- ToolEntry        — ToolVault 中的工具条目
- ToolSearchResult — 工具语义搜索结果

这些模型被 vault.py、bridge.py 和上层模块共同引用。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from youmi.core.tool import ToolDefinition


class ToolContextTier(str, Enum):
    """工具上下文层级

    状态流转:
        COLD → HOT  (通过语义搜索发现后加载)
        HOT  → WARM (LRU 回收: 连续 N 轮未使用)
        WARM → HOT  (再次需要时直接加载, 跳过发现)
    """

    HOT = "hot"      # 完整 schema 在 LLM 上下文中
    WARM = "warm"    # 仅摘要可见, 可快速重载
    COLD = "cold"    # 仅在 Vault 中, 需搜索发现


class ToolEntry(BaseModel):
    """ToolVault 中的工具条目

    包含工具完整定义、语义向量、上下文状态和使用追踪。

    Args:
        tool_name: 工具名称 (唯一标识)
        definition: 完整工具定义 (ToolDefinition)
        handler: 执行函数引用 (Any 类型, 因 BaseModel 不接受 Callable)
        provider_id: 来源 Provider 标识
        essential: 是否必备 (永不回收)
        embedding: 语义向量
        summary: 一句话摘要 (温态显示)
        tier: 当前上下文状态
        last_used_turn: 上次使用的对话轮次 (-1 = 从未使用)
        use_count: 总使用次数
    """

    tool_name: str = Field(description="工具名称 (唯一标识)")
    definition: ToolDefinition = Field(description="完整工具定义")
    handler: Any = Field(default=None, description="执行函数引用")
    provider_id: str = Field(default="", description="来源 Provider")
    essential: bool = Field(default=False, description="是否必备 (永不回收)")
    embedding: list[float] = Field(default_factory=list, description="语义向量")
    summary: str = Field(default="", description="一句话摘要 (温态显示)")
    tier: ToolContextTier = Field(default=ToolContextTier.COLD, description="当前上下文状态")
    last_used_turn: int = Field(default=-1, description="上次使用轮次")
    use_count: int = Field(default=0, description="总使用次数")
    version: str = Field(default="0.0.1", description="工具版本号")
    language: str = Field(default="python", description="工具实现语言")

    model_config = {"arbitrary_types_allowed": True}


class ToolSearchResult(BaseModel):
    """工具语义搜索结果

    Args:
        tool_name: 工具名称
        definition: 工具定义 (可选, 取决于调用方)
        score: 相似度分数 (0~1)
        summary: 工具摘要
    """

    tool_name: str
    definition: ToolDefinition | None = None
    score: float = Field(ge=0.0, le=1.0, description="相似度分数")
    summary: str = ""
