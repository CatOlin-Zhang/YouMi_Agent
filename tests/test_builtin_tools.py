"""内置工具单元测试

验证:
1. 文件操作工具 (file_search / file_read / file_write / list_directory / text_search)
2. Shell 操作工具 (shell_exec + 安全策略)
3. 数据工具 (get_datetime / json_tool)
4. BuiltinToolProvider 注册与执行
5. Agent 集成 (register_builtin_tools / connect_mcp 自动注册)
6. 沙箱隔离 (路径越权拒绝)
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

from youmi.core.agent import Agent, AgentConfig, AgentStatus
from youmi.core.types import LLMConfig, LLMProvider, MemoryConfig
from youmi.core.tool import ToolRegistry
from youmi.tools.file_ops import (
    file_search,
    file_read,
    file_write,
    list_directory,
    text_search,
)
from youmi.tools.shell_ops import shell_exec
from youmi.tools.data_ops import get_datetime, json_tool
from youmi.tools.builtin import BuiltinToolProvider


def make_config(name: str = "TestAgent") -> AgentConfig:
    return AgentConfig(
        name=name,
        llm_config=LLMConfig(provider=LLMProvider.LOCAL, model="test", api_key=""),
        memory_config=MemoryConfig(strategy="full"),
    )


# ===========================================================================
# 1. 文件操作工具
# ===========================================================================

class TestFileOps:
    """文件操作工具测试"""

    async def test_file_write_create(self, tmp_path):
        """创建文件"""
        result = await file_write("test.txt", "hello world", str(tmp_path), mode="create")
        assert "成功写入" in result
        assert (tmp_path / "test.txt").read_text() == "hello world"

    async def test_file_write_create_exists_error(self, tmp_path):
        """create 模式文件已存在时报错"""
        (tmp_path / "existing.txt").write_text("data")
        result = await file_write("existing.txt", "new data", str(tmp_path), mode="create")
        assert "错误" in result

    async def test_file_write_overwrite(self, tmp_path):
        """覆盖写入"""
        (tmp_path / "f.txt").write_text("old")
        result = await file_write("f.txt", "new", str(tmp_path), mode="overwrite")
        assert "成功写入" in result
        assert (tmp_path / "f.txt").read_text() == "new"

    async def test_file_write_append(self, tmp_path):
        """追加写入"""
        (tmp_path / "f.txt").write_text("hello")
        result = await file_write("f.txt", " world", str(tmp_path), mode="append")
        assert "追加" in result
        assert (tmp_path / "f.txt").read_text() == "hello world"

    async def test_file_read(self, tmp_path):
        """读取文件"""
        (tmp_path / "read_me.txt").write_text("line1\nline2\nline3")
        result = await file_read("read_me.txt", str(tmp_path))
        assert "line1" in result
        assert "line3" in result

    async def test_file_read_range(self, tmp_path):
        """读取指定行范围"""
        (tmp_path / "f.txt").write_text("a\nb\nc\nd\ne")
        result = await file_read("f.txt", str(tmp_path), max_lines=2, start_line=2)
        assert "b" in result
        assert "c" in result
        assert "e" not in result

    async def test_file_read_not_found(self, tmp_path):
        """读取不存在的文件"""
        result = await file_read("no_such_file.txt", str(tmp_path))
        assert "错误" in result
        assert "不存在" in result

    async def test_file_search(self, tmp_path):
        """搜索文件"""
        (tmp_path / "a.py").write_text("")
        (tmp_path / "b.py").write_text("")
        (tmp_path / "c.txt").write_text("")
        result = await file_search("*.py", str(tmp_path))
        assert "a.py" in result
        assert "b.py" in result
        assert "c.txt" not in result

    async def test_file_search_recursive(self, tmp_path):
        """递归搜索"""
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.py").write_text("")
        result = await file_search("*.py", str(tmp_path), recursive=True)
        assert "deep.py" in result

    async def test_list_directory(self, tmp_path):
        """列出目录"""
        (tmp_path / "file1.py").write_text("")
        (tmp_path / "file2.txt").write_text("")
        (tmp_path / "subdir").mkdir()
        result = await list_directory(".", str(tmp_path))
        assert "file1.py" in result
        assert "subdir" in result

    async def test_list_directory_hidden(self, tmp_path):
        """隐藏文件控制"""
        (tmp_path / ".hidden").write_text("")
        (tmp_path / "visible.txt").write_text("")
        result_no_hidden = await list_directory(".", str(tmp_path), show_hidden=False)
        assert ".hidden" not in result_no_hidden
        result_hidden = await list_directory(".", str(tmp_path), show_hidden=True)
        assert ".hidden" in result_hidden

    async def test_list_directory_detail(self, tmp_path):
        """详细模式"""
        (tmp_path / "big.txt").write_text("x" * 1000)
        result = await list_directory(".", str(tmp_path), detail=True)
        assert "bytes" in result

    async def test_text_search(self, tmp_path):
        """文本搜索"""
        (tmp_path / "code.py").write_text("def hello():\n    print('world')\n")
        (tmp_path / "readme.md").write_text("# Hello\nThis is a readme\n")
        result = await text_search("hello", str(tmp_path), case_sensitive=False)
        assert "code.py" in result
        assert "readme.md" in result

    async def test_text_search_case_sensitive(self, tmp_path):
        """区分大小写搜索"""
        (tmp_path / "f.txt").write_text("Hello\nhello\nHELLO")
        result = await text_search("hello", str(tmp_path), case_sensitive=True)
        assert "hello" in result
        # 不应匹配 Hello 和 HELLO（但行内容展示可能包含，检查匹配数）

    async def test_text_search_file_pattern(self, tmp_path):
        """限定文件类型搜索"""
        (tmp_path / "a.py").write_text("import os")
        (tmp_path / "b.txt").write_text("import os")
        result = await text_search("import", str(tmp_path), file_pattern="*.py")
        assert "a.py" in result
        assert "b.txt" not in result

    async def test_sandbox_isolation(self, tmp_path):
        """沙箱隔离 — 路径越权被拒绝"""
        # 尝试读取沙箱外的文件
        outside_path = str(Path(tmp_path).parent / "outside.txt")
        with pytest.raises(PermissionError, match="超出沙箱目录"):
            await file_read(outside_path, str(tmp_path))

    async def test_sandbox_write_isolation(self, tmp_path):
        """沙箱隔离 — 写入越权被拒绝"""
        outside = str(Path(tmp_path).parent / "hack.txt")
        with pytest.raises(PermissionError, match="超出沙箱目录"):
            await file_write(outside, "hacked", str(tmp_path))


# ===========================================================================
# 2. Shell 操作工具
# ===========================================================================

class TestShellOps:
    """Shell 操作工具测试"""

    async def test_basic_command(self, tmp_path):
        """基本命令执行"""
        result = await shell_exec("echo hello", str(tmp_path))
        assert "hello" in result
        assert "[exit_code: 0]" in result

    async def test_command_in_workdir(self, tmp_path):
        """命令在指定目录执行"""
        (tmp_path / "marker.txt").write_text("found")
        result = await shell_exec("dir" if os.name == "nt" else "ls", str(tmp_path))
        assert "marker.txt" in result

    async def test_dangerous_command_blocked(self, tmp_path):
        """危险命令被拦截"""
        result = await shell_exec("rm -rf /", str(tmp_path))
        assert "安全策略" in result
        assert "拦截" in result

    async def test_command_timeout(self, tmp_path):
        """命令超时"""
        python_exe = sys.executable
        cmd = "sleep 10" if os.name != "nt" else f'"{python_exe}" -c "import time; time.sleep(10)"'
        result = await shell_exec(cmd, str(tmp_path), timeout=1)
        assert "超时" in result or "终止" in result

    async def test_command_stderr(self, tmp_path):
        """stderr 输出捕获"""
        cmd = "echo error >&2" if os.name != "nt" else "echo error 1>&2"
        result = await shell_exec(cmd, str(tmp_path))
        assert "error" in result

    async def test_command_no_output(self, tmp_path):
        """无输出命令"""
        cmd = "true" if os.name != "nt" else "rem"
        result = await shell_exec(cmd, str(tmp_path))
        assert "exit_code: 0" in result


# ===========================================================================
# 3. 数据工具
# ===========================================================================

class TestDataOps:
    """数据处理工具测试"""

    async def test_get_datetime_default(self):
        """获取默认时区时间"""
        result = await get_datetime()
        assert "当前时间" in result
        assert "Unix" in result
        assert "星期" in result

    async def test_get_datetime_custom_tz(self):
        """自定义时区"""
        result = await get_datetime(timezone_offset=0)
        assert "UTC+0" in result or "UTC0" in result

    async def test_get_datetime_format(self):
        """自定义格式"""
        result = await get_datetime(format="%Y")
        assert "20" in result  # 年份以 20 开头

    async def test_json_tool_format(self):
        """JSON 格式化"""
        result = await json_tool('{"a":1,"b":2}', action="format")
        parsed = json.loads(result)
        assert parsed["a"] == 1

    async def test_json_tool_validate_ok(self):
        """JSON 校验通过"""
        result = await json_tool('[1,2,3]', action="validate")
        assert "校验通过" in result
        assert "3 个元素" in result

    async def test_json_tool_validate_fail(self):
        """JSON 校验失败"""
        result = await json_tool('{bad json}', action="validate")
        assert "校验失败" in result

    async def test_json_tool_minify(self):
        """JSON 压缩"""
        result = await json_tool('{"a": 1, "b": 2}', action="minify")
        assert " " not in result  # 无空格
        assert result == '{"a":1,"b":2}'


# ===========================================================================
# 4. BuiltinToolProvider
# ===========================================================================

class TestBuiltinToolProvider:
    """内置工具 Provider 测试"""

    def test_provider_creation(self):
        """Provider 创建并注册所有工具"""
        bp = BuiltinToolProvider(work_dir=".", provider_id="test")
        assert bp.provider_id == "test"
        assert len(bp.tool_names) == 9  # 9 个内置工具
        assert "file_search" in bp.tool_names
        assert "file_read" in bp.tool_names
        assert "file_write" in bp.tool_names
        assert "list_directory" in bp.tool_names
        assert "text_search" in bp.tool_names
        assert "shell_exec" in bp.tool_names
        assert "web_fetch" in bp.tool_names
        assert "get_datetime" in bp.tool_names
        assert "json_tool" in bp.tool_names

    def test_provider_exclude(self):
        """排除指定工具"""
        bp = BuiltinToolProvider(
            work_dir=".", exclude=["shell_exec", "web_fetch"],
        )
        assert "shell_exec" not in bp.tool_names
        assert "web_fetch" not in bp.tool_names
        assert "file_read" in bp.tool_names

    async def test_provider_get_tools(self):
        """get_tools 返回 MCP 工具信息"""
        bp = BuiltinToolProvider(work_dir=".")
        tools = await bp.get_tools()
        assert len(tools) == 9
        names = [t.name for t in tools]
        assert "file_read" in names

    async def test_provider_execute(self, tmp_path):
        """通过 Provider 执行工具"""
        from youmi.mcp.protocol import ToolContext

        bp = BuiltinToolProvider(work_dir=str(tmp_path))
        ctx = ToolContext(agent_id="test", session_id="s1")

        # 写入
        result = await bp.execute("file_write", {
            "path": "test.txt", "content": "hello", "mode": "overwrite",
        }, ctx)
        assert not result.is_error

        # 读取
        result = await bp.execute("file_read", {"path": "test.txt"}, ctx)
        assert not result.is_error
        assert "hello" in result.text

    async def test_provider_execute_shell(self, tmp_path):
        """通过 Provider 执行 shell"""
        from youmi.mcp.protocol import ToolContext

        bp = BuiltinToolProvider(work_dir=str(tmp_path))
        ctx = ToolContext(agent_id="test", session_id="s1")

        result = await bp.execute("shell_exec", {"command": "echo ok"}, ctx)
        assert not result.is_error
        assert "ok" in result.text

    async def test_openai_schema(self):
        """工具定义可生成 OpenAI tools schema"""
        bp = BuiltinToolProvider(work_dir=".")
        tools = await bp.get_tools()
        for tool in tools:
            assert tool.name
            assert tool.description
            assert tool.input_schema


# ===========================================================================
# 5. Agent 集成
# ===========================================================================

class TestAgentBuiltinTools:
    """Agent 集成内置工具测试"""

    async def test_register_builtin_tools(self):
        """Agent 手动注册内置工具"""
        agent = Agent(make_config())
        await agent.initialize()

        assert len(agent.tool_registry) == 0
        agent.register_builtin_tools()
        assert len(agent.tool_registry) == 10  # 9 内置 + search_new_tools
        assert "file_read" in agent.tool_registry
        assert "shell_exec" in agent.tool_registry

    async def test_register_builtin_with_exclude(self):
        """Agent 注册时排除部分工具"""
        agent = Agent(make_config())
        await agent.initialize()

        agent.register_builtin_tools(exclude=["shell_exec", "web_fetch"])
        assert "shell_exec" not in agent.tool_registry
        assert "web_fetch" not in agent.tool_registry
        assert "file_read" in agent.tool_registry

    async def test_connect_mcp_auto_registers_builtin(self):
        """connect_mcp 自动注册内置工具"""
        from youmi.mcp.server import MCPServer

        agent = Agent(make_config())
        server = MCPServer()
        agent.connect_mcp(server, builtin_tools=True)

        # 内置工具应自动注册到 ToolRegistry
        assert "file_read" in agent.tool_registry
        assert "file_write" in agent.tool_registry
        assert "shell_exec" in agent.tool_registry

    async def test_connect_mcp_no_builtin(self):
        """connect_mcp 可禁用自动注册"""
        from youmi.mcp.server import MCPServer

        agent = Agent(make_config())
        server = MCPServer()
        agent.connect_mcp(server, builtin_tools=False)

        # 不应注册内置工具
        assert "file_read" not in agent.tool_registry
        assert "file_write" not in agent.tool_registry

    async def test_builtin_tool_execution_via_agent(self, tmp_path):
        """通过 Agent ToolRegistry 执行内置工具"""
        config = AgentConfig(
            name="ToolAgent",
            llm_config=LLMConfig(provider=LLMProvider.LOCAL, model="test", api_key=""),
            memory_config=MemoryConfig(strategy="full"),
            env=str(tmp_path),
        )
        agent = Agent(config)
        await agent.initialize()
        agent.register_builtin_tools()

        # 通过 ToolRegistry 执行
        result = await agent.tool_registry.execute("json_tool", {
            "input_text": '{"key": "value"}',
            "action": "validate",
        })
        assert "校验通过" in result
