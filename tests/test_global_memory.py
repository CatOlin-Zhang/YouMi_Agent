"""全局记忆模块测试 (Phase 6)

测试覆盖:
1. KnowledgeEntry / KnowledgeCategory / ToolKnowledge 数据模型
2. GlobalMemory — CRUD / 关键词检索 / 向量检索 / 修复闭环 / 聚合查询
3. ToolExperienceExtractor — 规则提取 / 失败分析 (规则 + LLM)
4. PostTaskPipeline — 集成全局记忆 (第4阶段) / 工具版本自动触发
5. MemoryManager.search — 关键词检索 / 向量检索
6. 优雅降级 — 无 embedding_client / 无 global_memory
"""

import asyncio
import json

from youmi.knowledge import (
    GlobalMemory,
    KnowledgeCategory,
    KnowledgeEntry,
    ToolKnowledge,
    ToolExperienceExtractor,
)
from youmi.memory import MemoryManager
from youmi.coordinator.post_task import (
    PostTaskPipeline,
    ToolExperience,
)


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


class MockEmbeddingClient:
    """Mock Embedding 客户端 — 基于字符哈希的伪向量

    同一文本生成相同向量，相似文本 (共享前缀) 向量相近，
    足以验证向量检索路径的正确性。
    """

    def __init__(self, dim: int = 32) -> None:
        self._dim = dim

    def _embed_text(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for i, ch in enumerate(text):
            vec[i % self._dim] += ord(ch) % 97
        # 归一化
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_text(t) for t in texts]

    async def embed_one(self, text: str) -> list[float]:
        return self._embed_text(text)

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    async def similarity(
        self, query_vec: list[float], candidates: list[list[float]],
    ) -> list[float]:
        return [self.cosine_similarity(query_vec, c) for c in candidates]


class MockLatestEntry:
    """Mock ToolStore.get_latest_version 返回的条目"""

    def __init__(self) -> None:
        self.definition = "mock_tool_definition"


class MockToolStore:
    """Mock ToolStore — 记录版本更新调用"""

    def __init__(self) -> None:
        self.version_calls: list[dict] = []

    async def get_latest_version(self, tool_name: str):
        return MockLatestEntry()

    async def create_version(
        self, tool_name: str, new_definition, changelog: str, bump: str = "patch",
    ) -> str:
        new_id = f"{tool_name}@0.0.2"
        self.version_calls.append({
            "tool_name": tool_name,
            "changelog": changelog,
            "bump": bump,
            "new_tool_id": new_id,
        })
        return new_id


class MockAgent:
    """Mock 子 Agent — 持有对话记录"""

    def __init__(self, name: str, conversation: list[dict]) -> None:
        self.name = name
        self.agent_id = f"agent_{name}"
        self._conversation = conversation


class MockRecord:
    """Mock SubAgentRecord"""

    def __init__(self, agent: MockAgent, role: str = "worker") -> None:
        self.agent = agent
        self.role = role
        self.task = "test"


class MockMemory:
    """Mock MasterAgent.memory"""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def on_message(self, role: str, content: str, **kwargs) -> None:
        self.messages.append((role, content))


class MockMaster:
    """Mock MasterAgent — 供 PostTaskPipeline 使用"""

    def __init__(self, agents: list[MockAgent]) -> None:
        self._records = {a.agent_id: MockRecord(a) for a in agents}
        self.memory = MockMemory()
        self._workflow_id = "wf_test_001"

    def get_sub_agents(self) -> dict:
        return self._records


def make_conversation(tool_name: str, results: list[str]) -> list[dict]:
    """构造带工具调用的对话记录"""
    conversation = [{"role": "user", "content": "执行任务"}]
    for i, result in enumerate(results):
        call_id = f"call_{i}"
        conversation.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": tool_name, "arguments": "{}"},
            }],
        })
        conversation.append({
            "role": "tool",
            "content": result,
            "tool_call_id": call_id,
        })
    return conversation


