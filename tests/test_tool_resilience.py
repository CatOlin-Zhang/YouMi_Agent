"""
M1 工具治理测试 — ToolExecutionMixin 的熔断 / 超时 / 审计

覆盖:
- 成功路径: 结果返回 + conversation 写入 + 审计 ok
- 失败路径: 工具异常 / 未注册 / MCP is_error → 审计 error + 熔断计数
- 熔断: 连续失败达阈值后快速失败（不再执行工具）；拒绝不计入失败统计
- 熔断恢复: 成功后连续失败计数清零
- 超时兜底: YOUMI_TOOL_TIMEOUT_S 生效，超时补写 conversation
- MCP 路径: 成功 / is_error / bridge 异常（保持传播语义）
"""

from __future__ import annotations

import asyncio
import json

import pytest

import youmi.core.tool_executor as _tool_executor
from youmi.core.resilience import (
    CircuitBreakerConfig,
    CircuitBreakerRegistry,
    CircuitState,
)
from youmi.core.tool_executor import ToolExecutionMixin
from youmi.observability import AuditLogger


# ---------------------------------------------------------------------------
# 测试桩
# ---------------------------------------------------------------------------

class _FakeMcpResult:
    def __init__(self, text: str, is_error: bool = False) -> None:
        self.text = text
        self.is_error = is_error


class _FakeBridge:
    """模拟 ToolBridge.call_tool"""

    def __init__(
        self,
        *,
        text: str = "ok",
        is_error: bool = False,
        exc: Exception | None = None,
    ) -> None:
        self.text = text
        self.is_error = is_error
        self.exc = exc
        self.calls = 0

    async def call_tool(self, name, arguments):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return _FakeMcpResult(self.text, self.is_error)


class _FakeRegistry:
    """模拟 ToolRegistry（仅 __contains__ / execute）"""

    def __init__(self, handler=None) -> None:
        self.handler = handler or (lambda args: "ok")
        self.calls = 0

    def __contains__(self, name: str) -> bool:
        return name != "missing_tool"

    async def execute(self, name, arguments):
        self.calls += 1
        result = self.handler(arguments)
        if asyncio.iscoroutine(result):
            result = await result
        return result


class _FakeMemory:
    async def on_message(self, role, content, tool_name=""):
        pass


class _FakeAgent(ToolExecutionMixin):
    """最小 Agent 桩 — 仅提供 ToolExecutionMixin 所需属性"""

    def __init__(self, *, tool_registry=None, bridge=None) -> None:
        self._tool_registry = tool_registry or _FakeRegistry()
        self._tool_bridge = bridge
        self._conversation: list[dict] = []
        self._memory = _FakeMemory()
        self._tool_guardian_id = ""
        self._bus = None
        self._workflow_id = ""
        self.agent_id = "agent-1"
        self.name = "test-agent"


@pytest.fixture
def gov(monkeypatch):
    """注入独立熔断器注册表与审计实例（避免进程级单例污染）"""
    breakers = CircuitBreakerRegistry(
        CircuitBreakerConfig(failure_threshold=2, recovery_timeout_s=60.0)
    )
    audit = AuditLogger()
    monkeypatch.setattr(_tool_executor, "get_breaker_registry", lambda: breakers)
    monkeypatch.setattr(_tool_executor, "get_audit_logger", lambda: audit)
    return breakers, audit


# ---------------------------------------------------------------------------
# ToolRegistry 路径
# ---------------------------------------------------------------------------

