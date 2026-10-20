"""YouMi 能力 Provider — sidecar 暴露给 DSH 的 10 个 MCP 工具

契约见 docs/dsh-shell-migration-plan.md 第 5 节:

- 工具目录: youmi_tool_search / youmi_tool_get / youmi_tool_resolve
- Skill 索引: youmi_skill_index / youmi_skill_list / youmi_skill_get
- 计划复用: youmi_plan_search / youmi_plan_save
- 失败经验: youmi_memory_search / youmi_memory_add

设计约束:
- 只做检索与本地沉淀，不执行任何宿主工具（执行由 DSH 负责）
- 组件缺失或异常时返回结构化错误，不抛异常（fail-open）
- 返回值统一为 JSON 字符串，便于 DSH 侧直接透传
"""

from __future__ import annotations

import json
import logging
from typing import Any

from youmi.core.tool import RiskLevel
from youmi.knowledge.global_memory import GlobalMemory
from youmi.mcp.provider import LocalFunctionProvider
from youmi.mcp.skill_store import SkillStore
from youmi.mcp.tool_store import ToolStore
from youmi.mcp.version_router import CallPathRouter
from youmi.plan.models import WorkflowPlan
from youmi.plan.plan_memory import PlanMemory

logger = logging.getLogger(__name__)

PROVIDER_ID = "youmi"


# ---------------------------------------------------------------------------
# 序列化辅助
# ---------------------------------------------------------------------------

def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _err(message: str) -> str:
    return _dump({"error": message})


def _tool_payload(entry: Any) -> dict[str, Any]:
    """ToolEntry → 精简 JSON（供 DSH 侧直接使用）"""
    definition = getattr(entry, "definition", None)
    return {
        "name": entry.tool_name,
        "version": entry.version,
        "summary": entry.summary,
        "risk_level": entry.risk_level,
        "required_permissions": list(entry.required_permissions or []),
        "lineage_id": entry.lineage_id,
        "stage": entry.stage,
        "origin": entry.origin,
        "essential": entry.essential,
        "definition": definition.model_dump() if definition is not None else None,
    }


def _skill_payload(entry: Any) -> dict[str, Any]:
    return {
        "skill_id": f"{entry.skill_name}@{entry.version}",
        "skill_name": entry.skill_name,
        "version": entry.version,
        "summary": entry.summary,
        "summary_l2": entry.summary_l2,
        "bound_tool_name": entry.bound_tool_name,
        "risk_level": entry.risk_level,
        "tags": list(entry.tags or []),
        "describe": entry.describe,
    }


