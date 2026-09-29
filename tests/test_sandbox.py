"""
M1 沙箱测试 — 策略式沙箱 (youmi.security.sandbox)

覆盖:
- 内置危险命令黑名单（始终生效，enabled=False 也生效）
- 自定义拒绝规则 / 白名单模式 / 网络命令限制 / 无效正则容错
- check_command 抛 SandboxViolation
- check_path allowed_roots 围栏（未配置时放行）
- build_env 敏感变量清理（默认开启）
- clamp_timeout / clamp_output 资源上限
- policy_from_env 环境变量解析
- 单例 get / configure / reset
- shell_exec / file_ops 集成: 拦截、放行与根目录约束
"""

from __future__ import annotations

import os

import pytest

from youmi.security import (
    Sandbox,
    SandboxPolicy,
    SandboxViolation,
    configure_sandbox,
    get_sandbox,
    policy_from_env,
    reset_sandbox,
)
from youmi.tools.shell_ops import shell_exec


@pytest.fixture(autouse=True)
def _isolate_sandbox(monkeypatch):
    """隔离进程级沙箱单例与 YOUMI_SANDBOX_* 环境变量"""
    for key in list(os.environ):
        if key.startswith("YOUMI_SANDBOX_"):
            monkeypatch.delenv(key, raising=False)
    reset_sandbox()
    yield
    reset_sandbox()


# ---------------------------------------------------------------------------
# 命令检查
# ---------------------------------------------------------------------------

class TestCommandCheck:
    def test_dangerous_commands_blocked(self):
        s = Sandbox()
        assert s.evaluate_command("rm -rf /") == "包含危险操作"
        assert s.evaluate_command("format C:") == "包含危险操作"
        assert s.evaluate_command("mkfs.ext4 /dev/sda") == "包含危险操作"
        assert s.evaluate_command("shutdown -h now") == "包含危险操作"
        assert s.evaluate_command("dd if=/dev/zero of=/dev/sda") == "包含危险操作"

    def test_safe_commands_allowed(self):
        s = Sandbox()
        assert s.evaluate_command("echo hello") == ""
        assert s.evaluate_command("ls -la") == ""
        assert s.evaluate_command("git status") == ""

    def test_builtin_denied_even_when_disabled(self):
        """enabled=False 仍保留内置黑名单（基础安全底线）"""
        s = Sandbox(SandboxPolicy(enabled=False))
        assert s.evaluate_command("rm -rf /") == "包含危险操作"
        assert s.evaluate_command("echo hello") == ""

    def test_check_command_raises(self):
        s = Sandbox()
        with pytest.raises(SandboxViolation):
            s.check_command("reboot")

    def test_check_command_passes(self):
        Sandbox().check_command("echo hello")  # 不抛异常


class TestCustomPolicy:
    def test_custom_denied(self):
        s = Sandbox(SandboxPolicy(denied_commands=[r"\bdocker\b"]))
        assert s.evaluate_command("docker ps") == "匹配自定义拒绝规则"
        assert s.evaluate_command("echo ok") == ""

    def test_invalid_pattern_ignored(self):
        s = Sandbox(SandboxPolicy(denied_commands=["([bad", r"\bfoo\b"]))
        assert s.evaluate_command("foo run") == "匹配自定义拒绝规则"

    def test_network_blocked(self):
        s = Sandbox(SandboxPolicy(network_blocked=True))
        assert "网络" in s.evaluate_command("curl http://example.com")
        assert "网络" in s.evaluate_command("wget http://x")
        assert "网络" in s.evaluate_command("Invoke-WebRequest http://x")
        assert s.evaluate_command("echo hi") == ""

    def test_allowlist(self):
        s = Sandbox(SandboxPolicy(allowed_commands=["git", "echo"]))
        assert s.evaluate_command("git status") == ""
        assert s.evaluate_command("echo hi") == ""
        assert s.evaluate_command("python x.py") == "命令不在白名单内"

    def test_disabled_skips_enhanced(self):
        s = Sandbox(SandboxPolicy(
            enabled=False, network_blocked=True, allowed_commands=["git"],
        ))
        assert s.evaluate_command("curl http://x") == ""


# ---------------------------------------------------------------------------
# 路径检查
# ---------------------------------------------------------------------------

