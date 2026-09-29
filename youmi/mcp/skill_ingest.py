"""
SkillIngestor — SOP Skill 文档入库器 (P2 差距 7)

解析 skill 文件头 describe (Markdown frontmatter 或显式字段, 宽容解析)
→ 由 SummaryGenerator 生成 L1 摘要与 L2 摘要的摘要 (默认截断自
describe; 注入 LLM 时生成式) → 绑定 bound_tool_name → 写入 Skill
并行库 (SkillStore)。

SOP/Tool 二分 (用户指定):
- describe + 正文步骤 → Skill 库 (SkillStore);
- 文档中声明的工具段落 (frontmatter tools 列表或正文代码块标记)
  仅作为 bound_tool_name 绑定引用 — Tool 本身仍由 ToolStore 既有
  链路管理, ingest 侧不创建任何 Tool。

用法::

    from youmi.mcp.skill_store import SkillStore
    from youmi.mcp.skill_ingest import SkillIngestor

    store = SkillStore()
    await store.initialize()
    ingestor = SkillIngestor(store)

    entry = await ingestor.ingest(\"\"\"---
    skill: 邮件汇报流程
    describe: 每日汇总数据并发送邮件
    tools: send_email
    ---

    1. 拉取当日数据
    2. 渲染模板并调用邮件工具
    \"\"\")
    assert entry.bound_tool_name == "send_email"
"""

from __future__ import annotations

import logging
import re
from typing import Any

from youmi.core.tool import RiskLevel
from youmi.mcp.skill_store import SkillEntry, SkillStore
from youmi.mcp.summary import SummaryGenerator

logger = logging.getLogger(__name__)

# Markdown frontmatter: ---\nkey: value\n...\n---
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# 正文中的工具引用标记 (代码块或 "工具:" 行), 宽容匹配
_TOOL_MARKER_RE = re.compile(
    r"(?:^|\n)\s*(?:```)?\s*(?:bound_)?tools?\s*[:=]\s*([\w.\-]+)",
    re.IGNORECASE,
)

# 字段宽容映射: 目标字段 → 可接受的 frontmatter 键
_KEY_ALIASES = {
    "skill_name": ("skill_name", "skill", "name", "title"),
    "describe": ("describe", "description", "summary"),
    "bound_tool_name": ("bound_tool", "tool", "tools"),
    "tags": ("tags",),
    "risk_level": ("risk", "risk_level"),
    "content": ("content", "body", "steps"),
}


def parse_skill_doc(skill_doc: str | dict[str, Any]) -> dict[str, Any]:
    """宽容解析 skill 文档 — dict 直取, 文本走 frontmatter/首行启发式

    Args:
        skill_doc: Markdown 文本 (含/不含 frontmatter) 或显式字段 dict

    Returns:
        {skill_name, describe, bound_tool_name, tags, risk_level,
         content} — 缺省字段以空值/默认值填充
    """
    if isinstance(skill_doc, dict):
        parsed = _from_mapping(skill_doc)
        # dict 输入的 content 缺省: 序列化自身 (不含 content 键时)
        if not parsed.get("content"):
            payload = {k: v for k, v in skill_doc.items() if k != "content"}
            parsed["content"] = (
                payload.get("steps") or payload.get("body")
                or _dumps(payload)
            )
        return parsed

    text = (skill_doc or "").strip()
    meta: dict[str, str] = {}
    body = text

    match = _FRONTMATTER_RE.match(text)
    if match:
        meta = _parse_frontmatter(match.group(1))
        body = text[match.end():].strip()
    else:
        # 无 frontmatter: 首行 "key: value" 逐行探测 (宽容)
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if ":" in stripped:
                key, _, value = stripped.partition(":")
                if key.strip().lower() in {
                    alias for aliases in _KEY_ALIASES.values()
                    for alias in aliases
                }:
                    meta.setdefault(key.strip().lower(), value.strip())

    parsed = _from_mapping(meta)
    parsed["content"] = text  # 全文入库 (含 frontmatter, 保留原始信息)

    # bound_tool 兜底: 正文工具标记 (代码块 / "工具:" 行)
    if not parsed["bound_tool_name"] and body:
        m = _TOOL_MARKER_RE.search(body)
        if m:
            parsed["bound_tool_name"] = m.group(1)

    # skill_name 兜底: Markdown 标题或 describe 前缀
    if not parsed["skill_name"]:
        title = _first_heading(text) if text else ""
        base = title or parsed["describe"]
        parsed["skill_name"] = (base or "untitled_skill")[:20].strip()

    return parsed


