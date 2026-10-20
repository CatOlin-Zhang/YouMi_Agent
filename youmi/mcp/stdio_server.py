"""YouMi MCP sidecar — stdio JSON-RPC 服务

DSH 通过内置 dsh-mcp-client 以子进程方式挂载本服务::

    {
      "mcpServers": {
        "youmi": {
          "command": "python",
          "args": ["-m", "youmi.mcp.stdio_server"]
        }
      }
    }

协议: MCP 2024-11-05，newline-delimited JSON（每行一个 JSON-RPC 消息）。
stdout 只写协议消息，日志一律走 stderr，避免污染协议流。

fail-open: 任一存储初始化失败只记录错误并降级，进程不退出；
对应工具返回结构化错误，DSH 主链路不受影响。

环境变量:
- YOUMI_DATA_DIR: 数据目录（默认 .youmi）
- YOUMI_TOOLS_DB / YOUMI_SKILLS_DB / YOUMI_PLANS_DB / YOUMI_MEMORY_DB
- YOUMI_EMBEDDING_BASE_URL: 空 = 关闭向量检索（关键词降级）
- YOUMI_EMBEDDING_API_KEY / YOUMI_EMBEDDING_MODEL / YOUMI_EMBEDDING_DIM
- YOUMI_LOG_LEVEL: 默认 INFO（写 stderr）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from youmi.knowledge.global_memory import GlobalMemory
from youmi.llm.embeddings import EmbeddingClient
from youmi.mcp.capabilities import YouMiCapabilityProvider
from youmi.mcp.protocol import MCPToolResult, ToolContext
from youmi.mcp.server import MCPServer
from youmi.mcp.skill_store import SkillStore
from youmi.mcp.tool_store import ToolStore
from youmi.plan.plan_memory import PlanMemory

SERVER_NAME = "youmi-sidecar"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def _data_dir() -> Path:
    return Path(os.environ.get("YOUMI_DATA_DIR", ".youmi"))


def _db_path(env_name: str, filename: str) -> str:
    return os.environ.get(env_name, str(_data_dir() / filename))


def _build_embedding_client() -> EmbeddingClient | None:
    """按环境变量构造 Embedding 客户端（未配置时返回 None）"""
    base_url = os.environ.get("YOUMI_EMBEDDING_BASE_URL", "").strip()
    if not base_url:
        return None
    dim_raw = os.environ.get("YOUMI_EMBEDDING_DIM", "").strip()
    return EmbeddingClient(
        base_url=base_url,
        api_key=os.environ.get("YOUMI_EMBEDDING_API_KEY", ""),
        model=os.environ.get("YOUMI_EMBEDDING_MODEL", "nomic-embed-text"),
        dimensions=int(dim_raw) if dim_raw else None,
    )


# ---------------------------------------------------------------------------
# 运行时
# ---------------------------------------------------------------------------

class SidecarRuntime:
    """持有四个存储组件 + MCP 路由，负责初始化与优雅关闭"""

    def __init__(self) -> None:
        self.tool_store: ToolStore | None = None
        self.skill_store: SkillStore | None = None
        self.plan_memory: PlanMemory | None = None
        self.global_memory: GlobalMemory | None = None
        self.embedder: EmbeddingClient | None = None
        self.errors: dict[str, str] = {}
        self.server = MCPServer()

    async def initialize(self) -> None:
        self.embedder = _build_embedding_client()
        if self.embedder is not None:
            logger.info("embedding 已启用: %s", self.embedder)
        else:
            logger.info("embedding 未配置，检索降级为关键词模式")

        self.tool_store = await self._init_component(
            "tool_store",
            lambda: ToolStore(
                db_path=_db_path("YOUMI_TOOLS_DB", "tools.db"),
                embedding_client=self.embedder,
            ),
        )
        self.skill_store = await self._init_component(
            "skill_store",
            lambda: SkillStore(
                db_path=_db_path("YOUMI_SKILLS_DB", "skills.db"),
                embedding_client=self.embedder,
            ),
        )
        self.plan_memory = await self._init_component(
            "plan_memory",
            lambda: PlanMemory(
                db_path=_db_path("YOUMI_PLANS_DB", "plans.db"),
                embedding_client=self.embedder,
            ),
        )
        self.global_memory = await self._init_component(
            "global_memory",
            lambda: GlobalMemory(
                db_path=_db_path("YOUMI_MEMORY_DB", "knowledge.db"),
                embedding_client=self.embedder,
            ),
        )

        provider = YouMiCapabilityProvider(
            tool_store=self.tool_store,
            skill_store=self.skill_store,
            plan_memory=self.plan_memory,
            global_memory=self.global_memory,
        )
        await self.server.register_provider(provider)
        logger.info(
            "sidecar 就绪: %d 个工具, 降级组件=%s",
            self.server.tool_count, list(self.errors) or "无",
        )

    async def _init_component(self, name: str, factory: Any) -> Any:
        """初始化单个组件 — 失败时降级为 None（fail-open）"""
        try:
            component = factory()
            await component.initialize()
            return component
        except Exception as exc:
            logger.error("%s 初始化失败，相关工具降级: %s", name, exc)
            self.errors[name] = str(exc)
            return None

    async def close(self) -> None:
        for name in ("plan_memory", "global_memory", "skill_store", "tool_store"):
            component = getattr(self, name, None)
            if component is None:
                continue
            try:
                await component.close()
            except Exception as exc:
                logger.warning("%s 关闭失败: %s", name, exc)
        if self.embedder is not None:
            try:
                await self.embedder.close()
            except Exception as exc:
                logger.warning("embedding 关闭失败: %s", exc)

    def health(self) -> dict[str, Any]:
        return {
            "server": SERVER_NAME,
            "version": SERVER_VERSION,
            "protocol": PROTOCOL_VERSION,
            "tools": self.server.tool_count,
            "embedding": self.embedder is not None,
            "components": {
                "tool_store": self.tool_store is not None,
                "skill_store": self.skill_store is not None,
                "plan_memory": self.plan_memory is not None,
                "global_memory": self.global_memory is not None,
            },
            "errors": dict(self.errors),
        }

    # ------------------------------------------------------------------
    # JSON-RPC 分发
    # ------------------------------------------------------------------

    async def handle(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """处理单条 JSON-RPC 消息；通知（无 id）返回 None"""
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}
        is_notification = "id" not in request

        try:
            if method == "initialize":
                result: dict[str, Any] = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                }
            elif method in ("notifications/initialized", "notifications/cancelled"):
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                tools = await self.server.list_tools()
                result = {
                    "tools": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "inputSchema": tool.input_schema,
                        }
                        for tool in tools
                    ],
                }
            elif method == "tools/call":
                result = await self._call_tool(params)
            elif method == "youmi/health":
                result = self.health()
            else:
                if is_notification:
                    return None
                return self._error(req_id, -32601, f"未知方法: {method}")
        except Exception as exc:
            logger.exception("处理 %s 失败", method)
            if is_notification:
                return None
            return self._error(req_id, -32603, f"内部错误: {exc}")

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    async def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        meta = params.get("meta") or {}
        context = ToolContext(
            agent_id=str(meta.get("agent_id", "")),
            task_id=str(meta.get("task_id", "")),
        )
        result: MCPToolResult = await self.server.call_tool(name, arguments, context)
        return {"content": result.content, "isError": result.is_error}

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        }


# ---------------------------------------------------------------------------
# stdio 循环
# ---------------------------------------------------------------------------

def _write(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _configure_stdio() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


async def serve() -> int:
    runtime = SidecarRuntime()
    await runtime.initialize()
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                _write(SidecarRuntime._error(None, -32700, f"JSON 解析失败: {exc}"))
                continue

            if isinstance(message, list):
                responses = []
                for item in message:
                    if isinstance(item, dict):
                        response = await runtime.handle(item)
                        if response is not None:
                            responses.append(response)
                if responses:
                    _write(responses)
                continue

            if not isinstance(message, dict):
                _write(SidecarRuntime._error(None, -32600, "请求必须是 JSON 对象"))
                continue

            response = await runtime.handle(message)
            if response is not None:
                _write(response)
    finally:
        await runtime.close()
    return 0


def main() -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("YOUMI_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _configure_stdio()
    try:
        return asyncio.run(serve())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["PROTOCOL_VERSION", "SERVER_NAME", "SERVER_VERSION", "SidecarRuntime", "main"]
