"""安全模块 (M1: P0) — 策略式沙箱 + 认证"""

from youmi.security.auth import (
    AuthManager,
    AuthRole,
    AuthToken,
    Principal,
    auth_from_env,
    configure_auth_manager,
    get_auth_manager,
    reset_auth_manager,
)
from youmi.security.sandbox import (
    Sandbox,
    SandboxPolicy,
    SandboxViolation,
    configure_sandbox,
    get_sandbox,
    policy_from_env,
    reset_sandbox,
)

__all__ = [
    # 认证
    "AuthManager",
    "AuthRole",
    "AuthToken",
    "Principal",
    "auth_from_env",
    "configure_auth_manager",
    "get_auth_manager",
    "reset_auth_manager",
    # 沙箱
    "Sandbox",
    "SandboxPolicy",
    "SandboxViolation",
    "configure_sandbox",
    "get_sandbox",
    "policy_from_env",
    "reset_sandbox",
]
