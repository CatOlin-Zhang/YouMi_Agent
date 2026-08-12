"""
MCP 协议消息类型

基于 JSON-RPC 2.0 的 MCP (Model Context Protocol) 消息定义。
用于 Agent (MCPClient) 与 MCPServer 之间的工具调用通信。

支持的 MCP 方法:
- tools/list  — 列出所有可用工具
- tools/call  — 调用指定工具
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 工具问题汇报
# ---------------------------------------------------------------------------

class ToolIssueType(str, Enum):
    """工具问题分类 — Agent 向 ToolGuardianAgent 汇报时的错误类型"""

    UNCLEAR_DESCRIPTION = "unclear_description"
    PARAMETER_BOUNDARY = "parameter_boundary"
    MISSING_FEATURE = "missing_feature"
    UNEXPECTED_BEHAVIOR = "unexpected_behavior"
    ERROR_HANDLING = "error_handling"
    OTHER = "other"


class ToolIssueReport(BaseModel):
    """工具问题汇报 — 由调用工具的 Agent 构造，发送给 ToolGuardianAgent

    汇报流程:
    1. Agent 调用工具发生错误
    2. Agent 根据错误情况分类 issue_type
    3. 构造 ToolIssueReport 并发送给 ToolGuardianAgent
    4. ToolGuardianAgent 根据报告决定修改工具描述或生成代码修改建议
    """

    report_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    reporter_agent_id: str
    tool_name: str
    issue_type: ToolIssueType
    error_message: str = ""
    call_arguments: dict[str, Any] = Field(default_factory=dict)
    error_traceback: str = ""
    suggestion: str = Field(default="", description="汇报者对问题的初步建议")
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 基础
# ---------------------------------------------------------------------------

class MCPRequest(BaseModel):
    """MCP 请求 (JSON-RPC 2.0)"""

    jsonrpc: str = "2.0"
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


# 标准 JSON-RPC 错误码 (模块级常量)
MCP_ERROR_TOOL_NOT_FOUND = -32001
MCP_ERROR_PROVIDER_ERROR = -32002
MCP_ERROR_PERMISSION_DENIED = -32003


class MCPError(BaseModel):
    """MCP 错误"""

    code: int
    message: str
    data: Any = None


class MCPResponse(BaseModel):
    """MCP 响应 (JSON-RPC 2.0)"""

    jsonrpc: str = "2.0"
    id: str = ""
    result: dict[str, Any] | None = None
    error: MCPError | None = None

    @property
    def is_error(self) -> bool:
        return self.error is not None


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------

class MCPToolInfo(BaseModel):
    """MCP 工具信息 (tools/list 返回的单条工具)"""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    provider_id: str = ""


class MCPListToolsResult(BaseModel):
    """tools/list 响应结果"""

    tools: list[MCPToolInfo] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# tools/call
# ---------------------------------------------------------------------------

class MCPCallToolParams(BaseModel):
    """tools/call 请求参数"""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    meta: dict[str, Any] = Field(
        default_factory=dict,
        description="调用元数据 (agent_id, task_id, trace_id 等)",
    )


class MCPToolResult(BaseModel):
    """tools/call 响应中的工具执行结果"""

    content: list[dict[str, Any]] = Field(default_factory=list)
    is_error: bool = False

    @property
    def text(self) -> str:
        """提取第一个 text 类型内容"""
        for item in self.content:
            if item.get("type") == "text":
                return item.get("text", "")
        return ""

    @classmethod
    def success(cls, text: str) -> "MCPToolResult":
        return cls(content=[{"type": "text", "text": text}], is_error=False)

    @classmethod
    def failure(cls, message: str) -> "MCPToolResult":
        return cls(content=[{"type": "text", "text": message}], is_error=True)


# ---------------------------------------------------------------------------
# ToolContext — 传递给 ToolProvider 的执行上下文
# ---------------------------------------------------------------------------

class ToolContext(BaseModel):
    """工具执行上下文

    包含调用者身份、任务信息等，供 ToolProvider 做权限检查或日志。
    """

    agent_id: str = ""
    task_id: str = ""
    trace_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    extra: dict[str, Any] = Field(default_factory=dict)
