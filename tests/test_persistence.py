"""Session 持久化测试 (P0: Persistence)

测试覆盖:
1. MessageRecord / SessionRecord 数据模型
2. SQLiteBackend — CRUD 全流程
3. FileBackend — CRUD 全流程
4. MemoryManager + PersistenceBackend 集成
5. Agent + Persistence 自动恢复 session
"""

import asyncio
import os
import tempfile
from datetime import datetime

from youmi.memory.backends.base import (
    PersistenceBackend,
    MessageRecord,
    SessionRecord,
)
from youmi.memory.backends.sqlite_backend import SQLiteBackend
from youmi.memory.backends.file_backend import FileBackend
from youmi.memory.memory import MemoryManager
from youmi.core.agent import Agent, AgentConfig, AgentStatus, _Observation, _Thought
from youmi.core.types import (
    AgentMetadata,
    MemoryConfig,
    SessionPersistenceConfig,
)


# =========================================================================
# 辅助工具
# =========================================================================

SAMPLE_MESSAGES = [
    {"role": "system", "content": "你是助手"},
    {"role": "user", "content": "你好"},
    {"role": "assistant", "content": "你好！有什么可以帮助你的？"},
    {"role": "user", "content": "帮我写代码"},
    {"role": "assistant", "content": "好的，请问什么语言？"},
]


class EchoAgent(Agent):
    """测试用 Agent"""

    async def _think(self, observation: _Observation) -> _Thought:
        last = ""
        for msg in reversed(observation.messages):
            if msg.get("role") == "user":
                last = msg.get("content", "")
                break
        return _Thought(
            reasoning="echo",
            action_type="respond",
            action_payload={"response": f"Echo: {last}"},
            should_continue=False,
        )


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "[OK]" if condition else "[FAIL]"
    msg = f"{status} {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    assert condition, f"FAILED: {label} {detail}"


# =========================================================================
# 测试1: 数据模型
# =========================================================================

async def test_data_models():
    print("\n=== Test 1: Data Models ===")

    # MessageRecord 构造
    rec = MessageRecord(role="user", content="你好")
    check("role", rec.role == "user")
    check("content", rec.content == "你好")
    check("raw_data默认空", rec.raw_data == {})
    check("timestamp有值", rec.timestamp is not None)

    # to_openai_message
    msg = rec.to_openai_message()
    check("转openai格式", msg["role"] == "user" and msg["content"] == "你好")

    # from_openai_message
    raw_msg = {
        "role": "assistant",
        "content": "好的",
        "tool_calls": [{"id": "c1", "function": {"name": "test"}}],
    }
    rec2 = MessageRecord.from_openai_message(raw_msg)
    check("from_openai role", rec2.role == "assistant")
    check("from_openai content", rec2.content == "好的")
    check("raw_data含tool_calls", "tool_calls" in rec2.raw_data)

    # 转回 openai 格式保留 tool_calls
    msg2 = rec2.to_openai_message()
    check("保留tool_calls", "tool_calls" in msg2)

    # SessionRecord
    sess = SessionRecord(session_id="s1", agent_id="a1")
    check("session_id", sess.session_id == "s1")
    check("agent_id", sess.agent_id == "a1")
    check("metadata默认空", sess.metadata == {})


# =========================================================================
# 测试2: SQLiteBackend
# =========================================================================

