"""ToolGuardian 全局记忆闭环测试 (Phase 6 收尾)

测试覆盖:
1. ToolGuardianAgent 接入 GlobalMemory — 构造参数 / to_summary 状态
2. 核心闭环: 修复前查询历史经验 → 规则修复注入 known_issues
   → 修复成功后写入 BUG_FIX 经验 → 历史未解决问题标记 resolved
3. 优雅降级: 未接入 GlobalMemory / GlobalMemory 读失败
4. FixStrategiesMixin — LLM prompt 注入历史经验 / 规则路径注入
5. search_tool_experience 内置工具
6. 重复修复 — 第二轮无未解决条目时 resolved_count == 0
"""

import json

from youmi.core.agent import AgentStatus
from youmi.coordinator.tool_guardian import ToolGuardianAgent
from youmi.coordinator.fix_strategies import FixStrategiesMixin
from youmi.knowledge import GlobalMemory, KnowledgeCategory, ToolKnowledge
from youmi.mcp.protocol import ToolIssueReport, ToolIssueType
from youmi.mcp.provider import LocalFunctionProvider
from youmi.mcp.server import MCPServer


# =========================================================================
# 辅助工具
# =========================================================================

def check(label: str, condition: bool, detail: str = "") -> None:
    status = "[OK]" if condition else "[FAIL]"
    msg = f"{status} {label}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    assert condition, f"FAILED: {label} {detail}"


def get_weather(city: str, unit: str = "celsius") -> str:
    """获取指定城市的天气"""
    weather_db = {
        "北京": {"celsius": "25°C", "fahrenheit": "77°F"},
        "上海": {"celsius": "28°C", "fahrenheit": "82°F"},
    }
    data = weather_db.get(city)
    if data is None:
        raise ValueError(f"城市 '{city}' 不在支持列表中")
    return data.get(unit, data.get("celsius", "未知"))


async def make_server() -> MCPServer:
    """搭建带 get_weather 工具的 MCPServer"""
    server = MCPServer()
    provider = LocalFunctionProvider(provider_id="local")
    provider.register_function(get_weather)
    await server.register_provider(provider)
    return server


async def make_guardian(server: MCPServer, global_memory=None) -> ToolGuardianAgent:
    guardian = ToolGuardianAgent(mcp_server=server, global_memory=global_memory)
    await guardian.initialize()
    return guardian


def make_report(error_message: str = "城市 '天津' 不在支持列表中") -> ToolIssueReport:
    return ToolIssueReport(
        reporter_agent_id="worker-001",
        tool_name="get_weather",
        issue_type=ToolIssueType.PARAMETER_BOUNDARY,
        error_message=error_message,
        call_arguments={"city": "天津"},
        suggestion="建议在描述中列出支持的城市列表",
    )


class MockLLMClient:
    """Mock LLM 客户端 — 捕获 prompt 并返回合法 JSON"""

    def __init__(self, response: dict | None = None) -> None:
        self.messages_history: list[list[dict]] = []
        self._response = response or {
            "new_description": "获取指定城市的天气（仅支持北京、上海）",
            "param_updates": {"city": "城市名，仅支持北京、上海"},
            "code_suggestion": "",
        }

    async def chat(self, messages, **kwargs):
        self.messages_history.append(messages)
        # 模拟 LLMResponse
        class _Resp:
            def __init__(self, content: str) -> None:
                self.content = content

        return _Resp(json.dumps(self._response, ensure_ascii=False))


class FailingGlobalMemory:
    """全局记忆 Mock — 所有读操作抛异常，验证优雅降级"""

    async def get_tool_knowledge(self, tool_name: str) -> ToolKnowledge:
        raise RuntimeError("database corrupted")

    async def add_experience(self, **kwargs):
        raise RuntimeError("write failed")


# =========================================================================
# Test 1: 构造接入与状态
# =========================================================================

async def test_guardian_accepts_global_memory():
    print("\n=== Test 1: ToolGuardianAgent 接入 GlobalMemory ===")

    server = await make_server()
    memory = GlobalMemory(db_path=":memory:")
    await memory.initialize()

    guardian = await make_guardian(server, global_memory=memory)
    check("初始化成功", guardian.status == AgentStatus.IDLE)
    check("global_memory 已持有", guardian._global_memory is memory)
    check("摘要含 global_memory_enabled", guardian.to_summary()["global_memory_enabled"] is True)

    # 未接入时
    guardian2 = await make_guardian(server)
    check("未接入时摘要为 False", guardian2.to_summary()["global_memory_enabled"] is False)

    # from_config_dir 透传 global_memory
    guardian3 = ToolGuardianAgent.from_config_dir(
        mcp_server=server, global_memory=memory,
    )
    await guardian3.initialize()
    check("from_config_dir 透传 global_memory", guardian3._global_memory is memory)