def _dumps(payload: dict) -> str:
    import json
    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(payload)


def _parse_frontmatter(block: str) -> dict[str, str]:
    """解析 frontmatter 块 — key: value 逐行 (值支持 [a, b] 列表)"""
    meta: dict[str, str] = {}
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition(":")
        if not sep:
            continue
        meta[key.strip().lower()] = value.strip()
    return meta


def _first_heading(text: str) -> str:
    """提取首个 Markdown 标题文本 (无标题返回空串)"""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _from_mapping(mapping: dict[str, Any]) -> dict[str, Any]:
    """字段宽容映射 — 别名键 → 标准字段"""
    lower = {str(k).strip().lower(): v for k, v in mapping.items()}

    def _pick(field: str) -> Any:
        for alias in _KEY_ALIASES[field]:
            if alias in lower and lower[alias] not in (None, ""):
                return lower[alias]
        return None

    skill_name = _pick("skill_name")
    describe = _pick("describe")
    bound_tool = _pick("bound_tool_name")
    tags = _pick("tags")
    risk = _pick("risk_level")
    content = _pick("content")

    # bound_tool: "a, b" / ["a", "b"] → 首个 (一 Skill 绑定一 Tool 原始名称)
    if isinstance(bound_tool, (list, tuple)):
        bound_tool = bound_tool[0] if bound_tool else None
    elif isinstance(bound_tool, str):
        bound_tool = bound_tool.strip().strip("[]").split(",")[0].strip() or None

    # tags: "a, b" / ["a", "b"] → list[str]
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.strip().strip("[]").split(",") if t.strip()]
    elif not isinstance(tags, list):
        tags = []

    risk = risk if risk in ("low", "medium", "high", "critical") else RiskLevel.LOW

    return {
        "skill_name": str(skill_name or "").strip(),
        "describe": str(describe or "").strip(),
        "bound_tool_name": str(bound_tool or "").strip(),
        "tags": [str(t) for t in tags],
        "risk_level": risk,
        "content": str(content or ""),
    }


class SkillIngestor:
    """SOP Skill 文档入库器 — describe → L1/L2 → 绑定 → SkillStore

    Args:
        skill_store: SkillStore 并行库实例
        summary_generator: 三级摘要生成器 (默认启发式截断;
            注入 LLM 时生成式摘要, 见 youmi/mcp/summary.py)
    """

    def __init__(
        self,
        skill_store: SkillStore,
        summary_generator: SummaryGenerator | None = None,
    ) -> None:
        self._store = skill_store
        self._gen = summary_generator or SummaryGenerator()

    @property
    def skill_store(self) -> SkillStore:
        return self._store

    async def ingest(self, skill_doc: str | dict[str, Any]) -> SkillEntry:
        """解析并入库一份 skill 文档

        流程: 宽容解析文件头 describe → 生成 L1 摘要与 L2 摘要的摘要
        → 绑定 bound_tool_name (仅引用, 不创建 Tool) → upsert 入并行库。

        Args:
            skill_doc: Markdown 文本 (含/不含 frontmatter) 或显式字段 dict

        Returns:
            入库后的 SkillEntry (含生成的 L1/L2 与绑定关系)
        """
        parsed = parse_skill_doc(skill_doc)
        name = parsed["skill_name"]
        describe = parsed["describe"]

        # L1/L2 摘要: 默认截断自 describe; 注入 LLM 时生成式
        summary_l1 = await self._gen.generate_l1(name, describe)
        summary_l2 = await self._gen.generate_l2(name, summary_l1)

        entry = SkillEntry(
            skill_name=name,
            describe=describe,
            summary=summary_l1,
            summary_l2=summary_l2,
            bound_tool_name=parsed["bound_tool_name"],
            content_json=parsed["content"],
            tags=parsed["tags"],
            risk_level=parsed["risk_level"],
        )
        await self._store.upsert_skill(entry)
        logger.info(
            "SkillIngestor: ingested '%s' (bound_tool=%s)",
            name, entry.bound_tool_name or "<none>",
        )
        return entry

    def __repr__(self) -> str:
        return f"<SkillIngestor store={self._store!r}>"
