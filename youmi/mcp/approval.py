"""
ApprovalManager — 工具申请审批管理

从 MasterAgent 中提取的三级审批逻辑，独立为可复用模块:
- AUTO: 工具在自动审批清单内，自动通过
- MANUAL: 敏感工具需人工审批
- MASTER: 超出 Agent 授权范围，由 Master Agent 决策

审批流程:
1. Agent 通过 submit_request() 提交工具申请
2. ApprovalManager.evaluate() 根据规则决定审批级别
3. AUTO 级别自动通过; MANUAL/MASTER 级别进入待审批队列
4. approve() / deny() 处理待审批申请
5. get_audit_log() 返回完整审计日志

用法::

    from youmi.mcp.approval import ApprovalManager, ApprovalLevel

    manager = ApprovalManager(
        auto_approve_list={"file_read", "file_write"},
        sensitive_tools={"shell_exec", "delete_file"},
    )

    # 提交申请
    record = manager.submit_request(agent_id="agent-001", tool_name="shell_exec")

    # 查看审批级别
    level = manager.evaluate("agent-001", "shell_exec")
    assert level == ApprovalLevel.MANUAL

    # 审批
    manager.approve(record.record_id, decided_by="user", reason="已确认安全")

    # 审计日志
    log = manager.get_audit_log()
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class ApprovalLevel(str, Enum):
    """审批级别"""

    AUTO = "auto"        # 自动审批 (工具在 auto_approve_list 中)
    MANUAL = "manual"    # 人工审批 (工具在 sensitive_tools 中)
    MASTER = "master"    # Master Agent 审批 (默认)


class ApprovalDecision(str, Enum):
    """审批决策"""

    APPROVED = "approved"
    DENIED = "denied"
    PENDING = "pending"


# ---------------------------------------------------------------------------
# 审批记录
# ---------------------------------------------------------------------------

class ApprovalRecord(BaseModel):
    """工具审批记录

    Args:
        record_id: 审批记录唯一 ID
        agent_id: 申请工具的 Agent ID
        tool_name: 申请的工具名称
        level: 审批级别
        decision: 审批决策
        reason: 决策理由
        decided_by: 决策者 (agent_id / "user" / "system")
        timestamp: 创建时间
    """

    record_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    agent_id: str = ""
    tool_name: str = ""
    level: ApprovalLevel = ApprovalLevel.MASTER
    decision: ApprovalDecision = ApprovalDecision.PENDING
    reason: str = ""
    decided_by: str = ""
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ---------------------------------------------------------------------------
# ApprovalManager
# ---------------------------------------------------------------------------

class ApprovalManager:
    """工具审批管理器

    三级审批逻辑:
    1. 工具在 auto_approve_list 中 → AUTO (自动通过)
    2. 工具在 sensitive_tools 中 → MANUAL (人工审批)
    3. 其他 → MASTER (Master Agent 决策)

    Args:
        auto_approve_list: 自动审批的工具名称集合
        sensitive_tools: 需人工审批的敏感工具集合
    """

    def __init__(
        self,
        auto_approve_list: set[str] | None = None,
        sensitive_tools: set[str] | None = None,
    ) -> None:
        self._auto_approve_list: set[str] = auto_approve_list or set()
        self._sensitive_tools: set[str] = sensitive_tools or set()
        self._records: dict[str, ApprovalRecord] = {}
        self._audit_log: list[ApprovalRecord] = []

    # ==================================================================
    # 配置
    # ==================================================================

    @property
    def auto_approve_list(self) -> set[str]:
        return self._auto_approve_list

    @property
    def sensitive_tools(self) -> set[str]:
        return self._sensitive_tools

    def set_auto_approve_list(self, tools: set[str]) -> None:
        """设置自动审批清单"""
        self._auto_approve_list = set(tools)

    def set_sensitive_tools(self, tools: set[str]) -> None:
        """设置敏感工具列表"""
        self._sensitive_tools = set(tools)

    def add_auto_approve(self, tool_name: str) -> None:
        """添加自动审批工具"""
        self._auto_approve_list.add(tool_name)

    def add_sensitive_tool(self, tool_name: str) -> None:
        """添加敏感工具"""
        self._sensitive_tools.add(tool_name)

    # ==================================================================
    # 审批评估
    # ==================================================================

    def evaluate(self, agent_id: str, tool_name: str) -> ApprovalLevel:
        """评估工具申请的审批级别

        规则:
        1. 工具在 auto_approve_list 中 → AUTO
        2. 工具在 sensitive_tools 中 → MANUAL
        3. 其他 → MASTER

        Args:
            agent_id: 申请的 Agent ID
            tool_name: 申请的工具名称

        Returns:
            ApprovalLevel
        """
        if tool_name in self._auto_approve_list:
            return ApprovalLevel.AUTO

        if tool_name in self._sensitive_tools:
            return ApprovalLevel.MANUAL

        return ApprovalLevel.MASTER

    # ==================================================================
    # 申请与审批
    # ==================================================================

    def submit_request(self, agent_id: str, tool_name: str) -> ApprovalRecord:
        """提交工具申请

        自动评估审批级别:
        - AUTO 级别自动通过
        - MANUAL/MASTER 级别进入待审批队列

        Args:
            agent_id: 申请的 Agent ID
            tool_name: 申请的工具名称

        Returns:
            ApprovalRecord 审批记录
        """
        level = self.evaluate(agent_id, tool_name)

        record = ApprovalRecord(
            agent_id=agent_id,
            tool_name=tool_name,
            level=level,
        )

        if level == ApprovalLevel.AUTO:
            # 自动通过
            record.decision = ApprovalDecision.APPROVED
            record.decided_by = "system"
            record.reason = f"工具 '{tool_name}' 在自动审批清单中"
            logger.debug(
                "ApprovalManager: auto-approved '%s' for agent '%s'",
                tool_name, agent_id,
            )
        else:
            logger.info(
                "ApprovalManager: request for '%s' from agent '%s' → %s",
                tool_name, agent_id, level.value,
            )

        self._records[record.record_id] = record
        self._audit_log.append(record.model_copy())
        return record

    def approve(
        self,
        record_id: str,
        decided_by: str,
        reason: str = "",
    ) -> ApprovalRecord | None:
        """批准工具申请

        Args:
            record_id: 审批记录 ID
            decided_by: 决策者标识
            reason: 批准理由

        Returns:
            更新后的 ApprovalRecord，如果记录不存在返回 None
        """
        record = self._records.get(record_id)
        if record is None:
            logger.warning("ApprovalManager: record '%s' not found", record_id)
            return None

        if record.decision != ApprovalDecision.PENDING:
            logger.warning(
                "ApprovalManager: record '%s' already decided (%s)",
                record_id, record.decision.value,
            )
            return record

        record.decision = ApprovalDecision.APPROVED
        record.decided_by = decided_by
        record.reason = reason

        logger.info(
            "ApprovalManager: approved '%s' for agent '%s' by '%s'",
            record.tool_name, record.agent_id, decided_by,
        )

        # 追加审计日志
        self._audit_log.append(record.model_copy())
        return record

    def deny(
        self,
        record_id: str,
        decided_by: str,
        reason: str = "",
    ) -> ApprovalRecord | None:
        """拒绝工具申请

        Args:
            record_id: 审批记录 ID
            decided_by: 决策者标识
            reason: 拒绝理由

        Returns:
            更新后的 ApprovalRecord，如果记录不存在返回 None
        """
        record = self._records.get(record_id)
        if record is None:
            logger.warning("ApprovalManager: record '%s' not found", record_id)
            return None

        if record.decision != ApprovalDecision.PENDING:
            logger.warning(
                "ApprovalManager: record '%s' already decided (%s)",
                record_id, record.decision.value,
            )
            return record

        record.decision = ApprovalDecision.DENIED
        record.decided_by = decided_by
        record.reason = reason

        logger.info(
            "ApprovalManager: denied '%s' for agent '%s' by '%s': %s",
            record.tool_name, record.agent_id, decided_by, reason,
        )

        # 追加审计日志
        self._audit_log.append(record.model_copy())
        return record

    # ==================================================================
    # 查询
    # ==================================================================

    def get_record(self, record_id: str) -> ApprovalRecord | None:
        """获取审批记录"""
        return self._records.get(record_id)

    def get_pending_requests(self) -> list[ApprovalRecord]:
        """获取所有待审批的申请"""
        return [
            r for r in self._records.values()
            if r.decision == ApprovalDecision.PENDING
        ]

    def get_pending_for_agent(self, agent_id: str) -> list[ApprovalRecord]:
        """获取指定 Agent 的待审批申请"""
        return [
            r for r in self._records.values()
            if r.agent_id == agent_id and r.decision == ApprovalDecision.PENDING
        ]

    def get_audit_log(self) -> list[ApprovalRecord]:
        """获取完整审计日志 (按时间顺序)"""
        return list(self._audit_log)

    def get_approved_tools(self, agent_id: str) -> list[str]:
        """获取指定 Agent 已获批的工具名称列表"""
        return [
            r.tool_name for r in self._records.values()
            if r.agent_id == agent_id and r.decision == ApprovalDecision.APPROVED
        ]

    # ==================================================================
    # 批量操作
    # ==================================================================

    def clear_resolved(self) -> int:
        """清理已处理的审批记录 (保留审计日志)

        Returns:
            清理的记录数量
        """
        to_remove = [
            rid for rid, r in self._records.items()
            if r.decision != ApprovalDecision.PENDING
        ]
        for rid in to_remove:
            del self._records[rid]
        return len(to_remove)

    def reset(self) -> None:
        """重置所有审批状态 (清空记录和审计日志)"""
        self._records.clear()
        self._audit_log.clear()
        logger.debug("ApprovalManager: reset")

    # ==================================================================
    # 诊断
    # ==================================================================

    @property
    def total_requests(self) -> int:
        return len(self._records)

    @property
    def pending_count(self) -> int:
        return sum(1 for r in self._records.values() if r.decision == ApprovalDecision.PENDING)

    @property
    def approved_count(self) -> int:
        return sum(1 for r in self._records.values() if r.decision == ApprovalDecision.APPROVED)

    @property
    def denied_count(self) -> int:
        return sum(1 for r in self._records.values() if r.decision == ApprovalDecision.DENIED)

    def __repr__(self) -> str:
        return (
            f"<ApprovalManager pending={self.pending_count} "
            f"approved={self.approved_count} denied={self.denied_count}>"
        )
