"""
MasterAgent — 主协调 Agent

继承自 Agent 基类，覆写 ReAct 阶段以实现：
1. 与用户进行初始对话，理解任务需求
2. 分析任务并决定需要哪些子 Agent
3. 实例化子 Agent 并分配任务
4. 收集子 Agent 结果并汇总反馈
5. (P1) 处理子 Agent 工具权限申请
6. (P1) 多轮任务循环 (conversation_loop)
7. (P1) 任务完成后后台流水线 (PostTaskPipeline)
8. (P1) 子 Agent 进程隔离 (SubProcessAgentRunner)

子 Agent 配置约定：
    每个 Agent 的配置存放在 youmi/agents/<agent_name>/ 目录下，
    包含 config.yaml 配置文件。MasterAgent 通过 load_agent_config()
    加载配置并实例化子 Agent。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from youmi.core.agent import Agent, AgentConfig, AgentStatus, TaskResult
from youmi.core.types import (
    AgentMessage,
    AgentMetadata,
    LLMConfig,
    MemoryConfig,
    MessageRole,
)
from youmi.agents import load_agent_config
from youmi.llm.client import LLMClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 任务简报模板 — 注入到 SubAgent 的 system prompt 中
# ---------------------------------------------------------------------------

_TASK_BRIEF_TEMPLATE = """

---
## 任务简报
你的任务: {task}

