"""
ToolGuardianAgent — 工具全局记忆守护 Agent

继承自 Agent 基类，负责：
1. 收集来自任意 Agent 的工具调用问题汇报 (ToolIssueReport)
2. 分析问题根因（skill 描述不清、参数边界、缺失功能等）
3. 直接修改 MCP 服务中对应工具的描述/说明
4. 必要时生成具体的工具代码修改意见（含边界情况处理建议）

工作流程：
1. 其他 Agent 连接 ToolGuardianAgent（connect_guardian）
2. Agent 工具调用失败时自动汇报（report_tool_issue）
3. ToolGuardianAgent 接收汇报，分析问题类型
4. 根据问题类型执行修正：
   - UNCLEAR_DESCRIPTION → 重写工具/参数描述
   - PARAMETER_BOUNDARY → 补充参数边界说明
   - MISSING_FEATURE → 生成功能扩展建议
   - UNEXPECTED_BEHAVIOR → 补充使用注意事项
   - ERROR_HANDLING → 补充错误处理说明
5. 通过 MCPServer.update_tool_description() 直接修改工具表述
6. 将修改意见记录到历史中供后续参考

用法::

    from youmi.coordinator.tool_guardian import ToolGuardianAgent

    # 方式 1：从配置目录加载（推荐，配置存放于 youmi/agents/tool_guardian/）
    guardian = ToolGuardianAgent.from_config_dir(mcp_server=server)
    await guardian.initialize()

    # 方式 2：直接构造（使用默认配置）
    guardian = ToolGuardianAgent(mcp_server=server)
    await guardian.initialize()

    # 其他 Agent 连接 guardian
    worker_agent.connect_guardian(guardian.agent_id, broker, workflow_id)

    # guardian 监听并处理汇报
    await guardian.process_reports()

设计约定:
- ToolGuardianAgent 不参与具体任务执行，只负责工具记忆维护
- 修改工具描述时会保留原始描述作为历史参考
- 所有修改记录可追溯（_modification_history）
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any

from youmi.core.agent import Agent, AgentConfig, AgentStatus
from youmi.core.tool import ToolDefinition, ToolParameter
from youmi.core.types import (
    AgentMessage,
    AgentMetadata,
    LLMConfig,
    MemoryConfig,
    MessageRole,
)
from youmi.llm.client import LLMClient
from youmi.mcp.protocol import ToolIssueReport, ToolIssueType
from youmi.mcp.server import MCPServer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 修改记录
# ---------------------------------------------------------------------------

class ToolModification:
    """单次工具描述修改记录"""

    def __init__(
        self,
        tool_name: str,
        report: ToolIssueReport,
        old_description: str,
        new_description: str,
        param_updates: dict[str, str] | None = None,
        code_suggestion: str = "",
    ) -> None:
        self.tool_name = tool_name
        self.report = report
        self.old_description = old_description
        self.new_description = new_description
        self.param_updates = param_updates or {}
        self.code_suggestion = code_suggestion
        self.timestamp = report.timestamp

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "reporter_agent_id": self.report.reporter_agent_id,
            "issue_type": self.report.issue_type.value,
            "old_description": self.old_description,
            "new_description": self.new_description,
            "param_updates": self.param_updates,
            "code_suggestion": self.code_suggestion,
            "timestamp": self.timestamp,
        }


# ---------------------------------------------------------------------------
# ToolGuardianAgent
# ---------------------------------------------------------------------------

class ToolGuardianAgent(Agent):
    """工具全局记忆守护 Agent

    监听来自任意 Agent 的工具问题汇报，分析后修正 MCP 工具描述，
    必要时生成代码修改建议。类似 MasterAgent 的单例守护角色。

    配置存放于 youmi/agents/tool_guardian/config.yaml，
    通过 from_config_dir() 工厂方法加载。

    用法::

        server = MCPServer()
        guardian = ToolGuardianAgent.from_config_dir(mcp_server=server)
        await guardian.initialize()

        # 其他 Agent 连接
        worker.connect_guardian(guardian.agent_id, broker, workflow_id)

        # 处理汇报（可定时或按需调用）
        results = await guardian.process_reports()
    """

    # 默认系统提示词
    DEFAULT_SYSTEM_PROMPT = """你是 ToolGuardianAgent — 工具记忆守护。

