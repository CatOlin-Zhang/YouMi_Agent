"""
认证与授权 (M1: P0 安全)

配置启用式认证:
- 未配置任何 token 时（``YOUMI_AUTH_TOKEN`` / ``YOUMI_AUTH_TOKENS`` 为空）
  零摩擦放行（``validate`` 返回匿名 admin 主体），保持现有行为
- 配置 token 后，总线 / 网关强制校验（``hmac.compare_digest`` 恒时比较）
- 支持多 token 与角色（admin / agent / viewer）
- 支持多租户：token 可绑定 tenant，认证主体携带 tenant
  用于会话/记忆隔离（未绑定 = ``default`` 租户）

环境变量:
- ``YOUMI_AUTH_TOKEN``: 单 token（配合 ``YOUMI_AUTH_ROLE``，默认 agent）
- ``YOUMI_AUTH_ROLE``: 单 token 模式下的角色（admin/agent/viewer）
- ``YOUMI_AUTH_TENANT``: 单 token 模式下的租户（默认 default）
- ``YOUMI_AUTH_TOKENS``: 多 token 列表，格式
  ``token:role[:name[:tenant]];token2:role2``
  （与单 token 配置合并；重复 token 只保留一次）

用法::

    from youmi.security import get_auth_manager

    auth = get_auth_manager()

    if auth.enabled:
        principal = auth.validate(token)      # None = 拒绝
    else:
        principal = auth.validate("")         # 未启用 → 匿名 admin 主体
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from enum import Enum

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class AuthRole(str, Enum):
    """认证角色"""

    ADMIN = "admin"      # 完全权限（管理操作、审计查询）
    AGENT = "agent"      # Agent 正常通信
    VIEWER = "viewer"    # 只读观察


class AuthToken(BaseModel):
    """token 条目（配置项）"""

    token: str
    role: AuthRole = AuthRole.AGENT
    name: str = ""
    tenant: str = "default"  # 多租户: 该 token 绑定的租户标识

    model_config = {"frozen": True}


class Principal(BaseModel):
    """通过认证的主体（不携带原始 token）"""

    role: AuthRole
    name: str = ""
    tenant: str = "default"  # 多租户: 主体所属租户（会话/记忆隔离依据）
    token_fingerprint: str = ""

    model_config = {"frozen": True}


def _fingerprint(token: str) -> str:
    """token 指纹（SHA-256 前 8 位，用于审计与调试，不泄漏原始值）"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


class AuthManager:
    """token → Principal 校验器

    未配置 token 时 ``enabled=False``，``validate`` 恒返回匿名 admin 主体
    （零摩擦模式）；配置后强制校验，无效 token 返回 None。
    """

    def __init__(self, tokens: list[AuthToken] | None = None) -> None:
        self._tokens: list[AuthToken] = list(tokens or [])
        # 明文 token 仅存于内存用于恒时比较；日志/审计只用指纹
        self._token_map: dict[str, AuthToken] = {t.token: t for t in self._tokens}

    @property
    def enabled(self) -> bool:
        """是否启用认证（配置了至少一个 token）"""
        return len(self._tokens) > 0

    @property
    def token_count(self) -> int:
        return len(self._tokens)

    def validate(self, token: str | None) -> Principal | None:
        """校验 token

        Returns:
            Principal（通过）或 None（拒绝）
        """
        if not self.enabled:
            # 未启用认证 → 零摩擦放行
            return Principal(role=AuthRole.ADMIN, name="anonymous")

        if not token:
            return None

        for known_token, entry in self._token_map.items():
            if hmac.compare_digest(token, known_token):
                return Principal(
                    role=entry.role,
                    name=entry.name,
                    tenant=entry.tenant,
                    token_fingerprint=_fingerprint(token),
                )
        return None

    @staticmethod
    def has_role(principal: Principal | None, *roles: AuthRole) -> bool:
        """判断主体是否具备指定角色之一（roles 为空 = 仅要求已认证）"""
        if principal is None:
            return False
        if not roles:
            return True
        return principal.role in roles

    def __repr__(self) -> str:
        return f"<AuthManager enabled={self.enabled} tokens={len(self._tokens)}>"


# ---------------------------------------------------------------------------
# 环境变量配置与进程级单例
# ---------------------------------------------------------------------------

def _parse_role(raw: str) -> AuthRole:
    """解析角色字符串（无效值回退为 agent）"""
    try:
        return AuthRole(raw.strip().lower())
    except ValueError:
        return AuthRole.AGENT


def auth_from_env() -> AuthManager:
    """从环境变量构建 AuthManager

    - ``YOUMI_AUTH_TOKENS``: ``token:role[:name[:tenant]];token2:role2``
    - ``YOUMI_AUTH_TOKEN`` + ``YOUMI_AUTH_ROLE`` + ``YOUMI_AUTH_TENANT``:
      单 token（与多 token 配置合并）
    """
    tokens: list[AuthToken] = []

    multi = os.environ.get("YOUMI_AUTH_TOKENS", "").strip()
    if multi:
        for item in multi.split(";"):
            item = item.strip()
            if not item:
                continue
            parts = item.split(":")
            raw_token = parts[0].strip()
            if not raw_token:
                continue
            role = _parse_role(parts[1]) if len(parts) > 1 else AuthRole.AGENT
            name = parts[2].strip() if len(parts) > 2 else ""
            tenant = parts[3].strip() if len(parts) > 3 else "default"
            tokens.append(AuthToken(
                token=raw_token, role=role, name=name,
                tenant=tenant or "default",
            ))

    single = os.environ.get("YOUMI_AUTH_TOKEN", "").strip()
    if single and not any(t.token == single for t in tokens):
        role = _parse_role(os.environ.get("YOUMI_AUTH_ROLE", ""))
        tenant = os.environ.get("YOUMI_AUTH_TENANT", "").strip() or "default"
        tokens.append(AuthToken(
            token=single, role=role, name="", tenant=tenant,
        ))

    return AuthManager(tokens)


_default: AuthManager | None = None


def get_auth_manager() -> AuthManager:
    """获取进程级默认认证管理器（懒初始化，读取 ``YOUMI_AUTH_*`` 环境变量）"""
    global _default
    if _default is None:
        _default = auth_from_env()
    return _default


def configure_auth_manager(manager: AuthManager) -> None:
    """替换进程级默认认证管理器（启动配置用）"""
    global _default
    _default = manager


def reset_auth_manager() -> None:
    """清空进程级默认实例（测试隔离用）"""
    global _default
    _default = None
