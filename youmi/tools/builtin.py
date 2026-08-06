"""
BuiltinToolProvider — 内置工具提供者

继承 ToolProvider，将常用工具函数预注册为 MCP 工具。
Agent 实例化时可通过此 Provider 自动获得基础工具能力。

内置工具清单:
- file_search: 按 glob 模式搜索文件
- file_read: 读取文件内容
- file_write: 写入/创建文件
- list_directory: 列出目录内容
- text_search: 文件内容搜索（类 grep）
- shell_exec: 沙箱化命令执行
- web_fetch: 网页内容抓取
- get_datetime: 获取当前日期时间
- json_tool: JSON 解析/格式化/校验

用法::

    from youmi.tools.builtin import BuiltinToolProvider

    provider = BuiltinToolProvider(work_dir="/path/to/workspace")
    await provider.initialize()
    tools = await provider.get_tools()
"""

from __future__ import annotations

import logging
from typing import Any

from youmi.core.tool import ToolDefinition, ToolParameter
from youmi.mcp.provider import LocalFunctionProvider
from youmi.mcp.protocol import MCPToolInfo, MCPToolResult, ToolContext
from youmi.tools.file_ops import (
    file_search,
    file_read,
    file_write,
    list_directory,
    text_search,
)
from youmi.tools.shell_ops import shell_exec
from youmi.tools.web_ops import web_fetch
from youmi.tools.data_ops import get_datetime, json_tool

logger = logging.getLogger(__name__)


