"""
Shell 操作工具

提供沙箱化的命令执行能力:
- shell_exec: 在限定目录内执行 shell 命令

安全策略:
- 命令在 work_dir 内执行（cwd 设定为沙箱目录）
- 禁止危险命令（rm -rf /、format、del 等）
- 超时控制（默认 30 秒）
- 输出截断（防止超长输出）
- 审计日志
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# 危险命令模式（跨平台）
_DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+/",             # Linux/macOS: 删除根目录
    r"rm\s+-rf\s+~",             # 删除 home
    r"format\s+[a-zA-Z]:",       # Windows: 格式化磁盘
    r"del\s+/[sS]\s+/[qQ]\s+",  # Windows: 强制递归删除
    r"mkfs\.",                    # 格式化文件系统
    r"dd\s+if=.*of=/dev/",      # 直接写磁盘设备
    r":\(\)\s*\{",               # fork bomb
    r"shutdown",                  # 关机
    r"reboot",                    # 重启
    r"init\s+0",                 # 关机
]

_DANGEROUS_RE = [re.compile(p, re.IGNORECASE) for p in _DANGEROUS_PATTERNS]

# 输出最大字符数
_MAX_OUTPUT_CHARS = 10_000


async def shell_exec(
    command: str,
    work_dir: str,
    timeout: int = 30,
    max_output: int = _MAX_OUTPUT_CHARS,
) -> str:
    """在沙箱目录内执行 shell 命令。

    Args:
        command: 要执行的 shell 命令
        work_dir: 命令执行目录（沙箱），命令的 cwd 被限制在此目录内
        timeout: 超时秒数，默认 30
        max_output: 最大输出字符数，默认 10000
    """
    # 安全检查
    for pat in _DANGEROUS_RE:
        if pat.search(command):
            logger.warning("shell_exec BLOCKED dangerous command: %s", command[:100])
            return f"错误: 命令被安全策略拦截 - 包含危险操作"

    logger.info("shell_exec: command=%s dir=%s timeout=%d", command[:100], work_dir, timeout)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"错误: 命令超时（{timeout}s），已终止\n命令: {command}"

        stdout_text = stdout.decode("utf-8", errors="replace") if stdout else ""
        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        exit_code = proc.returncode

        # 截断
        if len(stdout_text) > max_output:
            stdout_text = stdout_text[:max_output] + f"\n... (输出已截断，共 {len(stdout_text)} 字符)"
        if len(stderr_text) > max_output:
            stderr_text = stderr_text[:max_output] + f"\n... (错误输出已截断)"

        result_parts = []
        result_parts.append(f"[exit_code: {exit_code}]")
        if stdout_text.strip():
            result_parts.append(f"--- stdout ---\n{stdout_text}")
        if stderr_text.strip():
            result_parts.append(f"--- stderr ---\n{stderr_text}")
        if not stdout_text.strip() and not stderr_text.strip():
            result_parts.append("(无输出)")

        return "\n".join(result_parts)

    except FileNotFoundError:
        return f"错误: shell 不可用"
    except Exception as e:
        return f"错误: 命令执行失败 - {e}"