# =========================================================================
# 测试1: 数据模型
# =========================================================================

async def test_models():
    print("\n=== Test 1: Knowledge Models ===")

    # KnowledgeEntry 默认值
    entry = KnowledgeEntry(tool_name="file_read", content="test")
    check("默认category", entry.category == KnowledgeCategory.TOOL_EXPERIENCE)
    check("entry_id自动生成", len(entry.entry_id) == 16)
    check("默认未解决", entry.resolved is False)
    check("默认无向量", entry.embedding is None)

    # is_bug
    check("未解决为bug", entry.is_bug is True)
    entry.resolved = True
    check("已解决非bug", entry.is_bug is False)

    # ToolKnowledge
    tk = ToolKnowledge(tool_name="file_read")
    check("空知识", tk.is_empty is True)
    tk.known_issues.append("路径必须为绝对路径")
    check("非空知识", tk.is_empty is False)


# =========================================================================
# 测试2: GlobalMemory CRUD
# =========================================================================

async def test_global_memory_crud():
    print("\n=== Test 2: GlobalMemory CRUD ===")

    memory = GlobalMemory(db_path=":memory:")
    await memory.initialize()

    # add_experience
    entry = await memory.add_experience(
        tool_name="file_read",
        content="路径参数必须使用绝对路径",
        category=KnowledgeCategory.TOOL_EXPERIENCE,
        source_task_id="task_001",
        success_rate=0.3,
        metadata={"usage_count": 10},
    )
    check("写入返回entry", entry.entry_id != "")
    check("无embedding时向量为空", entry.embedding is None)

    # get_entry
    loaded = await memory.get_entry(entry.entry_id)
    check("按ID读取", loaded is not None)
    check("内容一致", loaded.content == "路径参数必须使用绝对路径")
    check("工具名一致", loaded.tool_name == "file_read")
    check("metadata保留", loaded.metadata.get("usage_count") == 10)

    # 不存在的 entry
    none_entry = await memory.get_entry("nonexistent")
    check("不存在返回None", none_entry is None)

    # list_entries
    await memory.add_experience(
        tool_name="shell_exec",
        content="timeout 参数需要大于 0",
        source_task_id="task_001",
    )
    all_entries = await memory.list_entries()
    check("列出全部", len(all_entries) == 2)

    by_tool = await memory.list_entries(tool_name="file_read")
    check("按工具过滤", len(by_tool) == 1 and by_tool[0].tool_name == "file_read")

    by_category = await memory.list_entries(
        category=KnowledgeCategory.BUG_FIX,
    )
    check("按类别过滤为空", len(by_category) == 0)

    # delete_entry
    deleted = await memory.delete_entry(entry.entry_id)
    check("删除成功", deleted is True)
    after = await memory.get_entry(entry.entry_id)
    check("删除后读取None", after is None)

    deleted_again = await memory.delete_entry(entry.entry_id)
    check("重复删除返回False", deleted_again is False)

    await memory.close()


# =========================================================================
# 测试3: GlobalMemory 关键词检索 (降级)
# =========================================================================

async def test_global_memory_keyword_search():
    print("\n=== Test 3: GlobalMemory Keyword Search ===")

    memory = GlobalMemory(db_path=":memory:")  # 无 embedding_client
    await memory.initialize()

    await memory.add_experience(
        tool_name="file_read", content="路径参数必须使用绝对路径",
    )
    await memory.add_experience(
        tool_name="shell_exec", content="超时 timeout 需要设置足够大",
    )
    await memory.add_experience(
        tool_name="web_fetch", content="URL 必须包含 http 前缀",
    )

    # 关键词命中
    results = await memory.search("路径")
    check("关键词命中", len(results) >= 1)
    check("命中正确工具", results[0].tool_name == "file_read")

    # 按工具限定
    results2 = await memory.search("timeout", tool_name="shell_exec")
    check("限定工具检索", len(results2) == 1)

    # 无命中
    results3 = await memory.search("完全不相关词汇xyz")
    check("无命中返回空", len(results3) == 0)

    # 空查询
    results4 = await memory.search("  ")
    check("空查询返回空", len(results4) == 0)

    await memory.close()


