"""
Agent 间任务委派协议 (HandoffProtocol)

管理 Agent 间的 Handoff 通信:
- 自动匹配委派规则
- 发送/接收委派消息
- 跟踪委派链深度
- 处理委派结果回传

用法::

    from youmi.coordinator.handoff import HandoffProtocol

    # 创建协议管理器
    protocol = HandoffProtocol(broker=broker, workflow_id="wf-001")

    # 注册 Agent
    protocol.register_agent(agent_a)
    protocol.register_agent(agent_b)

    # Agent A 委派任务给 Agent B
    result = await protocol.handoff(
        from_agent=agent_a,
        target_agent_id=agent_b.agent_id,
        task="请帮我审查这段代码",
    )

    # 或者让协议自动匹配规则
    result = await protocol.auto_handoff(
        from_agent=agent_a,
        message_content="帮我审查这个 PR",
    )
"""

from __future__ import annotations

import logging
from typing import Any

from youmi.core.agent import Agent, _ActionResult
from youmi.core.types import HandoffRule

logger = logging.getLogger(__name__)


class HandoffProtocol:
    """Agent 间任务委派协议管理器

    封装 Agent 间的 Handoff 通信逻辑，提供:
    - Agent 注册表 (跟踪所有参与 handoff 的 Agent)
    - 自动规则匹配 (根据消息内容匹配 handoff_rules)
    - 委派执行 (发送 task 消息 + 等待 feedback)
    - 委派链深度追踪 (防止循环委派)

    Args:
        broker: MessageBroker 实例 (用于消息路由)
        workflow_id: 工作流 ID

    用法::

        protocol = HandoffProtocol(broker, workflow_id)
        protocol.register_agent(agent_a)
        protocol.register_agent(agent_b)

        # 显式委派
        result = await protocol.handoff(agent_a, agent_b.agent_id, "写代码")

        # 自动匹配规则委派
        result = await protocol.auto_handoff(agent_a, "帮我审查代码")
    """

    def __init__(
        self,
        broker: Any,  # MessageBroker
        workflow_id: str = "",
    ) -> None:
        self._broker = broker
        self._workflow_id = workflow_id
        self._agents: dict[str, Agent] = {}
        # 委派链追踪: {(from_agent_id, task_id): depth}
        self._delegation_chains: dict[tuple[str, str], int] = {}

    @property
    def registered_agents(self) -> dict[str, Agent]:
        return dict(self._agents)

    def register_agent(self, agent: Agent) -> None:
        """注册 Agent 到协议管理器

        Args:
            agent: Agent 实例 (需要已 connect_bus)
        """
        self._agents[agent.agent_id] = agent
        logger.info(
            "HandoffProtocol: registered agent '%s' (%s)",
            agent.name, agent.agent_id,
        )

    def unregister_agent(self, agent_id: str) -> None:
        """移除 Agent"""
        self._agents.pop(agent_id, None)

    async def handoff(
        self,
        from_agent: Agent,
        target_agent_id: str,
        task: str,
        depth: int = 0,
    ) -> _ActionResult:
        """执行任务委派

        Args:
            from_agent: 发起委派的 Agent
            target_agent_id: 目标 Agent ID
            task: 任务描述
            depth: 当前委派深度

        Returns:
            _ActionResult 包含委派结果
        """
        target = self._agents.get(target_agent_id)
        if target is None:
            return _ActionResult(
                success=False,
                error=f"目标 Agent '{target_agent_id}' 未在 HandoffProtocol 中注册",
            )

        # 检查目标 Agent 是否存活
        if not target.is_alive:
            return _ActionResult(
                success=False,
                error=f"目标 Agent '{target_agent_id}' 已终止",
            )

        # 调用发起方的 handoff 方法
        result = await from_agent.handoff(
            target_agent_id=target_agent_id,
            task=task,
        )

        logger.info(
            "Handoff %s → %s: success=%s",
            from_agent.name, target.name, result.success,
        )

        return result

    async def auto_handoff(
        self,
        from_agent: Agent,
        message_content: str,
        task: str = "",
    ) -> _ActionResult | None:
        """根据消息内容自动匹配委派规则并执行

        Args:
            from_agent: 发起委派的 Agent
            message_content: 消息内容 (用于规则匹配)
            task: 任务描述 (为空时使用 message_content)

        Returns:
            _ActionResult 或 None (无匹配规则时)
        """
        rule = from_agent.match_handoff_rule(message_content)
        if rule is None:
            return None

        logger.info(
            "Auto-handoff matched: rule='%s' target='%s' from='%s'",
            rule.name, rule.target_agent_id, from_agent.name,
        )

        # 使用规则模板
        actual_task = task or message_content
        message_template = rule.message_template

        return await from_agent._execute_delegation({
            "target_agent_id": rule.target_agent_id,
            "task": actual_task,
            "message_template": message_template,
            "depth": 0,
        })

    async def receive_and_process(
        self,
        target_agent: Agent,
        timeout: float = 30.0,
    ) -> bool:
        """接收委派消息并处理 (目标 Agent 端)

        等待并处理来自其他 Agent 的 task 消息:
        1. 从消息总线获取 task 消息
        2. 调用 Agent.run() 执行任务
        3. 将结果以 feedback 消息回传

        Args:
            target_agent: 目标 Agent (接收委派的一方)
            timeout: 等待消息超时秒数

        Returns:
            True 表示收到并处理了委派消息，False 表示超时未收到
        """
        from youmi.bus.message import WorkflowMessage, WorkflowMessageType

        if target_agent.bus is None:
            logger.warning(
                "Agent '%s' not connected to bus, cannot receive handoff",
                target_agent.name,
            )
            return False

        # 等待 task 消息
        msg = await target_agent.bus.wait_for_message(
            target_agent.agent_id, timeout=timeout,
        )
        if msg is None:
            return False

        if msg.msg_type != WorkflowMessageType.TASK:
            logger.debug(
                "Agent '%s' received non-task message (type=%s), ignoring",
                target_agent.name, msg.msg_type.value,
            )
            return False

        logger.info(
            "Agent '%s' received handoff from '%s': %s",
            target_agent.name, msg.from_agent_id, msg.content[:80],
        )

        # 执行任务
        depth = msg.metadata.get("depth", 0)
        original_agent_id = msg.metadata.get("original_agent_id", msg.from_agent_id)

        try:
            result = await target_agent.run(
                task=msg.content,
                task_id=f"handoff-{msg.message_id}",
            )

            # 回传 feedback
            feedback = WorkflowMessage(
                workflow_id=self._workflow_id,
                from_agent_id=target_agent.agent_id,
                to_agent_id=msg.from_agent_id,
                msg_type=WorkflowMessageType.FEEDBACK,
                content=str(result.output) if result.success else f"FAILED: {result.error}",
                metadata={
                    "delegation_result": True,
                    "depth": depth,
                    "original_agent_id": original_agent_id,
                    "success": result.success,
                },
            )
            await self._broker.publish(feedback)

        except Exception as exc:
            # 回传错误
            error_feedback = WorkflowMessage(
                workflow_id=self._workflow_id,
                from_agent_id=target_agent.agent_id,
                to_agent_id=msg.from_agent_id,
                msg_type=WorkflowMessageType.FEEDBACK,
                content=f"执行失败: {type(exc).__name__}: {exc}",
                metadata={"delegation_result": True, "success": False},
            )
            await self._broker.publish(error_feedback)
            logger.exception("Handoff task execution failed: %s", exc)

        return True

    def get_chain_depth(self, from_agent_id: str, task_id: str) -> int:
        """获取委派链深度"""
        return self._delegation_chains.get((from_agent_id, task_id), 0)

    def snapshot(self) -> dict[str, Any]:
        """协议状态快照"""
        return {
            "workflow_id": self._workflow_id,
            "registered_agents": list(self._agents.keys()),
            "active_chains": len(self._delegation_chains),
        }