你的职责是维护工具描述的质量和准确性。当其他 Agent 报告工具使用问题时，你需要：
1. 分析问题的根本原因（描述不清、参数边界未说明、缺少错误处理说明等）
2. 生成改进后的工具描述（中文描述，要清晰准确）
3. 必要时生成代码修改建议（如需要处理边界情况）

输出要求：
- 新描述要包含使用限制、参数范围、已知边界情况等关键信息
- 代码建议要具体可执行，指出需要修改的位置和方式
- 对于重复出现的问题，要在描述中添加明确的警告说明"""

    def __init__(
        self,
        mcp_server: MCPServer,
        config: AgentConfig | None = None,
        memory_strategy: str | None = None,
        llm_call: Any | None = None,
    ) -> None:
        """
        Args:
            mcp_server: MCPServer 实例，用于读写工具描述
            config: Agent 配置（可选，有合理默认值）
            memory_strategy: 记忆策略覆盖
            llm_call: LLM 调用函数覆盖
        """
        if config is None:
            config = AgentConfig(
                name="ToolGuardian",
                system_prompt=self.DEFAULT_SYSTEM_PROMPT,
                llm_config=LLMConfig(),
                memory_config=MemoryConfig(),
                metadata=AgentMetadata(
                    display_name="工具记忆守护",
                    role="tool_guardian",
                    description="收集工具调用问题，修正工具描述，生成代码修改建议",
                    capabilities=["tool_analysis", "description_update", "code_suggestion"],
                ),
            )

        super().__init__(config, memory_strategy=memory_strategy, llm_call=llm_call)

        self._mcp_server = mcp_server

        # 收集到的汇报历史: tool_name → [ToolIssueReport]
        self._reports: dict[str, list[ToolIssueReport]] = defaultdict(list)

        # 修改历史: tool_name → [ToolModification]
        self._modification_history: dict[str, list[ToolModification]] = defaultdict(list)

        # 待处理的汇报队列（从消息总线获取后暂存）
        self._pending_reports: list[ToolIssueReport] = []

        # 注册内置工具
        self._register_guardian_tools()

    # -----------------------------------------------------------------------
    # 工厂方法
    # -----------------------------------------------------------------------

    @classmethod
    def from_config_dir(
        cls,
        mcp_server: MCPServer,
        agent_name: str = "tool_guardian",
        overrides: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ToolGuardianAgent:
        """从 youmi/agents/<agent_name>/config.yaml 加载配置并创建实例

        与 MasterAgent.from_config_dir() 保持一致的配置加载方式。

        Args:
            mcp_server: MCPServer 实例，用于读写工具描述
            agent_name: Agent 配置目录名，默认 "tool_guardian"
            overrides: 覆盖配置项（优先级高于 YAML）
            **kwargs: 传给 __init__ 的额外参数

        Returns:
            ToolGuardianAgent 实例
        """
        from youmi.agents import load_agent_config

        data = load_agent_config(agent_name)

        # 应用覆盖
        if overrides:
            data.update(overrides)

        # 分离已知字段
        llm_data = data.pop("llm_config", {})
        memory_data = data.pop("memory_config", {})
        metadata_data = data.pop("metadata", {})

        config = AgentConfig(
            llm_config=LLMConfig(**llm_data),
            memory_config=MemoryConfig(**memory_data),
            metadata=AgentMetadata(**metadata_data),
            **data,
        )
        return cls(mcp_server=mcp_server, config=config, **kwargs)

    # -----------------------------------------------------------------------
    # 汇报接收与处理
    # -----------------------------------------------------------------------

    async def receive_report(self, report: ToolIssueReport) -> None:
        """直接接收一条工具问题汇报（无需消息总线，供测试和直接调用）"""
        self._reports[report.tool_name].append(report)
        self._pending_reports.append(report)
        logger.info(
            "ToolGuardian received report: tool=%s type=%s from=%s",
            report.tool_name, report.issue_type.value, report.reporter_agent_id,
        )

    async def receive_message(self, message: AgentMessage) -> None:
        """接收消息 — 自动解析 ToolIssueReport 格式的消息"""
        await super().receive_message(message)

        # 尝试解析为 ToolIssueReport
        if message.metadata.get("report_type") == "tool_issue":
            try:
                report = ToolIssueReport.model_validate_json(message.content)
                self._reports[report.tool_name].append(report)
                self._pending_reports.append(report)
                logger.info(
                    "ToolGuardian parsed report from message: tool=%s type=%s",
                    report.tool_name, report.issue_type.value,
                )
            except Exception:
                logger.warning("Failed to parse tool issue report from message", exc_info=True)

    async def process_reports(self, batch_size: int = 10) -> list[dict[str, Any]]:
        """处理待处理的汇报队列

        按工具名分组处理，对每个工具有多个汇报时合并分析。

        Args:
            batch_size: 单次最多处理的汇报数

        Returns:
            处理结果列表，每项包含工具名、修改操作、代码建议等
        """
        if not self._pending_reports:
            logger.info("ToolGuardian: no pending reports to process")
            return []

        batch = self._pending_reports[:batch_size]
        self._pending_reports = self._pending_reports[batch_size:]

        # 按工具名分组
        grouped: dict[str, list[ToolIssueReport]] = defaultdict(list)
        for report in batch:
            grouped[report.tool_name].append(report)

        results: list[dict[str, Any]] = []
        for tool_name, reports in grouped.items():
            result = await self._process_tool_reports(tool_name, reports)
            results.append(result)

        logger.info("ToolGuardian processed %d reports for %d tools",
                     len(batch), len(grouped))
        return results

    async def _process_tool_reports(
        self,
        tool_name: str,
        reports: list[ToolIssueReport],
    ) -> dict[str, Any]:
        """处理单个工具的汇报集合

        流程:
        1. 读取当前工具定义
        2. 分析问题类型集合
        3. 使用 LLM（如果有）或规则引擎生成新描述
        4. 应用修改到 MCPServer
        5. 记录修改历史

        Args:
            tool_name: 工具名
            reports: 该工具的汇报列表

        Returns:
            处理结果
        """
        result: dict[str, Any] = {
            "tool_name": tool_name,
            "report_count": len(reports),
            "issue_types": [r.issue_type.value for r in reports],
            "description_updated": False,
            "code_suggestion": "",
        }

        # 1. 读取当前工具定义
        current_defn = self._mcp_server.get_tool_definition(tool_name)
        if current_defn is None:
            result["error"] = f"工具 '{tool_name}' 在 MCPServer 中未找到"
            logger.warning(result["error"])
            return result

        old_description = current_defn.description
        old_params = {p.name: p.description for p in current_defn.parameters}

        # 2. 分析问题类型
        issue_types = [r.issue_type for r in reports]
        primary_type = max(set(issue_types), key=issue_types.count)

        # 3. 生成新描述和代码建议
        new_description, param_updates, code_suggestion = await self._generate_fix(
            tool_name=tool_name,
            current_description=old_description,
            current_params=old_params,
            reports=reports,
            primary_type=primary_type,
        )

        # 4. 应用修改到 MCPServer
        if new_description and new_description != old_description:
            success = self._mcp_server.update_tool_description(
                tool_name=tool_name,
                description=new_description,
                param_descriptions=param_updates or None,
            )
            result["description_updated"] = success
            result["old_description"] = old_description
            result["new_description"] = new_description
            result["param_updates"] = param_updates

            if success:
                logger.info("ToolGuardian updated tool '%s' description", tool_name)

        # 5. 记录代码建议
        if code_suggestion:
            result["code_suggestion"] = code_suggestion

        # 6. 记录修改历史
        modification = ToolModification(
            tool_name=tool_name,
            report=reports[0],  # 以第一条汇报为代表
            old_description=old_description,
            new_description=new_description or old_description,
            param_updates=param_updates,
            code_suggestion=code_suggestion,
        )
        self._modification_history[tool_name].append(modification)

        return result

    async def _generate_fix(
        self,
        tool_name: str,
        current_description: str,
        current_params: dict[str, str],
        reports: list[ToolIssueReport],
        primary_type: ToolIssueType,
    ) -> tuple[str, dict[str, str], str]:
        """生成工具描述修复和代码建议

        优先使用 LLM 分析，无 LLM 时退化为规则引擎。

        Returns:
            (new_description, param_updates, code_suggestion)
        """
        # 尝试使用 LLM
        if self._llm_client is not None:
            return await self._generate_fix_with_llm(
                tool_name, current_description, current_params, reports, primary_type,
            )

        # 退化为规则引擎
        return self._generate_fix_with_rules(
            tool_name, current_description, current_params, reports, primary_type,
        )

    async def _generate_fix_with_llm(
        self,
        tool_name: str,
        current_description: str,
        current_params: dict[str, str],
        reports: list[ToolIssueReport],
        primary_type: ToolIssueType,
    ) -> tuple[str, dict[str, str], str]:
        """使用 LLM 生成修复方案"""
        # 构建汇报摘要
        report_summaries = []
        for r in reports:
            report_summaries.append(
                f"- [{r.issue_type.value}] 来自 Agent {r.reporter_agent_id}: "
                f"{r.error_message} (参数: {json.dumps(r.call_arguments, ensure_ascii=False)})"
            )
            if r.suggestion:
                report_summaries.append(f"  建议: {r.suggestion}")

        prompt = f"""分析以下工具调用问题并生成修复方案。

