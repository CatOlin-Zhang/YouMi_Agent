"""
数据处理工具

提供常用数据处理工具:
- get_datetime: 获取当前日期时间信息
- json_tool: JSON 解析 / 格式化 / 校验
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


async def get_datetime(
    timezone_offset: int = 8,
    format: str = "",
) -> str:
    """获取当前日期时间信息。

    Args:
        timezone_offset: 时区偏移（小时），默认 8（东八区/中国标准时间）
        format: 自定义格式字符串（Python strftime），默认输出完整信息
    """
    from datetime import timedelta

    tz = timezone(timedelta(hours=timezone_offset))
    now = datetime.now(tz)

    if format:
        try:
            return now.strftime(format)
        except ValueError as e:
            return f"错误: 无效的格式字符串 - {e}"

    return (
        f"当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')} "
        f"(UTC{'+' if timezone_offset >= 0 else ''}{timezone_offset})\n"
        f"日期: {now.strftime('%Y年%m月%d日')}\n"
        f"星期: {['一','二','三','四','五','六','日'][now.weekday()]}\n"
        f"Unix 时间戳: {int(now.timestamp())}\n"
        f"ISO 格式: {now.isoformat()}"
    )


async def json_tool(
    input_text: str,
    action: str = "format",
    indent: int = 2,
) -> str:
    """JSON 解析、格式化或校验工具。

    Args:
        input_text: 输入的 JSON 字符串
        action: 操作类型 - "format"(格式化) / "validate"(校验) / "minify"(压缩)
        indent: 格式化缩进空格数，默认 2
    """
    try:
        data = json.loads(input_text)
    except json.JSONDecodeError as e:
        return f"JSON 校验失败: {e}\n位置: 行 {e.lineno} 列 {e.colno}"

    if action == "validate":
        logger.info("json_tool: validate OK (%d chars)", len(input_text))
        type_name = type(data).__name__
        if isinstance(data, list):
            return f"JSON 校验通过 ✓\n类型: array（{len(data)} 个元素）"
        elif isinstance(data, dict):
            return f"JSON 校验通过 ✓\n类型: object（{len(data)} 个键）"
        else:
            return f"JSON 校验通过 ✓\n类型: {type_name}\n值: {data}"

    elif action == "minify":
        result = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        logger.info("json_tool: minify %d → %d chars", len(input_text), len(result))
        return result

    else:  # format
        result = json.dumps(data, ensure_ascii=False, indent=indent)
        logger.info("json_tool: format %d → %d chars", len(input_text), len(result))
        return result
