"""YouMi Agent — 群聊式 Web GUI。

以 QQ/微信的「单聊 = 与单个 Agent 对话，群聊 = 多 Agent 协作」为隐喻，
把多 Agent 协作过程渲染成会话气泡。所有代码均位于 gui/ 包内，
通过 aiohttp 暴露 HTTP 静态资源 + REST + WebSocket，内部桥接 YouMi 引擎。
"""

__version__ = "0.2.0"
