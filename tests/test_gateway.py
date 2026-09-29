"""
tests/test_gateway.py — 网关基础（gw01）测试

覆盖：
- models：TaskStatus / TaskRecord / new_task_id
- queue：InMemoryTaskQueue FIFO 与 task_done/join
- registry：过滤 / 顺序 / 统计 / 容量淘汰
- events：TaskEventHub 发布订阅 / 关闭 / 空流 / 多订阅者
- worker：WorkerPool 状态流转 / 失败 / 容错 / 排水停止
- executor：MasterTaskExecutor 映射 / 串行 / reset / 多租户 factory
- service：GatewayService 端到端提交 → 执行 → 查询 / 事件流
"""

import asyncio

import pytest

from youmi.gateway import (
    GatewayService,
    InMemoryTaskQueue,
    MasterTaskExecutor,
    TaskEventHub,
    TaskExecutor,
    TaskOutcome,
    TaskRecord,
    TaskRegistry,
    TaskStatus,
    WorkerPool,
    new_task_id,
    task_update_event,
)


# ----------------------------------------------------------------------
# 测试替身
# ----------------------------------------------------------------------

class FakeExecutor(TaskExecutor):
    """记录调用并按任务名模拟成功 / 失败 / 抛异常"""

    def __init__(
        self,
        *,
        delay: float = 0.0,
        fail_tasks: tuple[str, ...] = (),
        raise_tasks: tuple[str, ...] = (),
    ) -> None:
        self.executed: list[str] = []
        self.delay = delay
        self.fail_tasks = set(fail_tasks)
        self.raise_tasks = set(raise_tasks)

    async def execute(self, task) -> TaskOutcome:
        self.executed.append(task.task)
        if self.delay:
            await asyncio.sleep(self.delay)
        if task.task in self.raise_tasks:
            raise RuntimeError(f"boom: {task.task}")
        if task.task in self.fail_tasks:
            return TaskOutcome(error=f"失败: {task.task}")
        return TaskOutcome(
            output=f"完成: {task.task}",
            iterations=2,
            tool_calls=["get_weather"],
        )


class FakeMaster:
    """模拟 MasterAgent：chat_turn / reset_for_new_task / destroy"""

    def __init__(
        self,
        *,
        error: str = "",
        raise_exc: Exception | None = None,
    ) -> None:
        self.turns: list[str] = []
        self.resets = 0
        self.destroyed = 0
        self._error = error
        self._raise = raise_exc

    async def chat_turn(self, text: str) -> dict:
        self.turns.append(text)
        if self._raise is not None:
            raise self._raise
        return {
            "response": f"应答: {text}",
            "iterations": 3,
            "tool_calls": ["t1", "t2"],
            "error": self._error,
        }

    async def reset_for_new_task(self) -> None:
        self.resets += 1

    async def destroy(self) -> None:
        self.destroyed += 1


class ConcurrencyProbe:
    """记录 chat_turn 最大并发数（验证执行器串行语义）"""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def chat_turn(self, text: str) -> dict:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return {"response": text}


# ----------------------------------------------------------------------
# models
# ----------------------------------------------------------------------

class TestModels:
    def test_status_finished(self):
        assert TaskStatus.COMPLETED.finished
        assert TaskStatus.FAILED.finished
        assert TaskStatus.CANCELLED.finished
        assert not TaskStatus.QUEUED.finished
        assert not TaskStatus.RUNNING.finished

    def test_new_task_id_unique(self):
        ids = {new_task_id() for _ in range(50)}
        assert len(ids) == 50
        assert all(i.startswith("task_") for i in ids)

    def test_record_defaults(self):
        r = TaskRecord(task="hello")
        assert r.tenant == "default"
        assert r.status == TaskStatus.QUEUED
        assert r.created_at > 0
        assert r.started_at is None
        assert r.duration_ms is None
        assert r.metadata == {}

    def test_duration_and_to_dict(self):
        r = TaskRecord(task="x")
        r.started_at = 100.0
        r.finished_at = 100.25
        assert r.duration_ms == 250.0
        d = r.to_dict()
        assert d["status"] == "queued"
        assert d["duration_ms"] == 250.0
        assert d["task_id"] == r.task_id

    def test_task_update_event_shape(self):
        r = TaskRecord(task="x", status=TaskStatus.RUNNING)
        ev = task_update_event(r)
        assert ev["type"] == "task_update"
        assert ev["status"] == "running"
        assert ev["task_id"] == r.task_id
        assert "ts" in ev


# ----------------------------------------------------------------------
# queue
# ----------------------------------------------------------------------

