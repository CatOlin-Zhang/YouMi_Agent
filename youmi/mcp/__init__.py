"""MCP 模块 — sidecar 的协议、存储与能力 Provider

宿主侧的 MCPClient / ToolBridge / ApprovalManager 已移除：
连接由 DSH 的 dsh-mcp-client 负责，审批由 DSH 的 approval 机制负责。
"""

from youmi.mcp.protocol import (
    MCPCallToolParams,
    MCPError,
    MCPListToolsResult,
    MCPRequest,
    MCPResponse,
    MCPToolInfo,
    MCPToolResult,
    ToolContext,
    ToolIssueReport,
    ToolIssueType,
    MCP_ERROR_PERMISSION_DENIED,
    MCP_ERROR_PROVIDER_ERROR,
    MCP_ERROR_TOOL_NOT_FOUND,
)
from youmi.mcp.provider import LocalFunctionProvider, ToolProvider
from youmi.mcp.server import MCPServer
from youmi.mcp.skill_store import SkillEntry, SkillSearchResult, SkillStore
from youmi.mcp.tool_store import ToolStore
from youmi.mcp.vault import ToolContextTier, ToolEntry, ToolSearchResult, ToolVault
from youmi.mcp.version_router import CallPathRouter, CallPathSource

__all__ = [
    "MCPRequest",
    "MCPResponse",
    "MCPError",
    "MCPToolInfo",
    "MCPToolResult",
    "MCPListToolsResult",
    "MCPCallToolParams",
    "ToolContext",
    "MCP_ERROR_TOOL_NOT_FOUND",
    "MCP_ERROR_PROVIDER_ERROR",
    "MCP_ERROR_PERMISSION_DENIED",
    "ToolIssueType",
    "ToolIssueReport",
    "ToolProvider",
    "LocalFunctionProvider",
    "MCPServer",
    # 工具目录
    "ToolVault",
    "ToolEntry",
    "ToolContextTier",
    "ToolSearchResult",
    "ToolStore",
    # Skill 索引
    "SkillStore",
    "SkillEntry",
    "SkillSearchResult",
    # 版本路由
    "CallPathRouter",
    "CallPathSource",
]
