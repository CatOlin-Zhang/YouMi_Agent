"""
P2 功能测试: Hook/插件系统 (OC-5) + System Prompt 动态组装 (OC-6)

测试覆盖:
1. HookRegistry — 注册、链式调用、block/modify/pass
2. Plugin — ABC 实现、PluginManager 生命周期
3. PromptAssembler — 分层组装、token 截断、优先级
4. Agent 集成 — Hook 在 ReAct 循环中生效、PromptAssembler 在 _observe() 中生效
5. 回归测试 — 确保 P2 改动不破坏现有功能
"""

from __future__ import annotations

import asyncio
import pytest

from youmi.core.hooks import (
    HookRegistry,
    HookType,
    HookContext,
    HookDecision,
    HookDecisionType,
)
from youmi.core.plugin import Plugin, PluginManager
from youmi.core.prompt import PromptAssembler, PromptLayer, estimate_tokens
from youmi.core.agent import Agent, AgentConfig
from youmi.core.types import LLMConfig, MemoryConfig


# ===================================================================
# 1. HookRegistry 测试
# ===================================================================

class TestHookRegistry:
    """HookRegistry 基础功能测试"""

    def test_register_and_count(self):
        registry = HookRegistry()
        assert registry.hook_count(HookType.BEFORE_TOOL_CALL) == 0

        async def handler(ctx):
            return HookDecision.pass_through()

        registry.register(HookType.BEFORE_TOOL_CALL, handler, plugin_name="test")
        assert registry.hook_count(HookType.BEFORE_TOOL_CALL) == 1
        assert registry.has_hooks(HookType.BEFORE_TOOL_CALL)
        assert not registry.has_hooks(HookType.AFTER_TOOL_CALL)

    def test_unregister(self):
        registry = HookRegistry()

        async def handler(ctx):
            return HookDecision.pass_through()

        registry.register(HookType.BEFORE_TOOL_CALL, handler)
        registry.unregister(HookType.BEFORE_TOOL_CALL, handler)
        assert registry.hook_count(HookType.BEFORE_TOOL_CALL) == 0

    def test_unregister_by_plugin(self):
        registry = HookRegistry()

        async def h1(ctx):
            return HookDecision.pass_through()

        async def h2(ctx):
            return HookDecision.pass_through()

        registry.register(HookType.BEFORE_TOOL_CALL, h1, plugin_name="plugin_a")
        registry.register(HookType.AFTER_TOOL_CALL, h2, plugin_name="plugin_a")
        registry.register(HookType.BEFORE_MODEL_CALL, h1, plugin_name="plugin_b")

        count = registry.unregister_all_by_plugin("plugin_a")
        assert count == 2
        assert registry.hook_count(HookType.BEFORE_MODEL_CALL) == 1

    @pytest.mark.asyncio
    async def test_invoke_pass(self):
        registry = HookRegistry()

        async def handler(ctx):
            return HookDecision.pass_through()

        registry.register(HookType.BEFORE_TOOL_CALL, handler)
        ctx = HookContext(hook_type=HookType.BEFORE_TOOL_CALL, tool_name="test")
        result = await registry.invoke(HookType.BEFORE_TOOL_CALL, ctx)
        assert result.decision == HookDecisionType.PASS

    @pytest.mark.asyncio
    async def test_invoke_block(self):
        registry = HookRegistry()

        async def blocker(ctx):
            return HookDecision.block(reason="不允许调用此工具")

        registry.register(HookType.BEFORE_TOOL_CALL, blocker)
        ctx = HookContext(hook_type=HookType.BEFORE_TOOL_CALL, tool_name="dangerous_tool")
        result = await registry.invoke(HookType.BEFORE_TOOL_CALL, ctx)
        assert result.decision == HookDecisionType.BLOCK
        assert "不允许" in result.reason

    @pytest.mark.asyncio
    async def test_invoke_modify(self):
        registry = HookRegistry()

        async def modifier(ctx):
            return HookDecision.modify(tool_arguments={"safe": True})

        registry.register(HookType.BEFORE_TOOL_CALL, modifier)
        ctx = HookContext(
            hook_type=HookType.BEFORE_TOOL_CALL,
            tool_name="test",
            tool_arguments={"safe": False},
        )
        result = await registry.invoke(HookType.BEFORE_TOOL_CALL, ctx)
        assert result.decision == HookDecisionType.MODIFY
        assert ctx.extra.get("tool_arguments") == {"safe": True}

    @pytest.mark.asyncio
    async def test_invoke_priority_chain(self):
        """低优先级先执行, block 终止后续链"""
        registry = HookRegistry()
        call_order = []

        async def first(ctx):
            call_order.append("first")
            return HookDecision.pass_through()

        async def second(ctx):
            call_order.append("second")
            return HookDecision.block("stopped")

        async def third(ctx):
            call_order.append("third")
            return HookDecision.pass_through()

        registry.register(HookType.BEFORE_MODEL_CALL, first, priority=0)
        registry.register(HookType.BEFORE_MODEL_CALL, second, priority=10)
        registry.register(HookType.BEFORE_MODEL_CALL, third, priority=20)

        ctx = HookContext(hook_type=HookType.BEFORE_MODEL_CALL)
        result = await registry.invoke(HookType.BEFORE_MODEL_CALL, ctx)

        assert call_order == ["first", "second"]
        assert result.decision == HookDecisionType.BLOCK

    @pytest.mark.asyncio
    async def test_invoke_handler_error_continues(self):
        """钩子处理函数出错不影响后续钩子"""
        registry = HookRegistry()

        async def bad_handler(ctx):
            raise ValueError("oops")

        async def good_handler(ctx):
            return HookDecision.pass_through()

        registry.register(HookType.BEFORE_TOOL_CALL, bad_handler, priority=0)
        registry.register(HookType.BEFORE_TOOL_CALL, good_handler, priority=10)

        ctx = HookContext(hook_type=HookType.BEFORE_TOOL_CALL)
        result = await registry.invoke(HookType.BEFORE_TOOL_CALL, ctx)
        assert result.decision == HookDecisionType.PASS

    @pytest.mark.asyncio
    async def test_invoke_no_hooks_returns_pass(self):
        registry = HookRegistry()
        ctx = HookContext(hook_type=HookType.BEFORE_TOOL_CALL)
        result = await registry.invoke(HookType.BEFORE_TOOL_CALL, ctx)
        assert result.decision == HookDecisionType.PASS


