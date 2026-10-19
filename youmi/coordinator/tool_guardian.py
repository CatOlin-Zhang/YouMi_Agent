"""
ToolGuardianAgent — 工具全局记忆守护 Agent

继承自 Agent 基类，负责：
1. 收集来自任意 Agent 的工具调用问题汇报 (ToolIssueReport)
2. 分析问题根因（skill 描述不清、参数边界、缺失功能等）
3. 直接修改 MCP 服务中对应工具的描述/说明
4. 必要时生成具体的工具代码修改意见（含边界情况处理建议）
5. P6 全局记忆闭环：接入 GlobalMemory 后，
   - 修复前查询该工具的历史经验作为修复上下文（known_issues/fix_history）
   - 修复成功后写入 BUG_FIX 经验，并将历史未解决问题标记 resolved
6. 回归闭环（版本规则）：
   - 动手修复前收集现场信息与历史提交（版本链 + 代码库最近提交）
   - 自动修复无实质变更时进入重试队列，累计 AUTO_FIX_MAX_ATTEMPTS 次仍失败才转人工审批
   - 修复成功后按修订号提交新版本（仅修 bug，不提升功能位）

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
7. 接入全局记忆时，修复结果写回全局记忆形成跨任务闭环

用法::

    from youmi.coordinator.tool_guardian import ToolGuardianAgent

    # 方式 1：从配置目录加载（推荐，配置存放于 youmi/agents/tool_guardian/）
    guardian = ToolGuardianAgent.from_config_dir(mcp_server=server)
    await guardian.initialize()

    # 方式 2：直接构造（使用默认配置，可接入全局记忆形成 P6 闭环）
    from youmi.knowledge.global_memory import GlobalMemory
    memory = GlobalMemory()
    guardian = ToolGuardianAgent(mcp_server=server, global_memory=memory)
    await guardian.initialize()

    # 其他 Agent 连接 guardian
    worker_agent.connect_guardian(guardian.agent_id, broker, workflow_id)

    # guardian 监听并处理汇报
    await guardian.process_reports()

设计约定:
- ToolGuardianAgent 不参与具体任务执行，只负责工具记忆维护
- 修改工具描述时会保留原始描述作为历史参考
- 所有修改记录可追溯（_modification_history）
- 全局记忆为可选依赖，未接入或读写失败时修复流程优雅降级
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from collections import defaultdict
from datetime import datetime
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
from youmi.core.versioning import (
    AUTO_FIX_MAX_ATTEMPTS,
    EVIDENCE_HISTORY_LIMIT,
    ChangeKind,
)
from youmi.coordinator.fix_strategies import FixStrategiesMixin
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

class ToolGuardianAgent(FixStrategiesMixin, Agent):
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
        global_memory: Any | None = None,
        tool_store: Any | None = None,
        approval_manager: Any | None = None,
        repo_root: str | None = None,
    ) -> None:
        """
        Args:
            mcp_server: MCPServer 实例，用于读写工具描述
            config: Agent 配置（可选，有合理默认值）
            memory_strategy: 记忆策略覆盖
            llm_call: LLM 调用函数覆盖
            global_memory: GlobalMemory 实例（可选），接入后形成 P6 闭环：
                修复前查询该工具的历史经验作为上下文，
                修复成功后写入 BUG_FIX 经验并标记旧问题已解决
            tool_store: ToolStore 实例（可选），用于收集历史提交
                并在修复成功后提交修订号版本
            approval_manager: ApprovalManager 实例（可选），
                自动修复多次无效后提交人工审批
            repo_root: 代码库根目录（可选，默认当前工作目录），
                用于收集 git 历史提交作为现场信息
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
        self._global_memory = global_memory
        self._tool_store = tool_store
        self._approval_manager = approval_manager
        self._repo_root = repo_root or os.getcwd()

        # 收集到的汇报历史: tool_name → [ToolIssueReport]
        self._reports: dict[str, list[ToolIssueReport]] = defaultdict(list)

        # 修改历史: tool_name → [ToolModification]
        self._modification_history: dict[str, list[ToolModification]] = defaultdict(list)

        # 待处理的汇报队列（从消息总线获取后暂存）
        self._pending_reports: list[ToolIssueReport] = []

        # 回归闭环: 工具名 → 已尝试但未奏效的自动修复次数
        self._fix_attempts: dict[str, int] = {}

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
        2. 从全局记忆查询该工具的历史经验（P6 闭环，可选）
        2.5 收集现场信息与历史提交（版本链 + git 最近提交）
        3. 分析问题类型集合
        4. 使用 LLM（如果有）或规则引擎生成新描述（注入历史经验与现场信息）
        5. 应用修改到 MCPServer
        6. 记录修改历史
        7. 修复成功后写回全局记忆：BUG_FIX 经验 + 标记旧问题已解决（P6 闭环）
        8. 回归闭环：无实质变更 → 重试；达上限 → 转人工；成功 → 提交修订版本

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
            "fix_attempts": 0,
            "retry_scheduled": False,
            "escalated": False,
            "evidence": {},
        }

        # 1. 读取当前工具定义
        current_defn = self._mcp_server.get_tool_definition(tool_name)
        if current_defn is None:
            result["error"] = f"工具 '{tool_name}' 在 MCPServer 中未找到"
            logger.warning(result["error"])
            return result

        old_description = current_defn.description
        old_params = {p.name: p.description for p in current_defn.parameters}

        # 2. P6 闭环：从全局记忆查询该工具的历史经验作为修复上下文
        tool_knowledge = await self._load_tool_knowledge(tool_name)
        if tool_knowledge is not None and not tool_knowledge.is_empty:
            result["knowledge_entries"] = len(tool_knowledge.entry_ids)

        # 2.5 回归闭环：收集现场信息与历史提交（版本链 + 代码库最近提交）
        evidence = await self._collect_evidence(tool_name, reports, current_defn)
        result["evidence"] = {
            "current_version": evidence.get("current_version", ""),
            "version_history": len(evidence.get("version_history", [])),
            "git_commits": len(evidence.get("git_commits", [])),
            "reporter_agents": evidence.get("reporter_agents", []),
        }

        # 3. 分析问题类型
        issue_types = [r.issue_type for r in reports]
        primary_type = max(set(issue_types), key=issue_types.count)

        # 4. 生成新描述和代码建议（注入历史经验）
        new_description, param_updates, code_suggestion = await self._generate_fix(
            tool_name=tool_name,
            current_description=old_description,
            current_params=old_params,
            reports=reports,
            primary_type=primary_type,
            tool_knowledge=tool_knowledge,
            evidence=evidence,
        )

        # 5. 应用修改到 MCPServer
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

        # 6. 记录代码建议
        if code_suggestion:
            result["code_suggestion"] = code_suggestion

        # 7. 记录修改历史
        modification = ToolModification(
            tool_name=tool_name,
            report=reports[0],  # 以第一条汇报为代表
            old_description=old_description,
            new_description=new_description or old_description,
            param_updates=param_updates,
            code_suggestion=code_suggestion,
        )
        self._modification_history[tool_name].append(modification)

        # 8. P6 闭环：修复成功后写回全局记忆
        if result["description_updated"] or code_suggestion:
            memory_result = await self._persist_fix_to_memory(tool_name, modification)
            if memory_result is not None:
                result["memory_updated"] = memory_result

        # 9. 回归闭环：判定修复是否有效 → 提交修订版本 / 重试 / 转人工
        changed = bool(
            (new_description and new_description != old_description)
            or param_updates
            or code_suggestion
        )
        attempts = self._fix_attempts.get(tool_name, 0)
        if changed:
            self._fix_attempts.pop(tool_name, None)
            result["fix_attempts"] = attempts + 1
            if self._tool_store is not None:
                fix_tool_id = await self._commit_fix_version(
                    tool_name,
                    new_description or old_description,
                    param_updates,
                    reports,
                    evidence=evidence,
                )
                if fix_tool_id:
                    result["fix_version"] = fix_tool_id
        else:
            attempts += 1
            self._fix_attempts[tool_name] = attempts
            result["fix_attempts"] = attempts
            result["error"] = (
                f"自动修复未产生实质变更 "
                f"(第 {attempts}/{AUTO_FIX_MAX_ATTEMPTS} 次)"
            )
            if attempts >= AUTO_FIX_MAX_ATTEMPTS:
                self._fix_attempts.pop(tool_name, None)
                result.update(
                    await self._escalate_to_human(tool_name, reports, attempts)
                )
            else:
                # 重新入队：下一轮携带现场信息与历史经验再次尝试
                self._pending_reports.extend(reports)
                result["retry_scheduled"] = True

        return result

    # -----------------------------------------------------------------------
    # 回归闭环（现场收集 / 重试 / 转人工 / 修订版本）
    # -----------------------------------------------------------------------

    async def _collect_evidence(
        self,
        tool_name: str,
        reports: list[ToolIssueReport],
        current_defn: ToolDefinition,
    ) -> dict[str, Any]:
        """收集回归现场信息与历史提交

        包括：问题汇报现场（报错/参数/汇报人）、当前定义快照、
        ToolStore 版本链最近提交、代码库 git 最近提交。
        任一来源失败均静默降级，不阻断修复流程。
        """
        evidence: dict[str, Any] = {
            "tool_name": tool_name,
            "collected_at": datetime.utcnow().isoformat(),
            "reporter_agents": sorted(
                {r.reporter_agent_id for r in reports if r.reporter_agent_id}
            ),
            "error_messages": [r.error_message[:200] for r in reports[:5]],
            "call_arguments": [dict(r.call_arguments) for r in reports[:5]],
            "current_version": getattr(current_defn, "version", ""),
            "current_description": current_defn.description[:300],
            "version_history": [],
            "git_commits": [],
        }

        if self._tool_store is not None:
            try:
                chain = await self._tool_store.get_version_chain(tool_name)
                evidence["version_history"] = [
                    {
                        "version": item.version,
                        "parent_version_id": item.parent_version_id,
                        "changelog": (item.changelog or "")[:200],
                    }
                    for item in chain[:EVIDENCE_HISTORY_LIMIT]
                ]
            except Exception as exc:
                logger.warning(
                    "ToolGuardian failed to collect version history for '%s': %s",
                    tool_name, exc,
                )

        evidence["git_commits"] = await self._collect_git_history(tool_name)
        return evidence

    async def _collect_git_history(self, tool_name: str) -> list[str]:
        """收集代码库最近提交（非 git 仓库时返回空列表）"""
        root = self._repo_root
        if not root or not os.path.isdir(os.path.join(root, ".git")):
            return []

        def _run() -> list[str]:
            messages: list[str] = []
            commands = [
                ["git", "log", "-n", "5", "--oneline"],
                ["git", "log", "-n", "5", "--oneline", "--grep", tool_name],
            ]
            for cmd in commands:
                try:
                    proc = subprocess.run(
                        cmd, cwd=root, capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=5,
                    )
                except (OSError, subprocess.SubprocessError):
                    continue
                if proc.returncode == 0:
                    messages.extend(
                        line.strip()
                        for line in proc.stdout.splitlines()
                        if line.strip()
                    )

            unique: list[str] = []
            seen: set[str] = set()
            for message in messages:
                if message in seen:
                    continue
                seen.add(message)
                unique.append(message)
            return unique[:EVIDENCE_HISTORY_LIMIT]

        return await asyncio.to_thread(_run)

    async def _commit_fix_version(
        self,
        tool_name: str,
        new_description: str,
        param_updates: dict[str, str],
        reports: list[ToolIssueReport],
        evidence: dict[str, Any] | None = None,
    ) -> str | None:
        """修复成功后提交修订号版本（change_kind=fix）

        版本规则：仅修复 bug、不涉及功能变化 → 只提交修订号（patch）。
        现场信息与历史提交写入 changelog（可追溯），不污染工具描述。

        Returns:
            新版本 tool_id；工具不在 ToolStore 中时返回 None
        """
        try:
            head = await self._tool_store.get_head_version(tool_name)
            if head is None:
                return None

            defn = head.definition
            updated_params = [
                p.model_copy(update={"description": param_updates[p.name]})
                if p.name in param_updates else p
                for p in defn.parameters
            ]
            updated_defn = defn.model_copy(update={
                "description": new_description or defn.description,
                "parameters": updated_params,
            })

            first = reports[0]
            changelog = (
                f"回归修复({first.issue_type.value}): "
                f"{first.error_message[:80]}"
            )
            if evidence:
                baseline = evidence.get("current_version") or "?"
                changelog += f" | 基线 v{baseline}"
                commits = evidence.get("git_commits") or []
                if commits:
                    changelog += f" | 最近提交: {commits[0][:80]}"
            tool_id = await self._tool_store.create_version(
                tool_name,
                updated_defn,
                changelog=changelog,
                change_kind=ChangeKind.FIX.value,
            )
            logger.info(
                "ToolGuardian committed fix version %s for '%s'",
                tool_id, tool_name,
            )
            return tool_id
        except Exception as exc:
            logger.warning(
                "ToolGuardian failed to commit fix version for '%s': %s",
                tool_name, exc,
            )
            return None

    async def _escalate_to_human(
        self,
        tool_name: str,
        reports: list[ToolIssueReport],
        attempts: int,
    ) -> dict[str, Any]:
        """自动修复多次无效 → 转人工审批"""
        out: dict[str, Any] = {"escalated": True}
        first = reports[0]
        reason = (
            f"回归修复 {attempts} 次仍未解决 '{tool_name}': "
            f"[{first.issue_type.value}] {first.error_message[:160]}"
        )
        out["escalation_reason"] = reason

        if self._approval_manager is None:
            out["escalation_note"] = "未接入审批管理器, 已记录待人工处理"
            logger.error("ToolGuardian escalation: %s", reason)
            return out

        try:
            record = self._approval_manager.submit_request(
                agent_id=self.agent_id,
                tool_name=tool_name,
                risk_level=getattr(first, "risk_level", None) or None,
            )
            record.reason = reason
            out["approval_record_id"] = record.record_id
            out["approval_level"] = getattr(
                record.level, "value", str(record.level),
            )
            logger.warning(
                "ToolGuardian escalated '%s' to human approval: %s",
                tool_name, reason,
            )
        except Exception as exc:
            logger.warning(
                "ToolGuardian escalation failed for '%s': %s", tool_name, exc,
            )
            out["escalation_note"] = f"审批提交失败: {exc}"
        return out

    # -----------------------------------------------------------------------
    # P6 全局记忆闭环
    # -----------------------------------------------------------------------

    async def _load_tool_knowledge(self, tool_name: str) -> Any | None:
        """从全局记忆查询该工具的历史经验（ToolKnowledge）

        全局记忆不可用或查询失败时返回 None，修复流程优雅降级。
        """
        if self._global_memory is None:
            return None
        try:
            knowledge = await self._global_memory.get_tool_knowledge(tool_name)
            if not knowledge.is_empty:
                logger.info(
                    "ToolGuardian loaded %d knowledge entries for tool '%s' "
                    "(known_issues=%d resolved=%d)",
                    len(knowledge.entry_ids), tool_name,
                    len(knowledge.known_issues), len(knowledge.resolved_issues),
                )
            return knowledge
        except Exception:
            logger.warning(
                "ToolGuardian failed to load tool knowledge for '%s', "
                "continuing without global memory context",
                tool_name, exc_info=True,
            )
            return None

    async def _persist_fix_to_memory(
        self,
        tool_name: str,
        modification: ToolModification,
    ) -> dict[str, Any] | None:
        """修复闭环：将修复结果写回全局记忆

        1. 写入一条 BUG_FIX 经验，记录本次修复方案
        2. 将该工具未解决的历史经验条目标记为 resolved

        Returns:
            写回统计 {"fix_entry_id", "resolved_count"}；全局记忆不可用时返回 None
        """
        if self._global_memory is None:
            return None

        from youmi.knowledge.models import KnowledgeCategory

        # 1. 构造修复描述并写入 BUG_FIX 经验
        fix_summary = f"ToolGuardian 修复 '{tool_name}' 描述"
        if modification.new_description != modification.old_description:
            fix_summary += f": {modification.new_description[:200]}"
        if modification.code_suggestion:
            fix_summary += f"\n代码建议: {modification.code_suggestion[:300]}"

        memory_result: dict[str, Any] = {}
        try:
            entry = await self._global_memory.add_experience(
                tool_name=tool_name,
                content=fix_summary,
                category=KnowledgeCategory.BUG_FIX,
                source_agent_id=self.agent_id,
                metadata={
                    "issue_type": modification.report.issue_type.value,
                    "reporter_agent_id": modification.report.reporter_agent_id,
                    "modification": modification.to_dict(),
                },
            )
            memory_result["fix_entry_id"] = entry.entry_id

            # BUG_FIX 条目写入后立即标记 resolved（修复记录本身即已完成）
            await self._global_memory.mark_resolved(entry.entry_id, fix_summary)

            # 2. 标记该工具未解决的历史经验为已解决
            unresolved = await self._global_memory.list_entries(
                tool_name=tool_name,
                unresolved_only=True,
            )
            resolved_count = 0
            for old_entry in unresolved:
                updated = await self._global_memory.mark_resolved(
                    old_entry.entry_id,
                    fix_description=fix_summary,
                )
                if updated is not None:
                    resolved_count += 1
            memory_result["resolved_count"] = resolved_count

            logger.info(
                "ToolGuardian persisted fix for '%s' to global memory: "
                "fix_entry=%s resolved=%d",
                tool_name, entry.entry_id, resolved_count,
            )
            return memory_result
        except Exception:
            logger.warning(
                "ToolGuardian failed to persist fix for '%s' to global memory",
                tool_name, exc_info=True,
            )
            return None



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

        # 工具: search_tool_experience (P6 全局记忆检索)
        search_exp_tool = ToolDefinition(
            name="search_tool_experience",
            description=(
                "从全局记忆中检索工具的历史经验（已知问题、历史修复、最佳实践）。"
                "在分析工具问题前调用，可参考过去的失败教训避免重复修复。"
            ),
            parameters=[
                ToolParameter(
                    name="query",
                    type="string",
                    description="检索关键词或问题描述",
                    required=True,
                ),
                ToolParameter(
                    name="tool_name",
                    type="string",
                    description="限定工具名（空字符串表示全部工具）",
                    required=False,
                    default="",
                ),
                ToolParameter(
                    name="top_k",
                    type="integer",
                    description="最多返回条数（默认 5）",
                    required=False,
                    default=5,
                ),
            ],
        )

        async def _search_exp_handler(**kwargs: Any) -> str:
            if self._global_memory is None:
                return json.dumps({
                    "status": "unavailable",
                    "error": "全局记忆未接入 (global_memory is None)",
                }, ensure_ascii=False)

            query = kwargs.get("query", "")
            tool_name = kwargs.get("tool_name", "") or None
            top_k = int(kwargs.get("top_k", 5) or 5)

            try:
                entries = await self._global_memory.search(
                    query=query, tool_name=tool_name, top_k=top_k,
                )
                return json.dumps({
                    "status": "ok",
                    "query": query,
                    "result_count": len(entries),
                    "entries": [
                        {
                            "entry_id": e.entry_id,
                            "tool_name": e.tool_name,
                            "category": e.category.value,
                            "content": e.content,
                            "resolved": e.resolved,
                            "resolution": e.resolution,
                        }
                        for e in entries
                    ],
                }, ensure_ascii=False, default=str)
            except Exception as exc:
                return json.dumps({
                    "status": "error",
                    "error": str(exc),
                }, ensure_ascii=False)

        self._tool_registry.register(search_exp_tool, _search_exp_handler)

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
        summary["global_memory_enabled"] = self._global_memory is not None
        return summary
