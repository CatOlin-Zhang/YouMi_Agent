"""MCP 服务层 — GUI 与核心 MCP 系统的桥接。

职责：
1. 创建并持有全局 MCPServer 实例（所有 Agent 共享）
2. 注册 BuiltinToolProvider（包装全部内置工具）
3. 为 MasterAgent 和子 Agent 提供 connect_mcp() 接入点
4. 集成 ToolStore (SQLite) + ToolVault (向量搜索) 实现工具持久化与语义发现
5. 暴露工具列表查询接口（供前端面板渲染）

架构：
    EngineBridge
    └── MCPService
         ├── MCPServer（全局单例）
         │    └── BuiltinToolProvider（内置工具）
         ├── ToolStore（SQLite 持久化：tools + vec_tools + changelogs）
         ├── ToolVault（内存缓存 + 语义搜索，底层委托 ToolStore）
         ├── EmbeddingClient（Ollama /v1/embeddings 生成工具向量）
         ├── connect_agent() → 为每个 Agent 创建独立 MCPClient + ToolBridge（附带 Vault）
         └── list_tools() → 供 REST API /api/tools 调用

默认模式（sqlite-vec 方案）：
    工具描述向量化后存入 SQLite，ToolVault 作为内存一级缓存，
    ToolStore 作为持久化层。Embedding 失败时自动降级为关键词搜索。

生命周期：与 EngineBridge 一致，init() 时创建，进程结束时自动清理。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class MCPService:
    """GUI 级 MCP 服务层。

    一个 GUI 进程只有一个 MCPService 实例，持有唯一的 MCPServer。
    所有 Agent（Master + Sub）共享 MCPServer，各自持有独立的 ToolBridge。

    默认启用 sqlite-vec 方案：
    - ToolStore: SQLite 持久化工具元数据 + 向量索引
    - ToolVault: 内存缓存 + 语义搜索（底层委托 ToolStore）
    - EmbeddingClient: 通过 Ollama /v1/embeddings 生成工具描述向量

    Args:
        vault_enabled: 是否启用 ToolVault + ToolStore（默认 True）
        db_path: ToolStore 数据库路径
        embedding_base_url: Embedding API 基础 URL
        embedding_model: Embedding 模型名称
    """

    def __init__(
        self,
        vault_enabled: bool = True,
        db_path: str = "",
        embedding_base_url: str = "http://localhost:11434/v1",
        embedding_model: str = "nomic-embed-text",
    ) -> None:
        self._server: Any = None           # MCPServer
        self._provider: Any = None         # BuiltinToolProvider
        self._vault: Any = None            # ToolVault
        self._store: Any = None            # ToolStore
        self._embedding_client: Any = None # EmbeddingClient
        self._vault_enabled: bool = vault_enabled
        self._db_path: str = db_path
        self._embedding_base_url: str = embedding_base_url
        self._embedding_model: str = embedding_model
        self._initialized: bool = False

    @property
    def server(self) -> Any:
        """MCPServer 实例"""
        return self._server

    @property
    def vault(self) -> Any:
        """ToolVault 实例（可能为 None）"""
        return self._vault

    @property
    def initialized(self) -> bool:
        return self._initialized

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    async def setup(self, master: Any) -> None:
        """初始化 MCP 服务并连接 MasterAgent。

        流程：
        1. 创建 MCPServer
        2. 创建并注册 BuiltinToolProvider（全部内置工具）
        3. 启动 MCPServer
        4. [Vault] 创建 ToolStore + EmbeddingClient + ToolVault
        5. [Vault] 从 Provider 导入工具到 Vault（向量化 + 入库）
        6. 为 MasterAgent 调用 connect_mcp()（附带 Vault 引用）

        Args:
            master: MasterAgent 实例（尚未 initialize）
        """
        from youmi.mcp.server import MCPServer
        from youmi.tools.builtin import BuiltinToolProvider

        # 1. 创建 MCPServer
        self._server = MCPServer()

        # 2. 创建 BuiltinToolProvider（全量注册，不排除任何工具）
        work_dir = getattr(master, 'env', '.') or '.'
        self._provider = BuiltinToolProvider(work_dir=work_dir)

        # 3. 注册 Provider 到 Server
        await self._server.register_provider(self._provider)
        await self._server.start()

        # 4. Vault 初始化（sqlite-vec 方案）
        if self._vault_enabled:
            await self._init_vault(work_dir)

        # 5. 为 MasterAgent 连接 MCP
        #    connect_mcp() 内部会创建 MCPClient + ToolBridge，
        #    并将已有 ToolRegistry 工具迁移到 Provider。
        #    builtin_tools=False 因为我们已通过 BuiltinToolProvider 全量注册。
        master.connect_mcp(
            server=self._server,
            provider_id=f"master-{master.agent_id[:8]}",
            builtin_tools=False,
        )

        # 6. 将 Master 的 Agent 级工具（协调器工具）导入 Vault
        #    connect_mcp() 将 ToolRegistry 中的协调器工具迁移到了
        #    LocalFunctionProvider（master._mcp_provider），但 Vault 中还没有。
        #    当 ToolBridge.to_openai_tools() 优先使用 Vault 时，
        #    这些工具对 LLM 不可见——必须补充导入。
        if self._vault is not None:
            agent_provider = getattr(master, '_mcp_provider', None)
            if agent_provider is not None:
                await self._import_agent_tools_to_vault(agent_provider)

        # 7. 将 Vault 注入 MasterAgent 的 ToolBridge
        if self._vault is not None and master._tool_bridge is not None:
            master._tool_bridge._vault = self._vault

        self._initialized = True
        logger.info(
            "MCPService 初始化完成: %d 个工具已注册到 MCPServer, vault=%s",
            self._server.tool_count,
            'yes' if self._vault else 'no',
        )

    # ------------------------------------------------------------------
    # Vault 初始化（sqlite-vec 方案）
    # ------------------------------------------------------------------

    async def _init_vault(self, work_dir: str) -> None:
        """初始化 ToolStore + EmbeddingClient + ToolVault。

        优雅降级策略：
        - EmbeddingClient 创建失败 → Vault 退化为关键词搜索
        - ToolStore 初始化失败 → Vault 退化为纯内存模式
        - 任何异常不阻塞 MCP 主流程
        """
        from youmi.mcp.vault import ToolVault

        embedding_client = None
        store = None

        # EmbeddingClient
        try:
            from youmi.llm.embeddings import EmbeddingClient
            embedding_client = EmbeddingClient(
                base_url=self._embedding_base_url,
                model=self._embedding_model,
            )
            logger.info("EmbeddingClient 已创建: model=%s", self._embedding_model)
        except Exception as exc:
            logger.warning("EmbeddingClient 创建失败，Vault 退化为关键词搜索: %s", exc)

        # ToolStore (SQLite 持久化)
        try:
            from youmi.mcp.tool_store import ToolStore
            db_path = self._db_path or ".youmi_tools.db"
            store = ToolStore(db_path=db_path, embedding_client=embedding_client)
            await store.initialize()
            logger.info("ToolStore 已初始化: %s", db_path)
        except Exception as exc:
            logger.warning("ToolStore 初始化失败，Vault 退化为纯内存模式: %s", exc)
            store = None

        # ToolVault (内存缓存 + 语义搜索)
        self._vault = ToolVault(
            embedding_client=embedding_client,
            store=store,
        )
        self._store = store
        self._embedding_client = embedding_client

        # 从 Provider 批量导入工具到 Vault（自动生成向量 + 入库）
        try:
            await self._vault.add_tools_from_provider(self._provider)
            logger.info(
                "ToolVault: %d 个工具已导入（store=%s, embedding=%s）",
                len(self._vault._entries),
                'yes' if store else 'no',
                'yes' if embedding_client else 'no',
            )
        except Exception as exc:
            logger.warning("ToolVault 工具导入失败: %s", exc)

    # ------------------------------------------------------------------
    # 子 Agent 接入
    # ------------------------------------------------------------------

    def connect_agent(
        self,
        agent: Any,
        allowed_tools: list[str] | None = None,
    ) -> None:
        """将子 Agent 接入共享 MCPServer。

        为 Agent 创建独立的 MCPClient + ToolBridge，
        权限由 allowed_tools 白名单控制。
        如果 Vault 已启用，同步注入到 ToolBridge。

        Args:
            agent: Agent 实例（尚未 initialize）
            allowed_tools: 授权工具列表，None 表示不限制
        """
        if not self._initialized or self._server is None:
            logger.warning("MCPService 未初始化，跳过 connect_agent")
            return

        agent.connect_mcp(
            server=self._server,
            provider_id=f"sub-{agent.agent_id[:8]}",
            builtin_tools=False,
        )

        # 如果指定了 allowed_tools，更新 ToolBridge 白名单
        if allowed_tools and agent._tool_bridge is not None:
            agent._tool_bridge._allowed_tools = set(allowed_tools)

        # 注入 Vault 引用（共享同一个 Vault 实例）
        if self._vault is not None and agent._tool_bridge is not None:
            agent._tool_bridge._vault = self._vault

        logger.info(
            "子 Agent '%s' 已接入 MCP (allowed=%s, vault=%s)",
            agent.name,
            allowed_tools or "*",
            'yes' if self._vault else 'no',
        )

    # ------------------------------------------------------------------
    # 工具查询（供 REST API 和前端面板使用）
    # ------------------------------------------------------------------

    async def list_tools(self) -> list[dict[str, Any]]:
        """列出 MCPServer 中所有已注册的工具。

        Returns:
            工具信息列表 [{"name", "description", "parameters"}]
        """
        if not self._initialized or self._server is None:
            return []

        tools = await self._server.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": [
                    {
                        "name": p.name,
                        "type": p.type,
                        "description": p.description,
                        "required": p.required,
                    }
                    for p in (t.parameters or [])
                ],
            }
            for t in tools
        ]

    def get_tool_stats(self) -> dict[str, Any]:
        """获取 MCPServer 统计信息。"""
        if not self._initialized or self._server is None:
            return {"providers": 0, "tools": 0, "calls": 0, "errors": 0}
        return self._server.stats

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """关闭 MCP 服务，释放 Vault/Store/Embedding 资源。"""
        # 关闭 EmbeddingClient
        if self._embedding_client is not None:
            try:
                await self._embedding_client.close()
            except Exception:
                pass
            self._embedding_client = None

        # 关闭 ToolStore
        if self._store is not None:
            try:
                await self._store.close()
            except Exception:
                pass
            self._store = None

        self._vault = None

        # 关闭 MCPServer
        if self._server is not None:
            await self._server.stop()
            self._server = None
        self._initialized = False
        logger.info("MCPService 已关闭")
