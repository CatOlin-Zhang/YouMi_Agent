"""
任务完成后后台流水线 (PostTaskPipeline)

在 MasterAgent 完成一轮任务后，自动在后台执行以下收集流程:
1. collect_tool_experiences() — 提取工具调用的成功/失败模式
2. summarize_task_outcomes() — 汇总任务结果，生成结构化摘要
3. update_tool_notes() — 将工具使用经验追加到 ToolGuardianAgent
4. update_global_memory() — (P6) 将经验沉淀到全局记忆，触发工具版本更新

用法::

    from youmi.coordinator.post_task import PostTaskPipeline

    pipeline = PostTaskPipeline()
    await pipeline.run(master_agent, task_results)

子类可通过覆写各阶段的钩子方法定制行为::

    class MyPipeline(PostTaskPipeline):
        async def collect_tool_experiences(self, ...):
            # 自定义工具经验收集逻辑
            ...
"""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

from pydantic import BaseModel, Field

from youmi.core.tool import ToolDefinition

if TYPE_CHECKING:
    from youmi.coordinator.master import MasterAgent
    from youmi.core.agent import TaskResult
    from youmi.mcp.tool_store import ToolStore
    from youmi.knowledge.global_memory import GlobalMemory

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 工具使用经验数据类
# ---------------------------------------------------------------------------

class ToolExperience(BaseModel):
    """单个工具的使用经验记录

    Attributes:
        tool_name: 工具名称
        success_patterns: 成功的调用模式描述
        failure_patterns: 失败的调用模式和错误信息
        boundary_notes: 发现的边界条件
        usage_count: 使用次数
        success_rate: 成功率 (0.0 ~ 1.0)
    """

    tool_name: str
    success_patterns: list[str] = Field(default_factory=list)
    failure_patterns: list[str] = Field(default_factory=list)
    boundary_notes: list[str] = Field(default_factory=list)
    usage_count: int = 0
    success_rate: float = 0.0


# ---------------------------------------------------------------------------
# 任务结果摘要
# ---------------------------------------------------------------------------

class TaskOutcomeSummary(BaseModel):
    """任务执行结果摘要

    Attributes:
        total_agents: 参与的 Agent 总数
        completed: 成功完成的 Agent 数
        failed: 失败的 Agent 数
        tool_experiences: 收集到的工具经验列表
        overall_output: 综合输出描述
    """

    total_agents: int = 0
    completed: int = 0
    failed: int = 0
    tool_experiences: list[ToolExperience] = Field(default_factory=list)
    overall_output: str = ""


# ---------------------------------------------------------------------------
# 后台流水线
# ---------------------------------------------------------------------------

