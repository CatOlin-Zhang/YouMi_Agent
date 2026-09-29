"""
CallPathRouter — 调用来源版本分流 (P1 差距 5)

不同调用来源对同一工具名解析到不同版本:

- SKILL:        Skill SOP 调用 → root 版本
                (Skill 绑定的 Tool 原始名称 = lineage 初始提交,
                保证 SOP 描述与工具行为一致)
- CROSS_DOMAIN: 跨域调用 → head 版本
                (对方域当前最新版本)
- DIRECT:       直接调用 → 最新版本 (现状语义)

P1 阶段以独立 API 形态提供 (ToolStore.resolve_alias 保持不动);
P2 Skill 库落地后, 由 SkillIngestor 的 bound_tool_name 绑定关系
自动填充 SKILL 来源: 注入 skill_store 时, SKILL 来源优先查
SkillStore 绑定 → 有绑定 → root 版本; 无绑定 → 回退 DIRECT (head)。
未注入 skill_store 时保持 P1 行为 (SKILL 无条件 root)。

用法::

    from youmi.mcp.version_router import CallPathRouter, CallPathSource

    router = CallPathRouter(store)                      # 无 Skill 库
    root_entry = await router.resolve("send_email", CallPathSource.SKILL)

    router = CallPathRouter(store, skill_store=skills)  # P2 接线
    head_entry = await router.resolve("send_email", CallPathSource.DIRECT)
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from youmi.mcp.models import ToolEntry

logger = logging.getLogger(__name__)


class CallPathSource(str, Enum):
    """调用来源 — 决定版本解析策略"""

    SKILL = "skill"                # Skill SOP 调用 → root 版本
    CROSS_DOMAIN = "cross_domain"  # 跨域调用 → head 版本
    DIRECT = "direct"              # 直接调用 → 最新版本


class CallPathRouter:
    """调用来源版本分流器

    同一工具名 (lineage) 按调用来源解析到不同版本:
    SKILL → root (初始提交), CROSS_DOMAIN/DIRECT → head (最新)。

    注入 skill_store 后 SKILL 来源精细化 (P2): 仅存在 Skill 绑定
    (SkillStore.resolve_bound_tool 命中) 的工具解析 root,
    无绑定回退 DIRECT (head) — SOP 背书才锁定初始版本。

    Args:
        store: ToolStore 实例 (版本树的权威数据源)
        skill_store: SkillStore 并行库 (可选; None 时 SKILL 来源
            无条件解析 root, 保持 P1 行为)
    """

    def __init__(self, store: Any, skill_store: Any = None) -> None:
        self._store = store
        self._skill_store = skill_store

    @property
    def store(self) -> Any:
        return self._store

    @property
    def skill_store(self) -> Any:
        return self._skill_store

    async def resolve(
        self,
        tool_name: str,
        source: CallPathSource | str = CallPathSource.DIRECT,
    ) -> ToolEntry | None:
        """按调用来源解析工具版本

        Args:
            tool_name: 工具名称 (P0 起 lineage_id 默认 = tool_name,
                同名工具的全部版本共享同一条版本链)
            source: 调用来源 (CallPathSource 或其字符串值)

        Returns:
            解析到的 ToolEntry; 版本链不存在时返回 None
        """
        if isinstance(source, str):
            source = CallPathSource(source)

        if source is CallPathSource.SKILL:
            # P2 接线: 注入 SkillStore 时仅绑定工具走 root, 无绑定回退 head
            if self._skill_store is not None:
                skill = await self._skill_store.resolve_bound_tool(tool_name)
                if skill is None:
                    logger.debug(
                        "CallPathRouter: no skill binding for '%s', "
                        "SKILL falls back to DIRECT", tool_name,
                    )
                    return await self._resolve_head(tool_name, source)
            # 未注入 SkillStore (P1 行为) 或存在绑定 → root 版本
            entry = await self._store.get_root_version(tool_name)
            if entry is None:
                logger.debug(
                    "CallPathRouter: no root version for '%s' (SKILL)", tool_name,
                )
                return None
            logger.debug(
                "CallPathRouter: SKILL '%s' → root %s",
                tool_name, entry.version,
            )
            return entry

        # CROSS_DOMAIN / DIRECT → head 版本 (最新)
        return await self._resolve_head(tool_name, source)

    async def _resolve_head(
        self, tool_name: str, source: CallPathSource,
    ) -> ToolEntry | None:
        """head (最新) 版本解析 — CROSS_DOMAIN/DIRECT/无绑定 SKILL 共用"""
        entry = await self._store.get_head_version(tool_name)
        if entry is None:
            logger.debug(
                "CallPathRouter: no head version for '%s' (%s)", tool_name, source.value,
            )
            return None
        logger.debug(
            "CallPathRouter: %s '%s' → head %s",
            source.value, tool_name, entry.version,
        )
        return entry

    async def resolve_tool_id(
        self,
        tool_name: str,
        source: CallPathSource | str = CallPathSource.DIRECT,
    ) -> str | None:
        """按调用来源解析 tool_id (name@version 形态, 便捷入口)

        Returns:
            解析到的 tool_id; 版本链不存在时返回 None
        """
        entry = await self.resolve(tool_name, source)
        if entry is None:
            return None
        return f"{entry.tool_name}@{entry.version}"

    def __repr__(self) -> str:
        return (
            f"<CallPathRouter store={'yes' if self._store else 'no'} "
            f"skill_store={'yes' if self._skill_store else 'no'}>"
        )