# =========================================================================
# Test 2: 核心闭环 — 查询经验 / 修复 / 写回 / 标记 resolved
# =========================================================================

async def test_guardian_memory_loop():
    print("\n=== Test 2: ToolGuardian 核心闭环 ===")

    server = await make_server()
    memory = GlobalMemory(db_path=":memory:")
    await memory.initialize()

    # 预置: 一条未解决的历史失败经验 (来自 PostTaskPipeline 沉淀)
    history_entry = await memory.add_experience(
        tool_name="get_weather",
        content="get_weather 失败根因: 城市参数传入 '天津' 时报错不在支持列表中",
        category=KnowledgeCategory.TOOL_EXPERIENCE,
        source_agent_id="worker_old",
        success_rate=0.4,
    )
    check("预置条目未解决", history_entry.resolved is False)

    guardian = await make_guardian(server, global_memory=memory)

    # 收到同类汇报
    await guardian.receive_report(make_report())
    results = await guardian.process_reports()

    check("处理结果数", len(results) == 1)
    result = results[0]

    # a. 修复前查询到了历史经验
    check("knowledge_entries 计数", result.get("knowledge_entries") == 1,
          f"got: {result.get('knowledge_entries')}")

    # b. 规则修复注入了全局记忆中的已知问题
    check("描述已更新", result["description_updated"] is True)
    check("新描述含全局记忆注入", "[全局记忆]" in result["new_description"],
          f"got: {result.get('new_description', '')[:200]}")
    check("新描述含历史问题内容", "天津" in result["new_description"])

    # c. 修复结果写回全局记忆
    memory_updated = result.get("memory_updated")
    check("memory_updated 存在", memory_updated is not None)
    check("resolved_count == 1", memory_updated["resolved_count"] == 1,
          f"got: {memory_updated}")

    # d. BUG_FIX 经验已写入且标记 resolved
    entries = await memory.list_entries(tool_name="get_weather")
    bug_fixes = [e for e in entries if e.category == KnowledgeCategory.BUG_FIX]
    check("BUG_FIX 经验已写入", len(bug_fixes) == 1)
    check("BUG_FIX 已标记 resolved", bug_fixes[0].resolved is True)
    check("BUG_FIX 含修复描述", "ToolGuardian 修复" in bug_fixes[0].content)
    check("BUG_FIX 含修改记录", "modification" in bug_fixes[0].metadata)

    # e. 历史未解决条目已被标记 resolved 并记录修复方案
    resolved_history = await memory.get_entry(history_entry.entry_id)
    check("历史条目已 resolved", resolved_history.resolved is True)
    check("历史条目记录了修复方案", "ToolGuardian 修复" in (resolved_history.resolution or ""))

    # f. 聚合知识更新: known_issues 清零，fix_history 有记录
    knowledge = await memory.get_tool_knowledge("get_weather")
    check("known_issues 已清空", len(knowledge.known_issues) == 0,
          f"got: {knowledge.known_issues}")
    check("fix_history 有记录", len(knowledge.fix_history) == 1)


# =========================================================================
# Test 3: 未接入 GlobalMemory 时优雅降级
# =========================================================================

async def test_guardian_without_memory():
    print("\n=== Test 3: 未接入 GlobalMemory 优雅降级 ===")

    server = await make_server()
    guardian = await make_guardian(server)  # 不传 global_memory

    await guardian.receive_report(make_report())
    results = await guardian.process_reports()

    check("处理结果数", len(results) == 1)
    result = results[0]
    check("描述仍被更新", result["description_updated"] is True)
    check("无 knowledge_entries", "knowledge_entries" not in result)
    check("无 memory_updated", "memory_updated" not in result)
    check("描述无全局记忆段", "[全局记忆]" not in result.get("new_description", ""))
    check("修改数正常", guardian.modification_count == 1)


# =========================================================================
# Test 4: GlobalMemory 读失败时修复流程不受影响
# =========================================================================

async def test_guardian_memory_failure_degrades():
    print("\n=== Test 4: GlobalMemory 读失败优雅降级 ===")

    server = await make_server()
    failing = FailingGlobalMemory()
    guardian = await make_guardian(server, global_memory=failing)

    await guardian.receive_report(make_report())
    results = await guardian.process_reports()

    check("处理结果数", len(results) == 1)
    result = results[0]
    # get_tool_knowledge 抛异常 → tool_knowledge 为 None → 规则修复正常
    check("描述仍被更新", result["description_updated"] is True)
    check("无 knowledge_entries", "knowledge_entries" not in result)
    # add_experience 也抛异常 → memory_updated 为 None → 不写入结果
    check("无 memory_updated", "memory_updated" not in result)


