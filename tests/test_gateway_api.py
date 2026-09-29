"""
tests/test_gateway_api.py — 网关 HTTP API（gw02）测试

覆盖：
- 端点：submit / query / list / stats / health / 校验错误
- SSE 事件流：已终结回放 + 运行中实时推送
- 认证：零摩擦 / Bearer / ?token= / 401 / 审计
- 多租户隔离：跨租户 404、列表过滤、任务 tenant 绑定认证主体
"""

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from youmi.gateway import GatewayService, TaskExecutor, TaskOutcome, TaskStatus
from youmi.gateway.api import create_app
from youmi.observability import get_audit_logger, reset_audit_logger
from youmi.security import (
    AuthManager,
    AuthRole,
    AuthToken,
    configure_auth_manager,
    reset_auth_manager,
)


# ----------------------------------------------------------------------
# 测试替身与工具
# ----------------------------------------------------------------------

class ApiFakeExecutor(TaskExecutor):
    """即时成功执行器（可选延迟）"""

    def __init__(self, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.executed: list[str] = []

    async def execute(self, task) -> TaskOutcome:
        self.executed.append(task.task)
        if self.delay:
            await asyncio.sleep(self.delay)
        return TaskOutcome(output=f"完成: {task.task}", iterations=1, tool_calls=["t1"])


class GateExecutor(TaskExecutor):
    """可外部控制完成时机的执行器（SSE 实时流测试用）"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, task) -> TaskOutcome:
        self.started.set()
        await self.release.wait()
        return TaskOutcome(output="完成", iterations=1, tool_calls=[])


@asynccontextmanager
async def gateway_client(executor: TaskExecutor | None = None, *, size: int = 1) -> Any:
    """构建 (service, httpx client) 测试上下文（手动管理生命周期）"""
    service = GatewayService(executor, size=size) if executor is not None else GatewayService()
    await service.start()
    transport = httpx.ASGITransport(app=create_app(service))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://gateway.test"
    ) as client:
        try:
            yield service, client
        finally:
            await service.stop()


@pytest.fixture(autouse=True)
def _clean_env():
    """认证 / 审计进程级单例隔离"""
    reset_auth_manager()
    reset_audit_logger()
    yield
    reset_auth_manager()
    reset_audit_logger()


def _sse_events(text: str) -> list[dict]:
    """解析 SSE 文本为事件 dict 列表"""
    events: list[dict] = []
    for block in text.strip().split("\n\n"):
        data_lines = [
            line[5:].strip() for line in block.splitlines() if line.startswith("data:")
        ]
        if data_lines:
            events.append(json.loads("\n".join(data_lines)))
    return events


def _configure_auth(*entries: tuple[str, AuthRole, str]) -> None:
    """配置认证：entries = (token, role, tenant)"""
    configure_auth_manager(AuthManager([
        AuthToken(token=t, role=r, name=f"user-{i}", tenant=tn)
        for i, (t, r, tn) in enumerate(entries)
    ]))


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ----------------------------------------------------------------------
# 基础端点
# ----------------------------------------------------------------------

class TestApiEndpoints:
    async def test_health(self):
        async with gateway_client(ApiFakeExecutor()) as (service, client):
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["auth_enabled"] is False
            assert data["workers"] == 1
            assert data["workers_running"] is True

    async def test_submit_and_query_e2e(self):
        async with gateway_client(ApiFakeExecutor()) as (service, client):
            resp = await client.post("/tasks", json={"task": "查天气"})
            assert resp.status_code == 201
            data = resp.json()
            assert data["status"] == "queued"
            assert data["tenant"] == "default"
            tid = data["task_id"]

            await asyncio.wait_for(service.queue.join(), 5.0)

            resp = await client.get(f"/tasks/{tid}")
            assert resp.status_code == 200
            done = resp.json()
            assert done["status"] == "completed"
            assert done["result"] == "完成: 查天气"
            assert done["iterations"] == 1
            assert done["tool_calls"] == ["t1"]
            assert done["duration_ms"] is not None

    async def test_submit_with_metadata_ignores_body_tenant(self):
        async with gateway_client() as (service, client):
            resp = await client.post("/tasks", json={
                "task": "x",
                "metadata": {"session_id": "s1"},
                "tenant": "hacked",  # 客户端指定应被忽略
            })
            assert resp.status_code == 201
            data = resp.json()
            assert data["metadata"] == {"session_id": "s1"}
            assert data["tenant"] == "default"

    async def test_submit_validation_errors(self):
        async with gateway_client() as (service, client):
            assert (await client.post("/tasks", json={})).status_code == 422
            assert (await client.post("/tasks", json={"task": ""})).status_code == 422
            assert (await client.post("/tasks", json={"task": "   "})).status_code == 422

    async def test_list_tasks_and_filters(self):
        async with gateway_client(ApiFakeExecutor()) as (service, client):
            await client.post("/tasks", json={"task": "a"})
            await client.post("/tasks", json={"task": "b"})
            await asyncio.wait_for(service.queue.join(), 5.0)

            resp = await client.get("/tasks")
            assert resp.json()["count"] == 2

            resp = await client.get("/tasks", params={"status": "completed"})
            assert resp.json()["count"] == 2

            resp = await client.get("/tasks", params={"status": "queued"})
            assert resp.json()["count"] == 0

            resp = await client.get("/tasks", params={"limit": 1})
            assert resp.json()["count"] == 1

            resp = await client.get("/tasks", params={"status": "bogus"})
            assert resp.status_code == 422

    async def test_unknown_task_404(self):
        async with gateway_client() as (service, client):
            assert (await client.get("/tasks/task_nope")).status_code == 404
            assert (await client.get("/tasks/task_nope/events")).status_code == 404

    async def test_stats(self):
        async with gateway_client(ApiFakeExecutor()) as (service, client):
            await client.post("/tasks", json={"task": "a"})
            await asyncio.wait_for(service.queue.join(), 5.0)
            resp = await client.get("/stats")
            assert resp.status_code == 200
            data = resp.json()
            assert data["workers"] == 1
            assert data["by_status"]["completed"] == 1
            assert data["total"] == 1


# ----------------------------------------------------------------------
# SSE 事件流
# ----------------------------------------------------------------------

class TestSseStream:
    async def test_finished_task_replays_snapshot(self):
        async with gateway_client(ApiFakeExecutor()) as (service, client):
            resp = await client.post("/tasks", json={"task": "x"})
            tid = resp.json()["task_id"]
            await asyncio.wait_for(service.queue.join(), 5.0)

            async with client.stream("GET", f"/tasks/{tid}/events") as stream_resp:
                assert stream_resp.status_code == 200
                assert stream_resp.headers["content-type"].startswith("text/event-stream")
                text = ""
                async for chunk in stream_resp.aiter_text():
                    text += chunk

            events = _sse_events(text)
            assert len(events) == 1
            assert events[0]["task_id"] == tid
            assert events[0]["status"] == "completed"
            assert events[0]["type"] == "task_update"

    async def test_live_stream_while_running(self):
        executor = GateExecutor()
        async with gateway_client(executor) as (service, client):
            resp = await client.post("/tasks", json={"task": "慢任务"})
            tid = resp.json()["task_id"]
            await asyncio.wait_for(executor.started.wait(), 5.0)
            assert service.get(tid).status == TaskStatus.RUNNING

            frames: list[dict] = []

            async def _consume() -> None:
                async with client.stream("GET", f"/tasks/{tid}/events") as r:
                    assert r.status_code == 200
                    text = ""
                    async for chunk in r.aiter_text():
                        text += chunk
                frames.extend(_sse_events(text))

            task = asyncio.create_task(_consume())
            await asyncio.sleep(0.1)  # 让订阅建立（任务此时仍在运行）
            assert service.get(tid).status == TaskStatus.RUNNING
            executor.release.set()  # 放行任务 → completed → 流结束
            await asyncio.wait_for(task, 5.0)

            assert frames, "应收到事件帧"
            assert all(f["task_id"] == tid for f in frames)
            assert frames[-1]["status"] == "completed"


# ----------------------------------------------------------------------
# 认证
# ----------------------------------------------------------------------

class TestAuth:
    async def test_missing_or_wrong_token_401(self):
        _configure_auth(("tok-a", AuthRole.AGENT, "tenant-a"))
        async with gateway_client(ApiFakeExecutor()) as (service, client):
            resp = await client.post("/tasks", json={"task": "x"})
            assert resp.status_code == 401
            resp = await client.post(
                "/tasks", json={"task": "x"}, headers=_bearer("wrong")
            )
            assert resp.status_code == 401

    async def test_bearer_token_ok_and_tenant_bound(self):
        _configure_auth(("tok-a", AuthRole.AGENT, "tenant-a"))
        async with gateway_client(ApiFakeExecutor()) as (service, client):
            resp = await client.post(
                "/tasks", json={"task": "x"}, headers=_bearer("tok-a")
            )
            assert resp.status_code == 201
            assert resp.json()["tenant"] == "tenant-a"

    async def test_query_token_compat(self):
        _configure_auth(("tok-a", AuthRole.AGENT, "tenant-a"))
        async with gateway_client(ApiFakeExecutor()) as (service, client):
            resp = await client.post("/tasks?token=tok-a", json={"task": "x"})
            assert resp.status_code == 201

    async def test_health_exempt_from_auth(self):
        _configure_auth(("tok-a", AuthRole.AGENT, "t1"))
        async with gateway_client(ApiFakeExecutor()) as (service, client):
            resp = await client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["auth_enabled"] is True

    async def test_stats_requires_admin(self):
        _configure_auth(
            ("tok-admin", AuthRole.ADMIN, "t1"),
            ("tok-viewer", AuthRole.VIEWER, "t1"),
        )
        async with gateway_client(ApiFakeExecutor()) as (service, client):
            resp = await client.get("/stats", headers=_bearer("tok-viewer"))
            assert resp.status_code == 403
            resp = await client.get("/stats", headers=_bearer("tok-admin"))
            assert resp.status_code == 200

    async def test_denied_writes_audit(self):
        _configure_auth(("tok-a", AuthRole.AGENT, "t1"))
        async with gateway_client() as (service, client):
            await client.post("/tasks", json={"task": "x"})
        events = get_audit_logger().get_recent(event_type="auth")
        assert any(e.status == "denied" for e in events)


# ----------------------------------------------------------------------
# lifespan（应用生命周期自动 start/stop）
# ----------------------------------------------------------------------

class TestLifespan:
    def test_lifespan_starts_and_stops_service(self):
        """TestClient 触发 lifespan：进入时启动 worker 池，退出时停止"""
        from starlette.testclient import TestClient

        service = GatewayService(ApiFakeExecutor(), size=1)
        app = create_app(service)
        with TestClient(app) as client:
            assert service.stats()["workers_running"] is True
            resp = client.get("/health")
            assert resp.status_code == 200
        assert service.stats()["workers_running"] is False


# ----------------------------------------------------------------------
# 多租户隔离
# ----------------------------------------------------------------------

class TestTenantIsolation:
    async def test_cross_tenant_404_and_list_isolation(self):
        _configure_auth(
            ("tok-a", AuthRole.AGENT, "tenant-a"),
            ("tok-b", AuthRole.AGENT, "tenant-b"),
        )
        async with gateway_client(ApiFakeExecutor()) as (service, client):
            h_a, h_b = _bearer("tok-a"), _bearer("tok-b")

            resp = await client.post("/tasks", json={"task": "a任务"}, headers=h_a)
            tid = resp.json()["task_id"]
            await asyncio.wait_for(service.queue.join(), 5.0)

            # 跨租户查询 / SSE 均 404（不泄漏存在性）
            assert (await client.get(f"/tasks/{tid}", headers=h_b)).status_code == 404
            assert (
                await client.get(f"/tasks/{tid}/events", headers=h_b)
            ).status_code == 404

            # 本租户可查
            assert (await client.get(f"/tasks/{tid}", headers=h_a)).status_code == 200

            # 列表按租户隔离
            assert (await client.get("/tasks", headers=h_a)).json()["count"] == 1
            assert (await client.get("/tasks", headers=h_b)).json()["count"] == 0

    async def test_task_list_filtered_by_own_tenant_only(self):
        _configure_auth(
            ("tok-a", AuthRole.AGENT, "tenant-a"),
            ("tok-b", AuthRole.AGENT, "tenant-b"),
        )
        async with gateway_client(ApiFakeExecutor()) as (service, client):
            await client.post("/tasks", json={"task": "a1"}, headers=_bearer("tok-a"))
            await client.post("/tasks", json={"task": "b1"}, headers=_bearer("tok-b"))
            await asyncio.wait_for(service.queue.join(), 5.0)

            tasks_a = (await client.get("/tasks", headers=_bearer("tok-a"))).json()["tasks"]
            assert [t["task"] for t in tasks_a] == ["a1"]
            assert all(t["tenant"] == "tenant-a" for t in tasks_a)
