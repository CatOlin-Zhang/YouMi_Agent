"""MasterAgent 单元测试

验证:
1. 从配置目录加载
2. 子 Agent 创建与管理
3. MasterAgent 内置工具注册
4. 子 Agent 运行（使用 EchoAgent 模式）
5. 环境路径继承
6. 生命周期钩子
"""

import asyncio
import json
import os
from pathlib import Path

import pytest

from youmi.core.agent import Agent, AgentConfig, AgentStatus, _Observation, _Thought
from youmi.core.types import AgentMetadata, LLMConfig, LLMProvider, MemoryConfig
from youmi.coordinator.master import MasterAgent, SubAgentRecord
from youmi.agents import get_agent_dir, list_agents, load_agent_config


# ---------------------------------------------------------------------------
# 辅助类
# ---------------------------------------------------------------------------

class EchoAgent(Agent):
    """测试用 Agent — 原样回复输入"""

    async def _think(self, observation: _Observation) -> _Thought:
        last_user_msg = ""
        for msg in reversed(observation.messages):
            if msg.get("role") == "user":
                last_user_msg = msg.get("content", "")
                break
        return _Thought(
            reasoning=f"Echo: {last_user_msg}",
            action_type="respond",
            action_payload={"response": f"Echo: {last_user_msg}"},
            should_continue=False,
        )


