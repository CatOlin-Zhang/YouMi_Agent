"""YouMi Agent — DSH 的能力 sidecar

master 分支保留完整的多 Agent 框架；dsh-shell 分支只保留
DSH 缺失的四类能力，通过 MCP (stdio) 暴露给 DSH 宿主：

- 工具目录检索（ToolStore / ToolVault / ConeRetriever）
- Skill 索引（SkillStore / SkillIngest）
- 计划复用（PlanMemory）
- 失败经验（GlobalMemory / ToolExperienceExtractor）

宿主职责（agent loop、session log、工具执行、subagent、approval、
sandbox、UI）全部由 DSH 承担，不在本包内。
"""

from youmi.core.tool import (
    RiskLevel,
    ToolDefinition,
    ToolParameter,
    ToolRegistry,
    ToolVersion,
    bump_version,
    risk_rank,
)
from youmi.knowledge import (
    GlobalMemory,
    KnowledgeCategory,
    KnowledgeEntry,
    ToolExperience,
    ToolExperienceExtractor,
    ToolKnowledge,
)
from youmi.llm.embeddings import EmbeddingClient, EmbeddingError
from youmi.mcp import (
    CallPathRouter,
    CallPathSource,
    LocalFunctionProvider,
    MCPServer,
    SkillEntry,
    SkillSearchResult,
    SkillStore,
    ToolContextTier,
    ToolEntry,
    ToolProvider,
    ToolSearchResult,
    ToolStore,
    ToolVault,
)
from youmi.plan import PlanMemory, StepStatus, WorkflowPlan, WorkflowStep

__all__ = [
    # 工具定义与版本规则
    "RiskLevel",
    "ToolDefinition",
    "ToolParameter",
    "ToolRegistry",
    "ToolVersion",
    "bump_version",
    "risk_rank",
    # 工具目录
    "ToolStore",
    "ToolVault",
    "ToolEntry",
    "ToolContextTier",
    "ToolSearchResult",
    # Skill 索引
    "SkillStore",
    "SkillEntry",
    "SkillSearchResult",
    # 版本路由
    "CallPathRouter",
    "CallPathSource",
    # 计划复用
    "PlanMemory",
    "WorkflowPlan",
    "WorkflowStep",
    "StepStatus",
    # 失败经验
    "GlobalMemory",
    "KnowledgeCategory",
    "KnowledgeEntry",
    "ToolKnowledge",
    "ToolExperience",
    "ToolExperienceExtractor",
    # Embedding
    "EmbeddingClient",
    "EmbeddingError",
    # MCP 基础
    "MCPServer",
    "ToolProvider",
    "LocalFunctionProvider",
]

__version__ = "0.2.0"
