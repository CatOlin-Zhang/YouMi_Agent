"""
OpenTelemetry 追踪接入 (M1: P0 可观测性)

对 LLM 调用、工具调用、总线消息统一埋点（trace + span），满足「谁、何时、
调了什么工具、卡在哪一步」的可观测要求。

导出策略（按优先级）:
1. ``endpoint`` 非空 → OTLP 导出（gRPC，缺省回退 HTTP，均不可用则降级）
2. ``trace_file`` 非空 → 内置 ``JsonlSpanExporter`` 落盘（无 collector 也可查）
3. 都为空 → 仅创建 span（无导出，测试/轻量场景无副作用）

环境变量（``ensure_tracing()`` 懒初始化时读取）:
- ``YOUMI_OTEL_SERVICE``   服务名（默认 youmi-agent）
- ``YOUMI_OTEL_ENDPOINT``  OTLP endpoint（如 http://localhost:4317）
- ``YOUMI_TRACE_FILE``     JSONL span 落盘路径

用法::

    from youmi.observability import span, set_span_attributes

    with span("tool.call", attributes={"tool.name": "file_read"}) as sp:
        result = await do_call()
        set_span_attributes(sp, {"youmi.success": True})
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Span, Status, StatusCode, Tracer

logger = logging.getLogger(__name__)

DEFAULT_SERVICE_NAME = "youmi-agent"


# ---------------------------------------------------------------------------
# JSONL 导出器（无 OTel collector 时的可查导出）
# ---------------------------------------------------------------------------

class JsonlSpanExporter(SpanExporter):
    """将 span 逐行以 JSON 落盘（每行一个 span）

    适用于本地开发/单机部署: 无需 OTel collector，直接查看 span 文件即可
    还原完整的调用链路（trace_id / span_id / parent_id / 属性 / 耗时）。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        try:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            logger.warning("追踪文件目录创建失败 (%s): %s", self._path, exc)

    @property
    def path(self) -> str:
        return self._path

    def export(self, spans) -> SpanExportResult:  # type: ignore[override]
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                for sp in spans:
                    # indent=None → 单行紧凑 JSON（JSONL 格式要求）
                    f.write(sp.to_json(indent=None) + "\n")
            return SpanExportResult.SUCCESS
        except Exception as exc:
            logger.warning("追踪导出失败 (%s): %s", self._path, exc)
            return SpanExportResult.FAILURE

    def shutdown(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Provider 管理
# ---------------------------------------------------------------------------

_provider: TracerProvider | None = None


def _build_exporter(
    endpoint: str, trace_file: str,
) -> SpanExporter | None:
    """按配置构建 span 导出器（OTLP 优先，失败降级 JSONL）"""
    if endpoint:
        exporter = _try_otlp_exporter(endpoint)
        if exporter is not None:
            return exporter
        logger.warning(
            "OTLP 导出器不可用（未安装 opentelemetry-exporter-otlp-*），"
            "降级为 JSONL/仅内存 span",
        )

    if trace_file:
        return JsonlSpanExporter(trace_file)

    return None


def _try_otlp_exporter(endpoint: str) -> SpanExporter | None:
    """尝试创建 OTLP 导出器（gRPC → HTTP 依次尝试）"""
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as GrpcExporter,
        )
        return GrpcExporter(endpoint=endpoint)
    except Exception:
        pass

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as HttpExporter,
        )
        return HttpExporter(endpoint=endpoint)
    except Exception:
        return None


def setup_tracing(
    service_name: str = DEFAULT_SERVICE_NAME,
    *,
    endpoint: str = "",
    trace_file: str = "",
    batch: bool = True,
) -> TracerProvider:
    """初始化全局 TracerProvider（幂等 — 已初始化时直接返回）

    Args:
        service_name: 服务名（OTel Resource service.name）
        endpoint: OTLP endpoint（空 = 不使用 OTLP）
        trace_file: JSONL span 落盘路径（OTLP 不可用时的降级导出）
        batch: True 使用 BatchSpanProcessor（生产），False 使用
            SimpleSpanProcessor（即时导出，测试/调试用）
    """
    global _provider
    if _provider is not None:
        return _provider

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    exporter = _build_exporter(endpoint, trace_file)
    if exporter is not None:
        processor = (
            BatchSpanProcessor(exporter) if batch else SimpleSpanProcessor(exporter)
        )
        provider.add_span_processor(processor)

    # 尽量安装为全局 provider（第三方库受益）；已被占用时保持独立 provider
    try:
        current = trace.get_tracer_provider()
        if isinstance(current, trace.ProxyTracerProvider):
            trace.set_tracer_provider(provider)
    except Exception:
        pass

    _provider = provider
    logger.info(
        "Tracing 已初始化: service=%s endpoint=%s trace_file=%s exporter=%s",
        service_name, endpoint or "-", trace_file or "-",
        type(exporter).__name__ if exporter else "none",
    )
    return provider


def ensure_tracing() -> TracerProvider:
    """按环境变量懒初始化追踪（首次调用时生效）"""
    if _provider is not None:
        return _provider
    return setup_tracing(
        service_name=os.environ.get("YOUMI_OTEL_SERVICE", DEFAULT_SERVICE_NAME),
        endpoint=os.environ.get("YOUMI_OTEL_ENDPOINT", "").strip(),
        trace_file=os.environ.get("YOUMI_TRACE_FILE", "").strip(),
    )


def get_provider() -> TracerProvider:
    """获取当前 TracerProvider（懒初始化）"""
    return ensure_tracing()


def get_tracer(name: str = "youmi") -> Tracer:
    """获取命名 Tracer（懒初始化）"""
    return ensure_tracing().get_tracer(name)


def shutdown_tracing() -> None:
    """刷新并关闭 TracerProvider（进程退出前调用，确保 span 落盘）"""
    global _provider
    if _provider is not None:
        try:
            _provider.shutdown()
        except Exception as exc:
            logger.debug("Tracing shutdown 异常: %s", exc)
        _provider = None


def reset_tracing() -> None:
    """重置追踪状态（测试隔离用，不做全局 provider 恢复）"""
    global _provider
    _provider = None


# ---------------------------------------------------------------------------
# 埋点辅助
# ---------------------------------------------------------------------------

def set_span_attributes(span: Span, attributes: dict[str, Any]) -> None:
    """批量设置 span 属性（None 跳过、超长字符串截断、异常忽略）"""
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, str) and len(value) > 500:
            value = value[:500] + "…"
        try:
            span.set_attribute(key, value)
        except Exception:
            # 不支持的属性类型 — 忽略，不影响主流程
            pass


def record_span_error(span: Span, exc: BaseException) -> None:
    """在 span 上记录异常并标记 ERROR 状态"""
    try:
        span.record_exception(exc)
        span.set_status(Status(StatusCode.ERROR, str(exc)[:300]))
    except Exception:
        pass


@contextmanager
def span(
    name: str,
    *,
    tracer_name: str = "youmi",
    attributes: dict[str, Any] | None = None,
) -> Iterator[Span]:
    """创建当前上下文的 span（异常自动标记 ERROR）

    用法（同步 / 异步均可）::

        with span("llm.chat", attributes={"llm.model": model}) as sp:
            ...
    """
    tracer = get_tracer(tracer_name)
    with tracer.start_as_current_span(name) as sp:
        if attributes:
            set_span_attributes(sp, attributes)
        try:
            yield sp
        except Exception as exc:
            record_span_error(sp, exc)
            raise
