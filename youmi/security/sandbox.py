"""
策略式沙箱 (M1: P0 安全)

跨平台、纯 Python 的进程与文件访问策略层，提供:
- 命令检查: 内置危险命令黑名单（始终生效）+ 自定义拒绝规则
  + 可选白名单模式 + 可选网络命令限制
- 路径检查: allowed_roots 根目录围栏（未配置时放行；调用方的
  work_dir jail 仍是基础防护）
- 环境清理: 剥离子进程环境中的敏感变量（API key / token 等），
  默认开启，防止凭据经由工具调用泄漏
- 资源限制: 命令超时上限与输出字符上限兜底

设计原则:
- 零专属依赖、Windows / Linux / macOS 通用
- 默认安全: 环境清理默认开启；强策略（白名单 / 网络限制 / 根目录）按需配置
- 优雅降级: 未配置时对现有行为零影响；``enabled=False`` 时仅保留内置
  危险命令黑名单
- 环境变量驱动: ``YOUMI_SANDBOX_*``

用法::

    from youmi.security import get_sandbox

    sandbox = get_sandbox()

    reason = sandbox.evaluate_command("curl http://x")   # "" = 放行
    sandbox.check_command("rm -rf /")                    # 抛 SandboxViolation

    env = sandbox.build_env()          # 清理后的环境变量（None = 继承父进程）
    timeout = sandbox.clamp_timeout(30)
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SandboxViolation(PermissionError):
    """沙箱策略违规 — 命令或路径被拒绝"""


# ---------------------------------------------------------------------------
# 内置规则（始终生效）
# ---------------------------------------------------------------------------

# 危险命令模式（跨平台）
BUILTIN_DENIED_PATTERNS = [
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

# 常见网络命令（network_blocked=True 时生效）
NETWORK_COMMAND_PATTERNS = [
    r"\bcurl\b",
    r"\bwget\b",
    r"\bssh\b",
    r"\bscp\b",
    r"\bsftp\b",
    r"\btelnet\b",
    r"\bncat\b",
    r"\bnc\b",
    r"invoke-webrequest",
    r"invoke-restmethod",
]

_BUILTIN_DENIED_RE = [re.compile(p, re.IGNORECASE) for p in BUILTIN_DENIED_PATTERNS]
_NETWORK_DENIED_RE = [re.compile(p, re.IGNORECASE) for p in NETWORK_COMMAND_PATTERNS]

# 默认剔除的环境变量名模式（防凭据泄漏）
_DEFAULT_ENV_DENY_PATTERNS = [
    r"(api[_-]?key|token|secret|password|passwd|credential|auth)",
]


class SandboxPolicy(BaseModel):
    """策略式沙箱配置

    Args:
        enabled: 是否启用增强策略（白名单 / 网络限制 / 根目录 / env 清理）。
            False 时仅保留内置危险命令黑名单（等价于历史行为）。
        allowed_roots: 允许访问的根目录列表（空 = 不限制，依赖 work_dir jail）
        denied_commands: 追加的拒绝命令正则（与内置黑名单合并）
        allowed_commands: 命令前缀白名单（非空时仅允许匹配的命令）
        scrub_env: 是否清理子进程环境中的敏感变量（默认开启）
        env_deny_patterns: 环境变量名剔除正则列表
        network_blocked: 是否禁止常见网络命令
        max_timeout_s: 命令超时上限（秒）
        max_output_chars: 输出字符上限
    """

    enabled: bool = True
    allowed_roots: list[str] = Field(default_factory=list)
    denied_commands: list[str] = Field(default_factory=list)
    allowed_commands: list[str] = Field(default_factory=list)
    scrub_env: bool = True
    env_deny_patterns: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_ENV_DENY_PATTERNS),
    )
    network_blocked: bool = False
    max_timeout_s: float = Field(default=600.0, gt=0.0)
    max_output_chars: int = Field(default=100_000, gt=0)

    model_config = {"frozen": True}


class Sandbox:
    """策略式沙箱 — 命令 / 路径 / 环境变量 / 资源限制的统一策略执行器"""

    def __init__(self, policy: SandboxPolicy | None = None) -> None:
        self._policy = policy or SandboxPolicy()
        self._custom_denied_re = self._compile_safe(self._policy.denied_commands)
        self._env_deny_re = self._compile_safe(self._policy.env_deny_patterns)

    @staticmethod
    def _compile_safe(patterns: list[str]) -> list[re.Pattern[str]]:
        """编译正则列表（忽略并告警无效项）"""
        compiled: list[re.Pattern[str]] = []
        for p in patterns:
            try:
                compiled.append(re.compile(p, re.IGNORECASE))
            except re.error as exc:
                logger.warning("沙箱: 忽略无效正则 %r (%s)", p, exc)
        return compiled

    @property
    def policy(self) -> SandboxPolicy:
        return self._policy

    # ------------------------------------------------------------------
    # 命令检查
    # ------------------------------------------------------------------

    def evaluate_command(self, command: str) -> str:
        """检查命令是否允许执行

        Returns:
            拒绝原因（空字符串 = 允许执行）
        """
        # 1. 内置危险命令黑名单 — 始终生效
        for pat in _BUILTIN_DENIED_RE:
            if pat.search(command):
                return "包含危险操作"

        if not self._policy.enabled:
            return ""

        # 2. 自定义拒绝规则
        for pat in self._custom_denied_re:
            if pat.search(command):
                return "匹配自定义拒绝规则"

        # 3. 网络命令限制
        if self._policy.network_blocked:
            for pat in _NETWORK_DENIED_RE:
                if pat.search(command):
                    return "网络访问被沙箱策略禁止"

        # 4. 命令白名单（配置后启用）
        if self._policy.allowed_commands:
            stripped = command.strip()
            if not any(
                stripped.startswith(cmd)
                for cmd in self._policy.allowed_commands
            ):
                return "命令不在白名单内"

        return ""

    def check_command(self, command: str) -> None:
        """检查命令，违规时抛出 SandboxViolation"""
        reason = self.evaluate_command(command)
        if reason:
            raise SandboxViolation(f"命令被沙箱拒绝: {reason}")

    # ------------------------------------------------------------------
    # 路径检查
    # ------------------------------------------------------------------

    def check_path(self, path: str | Path) -> Path:
        """解析路径并校验在 allowed_roots 内（未配置时放行）

        Returns:
            解析后的绝对路径

        Raises:
            SandboxViolation: 路径不在允许根目录内
        """
        resolved = Path(path).resolve()
        roots = self._policy.allowed_roots
        if not roots or not self._policy.enabled:
            return resolved
        for root in roots:
            try:
                resolved.relative_to(Path(root).resolve())
                return resolved
            except ValueError:
                continue
        raise SandboxViolation(f"路径 '{path}' 不在沙箱允许的根目录内")

    # ------------------------------------------------------------------
    # 环境清理
    # ------------------------------------------------------------------

    def build_env(
        self,
        base_env: dict[str, str] | None = None,
    ) -> dict[str, str] | None:
        """构建清理后的子进程环境变量

        Args:
            base_env: 基础环境（默认取 ``os.environ``）

        Returns:
            清理后的 env；策略禁用或关闭清理时返回 None（表示继承父进程环境）
        """
        if (
            not self._policy.enabled
            or not self._policy.scrub_env
            or not self._env_deny_re
        ):
            return None

        env = dict(base_env if base_env is not None else os.environ)
        removed = [k for k in env if any(p.search(k) for p in self._env_deny_re)]
        for key in removed:
            del env[key]
        if removed:
            logger.debug("沙箱 env 清理 %d 项: %s", len(removed), removed)
        return env

    # ------------------------------------------------------------------
    # 资源限制
    # ------------------------------------------------------------------

    def clamp_timeout(self, timeout: float) -> float:
        """命令超时上限约束（策略禁用时原样返回）"""
        if not self._policy.enabled:
            return timeout
        return min(timeout, self._policy.max_timeout_s)

    def clamp_output(self, max_output: int) -> int:
        """输出字符上限约束（策略禁用时原样返回）"""
        if not self._policy.enabled:
            return max_output
        return min(max_output, self._policy.max_output_chars)

    def __repr__(self) -> str:
        return (
            f"<Sandbox enabled={self._policy.enabled} "
            f"roots={len(self._policy.allowed_roots)} "
            f"network_blocked={self._policy.network_blocked}>"
        )


# ---------------------------------------------------------------------------
# 环境变量配置与进程级单例
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    """读取布尔型环境变量（空 = 默认值）"""
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


def policy_from_env() -> SandboxPolicy:
    """从环境变量构建沙箱策略

    环境变量:
    - ``YOUMI_SANDBOX_ENABLED``: 0/1（默认 1）
    - ``YOUMI_SANDBOX_ROOTS``: 允许根目录（``os.pathsep`` 分隔）
    - ``YOUMI_SANDBOX_DENY``: 追加拒绝正则（``;`` 分隔）
    - ``YOUMI_SANDBOX_ALLOW``: 命令白名单前缀（``;`` 分隔）
    - ``YOUMI_SANDBOX_SCRUB_ENV``: 0/1（默认 1）
    - ``YOUMI_SANDBOX_NETWORK_BLOCKED``: 0/1（默认 0）
    - ``YOUMI_SANDBOX_MAX_TIMEOUT_S``: 超时上限（默认 600）
    - ``YOUMI_SANDBOX_MAX_OUTPUT_CHARS``: 输出上限（默认 100000）
    """
    kwargs: dict[str, Any] = {}

    if os.environ.get("YOUMI_SANDBOX_ENABLED", "").strip():
        kwargs["enabled"] = _env_bool("YOUMI_SANDBOX_ENABLED", True)

    roots = os.environ.get("YOUMI_SANDBOX_ROOTS", "").strip()
    if roots:
        kwargs["allowed_roots"] = [p for p in roots.split(os.pathsep) if p.strip()]

    deny = os.environ.get("YOUMI_SANDBOX_DENY", "").strip()
    if deny:
        kwargs["denied_commands"] = [p for p in deny.split(";") if p.strip()]

    allow = os.environ.get("YOUMI_SANDBOX_ALLOW", "").strip()
    if allow:
        kwargs["allowed_commands"] = [p.strip() for p in allow.split(";") if p.strip()]

    if os.environ.get("YOUMI_SANDBOX_SCRUB_ENV", "").strip():
        kwargs["scrub_env"] = _env_bool("YOUMI_SANDBOX_SCRUB_ENV", True)

    if os.environ.get("YOUMI_SANDBOX_NETWORK_BLOCKED", "").strip():
        kwargs["network_blocked"] = _env_bool("YOUMI_SANDBOX_NETWORK_BLOCKED", False)

    raw = os.environ.get("YOUMI_SANDBOX_MAX_TIMEOUT_S", "").strip()
    if raw:
        try:
            kwargs["max_timeout_s"] = float(raw)
        except ValueError:
            logger.warning("YOUMI_SANDBOX_MAX_TIMEOUT_S 无效: %r", raw)

    raw = os.environ.get("YOUMI_SANDBOX_MAX_OUTPUT_CHARS", "").strip()
    if raw:
        try:
            kwargs["max_output_chars"] = int(raw)
        except ValueError:
            logger.warning("YOUMI_SANDBOX_MAX_OUTPUT_CHARS 无效: %r", raw)

    return SandboxPolicy(**kwargs)


_default: Sandbox | None = None


def get_sandbox() -> Sandbox:
    """获取进程级默认沙箱（懒初始化，读取 ``YOUMI_SANDBOX_*`` 环境变量）"""
    global _default
    if _default is None:
        _default = Sandbox(policy_from_env())
    return _default


def configure_sandbox(sandbox: Sandbox) -> None:
    """替换进程级默认沙箱（启动配置用）"""
    global _default
    _default = sandbox


def reset_sandbox() -> None:
    """清空进程级默认实例（测试隔离用）"""
    global _default
    _default = None