# =========================================================================
# Test 5: LLM 修复路径注入历史经验
# =========================================================================

async def test_llm_fix_injects_knowledge():
    print("\n=== Test 5: LLM 修复路径注入历史经验 ===")

    server = await make_server()
    guardian = await make_guardian(server)
    llm = MockLLMClient()
    guardian._llm_client = llm

    knowledge = ToolKnowledge(tool_name="get_weather")
    knowledge.known_issues.append("城市 '天津' 不在支持列表中 (历史问题)")
    knowledge.fix_history.append("曾经修复: 添加城市白名单说明")

    # mock 经验加载（GlobalMemory 真实查询链路由 Test 2 覆盖）
    async def fake_load(tool_name: str) -> ToolKnowledge:
        return knowledge
    guardian._load_tool_knowledge = fake_load

    result = await guardian._process_tool_reports(
        "get_weather", [make_report()],
    )

    # 直接验证 prompt 注入
    check("LLM 被调用", len(llm.messages_history) == 1)
    prompt = llm.messages_history[0][1]["content"]
    check("prompt 含历史经验段", "历史经验" in prompt)
    check("prompt 含已知问题", "历史问题" in prompt)
    check("prompt 含历史修复记录", "历史修复记录" in prompt)
    check("prompt 含根治提示", "根治性修复" in prompt)

    # LLM 返回的新描述被应用
    check("LLM 描述被应用", result["description_updated"] is True)
    check("新描述为 LLM 输出", "仅支持北京、上海" in result["new_description"])

    # 无知识时 prompt 不含历史经验段
    llm2 = MockLLMClient()
    guardian2 = await make_guardian(await make_server())
    guardian2._llm_client = llm2
    await guardian2._process_tool_reports("get_weather", [make_report()])
    prompt2 = llm2.messages_history[0][1]["content"]
    check("无知识时不注入", "历史经验" not in prompt2)


# =========================================================================
# Test 6: 规则修复路径注入历史经验 (直接调用 Mixin)
# =========================================================================

async def test_rules_fix_injects_knowledge():
    print("\n=== Test 6: 规则修复路径注入历史经验 ===")

    knowledge = ToolKnowledge(tool_name="get_weather")
    # 历史经验是根因分析文本，与本次 error_message 不同（相同文本会被去重）
    knowledge.known_issues.append("历史教训: 调用方常误传拼音形式的城市名导致报错")

    new_desc, param_updates, code_suggestion = FixStrategiesMixin._generate_fix_with_rules(
        tool_name="get_weather",
        current_description="获取指定城市的天气",
        current_params={"city": "城市名称"},
        reports=[make_report("城市 '广州' 不在支持列表中")],
        primary_type=ToolIssueType.PARAMETER_BOUNDARY,
        tool_knowledge=knowledge,
    )

    check("规则修复含 Guardian 段", "[Guardian 修正]" in new_desc)
    check("规则修复含全局记忆段", "[全局记忆]" in new_desc)
    check("known_issues 被附加", "拼音形式" in new_desc)

    # 无知识时行为不变
    new_desc2, _, _ = FixStrategiesMixin._generate_fix_with_rules(
        tool_name="get_weather",
        current_description="获取指定城市的天气",
        current_params={},
        reports=[make_report()],
        primary_type=ToolIssueType.PARAMETER_BOUNDARY,
        tool_knowledge=None,
    )
    check("无知识时不含全局记忆段", "[全局记忆]" not in new_desc2)

    # tool_knowledge 为空对象时也不注入
    empty_knowledge = ToolKnowledge(tool_name="get_weather")
    new_desc3, _, _ = FixStrategiesMixin._generate_fix_with_rules(
        tool_name="get_weather",
        current_description="获取指定城市的天气",
        current_params={},
        reports=[make_report()],
        primary_type=ToolIssueType.PARAMETER_BOUNDARY,
        tool_knowledge=empty_knowledge,
    )
    check("空知识对象不注入", "[全局记忆]" not in new_desc3)


# =========================================================================
# Test 7: search_tool_experience 内置工具
# =========================================================================