# ===================================================================
# 2. HookDecision 工厂方法测试
# ===================================================================

class TestHookDecision:
    """HookDecision 便捷工厂方法"""

    def test_pass_through(self):
        d = HookDecision.pass_through()
        assert d.decision == HookDecisionType.PASS

    def test_modify(self):
        d = HookDecision.modify(key="value")
        assert d.decision == HookDecisionType.MODIFY
        assert d.modified_data == {"key": "value"}

    def test_block(self):
        d = HookDecision.block("not allowed")
        assert d.decision == HookDecisionType.BLOCK
        assert d.reason == "not allowed"


# ===================================================================
# 3. Plugin + PluginManager 测试
# ===================================================================

class LoggingPlugin(Plugin):
    """测试用插件: 记录工具调用"""

    def __init__(self):
        self.tool_calls: list[str] = []
        self._setup_done = False
        self._teardown_done = False

    @property
    def name(self) -> str:
        return "logging"

    async def setup(self, hook_registry: HookRegistry) -> None:
        hook_registry.register(
            HookType.BEFORE_TOOL_CALL,
            self.on_before_tool,
            plugin_name=self.name,
        )
        self._setup_done = True

    async def teardown(self) -> None:
        self._teardown_done = True

    async def on_before_tool(self, ctx: HookContext) -> HookDecision:
        self.tool_calls.append(ctx.tool_name)
        return HookDecision.pass_through()


class BlockingPlugin(Plugin):
    """测试用插件: 拦截特定工具"""

    @property
    def name(self) -> str:
        return "blocker"

    async def setup(self, hook_registry: HookRegistry) -> None:
        hook_registry.register(
            HookType.BEFORE_TOOL_CALL,
            self.on_before_tool,
            plugin_name=self.name,
        )

    async def on_before_tool(self, ctx: HookContext) -> HookDecision:
        if ctx.tool_name == "shell_exec":
            return HookDecision.block("shell_exec 被拦截")
        return HookDecision.pass_through()