class TestToolGovernance:
    async def test_success_audited(self, gov):
        breakers, audit = gov
        agent = _FakeAgent(tool_registry=_FakeRegistry(lambda a: "结果内容"))

        action = await agent._do_execute_tool("file_read", {"path": "x.txt"}, "call_1")
        assert action.success
        assert action.output == "结果内容"
        assert agent._conversation[-1]["content"] == "结果内容"

        ev = audit.get_recent(event_type="tool_call")[0]
        assert ev.tool_name == "file_read"
        assert ev.status == "ok"
        assert ev.agent_id == "agent-1"
        assert ev.detail["arguments"]["path"] == "x.txt"

        breaker = breakers.get("tool:file_read")
        assert breaker.failure_count == 0

    async def test_failure_recorded(self, gov):
        breakers, audit = gov

        def boom(args):
            raise RuntimeError("工具内部错误")

        agent = _FakeAgent(tool_registry=_FakeRegistry(boom))
        action = await agent._do_execute_tool("bad_tool", {}, "call_1")

        assert not action.success
        assert "工具内部错误" in action.error
        # conversation 写入错误 JSON
        payload = json.loads(agent._conversation[-1]["content"])
        assert "error" in payload

        ev = audit.get_recent(event_type="tool_call")[0]
        assert ev.status == "error"
        assert breakers.get("tool:bad_tool").failure_count == 1

    async def test_unregistered_tool(self, gov):
        breakers, audit = gov
        agent = _FakeAgent(tool_registry=_FakeRegistry())

        action = await agent._do_execute_tool("missing_tool", {}, "call_1")
        assert not action.success
        assert "未注册" in action.error

        ev = audit.get_recent(event_type="tool_call")[0]
        assert ev.status == "error"

    async def test_circuit_opens_and_rejects(self, gov):
        breakers, audit = gov

        def boom(args):
            raise RuntimeError("boom")

        fake_registry = _FakeRegistry(boom)
        agent = _FakeAgent(tool_registry=fake_registry)

        # failure_threshold=2 → 两次失败后熔断
        r1 = await agent._do_execute_tool("bad_tool", {}, "c1")
        r2 = await agent._do_execute_tool("bad_tool", {}, "c2")
        assert not r1.success and not r2.success
        assert fake_registry.calls == 2

        breaker = breakers.get("tool:bad_tool")
        assert breaker.state == CircuitState.OPEN

        # 第三次 — 熔断拒绝，工具不再执行
        r3 = await agent._do_execute_tool("bad_tool", {}, "c3")
        assert not r3.success
        assert "熔断" in r3.error
        assert fake_registry.calls == 2
        # 拒绝消息写入 conversation（供 LLM 感知）
        assert "熔断" in agent._conversation[-1]["content"]

        # 拒绝事件也记录审计
        ev = audit.get_recent(event_type="tool_call")[0]
        assert ev.status == "error"
        assert "熔断" in ev.error

        # 拒绝不计入失败统计
        stats = breaker.stats()
        assert stats["total_failure"] == 2
        assert stats["rejected"] == 1

    async def test_success_resets_failure_count(self, gov):
        breakers, audit = gov
        seq = iter([RuntimeError("fail"), "ok", RuntimeError("fail"), "ok"])

        def handler(args):
            item = next(seq)
            if isinstance(item, Exception):
                raise item
            return item

        agent = _FakeAgent(tool_registry=_FakeRegistry(handler))

        assert not (await agent._do_execute_tool("flaky", {}, "c1")).success
        assert (await agent._do_execute_tool("flaky", {}, "c2")).success
        assert not (await agent._do_execute_tool("flaky", {}, "c3")).success
        assert (await agent._do_execute_tool("flaky", {}, "c4")).success

        breaker = breakers.get("tool:flaky")
        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

    async def test_timeout_fallback(self, gov, monkeypatch):
        breakers, audit = gov
        monkeypatch.setenv("YOUMI_TOOL_TIMEOUT_S", "0.1")

        async def slow(args):
            await asyncio.sleep(5)

        agent = _FakeAgent(tool_registry=_FakeRegistry(slow))
        action = await agent._do_execute_tool("slow_tool", {}, "c1")

        assert not action.success
        assert "超时" in action.error
        # conversation 补写超时错误
        payload = json.loads(agent._conversation[-1]["content"])
        assert "超时" in payload["error"]

        ev = audit.get_recent(event_type="tool_call")[0]
        assert ev.status == "error"
        assert "超时" in ev.error
        assert breakers.get("tool:slow_tool").failure_count == 1


# ---------------------------------------------------------------------------
# MCP (ToolBridge) 路径
# ---------------------------------------------------------------------------

class TestMcpPath:
    async def test_mcp_success_audited(self, gov):
        breakers, audit = gov
        bridge = _FakeBridge(text="MCP 结果")
        agent = _FakeAgent(bridge=bridge)

        action = await agent._do_execute_tool("mcp_tool", {}, "c1")
        assert action.success
        assert action.output == "MCP 结果"
        assert bridge.calls == 1

        ev = audit.get_recent(event_type="tool_call")[0]
        assert ev.tool_name == "mcp_tool"
        assert ev.status == "ok"

    async def test_mcp_error_result(self, gov):
        breakers, audit = gov
        bridge = _FakeBridge(text="工具执行错误", is_error=True)
        agent = _FakeAgent(bridge=bridge)

        action = await agent._do_execute_tool("mcp_tool", {}, "c1")
        assert not action.success
        assert action.error == "工具执行错误"

        ev = audit.get_recent(event_type="tool_call")[0]
        assert ev.status == "error"
        assert breakers.get("tool:mcp_tool").failure_count == 1

    async def test_mcp_exception_recorded_and_reraised(self, gov):
        breakers, audit = gov
        bridge = _FakeBridge(exc=RuntimeError("bridge 崩溃"))
        agent = _FakeAgent(bridge=bridge)

        with pytest.raises(RuntimeError):
            await agent._do_execute_tool("mcp_tool", {}, "c1")

        # 异常路径：熔断计数 + 审计 error
        assert breakers.get("tool:mcp_tool").failure_count == 1
        ev = audit.get_recent(event_type="tool_call")[0]
        assert ev.status == "error"
        assert "bridge 崩溃" in ev.error