class BuiltinToolProvider(LocalFunctionProvider):
    """内置工具提供者

    继承 LocalFunctionProvider，在初始化时自动注册所有内置工具。
    所有文件操作工具自动绑定到指定的 work_dir（沙箱目录）。

    Args:
        work_dir: 工具操作的沙箱根目录，所有文件操作限定在此目录内
        provider_id: Provider 标识，默认 "builtin"
        exclude: 要排除的工具名称列表（如 ["shell_exec", "web_fetch"]）
    """

    def __init__(
        self,
        work_dir: str = ".",
        provider_id: str = "builtin",
        exclude: list[str] | None = None,
    ) -> None:
        super().__init__(provider_id=provider_id)
        self._work_dir = work_dir
        self._exclude = set(exclude or [])
        self._register_builtin_tools()

    @property
    def work_dir(self) -> str:
        return self._work_dir

    def _register_builtin_tools(self) -> None:
        """注册所有内置工具"""

        # -- file_search --
        if "file_search" not in self._exclude:
            async def _file_search(pattern: str, recursive: bool = True, max_results: int = 50) -> str:
                return await file_search(pattern, self._work_dir, recursive, max_results)

            self.register(
                ToolDefinition(
                    name="file_search",
                    description="按 glob 模式在项目中搜索文件，返回匹配的文件路径列表",
                    parameters=[
                        ToolParameter(name="pattern", type="string", description="glob 匹配模式，如 *.py、src/*.ts", required=True),
                        ToolParameter(name="recursive", type="boolean", description="是否递归搜索子目录", required=False, default=True),
                        ToolParameter(name="max_results", type="integer", description="最大返回数量", required=False, default=50),
                    ],
                ),
                handler=_file_search,
            )

        # -- file_read --
        if "file_read" not in self._exclude:
            async def _file_read(path: str, encoding: str = "utf-8", max_lines: int = 0, start_line: int = 1) -> str:
                return await file_read(path, self._work_dir, encoding, max_lines, start_line)

            self.register(
                ToolDefinition(
                    name="file_read",
                    description="读取指定文件的内容，支持行号范围和编码指定",
                    parameters=[
                        ToolParameter(name="path", type="string", description="文件路径", required=True),
                        ToolParameter(name="encoding", type="string", description="文件编码", required=False, default="utf-8"),
                        ToolParameter(name="max_lines", type="integer", description="最大读取行数，0 表示全部", required=False, default=0),
                        ToolParameter(name="start_line", type="integer", description="起始行号（1-based）", required=False, default=1),
                    ],
                ),
                handler=_file_read,
            )

        # -- file_write --
        if "file_write" not in self._exclude:
            async def _file_write(path: str, content: str, mode: str = "overwrite", encoding: str = "utf-8") -> str:
                return await file_write(path, content, self._work_dir, mode, encoding)

            self.register(
                ToolDefinition(
                    name="file_write",
                    description="写入内容到文件，支持覆盖、追加、仅创建三种模式",
                    parameters=[
                        ToolParameter(name="path", type="string", description="文件路径", required=True),
                        ToolParameter(name="content", type="string", description="要写入的内容", required=True),
                        ToolParameter(name="mode", type="string", description="写入模式: overwrite/append/create", required=False, default="overwrite",
                                      enum=["overwrite", "append", "create"]),
                        ToolParameter(name="encoding", type="string", description="文件编码", required=False, default="utf-8"),
                    ],
                ),
                handler=_file_write,
            )

        # -- list_directory --
        if "list_directory" not in self._exclude:
            async def _list_directory(path: str = ".", show_hidden: bool = False, detail: bool = False) -> str:
                return await list_directory(path, self._work_dir, show_hidden, detail)

            self.register(
                ToolDefinition(
                    name="list_directory",
                    description="列出指定目录的内容，支持显示隐藏文件和详细信息",
                    parameters=[
                        ToolParameter(name="path", type="string", description="目录路径", required=False, default="."),
                        ToolParameter(name="show_hidden", type="boolean", description="是否显示隐藏文件", required=False, default=False),
                        ToolParameter(name="detail", type="boolean", description="是否显示详细信息（大小等）", required=False, default=False),
                    ],
                ),
                handler=_list_directory,
            )

        # -- text_search --
        if "text_search" not in self._exclude:
            async def _text_search(pattern: str, file_pattern: str = "*", case_sensitive: bool = True, max_results: int = 30) -> str:
                return await text_search(pattern, self._work_dir, file_pattern, case_sensitive, max_results)

            self.register(
                ToolDefinition(
                    name="text_search",
                    description="在文件内容中搜索文本模式（类似 grep），支持正则表达式",
                    parameters=[
                        ToolParameter(name="pattern", type="string", description="搜索的正则表达式或文本", required=True),
                        ToolParameter(name="file_pattern", type="string", description="限定搜索的文件 glob 模式", required=False, default="*"),
                        ToolParameter(name="case_sensitive", type="boolean", description="是否区分大小写", required=False, default=True),
                        ToolParameter(name="max_results", type="integer", description="最大匹配数", required=False, default=30),
                    ],
                ),
                handler=_text_search,
            )

        # -- shell_exec --
        if "shell_exec" not in self._exclude:
            async def _shell_exec(command: str, timeout: int = 30) -> str:
                return await shell_exec(command, self._work_dir, timeout)

            self.register(
                ToolDefinition(
                    name="shell_exec",
                    description="在项目目录内执行 shell 命令（有安全策略限制危险命令）",
                    parameters=[
                        ToolParameter(name="command", type="string", description="要执行的 shell 命令", required=True),
                        ToolParameter(name="timeout", type="integer", description="超时秒数", required=False, default=30),
                    ],
                ),
                handler=_shell_exec,
            )

        # -- web_fetch --
        if "web_fetch" not in self._exclude:
            async def _web_fetch(url: str, timeout: int = 15, max_chars: int = 8000) -> str:
                return await web_fetch(url, timeout, max_chars)

            self.register(
                ToolDefinition(
                    name="web_fetch",
                    description="抓取网页 URL 的内容并提取纯文本",
                    parameters=[
                        ToolParameter(name="url", type="string", description="网页 URL（http/https）", required=True),
                        ToolParameter(name="timeout", type="integer", description="请求超时秒数", required=False, default=15),
                        ToolParameter(name="max_chars", type="integer", description="最大返回字符数", required=False, default=8000),
                    ],
                ),
                handler=_web_fetch,
            )

        # -- get_datetime --
        if "get_datetime" not in self._exclude:
            async def _get_datetime(timezone_offset: int = 8, format: str = "") -> str:
                return await get_datetime(timezone_offset, format)

            self.register(
                ToolDefinition(
                    name="get_datetime",
                    description="获取当前日期和时间信息",
                    parameters=[
                        ToolParameter(name="timezone_offset", type="integer", description="时区偏移小时数", required=False, default=8),
                        ToolParameter(name="format", type="string", description="自定义 strftime 格式", required=False, default=""),
                    ],
                ),
                handler=_get_datetime,
            )

        # -- json_tool --
        if "json_tool" not in self._exclude:
            async def _json_tool(input_text: str, action: str = "format", indent: int = 2) -> str:
                return await json_tool(input_text, action, indent)

            self.register(
                ToolDefinition(
                    name="json_tool",
                    description="JSON 解析、格式化、压缩或校验工具",
                    parameters=[
                        ToolParameter(name="input_text", type="string", description="输入的 JSON 字符串", required=True),
                        ToolParameter(name="action", type="string", description="操作类型", required=False, default="format",
                                      enum=["format", "validate", "minify"]),
                        ToolParameter(name="indent", type="integer", description="格式化缩进空格数", required=False, default=2),
                    ],
                ),
                handler=_json_tool,
            )

        logger.info(
            "BuiltinToolProvider registered %d tools (work_dir=%s, excluded=%s)",
            len(self._definitions), self._work_dir, list(self._exclude),
        )
