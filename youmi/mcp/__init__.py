"""MCP 模块 — Model Context Protocol 统一工具调用层"""

from youmi.mcp.protocol import (
    MCPRequest,
    MCPResponse,
    MCPError,
    MCPToolInfo,
    MCPToolResult,
    MCPListToolsResult,
    MCPCallToolParams,
    ToolContext,
    MCP_ERROR_TOOL_NOT_FOUND,
    MCP_ERROR_PROVIDER_ERROR,
    MCP_ERROR_PERMISSION_DENIED,
    ToolIssueType,
    ToolIssueReport,
)
from youmi.mcp.provider import ToolProvider, LocalFunctionProvider
from youmi.mcp.server import MCPServer
from youmi.mcp.client import MCPClient
from youmi.mcp.bridge import ToolBridge
from youmi.mcp.vault import ToolVault, ToolEntry, ToolContextTier, ToolSearchResult
from youmi.mcp.tool_store import ToolStore
from youmi.mcp.context import AgentToolContext
from youmi.mcp.approval import (
    ApprovalManager,
    ApprovalLevel,
    ApprovalDecision,
    ApprovalRecord,
)

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
    "MCPClient",
    "ToolBridge",
    # ToolVault (工具发现与向量搜索)
    "ToolVault",
    "ToolEntry",
    "ToolContextTier",
    "ToolSearchResult",
    # Phase 4: 持久化存储层
    "ToolStore",
    # Phase 4: Agent 侧上下文状态
    "AgentToolContext",
    # Phase 4: 审批模块
    "ApprovalManager",
    "ApprovalLevel",
    "ApprovalDecision",
    "ApprovalRecord",
]
