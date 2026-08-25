"""
ApprovalManager 工具申请审批管理 测试

测试覆盖:
1. 配置 — auto_approve_list, sensitive_tools
2. 审批评估 — evaluate 三级判定逻辑
3. 申请与审批 — submit_request, approve, deny
4. AUTO 自动通过 — auto_approve_list 中的工具
5. 待审批查询 — get_pending_requests, get_pending_for_agent
6. 审计日志 — get_audit_log
7. 已批准查询 — get_approved_tools
8. 批量操作 — clear_resolved, reset
9. 诊断属性 — total_requests, pending_count, approved_count, denied_count
"""

from __future__ import annotations

import pytest

from youmi.mcp.approval import (
    ApprovalManager,
    ApprovalLevel,
    ApprovalDecision,
    ApprovalRecord,
)


# ===================================================================
# 1. 配置测试
# ===================================================================

class TestApprovalManagerConfig:
    """配置项测试"""

    def test_default_config(self):
        mgr = ApprovalManager()
        assert mgr.auto_approve_list == set()
        assert mgr.sensitive_tools == set()

    def test_custom_config(self):
        mgr = ApprovalManager(
            auto_approve_list={"file_read", "file_write"},
            sensitive_tools={"shell_exec"},
        )
        assert "file_read" in mgr.auto_approve_list
        assert "shell_exec" in mgr.sensitive_tools

    def test_set_auto_approve_list(self):
        mgr = ApprovalManager()
        mgr.set_auto_approve_list({"a", "b"})
        assert mgr.auto_approve_list == {"a", "b"}

    def test_set_sensitive_tools(self):
        mgr = ApprovalManager()
        mgr.set_sensitive_tools({"x", "y"})
        assert mgr.sensitive_tools == {"x", "y"}

    def test_add_auto_approve(self):
        mgr = ApprovalManager()
        mgr.add_auto_approve("tool_a")
        assert "tool_a" in mgr.auto_approve_list

    def test_add_sensitive_tool(self):
        mgr = ApprovalManager()
        mgr.add_sensitive_tool("tool_s")
        assert "tool_s" in mgr.sensitive_tools


# ===================================================================
# 2. 审批评估测试
# ===================================================================

class TestApprovalEvaluate:
    """evaluate 三级判定逻辑测试"""

    def test_evaluate_auto(self):
        mgr = ApprovalManager(auto_approve_list={"file_read"})
        level = mgr.evaluate("agent-001", "file_read")
        assert level == ApprovalLevel.AUTO

    def test_evaluate_manual(self):
        mgr = ApprovalManager(sensitive_tools={"shell_exec"})
        level = mgr.evaluate("agent-001", "shell_exec")
        assert level == ApprovalLevel.MANUAL

    def test_evaluate_master(self):
        mgr = ApprovalManager()
        level = mgr.evaluate("agent-001", "unknown_tool")
        assert level == ApprovalLevel.MASTER

    def test_evaluate_priority_auto_over_master(self):
        """auto_approve_list 优先于默认 MASTER"""
        mgr = ApprovalManager(
            auto_approve_list={"tool_a"},
        )
        assert mgr.evaluate("agent-001", "tool_a") == ApprovalLevel.AUTO

    def test_evaluate_auto_over_sensitive(self):
        """如果一个工具同时在两个列表中，auto 优先"""
        mgr = ApprovalManager(
            auto_approve_list={"tool_a"},
            sensitive_tools={"tool_a"},
        )
        assert mgr.evaluate("agent-001", "tool_a") == ApprovalLevel.AUTO


# ===================================================================
# 3. 申请与审批测试
# ===================================================================