class TestTaskQueue:
    async def test_fifo(self):
        q = InMemoryTaskQueue()
        await q.put(TaskRecord(task="a"))
        await q.put(TaskRecord(task="b"))
        assert q.qsize() == 2
        assert (await q.get()).task == "a"
        assert (await q.get()).task == "b"
        q.task_done()
        q.task_done()
        await asyncio.wait_for(q.join(), 1.0)
        assert q.qsize() == 0

    async def test_join_waits_for_task_done(self):
        q = InMemoryTaskQueue()
        await q.put(TaskRecord(task="a"))
        await q.get()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.join(), 0.05)
        q.task_done()
        await asyncio.wait_for(q.join(), 1.0)


# ----------------------------------------------------------------------
# registry
# ----------------------------------------------------------------------

class TestTaskRegistry:
    def test_put_get(self):
        reg = TaskRegistry()
        r = TaskRecord(task="a")
        reg.put(r)
        assert reg.get(r.task_id) is r
        assert reg.get("missing") is None
        assert len(reg) == 1

    def test_list_order_and_filters(self):
        reg = TaskRegistry()
        r1 = TaskRecord(task="a", tenant="t1")
        r2 = TaskRecord(task="b", tenant="t2")
        r3 = TaskRecord(task="c", tenant="t1")
        for r in (r1, r2, r3):
            reg.put(r)
        assert [r.task for r in reg.list()] == ["c", "b", "a"]  # 新→旧
        assert [r.task for r in reg.list(tenant="t1")] == ["c", "a"]
        r3.status = TaskStatus.COMPLETED
        reg.put(r3)
        assert [r.task for r in reg.list(status=TaskStatus.COMPLETED)] == ["c"]
        assert [r.task for r in reg.list(limit=1)] == ["c"]
        assert [r.task for r in reg.list(limit=0)] == ["c", "b", "a"]

    def test_stats(self):
        reg = TaskRegistry()
        r1 = TaskRecord(task="a", status=TaskStatus.COMPLETED)
        r2 = TaskRecord(task="b", status=TaskStatus.RUNNING)
        reg.put(r1)
        reg.put(r2)
        stats = reg.stats()
        assert stats["total"] == 2
        assert stats["by_status"]["completed"] == 1
        assert stats["by_status"]["running"] == 1
        assert stats["by_status"]["queued"] == 0

    def test_eviction_prefers_finished(self):
        reg = TaskRegistry(max_records=2)
        done1 = TaskRecord(task="d1", status=TaskStatus.COMPLETED)
        done2 = TaskRecord(task="d2", status=TaskStatus.COMPLETED)
        running = TaskRecord(task="r", status=TaskStatus.RUNNING)
        reg.put(done1)
        reg.put(done2)
        reg.put(running)  # 超限 → 淘汰最老已完结 done1
        assert len(reg) == 2
        assert reg.get(done1.task_id) is None
        assert reg.get(done2.task_id) is not None
        assert reg.get(running.task_id) is not None

    def test_eviction_keeps_running_when_no_finished(self):
        reg = TaskRegistry(max_records=2)
        for name in ("a", "b", "c"):
            reg.put(TaskRecord(task=name, status=TaskStatus.RUNNING))
        assert len(reg) == 3  # 全为运行中：暂不淘汰


# ----------------------------------------------------------------------
# events
# ----------------------------------------------------------------------

class TestTaskEventHub:
    async def test_publish_subscribe_and_close(self):
        hub = TaskEventHub()
        stream = hub.subscribe("t1")
        hub.publish("t1", {"type": "a"})
        hub.publish("t1", {"type": "b"})
        hub.close("t1")
        events = [ev async for ev in stream]
        assert [e["type"] for e in events] == ["a", "b"]
        assert hub.closed_count == 1
        assert hub.subscriber_count("t1") == 0

    async def test_subscribe_after_close_is_empty(self):
        hub = TaskEventHub()
        hub.close("t1")
        events = [ev async for ev in hub.subscribe("t1")]
        assert events == []

    async def test_multiple_subscribers(self):
        hub = TaskEventHub()
        s1 = hub.subscribe("t1")
        s2 = hub.subscribe("t1")
        assert hub.subscriber_count("t1") == 2
        hub.publish("t1", {"n": 1})
        hub.close("t1")
        e1 = [ev async for ev in s1]
        e2 = [ev async for ev in s2]
        assert e1 == e2 == [{"n": 1}]

    async def test_subscriber_cleanup_on_early_exit(self):
        hub = TaskEventHub()
        stream = hub.subscribe("t1")
        hub.publish("t1", {"n": 1})
        got = []
        async for ev in stream:
            got.append(ev)
            break
        # async generator 提前退出需显式 aclose 才会触发 finally 清理
        await stream.aclose()
        assert got == [{"n": 1}]
        assert hub.subscriber_count("t1") == 0


# ----------------------------------------------------------------------
# worker
# ----------------------------------------------------------------------

