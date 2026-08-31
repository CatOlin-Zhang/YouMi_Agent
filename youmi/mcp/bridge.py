"""
ToolBridge — Agent 与 MCP Server 之间的桥梁

职责:
1. 权限校验 — 检查 Agent 是否有权调用指定工具
2. 工具调用 — 通过 MCPClient 发送请求到 MCPServer
3. Schema 生成 — 为 LLM function calling 提供 tools 格式
4. 调用追踪 — 记录工具调用日志
5. 召回确认闭环 — 搜索工具后确认/否决/扩大搜索
6. 上下文注入 — 为 SubAgent 注入指定工具到 HOT 状态
7. 工具发现元工具 — search_new_tools（Vault 语义搜索 + 授权加载，
   Agent 工具不足时向 MCP 提需求，由其执行向量查询等步骤）

Agent 通过 ToolBridge 与 MCP 层交互，
不再直接持有 ToolRegistry。

用法::

    bridge = ToolBridge(
        agent_id="agent-001",
        mcp_client=mcp_client,
        allowed_tools=["get_weather", "calculate"],
        search_meta_tool=True,
    )

    # LLM 看到的 tools schema（含 search_new_tools 元工具）
    schemas = bridge.to_openai_tools()

    # 执行工具
    result = await bridge.call_tool("get_weather", {"city": "北京"})

    # 召回确认闭环
    result = await bridge.search_and_confirm("我需要一个发送邮件的工具")
    if result:
        print(f"找到工具: {result.tool_name}")
    else:
        print("没有该功能的工具")
"""

from __future__ import annotations

import json
import logging
from typing import Any

from youmi.mcp.client import MCPClient
from youmi.mcp.protocol import MCPToolInfo, MCPToolResult, ToolContext

# 延迟导入避免循环依赖
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from youmi.mcp.vault import ToolVault, ToolSearchResult
    from youmi.mcp.context import AgentToolContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# search_new_tools 元工具（工具发现入口）
# ---------------------------------------------------------------------------

SEARCH_NEW_TOOLS_NAME = "search_new_tools"
"""工具发现元工具名 — 在 ToolBridge 本地拦截执行，不经过 MCPServer 路由。

设计动机（structure.md §2 + §5.2）：任何 Agent 在工具不足时都应能
向 MCP 提需求，由 Vault 执行向量查询并返回候选；Agent 选定后加载
（自动授权 + 提升为 HOT）。schema 由 to_openai_tools() 附加，
执行由 call_tool() 拦截，因此不受 allowed_tools 白名单限制，
也不会与其他 Agent 的同名工具产生 Server 路由冲突。
"""


def _search_new_tools_schema() -> dict[str, Any]:
    """构造 search_new_tools 元工具的 OpenAI schema。

    每次调用返回新对象，避免调用方修改共享常量。
    语义与 youmi/core/agent.py 中 _register_search_new_tools 的
    ToolRegistry 版本保持一致（MCP 模式下由本 Bridge 接管）。
    """
    return {
        "type": "function",
        "function": {
            "name": SEARCH_NEW_TOOLS_NAME,
            "description": (
                "搜索发现当前尚不可用的工具。"
                "当现有工具不足以完成任务时，用自然语言描述所需能力，"
                "本工具会在工具库中执行语义/关键词检索并返回候选列表；"
                "选定后再次调用本工具并传 load=<工具名> 即可加载该工具"
                "（自动授权并注入上下文，之后可直接调用）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "自然语言描述你需要的工具功能"
                            "（load 模式下可省略）"
                        ),
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "返回候选工具数量，默认 5",
                    },
                    "load": {
                        "type": "string",
                        "description": (
                            "要加载的工具名（来自候选列表）。"
                            "提供此参数时执行加载而非搜索"
                        ),
                    },
                },
                "required": [],
            },
        },
    }


