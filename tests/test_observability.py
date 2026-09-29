"""
M1 可观测性模块测试 — 审计日志与 OpenTelemetry 追踪

覆盖:
- redact_data: 敏感字段脱敏 / 嵌套结构 / 截断
- AuditLogger: 内存缓冲 / JSONL 落盘 / 查询过滤 / 禁用 / 单例管理
- setup_tracing + JsonlSpanExporter: span 落盘 / 上下文传播 / 异常标记
- span() 辅助: 属性设置与截断
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from youmi.observability import (
    AuditLogger,
    JsonlSpanExporter,
    configure_audit_logger,
    get_audit_logger,
    redact_data,
    reset_audit_logger,
    reset_tracing,
    set_span_attributes,
    setup_tracing,
    shutdown_tracing,
    span,
)


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------

class TestRedactData:
    def test_sensitive_keys(self):
        data = {
            "api_key": "sk-123456",
            "Authorization": "Bearer abc",
            "password": "p@ss",
            "user_token": "tok",
            "model": "gpt-4o",
        }
        out = redact_data(data)
        assert out["api_key"] == "***"
        assert out["Authorization"] == "***"
        assert out["password"] == "***"
        assert out["user_token"] == "***"
        assert out["model"] == "gpt-4o"

    def test_nested_and_lists(self):
        data = {
            "headers": {"X-Auth-Token": "t", "Accept": "json"},
            "items": [{"secret": "s", "name": "n"}],
        }
        out = redact_data(data)
        assert out["headers"]["X-Auth-Token"] == "***"
        assert out["headers"]["Accept"] == "json"
        assert out["items"][0]["secret"] == "***"
        assert out["items"][0]["name"] == "n"

    def test_long_string_truncated(self):
        out = redact_data({"content": "x" * 1000})
        assert len(out["content"]) < 1000
        assert "截断" in out["content"]

    def test_scalar_passthrough(self):
        assert redact_data(42) == 42
        assert redact_data(None) is None
        assert redact_data(True) is True


# ---------------------------------------------------------------------------
# 审计日志
# ---------------------------------------------------------------------------

class TestAuditLogger:
    def test_memory_log_and_query(self):
        audit = AuditLogger()
        audit.log("tool_call", tool_name="file_read", agent_id="a1")
        audit.log("llm_call", agent_id="a1", duration_ms=120.0)
        audit.log("tool_call", tool_name="shell_exec", status="error", error="boom")

        assert audit.count() == 3

        # 倒序返回
        recent = audit.get_recent(limit=10)
        assert recent[0].tool_name == "shell_exec"

        # 类型过滤
        tool_events = audit.get_recent(event_type="tool_call")
        assert len(tool_events) == 2

        # limit
        assert len(audit.get_recent(limit=1)) == 1

    def test_file_sink(self, tmp_path: Path):
        log_path = tmp_path / "audit.jsonl"
        audit = AuditLogger(path=log_path)
        audit.log_tool_call(
            "file_write", agent_id="a1", success=True, duration_ms=5.0,
            arguments={"path": "x.txt", "api_key": "sk-secret"},
        )
        audit.log_llm_call("gpt-4o", provider="openai", duration_ms=99.0,
                           tokens={"total_tokens": 42})

        lines = log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2

        ev1 = json.loads(lines[0])
        assert ev1["event_type"] == "tool_call"
        assert ev1["tool_name"] == "file_write"
        # 敏感参数已脱敏
        assert ev1["detail"]["arguments"]["api_key"] == "***"
        assert ev1["detail"]["arguments"]["path"] == "x.txt"

        ev2 = json.loads(lines[1])
        assert ev2["event_type"] == "llm_call"
        assert ev2["detail"]["tokens"]["total_tokens"] == 42

    def test_disabled(self):
        audit = AuditLogger(enabled=False)
        result = audit.log("tool_call", tool_name="x")
        assert result is None
        assert audit.count() == 0

    def test_clear(self):
        audit = AuditLogger()
        audit.log("tool_call")
        audit.clear()
        assert audit.count() == 0

    def test_singleton_env(self, tmp_path: Path, monkeypatch):
        reset_audit_logger()
        log_path = tmp_path / "env_audit.jsonl"
        monkeypatch.setenv("YOUMI_AUDIT_LOG", str(log_path))

        a1 = get_audit_logger()
        a2 = get_audit_logger()
        assert a1 is a2
        assert a1.path == str(log_path)

        a1.log("tool_call", tool_name="t")
        assert log_path.exists()

        reset_audit_logger()
        custom = AuditLogger()
        configure_audit_logger(custom)
        assert get_audit_logger() is custom
        reset_audit_logger()
        monkeypatch.delenv("YOUMI_AUDIT_LOG", raising=False)
        assert get_audit_logger().path == ""


# ---------------------------------------------------------------------------
# 追踪
# ---------------------------------------------------------------------------

def _read_spans(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class TestTracing:
    def setup_method(self):
        reset_tracing()

    def teardown_method(self):
        shutdown_tracing()
        reset_tracing()

    def test_span_exported_to_jsonl(self, tmp_path: Path):
        trace_file = tmp_path / "traces.jsonl"
        setup_tracing(trace_file=str(trace_file), batch=False)

        with span("tool.call", attributes={"tool.name": "file_read", "youmi.agent_id": "a1"}):
            pass

        spans = _read_spans(trace_file)
        assert len(spans) == 1
        sp = spans[0]
        assert sp["name"] == "tool.call"
        attrs = sp["attributes"]
        assert attrs["tool.name"] == "file_read"
        assert attrs["youmi.agent_id"] == "a1"

    def test_nested_spans_share_trace(self, tmp_path: Path):
        trace_file = tmp_path / "nested.jsonl"
        setup_tracing(trace_file=str(trace_file), batch=False)

        with span("agent.think") as outer:
            with span("llm.chat") as inner:
                pass

        spans = _read_spans(trace_file)
        assert len(spans) == 2
        by_name = {s["name"]: s for s in spans}
        outer_json = by_name["agent.think"]
        inner_json = by_name["llm.chat"]
        # 同一 trace，且子 span 的 parent 指向父 span
        assert outer_json["context"]["trace_id"] == inner_json["context"]["trace_id"]
        assert (
            inner_json.get("parent_id") == outer_json["context"]["span_id"]
        )

    def test_exception_recorded(self, tmp_path: Path):
        trace_file = tmp_path / "err.jsonl"
        setup_tracing(trace_file=str(trace_file), batch=False)

        with pytest.raises(ValueError):
            with span("tool.call") as sp:
                raise ValueError("boom")

        spans = _read_spans(trace_file)
        assert len(spans) == 1
        sp = spans[0]
        # 状态被标记为 ERROR
        assert sp["status"]["status_code"] == "ERROR"
        events = sp.get("events", [])
        assert any("boom" in json.dumps(e) for e in events)

    def test_attribute_truncation(self, tmp_path: Path):
        trace_file = tmp_path / "attr.jsonl"
        setup_tracing(trace_file=str(trace_file), batch=False)

        with span("t") as sp:
            set_span_attributes(sp, {"long": "y" * 1000, "none": None, "ok": 1})

        attrs = _read_spans(trace_file)[0]["attributes"]
        assert len(attrs["long"]) < 1000
        assert "none" not in attrs
        assert attrs["ok"] == 1

    def test_setup_idempotent(self, tmp_path: Path):
        trace_file = tmp_path / "idem.jsonl"
        p1 = setup_tracing(trace_file=str(trace_file), batch=False)
        p2 = setup_tracing(trace_file=str(tmp_path / "other.jsonl"), batch=False)
        assert p1 is p2

    def test_jsonl_exporter_direct(self, tmp_path: Path):
        """导出器独立可用（接口兼容 OTel SpanExporter）"""
        from opentelemetry.sdk.trace.export import SpanExportResult

        exporter = JsonlSpanExporter(tmp_path / "direct.jsonl")
        assert exporter.export([]) == SpanExportResult.SUCCESS
        exporter.shutdown()
