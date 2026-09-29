"""可观测性模块 (M1: P0) — 审计日志 + OpenTelemetry 追踪"""

from youmi.observability.audit import (
    AuditEvent,
    AuditLogger,
    configure_audit_logger,
    get_audit_logger,
    redact_data,
    reset_audit_logger,
)
from youmi.observability.tracing import (
    JsonlSpanExporter,
    ensure_tracing,
    get_tracer,
    record_span_error,
    reset_tracing,
    set_span_attributes,
    setup_tracing,
    shutdown_tracing,
    span,
)

__all__ = [
    # 审计
    "AuditEvent",
    "AuditLogger",
    "configure_audit_logger",
    "get_audit_logger",
    "redact_data",
    "reset_audit_logger",
    # 追踪
    "JsonlSpanExporter",
    "ensure_tracing",
    "get_tracer",
    "record_span_error",
    "reset_tracing",
    "set_span_attributes",
    "setup_tracing",
    "shutdown_tracing",
    "span",
]