class ToolBridge:
    """Agent 与 MCP Server 之间的桥梁

    每个 Agent 实例持有一个 ToolBridge，负责:
    - 权限控制 (allowed_tools 白名单)
    - 工具调用 (委托给 MCPClient)
    - Schema 生成 (供 LLM function calling)
    - 调用追踪 (日志)
    - 工具发现 (search_new_tools 元工具，可选)

    Args:
        agent_id: Agent 唯一 ID
        mcp_client: MCP 客户端实例
        allowed_tools: 授权的工具名称列表。空列表表示不限制。
        vault: ToolVault 实例 (可选, 启用工具发现模式)
        context: AgentToolContext 实例 (可选, 启用 Agent 侧上下文状态管理)
        search_meta_tool: 是否提供 search_new_tools 工具发现元工具 (默认 False)。
            启用后 to_openai_tools() 会附加该工具 schema（不受白名单限制），
            call_tool() 对其本地拦截执行：语义搜索（Vault 向量/关键词）→
            返回候选 → load=<工具名> 加载（自动授权 + 提升为 HOT）。
    """

    def __init__(
        self,
        agent_id: str,
        mcp_client: MCPClient,
        allowed_tools: list[str] | None = None,
        vault: ToolVault | None = None,
        context: AgentToolContext | None = None,
        search_meta_tool: bool = False,
    ) -> None:
        self._agent_id = agent_id
        self._client = mcp_client
        self._allowed_tools: set[str] | None = (
            set(allowed_tools) if allowed_tools else None
        )
        self._call_count: int = 0
        # ToolVault 集成 (可选)
        self._vault = vault
        # AgentToolContext 集成 (可选, 优先于 Vault 的 tier 状态)
        self._context = context
        # 召回确认闭环状态
        self._rejected_tools: set[str] = set()
        # search_new_tools 元工具开关
        self._search_meta_tool = search_meta_tool

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def mcp_client(self) -> MCPClient:
        return self._client

    @property
    def allowed_tools(self) -> set[str] | None:
        return self._allowed_tools

    @property
    def vault(self) -> ToolVault | None:
        """ToolVault 实例 (可选)"""
        return self._vault

    @property
    def context(self) -> AgentToolContext | None:
        """AgentToolContext 实例 (可选)"""
        return self._context

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
        # search_new_tools 元工具 — 在本 Bridge 本地拦截执行，
        # 不经过 MCPServer 路由（避免多 Agent 同名工具路由冲突），
        # 也不受 allowed_tools 白名单限制（发现入口本身必须始终可用）。
        if tool_name == SEARCH_NEW_TOOLS_NAME and self._search_meta_tool:
            return await self._search_new_tools_impl(arguments)

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

        # 记录工具使用 (优先 AgentToolContext, 其次 Vault)
        if self._context is not None:
            self._context.record_usage(tool_name)
        elif self._vault is not None:
            self._vault.record_usage(tool_name)

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

        同步方法 — 优先级: AgentToolContext > ToolVault > MCPClient。
        如果启用了 AgentToolContext，只返回 Agent 的热态工具。
        如果设置了 allowed_tools，则过滤。
        启用 search_meta_tool 时，附加 search_new_tools 元工具
        （工具发现入口，不受白名单限制）。
        """
        # 优先级: AgentToolContext > ToolVault > MCPClient
        if self._context is not None:
            schemas = self._context.to_openai_tools()
        elif self._vault is not None:
            schemas = self._vault.to_openai_tools()
        else:
            schemas = self._client.to_openai_tools()

        if self._allowed_tools is None:
            result = list(schemas)
        else:
            result = [
                s for s in schemas
                if s.get("function", {}).get("name", "") in self._allowed_tools
            ]

        # search_new_tools 元工具 — 始终对 LLM 可见（工具发现入口）
        if self._search_meta_tool and not any(
            s.get("function", {}).get("name") == SEARCH_NEW_TOOLS_NAME
            for s in result
        ):
            result.append(_search_new_tools_schema())

        return result

    def to_warm_summaries(self) -> list[dict[str, str]]:
        """生成温态工具摘要 (优先 AgentToolContext, 其次 Vault)"""
        if self._context is not None:
            summaries = self._context.to_warm_summaries()
        elif self._vault is not None:
            summaries = self._vault.to_warm_summaries()
        else:
            return []

        if self._allowed_tools is not None:
            summaries = [s for s in summaries if s.get("name") in self._allowed_tools]
        return summaries

    # ------------------------------------------------------------------
    # 工具发现与动态加载 (Vault 模式)
    # ------------------------------------------------------------------

    async def discover_tools(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.3,
    ) -> list[dict[str, Any]]:
        """语义搜索工具 (仅 Vault 模式可用)

        Agent 生成对于工具功能的描述，向量匹配这些向量头拉取匹配度最高的。

        Args:
            query: 自然语言查询 (如 "我需要一个能发送通知的工具")
            top_k: 返回结果数量
            min_score: 最低相似度阈值

        Returns:
            搜索结果列表 [{"tool_name": ..., "score": ..., "summary": ...}]
        """
        if self._vault is None:
            return []

        results = await self._vault.search(query, top_k=top_k, min_score=min_score)

        # 应用 allowed_tools 过滤
        if self._allowed_tools is not None:
            results = [r for r in results if r.tool_name in self._allowed_tools]

        return [
            {
                "tool_name": r.tool_name,
                "score": r.score,
                "summary": r.summary,
            }
            for r in results
        ]

    async def load_tool(self, tool_name: str) -> bool:
        """将工具从 COLD/WARM 加载到 HOT

        Agent 选择好后 query 对应的工具内容并加载到上下文。
        WARM 工具直接加载，无需重走发现流程。
        如果启用了 AgentToolContext，委托给 context.promote()。

        Args:
            tool_name: 工具名称

        Returns:
            是否成功加载
        """
        # 优先 AgentToolContext
        if self._context is not None:
            return await self._context.promote(tool_name)

        if self._vault is None:
            return False

        entry = await self._vault.load_tool(tool_name)
        return entry is not None

    def recycle_tools(self, idle_threshold: int = 3) -> list[str]:
        """LRU 回收: 降级闲置的热态工具

        多轮对话没有调用就会回收，回收仅去除上下文的加载，
        不会回收 agent 对于这个工具的权限，仅保留工具摘要。
        优先使用 AgentToolContext.recycle()。

        Args:
            idle_threshold: 闲置轮次阈值

        Returns:
            被回收的工具名列表
        """
        if self._context is not None:
            return self._context.recycle(idle_threshold=idle_threshold)
        if self._vault is None:
            return []
        return self._vault.recycle(idle_threshold=idle_threshold)

    def advance_turn(self) -> int:
        """推进对话轮次计数器 (优先 AgentToolContext)"""
        if self._context is not None:
            return self._context.advance_turn()
        if self._vault is not None:
            return self._vault.advance_turn()
        return 0

    # ------------------------------------------------------------------
    # Vault 接入 (自动创建 Agent 侧上下文)
    # ------------------------------------------------------------------

    def attach_vault(
        self,
        vault: Any,  # ToolVault
        essential_names: set[str] | None = None,
    ) -> Any:  # AgentToolContext
        """接入共享 ToolVault，并自动创建 Agent 侧 AgentToolContext

        三级状态 (HOT/WARM/COLD) 由 Agent 侧上下文独立管理，
        Vault 只存工具定义（不同 Agent 可拥有不同的上下文视图）。

        必备工具 (永不回收) 规则:
        - 显式传入的 essential_names (如 Master 的协调器工具)
        - 当前白名单 allowed_tools（创建时被赋予的工具权限）
        其余 Vault 工具初始为 HOT（可见性仍受白名单过滤）。
        幂等：重复调用不会重建已有上下文。

        Args:
            vault: ToolVault 实例
            essential_names: 额外的必备工具名称集合

        Returns:
            AgentToolContext 实例（已初始化）
        """
        from youmi.mcp.context import AgentToolContext

        self._vault = vault
        if self._context is None:
            essential = set(essential_names or set())
            if self._allowed_tools:
                essential |= set(self._allowed_tools)

            ctx = AgentToolContext(agent_id=self._agent_id, vault=vault)
            ctx.init_tools(essential_names=essential)
            self._context = ctx
            logger.debug(
                "ToolBridge[%s]: attached vault (%d tools, %d essential)",
                self._agent_id, vault.tool_count, len(essential),
            )
        return self._context

    # ------------------------------------------------------------------
    # search_new_tools 元工具 (工具发现入口)
    # ------------------------------------------------------------------

    def _visible_tool_names(self) -> set[str]:
        """当前 Agent 已可见的工具名集合（搜索时排除，避免重复推荐）

        - 受限 Agent (allowed_tools 非空): 白名单即已可见集合
        - 无限制 Agent: AgentToolContext / Vault 中所有 HOT 工具已在上下文中
        """
        if self._allowed_tools is not None:
            return set(self._allowed_tools)
        if self._context is not None:
            return set(self._context.get_hot_tool_names())
        if self._vault is not None:
            return {e.tool_name for e in self._vault.get_hot_tools()}
        return set()

    async def _search_new_tools_impl(
        self, arguments: dict[str, Any],
    ) -> MCPToolResult:
        """执行 search_new_tools 元工具 — 在本 Bridge 上执行，不经过 MCPServer

        两种模式:
        - 搜索: query + top_k → Vault 语义/关键词检索"当前不可见"的工具，
          返回候选列表
        - 加载: load=<工具名> → 授权（白名单）+ 提升为 HOT，下一轮即可调用
        """
        self._call_count += 1

        # 加载模式
        load_name = str(arguments.get("load") or "").strip()
        if load_name:
            return await self._load_discovered_tool(load_name)

        # 搜索模式
        query = str(arguments.get("query") or "").strip()
        if not query:
            return MCPToolResult.failure("参数错误: query 与 load 至少提供一个")
        try:
            top_k = int(arguments.get("top_k", 5))
        except (TypeError, ValueError):
            top_k = 5

        exclude = self._visible_tool_names()
        candidates: list[dict[str, Any]] = []

        # 路径 1: ToolVault 语义搜索（向量 → 关键词回退）
        # 注: 挂载了 ToolStore 时 search 不做 tier 过滤，
        # 受限 Agent 也能发现 HOT 但未授权的工具
        if self._vault is not None:
            try:
                results = await self._vault.search(
                    query, top_k=top_k, min_score=0.2,
                    exclude=exclude or None,
                )
                candidates = [
                    {
                        "name": r.tool_name,
                        "score": round(r.score, 3),
                        "summary": r.summary,
                    }
                    for r in results
                ]
            except Exception as exc:
                logger.debug("ToolBridge[%s]: vault search failed: %s",
                             self._agent_id, exc)

        # 路径 2: 回退 — MCPClient 关键词匹配
        # (无 Vault，或语义搜索无结果时；纯无限制模式无新工具可发现，跳过)
        if not candidates and (
            self._vault is not None or self._allowed_tools is not None
        ):
            try:
                all_tools = await self._client.list_tools()
                query_lower = query.lower()
                for t in all_tools:
                    if t.name in exclude:
                        continue
                    desc = (t.description or "").lower()
                    # len>2 过滤英文停用词；中文双字词（最常见的词形态）
                    # 含 CJK 字符时放行，避免被误杀
                    if any(
                        kw in desc or kw in t.name.lower()
                        for kw in query_lower.split()
                        if kw and (
                            len(kw) > 2
                            or any("\u4e00" <= c <= "\u9fff" for c in kw)
                        )
                    ):
                        candidates.append({
                            "name": t.name,
                            "score": 0.5,
                            "summary": (t.description or "")[:100],
                        })
            except Exception as exc:
                logger.debug("ToolBridge[%s]: keyword fallback failed: %s",
                             self._agent_id, exc)

        candidates = candidates[:top_k]

        payload: dict[str, Any] = {
            "candidates": candidates,
            "total": len(candidates),
        }
        if candidates:
            payload["hint"] = (
                "找到以上候选工具。如需使用，再次调用本工具并传 "
                'load="<工具名>" 即可加载（自动授权并注入上下文）。'
            )
        else:
            payload["message"] = "没有找到匹配的新工具，可尝试更换关键词描述。"

        return MCPToolResult.success(json.dumps(payload, ensure_ascii=False))

    async def _load_discovered_tool(self, tool_name: str) -> MCPToolResult:
        """加载发现的工具: 授权 + 提升为 HOT

        - 受限 Agent: 自动加入白名单（自我授权，记录日志；
          正式审批流可后续接入 TOOL_REQUEST / ToolGuardian）
        - Vault 模式: 提升为 HOT 使 schema 立即可见
        """
        # 存在性校验（Vault 条目或 Server 路由）
        exists = self._vault is not None and tool_name in self._vault
        if not exists:
            try:
                all_tools = await self._client.list_tools()
                exists = any(t.name == tool_name for t in all_tools)
            except Exception:
                exists = True  # 校验失败不阻塞加载
        if not exists:
            return MCPToolResult.failure(
                f"工具 '{tool_name}' 不存在，请从候选列表中选择"
            )

        # 授权: 加入白名单（受限 Agent）
        if self._allowed_tools is not None:
            self.add_allowed_tool(tool_name)
            logger.info(
                "ToolBridge[%s]: search_new_tools 授权新工具 '%s'",
                self._agent_id, tool_name,
            )

        # 上下文: 提升为 HOT（复用 load_tool 的 context/vault 优先级）
        promoted = await self.load_tool(tool_name)
        if self._context is not None:
            self._context.record_usage(tool_name)
        elif self._vault is not None:
            self._vault.record_usage(tool_name)

        return MCPToolResult.success(json.dumps({
            "loaded": tool_name,
            "promoted_hot": promoted,
            "message": f"工具 '{tool_name}' 已加载，现在可以直接调用。",
        }, ensure_ascii=False))

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

    # ------------------------------------------------------------------
    # 召回确认闭环
    # ------------------------------------------------------------------

    async def search_and_confirm(
        self,
        query: str,
        max_retries: int = 3,
        top_k: int = 5,
        min_score: float = 0.3,
    ) -> ToolSearchResult | None:
        """搜索工具并确认闭环

        流程:
        1. 向量搜索返回 top-k
        2. 取最佳候选返回给调用方
        3. 调用方确认合适 → 自动加载到上下文并返回
        4. 调用方确认不合适 → 排除该项，扩大搜索
        5. max_retries 次后仍无合适结果 → 返回 None ("没有该功能的工具")

        注意: 此方法自动执行第 1-2 步并返回最佳候选。
        调用方可使用 confirm_search_result() / reject_search_result()
        完成闭环，或直接使用返回结果。

        Args:
            query: 自然语言查询
            max_retries: 最大重试次数
            top_k: 每次搜索结果数
            min_score: 最低相似度阈值

        Returns:
            最佳匹配的 ToolSearchResult，或 None (无匹配)
        """
        if self._vault is None:
            return None

        for attempt in range(max_retries):
            results = await self._vault.search(
                query,
                top_k=top_k,
                min_score=min_score,
                exclude=self._rejected_tools if self._rejected_tools else None,
            )

            # 应用 allowed_tools 过滤
            if self._allowed_tools is not None:
                results = [r for r in results if r.tool_name in self._allowed_tools]

            if not results:
                logger.info(
                    "ToolBridge[%s]: search_and_confirm 第%d次无结果",
                    self._agent_id, attempt + 1,
                )
                return None

            # 返回最佳候选
            best = results[0]
            logger.debug(
                "ToolBridge[%s]: search_and_confirm 候选 '%s' (score=%.3f, attempt=%d)",
                self._agent_id, best.tool_name, best.score, attempt + 1,
            )
            return best

        return None

    def confirm_search_result(self, tool_name: str) -> None:
        """确认搜索结果合适，加载到上下文并清理拒绝列表

        Args:
            tool_name: 确认的工具名称
        """
        # 添加到权限白名单
        self.add_allowed_tool(tool_name)

        # 清理拒绝列表
        self._rejected_tools.clear()

        logger.debug("ToolBridge[%s]: confirmed tool '%s'", self._agent_id, tool_name)

    def reject_search_result(self, tool_name: str) -> None:
        """否决搜索结果，将其加入排除列表

        下次 search_and_confirm() 将排除此工具。

        Args:
            tool_name: 被否决的工具名称
        """
        self._rejected_tools.add(tool_name)
        logger.debug(
            "ToolBridge[%s]: rejected tool '%s' (total rejected: %d)",
            self._agent_id, tool_name, len(self._rejected_tools),
        )

    def reset_rejected(self) -> None:
        """重置已否决工具列表 (新查询时调用)"""
        self._rejected_tools.clear()

    # ------------------------------------------------------------------
    # 上下文注入 (MCP_Agent 功能)
    # ------------------------------------------------------------------

    async def inject_tool_context(self, tool_names: list[str]) -> int:
        """为 SubAgent 注入工具上下文

        将指定工具加载到 AgentToolContext 的 HOT 状态，
        使 SubAgent 在下一轮 _think() 时能看到这些工具。

        Args:
            tool_names: 要注入的工具名称列表

        Returns:
            成功注入的工具数量
        """
        if self._context is None:
            # 退化: 仅添加到 allowed_tools
            for name in tool_names:
                self.add_allowed_tool(name)
            return len(tool_names)

        injected = 0
        for name in tool_names:
            success = await self._context.promote(name)
            if success:
                self.add_allowed_tool(name)
                injected += 1

        logger.info(
            "ToolBridge[%s]: injected %d/%d tools to context",
            self._agent_id, injected, len(tool_names),
        )
        return injected

    def __repr__(self) -> str:
        allowed = list(self._allowed_tools) if self._allowed_tools else "*"
        ctx_info = " context=True" if self._context else ""
        return (
            f"<ToolBridge agent={self._agent_id!r} "
            f"allowed={allowed} calls={self._call_count}{ctx_info}>"
        )