class TestPluginManager:
    """PluginManager 生命周期测试"""

    @pytest.mark.asyncio
    async def test_register_plugin(self):
        registry = HookRegistry()
        manager = PluginManager(registry)
        plugin = LoggingPlugin()

        await manager.register(plugin)
        assert "logging" in manager
        assert len(manager) == 1
        assert plugin._setup_done
        assert registry.hook_count(HookType.BEFORE_TOOL_CALL) == 1

    @pytest.mark.asyncio
    async def test_unregister_plugin(self):
        registry = HookRegistry()
        manager = PluginManager(registry)
        plugin = LoggingPlugin()

        await manager.register(plugin)
        await manager.unregister("logging")
        assert "logging" not in manager
        assert len(manager) == 0
        assert plugin._teardown_done
        assert registry.hook_count(HookType.BEFORE_TOOL_CALL) == 0

    @pytest.mark.asyncio
    async def test_duplicate_register_raises(self):
        registry = HookRegistry()
        manager = PluginManager(registry)
        await manager.register(LoggingPlugin())

        with pytest.raises(ValueError, match="已注册"):
            await manager.register(LoggingPlugin())

    @pytest.mark.asyncio
    async def test_unregister_nonexistent_raises(self):
        registry = HookRegistry()
        manager = PluginManager(registry)

        with pytest.raises(KeyError, match="未注册"):
            await manager.unregister("nonexistent")

    @pytest.mark.asyncio
    async def test_unregister_all(self):
        registry = HookRegistry()
        manager = PluginManager(registry)
        await manager.register(LoggingPlugin())
        await manager.register(BlockingPlugin())
        assert len(manager) == 2

        await manager.unregister_all()
        assert len(manager) == 0

    @pytest.mark.asyncio
    async def test_plugin_hooks_fire(self):
        """插件钩子实际触发"""
        registry = HookRegistry()
        manager = PluginManager(registry)
        plugin = LoggingPlugin()
        await manager.register(plugin)

        ctx = HookContext(
            hook_type=HookType.BEFORE_TOOL_CALL,
            tool_name="file_read",
        )
        await registry.invoke(HookType.BEFORE_TOOL_CALL, ctx)
        assert plugin.tool_calls == ["file_read"]

    @pytest.mark.asyncio
    async def test_blocking_plugin(self):
        """Blocking 插件拦截工具调用"""
        registry = HookRegistry()
        manager = PluginManager(registry)
        await manager.register(BlockingPlugin())

        ctx = HookContext(
            hook_type=HookType.BEFORE_TOOL_CALL,
            tool_name="shell_exec",
        )
        result = await registry.invoke(HookType.BEFORE_TOOL_CALL, ctx)
        assert result.decision == HookDecisionType.BLOCK
        assert "拦截" in result.reason


# ===================================================================
# 4. PromptAssembler 测试
# ===================================================================

class TestEstimateTokens:
    """Token 估算函数测试"""

    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_english(self):
        tokens = estimate_tokens("Hello world this is a test")
        assert 3 <= tokens <= 10

    def test_chinese(self):
        tokens = estimate_tokens("你好世界这是一个测试")
        assert 4 <= tokens <= 15

    def test_mixed(self):
        tokens = estimate_tokens("Hello 你好 World 世界")
        assert 3 <= tokens <= 15


class TestPromptLayer:
    """PromptLayer 数据模型测试"""

    def test_create(self):
        layer = PromptLayer(name="base", content="你是助手", priority=0)
        assert layer.name == "base"
        assert layer.priority == 0
        assert layer.estimated_tokens > 0

    def test_frozen(self):
        layer = PromptLayer(name="test", content="test")
        with pytest.raises(Exception):
            layer.name = "changed"


