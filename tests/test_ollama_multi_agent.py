"""
真实 Ollama 多 Agent 协作集成测试

使用本地 Ollama (qwen2.5:3b) 驱动所有 Agent 的 LLM 推理。

前置条件:
1. Ollama 服务已启动: ollama serve
2. 已拉取模型: ollama pull qwen2.5:3b

测试层级（由简到繁）:
- Test 1: 单 Agent 对话基线 — 确认 LLM 可正常回复
- Test 2: 独立多 Agent 并行 — 3 个 Agent 各自处理不同子任务
- Test 3: MasterAgent 程序化编排 — 创建子 Agent，串联流水线
- Test 4: MasterAgent + 消息总线 — 子 Agent 间通过 InProcessBroker 通信
- Test 5: MasterAgent 自主编排（LLM 驱动） — MasterAgent 通过 tool_calls 自主创建子 Agent
"""

import asyncio
import sys
import os
import time
import json
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from youmi.core.agent import Agent, AgentConfig, AgentStatus
from youmi.core.types import LLMConfig, LLMProvider, MemoryConfig, AgentMetadata
from youmi.coordinator.master import MasterAgent
from youmi.llm.client import LLMClient
from youmi.bus.broker import InProcessBroker
from youmi.bus.message import WorkflowMessage, WorkflowMessageType

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

OLLAMA_BASE_URL = "http://localhost:11434/v1"
MODEL = "qwen2.5:3b"

passed = 0
failed = 0


def check(label: str, condition: bool) -> None:
    global passed, failed
    if condition:
        print(f"  ✓ {label}")
        passed += 1
    else:
        print(f"  ✗ {label}")
        failed += 1


def make_llm_config(temperature: float = 0.3) -> LLMConfig:
    return LLMConfig(
        provider=LLMProvider.LOCAL,
        model=MODEL,
        base_url=OLLAMA_BASE_URL,
        api_key="ollama",
        temperature=temperature,
        max_tokens=512,
        timeout_s=120,
    )


def make_config(name: str, system_prompt: str, temperature: float = 0.3) -> AgentConfig:
    return AgentConfig(
        name=name,
        system_prompt=system_prompt,
        llm_config=make_llm_config(temperature),
        memory_config=MemoryConfig(strategy="full"),
        metadata=AgentMetadata(display_name=name, role="general"),
        max_iterations=5,
    )


# =========================================================================
# Test 1: 单 Agent 对话基线
# =========================================================================

async def test_single_agent() -> None:
    """验证单个 Agent 可通过 Ollama 正常对话"""
    print("\n" + "=" * 60)
    print("  Test 1: 单 Agent 对话基线")
    print("=" * 60)

    agent = Agent(make_config(
        "测试助手",
        "你是一个简洁的中文助手，回答控制在50字以内。",
    ))
    agent._llm_client = LLMClient(make_llm_config())
    await agent.initialize()

    t0 = time.time()
    result = await agent.run("Python 的创始人是谁？用一个名字回答。")
    elapsed = time.time() - t0

    print(f"  回复: {result.output}")
    print(f"  耗时: {elapsed:.1f}s | 迭代: {result.iterations}")

    check("任务成功", result.success)
    check("1 轮完成", result.iterations == 1)
    check("输出包含 Guido 或 Python", any(
        kw in str(result.output).lower()
        for kw in ("guido", "python", "吉多", "范罗苏姆")
    ))

    await agent._llm_client.close()
    await agent.destroy()


# =========================================================================
# Test 2: 独立多 Agent 并行执行
# =========================================================================