async def test_search_tool_experience_tool():
    print("\n=== Test 7: search_tool_experience 内置工具 ===")

    server = await make_server()

    # 未接入 global_memory
    guardian = await make_guardian(server)
    tool_names = guardian.tool_registry.tool_names
    check("search_tool_experience 已注册", "search_tool_experience" in tool_names)

    raw = await guardian.tool_registry.execute("search_tool_experience", {"query": "天气"})
    data = json.loads(raw)
    check("未接入时返回 unavailable", data["status"] == "unavailable")

    # 接入后可检索
    memory = GlobalMemory(db_path=":memory:")
    await memory.initialize()
    await memory.add_experience(
        tool_name="get_weather",
        content="get_weather 城市参数仅支持北京上海，其他城市报错",
        category=KnowledgeCategory.TOOL_EXPERIENCE,
    )

    guardian2 = await make_guardian(server, global_memory=memory)
    raw2 = await guardian2.tool_registry.execute(
        "search_tool_experience", {"query": "城市 参数 报错"},
    )
    data2 = json.loads(raw2)
    check("检索成功", data2["status"] == "ok")
    check("返回结果", data2["result_count"] >= 1)
    check("结果含 tool_name", data2["entries"][0]["tool_name"] == "get_weather")
    check("结果含 content", "北京上海" in data2["entries"][0]["content"])

    # 异常路径
    failing = FailingGlobalMemory()
    guardian3 = await make_guardian(server, global_memory=failing)
    raw3 = await guardian3.tool_registry.execute(
        "search_tool_experience", {"query": "anything"},
    )
    data3 = json.loads(raw3)
    check("异常时返回 error", data3["status"] == "error")


# =========================================================================
# Test 8: 重复修复 — resolved_count 归零 + BUG_FIX 持续累积
# =========================================================================

async def test_repeated_fix_dedup():
    print("\n=== Test 8: 重复修复去重 ===")

    server = await make_server()
    memory = GlobalMemory(db_path=":memory:")
    await memory.initialize()

    await memory.add_experience(
        tool_name="get_weather",
        content="get_weather 失败: 城市 '天津' 不在支持列表中",
        category=KnowledgeCategory.TOOL_EXPERIENCE,
    )

    guardian = await make_guardian(server, global_memory=memory)

    # 第一轮: 修复 + 标记历史条目
    await guardian.receive_report(make_report())
    results1 = await guardian.process_reports()
    check("第一轮 resolved_count == 1",
          results1[0]["memory_updated"]["resolved_count"] == 1)

    # 第二轮同类汇报: 无未解决条目 → resolved_count == 0，但 BUG_FIX +1
    await guardian.receive_report(make_report("城市 '广州' 不在支持列表中"))
    results2 = await guardian.process_reports()
    check("第二轮 resolved_count == 0",
          results2[0]["memory_updated"]["resolved_count"] == 0,
          f"got: {results2[0]['memory_updated']}")

    entries = await memory.list_entries(tool_name="get_weather")
    bug_fixes = [e for e in entries if e.category == KnowledgeCategory.BUG_FIX]
    check("BUG_FIX 累积 2 条", len(bug_fixes) == 2)
    check("全部 BUG_FIX 已 resolved", all(e.resolved for e in bug_fixes))

    unresolved = await memory.list_entries(
        tool_name="get_weather", unresolved_only=True,
    )
    check("无未解决条目残留", len(unresolved) == 0)


# =========================================================================
# Test 9: 端到端 — report_tool_issue → 消息总线 → receive_message → 闭环
# =========================================================================

async def test_end_to_end_with_bus():
    print("\n=== Test 9: 端到端消息总线闭环 ===")

    from youmi.core.agent import Agent, AgentConfig
    from youmi.bus.broker import InProcessBroker

    server = await make_server()
    memory = GlobalMemory(db_path=":memory:")
    await memory.initialize()

    await memory.add_experience(
        tool_name="get_weather",
        content="get_weather 失败: 城市 '深圳' 不在支持列表中",
        category=KnowledgeCategory.TOOL_EXPERIENCE,
    )

    broker = InProcessBroker()
    workflow_id = await broker.create_workflow()

    guardian = await make_guardian(server, global_memory=memory)
    await broker.subscribe(guardian.agent_id, workflow_id)

    # Worker Agent 汇报
    worker = Agent(AgentConfig(name="Worker", system_prompt="test", max_iterations=5))
    await broker.subscribe(worker.agent_id, workflow_id)
    worker.connect_guardian(guardian.agent_id, broker, workflow_id)
    await worker.report_tool_issue(
        tool_name="get_weather",
        error_message="城市 '深圳' 不在支持列表中",
        call_arguments={"city": "深圳"},
    )

    # Guardian 从总线接收消息
    import asyncio
    await asyncio.sleep(0.1)  # 等待消息投递
    messages = await broker.pending_messages(guardian.agent_id)
    check("Guardian 收到 1 条消息", len(messages) == 1)
    for msg in messages:
        await guardian.receive_message(msg.to_agent_message())
    check("Guardian 收到汇报", guardian.pending_count == 1)

    # 处理并验证闭环
    results = await guardian.process_reports()
    check("处理成功", len(results) == 1)
    check("resolved_count == 1", results[0]["memory_updated"]["resolved_count"] == 1)

    unresolved = await memory.list_entries(
        tool_name="get_weather", unresolved_only=True,
    )
    check("总线闭环后无未解决条目", len(unresolved) == 0)