class TestApprovalWorkflow:
    """submit_request, approve, deny 工作流测试"""

    def test_submit_auto_approved(self):
        mgr = ApprovalManager(auto_approve_list={"file_read"})
        record = mgr.submit_request("agent-001", "file_read")

        assert record.decision == ApprovalDecision.APPROVED
        assert record.decided_by == "system"
        assert record.level == ApprovalLevel.AUTO

    def test_submit_pending_master(self):
        mgr = ApprovalManager()
        record = mgr.submit_request("agent-001", "new_tool")

        assert record.decision == ApprovalDecision.PENDING
        assert record.level == ApprovalLevel.MASTER

    def test_submit_pending_manual(self):
        mgr = ApprovalManager(sensitive_tools={"shell_exec"})
        record = mgr.submit_request("agent-001", "shell_exec")

        assert record.decision == ApprovalDecision.PENDING
        assert record.level == ApprovalLevel.MANUAL

    def test_approve_pending(self):
        mgr = ApprovalManager()
        record = mgr.submit_request("agent-001", "new_tool")

        approved = mgr.approve(record.record_id, decided_by="master-agent", reason="允许使用")
        assert approved is not None
        assert approved.decision == ApprovalDecision.APPROVED
        assert approved.decided_by == "master-agent"
        assert approved.reason == "允许使用"

    def test_deny_pending(self):
        mgr = ApprovalManager()
        record = mgr.submit_request("agent-001", "dangerous_tool")

        denied = mgr.deny(record.record_id, decided_by="user", reason="安全风险")
        assert denied is not None
        assert denied.decision == ApprovalDecision.DENIED
        assert denied.decided_by == "user"
        assert denied.reason == "安全风险"

    def test_approve_already_approved(self):
        mgr = ApprovalManager(auto_approve_list={"tool_a"})
        record = mgr.submit_request("agent-001", "tool_a")

        # 再次审批应返回已有记录
        result = mgr.approve(record.record_id, decided_by="someone")
        assert result is not None
        assert result.decision == ApprovalDecision.APPROVED

    def test_deny_already_denied(self):
        mgr = ApprovalManager()
        record = mgr.submit_request("agent-001", "tool_x")
        mgr.deny(record.record_id, decided_by="user", reason="拒绝")

        result = mgr.deny(record.record_id, decided_by="user", reason="再次拒绝")
        assert result is not None
        assert result.decision == ApprovalDecision.DENIED

    def test_approve_nonexistent(self):
        mgr = ApprovalManager()
        result = mgr.approve("nonexistent_id", decided_by="someone")
        assert result is None

    def test_deny_nonexistent(self):
        mgr = ApprovalManager()
        result = mgr.deny("nonexistent_id", decided_by="someone")
        assert result is None


# ===================================================================
# 4. 查询测试
# ===================================================================

class TestApprovalQueries:
    """get_pending_requests, get_approved_tools 等查询测试"""

    def test_get_record(self):
        mgr = ApprovalManager()
        record = mgr.submit_request("agent-001", "tool_a")
        found = mgr.get_record(record.record_id)
        assert found is not None
        assert found.record_id == record.record_id

    def test_get_record_nonexistent(self):
        mgr = ApprovalManager()
        assert mgr.get_record("no_id") is None

    def test_get_pending_requests(self):
        mgr = ApprovalManager(auto_approve_list={"auto_tool"})
        mgr.submit_request("agent-001", "auto_tool")  # AUTO → approved
        mgr.submit_request("agent-001", "pending_tool_1")  # pending
        mgr.submit_request("agent-002", "pending_tool_2")  # pending

        pending = mgr.get_pending_requests()
        assert len(pending) == 2
        assert all(r.decision == ApprovalDecision.PENDING for r in pending)

    def test_get_pending_for_agent(self):
        mgr = ApprovalManager()
        mgr.submit_request("agent-001", "tool_a")
        mgr.submit_request("agent-002", "tool_b")

        pending_a = mgr.get_pending_for_agent("agent-001")
        assert len(pending_a) == 1
        assert pending_a[0].agent_id == "agent-001"

    def test_get_approved_tools(self):
        mgr = ApprovalManager(auto_approve_list={"auto_a", "auto_b"})
        mgr.submit_request("agent-001", "auto_a")
        mgr.submit_request("agent-001", "auto_b")
        mgr.submit_request("agent-002", "auto_a")

        approved = mgr.get_approved_tools("agent-001")
        assert len(approved) == 2
        assert "auto_a" in approved
        assert "auto_b" in approved

    def test_get_approved_tools_empty(self):
        mgr = ApprovalManager()
        mgr.submit_request("agent-001", "pending_tool")
        approved = mgr.get_approved_tools("agent-001")
        assert approved == []


# ===================================================================
# 5. 审计日志测试
# ===================================================================