# =========================================================================
# 测试4: GlobalMemory 向量检索
# =========================================================================

async def test_global_memory_vector_search():
    print("\n=== Test 4: GlobalMemory Vector Search ===")

    embedder = MockEmbeddingClient()
    memory = GlobalMemory(db_path=":memory:", embedding_client=embedder)
    await memory.initialize()

    e1 = await memory.add_experience(
        tool_name="file_read", content="文件路径必须使用绝对路径",
    )
    check("自动向量化", e1.embedding is not None and len(e1.embedding) == 32)

    await memory.add_experience(
        tool_name="shell_exec", content="shell 命令执行超时问题",
    )

    # 向量检索 (查询与已存内容一致 → 完全命中)
    results = await memory.search("文件路径必须使用绝对路径")
    check("向量检索有结果", len(results) >= 1)
    check("命中正确条目", results[0].entry_id == e1.entry_id)

    # batch_add 批量向量化
    entries = [
        KnowledgeEntry(tool_name="t1", content="批量向量化的内容一"),
        KnowledgeEntry(tool_name="t2", content="批量向量化的内容二"),
    ]
    ids = await memory.batch_add(entries)
    check("批量写入返回ID", len(ids) == 2)
    check("批量向量化", entries[0].embedding is not None)

    # batch_add 空列表
    empty_ids = await memory.batch_add([])
    check("空批量返回空", empty_ids == [])

    await memory.close()


# =========================================================================
# 测试5: 修复闭环 (mark_resolved + get_tool_knowledge)
# =========================================================================

async def test_mark_resolved_and_aggregation():
    print("\n=== Test 5: Mark Resolved + ToolKnowledge ===")

    memory = GlobalMemory(db_path=":memory:")
    await memory.initialize()

    # 未解决的失败经验
    bug1 = await memory.add_experience(
        tool_name="file_read",
        content="相对路径在不同 cwd 下失败",
        success_rate=0.2,
    )
    # 已解决的失败经验
    bug2 = await memory.add_experience(
        tool_name="file_read",
        content="编码错误导致中文乱码",
        success_rate=0.5,
    )
    await memory.mark_resolved(bug2.entry_id, "v0.0.2 增加了 encoding 参数")
    # 成功经验
    await memory.add_experience(
        tool_name="file_read",
        content="使用绝对路径调用成功",
        success_rate=1.0,
    )
    # 修复记录
    await memory.add_experience(
        tool_name="file_read",
        content="v0.0.2: 修复路径解析",
        category=KnowledgeCategory.BUG_FIX,
    )
    # 其他工具的经验 (不应被聚合)
    await memory.add_experience(
        tool_name="shell_exec", content="超时问题", success_rate=0.4,
    )

    # mark_resolved 不存在的条目
    none_result = await memory.mark_resolved("nonexistent", "fix")
    check("不存在返回None", none_result is None)

    # 聚合查询
    knowledge = await memory.get_tool_knowledge("file_read")
    check("已知问题归类", len(knowledge.known_issues) == 1)
    check("相对路径在known_issues",
          "相对路径" in knowledge.known_issues[0])
    check("已解决归类", len(knowledge.resolved_issues) == 1)
    check("修复方案包含", "encoding" in knowledge.resolved_issues[0])
    check("成功经验归类", len(knowledge.best_practices) == 1)
    check("修复历史归类", len(knowledge.fix_history) == 1)
    check("entry_ids数量", len(knowledge.entry_ids) == 4)

    # 无记录的工具
    empty_knowledge = await memory.get_tool_knowledge("nonexistent_tool")
    check("无记录为空知识", empty_knowledge.is_empty is True)

    # list_entries 过滤 unresolved_only
    unresolved = await memory.list_entries(unresolved_only=True)
    unresolved_file = [e for e in unresolved if e.tool_name == "file_read"]
    check("未解决过滤", len(unresolved_file) == 3)  # bug1 + 成功经验 + BUG_FIX

    # stats
    stats = await memory.stats()
    check("统计总数", stats["total_entries"] == 5)
    check("统计已解决", stats["resolved_entries"] == 1)
    check("top_tools", "file_read" in stats["top_tools"])

    await memory.close()


