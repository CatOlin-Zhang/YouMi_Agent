"""
ToolBridge — Agent 与 MCP Server 之间的桥梁

职责:
1. 权限校验 — 检查 Agent 是否有权调用指定工具
2. 工具调用 — 通过 MCPClient 发送请求到 MCPServer
3. Schema 生成 — 为 LLM function calling 提供 tools 格式
4. 调用追踪 — 记录工具调用日志

Agent 通过 ToolBridge 与 MCP 层交互，
不再直接持有 ToolRegistry。

用法::

    bridge = ToolBridge(
        agent_id="agent-001",
        mcp_client=mcp_client,
        allowed_tools=["get_weather", "calculate"],
    )

    # LLM 看到的 tools schema
    schemas = bridge.to_openai_tools()

    # 执行工具
    result = await bridge.call_tool("get_weather", {"city": "北京"})
"""

from __future__ import annotations

import logging
from typing import Any

from youmi.mcp.client import MCPClient
from youmi.mcp.protocol import MCPToolInfo, MCPToolResult, ToolContext

logger = logging.getLogger(__name__)


class ToolBridge:
    """Agent 与 MCP Server 之间的桥梁

    每个 Agent 实例持有一个 ToolBridge，负责:
    - 权限控制 (allowed_tools 白名单)
    - 工具调用 (委托给 MCPClient)
    - Schema 生成 (供 LLM function calling)
    - 调用追踪 (日志)

    Args:
        agent_id: Agent 唯一 ID
        mcp_client: MCP 客户端实例
        allowed_tools: 授权的工具名称列表。空列表表示不限制。
    """

    def __init__(
        self,
        agent_id: str,
        mcp_client: MCPClient,
        allowed_tools: list[str] | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._client = mcp_client
        self._allowed_tools: set[str] | None = (
            set(allowed_tools) if allowed_tools else None
        )
        self._call_count: int = 0

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def mcp_client(self) -> MCPClient:
        return self._client

    @property
    def allowed_tools(self) -> set[str] | None:
        return self._allowed_tools

    # ------------------------------------------------------------------
    # 工具调用
    # ------------------------------------------------------------------

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        task_id: str = "",
    ) -> MCPToolResult:
        """调用工具

        流程:
        1. 权限校验 (allowed_tools 白名单)
        2. 通过 MCPClient 发送 tools/call 请求
        3. 记录调用日志

        Args:
            tool_name: 工具名称
            arguments: 工具参数
            task_id: 当前任务 ID

        Returns:
            MCPToolResult
        """
        # 权限检查
        if not self._check_permission(tool_name):
            msg = f"权限拒绝: Agent '{self._agent_id}' 无权调用工具 '{tool_name}'"
            logger.warning(msg)
            return MCPToolResult.failure(msg)

        self._call_count += 1
        context = ToolContext(agent_id=self._agent_id, task_id=task_id)

        logger.debug("ToolBridge: %s → %s(%s)",
                      self._agent_id, tool_name, arguments)

        result = await self._client.call_tool(tool_name, arguments, context)

        if result.is_error:
            logger.debug("ToolBridge: %s ← ERROR: %s",
                          tool_name, result.text[:100])
        else:
            logger.debug("ToolBridge: %s ← OK: %s",
                          tool_name, result.text[:100])

        return result

    # ------------------------------------------------------------------
    # Schema 生成
    # ------------------------------------------------------------------

    async def list_tools(self) -> list[MCPToolInfo]:
        """列出 Agent 有权使用的所有工具"""
        all_tools = await self._client.list_tools()
        if self._allowed_tools is None:
            return all_tools
        return [t for t in all_tools if t.name in self._allowed_tools]

    def to_openai_tools(self) -> list[dict[str, Any]]:
        """生成 OpenAI tools 格式 schema (仅包含授权工具)

        同步方法 — 从 MCPClient/Server 缓存中获取。
        如果设置了 allowed_tools，则过滤。
        """
        all_schemas = self._client.to_openai_tools()
        if self._allowed_tools is None:
            return all_schemas

        return [
            s for s in all_schemas
            if s.get("function", {}).get("name", "") in self._allowed_tools
        ]

    # ------------------------------------------------------------------
    # 权限管理
    # ------------------------------------------------------------------

    def _check_permission(self, tool_name: str) -> bool:
        """检查工具调用权限"""
        if self._allowed_tools is None:
            return True  # 未设置白名单 = 允许所有
        return tool_name in self._allowed_tools

    def add_allowed_tool(self, tool_name: str) -> None:
        """添加授权工具"""
        if self._allowed_tools is None:
            self._allowed_tools = set()
        self._allowed_tools.add(tool_name)

    def remove_allowed_tool(self, tool_name: str) -> None:
        """移除授权工具"""
        if self._allowed_tools is not None:
            self._allowed_tools.discard(tool_name)

    # ------------------------------------------------------------------
    # 诊断
    # ------------------------------------------------------------------

    @property
    def call_count(self) -> int:
        return self._call_count

    def __repr__(self) -> str:
        allowed = list(self._allowed_tools) if self._allowed_tools else "*"
        return (
            f"<ToolBridge agent={self._agent_id!r} "
            f"allowed={allowed} calls={self._call_count}>"
        )
