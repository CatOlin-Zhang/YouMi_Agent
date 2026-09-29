"""
网关 HTTP API (gateway.api)

FastAPI 应用工厂与路由（P1 部署：FastAPI 网关 + 多 Worker）。

端点：
- ``GET  /health``                  健康探活（免认证，供负载均衡 / 监控使用）
- ``POST /tasks``                   提交任务（submit_task）
- ``GET  /tasks``                   任务列表（本租户，status / limit 过滤）
- ``GET  /tasks/{task_id}``         任务状态查询（query_status）
- ``GET  /tasks/{task_id}/events``  SSE 事件流（stream_events）
- ``GET  /stats``                   网关统计（admin）

认证（沿用 M1 配置启用式，与 GUI 同策略）：
- ``Authorization: Bearer <token>`` 优先，兼容 ``?token=``
- 未配置 ``YOUMI_AUTH_*`` → 零摩擦放行（匿名 admin 主体）
- 校验失败 → 401 并写审计（auth / denied）
- 任务 tenant 一律取认证主体绑定租户（客户端不可指定，防越权）

本模块依赖 fastapi（可选依赖）::

    pip install -e .[gateway]
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from youmi.gateway.events import task_update_event
from youmi.gateway.models import TaskStatus
from youmi.gateway.service import GatewayService
from youmi.observability import get_audit_logger
from youmi.security import AuthManager, AuthRole, Principal, get_auth_manager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class SubmitTaskRequest(BaseModel):
    """POST /tasks 请求体"""

    task: str = Field(min_length=1, description="任务描述（用户文本）")
    metadata: dict[str, Any] = Field(default_factory=dict, description="调用方附加信息")


# ---------------------------------------------------------------------------
# 认证依赖（配置启用式，与 GUI 同策略）
# ---------------------------------------------------------------------------

def _extract_token(request: Request) -> str:
    """提取请求 token（``Authorization: Bearer`` 优先，兼容 ``?token=``）"""
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.query_params.get("token", "")


async def require_principal(request: Request) -> Principal:
    """认证依赖：返回认证主体；失败抛 401 并写审计"""
    auth = get_auth_manager()
    if not auth.enabled:
        principal = auth.validate("")
        assert principal is not None  # 未启用时恒为匿名 admin 主体
        return principal

    principal = auth.validate(_extract_token(request))
    if principal is None:
        remote = request.client.host if request.client else ""
        logger.warning("网关认证失败: path=%s remote=%s", request.url.path, remote)
        get_audit_logger().log(
            "auth",
            status="denied",
            detail={"remote": remote, "path": request.url.path},
            error="Invalid or missing token",
        )
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: invalid or missing token",
        )
    return principal


def _service(request: Request) -> GatewayService:
    """从应用状态取出网关服务"""
    return request.app.state.service


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """健康探活（免认证）"""
    stats = _service(request).stats()
    return {
        "status": "ok",
        "auth_enabled": get_auth_manager().enabled,
        "workers": stats["workers"],
        "workers_running": stats["workers_running"],
        "queue_size": stats["queue_size"],
        "tasks": stats["total"],
    }


@router.post("/tasks", status_code=201)
async def submit_task(
    request: Request,
    body: SubmitTaskRequest,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """提交任务（异步执行，立即返回 task_id；tenant 取自认证主体）"""
    service = _service(request)
    if not body.task.strip():
        raise HTTPException(status_code=422, detail="task 不能为空白")
    record = await service.submit(
        body.task.strip(), tenant=principal.tenant, metadata=body.metadata
    )
    logger.info("网关任务已提交: %s tenant=%s", record.task_id, principal.tenant)
    return record.to_dict()


@router.get("/tasks")
async def list_tasks(
    request: Request,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """列出本租户任务（新→旧）"""
    status_enum: TaskStatus | None = None
    if status:
        try:
            status_enum = TaskStatus(status)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"无效状态: {status}")
    records = _service(request).list_tasks(
        tenant=principal.tenant, status=status_enum, limit=limit
    )
    return {"tasks": [r.to_dict() for r in records], "count": len(records)}


@router.get("/tasks/{task_id}")
async def query_status(
    request: Request,
    task_id: str,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """查询任务状态（跨租户返回 404，避免存在性泄漏）"""
    record = _service(request).get(task_id)
    if record is None or record.tenant != principal.tenant:
        raise HTTPException(status_code=404, detail=f"task '{task_id}' not found")
    return record.to_dict()


def _sse_frame(event: dict[str, Any]) -> str:
    """SSE 帧（``event:`` 类型 + ``data:`` JSON）"""
    payload = json.dumps(event, ensure_ascii=False)
    return f"event: {event.get('type', 'message')}\ndata: {payload}\n\n"


@router.get("/tasks/{task_id}/events")
async def stream_events(
    request: Request,
    task_id: str,
    principal: Principal = Depends(require_principal),
) -> StreamingResponse:
    """订阅任务事件流（SSE）。

    任务已终结时回放最终快照后立即结束；否则实时推送状态变更
    （running → completed / failed）直至任务终结。
    """
    service = _service(request)
    record = service.get(task_id)
    if record is None or record.tenant != principal.tenant:
        raise HTTPException(status_code=404, detail=f"task '{task_id}' not found")

    async def _generate() -> AsyncIterator[str]:
        current = service.get(task_id)
        if current is not None and current.status.finished:
            yield _sse_frame(task_update_event(current))
            return
        stream = service.subscribe(task_id)
        try:
            async for event in stream:
                if await request.is_disconnected():
                    break
                yield _sse_frame(event)
        finally:
            await stream.aclose()

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/stats")
async def stats(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """网关统计（仅 admin）"""
    if not AuthManager.has_role(principal, AuthRole.ADMIN):
        raise HTTPException(status_code=403, detail="需要 admin 角色")
    return _service(request).stats()


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------

def create_app(service: GatewayService | None = None) -> FastAPI:
    """创建 FastAPI 应用。

    Args:
        service: 网关服务（缺省新建无执行器实例，仅支持提交 / 查询场景）

    Returns:
        配置完成的 FastAPI 应用；lifespan 自动 start/stop service
    """
    service = service or GatewayService()

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(title="YouMi Gateway", version="0.1.0", lifespan=_lifespan)
    app.state.service = service
    app.include_router(router)
    return app


__all__ = ["create_app", "require_principal", "SubmitTaskRequest"]
