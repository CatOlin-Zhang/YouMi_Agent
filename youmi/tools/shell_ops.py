"""
Shell 操作工具

提供沙箱化的命令执行能力:
- shell_exec: 在限定目录内执行 shell 命令

安全策略 (M1: 策略式沙箱):
- 命令在 work_dir 内执行（cwd 设定为沙箱目录）
- 命令经 youmi.security 沙箱检查: 内置危险命令黑名单（始终生效）
  + 可选白名单 / 网络限制 / 自定义拒绝规则（YOUMI_SANDBOX_* 配置）
- 子进程环境变量自动清理（剥离 API key / token 等敏感项，防凭据泄漏）
- 超时控制（默认 30 秒，受沙箱上限约束）
- 输出截断（防止超长输出）
- 审计日志（由工具执行治理层记录）
"""

from __future__ import annotations

import asyncio
import logging

from youmi.security import get_sandbox

logger = logging.getLogger(__name__)

# 输出最大字符数
_MAX_OUTPUT_CHARS = 10_000


async def shell_exec(
    command: str,
    work_dir: str,
    timeout: int = 30,
    max_output: int = _MAX_OUTPUT_CHARS,
) -> str:
    """在沙箱目录内执行 shell 命令。

    M1 增强: 命令经策略式沙箱检查（黑名单/白名单/网络限制），
    子进程环境变量自动清理，超时与输出受策略上限约束。

    Args:
        command: 要执行的 shell 命令
        work_dir: 命令执行目录（沙箱），命令的 cwd 被限制在此目录内
        timeout: 超时秒数，默认 30
        max_output: 最大输出字符数，默认 10000
    """
    # M1: 策略式沙箱检查（内置危险命令黑名单始终生效）
    sandbox = get_sandbox()
    violation = sandbox.evaluate_command(command)
    if violation:
        logger.warning("shell_exec BLOCKED: %s | command=%s", violation, command[:100])
        return f"错误: 命令被安全策略拦截 - {violation}"

    timeout = sandbox.clamp_timeout(timeout)
    max_output = sandbox.clamp_output(max_output)
    env = sandbox.build_env()

    logger.info("shell_exec: command=%s dir=%s timeout=%s", command[:100], work_dir, timeout)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
            env=env,
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
