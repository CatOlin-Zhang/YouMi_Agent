"""
ConeRetriever — 锥形检索融合层

锥形区域 = 语义方向 ∩ tag 边界 ∩ 风险边界 ∩ 权限边界:

- 语义方向 + tag + 风险边界: 优先委托 ToolStore.search_cone()
  (KNN 子查询 JOIN 业务表，一条 SQL 内联合过滤)
- 权限边界: 权限是 per-Agent 运行时状态而非库内静态列，
  因此在 Python 层判定 ``set(required_permissions) ⊆ set(granted)``
- 纯 ToolVault (无 store) 模式: vault.search() 召回后在内存做
  风险/权限/lineage 联合过滤 (tag 边界依赖 store 表，此模式跳过)

用法::

    from youmi.mcp.cone import ConeQuery, ConeRetriever

    retriever = ConeRetriever(vault=vault, store=store)
    results = await retriever.retrieve(ConeQuery(
        query="我需要一个发送邮件的工具",
        tags=["communication"],
        risk_ceiling="high",
        granted_permissions={"fs:read", "net:smtp"},
    ))
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from youmi.core.tool import RiskLevel, risk_rank
from youmi.mcp.models import ToolSearchResult

logger = logging.getLogger(__name__)


@dataclass
class ConeQuery:
    """锥形检索查询 — 描述一个"锥形区域"的全部边界

    Args:
        query: 自然语言查询 (语义方向, 锥形的轴)
        tags: tag 边界 (命中任意一个 tag 才可召回; None/空 = 不限)
        risk_ceiling: 风险上限 (risk_level 高于此级别的工具不召回)
        granted_permissions: 调用者已被授予的权限集合。
            None = 不限制 (无权限系统的 Agent);
            空集 = 最严格 (任何 required_permissions 非空的工具都被裁掉)
        exclude_names: 排除的工具名称集合 (已可见/已否决)
        exclude_lineages: 排除的版本链集合 (GitLineageGuard 去重)
        top_k: 返回结果数量
        min_score: 最低相似度阈值
    """

    query: str
    tags: list[str] | None = None
    risk_ceiling: str = RiskLevel.CRITICAL
    granted_permissions: set[str] | None = None
    exclude_names: set[str] | None = None
    exclude_lineages: set[str] | None = None
    top_k: int = 5
    min_score: float = 0.3


@dataclass
class ConeStats:
    """锥形检索统计 — 各边界裁剪了多少候选 (诊断/审计用)"""

    recalled: int = 0            # 语义召回的原始候选数
    risk_filtered: int = 0       # 被风险边界裁掉的数量
    permission_filtered: int = 0  # 被权限边界裁掉的数量
    lineage_filtered: int = 0    # 被版本链去重裁掉的数量
    returned: int = 0            # 最终返回数量


@dataclass
class ConeResult:
    """锥形检索结果 — 结果列表 + 裁剪统计"""

    results: list[ToolSearchResult] = field(default_factory=list)
    stats: ConeStats = field(default_factory=ConeStats)

    def __bool__(self) -> bool:
        return bool(self.results)


def permissions_satisfied(
    required: list[str], granted: set[str] | None,
) -> bool:
    """权限子集判定: required ⊆ granted

    granted 为 None 表示无权限系统 (不限制，恒通过);
    required 为空表示无门槛工具 (恒通过)。
    """
    if granted is None:
        return True
    return set(required) <= granted


class ConeRetriever:
    """锥形检索融合层 — 语义方向 ∩ tag ∩ 风险 ∩ 权限

    优先级: ToolStore.search_cone() (SQL 锥形) > ToolVault 内存锥形。
    权限边界始终在 Python 层判定 (per-Agent 运行时状态)。

    Args:
        vault: ToolVault 实例 (内存候选来源)
        store: ToolStore 实例 (SQL 锥形来源, 优先)
    """

    def __init__(
        self,
        vault: Any = None,
        store: Any = None,
    ) -> None:
        self._vault = vault
        self._store = store

    @property
    def store(self) -> Any:
        return self._store

    @property
    def vault(self) -> Any:
        return self._vault

    async def retrieve(self, cone: ConeQuery) -> list[ToolSearchResult]:
        """执行锥形检索 (便捷入口, 等价 retrieve_detailed().results)"""
        return (await self.retrieve_detailed(cone)).results

    async def retrieve_detailed(self, cone: ConeQuery) -> ConeResult:
        """执行锥形检索并返回裁剪统计

        流程:
        1. 有 store → search_cone() (SQL 内完成 tag/风险/名称/lineage 裁剪)
        2. 仅 vault → vault.search() 召回后内存过滤
        3. 权限边界在 Python 层统一判定 (两条路径共用)
        """
        if self._store is not None:
            results = await self._retrieve_via_store(cone)
        elif self._vault is not None:
            results = await self._retrieve_via_vault(cone)
        else:
            return ConeResult()

        # 权限边界 (Python 层, per-Agent 运行时状态)
        stats = ConeStats(recalled=len(results))
        permitted: list[ToolSearchResult] = []
        for r in results:
            if not permissions_satisfied(
                r.required_permissions, cone.granted_permissions,
            ):
                stats.permission_filtered += 1
                logger.debug(
                    "ConeRetriever: '%s' blocked by permission "
                    "(requires %s, granted %s)",
                    r.tool_name, r.required_permissions,
                    cone.granted_permissions,
                )
                continue
            permitted.append(r)

        stats.returned = len(permitted[:cone.top_k])
        return ConeResult(results=permitted[:cone.top_k], stats=stats)

    # ------------------------------------------------------------------
    # Store 路径: SQL 锥形
    # ------------------------------------------------------------------

    async def _retrieve_via_store(self, cone: ConeQuery) -> list[ToolSearchResult]:
        """ToolStore.search_cone — 语义/tag/风险/排除在一条 SQL 内联合"""
        # fetch 覆盖面放大: SQL 内已裁剪, 但权限裁剪还在后面,
        # 多取一些避免权限裁剪后不足 top_k
        fetch_k = cone.top_k * 2 if cone.top_k > 0 else cone.top_k
        try:
            return await self._store.search_cone(
                cone.query,
                top_k=fetch_k,
                min_score=cone.min_score,
                exclude=cone.exclude_names,
                tags=cone.tags,
                risk_ceiling=cone.risk_ceiling,
                exclude_lineages=cone.exclude_lineages,
            )
        except Exception as exc:
            logger.warning("ConeRetriever: store cone search failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Vault 路径: 内存锥形
    # ------------------------------------------------------------------

    async def _retrieve_via_vault(self, cone: ConeQuery) -> list[ToolSearchResult]:
        """ToolVault 内存召回 + Python 层风险/lineage 过滤

        tag 边界依赖 store 的 tool_tags 表，纯 vault 模式跳过
        (记录 debug 日志)。
        """
        if cone.tags:
            logger.debug(
                "ConeRetriever: tag boundary %s ignored in vault-only mode "
                "(tags live in ToolStore)", cone.tags,
            )

        # fetch 覆盖面放大: 与 store 路径一致, 权限/风险/lineage 裁剪
        # 在 Python 层进行, 多取一些避免裁剪后不足 top_k
        fetch_k = cone.top_k * 2 if cone.top_k > 0 else cone.top_k
        try:
            results = await self._vault.search(
                cone.query,
                top_k=fetch_k,
                min_score=cone.min_score,
                exclude=cone.exclude_names,
            )
        except Exception as exc:
            logger.warning("ConeRetriever: vault search failed: %s", exc)
            return []

        ceiling = risk_rank(cone.risk_ceiling)
        filtered: list[ToolSearchResult] = []
        for r in results:
            if risk_rank(r.risk_level) > ceiling:
                continue
            lineage = r.lineage_id or r.tool_name
            if cone.exclude_lineages and lineage in cone.exclude_lineages:
                continue
            filtered.append(r)
        return filtered

    def __repr__(self) -> str:
        return (
            f"<ConeRetriever store={'yes' if self._store else 'no'} "
            f"vault={'yes' if self._vault else 'no'}>"
        )
