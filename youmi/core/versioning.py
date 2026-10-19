"""
工具版本号规则 — 单一规则源 (Version Policy)

版本号格式: ``MAJOR.MINOR.PATCH`` (主版本号.次版本号.修订号)

- MAJOR 主版本号: 添加了不兼容下层的修改时提升
- MINOR 次版本号: 添加兼容下层的功能时提升
- PATCH 修订号: 仅修复 bug, 不涉及功能上的变化

阶段 (stage):
- CANDIDATE 候选库: 0.x.y — 系统自生成工具的孵化期
- STABLE    正式库: >= 1.0.0 — 首个正式版固定为 1.0.0

来源 (origin) 与初始版本:
- EXTERNAL  外部接入 (MCP Server / 外部 Skill 及其携带的工具)
            → 直接以 1.0.0 进入正式库 (外部已保证可用性)
- GENERATED 系统自生成 → 0.0.1 候选; 自动测试通过 + 任务中连续成功
            调用达到阈值后由 ``ToolStore.promote_to_stable()`` 晋升为 1.0.0
            (``PostTaskPipeline`` 负责成功计数与自动测试门禁)

0.x 阶段遵循 semver 约定: 破坏性变更只提升次版本号 (0.(y+1).0),
不会自动跨过 1.0.0；进入正式库的唯一出口是显式晋升
(``promote_to_stable`` / ``mark_external``)。

回归修复: 测试或使用中发现回归 bug 时, 先收集现场信息与历史提交
(版本链 + git 历史), 自动修复重试 ``AUTO_FIX_MAX_ATTEMPTS`` 次,
仍无法解决才转人工审批。
"""

from __future__ import annotations

from enum import Enum

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 系统自生成工具的初始版本 (候选库)
GENERATED_INITIAL_VERSION = "0.0.1"

#: 首个正式版本号 (外部接入默认 / 晋升目标)
STABLE_INITIAL_VERSION = "1.0.0"

#: 晋升正式版所需的连续成功调用次数 (同一工具, 窗口内)
PROMOTION_MIN_SUCCESSES = 3

#: 晋升窗口内允许的失败次数 (0 = 必须零错误)
PROMOTION_MAX_FAILURES = 0

#: 回归 bug 自动修复的重试上限, 超过后转人工
AUTO_FIX_MAX_ATTEMPTS = 3

#: 现场信息中保留的历史提交条数上限
EVIDENCE_HISTORY_LIMIT = 5


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class ToolOrigin(str, Enum):
    """工具来源 — 决定初始版本号与是否允许自动晋升"""

    EXTERNAL = "external"    # 外部接入: MCP Server / 外部 Skill 及其携带工具
    GENERATED = "generated"  # 系统自生成: 内置工具 / 自动修复生成


class VersionStage(str, Enum):
    """版本阶段 — 以主版本号是否达到 1 划分"""

    CANDIDATE = "candidate"  # 候选库: 0.x.y (孵化期, 可自动晋升)
    STABLE = "stable"        # 正式库: >= 1.0.0


class ChangeKind(str, Enum):
    """变更类型 — 决定版本号提升位"""

    BREAKING = "breaking"  # 主版本号: 不兼容下层的修改
    FEATURE = "feature"    # 次版本号: 兼容下层的功能新增
    FIX = "fix"            # 修订号: 仅修复 bug


#: ChangeKind → bump_version 的 bump_type 映射 (1.x+ 语义)
_CHANGE_TO_BUMP = {
    ChangeKind.BREAKING.value: "major",
    ChangeKind.FEATURE.value: "minor",
    ChangeKind.FIX.value: "patch",
}


# ---------------------------------------------------------------------------
# 解析与比较
# ---------------------------------------------------------------------------

def parse_semver(version: str) -> tuple[int, int, int] | None:
    """解析 ``MAJOR.MINOR.PATCH``; 非法格式返回 None"""
    if not isinstance(version, str):
        return None
    parts = version.strip().split(".")
    if len(parts) != 3:
        return None
    try:
        major, minor, patch = (int(p) for p in parts)
    except ValueError:
        return None
    if major < 0 or minor < 0 or patch < 0:
        return None
    return major, minor, patch


