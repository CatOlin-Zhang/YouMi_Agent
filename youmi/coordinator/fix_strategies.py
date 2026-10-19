"""
修复策略 Mixin — ToolGuardianAgent 的工具描述修复逻辑

从 youmi/coordinator/tool_guardian.py 提取，包含：
- _generate_fix            — 修复策略入口 (LLM 优先，规则退路)
- _generate_fix_with_llm   — 使用 LLM 生成修复方案
- _generate_fix_with_rules — 基于规则的修复方案

通过 Mixin 注入 ToolGuardianAgent。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from youmi.mcp.protocol import ToolIssueReport, ToolIssueType

logger = logging.getLogger(__name__)


class FixStrategiesMixin:
    """修复策略 Mixin

    依赖 ToolGuardianAgent 的以下实例属性：
    - _llm_client: LLM 客户端 (可选)
    - _config: Agent 配置 (含 system_prompt)
    """

    async def _generate_fix(
        self,
        tool_name: str,
        current_description: str,
        current_params: dict[str, str],
        reports: list[ToolIssueReport],
        primary_type: ToolIssueType,
        tool_knowledge: Any | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, str], str]:
        """生成工具描述修复和代码建议

        优先使用 LLM 分析，无 LLM 时退化为规则引擎。

        Args:
            tool_knowledge: 全局记忆中该工具的历史经验 (ToolKnowledge，
                可选)。注入后 LLM/规则修复可以参考 known_issues、
                resolved_issues 和 fix_history，避免重复修复
            evidence: 回归现场信息 (版本历史 / 最近提交 / 环境)，
                由 ToolGuardian 在动手修复前收集。

        Returns:
            (new_description, param_updates, code_suggestion)
        """
        # 尝试使用 LLM
        if self._llm_client is not None:
            return await self._generate_fix_with_llm(
                tool_name, current_description, current_params, reports, primary_type,
                tool_knowledge=tool_knowledge, evidence=evidence,
            )

        # 退化为规则引擎
        return self._generate_fix_with_rules(
            tool_name, current_description, current_params, reports, primary_type,
            tool_knowledge=tool_knowledge, evidence=evidence,
        )

    async def _generate_fix_with_llm(
        self,
        tool_name: str,
        current_description: str,
        current_params: dict[str, str],
        reports: list[ToolIssueReport],
        primary_type: ToolIssueType,
        tool_knowledge: Any | None = None,
        evidence: dict[str, Any] | None = None,
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

        # P6 闭环：注入全局记忆中的历史经验
        knowledge_section = ""
        if tool_knowledge is not None and not tool_knowledge.is_empty:
            knowledge_lines = []
            if tool_knowledge.known_issues:
                issues = "\n".join(f"  - {c[:150]}" for c in tool_knowledge.known_issues[:5])
                knowledge_lines.append(f"### 已知未解决问题 (重复出现，优先根治):\n{issues}")
            if tool_knowledge.fix_history:
                fixes = "\n".join(f"  - {c[:150]}" for c in tool_knowledge.fix_history[:5])
                knowledge_lines.append(f"### 历史修复记录 (不要重复同样的修复):\n{fixes}")
            if tool_knowledge.resolved_issues:
                resolved = "\n".join(f"  - {c[:100]}" for c in tool_knowledge.resolved_issues[:3])
                knowledge_lines.append(f"### 已解决的历史问题:\n{resolved}")
            if knowledge_lines:
                knowledge_section = (
                    "\n## 历史经验 (来自全局记忆)\n"
                    + "\n".join(knowledge_lines)
                    + "\n\n注意：已知未解决问题与本次汇报可能同源，请给出根治性修复。\n"
                )

        prompt = f"""分析以下工具调用问题并生成修复方案。

## 工具信息
- 名称: {tool_name}
- 当前描述: {current_description}
- 当前参数描述: {json.dumps(current_params, ensure_ascii=False)}

## 问题汇报
{chr(10).join(report_summaries)}
{knowledge_section}{self._format_evidence(evidence)}
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
                tool_knowledge=tool_knowledge, evidence=evidence,
            )

    @staticmethod
    def _format_evidence(evidence: dict[str, Any] | None) -> str:
        """格式化回归现场信息 (版本历史 / 最近提交) 供修复 prompt 使用"""
        if not evidence:
            return ""

        lines: list[str] = []
        history = evidence.get("version_history") or []
        if history:
            lines.append("### 版本历史 (最近提交)")
            for item in history:
                lines.append(
                    f"  - v{item.get('version', '?')} "
                    f"parent={item.get('parent_version_id') or '-'} "
                    f"{(item.get('changelog') or '')[:120]}"
                )
        commits = evidence.get("git_commits") or []
        if commits:
            lines.append("### 代码库最近提交")
            lines.extend(f"  - {c[:140]}" for c in commits)

        if not lines:
            return ""
        return "\n## 现场信息与历史提交\n" + "\n".join(lines) + "\n"

    @staticmethod
    def _generate_fix_with_rules(
        tool_name: str,
        current_description: str,
        current_params: dict[str, str],
        reports: list[ToolIssueReport],
        primary_type: ToolIssueType,
        tool_knowledge: Any | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, str], str]:
        """基于规则的修复方案（LLM 不可用时的退路）

        tool_knowledge 非空时，将全局记忆中的已知未解决问题一并
        附加到描述修正中，保证规则路径也能利用历史经验。
        evidence 仅用于保证规则路径同样"先看现场"：历史提交由
        调用方写入新版本 changelog（不写入描述，避免版本号变化
        造成非实质性描述变更）。
        """
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

        # P6 闭环：附加全局记忆中的已知未解决问题（去重）
        if tool_knowledge is not None and not tool_knowledge.is_empty:
            existing = "\n".join(suffix_parts)
            extra_issues = [
                issue for issue in tool_knowledge.known_issues[:3]
                if issue[:60] not in existing
            ]
            if extra_issues:
                suffix_parts.append(
                    "\n[全局记忆] 历史已知未解决问题（本次一并修正）:\n"
                    + "\n".join(f"  - {issue[:120]}" for issue in extra_issues)
                )

        # 回归闭环：历史提交作为版本变更依据，由调用方写入 changelog；
        # 描述中不插入版本号引用 (每次修复都会变，会造成假修复与重试失效)

        new_description = current_description
        if suffix_parts:
            # 去重: 同一问题重复汇报时不重复追加相同修正 (避免假修复)
            fresh = [
                p for p in suffix_parts
                if p.strip() and p.strip() not in current_description
            ]
            if fresh:
                new_description = current_description + "\n".join(fresh)

        return new_description, param_updates, code_suggestion
