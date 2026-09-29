"""
审计日志模块 (M1: P0 可观测性)

提供结构化审计能力，回答「谁、何时、调了什么、耗时多少、成功与否」:
- ``AuditEvent``  — 结构化审计事件模型（可序列化）
- ``AuditLogger`` — 审计记录器（内存环形缓冲 + 可选 JSONL 落盘）
- ``redact_data`` — 敏感字段脱敏工具（api_key / token / password 等）

设计原则:
- 内存缓冲始终可用（供 GUI / 诊断查询），文件落盘为可选（``YOUMI_AUDIT_LOG``）
- 落库前统一脱敏，避免凭据进入审计文件
- 写入失败不影响主流程（审计降级，仅告警日志）
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# 敏感字段名模式 — 匹配的 key 其值将被脱敏
_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|credential|authorization|auth)",
    re.IGNORECASE,
)

_REDACTED = "***"

# 单个字符串值的最大保留长度（防止超长内容污染审计）
_MAX_STR_LEN = 500


def redact_data(data: Any, max_str_len: int = _MAX_STR_LEN) -> Any:
    """递归脱敏 + 截断

    - dict: 敏感 key 的字符串值 → ``"***"``；非字符串值（如 token 用量数字）
      递归保留；其余 key 递归处理
    - list/tuple: 逐项递归
    - str: 超过 max_str_len 截断
    - 其他类型原样返回
    """
    if isinstance(data, dict):
        out: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(key, str) and _SENSITIVE_KEY_RE.search(key):
                # 字符串才可能是凭据；数字/字典（如 {"total_tokens": 42}）保留
                if isinstance(value, str):
                    out[key] = _REDACTED
                    continue
            out[key] = redact_data(value, max_str_len)
        return out

    if isinstance(data, (list, tuple)):
        return [redact_data(item, max_str_len) for item in data]

    if isinstance(data, str) and len(data) > max_str_len:
        return data[:max_str_len] + f"...(截断,共{len(data)}字符)"

    return data


class AuditEvent(BaseModel):
    """结构化审计事件

    Args:
        event_type: 事件类型 (llm_call / tool_call / approval / auth / sandbox / ...)
        agent_id: 相关 Agent ID
        tenant: 租户标识 (多租户隔离, 默认 "default")
        task_id: 相关任务 ID
        tool_name: 相关工具名（事件类型相关）
        status: ok / error / denied / blocked
        duration_ms: 耗时（毫秒）
        detail: 脱敏后的结构化细节
        error: 错误摘要
    """

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )
    event_type: str
    agent_id: str = ""
    tenant: str = "default"
    task_id: str = ""
    tool_name: str = ""
    status: str = "ok"
    duration_ms: float | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    error: str = ""


class AuditLogger:
    """审计记录器

    Args:
        path: JSONL 落盘路径（空 = 仅内存缓冲）
        max_memory: 内存环形缓冲容量
        enabled: 总开关（False 时 log() 为 no-op）
    """

    def __init__(
        self,
        path: str | Path = "",
        max_memory: int = 1000,
        enabled: bool = True,
    ) -> None:
        self._path = str(path) if path else ""
        self._max_memory = max(1, max_memory)
        self._enabled = enabled
        self._events: deque[AuditEvent] = deque(maxlen=self._max_memory)
        self._lock = threading.Lock()

        if self._path:
            try:
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                logger.warning("审计日志目录创建失败 (%s): %s", self._path, exc)

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def path(self) -> str:
        return self._path

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------

    def log(
        self,
        event_type: str,
        *,
        agent_id: str = "",
        tenant: str = "default",
        task_id: str = "",
        tool_name: str = "",
        status: str = "ok",
        duration_ms: float | None = None,
        detail: dict[str, Any] | None = None,
        error: str = "",
        **extra: Any,
    ) -> AuditEvent | None:
        """记录一条审计事件（detail 自动脱敏）

        Returns:
            生成的 AuditEvent；禁用时返回 None
        """
        if not self._enabled:
            return None

        merged: dict[str, Any] = dict(detail or {})
        if extra:
            merged.update(extra)

        event = AuditEvent(
            event_type=event_type,
            agent_id=agent_id,
            tenant=tenant or "default",
            task_id=task_id,
            tool_name=tool_name,
            status=status,
            duration_ms=duration_ms,
            detail=redact_data(merged) if merged else {},
            error=str(error)[:500],
        )

        with self._lock:
            self._events.append(event)
            if self._path:
                self._write_line(event)

        return event

    def log_tool_call(
        self,
        tool_name: str,
        *,
        agent_id: str = "",
        tenant: str = "default",
        task_id: str = "",
        success: bool = True,
        duration_ms: float | None = None,
        arguments: dict[str, Any] | None = None,
        error: str = "",
    ) -> AuditEvent | None:
        """工具调用审计快捷方法"""
        return self.log(
            "tool_call",
            agent_id=agent_id,
            tenant=tenant,
            task_id=task_id,
            tool_name=tool_name,
            status="ok" if success else "error",
            duration_ms=duration_ms,
            detail={"arguments": arguments or {}},
            error=error,
        )

    def log_llm_call(
        self,
        model: str,
        *,
        agent_id: str = "",
        tenant: str = "default",
        provider: str = "",
        success: bool = True,
        duration_ms: float | None = None,
        tokens: dict[str, Any] | None = None,
        attempts: int = 1,
        error: str = "",
    ) -> AuditEvent | None:
        """LLM 调用审计快捷方法

        Args:
            attempts: 总尝试次数（含重试）
        """
        return self.log(
            "llm_call",
            agent_id=agent_id,
            tenant=tenant,
            status="ok" if success else "error",
            duration_ms=duration_ms,
            detail={
                "model": model,
                "provider": provider,
                "tokens": tokens or {},
                "attempts": attempts,
            },
            error=error,
        )

    # ------------------------------------------------------------------
    # 查询 / 维护
    # ------------------------------------------------------------------

    def get_recent(
        self,
        limit: int = 100,
        event_type: str = "",
        tenant: str = "",
    ) -> list[AuditEvent]:
        """获取最近的事件（按时间倒序）

        Args:
            limit: 最大返回条数
            event_type: 仅返回指定类型（空 = 全部）
            tenant: 仅返回指定租户（空 = 不过滤）
        """
        with self._lock:
            events: Iterable[AuditEvent] = list(self._events)

        if event_type:
            events = [e for e in events if e.event_type == event_type]
        if tenant:
            events = [e for e in events if e.tenant == tenant]

        return list(events)[-limit:][::-1]

    def count(self) -> int:
        """当前内存缓冲中的事件数"""
        with self._lock:
            return len(self._events)

    def clear(self) -> None:
        """清空内存缓冲（不影响已落盘文件）"""
        with self._lock:
            self._events.clear()

    def _write_line(self, event: AuditEvent) -> None:
        """追加写入 JSONL（失败仅告警，不影响主流程）"""
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event.model_dump(), ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.warning("审计日志写入失败 (%s): %s", self._path, exc)

    def __repr__(self) -> str:
        return (
            f"<AuditLogger events={self.count()} "
            f"path={self._path or '(memory)'} enabled={self._enabled}>"
        )


# ---------------------------------------------------------------------------
# 进程级默认实例（懒初始化，环境变量驱动）
# ---------------------------------------------------------------------------

_default: AuditLogger | None = None


def get_audit_logger() -> AuditLogger:
    """获取进程级默认审计记录器

    首次调用时读取环境变量 ``YOUMI_AUDIT_LOG``（JSONL 路径，为空则仅内存缓冲）。
    """
    global _default
    if _default is None:
        path = os.environ.get("YOUMI_AUDIT_LOG", "").strip()
        _default = AuditLogger(path=path)
    return _default


def configure_audit_logger(audit: AuditLogger) -> None:
    """替换进程级默认审计记录器（启动配置用）"""
    global _default
    _default = audit


def reset_audit_logger() -> None:
    """清空进程级默认实例（测试隔离用）"""
    global _default
    _default = None
