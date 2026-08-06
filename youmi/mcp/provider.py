"""
ToolProvider — 工具提供者抽象基类 + 内置实现

ToolProvider 是 MCP 架构中的插件扩展点:
- 每个 Provider 管理一组相关工具
- MCPServer 通过 Provider 发现和路由工具调用
- 支持本地函数、远程 API、子进程等多种实现

内置 Provider:
- LocalFunctionProvider: 包装 Python 函数为 MCP 工具
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, Awaitable

from youmi.core.tool import ToolDefinition, ToolHandler
from youmi.mcp.protocol import MCPToolInfo, MCPToolResult, ToolContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 抽象基类
# ---------------------------------------------------------------------------

class ToolProvider(ABC):
    """Tool Provider 抽象基类 — MCP 插件化扩展点

    每个 Provider 管理一组相关工具，MCPServer 通过 Provider 发现和路由。
    子类实现:
    - provider_id: 提供者唯一标识
    - get_tools(): 返回该 Provider 提供的所有工具
    - execute(): 执行指定工具
    """

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """提供者唯一标识"""
        ...

    @abstractmethod
    async def get_tools(self) -> list[MCPToolInfo]:
        """返回该 Provider 提供的所有工具信息"""
        ...

    @abstractmethod
    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> MCPToolResult:
        """执行指定工具

        Args:
            tool_name: 工具名称
            arguments: 工具参数
            context: 调用上下文

        Returns:
            MCPToolResult 工具执行结果
        """
        ...

    async def initialize(self) -> None:
        """Provider 初始化 (建立连接、加载配置等)"""
        pass

    async def shutdown(self) -> None:
        """Provider 关闭 (释放资源)"""
        pass


# ---------------------------------------------------------------------------
# 本地函数 Provider
# ---------------------------------------------------------------------------

class LocalFunctionProvider(ToolProvider):
    """本地函数 ToolProvider

    将 Python 函数包装为 MCP 工具，在进程内直接执行。
    兼容现有的 ToolDefinition + ToolRegistry 体系。

    用法::

        provider = LocalFunctionProvider(provider_id="local")

        def get_weather(city: str) -> str:
            return f"{city} 25°C"

        provider.register_function(get_weather)
        await provider.initialize()

        tools = await provider.get_tools()
        result = await provider.execute("get_weather", {"city": "北京"}, ctx)
    """

    def __init__(self, provider_id: str = "local") -> None:
        self._provider_id = provider_id
        self._definitions: dict[str, ToolDefinition] = {}
        self._handlers: dict[str, ToolHandler] = {}

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def tool_names(self) -> list[str]:
        return list(self._definitions.keys())

    def register(
        self,
        definition: ToolDefinition,
        handler: ToolHandler,
    ) -> None:
        """注册工具"""
        self._definitions[definition.name] = definition
        self._handlers[definition.name] = handler

    def register_function(
        self,
        func: ToolHandler,
        name: str = "",
        description: str = "",
        param_descriptions: dict[str, str] | None = None,
    ) -> None:
        """从函数自动提取定义并注册"""
        defn = ToolDefinition.from_function(
            func, name=name, description=description,
            param_descriptions=param_descriptions,
        )
        self.register(defn, handler=func)

    def unregister(self, name: str) -> None:
        """注销工具"""
        self._definitions.pop(name, None)
        self._handlers.pop(name, None)

    async def get_tools(self) -> list[MCPToolInfo]:
        tools: list[MCPToolInfo] = []
        for defn in self._definitions.values():
            tools.append(MCPToolInfo(
                name=defn.name,
                description=defn.description,
                input_schema=defn.to_openai_function_schema()["function"]["parameters"],
                provider_id=self._provider_id,
            ))
        return tools

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ToolContext,
    ) -> MCPToolResult:
        handler = self._handlers.get(tool_name)
        if handler is None:
            return MCPToolResult.failure(f"工具 '{tool_name}' 在 provider '{self._provider_id}' 中未找到")

        try:
            if asyncio.iscoroutinefunction(handler):
                result = await handler(**arguments)
            else:
                result = handler(**arguments)

            result_str = result if isinstance(result, str) else str(result)
            return MCPToolResult.success(result_str)

        except Exception as exc:
            logger.warning("Tool '%s' execution error: %s", tool_name, exc)
            return MCPToolResult.failure(f"工具执行异常: {exc}")

    def __len__(self) -> int:
        return len(self._definitions)

    def __repr__(self) -> str:
        return f"<LocalFunctionProvider id={self._provider_id!r} tools={self.tool_names}>"