async def test_parallel_agents() -> None:
    """3 个独立 Agent 各自处理不同子任务（并行）"""
    print("\n" + "=" * 60)
    print("  Test 2: 独立多 Agent 并行")
    print("=" * 60)

    tasks = [
        ("翻译Agent", "将以下中文翻译为英文：今天天气很好", "翻译", "translate"),
        ("摘要Agent", "用一句话总结：Python是一种高级编程语言，以简洁易读著称。", "总结", "summarize"),
        ("问答Agent", "1加1等于几？用数字回答。", "回答", "qa"),
    ]

    agents = []
    for name, task, _, _ in tasks:
        cfg = make_config(name, f"你是{name[:-5]}专家。回答简洁，不超过30字。", temperature=0.2)
        a = Agent(cfg)
        a._llm_client = LLMClient(make_llm_config(0.2))
        await a.initialize()
        agents.append((a, task))

    print(f"  已创建 {len(agents)} 个 Agent，开始并行执行...\n")

    t0 = time.time()
    results = await asyncio.gather(*[a.run(t) for a, t in agents])
    elapsed = time.time() - t0

    for i, (agent_result, (agent, task)) in enumerate(zip(results, agents)):
        print(f"  [{agent.name}]")
        print(f"    任务: {task}")
        print(f"    回复: {agent_result.output}")
        print(f"    状态: {agent_result.status.value} | 迭代: {agent_result.iterations}")
        check(f"{agent.name} 成功", agent_result.success)
        check(f"{agent.name} 输出非空", bool(agent_result.output))

    print(f"\n  总耗时: {elapsed:.1f}s（并行执行）")

    for a, _ in agents:
        await a._llm_client.close()
        await a.destroy()


# =========================================================================
# Test 3: MasterAgent 程序化编排 — 流水线
# =========================================================================

async def test_master_pipeline() -> None:
    """MasterAgent 创建 Writer → Reviewer 流水线"""
    print("\n" + "=" * 60)
    print("  Test 3: MasterAgent 程序化编排（流水线）")
    print("=" * 60)

    # 创建 MasterAgent
    master = MasterAgent(make_config(
        "MasterAgent",
        "你是一个任务协调者，负责管理子Agent完成任务。",
    ))
    await master.initialize()
    check("MasterAgent 初始化完成", master.status == AgentStatus.IDLE)

    # ---- 阶段 1: Writer 生成内容 ----
    print("\n  --- 阶段 1: Writer 生成 ---")
    writer = master.create_sub_agent(
        role="writer",
        task="用3句话介绍Python语言的优势",
        system_prompt="你是一个技术写作专家。回答简洁，用3句话描述。",
    )
    await writer.initialize()

    t0 = time.time()
    writer_result = await writer.run("用3句话介绍Python语言的优势")
    print(f"  Writer 输出: {writer_result.output}")
    print(f"  Writer 耗时: {time.time() - t0:.1f}s")
    check("Writer 成功", writer_result.success)

    # ---- 阶段 2: Reviewer 审核 Writer 输出 ----
    print("\n  --- 阶段 2: Reviewer 审核 ---")
    reviewer = master.create_sub_agent(
        role="reviewer",
        task="审核Writer的内容并给出改进建议",
        system_prompt="你是一个内容审核专家。对给出的内容进行简短点评，50字以内。",
    )
    await reviewer.initialize()

    t0 = time.time()
    reviewer_result = await reviewer.run(f"请审核以下内容：\n{writer_result.output}")
    print(f"  Reviewer 输出: {reviewer_result.output}")
    print(f"  Reviewer 耗时: {time.time() - t0:.1f}s")
    check("Reviewer 成功", reviewer_result.success)
    check("Reviewer 输出非空", bool(reviewer_result.output))

    # ---- 验证子 Agent 记录 ----
    subs = master.get_sub_agents()
    check("记录了 2 个子 Agent", len(subs) == 2)

    # ---- MasterAgent 汇总 ----
    summary = master.to_summary()
    print(f"\n  MasterAgent 摘要: {summary['sub_agent_count']} 个子 Agent")
    for aid, rec in summary["sub_agents"].items():
        print(f"    - {rec['name']} ({rec['role']}): {rec['status']}")

    await master.destroy()


# =========================================================================
# Test 4: MasterAgent + 消息总线跨 Agent 通信
# =========================================================================