# =========================================================================
# 测试6: ToolExperienceExtractor (规则模式)
# =========================================================================

async def test_experience_extractor_rules():
    print("\n=== Test 6: ToolExperienceExtractor (Rules) ===")

    extractor = ToolExperienceExtractor()
    check("无LLM", extractor.llm_enabled is False)

    # extract: 从对话提取
    conversation = make_conversation("file_read", [
        json.dumps({"content": "ok"}),                              # 成功
        json.dumps({"error": "文件不存在: /tmp/x.txt"}),            # 失败
        json.dumps({"error": "路径必须为绝对路径"}),                 # 失败
        "纯文本结果",                                                # 成功 (非JSON)
    ])
    exp = extractor.extract(conversation, "file_read")
    check("使用次数", exp.usage_count == 4)
    check("成功次数", exp.success_rate == 0.5)
    check("失败样本数", len(exp.failure_patterns) == 2)
    check("成功样本数", len(exp.success_patterns) == 2)

    # extract: 其他工具的调用不计入
    other_conversation = make_conversation("shell_exec", [
        json.dumps({"error": "失败"}),
    ])
    exp_other = extractor.extract(other_conversation, "file_read")
    check("其他工具不计入", exp_other.usage_count == 0)

    # analyze_failures: 规则模式
    insights = await extractor.analyze_failures(
        ["文件不存在: /tmp/x.txt", "路径必须为绝对路径"], "file_read",
    )
    check("规则分析有结果", len(insights) >= 1)
    check("包含工具名", all("file_read" in i for i in insights))

    # analyze_failures: 未分类错误
    insights2 = await extractor.analyze_failures(
        ["奇怪的未知错误xyz"], "file_read",
    )
    check("未分类错误有通用经验", len(insights2) == 1)
    check("通用经验含示例", "奇怪的未知错误xyz"[:20] in insights2[0])

    # analyze_failures: 空列表
    insights3 = await extractor.analyze_failures([], "file_read")
    check("空失败列表返回空", insights3 == [])

    # to_experience_content
    content = ToolExperienceExtractor.to_experience_content(exp)
    check("经验文本含统计", "4 次" in content)
    check("经验文本含成功率", "50%" in content)


# =========================================================================
# 测试7: ToolExperienceExtractor (LLM 模式)
# =========================================================================

async def test_experience_extractor_llm():
    print("\n=== Test 7: ToolExperienceExtractor (LLM) ===")

    async def mock_llm(system_prompt: str, user_prompt: str) -> str:
        return "[根因] 路径解析缺陷 | [建议] 强制绝对路径\n[根因] 编码缺失 | [建议] 显式指定utf-8"

    extractor = ToolExperienceExtractor(llm_call=mock_llm)
    check("LLM启用", extractor.llm_enabled is True)

    insights = await extractor.analyze_failures(
        ["错误1", "错误2"], "file_read",
    )
    check("LLM分析结果", len(insights) == 2)
    check("包含根因标记", "[根因]" in insights[0])

    # LLM 抛异常 → 降级到规则模式
    async def bad_llm(system_prompt: str, user_prompt: str) -> str:
        raise RuntimeError("LLM unavailable")

    extractor_bad = ToolExperienceExtractor(llm_call=bad_llm)
    insights_bad = await extractor_bad.analyze_failures(
        ["文件不存在: /tmp/x.txt"], "file_read",
    )
    check("LLM失败降级", len(insights_bad) >= 1)