def is_valid_semver(version: str) -> bool:
    """是否为合法的 ``MAJOR.MINOR.PATCH``"""
    return parse_semver(version) is not None


def compare_versions(left: str, right: str) -> int:
    """比较版本号大小 — 返回 -1 / 0 / 1 (非法版本视为最小)"""
    l = parse_semver(left)
    r = parse_semver(right)
    if l is None and r is None:
        return 0
    if l is None:
        return -1
    if r is None:
        return 1
    return (l > r) - (l < r)


# ---------------------------------------------------------------------------
# 来源 / 阶段
# ---------------------------------------------------------------------------

def normalize_origin(origin: str | ToolOrigin | None) -> str:
    """归一化来源标识 (未知值按系统自生成处理)"""
    if isinstance(origin, ToolOrigin):
        return origin.value
    if origin in (ToolOrigin.EXTERNAL.value, ToolOrigin.GENERATED.value):
        return str(origin)
    return ToolOrigin.GENERATED.value


def initial_version(origin: str | ToolOrigin | None = None) -> str:
    """按来源返回初始版本号

    - 外部接入 → 1.0.0 (首个正式版)
    - 系统自生成 → 0.0.1 (候选库)
    """
    if normalize_origin(origin) == ToolOrigin.EXTERNAL.value:
        return STABLE_INITIAL_VERSION
    return GENERATED_INITIAL_VERSION


def resolve_initial_version(
    declared: str | None,
    origin: str | ToolOrigin | None = None,
) -> str:
    """结合声明版本与来源解析初始版本

    外部接入的工具未显式声明版本 (空或占位默认 0.0.1) 时提升为
    1.0.0; 其余按声明值, 非法值回落到来源初始版本。
    """
    origin_value = normalize_origin(origin)
    if origin_value == ToolOrigin.EXTERNAL.value and declared in (
        None, "", GENERATED_INITIAL_VERSION,
    ):
        return STABLE_INITIAL_VERSION
    if not declared:
        return initial_version(origin_value)
    return declared if is_valid_semver(declared) else initial_version(origin_value)


def stage_of(version: str) -> str:
    """按版本号判定阶段: 主版本号 >= 1 → stable, 其余 → candidate"""
    parsed = parse_semver(version)
    if parsed is None:
        return VersionStage.CANDIDATE.value
    return (
        VersionStage.STABLE.value if parsed[0] >= 1
        else VersionStage.CANDIDATE.value
    )


def is_stable(version: str) -> bool:
    """是否正式库版本 (>= 1.0.0)"""
    return stage_of(version) == VersionStage.STABLE.value


def is_candidate(version: str) -> bool:
    """是否候选库版本 (0.x.y)"""
    return stage_of(version) == VersionStage.CANDIDATE.value


# ---------------------------------------------------------------------------
# 版本提升
# ---------------------------------------------------------------------------

def bump_type_for(change_kind: str | ChangeKind | None) -> str:
    """变更类型 → bump_type; 未知值按 patch (仅修订) 处理"""
    if isinstance(change_kind, ChangeKind):
        return _CHANGE_TO_BUMP[change_kind.value]
    return _CHANGE_TO_BUMP.get(str(change_kind or ""), "patch")


def bump_for(current: str, change_kind: str | ChangeKind | None) -> str:
    """按变更类型提升版本号

    0.x 阶段 (候选库) 的破坏性变更只提升次版本号 — 遵循 semver
    "主版本号为 0 时任何内容都可能随时改变" 的约定, 避免绕过
    晋升流程直接进入 1.0.0。
    """
    parsed = parse_semver(current)
    if parsed is None:
        return GENERATED_INITIAL_VERSION

    major, minor, patch = parsed
    bump_type = bump_type_for(change_kind)
    if bump_type == "major" and major == 0:
        bump_type = "minor"

    if bump_type == "major":
        return f"{major + 1}.0.0"
    if bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def is_promotion_eligible(
    success_count: int,
    failure_count: int = 0,
    min_successes: int = PROMOTION_MIN_SUCCESSES,
    max_failures: int = PROMOTION_MAX_FAILURES,
) -> bool:
    """候选工具是否满足晋升正式库的条件 (连续成功且零错误)"""
    return success_count >= min_successes and failure_count <= max_failures
