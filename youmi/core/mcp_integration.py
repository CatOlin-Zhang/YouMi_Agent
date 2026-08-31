"""
MCP 集成 Mixin

从 youmi/core/agent.py 提取，包含 Agent 与 MCP/ToolGuardian 集成相关方法：
- connect_guardian     — 连接 ToolGuardianAgent
- report_tool_issue    — 向 ToolGuardian 汇报工具问题
- _classify_tool_error — 错误分类（静态方法）
- connect_mcp          — 连接 MCP Server
- reset_tool_permissions — 重置工具权限
- _register_search_new_tools — 注册 search_new_tools 兜底工具

使用方式：
    class Agent(ToolExecutionMixin, MCPIntegrationMixin):
        ...
"""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

from youmi.core.types import MessageRole

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class MCPIntegrationMixin:
    """MCP 集成 Mixin — 为 Agent 提供 MCP 连接、ToolGuardian 汇报、工具发现能力

    依赖 Agent 实例的以下属性（通过继承链提供）:
    - _tool_guardian_id: str
    - _tool_guardian_bus: MessageBroker | None
    - _guardian_workflow_id: str
    - _bus: MessageBroker | None
    - _workflow_id: str
    - _tool_bridge: ToolBridge | None
    - _tool_registry: ToolRegistry
    - _config: AgentConfig
    - _env: str
    - _initial_allowed_tools: set[str] | None
    - _mcp_server / _mcp_provider / _mcp_pending_provider
    - agent_id / name: str (property)
    - _resolve_builtin_exclude() (Agent 方法)
    """

    def connect_guardian(
        self,
        guardian_id: str,
        broker: Any = None,  # MessageBroker
        workflow_id: str = "",
    ) -> None:
        """连接 ToolGuardianAgent，启用工具错误汇报闭环

        连接后，Agent 在工具调用失败时会自动分类错误并汇报给 ToolGuardianAgent。
        ToolGuardianAgent 根据汇报修正工具描述或生成代码修改建议。

        Args:
            guardian_id: ToolGuardianAgent 的 agent_id
            broker: 用于向 guardian 发送消息的 MessageBroker（可复用 connect_bus 的 broker）
            workflow_id: 工作流 ID（可复用 connect_bus 的 workflow_id）
        """
        self._tool_guardian_id = guardian_id
        self._tool_guardian_bus = broker or self._bus
        self._guardian_workflow_id = workflow_id or self._workflow_id
        logger.info(
            "Agent '%s' connected to ToolGuardian '%s'",
            self.name, guardian_id,
        )

    async def report_tool_issue(
        self,
        tool_name: str,
        error_message: str,
        call_arguments: dict[str, Any] | None = None,
        error_traceback: str = "",
        issue_type: str | None = None,
        suggestion: str = "",
    ) -> None:
        """向 ToolGuardianAgent 汇报工具调用问题

        自动分类错误类型（或由调用方显式指定 issue_type），
        构造 ToolIssueReport 并通过消息总线发送给 ToolGuardianAgent。

        错误分类规则（issue_type 为 None 时自动推断）:
        - 包含 "not found" / "未注册" → UNCLEAR_DESCRIPTION
        - 包含 "boundary" / "out of range" / "invalid" / "类型" / "边界" → PARAMETER_BOUNDARY
        - 包含 "not supported" / "不支持" → MISSING_FEATURE
        - 包含 "timeout" / "connection" / "超时" → ERROR_HANDLING
        - 其他 → UNEXPECTED_BEHAVIOR

        Args:
            tool_name: 出问题的工具名称
            error_message: 错误信息
            call_arguments: 调用时的参数
            error_traceback: 完整异常 traceback（可选）
            issue_type: 显式指定问题类型（ToolIssueType 值），None 则自动推断
            suggestion: 汇报者的初步修改建议
        """
        from youmi.mcp.protocol import ToolIssueType, ToolIssueReport

        # 自动推断 issue_type
        if issue_type is None:
            issue_type = self._classify_tool_error(error_message)
        elif isinstance(issue_type, str):
            try:
                issue_type = ToolIssueType(issue_type)
            except ValueError:
                issue_type = ToolIssueType.OTHER

        report = ToolIssueReport(
            reporter_agent_id=self.agent_id,
            tool_name=tool_name,
            issue_type=issue_type,
            error_message=error_message,
            call_arguments=call_arguments or {},
            error_traceback=error_traceback,
            suggestion=suggestion,
        )

        logger.info(
            "Agent '%s' reporting tool issue: tool=%s type=%s error=%s",
            self.name, tool_name, issue_type.value, error_message[:80],
        )

        # 通过消息总线发送给 ToolGuardianAgent
        if self._tool_guardian_bus is not None and self._tool_guardian_id:
            from youmi.bus.message import WorkflowMessage, WorkflowMessageType
            wf_msg = WorkflowMessage(
                workflow_id=getattr(self, '_guardian_workflow_id', self._workflow_id),
                from_agent_id=self.agent_id,
                to_agent_id=self._tool_guardian_id,
                msg_type=WorkflowMessageType.FEEDBACK,
                role=MessageRole.AGENT,
                content=report.model_dump_json(),
                metadata={
                    "report_type": "tool_issue",
                    "tool_name": tool_name,
                    "issue_type": issue_type.value,
                },
            )
            await self._tool_guardian_bus.publish(wf_msg)
        else:
            logger.warning(
                "Agent '%s' cannot report tool issue: guardian not connected",
                self.name,
            )

    @staticmethod
    def _classify_tool_error(error_message: str) -> Any:
        """根据错误信息自动推断工具问题类型"""
        from youmi.mcp.protocol import ToolIssueType

        msg_lower = error_message.lower()

        if any(kw in msg_lower for kw in ("not found", "未注册", "未找到", "不存在")):
            return ToolIssueType.UNCLEAR_DESCRIPTION

        if any(kw in msg_lower for kw in (
            "boundary", "out of range", "invalid", "type error",
            "类型", "边界", "超出范围", "参数错误", "不在", "列表中",
        )):
            return ToolIssueType.PARAMETER_BOUNDARY

        if any(kw in msg_lower for kw in ("not supported", "不支持", "未实现")):
            return ToolIssueType.MISSING_FEATURE

        if any(kw in msg_lower for kw in ("timeout", "connection", "超时", "连接")):
            return ToolIssueType.ERROR_HANDLING

        return ToolIssueType.UNEXPECTED_BEHAVIOR

    def connect_mcp(
        self,
        server: Any,  # MCPServer
        provider_id: str = "local",
        builtin_tools: bool = True,
        search_meta_tool: bool = True,
    ) -> None:
        """连接 MCP Server，启用统一工具调用层

        连接后:
        - Agent 通过 ToolBridge 调用工具 (权限 + MCP 路由)
        - 已注册的 ToolRegistry 工具自动迁移到 MCP Provider
        - 内置工具按 config.tools 声明装配（无声明时全量注册）
        - _think() 和 _execute_tool_call() 自动切换为 MCP 模式
        - search_new_tools 元工具由 ToolBridge 直接提供（schema + 本地
          拦截执行），Agent 工具不足时可自主发现并向 MCP 申请加载

        Args:
            server: MCPServer 实例
            provider_id: 本地工具 Provider 的 ID
            builtin_tools: 是否自动注册内置工具，默认 True
            search_meta_tool: 是否启用 search_new_tools 工具发现元工具，
                默认 True（Agent 可通过语义/关键词搜索发现新工具并授权加载）
        """
        from youmi.mcp.provider import LocalFunctionProvider
        from youmi.mcp.client import MCPClient
        from youmi.mcp.bridge import ToolBridge

        # 创建 LocalFunctionProvider，迁移已有工具
        provider = LocalFunctionProvider(provider_id=provider_id)
        for name, defn in self._tool_registry._definitions.items():
            handler = self._tool_registry._handlers.get(name)
            if handler:
                provider.register(defn, handler)

        # 注册内置工具 — 按 config.tools.builtin 声明装配
        if builtin_tools:
            from youmi.tools.builtin import BuiltinToolProvider

            # 计算排除列表（复用 register_builtin_tools 的逻辑）
            effective_exclude = self._resolve_builtin_exclude(None)
            bp = BuiltinToolProvider(work_dir=self._env, exclude=effective_exclude)

            registered = 0
            for name, defn in bp._definitions.items():
                if name not in provider._definitions:  # 不覆盖已有工具
                    handler = bp._handlers.get(name)
                    if handler:
                        provider.register(defn, handler)
                        # 同步到 ToolRegistry
                        self._tool_registry.register(defn, handler)
                        registered += 1

            logger.debug(
                "Agent '%s' assembled %d builtin tools via MCP (exclude=%s)",
                self.name, registered, effective_exclude,
            )

        # 注册到 MCPServer (异步操作在 initialize 或首次调用时完成)
        self._mcp_server = server
        self._mcp_provider = provider

        # 创建 MCPClient + ToolBridge
        client = MCPClient(server=server)
        allowed = self._config.allowed_tools or None
        self._tool_bridge = ToolBridge(
            agent_id=self.agent_id,
            mcp_client=client,
            allowed_tools=allowed,
            search_meta_tool=search_meta_tool,
        )
        # 保存初始工具权限快照（用于工作流级权限回收）
        self._initial_allowed_tools = (
            set(allowed) if allowed else None
        )

        self._tool_bridge._provider = provider  # 保留引用供 register_tool 使用
        self._mcp_pending_provider = provider  # 标记需要在 initialize 时注册

        logger.info("Agent '%s' connected to MCP (provider=%s, builtin=%s)",
                     self.name, provider_id, builtin_tools)

        # 注册 search_new_tools 兜底工具（structure.md §2 + §5.2）
        self._register_search_new_tools()

    def reset_tool_permissions(self) -> None:
        """重置工具权限到初始状态（工作流级回收）

        将 ToolBridge 的 allowed_tools 恢复为初始配置值。
        由 MasterAgent 在工作流结束后调用（structure.md §2 权限回收策略）。
        """
        if self._tool_bridge is not None:
            if self._initial_allowed_tools is not None:
                self._tool_bridge._allowed_tools = set(self._initial_allowed_tools)
            else:
                self._tool_bridge._allowed_tools = None
            logger.info(
                "Agent '%s' tool permissions reset to initial state",
                self.name,
            )

    def _register_search_new_tools(self) -> None:
        """注册 search_new_tools 兜底工具 (structure.md §2 + §5.2)

        MCP 模式下该元工具由 ToolBridge 直接提供（schema 注入 +
        call_tool 本地拦截执行，见 youmi/mcp/bridge.py）。此处注册到
        ToolRegistry 主要用于:
        1. 文本回退工具调用检测（chat_turn_stream 依据 registry
           的 tool_names 从 LLM 文本中解析工具调用）
        2. Agent 工具不足时的关键词搜索兜底
        """
        from youmi.core.tool import ToolDefinition, ToolParameter

        SEARCH_NEW_TOOLS_DEF = ToolDefinition(
            name="search_new_tools",
            description=(
                "搜索发现当前可用但尚未授权的新工具。"
                "当你觉得当前工具不足以完成任务时，调用此工具搜索可用工具。"
                "返回候选工具列表，你可以选择需要的工具并通过消息总线申请授权。"
            ),
            parameters=[
                ToolParameter(
                    name="query",
                    type="string",
                    description="自然语言描述你需要的工具功能",
                    required=True,
                ),
                ToolParameter(
                    name="top_k",
                    type="integer",
                    description="返回候选工具数量",
                    required=False,
                    default=5,
                ),
            ],
        )

        async def _search_new_tools(query: str, top_k: int = 5) -> str:
            results: list[dict[str, Any]] = []

            # 路径 1: ToolBridge + ToolVault 向量搜索 (MCP 模式)
            if self._tool_bridge is not None:
                vault = getattr(self._tool_bridge, '_vault', None)
                if vault is not None:
                    try:
                        search_results = await vault.search(
                            query, top_k=top_k, min_score=0.2,
                        )
                        current = self._tool_bridge.allowed_tools or set()
                        for r in search_results:
                            if r.tool_name not in current:
                                results.append({
                                    "name": r.tool_name,
                                    "score": round(r.score, 3),
                                    "summary": r.summary,
                                })
                    except Exception as exc:
                        logger.debug("Vault search failed: %s", exc)

                # 回退: 检查 provider 中未在白名单中的工具
                if not results:
                    try:
                        all_tools = await self._tool_bridge.mcp_client.list_tools()
                        current = self._tool_bridge.allowed_tools or set()
                        query_lower = query.lower()
                        for t in all_tools:
                            if t.name not in current:
                                desc = getattr(t, 'description', '')
                                if any(kw in desc.lower() for kw in query_lower.split() if len(kw) > 2):
                                    results.append({
                                        "name": t.name,
                                        "score": 0.5,
                                        "summary": desc[:100],
                                    })
                    except Exception:
                        pass

            # 路径 2: ToolRegistry 关键词搜索 (非 MCP 模式)
            if not results and self._tool_registry:
                all_defs = self._tool_registry._definitions
                query_lower = query.lower()
                for name, defn in all_defs.items():
                    desc = defn.description.lower()
                    if any(kw in desc or kw in name.lower()
                           for kw in query_lower.split() if len(kw) > 2):
                        results.append({
                            "name": name,
                            "score": 0.5,
                            "summary": defn.description[:100],
                        })

            return json.dumps(
                {"candidates": results[:top_k], "total": len(results)},
                ensure_ascii=False,
            )

        if self._tool_registry and "search_new_tools" not in self._tool_registry:
            self._tool_registry.register(SEARCH_NEW_TOOLS_DEF, _search_new_tools)
            logger.debug("Agent '%s' registered search_new_tools fallback", self.name)