class PostTaskPipeline:
    """任务完成后后台流水线

    在任务结束后自动执行四个阶段的收集流程:
    1. 工具经验收集 — 从 SubAgent 对话记录中提取工具调用模式
    2. 任务结果汇总 — 生成结构化摘要写入 MasterAgent 记忆
    3. 工具笔记更新 — 将经验追加到 ToolGuardianAgent（如已连接）
    4. 全局记忆沉淀 — (P6) 将经验写入 GlobalMemory，
       高失败率工具自动触发版本更新

    子类可覆写各阶段方法以定制行为。
    """

    def __init__(
        self,
        tool_store: ToolStore | None = None,
        global_memory: GlobalMemory | None = None,
    ) -> None:
        self._experiences: list[ToolExperience] = []
        self._summary: TaskOutcomeSummary | None = None
        self._tool_store = tool_store  # ToolStore 实例 (可选, 用于版本更新)
        self._global_memory = global_memory  # GlobalMemory 实例 (可选, P6)
        # P6: 累计失败次数 (跨任务的内存统计，按工具名累计)
        self._failure_counts: dict[str, int] = {}

    @property
    def experiences(self) -> list[ToolExperience]:
        """收集到的工具经验"""
        return list(self._experiences)

    @property
    def summary(self) -> TaskOutcomeSummary | None:
        """任务结果摘要"""
        return self._summary

    async def run(
        self,
        master: MasterAgent,
        task_results: dict[str, TaskResult],
    ) -> TaskOutcomeSummary:
        """执行后台流水线

        Args:
            master: MasterAgent 实例
            task_results: {agent_id: TaskResult} 映射

        Returns:
            TaskOutcomeSummary 任务结果摘要
        """
        logger.info(
            "PostTaskPipeline starting: %d task results",
            len(task_results),
        )

        # 阶段 1: 收集工具使用经验
        self._experiences = await self.collect_tool_experiences(master, task_results)

        # 阶段 2: 汇总任务结果
        self._summary = await self.summarize_task_outcomes(
            master, task_results, self._experiences,
        )

        # 阶段 3: 更新工具笔记（向 ToolGuardian 汇报）
        await self.update_tool_notes(master, self._experiences)

        # 阶段 4: (P6) 全局记忆沉淀 + 工具版本更新
        if self._global_memory is not None:
            try:
                await self.update_global_memory(master, self._experiences)
            except Exception as exc:
                logger.warning(
                    "PostTaskPipeline: update_global_memory failed: %s", exc,
                )

        logger.info(
            "PostTaskPipeline finished: %d experiences, %d/%d completed",
            len(self._experiences),
            self._summary.completed if self._summary else 0,
            self._summary.total_agents if self._summary else 0,
        )

        return self._summary

    # -----------------------------------------------------------------------
    # 阶段 1: 工具经验收集
    # -----------------------------------------------------------------------

    async def collect_tool_experiences(
        self,
        master: MasterAgent,
        task_results: dict[str, TaskResult],
    ) -> list[ToolExperience]:
        """从 SubAgent 对话记录中提取工具调用模式

        遍历所有 SubAgent 的 _conversation 列表，统计每个工具的
        成功/失败调用次数，提取关键错误信息。

        Args:
            master: MasterAgent 实例
            task_results: 任务结果映射

        Returns:
            ToolExperience 列表
        """
        tool_stats: dict[str, dict[str, Any]] = {}

        for record in master.get_sub_agents().values():
            agent = record.agent
            # 遍历 Agent 的对话记录，提取工具调用和结果
            conversation = getattr(agent, '_conversation', [])
            for msg in conversation:
                if msg.get("role") == "tool":
                    content = msg.get("content", "")
                    tool_call_id = msg.get("tool_call_id", "")

                    # 尝试从之前的 assistant 消息找到工具名
                    tool_name = self._extract_tool_name(conversation, tool_call_id)
                    if not tool_name:
                        continue

                    if tool_name not in tool_stats:
                        tool_stats[tool_name] = {
                            "success": 0,
                            "failure": 0,
                            "failures": [],
                            "successes": [],
                        }

                    stats = tool_stats[tool_name]
                    try:
                        data = json.loads(content)
                        if "error" in data:
                            stats["failure"] += 1
                            error_msg = data["error"]
                            if len(stats["failures"]) < 3:
                                stats["failures"].append(error_msg[:200])
                        else:
                            stats["success"] += 1
                            if len(stats["successes"]) < 3:
                                stats["successes"].append(content[:100])
                    except (json.JSONDecodeError, TypeError):
                        # 非 JSON 内容视为成功
                        stats["success"] += 1

        # 转换为 ToolExperience 列表
        experiences: list[ToolExperience] = []
        for tool_name, stats in tool_stats.items():
            total = stats["success"] + stats["failure"]
            experiences.append(ToolExperience(
                tool_name=tool_name,
                success_patterns=stats["successes"],
                failure_patterns=stats["failures"],
                usage_count=total,
                success_rate=stats["success"] / total if total > 0 else 0.0,
            ))

        logger.debug(
            "Collected %d tool experiences from %d sub-agents",
            len(experiences), len(task_results),
        )
        return experiences

    @staticmethod
    def _extract_tool_name(conversation: list[dict], tool_call_id: str) -> str:
        """从对话记录中根据 tool_call_id 反查工具名"""
        for msg in reversed(conversation):
            if msg.get("role") == "assistant":
                # 检查 tool_calls 字段
                tool_calls = msg.get("tool_calls", [])
                for tc in tool_calls:
                    if tc.get("id") == tool_call_id:
                        return tc.get("function", {}).get("name", "")
        return ""

    # -----------------------------------------------------------------------
    # 阶段 2: 任务结果汇总
    # -----------------------------------------------------------------------

    async def summarize_task_outcomes(
        self,
        master: MasterAgent,
        task_results: dict[str, TaskResult],
        experiences: list[ToolExperience],
    ) -> TaskOutcomeSummary:
        """汇总任务结果，生成结构化摘要

        将摘要写入 MasterAgent 的记忆系统，便于后续任务参考。

        Args:
            master: MasterAgent 实例
            task_results: 任务结果映射
            experiences: 工具经验列表

        Returns:
            TaskOutcomeSummary
        """
        completed = sum(1 for r in task_results.values() if r.success)
        failed = sum(1 for r in task_results.values() if not r.success)

        # 拼接综合输出
        output_parts: list[str] = []
        for agent_id, result in task_results.items():
            record = master.get_sub_agents().get(agent_id)
            role = record.role if record else "unknown"
            status = "成功" if result.success else "失败"
            output_preview = str(result.output)[:200] if result.output else ""
            output_parts.append(
                f"- [{role}] {status}: {output_preview}"
            )

        overall_output = "\n".join(output_parts)

        summary = TaskOutcomeSummary(
            total_agents=len(task_results),
            completed=completed,
            failed=failed,
            tool_experiences=experiences,
            overall_output=overall_output,
        )

        # 写入 MasterAgent 记忆
        try:
            await master.memory.on_message(
                "system",
                f"[后台流水线] 任务完成摘要:\n"
                f"共 {summary.total_agents} 个 Agent, "
                f"{summary.completed} 成功, {summary.failed} 失败。\n"
                f"工具经验: {len(experiences)} 个工具被使用。\n"
                f"{overall_output}",
            )
        except Exception as exc:
            logger.debug("Failed to write summary to memory: %s", exc)

        return summary

    # -----------------------------------------------------------------------
    # 阶段 3: 工具笔记更新
    # -----------------------------------------------------------------------

    async def update_tool_notes(
        self,
        master: MasterAgent,
        experiences: list[ToolExperience],
    ) -> None:
        """将工具使用经验追加到 ToolGuardianAgent

        对于失败率较高的工具:
        1. 自动向 ToolGuardianAgent 汇报 (复用 report_tool_issue 机制)
        2. 如果启用了 ToolStore，记录变更日志

        Args:
            master: MasterAgent 实例
            experiences: 工具经验列表
        """
        for exp in experiences:
            # 失败率 > 30% 的工具自动汇报
            if exp.success_rate < 0.7 and exp.failure_patterns:
                # 1. 向 ToolGuardianAgent 汇报
                for record in master.get_sub_agents().values():
                    agent = record.agent
                    if hasattr(agent, '_tool_guardian_id') and agent._tool_guardian_id:
                        try:
                            await agent.report_tool_issue(
                                tool_name=exp.tool_name,
                                error_message=(
                                    f"后台流水线检测到高失败率 "
                                    f"({1 - exp.success_rate:.0%}): "
                                    + "; ".join(exp.failure_patterns[:2])
                                ),
                                issue_type="UNEXPECTED_BEHAVIOR",
                                suggestion="建议优化工具描述或修复实现",
                            )
                        except Exception as exc:
                            logger.debug(
                                "Failed to report tool issue for '%s': %s",
                                exp.tool_name, exc,
                            )
                        break  # 只需一个 Agent 汇报即可

                # 2. 写入 ToolStore 变更日志 (如果有)
                if self._tool_store is not None:
                    try:
                        changelog_desc = (
                            f"自动检测到高失败率 ({1 - exp.success_rate:.0%}): "
                            + "; ".join(exp.failure_patterns[:2])
                        )
                        await self._tool_store.add_changelog(
                            tool_name=exp.tool_name,
                            change_type="bugfix",
                            description=changelog_desc,
                            source="post_task_pipeline",
                        )
                    except Exception as exc:
                        logger.debug(
                            "Failed to add changelog for '%s': %s",
                            exp.tool_name, exc,
                        )

    # -----------------------------------------------------------------------
    # 阶段 4: 全局记忆沉淀 (P6)
    # -----------------------------------------------------------------------

    async def update_global_memory(
        self,
        master: MasterAgent,
        experiences: list[ToolExperience],
    ) -> None:
        """将工具经验沉淀到全局记忆 (GlobalMemory)

        1. 将每条 ToolExperience 转为 KnowledgeEntry 写入 GlobalMemory
        2. 对失败率高的工具，使用 ToolExperienceExtractor 做语义分析
           (失败根因 + 修复建议)，生成更有价值的经验条目
        3. 当某工具累计失败次数达到阈值且成功率低时，
           自动调用 trigger_tool_version_update() 创建新版本

        经验专供工具管理 Agent (ToolGuardian) 使用，不注入子 Agent。

        Args:
            master: MasterAgent 实例
            experiences: 工具经验列表
        """
        from youmi.knowledge.models import KnowledgeCategory
        from youmi.knowledge.experience_extractor import ToolExperienceExtractor

        if self._global_memory is None:
            return

        task_id = getattr(master, '_workflow_id', '') or ''
        extractor = ToolExperienceExtractor()

        for exp in experiences:
            if exp.usage_count == 0:
                continue

            # 1. 写入整体使用统计经验
            try:
                content = ToolExperienceExtractor.to_experience_content(exp)
                await self._global_memory.add_experience(
                    tool_name=exp.tool_name,
                    content=content,
                    category=KnowledgeCategory.TOOL_EXPERIENCE,
                    source_task_id=task_id,
                    success_rate=exp.success_rate,
                    metadata={"usage_count": exp.usage_count},
                )
            except Exception as exc:
                logger.debug(
                    "Failed to write experience to GlobalMemory for '%s': %s",
                    exp.tool_name, exc,
                )
                continue

            # 2. 高失败率工具做语义分析，写入失败根因经验
            if exp.failure_patterns and exp.success_rate < 0.7:
                try:
                    insights = await extractor.analyze_failures(
                        exp.failure_patterns, exp.tool_name,
                    )
                    for insight in insights:
                        await self._global_memory.add_experience(
                            tool_name=exp.tool_name,
                            content=insight,
                            category=KnowledgeCategory.TOOL_EXPERIENCE,
                            source_task_id=task_id,
                            success_rate=exp.success_rate,
                        )
                except Exception as exc:
                    logger.debug(
                        "Failed to analyze failures for '%s': %s",
                        exp.tool_name, exc,
                    )

            # 3. 累计失败次数 → 触发工具版本更新
            failure_count = int(round(exp.usage_count * (1 - exp.success_rate)))
            self._failure_counts[exp.tool_name] = (
                self._failure_counts.get(exp.tool_name, 0) + failure_count
            )
            if (
                self._failure_counts[exp.tool_name] >= 3
                and exp.success_rate < 0.5
            ):
                fix_description = (
                    f"全局记忆累计检测到 {self._failure_counts[exp.tool_name]} 次失败 "
                    f"(最近成功率 {exp.success_rate:.0%}): "
                    + "; ".join(exp.failure_patterns[:2])
                )
                new_tool_id = await self.trigger_tool_version_update(
                    tool_name=exp.tool_name,
                    fix_description=fix_description,
                )
                if new_tool_id:
                    # 触发成功后记录修复经验并重置计数
                    try:
                        await self._global_memory.add_experience(
                            tool_name=exp.tool_name,
                            content=fix_description,
                            category=KnowledgeCategory.BUG_FIX,
                            source_task_id=task_id,
                        )
                    except Exception as exc:
                        logger.debug(
                            "Failed to record bug_fix experience for '%s': %s",
                            exp.tool_name, exc,
                        )
                    self._failure_counts[exp.tool_name] = 0

        logger.debug(
            "PostTaskPipeline: %d experiences persisted to GlobalMemory",
            len(experiences),
        )

    # -----------------------------------------------------------------------
    # 工具版本更新触发
    # -----------------------------------------------------------------------

    async def trigger_tool_version_update(
        self,
        tool_name: str,
        fix_description: str,
        new_definition: ToolDefinition | None = None,
        bump: str = "patch",
    ) -> str | None:
        """触发工具版本更新

        基于全局记忆收集的修复方案，创建新版本。
        通常由 ToolGuardianAgent 修复后调用。

        Args:
            tool_name: 工具名称
            fix_description: 修复说明
            new_definition: 新的工具定义 (None = 从 ToolStore 读取当前定义)
            bump: 版本号自增类型 "patch" | "minor" | "major"

        Returns:
            新版本 tool_id，如果 ToolStore 未启用或工具不存在返回 None
        """
        if self._tool_store is None:
            logger.warning(
                "PostTaskPipeline: trigger_tool_version_update skipped "
                "(no ToolStore configured)"
            )
            return None

        try:
            # 如果未提供新定义，从 ToolStore 读取当前定义
            if new_definition is None:
                entry = await self._tool_store.get_latest_version(tool_name)
                if entry is None:
                    logger.warning(
                        "PostTaskPipeline: tool '%s' not found in store", tool_name
                    )
                    return None
                new_definition = entry.definition

            # 创建新版本
            new_tool_id = await self._tool_store.create_version(
                tool_name=tool_name,
                new_definition=new_definition,
                changelog=fix_description,
                bump=bump,
            )

            logger.info(
                "PostTaskPipeline: created new version for '%s': %s",
                tool_name, new_tool_id,
            )
            return new_tool_id

        except Exception as exc:
            logger.warning(
                "PostTaskPipeline: failed to create version for '%s': %s",
                tool_name, exc,
            )
            return None
