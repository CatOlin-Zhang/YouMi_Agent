"""
内置工具模块 (youmi.tools)

提供 Agent 常用的内置工具:
- file_search / file_read / file_write: 文件操作
- list_directory / text_search: 目录浏览与文本搜索
- shell_exec: 沙箱化命令执行
- web_fetch: 网页内容抓取
- get_datetime: 日期时间
- json_tool: JSON 处理

核心组件:
- BuiltinToolProvider: 内置工具 MCP Provider，自动注册所有工具到沙箱目录

用法::

    from youmi.tools import BuiltinToolProvider

    provider = BuiltinToolProvider(work_dir="/path/to/project")
"""

from youmi.tools.builtin import BuiltinToolProvider
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

__all__ = [
    "BuiltinToolProvider",
    "file_search",
    "file_read",
    "file_write",
    "list_directory",
    "text_search",
    "shell_exec",
    "web_fetch",
    "get_datetime",
    "json_tool",
]