class TestWorkerPool:
    async def test_task_status_flow(self):
        executor = FakeExecutor()
        pool = WorkerPool(executor, size=1)
        await pool.start()
        try:
            record = TaskRecord(task="查天气")
            await pool.queue.put(record)
            await asyncio.wait_for(pool.queue.join(), 5.0)
        finally:
            await pool.stop()

        assert record.status == TaskStatus.COMPLETED
        assert record.result == "完成: 查天气"
        assert record.iterations == 2
        assert record.tool_calls == ["get_weather"]
        assert record.worker_id == "worker-0"
        assert record.started_at is not None
        assert record.finished_at is not None
        assert record.duration_ms is not None
        # registry 中可见
        assert pool.registry.get(record.task_id).status == TaskStatus.COMPLETED

    async def test_failed_task(self):
        executor = FakeExecutor(fail_tasks=("bad",))
        pool = WorkerPool(executor, size=1)
        await pool.start()
        try:
            record = TaskRecord(task="bad")
            await pool.queue.put(record)
            await asyncio.wait_for(pool.queue.join(), 5.0)
        finally:
            await pool.stop()
        assert record.status == TaskStatus.FAILED
        assert record.error == "失败: bad"

    async def test_worker_survives_executor_exception(self):
        executor = FakeExecutor(raise_tasks=("boom",))
        pool = WorkerPool(executor, size=1)
        await pool.start()
        try:
            bad = TaskRecord(task="boom")
            good = TaskRecord(task="ok")
            await pool.queue.put(bad)
            await pool.queue.put(good)
            await asyncio.wait_for(pool.queue.join(), 5.0)
        finally:
            await pool.stop()
        assert bad.status == TaskStatus.FAILED
        assert "boom" in bad.error
        assert good.status == TaskStatus.COMPLETED

    async def test_events_published_in_order(self):
        executor = FakeExecutor()
        pool = WorkerPool(executor, size=1)
        record = TaskRecord(task="x", task_id="t-stream")
        stream = pool.events.subscribe("t-stream")
        await pool.start()
        try:
            await pool.queue.put(record)
            await asyncio.wait_for(pool.queue.join(), 5.0)
            events = [ev async for ev in stream]
        finally:
            await pool.stop()
        assert [e["status"] for e in events] == ["running", "completed"]

    async def test_start_idempotent_and_worker_ids(self):
        pool = WorkerPool(FakeExecutor(), size=3, worker_prefix="w")
        await pool.start()
        ids = pool.worker_ids
        await pool.start()  # 幂等
        assert pool.worker_ids == ids == ["w-0", "w-1", "w-2"]
        assert pool.running
        await pool.stop(drain=False)
        assert not pool.running
        assert pool.worker_ids == []

    async def test_stop_drain_waits_for_pending(self):
        executor = FakeExecutor(delay=0.05)
        pool = WorkerPool(executor, size=1)
        await pool.start()
        await pool.queue.put(TaskRecord(task="a"))
        await pool.queue.put(TaskRecord(task="b"))
        await pool.stop(drain=True, timeout=5.0)
        assert not pool.running
        assert executor.executed == ["a", "b"]  # 排水：两个任务都执行完

    async def test_stop_no_drain_cancels(self):
        executor = FakeExecutor(delay=5.0)
        pool = WorkerPool(executor, size=1)
        await pool.start()
        record = TaskRecord(task="slow")
        await pool.queue.put(record)
        await asyncio.sleep(0.05)  # 让 worker 进入执行
        await pool.stop(drain=False)
        assert not pool.running
        assert record.status == TaskStatus.RUNNING  # 被取消，停留执行中

    def test_invalid_size(self):
        with pytest.raises(ValueError):
            WorkerPool(FakeExecutor(), size=0)


# ----------------------------------------------------------------------
# executor
# ----------------------------------------------------------------------