class TestPromptAssembler:
    """PromptAssembler 组装逻辑测试"""

    def test_empty_assembler(self):
        asm = PromptAssembler()
        assert asm.assemble() == ""

    def test_single_layer(self):
        asm = PromptAssembler()
        asm.add_layer(PromptLayer(name="base", content="你是助手", priority=0))
        result = asm.assemble()
        assert result == "你是助手"

    def test_multiple_layers_order(self):
        """低 priority 在前"""
        asm = PromptAssembler()
        asm.add_layer(PromptLayer(name="runtime", content="当前任务: 写代码", priority=60))
        asm.add_layer(PromptLayer(name="base", content="你是助手", priority=0))
        asm.add_layer(PromptLayer(name="context", content="项目: YouMi", priority=40))

        result = asm.assemble()
        parts = result.split("\n\n")
        assert parts[0] == "你是助手"
        assert parts[1] == "项目: YouMi"
        assert parts[2] == "当前任务: 写代码"

    def test_replace_layer(self):
        """同名层替换"""
        asm = PromptAssembler()
        asm.add_layer(PromptLayer(name="base", content="V1", priority=0))
        asm.add_layer(PromptLayer(name="base", content="V2", priority=0))
        assert len(asm.layers) == 1
        assert asm.assemble() == "V2"

    def test_remove_layer(self):
        asm = PromptAssembler()
        asm.add_layer(PromptLayer(name="base", content="base", priority=0))
        asm.add_layer(PromptLayer(name="extra", content="extra", priority=50))
        assert asm.remove_layer("extra")
        assert len(asm.layers) == 1
        assert not asm.remove_layer("nonexistent")

    def test_get_layer(self):
        asm = PromptAssembler()
        asm.add_layer(PromptLayer(name="base", content="base", priority=0))
        assert asm.get_layer("base") is not None
        assert asm.get_layer("nonexistent") is None

    def test_assemble_with_max_tokens_no_truncation(self):
        """token 预算充足时不截断"""
        asm = PromptAssembler()
        asm.add_layer(PromptLayer(name="base", content="Short", priority=0))
        result = asm.assemble(max_tokens=1000)
        assert result == "Short"

    def test_assemble_with_max_tokens_truncation(self):
        """token 预算不足时截断高 priority 层"""
        asm = PromptAssembler()
        asm.add_layer(PromptLayer(name="base", content="你是助手", priority=0))
        asm.add_layer(PromptLayer(name="context", content="A" * 1000, priority=40))
        asm.add_layer(PromptLayer(name="override", content="B" * 1000, priority=80))

        # 设置很小的预算
        result = asm.assemble(max_tokens=20)
        # base 层永不被截断
        assert "你是助手" in result

    def test_from_system_prompt(self):
        asm = PromptAssembler.from_system_prompt("你是助手")
        assert len(asm.layers) == 1
        assert asm.layers[0].name == "base"
        assert asm.assemble() == "你是助手"

    def test_from_system_prompt_with_extra(self):
        extra = [PromptLayer(name="context", content="额外上下文", priority=40)]
        asm = PromptAssembler.from_system_prompt("你是助手", extra_layers=extra)
        assert len(asm.layers) == 2
        result = asm.assemble()
        assert "你是助手" in result
        assert "额外上下文" in result

    def test_estimated_total_tokens(self):
        asm = PromptAssembler()
        asm.add_layer(PromptLayer(name="base", content="Hello world", priority=0))
        assert asm.estimated_total_tokens > 0

    def test_layers_property_sorted(self):
        asm = PromptAssembler()
        asm.add_layer(PromptLayer(name="c", content="c", priority=80))
        asm.add_layer(PromptLayer(name="a", content="a", priority=0))
        asm.add_layer(PromptLayer(name="b", content="b", priority=40))
        priorities = [l.priority for l in asm.layers]
        assert priorities == [0, 40, 80]

    def test_empty_layers_skipped(self):
        """空白内容的层被跳过"""
        asm = PromptAssembler()
        asm.add_layer(PromptLayer(name="base", content="base", priority=0))
        asm.add_layer(PromptLayer(name="empty", content="  \n  ", priority=50))
        result = asm.assemble()
        assert result == "base"


# ===================================================================
# 5. Agent 集成测试
# ===================================================================