class TestPathCheck:
    def test_allowed_roots(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        s = Sandbox(SandboxPolicy(allowed_roots=[str(root)]))

        inside = s.check_path(root / "sub" / "a.txt")
        assert str(inside).startswith(str(root))

        with pytest.raises(SandboxViolation):
            s.check_path(tmp_path / "other.txt")

    def test_no_roots_allows_all(self, tmp_path):
        s = Sandbox()
        assert s.check_path(tmp_path / "x.txt") is not None

    def test_disabled_skips_roots(self, tmp_path):
        s = Sandbox(SandboxPolicy(
            enabled=False, allowed_roots=[str(tmp_path / "nonexistent")],
        ))
        assert s.check_path(tmp_path / "x.txt") is not None


# ---------------------------------------------------------------------------
# 环境清理
# ---------------------------------------------------------------------------

class TestEnvScrub:
    def test_build_env_scrubs_sensitive(self):
        s = Sandbox()
        base = {
            "PATH": "/usr/bin",
            "HOME": "/home/u",
            "OPENAI_API_KEY": "sk-x",
            "MY_TOKEN": "t",
            "DB_PASSWORD": "p",
            "HTTP_AUTHORIZATION": "Bearer x",
        }
        out = s.build_env(base)
        assert out is not None
        assert out["PATH"] == "/usr/bin"
        assert out["HOME"] == "/home/u"
        assert "OPENAI_API_KEY" not in out
        assert "MY_TOKEN" not in out
        assert "DB_PASSWORD" not in out
        assert "HTTP_AUTHORIZATION" not in out

    def test_build_env_disabled(self):
        assert Sandbox(SandboxPolicy(scrub_env=False)).build_env({"A": "1"}) is None
        assert Sandbox(SandboxPolicy(enabled=False)).build_env({"A": "1"}) is None

    def test_custom_env_deny(self):
        s = Sandbox(SandboxPolicy(env_deny_patterns=[r"^FOO$"]))
        out = s.build_env({"FOO": "x", "BAR": "y"})
        assert out is not None
        assert "FOO" not in out
        assert out["BAR"] == "y"


# ---------------------------------------------------------------------------
# 资源限制
# ---------------------------------------------------------------------------

class TestLimits:
    def test_clamps(self):
        s = Sandbox(SandboxPolicy(max_timeout_s=60, max_output_chars=1000))
        assert s.clamp_timeout(30) == 30
        assert s.clamp_timeout(120) == 60
        assert s.clamp_output(500) == 500
        assert s.clamp_output(5000) == 1000

    def test_clamp_disabled(self):
        s = Sandbox(SandboxPolicy(enabled=False, max_timeout_s=1))
        assert s.clamp_timeout(999) == 999
        assert s.clamp_output(99999) == 99999


# ---------------------------------------------------------------------------
# 环境变量配置与单例
# ---------------------------------------------------------------------------

class TestPolicyFromEnv:
    def test_parse(self, monkeypatch):
        monkeypatch.setenv("YOUMI_SANDBOX_ROOTS", os.pathsep.join(["/a", "/b"]))
        monkeypatch.setenv("YOUMI_SANDBOX_DENY", r"\bfoo\b;bar")
        monkeypatch.setenv("YOUMI_SANDBOX_ALLOW", "git;echo")
        monkeypatch.setenv("YOUMI_SANDBOX_NETWORK_BLOCKED", "1")
        monkeypatch.setenv("YOUMI_SANDBOX_SCRUB_ENV", "0")
        monkeypatch.setenv("YOUMI_SANDBOX_MAX_TIMEOUT_S", "42.5")
        monkeypatch.setenv("YOUMI_SANDBOX_MAX_OUTPUT_CHARS", "1234")

        p = policy_from_env()
        assert p.allowed_roots == ["/a", "/b"]
        assert p.denied_commands == [r"\bfoo\b", "bar"]
        assert p.allowed_commands == ["git", "echo"]
        assert p.network_blocked is True
        assert p.scrub_env is False
        assert p.max_timeout_s == 42.5
        assert p.max_output_chars == 1234

    def test_defaults(self):
        p = policy_from_env()
        assert p.enabled is True
        assert p.allowed_roots == []
        assert p.network_blocked is False
        assert p.max_timeout_s == 600.0

    def test_invalid_number_falls_back(self, monkeypatch):
        monkeypatch.setenv("YOUMI_SANDBOX_MAX_TIMEOUT_S", "abc")
        assert policy_from_env().max_timeout_s == 600.0


class TestSingleton:
    def test_singleton_and_configure(self):
        s1 = get_sandbox()
        assert get_sandbox() is s1

        custom = Sandbox(SandboxPolicy(network_blocked=True))
        configure_sandbox(custom)
        assert get_sandbox() is custom

        reset_sandbox()
        assert get_sandbox() is not custom

    def test_singleton_reads_env(self, monkeypatch):
        monkeypatch.setenv("YOUMI_SANDBOX_NETWORK_BLOCKED", "1")
        assert get_sandbox().policy.network_blocked is True


# ---------------------------------------------------------------------------
# 工具集成
# ---------------------------------------------------------------------------

class TestToolIntegration:
    async def test_shell_blocks_dangerous(self, tmp_path):
        result = await shell_exec("rm -rf /", str(tmp_path))
        assert "安全策略" in result
        assert "拦截" in result

    async def test_shell_network_blocked_via_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("YOUMI_SANDBOX_NETWORK_BLOCKED", "1")
        result = await shell_exec("curl http://example.com", str(tmp_path))
        assert "拦截" in result

    async def test_shell_normal_command(self, tmp_path):
        result = await shell_exec("echo sandbox_ok", str(tmp_path))
        assert "sandbox_ok" in result
        assert "[exit_code: 0]" in result

    async def test_file_ops_allowed_roots(self, tmp_path):
        from youmi.tools.file_ops import file_write

        allowed = tmp_path / "allowed"
        allowed.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        configure_sandbox(Sandbox(SandboxPolicy(allowed_roots=[str(allowed)])))

        # 允许目录内 → 正常写入
        r1 = await file_write("a.txt", "hi", str(allowed))
        assert "成功" in r1

        # 目录外 → 沙箱拒绝
        with pytest.raises(PermissionError):
            await file_write("b.txt", "hi", str(outside))