async def test_bus_communication() -> None:
    """Agent 间通过 InProcessBroker 传递真实 LLM 产出内容"""
    print("\n" + "=" * 60)
    print("  Test 4: 消息总线跨 Agent 通信")
    print("=" * 60)

    broker = InProcessBroker()
    workflow_id = await broker.create_workflow()

    # 创建 WriterAgent 和 EditorAgent
    writer = Agent(make_config(
        "BusWriter",
        "你是一个写作助手。收到主题后用2-3句话写一篇短文。",
        temperature=0.3,
    ))
    editor = Agent(make_config(
        "BusEditor",
        "你是一个编辑。收到文章后给出简短修改建议，不超过30字。",
        temperature=0.2,
    ))

    # 连接总线
    for a in (writer, editor):
        a._llm_client = LLMClient(make_llm_config(0.3 if a == writer else 0.2))
        await broker.subscribe(a.agent_id, workflow_id)
        a.connect_bus(broker, workflow_id)
        await a.initialize()

    # Writer 生成内容
    print("\n  --- Writer 生成内容 ---")
    t0 = time.time()
    writer_result = await writer.run("请写一篇关于人工智能的短文")
    writer_content = str(writer_result.output)
    print(f"  Writer: {writer_content[:80]}...")
    print(f"  Writer 耗时: {time.time() - t0:.1f}s")
    check("Writer 成功", writer_result.success)

    # Writer 通过总线发送给 Editor
    await writer.send_message(
        to_agent_id=editor.agent_id,
        content=writer_content,
    )

    # Editor 接收并处理
    print("\n  --- Editor 接收并审核 ---")
    msg = await editor.wait_for_message(timeout=5.0)
    check("Editor 收到消息", msg is not None)

    if msg:
        t0 = time.time()
        editor_result = await editor.run(f"请审核并修改这篇短文：\n{msg.content}")
        print(f"  Editor: {editor_result.output}")
        print(f"  Editor 耗时: {time.time() - t0:.1f}s")
        check("Editor 成功", editor_result.success)

        # Editor 回复给 Writer
        await editor.send_message(
            to_agent_id=writer.agent_id,
            content=str(editor_result.output),
        )

        # Writer 收到反馈
        feedback = await writer.wait_for_message(timeout=5.0)
        check("Writer 收到反馈", feedback is not None)
        if feedback:
            print(f"  Writer 收到反馈: {feedback.content[:60]}...")

    # 清理
    for a in (writer, editor):
        await a._llm_client.close()
        await a.destroy()
    await broker.close()


# =========================================================================
# Test 5: MasterAgent 自主编排（LLM 驱动 tool_calls）
# =========================================================================