async def test_sqlite_backend():
    print("\n=== Test 2: SQLiteBackend ===")

    # 使用内存数据库
    backend = SQLiteBackend(db_path=":memory:")
    await backend.initialize()

    # save_session
    await backend.save_session("s1", "a1", SAMPLE_MESSAGES)
    check("save成功", True)

    # load_messages
    loaded = await backend.load_messages("s1")
    check("load消息数", len(loaded) == len(SAMPLE_MESSAGES), f"got {len(loaded)}")
    check("load第一条role", loaded[0]["role"] == "system")
    check("load第一条content", loaded[0]["content"] == "你是助手")

    # list_sessions
    sessions = await backend.list_sessions("a1")
    check("sessions数", len(sessions) == 1)
    check("session_id匹配", sessions[0].session_id == "s1")
    check("agent_id匹配", sessions[0].agent_id == "a1")

    # get_latest_session
    latest = await backend.get_latest_session("a1")
    check("latest存在", latest is not None)
    check("latest_id", latest.session_id == "s1")

    # 保存第二个 session
    await backend.save_session("s2", "a1", [{"role": "user", "content": "新对话"}])
    sessions2 = await backend.list_sessions("a1")
    check("两个sessions", len(sessions2) == 2)

    # 覆盖写入 s1
    new_msgs = [{"role": "user", "content": "更新后"}]
    await backend.save_session("s1", "a1", new_msgs)
    reloaded = await backend.load_messages("s1")
    check("覆盖写入", len(reloaded) == 1)
    check("覆盖内容", reloaded[0]["content"] == "更新后")

    # delete_session
    await backend.delete_session("s2")
    sessions3 = await backend.list_sessions("a1")
    check("删除后剩余1", len(sessions3) == 1)
    check("剩余是s1", sessions3[0].session_id == "s1")

    # 不存在的 session
    empty = await backend.load_messages("nonexistent")
    check("不存在返回空", len(empty) == 0)

    latest_none = await backend.get_latest_session("nonexistent_agent")
    check("不存在返回None", latest_none is None)

    # 带 metadata 保存
    await backend.save_session(
        "s3", "a1",
        [{"role": "user", "content": "test"}],
        metadata={"tag": "important"},
    )
    s3 = await backend.get_latest_session("a1")
    check("metadata保存", s3 is not None and s3.metadata.get("tag") == "important")

    # 带 tool_calls 的消息
    tool_msgs = [
        {"role": "user", "content": "调用工具"},
        {
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "calc", "arguments": "{}"}}],
        },
        {"role": "tool", "content": "42", "tool_call_id": "c1"},
    ]
    await backend.save_session("s4", "a1", tool_msgs)
    loaded_tool = await backend.load_messages("s4")
    check("tool消息保存", len(loaded_tool) == 3)
    check("tool_call_id保留", loaded_tool[2].get("tool_call_id") == "c1")

    await backend.close()
    check("close成功", True)


# =========================================================================
# 测试3: FileBackend
# =========================================================================

async def test_file_backend():
    print("\n=== Test 3: FileBackend ===")

    with tempfile.TemporaryDirectory() as tmpdir:
        backend = FileBackend(base_dir=tmpdir)
        await backend.initialize()

        # save_session
        await backend.save_session("s1", "a1", SAMPLE_MESSAGES)
        check("save成功", True)

        # load_messages (FileBackend 通过遍历目录查找)
        loaded = await backend.load_messages("s1")
        check("load消息数", len(loaded) == len(SAMPLE_MESSAGES), f"got {len(loaded)}")
        check("load第一条content", loaded[0]["content"] == "你是助手")

        # list_sessions
        sessions = await backend.list_sessions("a1")
        check("sessions数", len(sessions) == 1)
        check("session_id匹配", sessions[0].session_id == "s1")

        # get_latest_session
        latest = await backend.get_latest_session("a1")
        check("latest存在", latest is not None)

        # 保存第二个 session
        await backend.save_session("s2", "a1", [{"role": "user", "content": "新对话"}])
        sessions2 = await backend.list_sessions("a1")
        check("两个sessions", len(sessions2) == 2)

        # 覆盖 s1
        await backend.save_session("s1", "a1", [{"role": "user", "content": "更新"}])
        reloaded = await backend.load_messages("s1")
        check("覆盖写入", len(reloaded) == 1)

        # delete_session
        await backend.delete_session("s2")
        sessions3 = await backend.list_sessions("a1")
        check("删除后剩1", len(sessions3) == 1)

        # 不同 agent 隔离
        await backend.save_session("s10", "a2", [{"role": "user", "content": "agent2"}])
        a1_sessions = await backend.list_sessions("a1")
        a2_sessions = await backend.list_sessions("a2")
        check("a1隔离", len(a1_sessions) == 1)
        check("a2隔离", len(a2_sessions) == 1)


# =========================================================================
# 测试4: MemoryManager + Persistence 集成
# =========================================================================

