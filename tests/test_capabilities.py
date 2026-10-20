"""sidecar 能力层测试

覆盖三件事：
1. ToolDefinition.from_function 对字符串注解（PEP 563）的类型解析；
2. YouMiCapabilityProvider 注册的 youmi_* 工具契约；
3. SidecarRuntime 的 JSON-RPC 分发（initialize / tools/list / tools/call）。
"""

from __future__ import annotations

import json

from youmi.core.tool import ToolDefinition
from youmi.mcp.capabilities import YouMiCapabilityProvider
from youmi.mcp.stdio_server import PROTOCOL_VERSION, SERVER_NAME, SidecarRuntime
from youmi.mcp.protocol import ToolContext
from youmi.mcp.tool_store import ToolStore


# ---------------------------------------------------------------------------
# 1. 注解解析
# ---------------------------------------------------------------------------

def test_from_function_resolves_string_annotations():
    """本模块启用了 PEP 563，注解是字符串，仍应还原成正确 JSON 类型"""

    async def search(query: str, top_k: int = 5, strict: bool = False) -> str:
        return query

    definition = ToolDefinition.from_function(search)
    types = {p.name: p.type for p in definition.parameters}

    assert types == {"query": "string", "top_k": "integer", "strict": "boolean"}
    assert [p.name for p in definition.parameters if p.required] == ["query"]


def test_from_function_resolves_optional_annotations():
    async def fetch(url: str, limit: int | None = None) -> str:
        return url

    definition = ToolDefinition.from_function(fetch)
    types = {p.name: p.type for p in definition.parameters}

    assert types["limit"] == "string"  # 联合类型无法映射时安全退化为 string
    assert types["url"] == "string"


# ---------------------------------------------------------------------------
# 2. 能力 Provider
# ---------------------------------------------------------------------------

async def test_capability_provider_tool_contract():
    store = ToolStore(db_path=":memory:")
    await store.initialize()
    try:
        provider = YouMiCapabilityProvider(tool_store=store)
        tools = await provider.get_tools()
        by_name = {tool.name: tool for tool in tools}

        assert len(tools) == 10
        assert set(by_name) == {
            "youmi_tool_search",
            "youmi_tool_get",
            "youmi_tool_resolve",
            "youmi_skill_index",
            "youmi_skill_list",
            "youmi_skill_get",
            "youmi_plan_search",
            "youmi_plan_save",
            "youmi_memory_search",
            "youmi_memory_add",
        }

        search_schema = by_name["youmi_tool_search"].input_schema
        assert search_schema["required"] == ["query"]
        assert search_schema["properties"]["top_k"]["type"] == "integer"
    finally:
        await store.close()


async def test_capability_provider_degrades_without_stores():
    """未注入组件时返回结构化错误 JSON，而不是抛异常"""
    provider = YouMiCapabilityProvider()
    tools = await provider.get_tools()
    assert len(tools) == 10

    result = await provider.execute(
        "youmi_tool_search", {"query": "读取文件"}, ToolContext(),
    )
    text = result.content[0]["text"]
    assert json.loads(text) == {"error": "tool_store 未启用"}


# ---------------------------------------------------------------------------
# 3. JSON-RPC 分发
# ---------------------------------------------------------------------------

async def test_sidecar_runtime_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUMI_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("YOUMI_EMBEDDING_BASE_URL", raising=False)

    runtime = SidecarRuntime()
    await runtime.initialize()
    try:
        init = await runtime.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
        })
        assert init is not None
        assert init["result"]["protocolVersion"] == PROTOCOL_VERSION
        assert init["result"]["serverInfo"]["name"] == SERVER_NAME

        listed = await runtime.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert listed is not None
        assert len(listed["result"]["tools"]) == 10

        added = await runtime.handle({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "youmi_memory_add",
                "arguments": {
                    "task": "读取配置文件",
                    "failure": "路径不存在",
                    "fix": "先检查绝对路径",
                    "tags": "fs,path",
                    "tool_name": "file_read",
                },
            },
        })
        assert added is not None
        assert added["result"]["isError"] is False

        found = await runtime.handle({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "youmi_memory_search",
                "arguments": {"query": "路径", "top_k": 5},
            },
        })
        assert found is not None
        assert found["result"]["isError"] is False
        payload = json.loads(found["result"]["content"][0]["text"])
        assert payload and payload[0]["tool_name"] == "file_read"

        missing = await runtime.handle({"jsonrpc": "2.0", "id": 5, "method": "nope"})
        assert missing is not None
        assert missing["error"]["code"] == -32601

        notification = await runtime.handle({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        })
        assert notification is None
    finally:
        await runtime.close()