class TestMasterTaskExecutor:
    async def test_maps_chat_turn_result(self):
        master = FakeMaster()
        executor = MasterTaskExecutor(master)
        outcome = await executor.execute(TaskRecord(task="你好"))
        assert outcome.success
        assert outcome.output == "应答: 你好"
        assert outcome.iterations == 3
        assert outcome.tool_calls == ["t1", "t2"]

    async def test_reset_between_tasks(self):
        master = FakeMaster()
        executor = MasterTaskExecutor(master)
        await executor.execute(TaskRecord(task="一"))
        await executor.execute(TaskRecord(task="二"))
        assert master.turns == ["一", "二"]
        assert master.resets == 1  # 首个不重置，第二个任务前重置一次

    async def test_reset_disabled(self):
        master = FakeMaster()
        executor = MasterTaskExecutor(master, reset_between_tasks=False)
        await executor.execute(TaskRecord(task="一"))
        await executor.execute(TaskRecord(task="二"))
        assert master.resets == 0

    async def test_execution_serialized_by_lock(self):
        probe = ConcurrencyProbe()
        executor = MasterTaskExecutor(probe, reset_between_tasks=False)
        await asyncio.gather(
            executor.execute(TaskRecord(task="a")),
            executor.execute(TaskRecord(task="b")),
        )
        assert probe.max_active == 1

    async def test_error_from_result_maps_to_error(self):
        master = FakeMaster(error="模型不可用")
        executor = MasterTaskExecutor(master)
        outcome = await executor.execute(TaskRecord(task="x"))
        assert not outcome.success
        assert "模型不可用" in outcome.error

    async def test_chat_turn_exception_caught(self):
        master = FakeMaster(raise_exc=RuntimeError("网络中断"))
        executor = MasterTaskExecutor(master)
        outcome = await executor.execute(TaskRecord(task="x"))
        assert not outcome.success
        assert "网络中断" in outcome.error

    async def test_factory_multi_tenant(self):
        created: dict[str, FakeMaster] = {}

        def factory(tenant: str) -> FakeMaster:
            master = FakeMaster()
            created[tenant] = master
            return master

        executor = MasterTaskExecutor(factory=factory)
        await executor.execute(TaskRecord(task="a", tenant="t1"))
        await executor.execute(TaskRecord(task="b", tenant="t2"))
        await executor.execute(TaskRecord(task="c", tenant="t1"))

        assert sorted(created) == ["t1", "t2"]
        assert created["t1"].turns == ["a", "c"]  # 同租户复用同一实例
        assert created["t2"].turns == ["b"]
        assert sorted(executor.tenants) == ["t1", "t2"]
        assert created["t1"].resets == 1

    async def test_aclose_destroys_masters(self):
        master = FakeMaster()
        executor = MasterTaskExecutor(master)
        await executor.execute(TaskRecord(task="x"))
        await executor.aclose()
        assert master.destroyed == 1

    def test_requires_master_or_factory(self):
        with pytest.raises(ValueError):
            MasterTaskExecutor()


# ----------------------------------------------------------------------
# service 端到端
# ----------------------------------------------------------------------

class TestGatewayService:
    async def test_submit_and_complete_e2e(self):
        executor = FakeExecutor()
        service = GatewayService(executor, size=2)
        await service.start()
        try:
            record = await service.submit("查天气", tenant="t1")
            assert record.status == TaskStatus.QUEUED
            await asyncio.wait_for(service.queue.join(), 5.0)
            saved = service.get(record.task_id)
            assert saved.status == TaskStatus.COMPLETED
            assert saved.result == "完成: 查天气"
            assert saved.worker_id  # worker-0 / worker-1
        finally:
            await service.stop()

    async def test_subscribe_full_stream(self):
        executor = FakeExecutor()
        service = GatewayService(executor, size=1)
        await service.start()
        try:
            record = await service.submit("x")
            stream = service.subscribe(record.task_id)
            await asyncio.wait_for(service.queue.join(), 5.0)
            events = [ev async for ev in stream]
        finally:
            await service.stop()
        # submit 时已发布的 queued 事件早于订阅建立，订阅流从 running 开始
        assert [e["status"] for e in events] == ["running", "completed"]

    async def test_list_filters_and_stats(self):
        executor = FakeExecutor()
        service = GatewayService(executor, size=1)
        await service.start()
        try:
            r1 = await service.submit("a", tenant="t1")
            r2 = await service.submit("b", tenant="t2")
            await asyncio.wait_for(service.queue.join(), 5.0)
            assert [r.task_id for r in service.list_tasks(tenant="t1")] == [r1.task_id]
            done = service.list_tasks(status=TaskStatus.COMPLETED)
            assert {r.task_id for r in done} == {r1.task_id, r2.task_id}
            stats = service.stats()
            assert stats["by_status"]["completed"] == 2
            assert stats["workers"] == 1
            assert stats["workers_running"] is True
            assert stats["queue_size"] == 0
        finally:
            await service.stop()
        assert service.stats()["workers_running"] is False

    async def test_without_executor_submit_only(self):
        service = GatewayService()
        await service.start()  # no-op
        record = await service.submit("x")
        assert record.status == TaskStatus.QUEUED
        stats = service.stats()
        assert stats["workers"] == 0
        assert stats["workers_running"] is False
        assert stats["queue_size"] == 1
        assert service.pool is None
        await service.stop()

    async def test_submit_with_metadata_and_custom_id(self):
        service = GatewayService()
        record = await service.submit(
            "x", tenant="team-a", metadata={"session_id": "s1"}, task_id="fixed-1"
        )
        assert record.task_id == "fixed-1"
        assert record.tenant == "team-a"
        assert record.metadata == {"session_id": "s1"}
        assert service.get("fixed-1") is record
