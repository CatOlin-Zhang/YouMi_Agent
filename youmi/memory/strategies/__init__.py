"""
记忆策略注册与动态加载

预置策略:
- "full"    → FullMemoryStrategy    (全量管理)
- "summary" → SummaryMemoryStrategy (对话摘要)
- "lstm"    → LSTMMemoryStrategy    (长短时记忆)

自定义策略:
    用户可传入 .py 文件路径，框架自动加载其中的 MemoryStrategy 子类。
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable, Awaitable

from youmi.memory.strategies.base import MemoryStrategy
from youmi.memory.strategies.full import FullMemoryStrategy
from youmi.memory.strategies.summary import SummaryMemoryStrategy
from youmi.memory.strategies.lstm import LSTMMemoryStrategy

# LLM 调用函数签名
LLMCallFn = Callable[[list[dict[str, str]]], Awaitable[str]]

# ---------------------------------------------------------------------------
# 预置策略注册表
# ---------------------------------------------------------------------------

_BUILTIN_STRATEGIES: dict[str, type[MemoryStrategy]] = {
    "full": FullMemoryStrategy,
    "summary": SummaryMemoryStrategy,
    "lstm": LSTMMemoryStrategy,
}


def list_strategies() -> list[str]:
    """列出所有已注册的记忆策略名称"""
    return list(_BUILTIN_STRATEGIES.keys())


def register_strategy(name: str, strategy_cls: type[MemoryStrategy]) -> None:
    """手动注册一个自定义策略类"""
    _BUILTIN_STRATEGIES[name] = strategy_cls


# ---------------------------------------------------------------------------
# 策略工厂
# ---------------------------------------------------------------------------

def create_strategy(
    strategy: str,
    agent_id: str,
    config: dict[str, Any] | None = None,
    llm_call: LLMCallFn | None = None,
) -> MemoryStrategy:
    """根据策略名称创建 MemoryStrategy 实例

    Args:
        strategy: 策略名称 ("full" / "summary" / "lstm") 或自定义策略的 .py 文件路径
        agent_id: Agent 唯一 ID
        config: 策略配置参数
        llm_call: LLM 调用函数 (summary 和 lstm 策略使用)

    Returns:
        MemoryStrategy 实例

    Raises:
        ValueError: 策略名称不存在且不是有效文件路径
        ImportError: 从文件加载策略失败

    Examples::

        # 预置策略
        strategy = create_strategy("full", agent_id="a1")

        # 预置策略 + LLM
        strategy = create_strategy("summary", agent_id="a1", llm_call=my_llm)

        # 自定义策略文件
        strategy = create_strategy(
            "/path/to/my_strategy.py",
            agent_id="a1",
            config={"custom_param": "value"},
        )
    """
    # 检查是否为文件路径
    path = Path(strategy)
    if path.is_file() and path.suffix == ".py":
        return _load_strategy_from_file(path, agent_id, config)

    # 预置策略查找
    strategy_cls = _BUILTIN_STRATEGIES.get(strategy)
    if strategy_cls is None:
        available = ", ".join(sorted(_BUILTIN_STRATEGIES.keys()))
        raise ValueError(
            f"未知记忆策略 '{strategy}'。"
            f"预置策略: [{available}]，或传入自定义策略的 .py 文件路径。"
        )

    # 实例化 — summary 和 lstm 接受 llm_call 参数
    if strategy in ("summary", "lstm"):
        return strategy_cls(agent_id=agent_id, config=config, llm_call=llm_call)  # type: ignore[call-arg]
    return strategy_cls(agent_id=agent_id, config=config)


def _load_strategy_from_file(
    file_path: Path,
    agent_id: str,
    config: dict[str, Any] | None = None,
) -> MemoryStrategy:
    """从 .py 文件动态加载 MemoryStrategy 子类并实例化

    加载规则:
    1. 动态导入指定 .py 文件
    2. 在模块中查找 MemoryStrategy 的子类
    3. 如果有多个子类，选择第一个非抽象子类
    4. 实例化并返回
    """
    module_name = f"_youmi_custom_strategy_{file_path.stem}"

    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载策略文件: {file_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    # 查找 MemoryStrategy 子类
    strategy_classes: list[type[MemoryStrategy]] = []
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (
            isinstance(attr, type)
            and issubclass(attr, MemoryStrategy)
            and attr is not MemoryStrategy
        ):
            strategy_classes.append(attr)

    if not strategy_classes:
        raise ImportError(
            f"文件 {file_path} 中未找到 MemoryStrategy 的子类。"
            f"请确保定义了继承自 youmi.memory.strategies.base.MemoryStrategy 的类。"
        )

    strategy_cls = strategy_classes[0]
    return strategy_cls(agent_id=agent_id, config=config)


__all__ = [
    "MemoryStrategy",
    "FullMemoryStrategy",
    "SummaryMemoryStrategy",
    "LSTMMemoryStrategy",
    "create_strategy",
    "list_strategies",
    "register_strategy",
    "LLMCallFn",
]
