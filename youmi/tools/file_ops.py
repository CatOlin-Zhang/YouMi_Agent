"""
文件操作工具

提供 Agent 常用的文件系统操作工具函数:
- file_search: 按 glob 模式搜索文件
- file_read: 读取文件内容
- file_write: 写入/创建文件
- list_directory: 列出目录内容
- text_search: 在文件中搜索文本模式（类 grep）

所有操作限定在沙箱目录（work_dir）内，防止越权访问。
每次操作记录审计日志。
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 安全校验
# ---------------------------------------------------------------------------

def _resolve_safe_path(path: str, work_dir: str) -> Path:
    """解析路径并确保在沙箱目录内

    Args:
        path: 目标路径（绝对或相对）
        work_dir: 沙箱工作目录

    Returns:
        解析后的 Path 对象

    Raises:
        PermissionError: 路径越权
    """
    base = Path(work_dir).resolve()
    target = Path(path)

    if target.is_absolute():
        resolved = target.resolve()
    else:
        resolved = (base / target).resolve()

    # 确保在沙箱内
    try:
        resolved.relative_to(base)
    except ValueError:
        raise PermissionError(
            f"路径 '{path}' 超出沙箱目录 '{work_dir}'，操作被拒绝"
        )

    return resolved


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

async def file_search(
    pattern: str,
    work_dir: str,
    recursive: bool = True,
    max_results: int = 50,
) -> str:
    """按 glob 模式搜索文件，返回匹配的文件路径列表。

    Args:
        pattern: glob 匹配模式（如 "*.py"、"src/*.ts"）
        work_dir: 搜索的根目录（沙箱）
        recursive: 是否递归搜索子目录，默认 True
        max_results: 最大返回数量，默认 50
    """
    base = Path(work_dir).resolve()
    if not base.exists():
        return f"错误: 目录 '{work_dir}' 不存在"

    matches: list[str] = []

    if recursive:
        for root, dirs, files in os.walk(base):
            # 跳过隐藏目录
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for fname in files:
                if fnmatch.fnmatch(fname, pattern) or fnmatch.fnmatch(
                    os.path.relpath(os.path.join(root, fname), base), pattern
                ):
                    rel = os.path.relpath(os.path.join(root, fname), base)
                    matches.append(rel)
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break
    else:
        for entry in base.iterdir():
            if entry.is_file() and fnmatch.fnmatch(entry.name, pattern):
                matches.append(entry.name)
                if len(matches) >= max_results:
                    break

    logger.info("file_search: pattern=%s dir=%s found=%d", pattern, work_dir, len(matches))

    if not matches:
        return f"未找到匹配 '{pattern}' 的文件"

    result = f"找到 {len(matches)} 个文件:\n"
    for m in matches:
        result += f"  {m}\n"
    if len(matches) >= max_results:
        result += f"  ... (已达到最大返回数 {max_results})"
    return result


async def file_read(
    path: str,
    work_dir: str,
    encoding: str = "utf-8",
    max_lines: int = 0,
    start_line: int = 1,
) -> str:
    """读取指定文件的内容。

    Args:
        path: 文件路径（相对或绝对，必须在 work_dir 内）
        work_dir: 沙箱工作目录
        encoding: 文件编码，默认 utf-8
        max_lines: 最大读取行数，0 表示全部读取
        start_line: 起始行号（1-based），默认 1
    """
    safe_path = _resolve_safe_path(path, work_dir)

    if not safe_path.exists():
        return f"错误: 文件 '{path}' 不存在"
    if not safe_path.is_file():
        return f"错误: '{path}' 不是文件"

    try:
        text = safe_path.read_text(encoding=encoding)
    except UnicodeDecodeError:
        return f"错误: 无法以 {encoding} 编码读取 '{path}'，可能是二进制文件"
    except Exception as e:
        return f"错误: 读取失败 - {e}"

    lines = text.splitlines()
    total = len(lines)

    # 行号裁剪
    start_idx = max(0, start_line - 1)
    if max_lines > 0:
        end_idx = min(total, start_idx + max_lines)
        selected = lines[start_idx:end_idx]
        header = f"[文件: {path} | 行 {start_idx + 1}-{end_idx}/{total}]"
    else:
        selected = lines[start_idx:]
        header = f"[文件: {path} | 共 {total} 行]"

    logger.info("file_read: %s (%d lines)", path, len(selected))
    return header + "\n" + "\n".join(selected)


async def file_write(
    path: str,
    content: str,
    work_dir: str,
    mode: str = "overwrite",
    encoding: str = "utf-8",
) -> str:
    """写入内容到指定文件。

    Args:
        path: 文件路径（相对或绝对，必须在 work_dir 内）
        content: 要写入的内容
        work_dir: 沙箱工作目录
        mode: 写入模式 - "overwrite"(覆盖) / "append"(追加) / "create"(仅创建，已存在则报错)
        encoding: 文件编码，默认 utf-8
    """
    safe_path = _resolve_safe_path(path, work_dir)

    if mode == "create" and safe_path.exists():
        return f"错误: 文件 '{path}' 已存在，mode=create 不允许覆盖"

    # 自动创建父目录
    safe_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        if mode == "append":
            with open(safe_path, "a", encoding=encoding) as f:
                f.write(content)
            action = "追加"
        else:
            safe_path.write_text(content, encoding=encoding)
            action = "写入"

        logger.info("file_write: %s (%s, %d chars)", path, action, len(content))
        return f"成功{action}文件 '{path}'（{len(content)} 字符）"

    except Exception as e:
        return f"错误: 写入失败 - {e}"


async def list_directory(
    path: str,
    work_dir: str,
    show_hidden: bool = False,
    detail: bool = False,
) -> str:
    """列出目录内容。

    Args:
        path: 目录路径（相对或绝对，必须在 work_dir 内）
        work_dir: 沙箱工作目录
        show_hidden: 是否显示隐藏文件（以 . 开头），默认 False
        detail: 是否显示详细信息（大小、权限），默认 False
    """
    safe_path = _resolve_safe_path(path, work_dir)

    if not safe_path.exists():
        return f"错误: 目录 '{path}' 不存在"
    if not safe_path.is_dir():
        return f"错误: '{path}' 不是目录"

    entries: list[str] = []
    for entry in sorted(safe_path.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
        if not show_hidden and entry.name.startswith("."):
            continue

        if detail:
            stat = entry.stat()
            kind = "d" if entry.is_dir() else "f"
            size = stat.st_size
            entries.append(f"  [{kind}] {entry.name:<40s} {size:>10,d} bytes")
        else:
            suffix = "/" if entry.is_dir() else ""
            entries.append(f"  {entry.name}{suffix}")

    logger.info("list_directory: %s (%d entries)", path, len(entries))

    if not entries:
        return f"目录 '{path}' 为空"

    header = f"[目录: {path} | {len(entries)} 项]"
    return header + "\n" + "\n".join(entries)


async def text_search(
    pattern: str,
    work_dir: str,
    file_pattern: str = "*",
    case_sensitive: bool = True,
    max_results: int = 30,
) -> str:
    """在文件内容中搜索文本模式（类似 grep）。

    Args:
        pattern: 搜索的正则表达式或文本模式
        work_dir: 搜索的根目录（沙箱）
        file_pattern: 限定搜索的文件 glob 模式（如 "*.py"），默认所有文件
        case_sensitive: 是否区分大小写，默认 True
        max_results: 最大匹配数，默认 30
    """
    base = Path(work_dir).resolve()
    if not base.exists():
        return f"错误: 目录 '{work_dir}' 不存在"

    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"错误: 无效的正则表达式 - {e}"

    matches: list[str] = []
    files_searched = 0

    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fname in files:
            if not fnmatch.fnmatch(fname, file_pattern):
                continue

            fpath = Path(root) / fname
            rel = str(fpath.relative_to(base))
            files_searched += 1

            try:
                text = fpath.read_text(encoding="utf-8", errors="ignore")
                for i, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        matches.append(f"{rel}:{i}: {line.strip()[:120]}")
                        if len(matches) >= max_results:
                            break
            except (PermissionError, OSError):
                continue

            if len(matches) >= max_results:
                break
        if len(matches) >= max_results:
            break

    logger.info("text_search: pattern=%s dir=%s files=%d matches=%d",
                pattern, work_dir, files_searched, len(matches))

    if not matches:
        return f"在 {files_searched} 个文件中未找到匹配 '{pattern}' 的内容"

    header = f"[搜索: '{pattern}' | {files_searched} 个文件 | {len(matches)} 处匹配]"
    result = header + "\n" + "\n".join(matches)
    if len(matches) >= max_results:
        result += f"\n... (已达到最大匹配数 {max_results})"
    return result