class TestAgentHookIntegration:
    """Agent 上的 Hook/Plugin 集成测试"""

    def _make_agent(self, system_prompt="你是助手") -> Agent:
        config = AgentConfig(
            name="TestAgent",
            system_prompt=system_prompt,
            llm_config=LLMConfig(model="test"),
        )
        return Agent(config)

    def test_agent_has_hook_registry(self):
        agent = self._make_agent()
        assert agent.hook_registry is not None
        assert isinstance(agent.hook_registry, HookRegistry)

    def test_agent_has_plugin_manager(self):
        agent = self._make_agent()
        assert agent.plugin_manager is not None
        assert isinstance(agent.plugin_manager, PluginManager)

    def test_agent_prompt_assembler_initially_none(self):
        """未初始化前为 None"""
        agent = self._make_agent()
        assert agent.prompt_assembler is None

    @pytest.mark.asyncio
    async def test_agent_initialize_creates_assembler(self):
        """initialize() 后 PromptAssembler 被创建"""
        agent = self._make_agent(system_prompt="你是助手")
        await agent.initialize()
        assert agent.prompt_assembler is not None
        assert len(agent.prompt_assembler.layers) == 1
        assert agent.prompt_assembler.layers[0].name == "base"

    @pytest.mark.asyncio
    async def test_add_prompt_layer(self):
        """add_prompt_layer 便捷方法"""
        agent = self._make_agent(system_prompt="你是助手")
        agent.add_prompt_layer(
            PromptLayer(name="context", content="项目: YouMi", priority=40)
        )
        assert agent.prompt_assembler is not None
        assert len(agent.prompt_assembler.layers) == 2

    @pytest.mark.asyncio
    async def test_install_plugin(self):
        """install_plugin 便捷方法"""
        agent = self._make_agent()
        await agent.install_plugin(LoggingPlugin())
        assert "logging" in agent.plugin_manager
        assert agent.hook_registry.hook_count(HookType.BEFORE_TOOL_CALL) == 1

    @pytest.mark.asyncio
    async def test_observe_without_hooks_unchanged(self):
        """无 Hook 时 _observe() 行为不变"""
        agent = self._make_agent(system_prompt="你是助手")
        await agent.initialize()
        agent._conversation = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
        ]
        obs = await agent._observe()
        assert len(obs.messages) == 2
        assert obs.messages[0]["content"] == "你是助手"

    @pytest.mark.asyncio
    async def test_observe_with_prompt_assembler(self):
        """有额外 Prompt 层时 _observe() 组装 system prompt"""
        agent = self._make_agent(system_prompt="你是助手")
        await agent.initialize()
        agent.add_prompt_layer(
            PromptLayer(name="context", content="当前项目: YouMi Agent", priority=40)
        )
        agent._conversation = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
        ]
        obs = await agent._observe()
        # system prompt 被组装为 base + context
        system_content = obs.messages[0]["content"]
        assert "你是助手" in system_content
        assert "YouMi Agent" in system_content

    @pytest.mark.asyncio
    async def test_observe_with_before_prompt_build_hook(self):
        """before_prompt_build 钩子可以修改 messages"""
        agent = self._make_agent(system_prompt="你是助手")
        await agent.initialize()

        async def inject_context(ctx):
            modified = list(ctx.messages)
            if modified and modified[0]["role"] == "system":
                modified[0] = {
                    "role": "system",
                    "content": modified[0]["content"] + "\n\n[注入的上下文]",
                }
            return HookDecision.modify(messages=modified)

        agent.hook_registry.register(
            HookType.BEFORE_PROMPT_BUILD, inject_context, plugin_name="injector",
        )

        agent._conversation = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "你好"},
        ]
        obs = await agent._observe()
        assert "[注入的上下文]" in obs.messages[0]["content"]

    @pytest.mark.asyncio
    async def test_on_destroy_cleans_plugins(self):
        """on_destroy 自动卸载所有插件"""
        agent = self._make_agent()
        plugin = LoggingPlugin()
        await agent.install_plugin(plugin)
        assert len(agent.plugin_manager) == 1

        await agent.on_destroy()
        assert len(agent.plugin_manager) == 0
        assert plugin._teardown_done


# ===================================================================
# 6. 回归测试 — 确保 P2 改动不破坏现有功能
# ===================================================================

class TestP2Regression:
    """P2 回归测试"""

    @pytest.mark.asyncio
    async def test_agent_basic_lifecycle(self):
        """Agent 基本生命周期不变"""
        config = AgentConfig(
            name="RegressionAgent",
            system_prompt="测试",
            llm_config=LLMConfig(model="test"),
        )
        agent = Agent(config)
        assert agent.status.value == "created"

        await agent.initialize()
        assert agent.status.value == "idle"

        result = await agent.run("hello")
        assert result.status.value == "completed"

    @pytest.mark.asyncio
    async def test_agent_no_system_prompt(self):
        """无 system_prompt 时正常工作"""
        config = AgentConfig(
            name="NoPrompt",
            llm_config=LLMConfig(model="test"),
        )
        agent = Agent(config)
        await agent.initialize()
        result = await agent.run("hello")
        assert result.status.value == "completed"

    @pytest.mark.asyncio
    async def test_imports(self):
        """所有 P2 类型可正常导入"""
        from youmi import (
            HookRegistry, HookType, HookContext, HookDecision, HookDecisionType,
            Plugin, PluginManager, PromptAssembler, PromptLayer,
        )
        assert HookRegistry is not None
        assert Plugin is not None
        assert PromptAssembler is not None

    @pytest.mark.asyncio
    async def test_core_imports(self):
        """core 模块导出正常"""
        from youmi.core import (
            HookRegistry, HookType, Plugin, PluginManager,
            PromptAssembler, PromptLayer,
        )
        assert all(x is not None for x in [
            HookRegistry, HookType, Plugin, PluginManager,
            PromptAssembler, PromptLayer,
        ])