async def test_autonomous_orchestration() -> None:
    """MasterAgent 通过 LLM 自主决策是否创建子 Agent（探索性测试）"""
    print("\n" + "=" * 60)
    print("  Test 5: MasterAgent 自主编排（探索性）")
    print("=" * 60)
    print("  注意: 3B 模型可能无法稳定进行自主编排，此测试为探索性。")

    master = MasterAgent(make_config(
        "AutoMaster",
        (
            "你是一个任务协调 MasterAgent。你有以下工具可以创建和管理子Agent：\n"
            "- create_sub_agent: 创建子Agent（参数: role, task, system_prompt）\n"
            "- run_sub_agent: 运行子Agent（参数: agent_id）\n"
            "- list_sub_agents: 列出所有子Agent\n\n"
            "当任务复杂时，创建子Agent来分工。当任务简单时，直接回答即可。"
        ),
        temperature=0.2,
    ))
    await master.initialize()
    print(f"  MasterAgent 工具: {master.tool_registry.tool_names}")

    # 用一个简单任务测试 —— LLM 可能选择直接回答
    print("\n  --- 简单任务: 直接回答 vs 创建子Agent ---")
    t0 = time.time()
    result = await master.run("1加1等于几？")
    elapsed = time.time() - t0

    print(f"  回复: {result.output}")
    print(f"  耗时: {elapsed:.1f}s | 迭代: {result.iterations}")

    tool_msgs = [m for m in master._conversation if m.get("role") == "tool"]
    used_tools = len(tool_msgs) > 0
    check("简单任务完成", result.success)

    if used_tools:
        print("  → MasterAgent 使用了工具（可能对简单任务也创建了子Agent）")
        for tm in tool_msgs:
            print(f"    工具返回: {tm['content'][:80]}...")
    else:
        print("  → MasterAgent 直接回答（未使用工具，符合预期）")

    # 用一个复杂任务测试 —— LLM 更可能使用工具
    print("\n  --- 复杂任务: 需要分工的多步骤任务 ---")
    master2 = MasterAgent(make_config(
        "AutoMaster2",
        (
            "你是一个任务协调 MasterAgent。你**必须**使用工具来分工完成复杂任务。\n"
            "请按照以下步骤：\n"
            "1. 使用 create_sub_agent 创建一个 writer 子Agent，任务是写一篇关于Python的短文\n"
            "2. 使用 run_sub_agent 运行该子Agent\n"
            "3. 根据子Agent的输出，给出最终总结\n\n"
            "工具使用说明：\n"
            "- create_sub_agent(role='writer', task='写一篇关于Python的短文', system_prompt='你是写作专家，用3句话描述。')\n"
            "- run_sub_agent(agent_id=上面返回的agent_id)"
        ),
        temperature=0.1,
    ))
    await master2.initialize()

    t0 = time.time()
    result2 = await master2.run("请帮我写一篇关于Python编程语言的介绍文章。")
    elapsed = time.time() - t0

    print(f"  回复: {str(result2.output)[:200]}...")
    print(f"  耗时: {elapsed:.1f}s | 迭代: {result2.iterations}")

    tool_msgs2 = [m for m in master2._conversation if m.get("role") == "tool"]
    check("复杂任务完成", result2.success)
    print(f"  工具调用次数: {len(tool_msgs2)}")

    subs = master2.get_sub_agents()
    if subs:
        print(f"  创建了 {len(subs)} 个子Agent:")
        for aid, rec in subs.items():
            info = rec.to_dict()
            print(f"    - {info['name']} ({info['role']}): {info['status']}")
            if rec.result:
                print(f"      输出: {str(rec.result.output)[:80]}...")
        check("成功创建子Agent", True)
    else:
        print("  (3B模型未成功创建子Agent，这是预期行为)")

    await master.destroy()
    await master2.destroy()


# =========================================================================
# Main
# =========================================================================

async def main() -> None:
    global passed, failed

    print("=" * 60)
    print("  YouMi Agent — Ollama 多 Agent 协作真实测试")
    print(f"  Ollama: {OLLAMA_BASE_URL}")
    print(f"  Model:  {MODEL}")
    print("=" * 60)

    # 检测 Ollama
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{OLLAMA_BASE_URL}/models", timeout=10)
            models = resp.json().get("data", [])
            model_names = [m["id"] for m in models]
            print(f"  可用模型: {model_names}")
            if not any(MODEL in name for name in model_names):
                print(f"\n  [ERROR] 模型 '{MODEL}' 未找到！请先运行: ollama pull {MODEL}")
                return
    except Exception as e:
        print(f"\n  [ERROR] 无法连接 Ollama: {e}")
        return

    try:
        await test_single_agent()
        await test_parallel_agents()
        await test_master_pipeline()
        await test_bus_communication()
        await test_autonomous_orchestration()
    except Exception as e:
        print(f"\n  [FATAL] 测试异常: {e}")
        traceback.print_exc()

    print("\n" + "=" * 60)
    print(f"  结果: {passed} 通过, {failed} 失败")
    print("=" * 60)

    if failed > 0:
        sys.exit(1)
    print("\n  === 全部测试通过！ ===")


if __name__ == "__main__":
    asyncio.run(main())