# =========================================================================
# 测试8: PostTaskPipeline 集成全局记忆
# =========================================================================

async def test_pipeline_global_memory_integration():
    print("\n=== Test 8: PostTaskPipeline + GlobalMemory ===")

    memory = GlobalMemory(db_path=":memory:")
    await memory.initialize()

    # 构造带高失败率工具调用的对话
    conversation = make_conversation("file_read", [
        json.dumps({"content": "ok"}),
        json.dumps({"error": "文件不存在: /tmp/x.txt"}),
        json.dumps({"error": "权限不足 denied"}),
        json.dumps({"error": "路径错误"}),
    ])
    agent = MockAgent("worker1", conversation)
    master = MockMaster([agent])

    pipeline = PostTaskPipeline(global_memory=memory)
    summary = await pipeline.run(master, {})

    check("流水线完成", summary is not None)
    check("收集到经验", len(pipeline.experiences) == 1)
    check("经验正确", pipeline.experiences[0].tool_name == "file_read")

    # 全局记忆中应有条目
    entries = await memory.list_entries(tool_name="file_read")
    check("经验已沉淀", len(entries) >= 1)
    check("记录来源任务", entries[0].source_task_id == "wf_test_001")

    # 高失败率 (75%) 应触发语义分析条目
    contents = [e.content for e in entries]
    analysis_entries = [
        c for c in contents if "目标资源不存在" in c or "权限不足" in c
    ]
    check("失败根因经验已写入", len(analysis_entries) >= 1)

    await memory.close()


async def test_pipeline_version_update_trigger():
    print("\n=== Test 9: Pipeline Tool Version Update ===")

    memory = GlobalMemory(db_path=":memory:")
    await memory.initialize()
    tool_store = MockToolStore()

    # 失败 3 次 / 共 3 次 → 累计失败 >= 3 且成功率 < 0.5
    conversation = make_conversation("broken_tool", [
        json.dumps({"error": "失败1"}),
        json.dumps({"error": "失败2"}),
        json.dumps({"error": "失败3"}),
    ])
    agent = MockAgent("worker1", conversation)
    master = MockMaster([agent])

    pipeline = PostTaskPipeline(
        tool_store=tool_store, global_memory=memory,
    )
    await pipeline.run(master, {})

    check("版本更新被触发", len(tool_store.version_calls) == 1)
    call = tool_store.version_calls[0]
    check("触发工具正确", call["tool_name"] == "broken_tool")
    check("修复说明含失败次数", "3 次失败" in call["changelog"])

    # BUG_FIX 修复经验已记录
    fix_entries = await memory.list_entries(
        tool_name="broken_tool", category=KnowledgeCategory.BUG_FIX,
    )
    check("BUG_FIX经验已记录", len(fix_entries) == 1)

    # 触发后计数重置 → 再跑一次相同失败不再触发 (仅 3 次, 重置后需再累计)
    tool_store.version_calls.clear()
    master2 = MockMaster([
        MockAgent("worker2", make_conversation("broken_tool", [
            json.dumps({"error": "失败"}),
        ])),
    ])
    await pipeline.run(master2, {})
    check("计数重置后不重复触发", len(tool_store.version_calls) == 0)

    await memory.close()