### 自检提醒
在开始工作前，请先评估你当前可用的工具是否足以完成任务。
如果工具不足，请尽力使用已有工具完成，必要时可以通过消息总线申请扩展工具。
"""

# 闲聊关键词 — 不视为新任务信号
_CHAT_KEYWORDS = frozenset({
    "你好", "您好", "hello", "hi", "hey",
    "谢谢", "感谢", "thanks", "thank you",
    "再见", "拜拜", "bye", "goodbye",
    "好的", "ok", "okay", "行",
    "嗯", "嗯嗯", "是", "不是",
    "对", "不对", "没问题",
})


# ---------------------------------------------------------------------------
# 子 Agent 注册表 — 记录 MasterAgent 创建的所有子 Agent
# ---------------------------------------------------------------------------

class SubAgentRecord:
    """子 Agent 运行记录"""

    def __init__(
        self,
        agent: Agent,
        role: str,
        task: str,
        isolated: bool = False,
    ) -> None:
        self.agent = agent
        self.role = role
        self.task = task
        self.result: TaskResult | None = None
        self.isolated = isolated
        # 进程隔离句柄 (仅 isolated=True 时使用)
        self._subprocess_handle: Any = None  # SubProcessHandle | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent.agent_id,
            "name": self.agent.name,
            "role": self.role,
            "task": self.task,
            "status": self.agent.status.value,
            "result": self.result.model_dump() if self.result else None,
            "isolated": self.isolated,
        }


# ---------------------------------------------------------------------------
# MasterAgent
# ---------------------------------------------------------------------------

class MasterAgent(Agent):
    """主协调 Agent

    MasterAgent 是框架的入口 Agent，负责：
    - 与用户进行初始对话
    - 分析任务并拆解子任务
    - 按需实例化子 Agent
    - 编排执行流程并收集结果

    用法::

        from youmi.coordinator.master import MasterAgent

        master = MasterAgent.from_config_dir()  # 从 youmi/agents/master/ 加载配置
        await master.initialize()
        result = await master.run("帮我写一个 Python 排序算法")

    子类可通过覆写 `_analyze_task()` 和 `_create_sub_agents()` 扩展分析逻辑。
    """

    def __init__(
        self,
        config: AgentConfig,
        memory_strategy: str | None = None,
        llm_call: Any | None = None,
    ) -> None:
        super().__init__(config, memory_strategy=memory_strategy, llm_call=llm_call)

        # 子 Agent 注册表
        self._sub_agents: dict[str, SubAgentRecord] = {}

        # P1: 工具申请待处理队列 {requester_agent_id: (tool_description, reason)}
        self._pending_tool_requests: dict[str, tuple[str, str]] = {}

        # P1: 工具申请监听任务
        self._tool_request_listener_task: asyncio.Task | None = None

        # P1: 后台流水线
        self._post_task_pipeline: Any = None  # PostTaskPipeline | None

        # structure.md §2: 三级审批模型
        self._auto_approve_list: set[str] = set()      # 自动审批工具清单
        self._sensitive_tools: set[str] = set()          # 需人工审批的敏感工具
        self._manual_review_queue: dict[str, dict[str, Any]] = {}  # 待人工审批队列

        # 注册 MasterAgent 内置工具
        self._register_master_tools()

    # -----------------------------------------------------------------------
    # 工厂方法
    # -----------------------------------------------------------------------

    @classmethod
    def from_config_dir(
        cls,
        agent_name: str = "master",
        overrides: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> MasterAgent:
        """从 youmi/agents/<agent_name>/config.yaml 加载配置并创建实例

        Args:
            agent_name: Agent 配置目录名，默认 "master"
            overrides: 覆盖配置项（优先级高于 YAML）
            **kwargs: 传给 __init__ 的额外参数

        Returns:
            MasterAgent 实例
        """
        data = load_agent_config(agent_name)

        # 应用覆盖
        if overrides:
            data.update(overrides)

        # 分离已知字段
        llm_data = data.pop("llm_config", {})
        memory_data = data.pop("memory_config", {})
        metadata_data = data.pop("metadata", {})

        config = AgentConfig(
            llm_config=LLMConfig(**llm_data),
            memory_config=MemoryConfig(**memory_data),
            metadata=AgentMetadata(**metadata_data),
            **data,
        )
        return cls(config, **kwargs)

    # -----------------------------------------------------------------------
    # 子 Agent 管理
    # -----------------------------------------------------------------------

    def create_sub_agent(
        self,
        role: str,
        name: str = "",
        system_prompt: str = "",
        task: str = "",
        allowed_tools: list[str] | None = None,
        env: str = "",
        config_overrides: dict[str, Any] | None = None,
        isolated: bool = False,
    ) -> Agent:
        """创建并注册一个子 Agent

        优先尝试从 youmi/agents/<role>/config.yaml 加载配置，
        找不到则使用参数构造默认配置。

        Args:
            role: Agent 角色标识（如 coder / reviewer / researcher）
            name: Agent 实例名称，默认使用 role
            system_prompt: 系统提示词覆盖
            task: 分配给子 Agent 的任务描述（仅记录，不自动执行）
            allowed_tools: 允许使用的工具列表
            env: 运行环境路径，默认继承 MasterAgent 的 env
            config_overrides: 其他配置覆盖项

        Returns:
            新创建的 Agent 实例（尚未 initialize）
        """
        agent_name = name or role

        # 尝试从配置目录加载
        try:
            data = load_agent_config(role)
            llm_data = data.pop("llm_config", {})
            memory_data = data.pop("memory_config", {})
            metadata_data = data.pop("metadata", {})

            # 移除 YAML 中的 name（使用参数传入的 agent_name）
            data.pop("name", None)

            # 参数覆盖
            if system_prompt:
                data["system_prompt"] = system_prompt
            if allowed_tools is not None:
                data["allowed_tools"] = allowed_tools

            if config_overrides:
                data.update(config_overrides)

            sub_config = AgentConfig(
                name=agent_name,
                llm_config=LLMConfig(**llm_data),
                memory_config=MemoryConfig(**memory_data),
                metadata=AgentMetadata(**metadata_data),
                env=env or self._env,
                **data,
            )
        except FileNotFoundError:
            # 没有配置文件，使用参数构造
            # 自动生成详细的系统提示词（基于 role + task）
            if not system_prompt:
                system_prompt = (
                    f"你是一个专业的 {role} Agent。\n"
                    f"你的具体任务：{task}\n\n"
                    f"要求：\n"
                    f"- 始终围绕上述任务进行回复和工作\n"
                    f"- 以 {role} 的专业视角分析和输出\n"
                    f"- 回复要有条理、具体、可执行\n"
                    f"- 用中文简洁回复"
                )

            sub_config = AgentConfig(
                name=agent_name,
                system_prompt=system_prompt or f"你是一个 {role} 角色的 Agent。",
                llm_config=self._config.llm_config,
                memory_config=self._config.memory_config,
                allowed_tools=allowed_tools or [],
                env=env or self._env,
                metadata=AgentMetadata(
                    display_name=agent_name,
                    role=role,
                    description=f"由 MasterAgent 创建的 {role} Agent",
                ),
            )

        # P1: 注入任务简报到 system prompt
        if task and not isolated:
            task_brief = _TASK_BRIEF_TEMPLATE.format(task=task[:500])
            sub_config = sub_config.model_copy(
                update={"system_prompt": sub_config.system_prompt + task_brief},
            )

        agent = Agent(sub_config)

        # 共享 LLM 客户端（如果 MasterAgent 有，且非隔离模式）
        if self._llm_client is not None and not isolated:
            agent._llm_client = self._llm_client

        # 注册
        record = SubAgentRecord(
            agent=agent,
            role=role,
            task=task,
            isolated=isolated,
        )
        self._sub_agents[agent.agent_id] = record

        logger.info(
            "MasterAgent created sub-agent: name=%s role=%s id=%s isolated=%s",
            agent.name, role, agent.agent_id, isolated,
        )
        return agent

    async def run_sub_agent(self, agent_id: str) -> TaskResult:
        """初始化并运行指定的子 Agent

        支持进程隔离模式：如果 SubAgentRecord.isolated=True，
        通过 SubProcessAgentRunner 在独立进程中执行。

        Args:
            agent_id: 子 Agent ID

        Returns:
            TaskResult

        Raises:
            KeyError: agent_id 不在子 Agent 注册表中
        """
        record = self._sub_agents.get(agent_id)
        if record is None:
            raise KeyError(f"子 Agent '{agent_id}' 未注册，请先调用 create_sub_agent()")

        # P1: 进程隔离模式
        if record.isolated:
            return await self._run_isolated_sub_agent(record)

        agent = record.agent

        # 初始化（如果尚未初始化）
        if agent.status == AgentStatus.CREATED:
            await agent.initialize()

        # 执行任务
        result = await agent.run(task=record.task, task_id=agent_id)
        record.result = result

        logger.info(
            "Sub-agent '%s' finished: status=%s iterations=%d",
            agent.name, result.status.value, result.iterations,
        )
        return result

    async def _run_isolated_sub_agent(self, record: SubAgentRecord) -> TaskResult:
        """在独立子进程中运行子 Agent

        通过 SubProcessAgentRunner 启动子进程，发送任务，等待结果。
        需要 MasterAgent 已连接 BusServer（WebSocket 模式）。

        Args:
            record: SubAgentRecord

        Returns:
            TaskResult
        """
        from youmi.coordinator.subprocess_agent import SubProcessAgentRunner

        if self._bus is None:
            logger.error(
                "Cannot run isolated sub-agent '%s': MasterAgent not connected to bus",
                record.agent.name,
            )
            result = TaskResult(
                agent_id=record.agent.agent_id,
                status=AgentStatus.FAILED,
                error="进程隔离模式需要 MasterAgent 连接 BusServer",
            )
            record.result = result
            return result

        # 推断 WebSocket URL（从 bus 连接信息）
        ws_url = getattr(self._bus, '_ws_url', 'ws://localhost:8765')

        runner = SubProcessAgentRunner(ws_url=ws_url)

        try:
            sp_result = await runner.launch_and_run(
                config=record.agent.config,
                task=record.task,
                broker=self._bus,
                workflow_id=self._workflow_id,
                timeout=300.0,
            )

            result = TaskResult(
                agent_id=record.agent.agent_id,
                status=AgentStatus.COMPLETED if sp_result.success else AgentStatus.FAILED,
                output=sp_result.output,
                iterations=sp_result.iterations,
                error=sp_result.error,
            )
        except Exception as exc:
            logger.exception("Isolated sub-agent '%s' failed: %s", record.agent.name, exc)
            result = TaskResult(
                agent_id=record.agent.agent_id,
                status=AgentStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )

        record.result = result
        return result

    async def run_all_sub_agents(self, parallel: bool = False) -> dict[str, TaskResult]:
        """运行所有已注册但未执行的子 Agent

        Args:
            parallel: 是否并行执行（默认串行）

        Returns:
            {agent_id: TaskResult} 映射
        """
        import asyncio

        pending = [
            rec for rec in self._sub_agents.values()
            if rec.result is None and rec.agent.status in (
                AgentStatus.CREATED, AgentStatus.IDLE,
            )
        ]

        results: dict[str, TaskResult] = {}

        if parallel:
            tasks = [self.run_sub_agent(rec.agent.agent_id) for rec in pending]
            task_results = await asyncio.gather(*tasks, return_exceptions=True)
            for rec, res in zip(pending, task_results):
                if isinstance(res, Exception):
                    logger.error("Sub-agent '%s' failed: %s", rec.agent.name, res)
                    results[rec.agent.agent_id] = TaskResult(
                        agent_id=rec.agent.agent_id,
                        status=AgentStatus.FAILED,
                        error=str(res),
                    )
                else:
                    results[rec.agent.agent_id] = res
        else:
            for rec in pending:
                result = await self.run_sub_agent(rec.agent.agent_id)
                results[rec.agent.agent_id] = result

        return results

    def get_sub_agent(self, agent_id: str) -> Agent | None:
        """获取子 Agent 实例"""
        record = self._sub_agents.get(agent_id)
        return record.agent if record else None

    def get_sub_agents(self) -> dict[str, SubAgentRecord]:
        """获取所有子 Agent 记录"""
        return dict(self._sub_agents)

    # -----------------------------------------------------------------------
    # 内置工具注册
    # -----------------------------------------------------------------------

    def _register_master_tools(self) -> None:
        """注册 MasterAgent 专用工具

        委托给 youmi.tools.coordinator_ops 层实现，
        工具代码统一存放在 tools/ 目录下。
        """
        from youmi.tools.coordinator_ops import register_coordinator_tools
        register_coordinator_tools(self)

    # -----------------------------------------------------------------------
    # P1: 工具申请处理
    # -----------------------------------------------------------------------

    async def _start_tool_request_listener(self) -> None:
        """启动工具申请监听任务

        在后台持续监听 SubAgent 发送的 TOOL_REQUEST 消息。
        自动 subscribe 到 broker（如果尚未 subscribe）。
        """
        if self._bus is None:
            return

        # 确保 MasterAgent 已 subscribe 到 broker
        try:
            await self._bus.subscribe(self.agent_id, self._workflow_id)
        except Exception:
            pass  # 已 subscribe 或不支持 subscribe 时忽略

        async def _listener():
            from youmi.bus.message import WorkflowMessage, WorkflowMessageType
            while self._status in (AgentStatus.RUNNING, AgentStatus.IDLE):
                try:
                    msg = await self._bus.wait_for_message(
                        self.agent_id, timeout=2.0,
                    )
                    if msg is None:
                        continue
                    if msg.msg_type == WorkflowMessageType.TOOL_REQUEST:
                        await self._handle_tool_request(msg)
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    logger.debug("Tool request listener error: %s", exc)

        self._tool_request_listener_task = asyncio.create_task(_listener())
        logger.info("MasterAgent tool request listener started")

    async def _handle_tool_request(self, message: Any) -> None:
        """处理子 Agent 的工具申请

        解析申请内容，在已有工具库中搜索匹配，回复批准或拒绝。
        支持三级审批模型 (structure.md §2):
        - 自动审批: 工具在 auto_approve_list 中
        - 人工审批: 工具在 sensitive_tools 中
        - Master 审批: 其他情况，自动匹配后批准

        Args:
            message: WorkflowMessage (TOOL_REQUEST)
        """
        from youmi.bus.message import WorkflowMessage, WorkflowMessageType

        requester_id = message.from_agent_id
        try:
            req_data = json.loads(message.content)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Invalid tool request from '%s'", requester_id)
            return

        tool_desc = req_data.get("tool_description", "")
        reason = req_data.get("reason", "")

        logger.info(
            "Tool request from '%s': %s (reason: %s)",
            requester_id, tool_desc, reason[:60],
        )

        # 记录待处理申请
        self._pending_tool_requests[requester_id] = (tool_desc, reason)

        # ---- 搜索匹配工具 ----
        available_tools = list(self._tool_registry.tool_names) if self._tool_registry else []
        matched_tools: list[str] = []

        # 优先: ToolVault 向量搜索 (structure.md §2 自然语言工具发现)
        vault = getattr(self._tool_bridge, '_vault', None) if self._tool_bridge else None
        if vault is not None:
            try:
                search_results = await vault.search(tool_desc, top_k=5, min_score=0.2)
                for r in search_results:
                    if r.tool_name not in matched_tools:
                        matched_tools.append(r.tool_name)
            except Exception as exc:
                logger.debug("ToolVault search in _handle_tool_request failed: %s", exc)

        # 回退: 关键词匹配 + 工具描述搜索
        if not matched_tools:
            keywords = [kw for kw in tool_desc.lower().split() if len(kw) > 2]
            for tool_name in available_tools:
                name_lower = tool_name.lower()
                if any(kw in name_lower for kw in keywords):
                    matched_tools.append(tool_name)
                    continue
                # 也搜索工具描述
                if self._tool_registry:
                    defn = self._tool_registry._definitions.get(tool_name)
                    if defn and any(kw in defn.description.lower() for kw in keywords):
                        matched_tools.append(tool_name)

        # ---- 三级审批决策 (structure.md §2) ----
        approved = False
        approval_mode = "master"  # 默认 Master 审批

        if matched_tools:
            # 检查自动审批清单
            auto_approved = [t for t in matched_tools if t in self._auto_approve_list]
            # 检查敏感工具
            sensitive = [t for t in matched_tools if t in self._sensitive_tools]

            if auto_approved and not sensitive:
                # 自动审批: 工具在可扩展清单内
                approved = True
                approval_mode = "auto"
                matched_tools = auto_approved
            elif sensitive:
                # 人工审批: 涉及敏感工具，加入待审核队列
                self._manual_review_queue[requester_id] = {
                    "tool_desc": tool_desc,
                    "reason": reason,
                    "matched_tools": sensitive,
                }
                response_content = json.dumps({
                    "approved": False,
                    "reason": f"工具 {', '.join(sensitive)} 需要人工审批，已加入待审核队列",
                    "pending_manual_review": True,
                }, ensure_ascii=False)
                response_msg = WorkflowMessage(
                    workflow_id=message.workflow_id,
                    from_agent_id=self.agent_id,
                    to_agent_id=requester_id,
                    msg_type=WorkflowMessageType.TOOL_RESPONSE,
                    role=MessageRole.AGENT,
                    content=response_content,
                    metadata={"approved": False, "pending_manual_review": True},
                )
                await self._bus.publish(response_msg)
                logger.info(
                    "Tool request from '%s' queued for manual review: %s",
                    requester_id, sensitive,
                )
                return  # 不立即回复，等待人工确认
            else:
                # Master 审批: 自动批准匹配到的工具
                approved = True
                approval_mode = "master"

        # ---- 将工具添加到 SubAgent 的 ToolBridge (structure.md §2 热更新时序) ----
        if approved and matched_tools:
            record = self._sub_agents.get(requester_id)
            if record is not None:
                bridge = record.agent._tool_bridge
                for tn in matched_tools:
                    if bridge is not None:
                        bridge.add_allowed_tool(tn)
                    # 同步更新 config.allowed_tools
                    current = list(record.agent.config.allowed_tools)
                    if tn not in current:
                        current.append(tn)
                    record.agent._config = record.agent.config.model_copy(
                        update={"allowed_tools": current}
                    )
                logger.info(
                    "ToolBridge updated for '%s': +%s (approval=%s)",
                    requester_id, matched_tools, approval_mode,
                )

        if approved:
            response_content = json.dumps({
                "approved": True,
                "matched_tools": matched_tools,
                "approval_mode": approval_mode,
                "reason": f"找到匹配工具 ({approval_mode}): {', '.join(matched_tools)}",
            }, ensure_ascii=False)
            logger.info(
                "Tool request approved for '%s': %s (mode=%s)",
                requester_id, matched_tools, approval_mode,
            )
        else:
            response_content = json.dumps({
                "approved": False,
                "reason": f"未找到匹配的工具，当前可用: {', '.join(available_tools[:10])}",
            }, ensure_ascii=False)
            logger.info(
                "Tool request denied for '%s': no matching tools",
                requester_id,
            )

        # 发送回复
        response_msg = WorkflowMessage(
            workflow_id=message.workflow_id,
            from_agent_id=self.agent_id,
            to_agent_id=requester_id,
            msg_type=WorkflowMessageType.TOOL_RESPONSE,
            role=MessageRole.AGENT,
            content=response_content,
            metadata={"approved": approved},
        )
        await self._bus.publish(response_msg)

        # 清理待处理队列
        self._pending_tool_requests.pop(requester_id, None)

    def approve_tool_request(self, agent_id: str, tool_names: list[str]) -> bool:
        """手动批准子 Agent 的工具申请

        将工具添加到 SubAgent 的 ToolBridge 和 config.allowed_tools，
        使下一轮 _think() 自动包含新工具 (structure.md §2 热更新时序)。

        Args:
            agent_id: 子 Agent ID
            tool_names: 批准的工具名列表

        Returns:
            True 如果成功批准
        """
        record = self._sub_agents.get(agent_id)
        if record is None:
            return False

        # 1. 更新 ToolBridge (structure.md §2: add_allowed_tool 立即生效)
        bridge = record.agent._tool_bridge
        if bridge is not None:
            for tn in tool_names:
                bridge.add_allowed_tool(tn)

        # 2. 同步更新 config.allowed_tools
        current = list(record.agent.config.allowed_tools)
        changed = False
        for tn in tool_names:
            if tn not in current:
                current.append(tn)
                changed = True
        if changed:
            record.agent._config = record.agent.config.model_copy(
                update={"allowed_tools": current}
            )

        # 3. 清理待处理队列
        self._pending_tool_requests.pop(agent_id, None)
        self._manual_review_queue.pop(agent_id, None)

        logger.info("Approved tools for '%s': %s", agent_id, tool_names)
        return True

    def deny_tool_request(self, agent_id: str, reason: str = "") -> bool:
        """拒绝子 Agent 的工具申请

        Args:
            agent_id: 子 Agent ID
            reason: 拒绝原因

        Returns:
            True 如果成功拒绝
        """
        if agent_id in self._pending_tool_requests:
            self._pending_tool_requests.pop(agent_id)
            logger.info("Denied tool request from '%s': %s", agent_id, reason)
            return True
        return False

    def set_auto_approve_list(self, tool_names: list[str]) -> None:
        """设置自动审批工具清单 (structure.md §2 审批决策模型)

        工具在此清单内时，SubAgent 的工具申请将被自动批准，
        无需 MasterAgent 干预。

        Args:
            tool_names: 自动审批的工具名称列表
        """
        self._auto_approve_list = set(tool_names)
        logger.info("Auto-approve list set: %s", tool_names)

    def set_sensitive_tools(self, tool_names: list[str]) -> None:
        """设置敏感工具清单 (structure.md §2 审批决策模型)

        工具在此清单内时，SubAgent 的申请将进入人工审批队列，
        暂停 Agent 等待用户确认。

        Args:
            tool_names: 需人工审批的工具名称列表
        """
        self._sensitive_tools = set(tool_names)
        logger.info("Sensitive tools list set: %s", tool_names)

    def get_manual_review_queue(self) -> dict[str, dict[str, Any]]:
        """获取待人工审批的工具申请队列

        Returns:
            {requester_agent_id: {tool_desc, reason, matched_tools}}
        """
        return dict(self._manual_review_queue)

    # -----------------------------------------------------------------------
    # P1: 新任务循环
    # -----------------------------------------------------------------------

    async def conversation_loop(
        self,
        max_turns: int = 0,
        exit_keywords: tuple[str, ...] = ("exit", "quit", "退出"),
    ) -> None:
        """多轮任务循环 — 等待用户新一轮对话

        循环调用 chat_turn() 与用户交互。检测到新任务信号时触发完整任务流程，
        任务完成后输出汇总并等待下一轮用户输入。

        Args:
            max_turns: 最大轮数，0 表示不限制
            exit_keywords: 退出关键词元组
        """
        turns = 0
        task_count = 0

        logger.info("MasterAgent conversation loop started")

        while True:
            # 检查退出条件
            if max_turns > 0 and turns >= max_turns:
                logger.info("Max turns (%d) reached, exiting loop", max_turns)
                break

            turns += 1

            # 通过 chat_turn 获取用户输入和回复
            # 注意: 实际场景中用户输入由 GUI/CLI 层提供
            # 这里提供一个接口供上层调用
            user_message = await self._get_user_input()
            if user_message is None:
                logger.info("No user input, exiting conversation loop")
                break

            # 检查退出关键词
            if user_message.strip().lower() in exit_keywords:
                logger.info("Exit keyword detected, exiting conversation loop")
                break

            # 检查是否为新任务信号
            if self._is_new_task_signal(user_message):
                task_count += 1
                logger.info(
                    "New task detected (task #%d): %s",
                    task_count, user_message[:80],
                )

                # 重置上一轮任务状态
                if task_count > 1:
                    await self.reset_for_new_task()

            # 执行对话轮次
            result = await self.chat_turn(user_message)

            logger.debug(
                "Turn %d result: iterations=%d error=%s",
                turns, result.get("iterations", 0), result.get("error"),
            )

        logger.info(
            "Conversation loop ended: %d turns, %d tasks",
            turns, task_count,
        )

    async def _get_user_input(self) -> str | None:
        """获取用户输入 — 子类可覆写

        默认返回 None（无输入）。GUI/CLI 层应覆写此方法，
        从用户界面获取输入。

        Returns:
            用户输入字符串，None 表示无输入
        """
        return None

    @staticmethod
    def _is_new_task_signal(message: str) -> bool:
        """判断消息是否为新任务信号

        简单规则：消息长度 > 10 且不匹配闲聊关键词。

        Args:
            message: 用户消息

        Returns:
            True 表示是新任务信号
        """
        if len(message.strip()) <= 10:
            return False
        msg_lower = message.strip().lower()
        return not any(kw in msg_lower for kw in _CHAT_KEYWORDS)

    async def reset_for_new_task(self) -> None:
        """重置状态以准备新一轮任务

        - 重置所有子 Agent 工具权限到初始状态 (structure.md §2 工作流级回收)
        - 销毁所有子 Agent
        - 清空子 Agent 注册表
        - 重置 MasterAgent 状态为 IDLE
        - 保留记忆系统（跨任务记忆持续）
        """
        logger.info("MasterAgent resetting for new task")

        # 停止工具申请监听
        if self._tool_request_listener_task:
            self._tool_request_listener_task.cancel()
            self._tool_request_listener_task = None

        # structure.md §2: 工作流级权限回收 — 重置所有子 Agent 工具权限
        for record in self._sub_agents.values():
            try:
                record.agent.reset_tool_permissions()
            except Exception as exc:
                logger.debug("Error resetting tool permissions for '%s': %s",
                             record.agent.name, exc)

        # 销毁所有子 Agent
        for record in self._sub_agents.values():
            if record.isolated and record._subprocess_handle:
                await record._subprocess_handle.terminate()
            elif record.agent.is_alive:
                try:
                    await record.agent.destroy()
                except Exception as exc:
                    logger.debug("Error destroying sub-agent: %s", exc)

        self._sub_agents.clear()
        self._pending_tool_requests.clear()
        self._manual_review_queue.clear()

        # 重置状态为 IDLE
        if self._status != AgentStatus.DESTROYED:
            self._status = AgentStatus.IDLE

        logger.info("MasterAgent reset complete")

    # -----------------------------------------------------------------------
    # 生命周期钩子
    # -----------------------------------------------------------------------

    async def on_initialize(self) -> None:
        """初始化钩子：创建 LLM 客户端"""
        # 创建 LLM 客户端（如果配置了 api_key）
        llm_cfg = self._config.llm_config
        if llm_cfg.api_key or llm_cfg.base_url:
            self._llm_client = LLMClient(llm_cfg)
            logger.info("MasterAgent LLM client created: model=%s", llm_cfg.model)

    async def on_start(self, task: str) -> None:
        """任务开始钩子：启动工具申请监听"""
        logger.info("MasterAgent starting task: %s", task[:100])

        # P1: 启动工具申请监听
        await self._start_tool_request_listener()

    async def on_stop(self, error: str | None) -> None:
        """任务结束钩子：汇总子 Agent 状态 + 触发后台流水线"""
        if self._sub_agents:
            summary = {
                aid: rec.agent.status.value
                for aid, rec in self._sub_agents.items()
            }
            logger.info("MasterAgent stopped. Sub-agents status: %s", summary)

        # P1: 停止工具申请监听
        if self._tool_request_listener_task:
            self._tool_request_listener_task.cancel()
            self._tool_request_listener_task = None

        # P1: 触发后台流水线
        completed_results = {
            aid: rec.result
            for aid, rec in self._sub_agents.items()
            if rec.result is not None
        }
        if completed_results:
            try:
                from youmi.coordinator.post_task import PostTaskPipeline
                pipeline = PostTaskPipeline()
                self._post_task_pipeline = pipeline
                # 在后台运行，不阻塞主流程
                asyncio.create_task(pipeline.run(self, completed_results))
                logger.info("PostTaskPipeline triggered in background")
            except Exception as exc:
                logger.debug("Failed to trigger PostTaskPipeline: %s", exc)

    async def on_destroy(self) -> None:
        """销毁钩子：销毁所有子 Agent + 清理资源"""
        # 停止工具申请监听
        if self._tool_request_listener_task:
            self._tool_request_listener_task.cancel()
            self._tool_request_listener_task = None

        for record in self._sub_agents.values():
            if record.isolated and record._subprocess_handle:
                await record._subprocess_handle.terminate()
            elif record.agent.is_alive:
                await record.agent.destroy()
        self._sub_agents.clear()
        logger.info("MasterAgent destroyed all sub-agents.")

    # -----------------------------------------------------------------------
    # 序列化
    # -----------------------------------------------------------------------

    def to_summary(self) -> dict[str, Any]:
        """MasterAgent 状态摘要（含子 Agent 信息）"""
        summary = super().to_summary()
        summary["sub_agents"] = {
            aid: rec.to_dict() for aid, rec in self._sub_agents.items()
        }
        summary["sub_agent_count"] = len(self._sub_agents)
        return summary