def make_master_config(**overrides) -> AgentConfig:
    """创建测试用 MasterAgent 配置"""
    defaults = dict(
        name="TestMaster",
        system_prompt="你是测试用的 MasterAgent。",
        llm_config=LLMConfig(provider=LLMProvider.LOCAL, model="test", api_key="test"),
        memory_config=MemoryConfig(strategy="full"),
        metadata=AgentMetadata(
            display_name="Test Master",
            role="master",
            tags=["test", "master"],
            capabilities=["test"],
        ),
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


# ---------------------------------------------------------------------------
# 测试: Agent env 属性
# ---------------------------------------------------------------------------

class TestAgentEnv:
    """验证 Agent 基类的 env 属性"""

    async def test_default_env_detects_project_root(self):
        """默认 env 应自动检测到项目根目录（包含 pyproject.toml）"""
        config = make_master_config()
        agent = Agent(config)
        # env 应该是一个存在的目录
        assert os.path.isdir(agent.env)
        # 应该包含 pyproject.toml
        assert os.path.exists(os.path.join(agent.env, "pyproject.toml"))

    async def test_custom_env(self):
        """指定 env 时应使用指定值"""
        custom_path = str(Path(__file__).parent)
        config = make_master_config(env=custom_path)
        agent = Agent(config)
        assert agent.env == custom_path

    async def test_env_in_summary(self):
        """to_summary 应包含 env 字段"""
        config = make_master_config()
        agent = Agent(config)
        summary = agent.to_summary()
        assert "env" in summary


# ---------------------------------------------------------------------------
# 测试: agents 模块
# ---------------------------------------------------------------------------

class TestAgentsModule:
    """验证 youmi/agents/ 模块的工具函数"""

    async def test_get_agent_dir(self):
        """get_agent_dir 返回正确路径"""
        agent_dir = get_agent_dir("master")
        assert agent_dir.name == "master"
        assert agent_dir.parent.name == "agents"

    async def test_list_agents_includes_master(self):
        """list_agents 应包含 master"""
        agents = list_agents()
        assert "master" in agents

    async def test_load_master_config(self):
        """应能加载 master 的 config.yaml"""
        data = load_agent_config("master")
        assert data["name"] == "MasterAgent"
        assert "metadata" in data
        assert data["metadata"]["role"] == "master"

    async def test_load_nonexistent_config_raises(self):
        """加载不存在的配置应抛出 FileNotFoundError"""
        with pytest.raises(FileNotFoundError):
            load_agent_config("nonexistent_agent_xyz")


# ---------------------------------------------------------------------------
# 测试: MasterAgent 创建
# ---------------------------------------------------------------------------

class TestMasterAgentCreation:
    """验证 MasterAgent 实例化"""

    async def test_create_from_config(self):
        """从 AgentConfig 直接创建"""
        config = make_master_config()
        master = MasterAgent(config)
        assert master.status == AgentStatus.CREATED
        assert master.name == "TestMaster"
        assert master.config.metadata.role == "master"

    async def test_create_from_config_dir(self):
        """从配置目录创建"""
        master = MasterAgent.from_config_dir("master")
        assert master.name == "MasterAgent"
        assert master.config.metadata.role == "master"

    async def test_initialize_and_run(self):
        """初始化并运行简单任务（无 LLM 客户端时的退化模式）"""
        # api_key 为空，MasterAgent 不会创建 LLM 客户端
        config = make_master_config(
            llm_config=LLMConfig(provider=LLMProvider.LOCAL, model="test", api_key=""),
        )
        master = MasterAgent(config)
        await master.initialize()
        assert master.status == AgentStatus.IDLE

        # 无 LLM 客户端时使用退化模式（echo）
        result = await master.run(task="Hello Master!")
        assert result.status == AgentStatus.COMPLETED
        assert result.iterations == 1


# ---------------------------------------------------------------------------
# 测试: 子 Agent 管理
# ---------------------------------------------------------------------------

class TestSubAgentManagement:
    """验证子 Agent 的创建与管理"""

    async def test_create_sub_agent_default(self):
        """创建子 Agent（使用默认配置，无 YAML）"""
        config = make_master_config()
        master = MasterAgent(config)

        sub = master.create_sub_agent(
            role="coder",
            task="写一个排序算法",
        )
        assert sub.status == AgentStatus.CREATED
        assert sub.config.metadata.role == "coder"
        # env 应继承自 MasterAgent
        assert sub.env == master.env

    async def test_create_sub_agent_custom_env(self):
        """创建子 Agent 并指定自定义 env"""
        config = make_master_config()
        master = MasterAgent(config)

        custom_env = str(Path(__file__).parent)
        sub = master.create_sub_agent(
            role="reviewer",
            task="审查代码",
            env=custom_env,
        )
        assert sub.env == custom_env

    async def test_create_sub_agent_from_yaml(self):
        """创建子 Agent（从 YAML 配置加载 master 角色）"""
        config = make_master_config()
        master = MasterAgent(config)

        # 使用 master 作为角色（有 config.yaml）
        sub = master.create_sub_agent(
            role="master",
            name="SubMaster",
            task="协调子任务",
        )
        assert sub.name == "SubMaster"
        assert sub.config.metadata.role == "master"

    async def test_get_sub_agent(self):
        """get_sub_agent 应返回已创建的子 Agent"""
        config = make_master_config()
        master = MasterAgent(config)

        sub = master.create_sub_agent(role="coder", task="test")
        retrieved = master.get_sub_agent(sub.agent_id)
        assert retrieved is sub

    async def test_get_sub_agents(self):
        """get_sub_agents 应返回所有子 Agent"""
        config = make_master_config()
        master = MasterAgent(config)

        master.create_sub_agent(role="coder", task="task1")
        master.create_sub_agent(role="reviewer", task="task2")

        subs = master.get_sub_agents()
        assert len(subs) == 2

    async def test_get_nonexistent_sub_agent(self):
        """查询不存在的子 Agent 应返回 None"""
        config = make_master_config()
        master = MasterAgent(config)

        assert master.get_sub_agent("nonexistent-id") is None


# ---------------------------------------------------------------------------
# 测试: 子 Agent 运行
# ---------------------------------------------------------------------------

class TestSubAgentExecution:
    """验证子 Agent 的执行"""

    async def test_run_sub_agent(self):
        """运行子 Agent 应返回 TaskResult"""
        config = make_master_config()
        master = MasterAgent(config)
        await master.initialize()

        # 创建 EchoAgent 子 Agent 并手动替换
        sub_config = make_master_config(name="EchoSub")
        sub = EchoAgent(sub_config)

        # 手动注册到 master
        master._sub_agents[sub.agent_id] = SubAgentRecord(
            agent=sub, role="echo", task="Hello Sub!",
        )

        result = await master.run_sub_agent(sub.agent_id)
        assert result.success
        assert result.output == "Echo: Hello Sub!"

    async def test_run_sub_agent_not_found(self):
        """运行未注册的子 Agent 应抛出 KeyError"""
        config = make_master_config()
        master = MasterAgent(config)
        await master.initialize()

        with pytest.raises(KeyError):
            await master.run_sub_agent("nonexistent-id")

    async def test_run_all_sub_agents_serial(self):
        """串行运行所有子 Agent"""
        config = make_master_config()
        master = MasterAgent(config)
        await master.initialize()

        # 注册两个 EchoAgent
        for i in range(2):
            sub_config = make_master_config(name=f"Echo{i}")
            sub = EchoAgent(sub_config)
            master._sub_agents[sub.agent_id] = SubAgentRecord(
                agent=sub, role="echo", task=f"Hello {i}!",
            )

        results = await master.run_all_sub_agents(parallel=False)
        assert len(results) == 2
        assert all(r.success for r in results.values())

    async def test_run_all_sub_agents_parallel(self):
        """并行运行所有子 Agent"""
        config = make_master_config()
        master = MasterAgent(config)
        await master.initialize()

        for i in range(3):
            sub_config = make_master_config(name=f"Echo{i}")
            sub = EchoAgent(sub_config)
            master._sub_agents[sub.agent_id] = SubAgentRecord(
                agent=sub, role="echo", task=f"Task {i}",
            )

        results = await master.run_all_sub_agents(parallel=True)
        assert len(results) == 3
        assert all(r.success for r in results.values())


# ---------------------------------------------------------------------------
# 测试: 内置工具注册
# ---------------------------------------------------------------------------

class TestMasterTools:
    """验证 MasterAgent 内置工具"""

    async def test_master_tools_registered(self):
        """MasterAgent 应注册 4 个内置工具"""
        config = make_master_config()
        master = MasterAgent(config)

        tool_names = [d.name for d in master.tool_registry._definitions.values()]
        assert "create_sub_agent" in tool_names
        assert "run_sub_agent" in tool_names
        assert "list_sub_agents" in tool_names
        assert "list_available_roles" in tool_names

    async def test_create_sub_agent_tool(self):
        """create_sub_agent 工具应能创建子 Agent"""
        config = make_master_config()
        master = MasterAgent(config)

        result = await master.tool_registry.execute(
            "create_sub_agent",
            {"role": "coder", "task": "写代码"},
        )
        data = json.loads(result)
        assert data["role"] == "coder"
        assert data["task"] == "写代码"
        assert data["status"] == "created"
        assert len(master.get_sub_agents()) == 1

    async def test_list_sub_agents_tool(self):
        """list_sub_agents 工具应返回子 Agent 列表"""
        config = make_master_config()
        master = MasterAgent(config)

        # 先创建一个子 Agent
        master.create_sub_agent(role="coder", task="test")

        result = await master.tool_registry.execute("list_sub_agents", {})
        data = json.loads(result)
        assert len(data) == 1
        assert data[0]["role"] == "coder"

    async def test_list_available_roles_tool(self):
        """list_available_roles 工具应返回已配置的角色列表"""
        config = make_master_config()
        master = MasterAgent(config)

        result = await master.tool_registry.execute("list_available_roles", {})
        data = json.loads(result)
        assert "master" in data["available_roles"]


# ---------------------------------------------------------------------------
# 测试: 生命周期
# ---------------------------------------------------------------------------

class TestMasterLifecycle:
    """验证 MasterAgent 生命周期"""

    async def test_destroy_destroys_sub_agents(self):
        """销毁 MasterAgent 应同时销毁所有子 Agent"""
        config = make_master_config()
        master = MasterAgent(config)
        await master.initialize()

        sub1 = master.create_sub_agent(role="coder", task="t1")
        sub2 = master.create_sub_agent(role="reviewer", task="t2")

        # 初始化子 Agent
        await sub1.initialize()
        await sub2.initialize()

        await master.destroy()

        assert master.status == AgentStatus.DESTROYED
        assert sub1.status == AgentStatus.DESTROYED
        assert sub2.status == AgentStatus.DESTROYED
        assert len(master.get_sub_agents()) == 0

    async def test_summary_includes_sub_agents(self):
        """to_summary 应包含子 Agent 信息"""
        config = make_master_config()
        master = MasterAgent(config)

        master.create_sub_agent(role="coder", task="t1")
        master.create_sub_agent(role="reviewer", task="t2")

        summary = master.to_summary()
        assert summary["sub_agent_count"] == 2
        assert len(summary["sub_agents"]) == 2


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