async def test_pipeline_degradation():
    print("\n=== Test 10: Pipeline Degradation ===")

    # 无 global_memory → 第4阶段跳过, 不报错
    conversation = make_conversation("file_read", [
        json.dumps({"error": "失败"}),
    ])
    master = MockMaster([MockAgent("w1", conversation)])
    pipeline = PostTaskPipeline()  # 无 tool_store, 无 global_memory
    summary = await pipeline.run(master, {})
    check("无全局记忆正常完成", summary is not None)

    # global_memory 未初始化 → update_global_memory 内部报错被捕获
    from unittest.mock import MagicMock
    bad_memory = MagicMock()
    bad_memory.add_experience = MagicMock(
        side_effect=RuntimeError("not initialized"),
    )
    master2 = MockMaster([MockAgent("w2", conversation)])
    pipeline2 = PostTaskPipeline(global_memory=bad_memory)
    summary2 = await pipeline2.run(master2, {})
    check("全局记忆异常不阻塞流水线", summary2 is not None)


# =========================================================================
# 测试11: MemoryManager 检索
# =========================================================================

async def test_memory_manager_search():
    print("\n=== Test 11: MemoryManager Search ===")

    # 关键词检索 (full 策略)
    manager = MemoryManager(agent_id="a1", strategy="full")
    await manager.initialize()
    await manager.on_message("user", "帮我读取文件内容")
    await manager.on_message("assistant", "好的，使用 file_read 工具")
    await manager.on_message("user", "顺便执行 shell 命令")

    hits = await manager.search("文件")
    check("full关键词检索", len(hits) >= 1)
    check("命中正确内容", "文件" in hits[0]["content"])

    hits_empty = await manager.search("不存在的词xyz")
    check("无命中返回空", len(hits_empty) == 0)

    # 向量检索
    embedder = MockEmbeddingClient()
    vector_hits = await manager.search(
        "帮我读取文件内容", embedding_client=embedder,
    )
    check("向量检索有结果", len(vector_hits) >= 1)

    # 向量检索失败 → 降级
    class BadEmbedder:
        async def embed(self, texts):
            raise RuntimeError("embedding down")

        async def embed_one(self, text):
            raise RuntimeError("embedding down")

        async def similarity(self, q, candidates):
            return [0.0] * len(candidates)

    degraded_hits = await manager.search(
        "文件", embedding_client=BadEmbedder(),
    )
    check("向量失败降级关键词", len(degraded_hits) >= 1)

    # summary 策略检索
    manager2 = MemoryManager(agent_id="a2", strategy="summary")
    await manager2.initialize()
    await manager2.on_message("user", "记住这个偏好: 喜欢深色主题")
    hits2 = await manager2.search("偏好")
    check("summary策略检索", len(hits2) >= 1)

    # lstm 策略检索
    manager3 = MemoryManager(agent_id="a3", strategy="lstm")
    await manager3.initialize()
    await manager3.on_message("user", "请记住我喜欢简洁回复")
    hits3 = await manager3.search("记住")
    check("lstm策略检索", len(hits3) >= 1)


# =========================================================================
# 测试12: 顶层导出
# =========================================================================

async def test_top_level_exports():
    print("\n=== Test 12: Top-level Exports ===")

    import youmi
    check("GlobalMemory导出", hasattr(youmi, "GlobalMemory"))
    check("KnowledgeCategory导出", hasattr(youmi, "KnowledgeCategory"))
    check("KnowledgeEntry导出", hasattr(youmi, "KnowledgeEntry"))
    check("ToolKnowledge导出", hasattr(youmi, "ToolKnowledge"))
    check("ToolExperienceExtractor导出",
          hasattr(youmi, "ToolExperienceExtractor"))
    check("GlobalMemory在__all__", "GlobalMemory" in youmi.__all__)


# =========================================================================
# 主入口
# =========================================================================

async def main():
    test_models()
    await test_global_memory_crud()
    await test_global_memory_keyword_search()
    await test_global_memory_vector_search()
    await test_mark_resolved_and_aggregation()
    await test_experience_extractor_rules()
    await test_experience_extractor_llm()
    await test_pipeline_global_memory_integration()
    await test_pipeline_version_update_trigger()
    await test_pipeline_degradation()
    await test_memory_manager_search()
    await test_top_level_exports()
    print("\n" + "=" * 50)
    print("ALL TESTS PASSED")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
