"""
Agent 配置目录模块

每个 Agent 在 youmi/agents/<agent_name>/ 下拥有独立的配置子目录，结构约定：

    youmi/agents/
    ├── __init__.py
    ├── master/                 # MasterAgent 配置
    │   └── config.yaml
    ├── coder/                  # 示例：Coder Agent 配置
    │   └── config.yaml
    └── ...                     # 其他 Agent

提供:
- get_agent_dir(): 获取指定 Agent 的配置目录路径
- list_agents(): 列出所有已配置的 Agent
- load_agent_config(): 从 YAML 文件加载 AgentConfig
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Agent 配置根目录: youmi/agents/
AGENTS_DIR = Path(__file__).parent


def get_agent_dir(agent_name: str) -> Path:
    """获取指定 Agent 的配置目录路径

    Args:
        agent_name: Agent 名称（对应子目录名）

    Returns:
        Agent 配置目录的 Path 对象（不保证存在）
    """
    return AGENTS_DIR / agent_name


def list_agents() -> list[str]:
    """列出所有已配置的 Agent 名称

    扫描 youmi/agents/ 下的子目录，排除 __pycache__ 等。
    """
    return [
        d.name
        for d in AGENTS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(("_", "."))
    ]


def load_agent_config(agent_name: str) -> dict[str, Any]:
    """从 YAML 文件加载 Agent 原始配置字典

    查找 youmi/agents/<agent_name>/config.yaml。
    返回原始字典，调用方负责转换为 AgentConfig。

    Args:
        agent_name: Agent 名称

    Returns:
        配置字典

    Raises:
        FileNotFoundError: 配置文件不存在
    """
    import yaml

    config_path = get_agent_dir(agent_name) / "config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(
            f"Agent 配置文件不存在: {config_path}"
        )

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    logger.debug("Loaded config for agent '%s' from %s", agent_name, config_path)
    return data
