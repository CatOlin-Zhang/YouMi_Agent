"""
MCPClient — MCP 进程内客户端

Agent 通过 MCPClient 与 MCPServer 通信。
当前实现为进程内直接调用 (零序列化开销)，
未来可扩展为 HTTP/SSE 远程调用。

用法::

    client = MCPClient(server=mcp_server)

    # 列出工具
    tools = await client.list_tools()

    # 调用工具
    result = await client.call_tool("get_weather", {"city": "北京"})

    await client.close()
"""

from __future__ import annotations

import logging
from typing import Any

from youmi.mcp.protocol import (
    MCPRequest,
    MCPResponse,
    MCPToolInfo,
    MCPToolResult,
    ToolContext,
)
from youmi.mcp.server import MCPServer

logger = logging.getLogger(__name__)


class MCPClient:
    """MCP 客户端 — Agent 与 MCPServer 之间的通信接口

    当前为进程内调用 (直接引用 MCPServer)，
    未来可替换为 HTTP/SSE 远程传输。

    Args:
        server: MCPServer 实例
    """

    def __init__(self, server: MCPServer) -> None:
        self._server = server

    @property
    def server(self) -> MCPServer:
        return self._server

    async def list_tools(self) -> list[MCPToolInfo]:
        """列出所有可用工具

        发送 tools/list 请求到 MCPServer。
        """
        request = MCPRequest(method="tools/list")
        response = await self._server.handle(request)

        if response.is_error:
            logger.error("tools/list failed: %s", response.error)
            return []

        tools_data = response.result.get("tools", []) if response.result else []
        return [MCPToolInfo(**t) for t in tools_data]

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolContext | None = None,
    ) -> MCPToolResult:
        """调用工具

        发送 tools/call 请求到 MCPServer。

        Args:
            tool_name: 工具名称
            arguments: 工具参数
            context: 调用上下文 (agent_id, task_id 等)

        Returns:
            MCPToolResult 工具执行结果
        """
        params: dict[str, Any] = {
            "name": tool_name,
            "arguments": arguments,
        }
        if context:
            params["meta"] = {
                "agent_id": context.agent_id,
                "task_id": context.task_id,
                "trace_id": context.trace_id,
                **context.extra,
            }

        request = MCPRequest(method="tools/call", params=params)
        response = await self._server.handle(request)

        if response.is_error:
            return MCPToolResult.failure(
                f"MCP 错误 [{response.error.code}]: {response.error.message}"
            )

        if response.result:
            return MCPToolResult(**response.result)

        return MCPToolResult.failure("MCP 服务器返回空结果")

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """获取 OpenAI tools 格式 schema (代理到 server)"""
        return self._server.to_openai_tools()

    async def close(self) -> None:
        """关闭客户端 (进程内模式无需清理)"""
        pass

    def __repr__(self) -> str:
        return f"<MCPClient server={self._server!r}>"