def _knowledge_payload(entry: Any) -> dict[str, Any]:
    return entry.model_dump(mode="json", exclude={"embedding"})


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class YouMiCapabilityProvider(LocalFunctionProvider):
    """把 YouMi 的四个能力组包装成 MCP 工具

    Args:
        tool_store: 工具目录（缺失时工具目录类工具返回错误）
        skill_store: Skill 索引
        plan_memory: 计划复用
        global_memory: 失败经验
    """

    def __init__(
        self,
        tool_store: ToolStore | None = None,
        skill_store: SkillStore | None = None,
        plan_memory: PlanMemory | None = None,
        global_memory: GlobalMemory | None = None,
    ) -> None:
        super().__init__(provider_id=PROVIDER_ID, origin="generated")
        self._tool_store = tool_store
        self._skill_store = skill_store
        self._plan_memory = plan_memory
        self._global_memory = global_memory
        self._router = (
            CallPathRouter(tool_store, skill_store)
            if tool_store is not None else None
        )
        self._register_tools()

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def _register_tools(self) -> None:
        self.register_function(
            self.youmi_tool_search,
            param_descriptions={
                "query": "自然语言描述当前需要的能力",
                "top_k": "返回候选数量，默认 5",
                "risk_ceiling": "风险上限 low/medium/high/critical，空 = 不限制",
            },
        )
        self.register_function(
            self.youmi_tool_get,
            param_descriptions={
                "name": "工具名称",
                "version": "版本号，空 = 最新版本",
            },
        )
        self.register_function(
            self.youmi_tool_resolve,
            param_descriptions={
                "name": "工具名称（版本链标识）",
                "call_path": "调用来源 skill/cross_domain/direct，默认 direct",
            },
        )
        self.register_function(
            self.youmi_skill_index,
            param_descriptions={
                "query": "自然语言描述需要的流程/SOP",
                "top_k": "返回候选数量，默认 5",
            },
        )
        self.register_function(
            self.youmi_skill_list,
            param_descriptions={
                "cursor": "分页游标（上页返回的 next_cursor），空 = 第一页",
                "limit": "每页数量，默认 20",
            },
        )
        self.register_function(
            self.youmi_skill_get,
            param_descriptions={
                "skill_id": "Skill 标识，形如 name 或 name@version",
            },
        )
        self.register_function(
            self.youmi_plan_search,
            param_descriptions={
                "task": "当前任务的自然语言描述",
                "top_k": "返回候选数量，默认 3",
            },
        )
        self.register_function(
            self.youmi_plan_save,
            param_descriptions={
                "task": "原始任务文本",
                "plan_json": "WorkflowPlan JSON（name/steps/description）",
                "outcome": "执行结果 success/failure",
            },
        )
        self.register_function(
            self.youmi_memory_search,
            param_descriptions={
                "query": "检索关键词（错误信息、工具名、场景）",
                "top_k": "返回条数，默认 5",
                "tool_name": "限定工具名，空 = 全部",
            },
        )
        self.register_function(
            self.youmi_memory_add,
            param_descriptions={
                "task": "任务描述",
                "failure": "失败现象/错误信息",
                "fix": "修复方式",
                "tags": "逗号分隔的标签",
                "tool_name": "关联工具名，可空",
            },
        )

    # ------------------------------------------------------------------
    # 工具目录
    # ------------------------------------------------------------------

    async def youmi_tool_search(
        self,
        query: str,
        top_k: int = 5,
        risk_ceiling: str = "",
    ) -> str:
        """按能力描述语义检索工具目录，返回候选工具及定义。"""
        if self._tool_store is None:
            return _err("tool_store 未启用")
        try:
            results = await self._tool_store.search_cone(
                query,
                top_k=max(1, int(top_k)),
                risk_ceiling=risk_ceiling or RiskLevel.CRITICAL,
            )
            return _dump([
                {
                    "name": r.tool_name,
                    "score": round(float(r.score), 4),
                    "summary": r.summary,
                    "risk_level": r.risk_level,
                    "required_permissions": list(r.required_permissions or []),
                    "lineage_id": r.lineage_id,
                    "definition": r.definition.model_dump() if r.definition else None,
                }
                for r in results
            ])
        except Exception as exc:
            logger.warning("youmi_tool_search failed: %s", exc)
            return _err(f"工具检索失败: {exc}")

    async def youmi_tool_get(self, name: str, version: str = "") -> str:
        """获取单个工具的完整定义与版本元数据。"""
        if self._tool_store is None:
            return _err("tool_store 未启用")
        try:
            entry = await self._tool_store.get_tool(name, version or None)
            if entry is None:
                return _err(f"工具不存在: {name}")
            return _dump(_tool_payload(entry))
        except Exception as exc:
            logger.warning("youmi_tool_get failed: %s", exc)
            return _err(f"工具读取失败: {exc}")

    async def youmi_tool_resolve(self, name: str, call_path: str = "direct") -> str:
        """按调用来源解析工具版本（skill → root，其余 → head）。"""
        if self._router is None:
            return _err("tool_store 未启用")
        try:
            entry = await self._router.resolve(name, call_path or "direct")
            if entry is None:
                return _err(f"版本链不存在: {name}")
            return _dump(_tool_payload(entry))
        except ValueError:
            return _err(f"非法 call_path: {call_path}（允许 skill/cross_domain/direct）")
        except Exception as exc:
            logger.warning("youmi_tool_resolve failed: %s", exc)
            return _err(f"版本解析失败: {exc}")

    # ------------------------------------------------------------------
    # Skill 索引
    # ------------------------------------------------------------------

    async def youmi_skill_index(self, query: str, top_k: int = 5) -> str:
        """按任务描述语义检索 Skill/SOP 索引。"""
        if self._skill_store is None:
            return _err("skill_store 未启用")
        try:
            results = await self._skill_store.search_cone(
                query, top_k=max(1, int(top_k)),
            )
            return _dump([
                {
                    "skill_id": f"{r.skill_name}@{r.version}",
                    "skill_name": r.skill_name,
                    "score": round(float(r.score), 4),
                    "summary": r.summary,
                    "summary_l2": r.summary_l2,
                    "bound_tool_name": r.bound_tool_name,
                    "risk_level": r.risk_level,
                    "tags": list(r.tags or []),
                }
                for r in results
            ])
        except Exception as exc:
            logger.warning("youmi_skill_index failed: %s", exc)
            return _err(f"Skill 检索失败: {exc}")

    async def youmi_skill_list(self, cursor: str = "", limit: int = 20) -> str:
        """分页列出 Skill 索引（按更新时间倒序）。"""
        if self._skill_store is None:
            return _err("skill_store 未启用")
        try:
            offset = int(cursor) if cursor else 0
            page_size = max(1, min(int(limit), 100))
            entries = await self._skill_store.list_skills(
                limit=page_size + 1, offset=offset,
            )
            has_more = len(entries) > page_size
            entries = entries[:page_size]
            return _dump({
                "skills": [_skill_payload(e) for e in entries],
                "next_cursor": str(offset + page_size) if has_more else "",
            })
        except Exception as exc:
            logger.warning("youmi_skill_list failed: %s", exc)
            return _err(f"Skill 列表读取失败: {exc}")

    async def youmi_skill_get(self, skill_id: str) -> str:
        """读取单个 Skill 的完整内容（含 SOP 正文）。"""
        if self._skill_store is None:
            return _err("skill_store 未启用")
        try:
            name, version = skill_id, None
            if "@" in skill_id:
                name, version = skill_id.rsplit("@", 1)
            entry = await self._skill_store.get_skill(name, version)
            if entry is None:
                return _err(f"Skill 不存在: {skill_id}")
            payload = _skill_payload(entry)
            payload["content_json"] = entry.content_json
            return _dump(payload)
        except Exception as exc:
            logger.warning("youmi_skill_get failed: %s", exc)
            return _err(f"Skill 读取失败: {exc}")

    # ------------------------------------------------------------------
    # 计划复用
    # ------------------------------------------------------------------

    async def youmi_plan_search(self, task: str, top_k: int = 3) -> str:
        """检索可复用的历史计划骨架（相似度阈值 0.85）。"""
        if self._plan_memory is None:
            return _err("plan_memory 未启用")
        try:
            candidates = await self._plan_memory.search_plan(
                task, top_k=max(1, int(top_k)),
            )
            return _dump([
                {
                    "score": round(float(sim), 4),
                    "plan": plan.model_dump(),
                }
                for plan, sim in candidates
            ])
        except Exception as exc:
            logger.warning("youmi_plan_search failed: %s", exc)
            return _err(f"计划检索失败: {exc}")

    async def youmi_plan_save(
        self,
        task: str,
        plan_json: str,
        outcome: str = "success",
    ) -> str:
        """保存成功执行的计划骨架，供后续任务复用。"""
        if self._plan_memory is None:
            return _err("plan_memory 未启用")
        try:
            if isinstance(plan_json, str):
                plan = WorkflowPlan.model_validate_json(plan_json)
            else:
                plan = WorkflowPlan.model_validate(plan_json)
            errors = plan.validate()
            if errors:
                return _err("计划校验失败: " + "; ".join(errors))
            plan_id = await self._plan_memory.save_plan(
                task, plan, success=(outcome != "failure"),
            )
            return _dump({"plan_id": plan_id, "saved": True})
        except Exception as exc:
            logger.warning("youmi_plan_save failed: %s", exc)
            return _err(f"计划保存失败: {exc}")

    # ------------------------------------------------------------------
    # 失败经验
    # ------------------------------------------------------------------

    async def youmi_memory_search(
        self,
        query: str,
        top_k: int = 5,
        tool_name: str = "",
    ) -> str:
        """检索历史失败经验与修复方式。"""
        if self._global_memory is None:
            return _err("global_memory 未启用")
        try:
            entries = await self._global_memory.search(
                query,
                tool_name=tool_name or None,
                top_k=max(1, int(top_k)),
            )
            return _dump([_knowledge_payload(e) for e in entries])
        except Exception as exc:
            logger.warning("youmi_memory_search failed: %s", exc)
            return _err(f"经验检索失败: {exc}")

    async def youmi_memory_add(
        self,
        task: str,
        failure: str,
        fix: str,
        tags: str = "",
        tool_name: str = "",
    ) -> str:
        """沉淀一条失败经验（任务 / 失败现象 / 修复方式）。"""
        if self._global_memory is None:
            return _err("global_memory 未启用")
        try:
            content = f"任务: {task}\n失败: {failure}\n修复: {fix}"
            entry = await self._global_memory.add_experience(
                tool_name=tool_name,
                content=content,
                metadata={
                    "tags": [t.strip() for t in tags.split(",") if t.strip()],
                    "source": "dsh-sidecar",
                },
            )
            return _dump({"entry_id": entry.entry_id, "saved": True})
        except Exception as exc:
            logger.warning("youmi_memory_add failed: %s", exc)
            return _err(f"经验保存失败: {exc}")


__all__ = ["PROVIDER_ID", "YouMiCapabilityProvider"]
