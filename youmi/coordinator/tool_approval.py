"""
工具审批 Mixin — MasterAgent 的工具申请处理逻辑

从 youmi/coordinator/master.py 提取，包含三级审批模型相关方法：
- _start_tool_request_listener  — 启动工具申请监听
- _handle_tool_request         — 处理子 Agent 工具申请
- approve_tool_request          — 手动批准工具申请
- deny_tool_request             — 拒绝工具申请
- set_auto_approve_list         — 设置自动审批清单
- set_sensitive_tools           — 设置敏感工具清单
- get_manual_review_queue       — 获取人工审批队列

通过 Mixin 注入 MasterAgent。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from youmi.core.models import AgentStatus
from youmi.core.types import MessageRole
from youmi.mcp.approval import ApprovalLevel

logger = logging.getLogger(__name__)


class ToolApprovalMixin:
    """工具审批 Mixin

    三级审批决策与审计日志委托 `youmi.mcp.approval.ApprovalManager`
    （实例属性 `_approval_manager`，由 MasterAgent.__init__ 初始化）。

    依赖 MasterAgent 的以下实例属性（由 __init__ 初始化）：
    - _bus: 消息总线
    - _workflow_id: 工作流 ID
    - agent_id: Agent ID (property)
    - _status: Agent 状态
    - _tool_request_listener_task: 监听任务
    - _tool_registry: 工具注册表
    - _tool_bridge: 工具桥接
    - _sub_agents: 子 Agent 注册表
    - _pending_tool_requests: 待处理工具申请
    - _approval_manager: ApprovalManager 审批管理器（决策 + 审计单一来源）
    - _auto_approve_list: 自动审批清单（镜像，与 manager 同步）
    - _sensitive_tools: 敏感工具清单（镜像，与 manager 同步）
    - _manual_review_queue: 人工审批队列（存放申请负载）
    """

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

        # ---- 三级审批决策（委托 ApprovalManager，含审计日志）----
        approved = False
        approval_mode = "master"  # 默认 Master 审批
        mgr = self._approval_manager

        if matched_tools:
            # 敏感工具优先：MANUAL 级别进入人工审批队列（暂停 Agent 等待确认）
            sensitive = [
                t for t in matched_tools
                if mgr.evaluate(requester_id, t) == ApprovalLevel.MANUAL
            ]

            if sensitive:
                # 人工审批：待审批记录由 ApprovalManager 保存，负载入队
                for tn in sensitive:
                    mgr.submit_request(requester_id, tn)
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
                # 逐个提交审批：AUTO 自动通过，MASTER 由 MasterAgent 批准匹配结果
                records = [mgr.submit_request(requester_id, tn) for tn in matched_tools]
                master_records = [
                    r for r in records if r.level == ApprovalLevel.MASTER
                ]
                for r in master_records:
                    mgr.approve(
                        r.record_id,
                        decided_by=self.agent_id,
                        reason="Master 审批：匹配到可用工具",
                    )
                approved = True
                approval_mode = "master" if master_records else "auto"

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

        # 3. 同步 ApprovalManager：批准该 Agent 的待审批记录（审计留痕）
        for rec in self._approval_manager.get_pending_for_agent(agent_id):
            self._approval_manager.approve(
                rec.record_id,
                decided_by="user",
                reason=f"人工批准: {', '.join(tool_names)}",
            )

        # 4. 清理待处理队列
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
        # 同步 ApprovalManager：拒绝该 Agent 的待审批记录（审计留痕）
        for rec in self._approval_manager.get_pending_for_agent(agent_id):
            self._approval_manager.deny(
                rec.record_id,
                decided_by=self.agent_id,
                reason=reason or "Master 拒绝",
            )

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
        self._approval_manager.set_auto_approve_list(set(tool_names))
        logger.info("Auto-approve list set: %s", tool_names)

    def set_sensitive_tools(self, tool_names: list[str]) -> None:
        """设置敏感工具清单 (structure.md §2 审批决策模型)

        工具在此清单内时，SubAgent 的申请将进入人工审批队列，
        暂停 Agent 等待用户确认。

        Args:
            tool_names: 需人工审批的工具名称列表
        """
        self._sensitive_tools = set(tool_names)
        self._approval_manager.set_sensitive_tools(set(tool_names))
        logger.info("Sensitive tools list set: %s", tool_names)

    def get_manual_review_queue(self) -> dict[str, dict[str, Any]]:
        """获取待人工审批的工具申请队列

        Returns:
            {requester_agent_id: {tool_desc, reason, matched_tools}}
        """
        return dict(self._manual_review_queue)

    def get_approval_audit_log(self) -> list[Any]:
        """获取完整工具审批审计日志（委托 ApprovalManager）

        Returns:
            按时间顺序的 ApprovalRecord 列表（含 auto/manual/master 全部决策）
        """
        return self._approval_manager.get_audit_log()
