"""aiohttp Web 服务器 — 把 GUI 引擎桥接层暴露为 HTTP + WebSocket 服务。

职责：
1. 创建 aiohttp Application，注册路由与中间件
2. 启动时初始化 EngineBridge（MasterAgent + Hooks）
3. 静态资源服务（gui/static/）
4. REST API：agents / sessions / messages
5. WebSocket 端点：实时双向通信（命令分发 + 事件广播）

用法::

    python -m gui                     # 默认 127.0.0.1:8766
    YOUMI_GUI_PORT=9000 python -m gui # 自定义端口
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from typing import Any

from aiohttp import web

from gui.config import GUIConfig, load_config
from gui.engine.bridge import EngineBridge
from gui.hub.events import hello, pong, tool_list as tool_list_event
from gui.hub.ws_hub import WebSocketHub

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# aiohttp Application 工厂
# ---------------------------------------------------------------------------

def create_app(
    config: GUIConfig | None = None, *, use_mock: bool = False
) -> web.Application:
    """创建 aiohttp Application。

    Args:
        config: GUI 配置，None 时从环境变量自动加载
        use_mock: 为 True 时使用本地 Mock 引擎（不依赖真实 LLM），
            便于在没有 LLM API 的情况下预览群聊式 UI。

    Returns:
        配置完成但尚未启动的 aiohttp Application
    """
    config = config or load_config()

    app = web.Application()
    app["config"] = config
    app["hub"] = WebSocketHub()
    if use_mock:
        from gui.mock_engine import MockEngineBridge
        app["bridge"] = MockEngineBridge(hub=app["hub"], config=config)
    else:
        app["bridge"] = EngineBridge(hub=app["hub"], config=config)

    _setup_routes(app)
    _setup_static(app, config)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    return app


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------

async def _on_startup(app: web.Application) -> None:
    """服务启动后初始化引擎（MasterAgent + Hooks + 持久化加载）。"""
    bridge: EngineBridge = app["bridge"]
    await bridge.init()
    logger.info(
        "引擎初始化完成: master=%s, sessions=%d",
        bridge.master.name if bridge.master else "?",
        len(bridge.sessions),
    )


async def _on_cleanup(app: web.Application) -> None:
    """服务关闭时清理引擎资源。"""
    bridge: EngineBridge = app["bridge"]
    # 清理 MCP 和总线资源
    if hasattr(bridge, 'shutdown'):
        try:
            await bridge.shutdown()
        except Exception:
            pass
    if bridge.master:
        try:
            await bridge.master.destroy()
        except Exception:
            pass
    logger.info("服务已关闭")


# ---------------------------------------------------------------------------
# REST 端点
# ---------------------------------------------------------------------------

async def _handle_list_agents(request: web.Request) -> web.Response:
    """GET /api/agents — 列出 youmi/agents/ 下所有可用角色。"""
    bridge: EngineBridge = request.app["bridge"]
    agents = await bridge.list_agents()
    return web.json_response({"agents": agents})


async def _handle_list_sessions(request: web.Request) -> web.Response:
    """GET /api/sessions — 列出所有会话。"""
    bridge: EngineBridge = request.app["bridge"]
    return web.json_response({"sessions": bridge.list_sessions()})


async def _handle_get_session(request: web.Request) -> web.Response:
    """GET /api/sessions/{session_id} — 获取单个会话详情（含消息历史和成员）。"""
    bridge: EngineBridge = request.app["bridge"]
    session_id = request.match_info["session_id"]
    data = bridge.get_session(session_id)
    if data is None:
        raise web.HTTPNotFound(text=f"Session '{session_id}' not found")
    return web.json_response(data)


async def _handle_create_session(request: web.Request) -> web.Response:
    """POST /api/sessions — 创建新会话。

    Body JSON::

        {"type": "single" | "group", "name": "可选名称"}
    """
    bridge: EngineBridge = request.app["bridge"]
    body = await request.json()
    result = await bridge.create_session(
        type_=body.get("type", "single"),
        name=body.get("name", ""),
    )
    return web.json_response(result, status=201)


async def _handle_delete_session(request: web.Request) -> web.Response:
    """DELETE /api/sessions/{session_id} — 删除会话及其消息。"""
    bridge: EngineBridge = request.app["bridge"]
    session_id = request.match_info["session_id"]
    await bridge.delete_session(session_id)
    return web.json_response({"ok": True})


async def _handle_list_tools(request: web.Request) -> web.Response:
    """GET /api/tools — 列出 MCPServer 中所有已注册的工具。"""
    bridge: EngineBridge = request.app["bridge"]
    tools = await bridge.list_tools()
    stats = bridge.get_tool_stats()
    return web.json_response({"tools": tools, "stats": stats})


# ---------------------------------------------------------------------------
# WebSocket 端点
# ---------------------------------------------------------------------------

async def _handle_ws(request: web.Request) -> web.WebSocketResponse:
    """WS /ws — 浏览器 WebSocket 连接入口。

    连接建立后:
    1. 发送 ``hello`` 事件
    2. 循环读取客户端命令并分发
    3. 连接断开时自动从 hub 移除
    """
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    hub: WebSocketHub = request.app["hub"]
    hub.add(ws)
    logger.info("WebSocket 客户端已连接 (总数=%d)", hub.count)

    try:
        # 握手
        bridge: EngineBridge = request.app["bridge"]
        master_id = bridge.master.agent_id if bridge.master else ""
        await hub.send(ws, hello(master_id=master_id))

        # 推送 MCP 工具列表（如果可用）
        if hasattr(bridge, 'list_tools'):
            try:
                tools = await bridge.list_tools()
                stats = bridge.get_tool_stats()
                await hub.send(ws, tool_list_event(tools=tools, stats=stats))
            except Exception:
                pass

        async for raw_msg in ws:
            if raw_msg.type == web.WSMsgType.TEXT:
                await _dispatch_ws_command(request.app, ws, raw_msg.data)
            elif raw_msg.type == web.WSMsgType.ERROR:
                logger.warning("WebSocket 错误: %s", ws.exception())
    finally:
        hub.remove(ws)
        logger.info("WebSocket 客户端已断开 (剩余=%d)", hub.count)

    return ws


async def _dispatch_ws_command(
    app: web.Application, ws: web.WebSocketResponse, raw: str,
) -> None:
    """解析并分发客户端 WebSocket 命令。

    支持的命令::

        {"type": "ping"}
        {"type": "send_message", "session_id": "...", "text": "..."}
        {"type": "get_history", "session_id": "..."}
        {"type": "create_session", "type": "single", "name": "..."}
        {"type": "delete_session", "session_id": "..."}
        {"type": "add_member", "session_id": "...", "role": "coder", "task": "..."}
        {"type": "typing", "session_id": "..."}
    """
    hub: WebSocketHub = app["hub"]
    bridge: EngineBridge = app["bridge"]

    try:
        cmd = json.loads(raw)
    except json.JSONDecodeError:
        await hub.send(ws, {"type": "error", "message": "无效 JSON"})
        return

    cmd_type = cmd.get("type", "")

    if cmd_type == "ping":
        await hub.send(ws, pong())

    elif cmd_type == "send_message":
        session_id = cmd.get("session_id", "")
        text = cmd.get("text", "")
        if not session_id or not text.strip():
            await hub.send(ws, {"type": "error", "message": "缺少 session_id 或 text"})
            return
        await bridge.send_user_message(session_id, text)

    elif cmd_type == "get_history":
        session_id = cmd.get("session_id", "")
        if session_id:
            await bridge.push_history(ws, session_id)

    elif cmd_type == "create_session":
        await bridge.create_session(
            type_=cmd.get("type", "single"),
            name=cmd.get("name", ""),
        )

    elif cmd_type == "delete_session":
        session_id = cmd.get("session_id", "")
        if session_id:
            await bridge.delete_session(session_id)

    elif cmd_type == "add_member":
        session_id = cmd.get("session_id", "")
        role = cmd.get("role", "")
        task = cmd.get("task", "")
        if session_id and role:
            await bridge.add_member(session_id, role, task)

    elif cmd_type == "typing":
        # 打字状态由前端 P2P 广播，后端仅做透传
        from gui.hub.events import typing as typing_event
        session_id = cmd.get("session_id", "")
        agent_id = cmd.get("agent_id", "__user__")
        await hub.broadcast(typing_event(session_id, agent_id, True))

    else:
        await hub.send(ws, {
            "type": "error",
            "message": f"未知命令: {cmd_type}",
        })


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------

def _setup_routes(app: web.Application) -> None:
    """注册 REST + WebSocket 路由。"""
    app.router.add_get("/api/agents", _handle_list_agents)
    app.router.add_get("/api/sessions", _handle_list_sessions)
    app.router.add_get("/api/sessions/{session_id}", _handle_get_session)
    app.router.add_post("/api/sessions", _handle_create_session)
    app.router.add_delete("/api/sessions/{session_id}", _handle_delete_session)
    app.router.add_get("/api/tools", _handle_list_tools)
    app.router.add_get("/ws", _handle_ws)


def _setup_static(app: web.Application, config: GUIConfig) -> None:
    """配置静态资源服务。

    如果 ``gui/static/`` 目录不存在，自动创建并放置一个占位页面，
    避免 aiohttp ``add_static`` 因目录缺失而报错。
    """
    static_dir = config.static_dir
    os.makedirs(static_dir, exist_ok=True)

    index_path = os.path.join(static_dir, "index.html")
    if not os.path.exists(index_path):
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(_PLACEHOLDER_HTML)
        logger.info("已生成占位页面: %s", index_path)

    # 保存 index 路径供根路由使用
    app["_index_path"] = index_path

    app.router.add_static("/static/", static_dir, show_index=False)
    app.router.add_get("/", _handle_index)
    logger.info("静态资源: %s → /static/", static_dir)


async def _handle_index(request: web.Request) -> web.FileResponse:
    """GET / — 返回 index.html。"""
    index_path = request.app["_index_path"]
    return web.FileResponse(index_path)


_PLACEHOLDER_HTML = """\
<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="utf-8">
    <title>YouMi Agent — 群聊 GUI</title>
    <style>
        body {
            font-family: -apple-system, "Segoe UI", sans-serif;
            display: flex; justify-content: center; align-items: center;
            min-height: 100vh; margin: 0;
            background: #f0f2f5; color: #333;
        }
        .card {
            text-align: center; padding: 3rem 2rem;
            background: #fff; border-radius: 12px;
            box-shadow: 0 2px 12px rgba(0,0,0,0.08);
        }
        h1 { margin: 0 0 0.5rem; font-size: 1.5rem; }
        p { margin: 0; color: #666; }
    </style>
</head>
<body>
    <div class="card">
        <h1>YouMi Agent</h1>
        <p>群聊式 GUI 后端已启动，前端页面待实现 (M1+)。</p>
        <p style="margin-top:1rem;font-size:0.85rem;color:#999;">
            WebSocket: <code>ws://HOST:PORT/ws</code><br>
            REST: <code>/api/agents</code> · <code>/api/sessions</code>
        </p>
    </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------

def run_server() -> None:
    """启动 Web 服务器（阻塞）。

    支持命令行参数::

        python -m gui --host 0.0.0.0 --port 8766
    """
    parser = argparse.ArgumentParser(description="YouMi Agent Web GUI")
    parser.add_argument("--host", default=None, help="监听地址（默认从环境变量或 127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认从环境变量或 8766）")
    parser.add_argument("--mock", action="store_true", help="使用本地 Mock 引擎（不依赖真实 LLM）预览 UI")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    config = load_config()
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port

    use_mock = bool(args.mock) or os.environ.get("YOUMI_GUI_MOCK") == "1"
    app = create_app(config, use_mock=use_mock)
    logger.info("启动 Web GUI: http://%s:%d", config.host, config.port)
    web.run_app(app, host=config.host, port=config.port, print=None)