## 工具信息
- 名称: {tool_name}
- 当前描述: {current_description}
- 当前参数描述: {json.dumps(current_params, ensure_ascii=False)}

## 问题汇报
{chr(10).join(report_summaries)}

## 主要问题类型: {primary_type.value}

## 要求
请输出 JSON 格式，包含以下字段：
1. "new_description": 改进后的工具描述（包含使用限制、边界情况等）
2. "param_updates": 参数描述更新，格式 {{"参数名": "新描述"}}（只更新需要改的参数）
3. "code_suggestion": 代码修改建议（如果不需要代码修改则为空字符串）

只输出 JSON，不要其他内容。"""

        try:
            response = await self._llm_client.chat(
                messages=[
                    {"role": "system", "content": self._config.system_prompt},
                    {"role": "user", "content": prompt},
                ],
            )

            # 尝试解析 JSON
            content = response.content.strip()
            # 去掉可能的 markdown 代码块
            if content.startswith("```"):
                content = content.split("\n", 1)[1] if "\n" in content else content[3:]
            if content.endswith("```"):
                content = content[:-3]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

            data = json.loads(content)
            return (
                data.get("new_description", current_description),
                data.get("param_updates", {}),
                data.get("code_suggestion", ""),
            )
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("ToolGuardian LLM fix generation failed: %s, falling back to rules", exc)
            return self._generate_fix_with_rules(
                tool_name, current_description, current_params, reports, primary_type,
            )

    @staticmethod
    def _generate_fix_with_rules(
        tool_name: str,
        current_description: str,
        current_params: dict[str, str],
        reports: list[ToolIssueReport],
        primary_type: ToolIssueType,
    ) -> tuple[str, dict[str, str], str]:
        """基于规则的修复方案（LLM 不可用时的退路）"""
        # 收集所有错误信息
        all_errors = [r.error_message for r in reports]
        all_suggestions = [r.suggestion for r in reports if r.suggestion]
        all_args = [r.call_arguments for r in reports]

        # 根据问题类型生成修复
        suffix_parts: list[str] = []
        param_updates: dict[str, str] = {}
        code_suggestion = ""

        if primary_type == ToolIssueType.UNCLEAR_DESCRIPTION:
            # 在描述末尾追加常见问题说明
            suffix_parts.append(
                f"\n\n[Guardian 修正] 已知问题：该工具描述可能导致误解。"
                f"常见错误: {'; '.join(set(all_errors[:3]))}"
            )
            if all_suggestions:
                suffix_parts.append(f"使用建议: {'; '.join(all_suggestions[:2])}")

        elif primary_type == ToolIssueType.PARAMETER_BOUNDARY:
            # 从错误中提取参数边界信息
            suffix_parts.append("\n\n[Guardian 修正] 参数边界注意事项：")
            for i, (err, args) in enumerate(zip(all_errors[:3], all_args[:3])):
                suffix_parts.append(f"  - 当参数为 {json.dumps(args, ensure_ascii=False)} 时可能出现: {err}")

        elif primary_type == ToolIssueType.MISSING_FEATURE:
            suffix_parts.append(
                f"\n\n[Guardian 修正] 当前不支持的场景: {'; '.join(set(all_errors[:3]))}"
            )
            code_suggestion = (
                f"工具 '{tool_name}' 需要扩展功能以处理以下场景:\n"
                + "\n".join(f"  - {err}" for err in set(all_errors[:5]))
            )

        elif primary_type == ToolIssueType.UNEXPECTED_BEHAVIOR:
            suffix_parts.append(
                f"\n\n[Guardian 修正] 已知异常行为: {'; '.join(set(all_errors[:3]))}"
            )
            code_suggestion = (
                f"工具 '{tool_name}' 存在意外行为，建议排查:\n"
                + "\n".join(f"  - {err}" for err in set(all_errors[:5]))
            )

        elif primary_type == ToolIssueType.ERROR_HANDLING:
            suffix_parts.append(
                f"\n\n[Guardian 修正] 错误处理注意事项: {'; '.join(set(all_errors[:3]))}"
            )
            code_suggestion = (
                f"工具 '{tool_name}' 需要增强错误处理:\n"
                + "\n".join(f"  - 处理: {err}" for err in set(all_errors[:5]))
            )

        new_description = current_description
        if suffix_parts:
            new_description = current_description + "\n".join(suffix_parts)

        return new_description, param_updates, code_suggestion

    # -----------------------------------------------------------------------
    # 内置工具注册
    # -----------------------------------------------------------------------

    def _register_guardian_tools(self) -> None:
        """注册 ToolGuardianAgent 专用工具"""

        # 工具: list_tool_reports
        list_reports_tool = ToolDefinition(
            name="list_tool_reports",
            description="列出所有已收集的工具问题汇报，可按工具名过滤。",
            parameters=[
                ToolParameter(
                    name="tool_name",
                    type="string",
                    description="按工具名过滤（空字符串表示列出所有）",
                    required=False,
                    default="",
                ),
            ],
        )

        async def _list_reports_handler(**kwargs: Any) -> str:
            tool_name = kwargs.get("tool_name", "")
            if tool_name:
                reports = self._reports.get(tool_name, [])
                return json.dumps({
                    "tool_name": tool_name,
                    "report_count": len(reports),
                    "reports": [r.model_dump() for r in reports],
                }, ensure_ascii=False, default=str)
            else:
                summary = {
                    name: len(reports)
                    for name, reports in self._reports.items()
                }
                return json.dumps({
                    "total_tools_with_reports": len(self._reports),
                    "reports_per_tool": summary,
                }, ensure_ascii=False)

        self._tool_registry.register(list_reports_tool, _list_reports_handler)

        # 工具: list_tool_definitions
        list_defs_tool = ToolDefinition(
            name="list_tool_definitions",
            description="列出 MCPServer 中所有工具的当前描述信息。",
            parameters=[],
        )

        async def _list_defs_handler(**kwargs: Any) -> str:
            tools = await self._mcp_server.list_tools()
            return json.dumps({
                "tools": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "provider_id": t.provider_id,
                    }
                    for t in tools
                ],
            }, ensure_ascii=False)

        self._tool_registry.register(list_defs_tool, _list_defs_handler)

        # 工具: update_tool_description
        update_tool = ToolDefinition(
            name="update_tool_description",
            description=(
                "手动更新指定工具的描述信息。"
                "可以修改工具的整体描述和参数描述。"
            ),
            parameters=[
                ToolParameter(
                    name="tool_name",
                    type="string",
                    description="要更新的工具名称",
                    required=True,
                ),
                ToolParameter(
                    name="new_description",
                    type="string",
                    description="新的工具描述",
                    required=False,
                    default="",
                ),
                ToolParameter(
                    name="param_descriptions",
                    type="object",
                    description="参数描述更新 {参数名: 新描述}",
                    required=False,
                ),
            ],
        )

        async def _update_handler(**kwargs: Any) -> str:
            tool_name = kwargs.get("tool_name", "")
            new_desc = kwargs.get("new_description", "") or None
            param_descs = kwargs.get("param_descriptions", {}) or None

            success = self._mcp_server.update_tool_description(
                tool_name=tool_name,
                description=new_desc,
                param_descriptions=param_descs,
            )

            if success:
                return json.dumps({
                    "status": "updated",
                    "tool_name": tool_name,
                }, ensure_ascii=False)
            else:
                return json.dumps({
                    "status": "failed",
                    "tool_name": tool_name,
                    "error": f"工具 '{tool_name}' 未找到或不支持修改",
                }, ensure_ascii=False)

        self._tool_registry.register(update_tool, _update_handler)

        # 工具: process_pending_reports
        process_tool = ToolDefinition(
            name="process_pending_reports",
            description="处理所有待处理的工具问题汇报，自动分析并修正工具描述。",
            parameters=[
                ToolParameter(
                    name="batch_size",
                    type="integer",
                    description="单次最多处理的汇报数（默认 10）",
                    required=False,
                    default=10,
                ),
            ],
        )

        async def _process_handler(**kwargs: Any) -> str:
            batch_size = kwargs.get("batch_size", 10)
            results = await self.process_reports(batch_size=batch_size)
            return json.dumps({
                "processed": len(results),
                "results": results,
            }, ensure_ascii=False, default=str)

        self._tool_registry.register(process_tool, _process_handler)

        # 工具: get_modification_history
        history_tool = ToolDefinition(
            name="get_modification_history",
            description="获取工具描述的修改历史记录。",
            parameters=[
                ToolParameter(
                    name="tool_name",
                    type="string",
                    description="按工具名过滤（空字符串表示所有）",
                    required=False,
                    default="",
                ),
            ],
        )

        async def _history_handler(**kwargs: Any) -> str:
            tool_name = kwargs.get("tool_name", "")
            if tool_name:
                mods = self._modification_history.get(tool_name, [])
                return json.dumps({
                    "tool_name": tool_name,
                    "modification_count": len(mods),
                    "modifications": [m.to_dict() for m in mods],
                }, ensure_ascii=False, default=str)
            else:
                summary = {
                    name: len(mods)
                    for name, mods in self._modification_history.items()
                }
                return json.dumps({
                    "total_tools_modified": len(self._modification_history),
                    "modifications_per_tool": summary,
                }, ensure_ascii=False)

        self._tool_registry.register(history_tool, _history_handler)

    # -----------------------------------------------------------------------
    # 生命周期钩子
    # -----------------------------------------------------------------------

    async def on_initialize(self) -> None:
        """初始化钩子：创建 LLM 客户端"""
        llm_cfg = self._config.llm_config
        if llm_cfg.api_key or llm_cfg.base_url:
            self._llm_client = LLMClient(llm_cfg)
            logger.info("ToolGuardianAgent LLM client created: model=%s", llm_cfg.model)

    async def on_start(self, task: str) -> None:
        logger.info("ToolGuardianAgent starting: %s", task[:100])

    async def on_stop(self, error: str | None) -> None:
        total_reports = sum(len(r) for r in self._reports.values())
        total_mods = sum(len(m) for m in self._modification_history.values())
        logger.info(
            "ToolGuardianAgent stopped. reports=%d modifications=%d",
            total_reports, total_mods,
        )

    # -----------------------------------------------------------------------
    # 诊断
    # -----------------------------------------------------------------------

    @property
    def report_count(self) -> int:
        """已收到的汇报总数"""
        return sum(len(r) for r in self._reports.values())

    @property
    def modification_count(self) -> int:
        """已执行的修改总数"""
        return sum(len(m) for m in self._modification_history.values())

    @property
    def pending_count(self) -> int:
        """待处理的汇报数"""
        return len(self._pending_reports)

    def to_summary(self) -> dict[str, Any]:
        summary = super().to_summary()
        summary["report_count"] = self.report_count
        summary["modification_count"] = self.modification_count
        summary["pending_count"] = self.pending_count
        summary["tools_with_reports"] = list(self._reports.keys())
        summary["tools_modified"] = list(self._modification_history.keys())
        return summary
