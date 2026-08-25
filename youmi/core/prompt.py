"""
System Prompt 动态组装引擎

参考 OpenClaw 的多层 prompt 拼接机制，将静态 system_prompt 升级为分层组装。

Prompt 分层设计:
- base: Agent 身份与核心指令 (最高优先级，永远不被截断)
- skills: 可用 Skill/能力说明
- context: 上下文信息 (知识库摘要、文件注入)
- runtime: 运行时动态注入 (任务描述、环境变量)
- overrides: per-run 覆盖 (临时指令)

每层有:
- priority: 截断优先级 (值越大越先被截断)
- token_budget: 该层允许的最大 token 数 (0 = 不限制)

组装流程:
1. 按 priority 降序排列 (低 priority 最重要)
2. 高优先级层完整保留
3. 当总 token 超出预算时，从低优先级（高 priority 值）开始截断
4. 与 Compaction 协同: Compaction 只压缩 conversation，不动 system prompt
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Token 估算工具
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """估算文本的 token 数量

    使用简单的字符除法近似:
    - 英文: 约 1 token / 4 字符
    - 中文: 约 1 token / 1.5-2 字符
    - 混合场景: 约 1 token / 3.5 字符 (折中)

    这是一个保守估计，实际 token 数可能更少。
    """
    if not text:
        return 0
    # 统计中文字符比例
    chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    total_chars = len(text)

    if total_chars == 0:
        return 0

    chinese_ratio = chinese_chars / total_chars

    # 根据中英文比例动态调整
    if chinese_ratio > 0.5:
        # 以中文为主: 约 1 token / 2 字符
        return max(1, int(total_chars / 2))
    elif chinese_ratio > 0.1:
        # 混合: 约 1 token / 3 字符
        return max(1, int(total_chars / 3))
    else:
        # 以英文为主: 约 1 token / 4 字符
        return max(1, int(total_chars / 4))


# ---------------------------------------------------------------------------
# Prompt 层定义
# ---------------------------------------------------------------------------

class PromptLayer(BaseModel):
    """Prompt 层定义

    每个层代表 system prompt 的一个组成部分。

    Args:
        name: 层标识 (如 "base", "skills", "context", "runtime", "override")
        content: 层内容文本
        priority: 截断优先级，值越大越先被截断 (0 = 最高优先级，永不被截断)
        token_budget: 该层允许的最大 token 数 (0 = 不限制)
    """

    name: str = Field(description="层标识名称")
    content: str = Field(default="", description="层内容文本")
    priority: int = Field(
        default=50, ge=0, le=100,
        description="截断优先级: 值越大越先被截断 (0=永不被截断)",
    )
    token_budget: int = Field(
        default=0, ge=0,
        description="该层最大 token 数 (0=不限制)",
    )

    model_config = {"frozen": True}

    @property
    def estimated_tokens(self) -> int:
        """估算本层的 token 数"""
        return estimate_tokens(self.content)


# ---------------------------------------------------------------------------
# Prompt 组装器
# ---------------------------------------------------------------------------

class PromptAssembler:
    """System Prompt 动态组装器

    将多个 PromptLayer 按优先级组装为最终的 system prompt 字符串。
    当总 token 超出预算时，从低优先级层开始截断。

    标准分层优先级 (约定):
    - 0:  base (Agent 身份，永不被截断)
    - 20: skills (能力说明)
    - 40: context (上下文注入)
    - 60: runtime (运行时信息)
    - 80: overrides (per-run 覆盖)

    用法::

        assembler = PromptAssembler()
        assembler.add_layer(PromptLayer(name="base", content="你是一个...", priority=0))
        assembler.add_layer(PromptLayer(name="context", content="当前项目...", priority=40))

        system_prompt = assembler.assemble(max_tokens=2000)
    """

    # 标准层名称 → 默认优先级映射
    STANDARD_PRIORITIES: dict[str, int] = {
        "base": 0,
        "identity": 0,
        "skills": 20,
        "capabilities": 20,
        "context": 40,
        "knowledge": 40,
        "runtime": 60,
        "task": 60,
        "overrides": 80,
        "override": 80,
    }

    def __init__(self) -> None:
        self._layers: list[PromptLayer] = []

    def add_layer(self, layer: PromptLayer) -> None:
        """添加一个 Prompt 层

        如果同名层已存在，则替换。
        """
        # 同名替换
        self._layers = [l for l in self._layers if l.name != layer.name]
        self._layers.append(layer)

    def remove_layer(self, name: str) -> bool:
        """移除指定名称的层

        Returns:
            是否成功移除
        """
        before = len(self._layers)
        self._layers = [l for l in self._layers if l.name != name]
        return len(self._layers) < before

    def get_layer(self, name: str) -> PromptLayer | None:
        """获取指定名称的层"""
        for layer in self._layers:
            if layer.name == name:
                return layer
        return None

    @property
    def layers(self) -> list[PromptLayer]:
        """所有已注册的层（按 priority 升序排列）"""
        return sorted(self._layers, key=lambda l: l.priority)

    def assemble(self, max_tokens: int = 0) -> str:
        """组装所有层为最终的 system prompt

        组装算法:
        1. 按 priority 升序排列 (低 priority = 高重要性)
        2. 计算每层的 token 数
        3. 如果总 token <= max_tokens，完整拼接
        4. 如果超预算，从最高 priority (最不重要) 开始截断
        5. priority=0 的层永不被截断

        Args:
            max_tokens: 最大 token 预算 (0 = 不限制)

        Returns:
            组装后的 system prompt 字符串
        """
        if not self._layers:
            return ""

        # 按 priority 升序排列: 最重要的在前面
        sorted_layers = sorted(self._layers, key=lambda l: l.priority)

        # 先应用每层自身的 token_budget
        processed: list[tuple[PromptLayer, int]] = []
        for layer in sorted_layers:
            tokens = layer.estimated_tokens
            if layer.token_budget > 0 and tokens > layer.token_budget:
                # 按 token_budget 截断该层内容
                tokens = layer.token_budget
            processed.append((layer, tokens))

        # 计算总 token
        total_tokens = sum(tokens for _, tokens in processed)

        if max_tokens <= 0 or total_tokens <= max_tokens:
            # 无需截断，直接拼接
            return self._join_layers(sorted_layers)

        # 需要截断: 从后往前 (高 priority = 不重要 = 先截断)
        budget = max_tokens
        result_layers: list[PromptLayer] = []

        # 第一遍: 为 priority=0 的层预留空间
        reserved_tokens = sum(
            tokens for layer, tokens in processed if layer.priority == 0
        )
        remaining = budget - reserved_tokens

        # 第二遍: 分配空间
        for layer, tokens in processed:
            if layer.priority == 0:
                # 永不被截断
                result_layers.append(layer)
                continue

            if remaining <= 0:
                # 预算耗尽，跳过此层
                logger.debug(
                    "Prompt layer '%s' dropped (budget exhausted)",
                    layer.name,
                )
                continue

            if tokens <= remaining:
                # 本层可以完整放入
                result_layers.append(layer)
                remaining -= tokens
            else:
                # 部分截断
                # 按字符比例截断 (近似)
                keep_ratio = remaining / tokens if tokens > 0 else 0
                truncated_content = layer.content[:int(len(layer.content) * keep_ratio)]
                truncated_layer = PromptLayer(
                    name=layer.name,
                    content=truncated_content + "\n[... 内容已截断 ...]",
                    priority=layer.priority,
                    token_budget=layer.token_budget,
                )
                result_layers.append(truncated_layer)
                remaining = 0
                logger.debug(
                    "Prompt layer '%s' truncated (%.0f%%)",
                    layer.name, keep_ratio * 100,
                )

        return self._join_layers(result_layers)

    def _join_layers(self, layers: list[PromptLayer]) -> str:
        """将层列表拼接为最终文本"""
        parts: list[str] = []
        for layer in layers:
            if layer.content.strip():
                parts.append(layer.content.strip())
        return "\n\n".join(parts)

    @property
    def estimated_total_tokens(self) -> int:
        """所有层的总 token 估算"""
        return sum(layer.estimated_tokens for layer in self._layers)

    @classmethod
    def from_system_prompt(
        cls,
        system_prompt: str,
        extra_layers: list[PromptLayer] | None = None,
    ) -> PromptAssembler:
        """从现有 system_prompt 字符串创建组装器

        将 system_prompt 作为 base 层，可追加额外层。

        Args:
            system_prompt: 现有的系统提示词
            extra_layers: 额外层列表

        Returns:
            配置好的 PromptAssembler
        """
        assembler = cls()
        if system_prompt:
            assembler.add_layer(PromptLayer(
                name="base",
                content=system_prompt,
                priority=0,
            ))
        for layer in (extra_layers or []):
            assembler.add_layer(layer)
        return assembler

    def __repr__(self) -> str:
        layer_info = [(l.name, l.priority, l.estimated_tokens) for l in self.layers]
        return f"<PromptAssembler layers={layer_info} total_tokens={self.estimated_total_tokens}>"
