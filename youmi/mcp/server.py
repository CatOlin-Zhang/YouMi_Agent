"""
MCPServer — 统一工具路由服务器

作为所有工具调用的网关，采用插件化架构:
- 每个 ToolProvider 独立注册一组工具
- MCPServer 负责工具发现 (tools/list) 和调用路由 (tools/call)
- 进程内调用 (无需 HTTP/SSE)，未来可扩展远程通信

架构::

    MCPServer
    ├── LocalFunctionProvider (本地 Python 函数)
    ├── (未来) RemoteMCPProvider (远程 MCP Server)
    └── (未来) SubprocessProvider (子进程工具)
"""

from __future__ import annotations

import logging
import time
from typing import Any

from youmi.mcp.protocol import (
    MCPError,
    MCP_ERROR_TOOL_NOT_FOUND,
    MCPListToolsResult,
    MCPRequest,
    MCPResponse,
    MCPToolInfo,
    MCPToolResult,
    ToolContext,
)
from youmi.mcp.provider import ToolProvider

logger = logging.getLogger(__name__)


class MCPServer:
    """统一 MCP 服务器

    管理多个 ToolProvider，路由 tools/list 和 tools/call 请求。

    用法::

        server = MCPServer()

        # 注册 Provider
        local = LocalFunctionProvider()
        local.register_function(get_weather)
        server.register_provider(local)

        # 处理请求
        response = await server.handle(request)

        # 或直接用便捷方法
        tools = await server.list_tools()
        result = await server.call_tool("get_weather", {"city": "北京"}, ctx)
    """

    def __init__(self) -> None:
        self._providers: dict[str, ToolProvider] = {}
        # 工具名 → provider_id 的路由缓存
        self._tool_route: dict[str, str] = {}
        self._started: bool = False
        # 调用统计
        self._call_count: int = 0
        self._error_count: int = 0

    # ------------------------------------------------------------------
    # Provider 管理
    # ------------------------------------------------------------------

    async def register_provider(self, provider: ToolProvider) -> None:
        """注册 ToolProvider

        Args:
            provider: 工具提供者实例
        """
        pid = provider.provider_id
        if pid in self._providers:
            logger.warning("Provider '%s' already registered, replacing.", pid)
            await self.unregister_provider(pid)

        await provider.initialize()
        self._providers[pid] = provider

        # 更新路由表
        tools = await provider.get_tools()
        for tool in tools:
            self._tool_route[tool.name] = pid

        logger.info("Registered provider '%s' with %d tools: %s",
                     pid, len(tools), [t.name for t in tools])

    async def unregister_provider(self, provider_id: str) -> None:
        """注销 Provider 及其所有工具"""
        provider = self._providers.pop(provider_id, None)
        if provider is None:
            return

        # 清理路由
        tools = await provider.get_tools()
        for tool in tools:
            self._tool_route.pop(tool.name, None)

        await provider.shutdown()
        logger.info("Unregistered provider '%s'", provider_id)

    @property
    def provider_ids(self) -> list[str]:
        return list(self._providers.keys())

    @property
    def tool_count(self) -> int:
        return len(self._tool_route)

    # ------------------------------------------------------------------
    # MCP 协议处理
    # ------------------------------------------------------------------

    async def handle(self, request: MCPRequest) -> MCPResponse:
        """处理 MCP 请求 (JSON-RPC 2.0 入口)

        Args:
            request: MCP 请求

        Returns:
            MCPResponse
        """
        try:
            if request.method == "tools/list":
                return await self._handle_list_tools(request)
            elif request.method == "tools/call":
                return await self._handle_call_tool(request)
            else:
                return MCPResponse(
                    id=request.id,
                    error=MCPError(code=-32601, message=f"未知方法: {request.method}"),
                )
        except Exception as exc:
            logger.exception("MCP server error handling %s", request.method)
            return MCPResponse(
                id=request.id,
                error=MCPError(code=-32603, message=f"内部错误: {exc}"),
            )

    async def _handle_list_tools(self, request: MCPRequest) -> MCPResponse:
        """处理 tools/list"""
        all_tools: list[MCPToolInfo] = []
        for provider in self._providers.values():
            tools = await provider.get_tools()
            all_tools.extend(tools)

        result = MCPListToolsResult(tools=all_tools)
        return MCPResponse(
            id=request.id,
            result=result.model_dump(),
        )

    async def _handle_call_tool(self, request: MCPRequest) -> MCPResponse:
        """处理 tools/call"""
        self._call_count += 1
        tool_name = request.params.get("name", "")
        arguments = request.params.get("arguments", {})
        meta = request.params.get("meta", {})

        # 路由查找
        provider_id = self._tool_route.get(tool_name)
        if provider_id is None:
            self._error_count += 1
            return MCPResponse(
                id=request.id,
                error=MCPError(
                    code=MCP_ERROR_TOOL_NOT_FOUND,
                    message=f"工具 '{tool_name}' 未找到 (已注册: {list(self._tool_route.keys())})",
                ),
            )

        provider = self._providers[provider_id]
        context = ToolContext(
            agent_id=meta.get("agent_id", ""),
            task_id=meta.get("task_id", ""),
            extra=meta,
        )

        t0 = time.time()
        result = await provider.execute(tool_name, arguments, context)
        elapsed = time.time() - t0

        if result.is_error:
            self._error_count += 1

        logger.debug("tools/call '%s' → %s (%.3fs)",
                      tool_name, "error" if result.is_error else "ok", elapsed)

        return MCPResponse(
            id=request.id,
            result=result.model_dump(),
        )

    # ------------------------------------------------------------------
    # 便捷方法 (无需构造 MCPRequest)
    # ------------------------------------------------------------------

    async def list_tools(self) -> list[MCPToolInfo]:
        """列出所有可用工具"""
        all_tools: list[MCPToolInfo] = []
        for provider in self._providers.values():
            tools = await provider.get_tools()
            all_tools.extend(tools)
        return all_tools

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolContext | None = None,
    ) -> MCPToolResult:
        """直接调用工具 (便捷方法)

        Args:
            tool_name: 工具名称
            arguments: 参数
            context: 调用上下文

        Returns:
            MCPToolResult
        """
        provider_id = self._tool_route.get(tool_name)
        if provider_id is None:
            return MCPToolResult.failure(f"工具 '{tool_name}' 未找到")

        provider = self._providers[provider_id]
        ctx = context or ToolContext()
        return await provider.execute(tool_name, arguments, ctx)

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """生成 OpenAI tools 格式 schema (供 LLM function calling 使用)

        同步方法 — 从缓存的路由表中直接生成。
        需要先调用 list_tools() 或直接 register_provider() 以填充路由。
        """
        schemas: list[dict[str, Any]] = []
        for provider in self._providers.values():
            # 从 provider 内部定义生成 (LocalFunctionProvider 有 _definitions)
            definitions = getattr(provider, "_definitions", {})
            for defn in definitions.values():
                schemas.append(defn.to_openai_function_schema())
        return schemas

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动服务器"""
        self._started = True
        logger.info("MCPServer started with %d providers, %d tools",
                     len(self._providers), self.tool_count)

    async def stop(self) -> None:
        """停止服务器，注销所有 Provider"""
        for pid in list(self._providers.keys()):
            await self.unregister_provider(pid)
        self._started = False
        logger.info("MCPServer stopped")

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "providers": len(self._providers),
            "tools": self.tool_count,
            "calls": self._call_count,
            "errors": self._error_count,
        }

    def __repr__(self) -> str:
        return (
            f"<MCPServer providers={self.provider_ids} "
            f"tools={self.tool_count} calls={self._call_count}>"
        )
