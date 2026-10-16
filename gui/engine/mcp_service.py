"""MCP 服务层 — GUI 与核心 MCP 系统的桥接。

职责：
1. 创建并持有全局 MCPServer 实例（所有 Agent 共享）
2. 注册 BuiltinToolProvider（包装全部内置工具）
3. 为 MasterAgent 和子 Agent 提供 connect_mcp() 接入点：
   - 创建时被赋予的工具权限 → 经 ToolBridge 白名单注入（工具注入）
   - Agent 工具不足 → 通过 search_new_tools 元工具向 MCP 提需求，
     由 Vault 执行向量查询并返回候选，选定后自动授权加载
4. 集成 ToolStore (SQLite) + ToolVault (向量搜索) 实现工具持久化与语义发现
5. 将 Vault 挂载到 MCPServer：任何 Agent 的 Provider 注册
   （Agent.initialize → register_provider）都会自动同步工具到 Vault，
   实现所有 Agent 的工具注入与向量化
6. 暴露工具列表查询接口（供前端面板渲染）

架构：
    EngineBridge
    └── MCPService
         ├── MCPServer（全局单例，挂载 Vault）
         │    └── BuiltinToolProvider（内置工具）
         ├── ToolStore（SQLite 持久化：tools + vec_tools + changelogs）
         ├── ToolVault（内存缓存 + 语义搜索，底层委托 ToolStore）
         ├── EmbeddingClient（Ollama /v1/embeddings 生成工具向量）
         ├── SkillStore + SkillIngestor（SOP 知识库，与 Tool 库并行）
         ├── AuditGate（召回审计闸门，注入每个 Agent 的 ToolBridge）
         ├── CallPathRouter（SKILL→root / DIRECT→head 版本分流）
         ├── connect_agent() → 为每个 Agent 创建独立 MCPClient + ToolBridge
         │    （附带 Vault + search_new_tools 元工具 + AuditGate）
         ├── ingest_skill()/search_skills() → 供 REST API /api/skills 调用
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
        skill_enabled: 是否启用 SkillStore + SkillIngestor SOP 知识库
        skill_db_path: SkillStore 数据库路径（默认 ".youmi_skills.db"）
        risk_threshold: AuditGate 风险阈值（超过此级别转人工审批）
    """

    def __init__(
        self,
        vault_enabled: bool = True,
        db_path: str = "",
        embedding_base_url: str = "http://localhost:11434/v1",
        embedding_model: str = "nomic-embed-text",
        skill_enabled: bool = True,
        skill_db_path: str = "",
        risk_threshold: str = "high",
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
        # Skill 知识库（SOP 流程知识，与 Tool 库并行）
        self._skill_enabled: bool = skill_enabled
        self._skill_db_path: str = skill_db_path
        self._skill_store: Any = None      # SkillStore
        self._skill_ingestor: Any = None   # SkillIngestor
        # 召回治理三件套
        self._audit_gate: Any = None       # AuditGate
        self._version_router: Any = None   # CallPathRouter
        self._risk_threshold: str = risk_threshold

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

    @property
    def skill_store(self) -> Any:
        """SkillStore 实例（可能为 None）"""
        return self._skill_store

    @property
    def skill_ingestor(self) -> Any:
        """SkillIngestor 实例（可能为 None）"""
        return self._skill_ingestor

    @property
    def audit_gate(self) -> Any:
        """AuditGate 召回审计闸门（可能为 None）"""
        return self._audit_gate

    @property
    def version_router(self) -> Any:
        """CallPathRouter 版本分流器（可能为 None）"""
        return self._version_router

    @property
    def embedding_client(self) -> Any:
        """EmbeddingClient 实例（可能为 None；供 GlobalMemory 等复用）"""
        return self._embedding_client

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    async def setup(self, master: Any) -> None:
        """初始化 MCP 服务并连接 MasterAgent。

        流程：
        1. 创建 MCPServer
        2. 创建并注册 BuiltinToolProvider（全部内置工具）
        3. 启动 MCPServer
        4. [Vault] 创建 ToolStore + EmbeddingClient + ToolVault，
           并挂载到 MCPServer（后续任何 Agent 的 Provider 注册自动同步 Vault）
        5. [Vault] 从 Provider 导入工具到 Vault（向量化 + 入库）
        6. 为 MasterAgent 调用 connect_mcp()（附带 Vault 引用 + 工具发现元工具）
        7. [Vault] 将 Master 的 Agent 级工具（协调器工具）导入 Vault
        8. [Vault] 将 Vault 注入 MasterAgent 的 ToolBridge

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
            # ★ 将 Vault 挂载到 MCPServer：此后任何 Agent 的 Provider 注册
            #   （Agent.initialize() → register_provider）都会自动把该 Agent
            #   的工具同步进 Vault（向量化 + 持久化），
            #   实现"所有 Agent 创建即注入"的工具接入语义。
            if self._vault is not None:
                self._server._vault = self._vault

        # 4b. Skill 知识库初始化（SOP 流程知识，与 Tool 库并行）
        if self._skill_enabled:
            await self._init_skill_store()

        # 4c. 召回治理：AuditGate（权限 BLOCK / 风险 MANUAL / 其余 PASS）
        #     + CallPathRouter（SKILL→root 版本 / DIRECT→head 版本分流）
        from youmi.mcp.audit_gate import AuditGate
        from youmi.mcp.version_router import CallPathRouter

        self._audit_gate = AuditGate(
            approval_manager=getattr(master, "_approval_manager", None),
            risk_threshold=self._risk_threshold,
        )
        self._version_router = CallPathRouter(
            store=self._store, skill_store=self._skill_store,
        )
        logger.info(
            "召回治理已启用: audit_gate=%s, version_router=%s",
            self._audit_gate, self._version_router,
        )

        # 6. 为 MasterAgent 连接 MCP
        #    connect_mcp() 内部会创建 MCPClient + ToolBridge，
        #    并将已有 ToolRegistry 工具迁移到 Provider。
        #    builtin_tools=False 因为我们已通过 BuiltinToolProvider 全量注册；
        #    search_meta_tool=True 使 Master 可通过 search_new_tools
        #    发现并加载新工具。
        master.connect_mcp(
            server=self._server,
            provider_id=f"master-{master.agent_id[:8]}",
            builtin_tools=False,
            search_meta_tool=True,
        )

        # 7. 将 Master 的 Agent 级工具（协调器工具）导入 Vault
        #    connect_mcp() 将 ToolRegistry 中的协调器工具迁移到了
        #    LocalFunctionProvider（master._mcp_provider），但 Vault 中还没有。
        #    当 ToolBridge.to_openai_tools() 优先使用 Vault 时，
        #    这些工具对 LLM 不可见——必须补充导入。
        #    （注: Master initialize() 时 register_provider 也会自动同步，
        #    此处作为初始化前的可见性兜底）
        if self._vault is not None:
            agent_provider = getattr(master, '_mcp_provider', None)
            if agent_provider is not None:
                await self._import_agent_tools_to_vault(agent_provider)

        # 8. 将 Vault 接入 MasterAgent 的 ToolBridge（自动创建 Agent 侧
        #    AgentToolContext，HOT/WARM/COLD 状态由 Master 独立管理；
        #    协调器工具标记为必备，永不回收）
        if self._vault is not None and master._tool_bridge is not None:
            agent_provider = getattr(master, '_mcp_provider', None)
            essential = (
                set(getattr(agent_provider, '_definitions', {}).keys())
                if agent_provider is not None else None
            )
            master._tool_bridge.attach_vault(self._vault, essential_names=essential)

        # 9. 将 AuditGate 注入 Master 的 ToolBridge（召回审计拦截：
        #    search_new_tools 的 load 路径与 search_and_confirm 均生效）
        if self._audit_gate is not None and master._tool_bridge is not None:
            master._tool_bridge.set_audit_gate(self._audit_gate)

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

    async def _init_skill_store(self) -> None:
        """初始化 SkillStore + SkillIngestor（SOP 流程知识库）。

        与 ToolStore 并行、结构镜像：复用同一个 EmbeddingClient
        （Ollama 不可用时降级为关键词检索）。任何失败不阻塞 MCP
        主流程，仅放弃 Skill 能力。
        """
        # SkillStore 初始化失败（如目录不可写）时保持 None 优雅降级
        store = None
        try:
            from youmi.mcp.skill_store import SkillStore
            db_path = self._skill_db_path or ".youmi_skills.db"
            store = SkillStore(
                db_path=db_path,
                embedding_client=self._embedding_client,
            )
            await store.initialize()
            logger.info("SkillStore 已初始化: %s", db_path)
        except Exception as exc:
            logger.warning("SkillStore 初始化失败，Skill 能力降级: %s", exc)
            store = None

        self._skill_store = store

        if store is not None:
            try:
                from youmi.mcp.skill_ingest import SkillIngestor
                self._skill_ingestor = SkillIngestor(skill_store=store)
                logger.info("SkillIngestor 已就绪 (store=%s)", db_path)
            except Exception as exc:
                logger.warning("SkillIngestor 创建失败: %s", exc)
                self._skill_ingestor = None

    async def _import_agent_tools_to_vault(self, agent_provider: Any) -> None:
        """将 Agent 级工具（协调器工具等）导入 Vault。

        解决工具不可见问题：connect_mcp() 将 ToolRegistry 工具迁移到
        LocalFunctionProvider，但 Vault 中仍缺少这些工具。
        当 ToolBridge.to_openai_tools() 优先使用 Vault 时，
        这些工具对 LLM 不可见。

        只导入 Vault 中尚不存在的工具，避免覆盖 BuiltinToolProvider 的同名工具。
        所有 Agent 级工具标记为 essential=True（HOT 状态，永不回收）。
        """
        from youmi.mcp.vault import ToolEntry, ToolContextTier

        definitions = getattr(agent_provider, '_definitions', {})
        handlers = getattr(agent_provider, '_handlers', {})
        provider_id = getattr(agent_provider, 'provider_id', '')

        imported = 0
        for name, defn in definitions.items():
            if name in self._vault._entries:
                continue  # 已在 Vault 中（来自 BuiltinToolProvider），不覆盖

            entry = ToolEntry(
                tool_name=name,
                definition=defn,
                handler=handlers.get(name),
                provider_id=provider_id,
                essential=True,  # Agent 级工具永不回收
                summary=defn.description[:80],
                tier=ToolContextTier.HOT,
            )
            self._vault._entries[name] = entry
            imported += 1

        if imported > 0:
            logger.info(
                "ToolVault: 补充导入 %d 个 Agent 级工具 (provider=%s)",
                imported, provider_id,
            )

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
        权限由 allowed_tools 白名单控制（创建时赋予的工具访问权限
        在此注入）；其自有工具在 Agent.initialize() 注册 Provider 时
        自动同步到 Vault。
        如果 Vault 已启用，同步注入到 ToolBridge。
        search_meta_tool=True 使子 Agent 工具不足时可通过
        search_new_tools 向 MCP 提需求（向量查询 + 授权加载）。

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
            search_meta_tool=True,
        )

        # 如果指定了 allowed_tools，更新 ToolBridge 白名单
        if allowed_tools and agent._tool_bridge is not None:
            agent._tool_bridge._allowed_tools = set(allowed_tools)

        # 接入 Vault（共享同一个 Vault 实例，自动创建 Agent 侧
        # AgentToolContext；白名单工具自动标记为必备）
        if self._vault is not None and agent._tool_bridge is not None:
            agent._tool_bridge.attach_vault(self._vault)

        # 注入召回审计闸门（search_new_tools 的 load 路径生效：
        # 权限不满足 BLOCK / 风险超阈值 MANUAL 转人工）
        if self._audit_gate is not None and agent._tool_bridge is not None:
            agent._tool_bridge.set_audit_gate(self._audit_gate)

        logger.info(
            "子 Agent '%s' 已接入 MCP (allowed=%s, vault=%s, audit=%s)",
            agent.name,
            allowed_tools or "*",
            'yes' if self._vault else 'no',
            'yes' if self._audit_gate else 'no',
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
    # Skill 知识库（供 REST API /api/skills 和前端面板使用）
    # ------------------------------------------------------------------

    async def ingest_skill(self, skill_doc: str | dict[str, Any]) -> dict[str, Any]:
        """解析并入库一份 SOP Skill 文档。

        Args:
            skill_doc: Markdown 文本（含/不含 frontmatter）或显式字段 dict

        Returns:
            入库结果 {"ok", "skill", "error"}；Skill 库未启用时 ok=False
        """
        if self._skill_ingestor is None:
            return {"ok": False, "error": "Skill 库未启用或初始化失败"}
        try:
            entry = await self._skill_ingestor.ingest(skill_doc)
            return {"ok": True, "skill": self._skill_entry_to_dict(entry)}
        except Exception as exc:
            logger.warning("Skill 入库失败: %s", exc)
            return {"ok": False, "error": str(exc)}

    async def search_skills(
        self,
        query: str,
        top_k: int = 5,
        tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """锥形检索 Skill（语义 ∩ tag ∩ 风险边界）。

        Args:
            query: 自然语言查询
            top_k: 返回数量
            tags: tag 边界过滤（None = 不限）

        Returns:
            [{"skill_name", "score", "summary", "bound_tool_name",
              "risk_level", "tags", ...}]；Skill 库未启用时返回空列表
        """
        if self._skill_store is None:
            return []
        try:
            results = await self._skill_store.search_cone(
                query, top_k=top_k, tags=tags,
            )
            return [
                {
                    "skill_name": r.skill_name,
                    "score": round(r.score, 4),
                    "summary": r.summary,
                    "summary_l2": r.summary_l2,
                    "version": r.version,
                    "bound_tool_name": r.bound_tool_name,
                    "risk_level": r.risk_level,
                    "tags": r.tags,
                }
                for r in results
            ]
        except Exception as exc:
            logger.warning("Skill 检索失败: %s", exc)
            return []

    async def get_skill(self, skill_name: str) -> dict[str, Any] | None:
        """按名称获取单个 Skill 详情（含 SOP 全文）。"""
        if self._skill_store is None:
            return None
        try:
            entry = await self._skill_store.get_skill(skill_name)
            if entry is None:
                return None
            return self._skill_entry_to_dict(entry)
        except Exception as exc:
            logger.warning("Skill 查询失败: %s", exc)
            return None

    async def get_skill_stats(self) -> dict[str, Any]:
        """Skill 库统计信息（未启用时 enabled=False）。"""
        if self._skill_store is None:
            return {"enabled": False, "skills": 0}
        try:
            stats = await self._skill_store.stats()
            stats["enabled"] = True
            return stats
        except Exception:
            return {"enabled": True, "skills": 0}

    @staticmethod
    def _skill_entry_to_dict(entry: Any) -> dict[str, Any]:
        """SkillEntry → REST 友好 dict。"""
        return {
            "skill_name": entry.skill_name,
            "version": entry.version,
            "bound_tool_name": entry.bound_tool_name,
            "describe": entry.describe,
            "summary": entry.summary,
            "summary_l2": entry.summary_l2,
            "tags": entry.tags,
            "risk_level": entry.risk_level,
            "created_at": entry.created_at,
            "updated_at": entry.updated_at,
        }

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        """关闭 MCP 服务，释放 Vault/SkillStore/Store/Embedding 资源。"""
        # 关闭 SkillStore（与 ToolStore 并行的 SOP 知识库）
        if self._skill_store is not None:
            try:
                await self._skill_store.close()
            except Exception:
                pass
            self._skill_store = None
            self._skill_ingestor = None

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
        self._version_router = None

        # 关闭 MCPServer
        if self._server is not None:
            await self._server.stop()
            self._server = None
        self._initialized = False
        logger.info("MCPService 已关闭")
