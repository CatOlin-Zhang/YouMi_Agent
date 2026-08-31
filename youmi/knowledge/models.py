"""
全局记忆数据模型

Phase 6: 跨任务的工具使用经验知识库。
经验专供工具管理 Agent（如 ToolGuardian）诊断和修复工具问题使用，
不注入子 Agent prompt。

数据模型:
- KnowledgeEntry: 单条知识条目 (工具经验 / 任务模式 / 修复记录)
- ToolKnowledge: 单个工具的聚合知识 (正确用法 / 已知问题 / 修复历史)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class KnowledgeCategory(str, Enum):
    """知识条目类别"""

    TOOL_EXPERIENCE = "tool_experience"   # 工具使用经验 (成功模式 / 失败原因)
    TASK_PATTERN = "task_pattern"         # 任务执行模式
    BUG_FIX = "bug_fix"                   # 修复记录


class KnowledgeEntry(BaseModel):
    """单条全局知识条目

    Attributes:
        entry_id: 唯一 ID
        category: 知识类别
        tool_name: 关联工具名（任务模式类条目可为空）
        content: 经验描述文本
        embedding: content 的语义向量 (None = 未向量化)
        source_task_id: 来源任务 ID
        source_agent_id: 来源 Agent ID
        success_rate: 关联的工具调用成功率 (0.0 ~ 1.0)
        resolved: 是否已被修复 (仅 bug 类经验有意义)
        resolution: 修复说明 (resolved=True 时填写)
        metadata: 扩展字段
        created_at: 创建时间
        updated_at: 更新时间
    """

    entry_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    category: KnowledgeCategory = KnowledgeCategory.TOOL_EXPERIENCE
    tool_name: str = ""
    content: str = ""
    embedding: list[float] | None = None
    source_task_id: str = ""
    source_agent_id: str = ""
    success_rate: float = 0.0
    resolved: bool = False
    resolution: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def is_bug(self) -> bool:
        """是否为未解决的 bug 类经验"""
        return self.category == KnowledgeCategory.TOOL_EXPERIENCE and not self.resolved


class ToolKnowledge(BaseModel):
    """单个工具的聚合知识

    由 GlobalMemory.get_tool_knowledge() 从多条 KnowledgeEntry 聚合而成，
    供工具管理 Agent 快速了解某工具的历史使用情况。

    Attributes:
        tool_name: 工具名称
        best_practices: 正确调用方式描述列表
        known_issues: 已知问题与边界条件 (未解决的)
        resolved_issues: 已修复的问题及其修复方案
        fix_history: 修复历史记录
        entry_ids: 关联的 KnowledgeEntry ID 列表
    """

    tool_name: str
    best_practices: list[str] = Field(default_factory=list)
    known_issues: list[str] = Field(default_factory=list)
    resolved_issues: list[str] = Field(default_factory=list)
    fix_history: list[str] = Field(default_factory=list)
    entry_ids: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """是否为空知识 (无任何经验记录)"""
        return not (
            self.best_practices or self.known_issues
            or self.resolved_issues or self.fix_history
        )


__all__ = [
    "KnowledgeCategory",
    "KnowledgeEntry",
    "ToolKnowledge",
]
