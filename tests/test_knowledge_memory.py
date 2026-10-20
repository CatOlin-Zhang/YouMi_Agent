"""GlobalMemory / ToolExperienceExtractor 测试（sidecar 版）

宿主侧的 PostTaskPipeline / MemoryManager 已随 DSH 迁移移除，
本文件只覆盖 sidecar 的失败经验闭环。
"""

import json

from youmi.knowledge import (
    GlobalMemory,
    KnowledgeCategory,
    KnowledgeEntry,
    ToolExperienceExtractor,
    ToolKnowledge,
)


class MockEmbeddingClient:
    """字符哈希伪向量 — 足以验证向量检索路径"""

    def __init__(self, dim: int = 32) -> None:
        self._dim = dim

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for i, ch in enumerate(text):
            vec[i % self._dim] += ord(ch) % 97
        norm = sum(v * v for v in vec) ** 0.5
        return [v / norm for v in vec] if norm else vec

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]

    async def embed_one(self, text: str) -> list[float]:
        return self._embed(text)

    async def similarity(
        self, query_vec: list[float], candidates: list[list[float]],
    ) -> list[float]:
        return [sum(a * b for a, b in zip(query_vec, c)) for c in candidates]


def test_models_defaults():
    entry = KnowledgeEntry(tool_name="file_read", content="test")
    assert entry.category == KnowledgeCategory.TOOL_EXPERIENCE
    assert len(entry.entry_id) == 16
    assert entry.resolved is False
    assert entry.is_bug is True

    entry.resolved = True
    assert entry.is_bug is False

    knowledge = ToolKnowledge(tool_name="file_read")
    assert knowledge.is_empty is True
    knowledge.known_issues.append("路径必须为绝对路径")
    assert knowledge.is_empty is False


async def test_global_memory_crud_and_search():
    memory = GlobalMemory(db_path=":memory:")
    await memory.initialize()
    try:
        first = await memory.add_experience(
            tool_name="file_read", content="读取文件时必须使用绝对路径",
        )
        await memory.add_experience(
            tool_name="file_read", content="文件不存在时先检查路径是否存在",
        )
        await memory.add_experience(
            tool_name="shell_exec", content="shell 命令中的空格需要转义",
        )

        hits = await memory.search("路径")
        assert hits
        assert all(e.tool_name == "file_read" for e in hits)

        entries = await memory.list_entries(tool_name="file_read")
        assert len(entries) == 2

        resolved = await memory.mark_resolved(first.entry_id, "统一改为绝对路径")
        assert resolved is not None and resolved.resolved
        assert resolved.resolution == "统一改为绝对路径"

        knowledge = await memory.get_tool_knowledge("file_read")
        assert len(knowledge.entry_ids) == 2
        assert knowledge.best_practices or knowledge.known_issues
    finally:
        await memory.close()


async def test_global_memory_vector_search():
    memory = GlobalMemory(
        db_path=":memory:",
        embedding_client=MockEmbeddingClient(),
        embedding_dim=32,
    )
    await memory.initialize()
    try:
        await memory.add_experience(
            tool_name="file_read", content="绝对路径读取文件",
        )
        await memory.add_experience(
            tool_name="file_read", content="网络请求超时重试",
        )
        hits = await memory.search("绝对路径读取文件", top_k=2)
        assert hits
        assert any("路径" in e.content for e in hits)
    finally:
        await memory.close()


def _conversation(tool_name: str, results: list[str]) -> list[dict]:
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


def test_experience_extractor_rules():
    extractor = ToolExperienceExtractor()
    conversation = _conversation("file_read", [
        json.dumps({"content": "ok"}),
        json.dumps({"error": "文件不存在: /tmp/x.txt"}),
    ])

    experience = extractor.extract(conversation, "file_read")
    assert experience.tool_name == "file_read"
    assert experience.usage_count == 2
    assert experience.success_rate == 0.5
    assert experience.failure_patterns


async def test_experience_extractor_llm_and_fallback():
    async def mock_llm(system_prompt: str, user_prompt: str) -> str:
        return "[根因] 路径解析缺陷 | [建议] 强制绝对路径"

    extractor = ToolExperienceExtractor(llm_call=mock_llm)
    insights = await extractor.analyze_failures(["错误"], "file_read")
    assert insights and "[根因]" in insights[0]

    async def bad_llm(system_prompt: str, user_prompt: str) -> str:
        raise RuntimeError("LLM unavailable")

    fallback = ToolExperienceExtractor(llm_call=bad_llm)
    insights = await fallback.analyze_failures(["文件不存在"], "file_read")
    assert insights


def test_top_level_exports():
    import youmi

    assert "GlobalMemory" in youmi.__all__
    assert "ToolExperienceExtractor" in youmi.__all__
    assert youmi.__version__ == "0.2.0"
