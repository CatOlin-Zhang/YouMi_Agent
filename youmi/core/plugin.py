"""
插件系统

基于 HookRegistry 的高层插件抽象。
每个 Plugin 封装一组钩子处理函数，通过 PluginManager 统一管理。

插件生命周期:
1. 创建 Plugin 实例
2. PluginManager.register(plugin, agent) → 调用 plugin.setup(hook_registry)
3. Agent 运行期间，插件的钩子自动生效
4. PluginManager.unregister(plugin_name) → 调用 plugin.teardown() + 注销钩子

用法::

    class LoggingPlugin(Plugin):
        name = "logging"

        async def setup(self, hook_registry):
            hook_registry.register(
                HookType.BEFORE_MODEL_CALL,
                self.on_before_model,
                priority=10,
                plugin_name=self.name,
            )

        async def on_before_model(self, ctx):
            print(f"[LOG] LLM call with {len(ctx.messages)} messages")
            return HookDecision.pass_through()

    # 注册
    manager = PluginManager(hook_registry)
    await manager.register(LoggingPlugin(), agent)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from youmi.core.hooks import HookRegistry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plugin 抽象基类
# ---------------------------------------------------------------------------

class Plugin(ABC):
    """插件抽象基类

    子类必须实现:
    - name: 插件唯一标识名称
    - setup(hook_registry): 注册钩子处理函数
    - teardown(): 释放资源（可选）

    典型模式:
    - 在 setup() 中通过 hook_registry.register() 注册所有钩子
    - 在 teardown() 中清理外部资源（HookRegistry 会由 PluginManager 负责注销）
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """插件唯一标识名称"""
        ...

    @abstractmethod
    async def setup(self, hook_registry: HookRegistry) -> None:
        """插件安装 — 注册钩子处理函数

        Args:
            hook_registry: Agent 的 HookRegistry 实例
        """
        ...

    async def teardown(self) -> None:
        """插件卸载 — 释放资源

        默认空实现。子类可覆写以清理外部连接、文件句柄等。
        注意: 钩子注销由 PluginManager 自动完成，无需在此处理。
        """
        pass


# ---------------------------------------------------------------------------
# 插件管理器
# ---------------------------------------------------------------------------

class _PluginRecord:
    """内部: 插件注册记录"""

    __slots__ = ("plugin", "active")

    def __init__(self, plugin: Plugin) -> None:
        self.plugin = plugin
        self.active = True


class PluginManager:
    """插件管理器

    管理 Agent 上所有插件的注册、卸载和生命周期。

    与 HookRegistry 的关系:
    - HookRegistry 管理原始钩子注册
    - PluginManager 管理 Plugin 实例，通过 plugin_name 关联到 HookRegistry

    每个 Agent 实例持有一个 PluginManager。
    """

    def __init__(self, hook_registry: HookRegistry) -> None:
        self._registry = hook_registry
        self._plugins: dict[str, _PluginRecord] = {}

    async def register(self, plugin: Plugin) -> None:
        """注册并安装插件

        流程:
        1. 检查名称唯一性
        2. 调用 plugin.setup(hook_registry)
        3. 记录到已注册列表

        Args:
            plugin: Plugin 实例

        Raises:
            ValueError: 插件名称已存在
        """
        if plugin.name in self._plugins:
            raise ValueError(f"插件 '{plugin.name}' 已注册")

        await plugin.setup(self._registry)
        self._plugins[plugin.name] = _PluginRecord(plugin)

        logger.info("Plugin registered: %s", plugin.name)

    async def unregister(self, plugin_name: str) -> None:
        """卸载插件

        流程:
        1. 调用 plugin.teardown() 释放资源
        2. 从 HookRegistry 注销该插件的所有钩子
        3. 从已注册列表中移除

        Args:
            plugin_name: 插件名称

        Raises:
            KeyError: 插件未注册
        """
        record = self._plugins.get(plugin_name)
        if record is None:
            raise KeyError(f"插件 '{plugin_name}' 未注册")

        try:
            await record.plugin.teardown()
        except Exception as exc:
            logger.warning("Plugin teardown error (%s): %s", plugin_name, exc)

        self._registry.unregister_all_by_plugin(plugin_name)
        record.active = False
        del self._plugins[plugin_name]

        logger.info("Plugin unregistered: %s", plugin_name)

    async def unregister_all(self) -> None:
        """卸载所有插件（Agent 销毁时调用）"""
        names = list(self._plugins.keys())
        for name in reversed(names):
            try:
                await self.unregister(name)
            except Exception as exc:
                logger.warning("Error unregistering plugin '%s': %s", name, exc)

    def get(self, plugin_name: str) -> Plugin | None:
        """按名称获取插件实例"""
        record = self._plugins.get(plugin_name)
        return record.plugin if record else None

    @property
    def plugins(self) -> dict[str, Plugin]:
        """已注册插件列表"""
        return {
            name: record.plugin
            for name, record in self._plugins.items()
            if record.active
        }

    def __contains__(self, plugin_name: str) -> bool:
        return plugin_name in self._plugins

    def __len__(self) -> int:
        return len(self._plugins)

    def __repr__(self) -> str:
        return f"<PluginManager plugins={list(self._plugins.keys())}>"