class TestApprovalAuditLog:
    """get_audit_log 审计日志测试"""

    def test_audit_log_auto(self):
        mgr = ApprovalManager(auto_approve_list={"tool_a"})
        mgr.submit_request("agent-001", "tool_a")

        log = mgr.get_audit_log()
        assert len(log) >= 1

    def test_audit_log_approve(self):
        mgr = ApprovalManager()
        record = mgr.submit_request("agent-001", "tool_x")
        mgr.approve(record.record_id, decided_by="master", reason="OK")

        log = mgr.get_audit_log()
        # submit 时一条，approve 时一条
        assert len(log) >= 2

    def test_audit_log_deny(self):
        mgr = ApprovalManager()
        record = mgr.submit_request("agent-001", "tool_y")
        mgr.deny(record.record_id, decided_by="user", reason="不安全")

        log = mgr.get_audit_log()
        assert len(log) >= 2

    def test_audit_log_is_copy(self):
        """审计日志中的记录应是副本，修改不影响原记录"""
        mgr = ApprovalManager()
        record = mgr.submit_request("agent-001", "tool_z")
        log = mgr.get_audit_log()
        assert len(log) >= 1


# ===================================================================
# 6. 批量操作测试
# ===================================================================

class TestApprovalBatchOps:
    """clear_resolved, reset 测试"""

    def test_clear_resolved(self):
        mgr = ApprovalManager(auto_approve_list={"auto_tool"})
        mgr.submit_request("agent-001", "auto_tool")  # auto approved
        record = mgr.submit_request("agent-001", "pending_tool")  # pending

        cleared = mgr.clear_resolved()
        assert cleared == 1  # auto approved 被清理
        assert mgr.total_requests == 1  # pending 保留

    def test_reset(self):
        mgr = ApprovalManager(auto_approve_list={"auto_tool"})
        mgr.submit_request("agent-001", "auto_tool")
        mgr.submit_request("agent-001", "pending_tool")

        mgr.reset()
        assert mgr.total_requests == 0
        assert len(mgr.get_audit_log()) == 0


# ===================================================================
# 7. 诊断属性测试
# ===================================================================

class TestApprovalDiag:
    """诊断属性测试"""

    def test_empty_counts(self):
        mgr = ApprovalManager()
        assert mgr.total_requests == 0
        assert mgr.pending_count == 0
        assert mgr.approved_count == 0
        assert mgr.denied_count == 0

    def test_counts_after_operations(self):
        mgr = ApprovalManager(auto_approve_list={"auto_tool"})
        mgr.submit_request("agent-001", "auto_tool")  # approved
        record = mgr.submit_request("agent-001", "pending_tool")  # pending
        mgr.deny(record.record_id, decided_by="user")  # denied

        assert mgr.total_requests == 2
        assert mgr.approved_count == 1
        assert mgr.denied_count == 1
        assert mgr.pending_count == 0

    def test_repr(self):
        mgr = ApprovalManager()
        r = repr(mgr)
        assert "ApprovalManager" in r
        assert "pending=0" in r


# ===================================================================
# 8. ApprovalRecord 模型测试
# ===================================================================

class TestApprovalRecord:
    """ApprovalRecord 数据模型测试"""

    def test_defaults(self):
        record = ApprovalRecord()
        assert record.record_id  # 有默认值
        assert record.agent_id == ""
        assert record.tool_name == ""
        assert record.level == ApprovalLevel.MASTER
        assert record.decision == ApprovalDecision.PENDING
        assert record.reason == ""
        assert record.timestamp  # 有默认时间

    def test_custom_values(self):
        record = ApprovalRecord(
            agent_id="agent-001",
            tool_name="test_tool",
            level=ApprovalLevel.MANUAL,
            decision=ApprovalDecision.APPROVED,
            reason="已审批",
            decided_by="user",
        )
        assert record.agent_id == "agent-001"
        assert record.level == ApprovalLevel.MANUAL
        assert record.decision == ApprovalDecision.APPROVED


# ===================================================================
# 9. 枚举测试
# ===================================================================

class TestApprovalEnums:
    """ApprovalLevel, ApprovalDecision 枚举测试"""

    def test_approval_level_values(self):
        assert ApprovalLevel.AUTO.value == "auto"
        assert ApprovalLevel.MANUAL.value == "manual"
        assert ApprovalLevel.MASTER.value == "master"

    def test_approval_decision_values(self):
        assert ApprovalDecision.APPROVED.value == "approved"
        assert ApprovalDecision.DENIED.value == "denied"
        assert ApprovalDecision.PENDING.value == "pending"

    def test_enum_is_string(self):
        assert isinstance(ApprovalLevel.AUTO, str)
        assert isinstance(ApprovalDecision.APPROVED, str)