async def test_memory_manager_persistence():
    print("\n=== Test 4: MemoryManager + Persistence ===")

    backend = SQLiteBackend(db_path=":memory:")
    manager = MemoryManager(
        agent_id="a1",
        strategy="full",
        persistence_backend=backend,
    )

    # 属性
    check("persistence存在", manager.persistence is not None)
    check("session_id初始空", manager.current_session_id == "")

    await manager.initialize()

    # start_session
    sid = manager.start_session("test-session-1")
    check("session_id设置", sid == "test-session-1")
    check("current_session_id", manager.current_session_id == "test-session-1")

    # 记录消息
    await manager.on_message("user", "你好")
    await manager.on_message("assistant", "你好！")
    await manager.on_message("user", "帮我写排序")

    # save_session (从策略中获取上下文保存)
    await manager.on_session_end()

    # 验证持久化
    loaded = await backend.load_messages("test-session-1")
    check("持久化消息数", len(loaded) >= 3, f"got {len(loaded)}")

    # 恢复 session (共享同一个 :memory: backend 实例)
    manager2 = MemoryManager(
        agent_id="a1",
        strategy="full",
        persistence_backend=backend,
    )
    await manager2.initialize()

    restored = await manager2.restore_session("test-session-1")
    check("恢复成功", restored is not None)
    check("恢复消息数", len(restored) >= 3, f"got {len(restored)}")
    check("current_session恢复", manager2.current_session_id == "test-session-1")

    # 自动恢复最近 session (共享同一个 :memory: backend)
    manager3 = MemoryManager(
        agent_id="a1",
        strategy="full",
        persistence_backend=backend,
    )
    await manager3.initialize()

    restored2 = await manager3.restore_session()  # 不指定 id, 自动恢复最近
    check("自动恢复成功", restored2 is not None)
    check("自动恢复有消息", len(restored2) >= 3)

    # close
    await manager.close()
    await manager2.close()
    await manager3.close()


# =========================================================================
# 测试5: Agent + Persistence 集成
# =========================================================================

async def test_agent_persistence_integration():
    print("\n=== Test 5: Agent + Persistence ===")

    # 5a: persistence 启用 → Agent 有 persistence backend
    config = AgentConfig(
        name="PersistAgent",
        memory_config=MemoryConfig(
            persistence=SessionPersistenceConfig(
                enabled=True,
                backend="sqlite",
                db_path=":memory:",
                auto_restore=False,  # 测试时不自动恢复
            ),
        ),
        metadata=AgentMetadata(role="test"),
    )
    agent = EchoAgent(config)
    await agent.initialize()

    check("persistence已启用", agent.memory.persistence is not None)

    result = await agent.run(task="持久化测试", task_id="t1")
    check("任务成功", result.success)
    check("session已创建", agent.memory.current_session_id != "")

    # 验证 session 已保存
    sessions = await agent.memory.persistence.list_sessions(agent.agent_id)
    check("session已保存", len(sessions) >= 1)

    await agent.destroy()

    # 5b: persistence 禁用 → Agent 无 persistence
    config2 = AgentConfig(
        name="NoPersistAgent",
        memory_config=MemoryConfig(
            persistence=SessionPersistenceConfig(enabled=False),
        ),
        metadata=AgentMetadata(role="test"),
    )
    agent2 = EchoAgent(config2)
    await agent2.initialize()

    check("persistence未启用", agent2.memory.persistence is None)

    result2 = await agent2.run(task="无持久化", task_id="t2")
    check("无持久化任务成功", result2.success)
    await agent2.destroy()

    # 5c: file backend
    with tempfile.TemporaryDirectory() as tmpdir:
        config3 = AgentConfig(
            name="FilePersistAgent",
            memory_config=MemoryConfig(
                persistence=SessionPersistenceConfig(
                    enabled=True,
                    backend="file",
                    base_dir=tmpdir,
                    auto_restore=False,
                ),
            ),
            metadata=AgentMetadata(role="test"),
        )
        agent3 = EchoAgent(config3)
        await agent3.initialize()

        check("file backend", isinstance(agent3.memory.persistence, FileBackend))

        result3 = await agent3.run(task="文件持久化测试", task_id="t3")
        check("文件持久化成功", result3.success)

        # 验证文件已创建
        sessions3 = await agent3.memory.persistence.list_sessions(agent3.agent_id)
        check("文件session保存", len(sessions3) >= 1)

        await agent3.destroy()


# =========================================================================
# 测试6: 未知后端类型 → 返回 None
# =========================================================================

async def test_unknown_backend():
    print("\n=== Test 6: Unknown persistence backend ===")

    config = AgentConfig(
        name="BadBackendAgent",
        memory_config=MemoryConfig(
            persistence=SessionPersistenceConfig(
                enabled=True,
                backend="redis",  # 不支持的类型
            ),
        ),
        metadata=AgentMetadata(role="test"),
    )
    agent = EchoAgent(config)
    await agent.initialize()

    # 未知后端应该退化为 None
    check("未知后端退化None", agent.memory.persistence is None)

    result = await agent.run(task="测试", task_id="t6")
    check("任务仍成功", result.success)
    await agent.destroy()


# =========================================================================
# 主入口
# =========================================================================

async def main():
    await test_data_models()
    await test_sqlite_backend()
    await test_file_backend()
    await test_memory_manager_persistence()
    await test_agent_persistence_integration()
    await test_unknown_backend()
    print("\n" + "=" * 50)
    print("  All persistence tests passed!")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
