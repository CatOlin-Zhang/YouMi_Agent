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
from typing import Any

from pydantic import BaseModel, Field


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
