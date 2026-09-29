"""
AuditGate — 召回审计闸门

召回命中后、加载 schema **前**的独立审计拦截层（P1 差距 8）:
此前审计发生在 confirm 之后/执行时，风险工具的 schema 已进入
LLM 上下文。AuditGate 在「候选确认 → 加载」之间插入一次判定:

- BLOCK: required_permissions 未被授予（权限不满足，直接拦截）
- MANUAL: risk_level 超过阈值（转人工审批，暂不加载 schema）
- PASS: 放行加载

每一判定均记录审计事件（event_type = ``tool.recall_audit``，
复用 observability AuditLogger 的 JSONL 通道，detail 自动脱敏）。

用法::

    from youmi.mcp.audit_gate import AuditGate

    gate = AuditGate(
        approval_manager=ApprovalManager(...),
        risk_threshold="high",          # critical 超阈值 → MANUAL
        audit_logger=AuditLogger(path="audit.jsonl"),
    )
    result = await gate.check(candidate, agent_id="agent-001",
                              granted_permissions={"fs:read"})
    if result.blocked:   # 权限不满足
        ...
    elif result.manual:  # 已入待审队列 (result.record_id)
        ...
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from youmi.core.tool import RiskLevel, risk_rank
from youmi.mcp.models import ToolSearchResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 判定三态
# ---------------------------------------------------------------------------

class AuditDecision(str, Enum):
    """审计判定三态"""

    PASS = "pass"      # 放行加载
    BLOCK = "block"    # 拦截 (required_permissions 未被授予)
    MANUAL = "manual"  # 转人工审批 (risk_level 超过阈值)


@dataclass
class AuditResult:
    """审计判定结果

    Attributes:
        decision: 判定三态
        reason: 判定理由 (BLOCK 含缺失权限, MANUAL 含风险级别)
        record_id: MANUAL 时生成的审批记录 ID (无 ApprovalManager 时为空)
    """

    decision: AuditDecision = AuditDecision.PASS
    reason: str = ""
    record_id: str = ""

    @property
    def passed(self) -> bool:
        return self.decision is AuditDecision.PASS

    @property
    def blocked(self) -> bool:
        return self.decision is AuditDecision.BLOCK

    @property
    def manual(self) -> bool:
        return self.decision is AuditDecision.MANUAL


# 审计事件类型 — 与 observability 现有事件命名风格一致
# (llm_call / tool_call / approval / auth / sandbox / tool.recall_audit)
AUDIT_EVENT_TYPE = "tool.recall_audit"

# 判定三态 → AuditEvent.status (audit.py 惯例: ok/error/denied/blocked,
# MANUAL 属"待审批"语义用 pending)
_STATUS_BY_DECISION: dict[AuditDecision, str] = {
    AuditDecision.PASS: "ok",
    AuditDecision.BLOCK: "blocked",
    AuditDecision.MANUAL: "pending",
}


# ---------------------------------------------------------------------------
# AuditGate
# ---------------------------------------------------------------------------

class AuditGate:
    """召回审计闸门 — 召回命中后、加载 schema 前拦截

    规则（优先级从高到低）:
    1. ``set(required_permissions) ⊄ set(granted_permissions)`` → BLOCK
       （granted=None 表示无权限系统，不限制）
    2. ``risk_rank(risk_level) > risk_rank(risk_threshold)`` → MANUAL
       （有 ApprovalManager 时自动 submit_request 入待审队列）
    3. 否则 → PASS

    Args:
        approval_manager: ApprovalManager 实例（可选; MANUAL 时入待审队列）
        risk_threshold: MANUAL 阈值（risk_rank 高于此值转人工;
            默认 "high" → 仅 critical 触发，与 approval.py 联动一致）
        audit_logger: AuditLogger 实例（可选; 记录 tool.recall_audit 事件）
    """

    def __init__(
        self,
        approval_manager: Any = None,
        risk_threshold: str = RiskLevel.HIGH,
        audit_logger: Any = None,
    ) -> None:
        self._approval_manager = approval_manager
        self._risk_threshold = risk_threshold
        self._audit_logger = audit_logger

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def risk_threshold(self) -> str:
        return self._risk_threshold

    @risk_threshold.setter
    def risk_threshold(self, value: str) -> None:
        self._risk_threshold = value

    @property
    def approval_manager(self) -> Any:
        return self._approval_manager

    @property
    def audit_logger(self) -> Any:
        return self._audit_logger

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------

    async def check(
        self,
        candidate: ToolSearchResult,
        agent_id: str,
        granted_permissions: set[str] | None = None,
    ) -> AuditResult:
        """审计检查 — 在加载 schema 前调用

        Args:
            candidate: 召回候选 (携带 risk_level / required_permissions /
                lineage_id，由锥形检索或 vault 元数据提供)
            agent_id: 请求加载的 Agent ID
            granted_permissions: 调用者已被授予的权限集合
                (None = 无权限系统，不限制)

        Returns:
            AuditResult（PASS / BLOCK / MANUAL + reason）
        """
        # 规则 1: 权限边界 — required ⊄ granted → BLOCK
        if granted_permissions is not None and candidate.required_permissions:
            missing = set(candidate.required_permissions) - granted_permissions
            if missing:
                result = AuditResult(
                    decision=AuditDecision.BLOCK,
                    reason=(
                        f"权限不满足: 需要权限 {sorted(missing)} "
                        f"未被授予 (required={candidate.required_permissions})"
                    ),
                )
                logger.info(
                    "AuditGate: BLOCK '%s' for agent '%s' "
                    "(missing permissions: %s)",
                    candidate.tool_name, agent_id, sorted(missing),
                )
                self._log_event(
                    candidate, agent_id, result,
                    missing_permissions=sorted(missing),
                )
                return result

        # 规则 2: 风险边界 — risk_level 超过阈值 → MANUAL
        if risk_rank(candidate.risk_level) > risk_rank(self._risk_threshold):
            record_id = ""
            if self._approval_manager is not None:
                try:
                    record = self._approval_manager.submit_request(
                        agent_id,
                        candidate.tool_name,
                        risk_level=candidate.risk_level,
                    )
                    record_id = record.record_id
                except Exception as exc:
                    # 提交失败不改变判定（仍 MANUAL），仅降级日志
                    logger.warning(
                        "AuditGate: submit_request failed for '%s': %s",
                        candidate.tool_name, exc,
                    )
            result = AuditResult(
                decision=AuditDecision.MANUAL,
                reason=(
                    f"风险级别 {candidate.risk_level} 超过阈值 "
                    f"{self._risk_threshold}，转人工审批"
                ),
                record_id=record_id,
            )
            logger.info(
                "AuditGate: MANUAL '%s' for agent '%s' "
                "(risk=%s > threshold=%s, record=%s)",
                candidate.tool_name, agent_id, candidate.risk_level,
                self._risk_threshold, record_id or "-",
            )
            self._log_event(candidate, agent_id, result)
            return result

        # 规则 3: PASS
        result = AuditResult(decision=AuditDecision.PASS)
        logger.debug(
            "AuditGate: PASS '%s' for agent '%s'",
            candidate.tool_name, agent_id,
        )
        self._log_event(candidate, agent_id, result)
        return result

    # ------------------------------------------------------------------
    # 审计事件
    # ------------------------------------------------------------------

    def _log_event(
        self,
        candidate: ToolSearchResult,
        agent_id: str,
        result: AuditResult,
        **extra: Any,
    ) -> None:
        """记录 tool.recall_audit 审计事件 (三态均记录, BLOCK 含 reason)"""
        if self._audit_logger is None:
            return

        detail: dict[str, Any] = {
            "decision": result.decision.value,
            "risk_level": candidate.risk_level,
            "required_permissions": list(candidate.required_permissions),
            "risk_threshold": self._risk_threshold,
            "lineage_id": candidate.lineage_id or candidate.tool_name,
        }
        if result.reason:
            detail["reason"] = result.reason
        if result.record_id:
            detail["record_id"] = result.record_id
        detail.update(extra)

        try:
            self._audit_logger.log(
                AUDIT_EVENT_TYPE,
                agent_id=agent_id,
                tool_name=candidate.tool_name,
                status=_STATUS_BY_DECISION[result.decision],
                detail=detail,
            )
        except Exception as exc:
            # 审计降级: 写入失败不影响主流程
            logger.warning("AuditGate: audit log failed: %s", exc)

    def __repr__(self) -> str:
        return (
            f"<AuditGate threshold={self._risk_threshold!r} "
            f"approval={'yes' if self._approval_manager else 'no'} "
            f"logger={'yes' if self._audit_logger else 'no'}>"
        )
