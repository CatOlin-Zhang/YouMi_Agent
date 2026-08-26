"""GUI 运行时配置。

通过环境变量可覆盖默认监听地址与默认主 Agent 名称：
- YOUMI_GUI_HOST  (默认 127.0.0.1)
- YOUMI_GUI_PORT  (默认 8766)
- YOUMI_GUI_MASTER (默认 master，对应 youmi/agents/<name>/config.yaml)
- YOUMI_GUI_MCP    (默认 1，启用 MCP 工具调用层)
- YOUMI_GUI_BUS    (默认 1，启用进程内消息总线)
- YOUMI_GUI_VAULT  (默认 1，启用 ToolVault + ToolStore sqlite-vec 方案)
"""

from __future__ import annotations

import os
from pathlib import Path


class GUIConfig:
    """GUI 进程级配置。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8766,
        master_agent_name: str = "master",
        mcp_enabled: bool = True,
        bus_enabled: bool = True,
        vault_enabled: bool = True,
        vault_db_path: str = "",
        embedding_base_url: str = "http://localhost:11434/v1",
        embedding_model: str = "nomic-embed-text",
    ) -> None:
        self.host = host
        self.port = port
        self.master_agent_name = master_agent_name
        self.mcp_enabled = mcp_enabled
        self.bus_enabled = bus_enabled
        self.vault_enabled = vault_enabled
        self.vault_db_path = vault_db_path
        self.embedding_base_url = embedding_base_url
        self.embedding_model = embedding_model

        here = Path(__file__).resolve().parent
        self.package_dir = str(here)
        self.static_dir = str(here / "static")
        self.data_dir = str(here / "data")
        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(os.path.join(self.data_dir, "messages"), exist_ok=True)


def load_config() -> GUIConfig:
    """从环境变量读取配置。"""
    host = os.environ.get("YOUMI_GUI_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("YOUMI_GUI_PORT", "8766"))
    except ValueError:
        port = 8766
    master = os.environ.get("YOUMI_GUI_MASTER", "master")
    mcp_enabled = os.environ.get("YOUMI_GUI_MCP", "1") == "1"
    bus_enabled = os.environ.get("YOUMI_GUI_BUS", "1") == "1"
    vault_enabled = os.environ.get("YOUMI_GUI_VAULT", "1") == "1"
    vault_db_path = os.environ.get("YOUMI_GUI_VAULT_DB", "")
    embedding_base_url = os.environ.get(
        "YOUMI_GUI_EMBEDDING_URL", "http://localhost:11434/v1"
    )
    embedding_model = os.environ.get(
        "YOUMI_GUI_EMBEDDING_MODEL", "nomic-embed-text"
    )
    return GUIConfig(
        host=host, port=port, master_agent_name=master,
        mcp_enabled=mcp_enabled, bus_enabled=bus_enabled,
        vault_enabled=vault_enabled, vault_db_path=vault_db_path,
        embedding_base_url=embedding_base_url,
        embedding_model=embedding_model,
    )
