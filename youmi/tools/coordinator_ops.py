"""
协调器工具 — MasterAgent 多 Agent 编排专用工具

提供 MasterAgent 用于子 Agent 创建、运行、查询的工具:
- create_sub_agent: 创建子 Agent（指定角色和任务）
- run_sub_agent: 运行已创建的子 Agent 并获取结果
- list_sub_agents: 列出所有子 Agent 及其状态
- list_available_roles: 列出所有已配置的 Agent 角色

用法::

    from youmi.tools.coordinator_ops import register_coordinator_tools

    # 在 MasterAgent 中调用
    register_coordinator_tools(self)
"""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

from youmi.core.tool import ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from youmi.coordinator.master import MasterAgent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 工具处理函数
# ---------------------------------------------------------------------------

async def create_sub_agent(master: MasterAgent, **kwargs: Any) -> str:
    """创建子 Agent。

    Args:
        master: MasterAgent 实例
        **kwargs: role(必需), task(必需), system_prompt(可选), allowed_tools(可选)

    Returns:
        JSON 字符串，包含 agent_id、name、role、task、status
    """
    role = kwargs.get("role", "general")
    task = kwargs.get("task", "")
    system_prompt = kwargs.get("system_prompt", "")
    allowed_tools = kwargs.get("allowed_tools") or []

    agent = master.create_sub_agent(
        role=role,
        task=task,
        system_prompt=system_prompt,
        allowed_tools=allowed_tools or None,
    )

    logger.info("Tool create_sub_agent: role=%s task=%s → id=%s",
                role, task[:60], agent.agent_id)

    return json.dumps({
        "agent_id": agent.agent_id,
        "name": agent.name,
        "role": role,
        "task": task,
        "status": "created",
    }, ensure_ascii=False)


async def run_sub_agent(master: MasterAgent, **kwargs: Any) -> str:
    """运行指定的子 Agent，让其执行已分配的任务。

    Args:
        master: MasterAgent 实例
        **kwargs: agent_id(必需)

    Returns:
        JSON 字符串，包含 agent_id、status、output、iterations
    """
    agent_id = kwargs.get("agent_id", "")

    try:
        result = await master.run_sub_agent(agent_id)
        logger.info("Tool run_sub_agent: %s → %s (%d iter)",
                    agent_id, result.status.value, result.iterations)
        return json.dumps({
            "agent_id": agent_id,
            "status": result.status.value,
            "output": result.output,
            "iterations": result.iterations,
            "error": result.error,
        }, ensure_ascii=False)
    except KeyError as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def list_sub_agents(master: MasterAgent, **kwargs: Any) -> str:
    """列出所有已创建的子 Agent 及其状态。

    Args:
        master: MasterAgent 实例

    Returns:
        JSON 字符串，包含每个子 Agent 的摘要信息和输出预览
    """
    agents_info = []
    for rec in master._sub_agents.values():
        info = rec.to_dict()
        # 添加输出预览（方便 LLM 决策）
        if rec.result and rec.result.output:
            output_str = str(rec.result.output)
            info["output_preview"] = output_str[:200] + ("..." if len(output_str) > 200 else "")
        agents_info.append(info)

    logger.info("Tool list_sub_agents: %d agents", len(agents_info))
    return json.dumps(agents_info, ensure_ascii=False)


async def list_available_roles(master: MasterAgent, **kwargs: Any) -> str:
    """列出所有已配置的 Agent 角色（在 youmi/agents/ 目录中有配置的）。

    Args:
        master: MasterAgent 实例

    Returns:
        JSON 字符串，包含可用角色列表
    """
    from youmi.agents import list_agents

    roles = list_agents()
    logger.info("Tool list_available_roles: %s", roles)
    return json.dumps({
        "available_roles": roles,
        "description": "这些角色在 youmi/agents/ 中有配置文件，可以直接用 create_sub_agent 创建",
    }, ensure_ascii=False)


async def approve_tool_request(master: MasterAgent, **kwargs: Any) -> str:
    """批准子 Agent 的工具权限申请。

    Args:
        master: MasterAgent 实例
        **kwargs: agent_id(必需), tool_names(必需)

    Returns:
        JSON 字符串，包含批准结果
    """
    agent_id = kwargs.get("agent_id", "")
    tool_names = kwargs.get("tool_names") or []

    ok = master.approve_tool_request(agent_id, tool_names)
    logger.info("Tool approve_tool_request: agent=%s tools=%s → %s",
                agent_id, tool_names, "approved" if ok else "failed")

    return json.dumps({
        "agent_id": agent_id,
        "approved": ok,
        "tool_names": tool_names,
    }, ensure_ascii=False)


async def deny_tool_request(master: MasterAgent, **kwargs: Any) -> str:
    """拒绝子 Agent 的工具权限申请。

    Args:
        master: MasterAgent 实例
        **kwargs: agent_id(必需), reason(可选)

    Returns:
        JSON 字符串，包含拒绝结果
    """
    agent_id = kwargs.get("agent_id", "")
    reason = kwargs.get("reason", "")

    ok = master.deny_tool_request(agent_id, reason)
    logger.info("Tool deny_tool_request: agent=%s → %s",
                agent_id, "denied" if ok else "no_pending_request")

    return json.dumps({
        "agent_id": agent_id,
        "denied": ok,
        "reason": reason,
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 工具定义 (ToolDefinition)
# ---------------------------------------------------------------------------

CREATE_SUB_AGENT_DEF = ToolDefinition(
    name="create_sub_agent",
    description=(
        "创建一个新的子 Agent 来执行特定任务。"
        "指定角色（如 coder、reviewer、researcher）和任务描述。"
        "创建后需要调用 run_sub_agent 来让它执行任务。"
    ),
    parameters=[
        ToolParameter(
            name="role",
            type="string",
            description="Agent 角色标识，如 coder/reviewer/researcher/writer",
            required=True,
        ),
        ToolParameter(
            name="task",
            type="string",
            description="分配给子 Agent 的具体任务描述",
            required=True,
        ),
        ToolParameter(
            name="system_prompt",
            type="string",
            description="自定义系统提示词（可选），覆盖角色默认提示",
            required=False,
            default="",
        ),
        ToolParameter(
            name="allowed_tools",
            type="array",
            description="允许使用的工具名称列表（可选）",
            required=False,
            items={"type": "string"},
        ),
    ],
)

RUN_SUB_AGENT_DEF = ToolDefinition(
    name="run_sub_agent",
    description=(
        "运行指定的子 Agent，让它执行已分配的任务并返回结果。"
        "必须先通过 create_sub_agent 创建后才能运行。"
    ),
    parameters=[
        ToolParameter(
            name="agent_id",
            type="string",
            description="要运行的子 Agent ID（从 create_sub_agent 返回）",
            required=True,
        ),
    ],
)

LIST_SUB_AGENTS_DEF = ToolDefinition(
    name="list_sub_agents",
    description="列出所有已创建的子 Agent 及其状态、输出预览。",
    parameters=[],
)

LIST_AVAILABLE_ROLES_DEF = ToolDefinition(
    name="list_available_roles",
    description="列出所有已配置的 Agent 角色（在 youmi/agents/ 目录中有配置的），可用于 create_sub_agent 的 role 参数。",
    parameters=[],
)

APPROVE_TOOL_REQUEST_DEF = ToolDefinition(
    name="approve_tool_request",
    description=(
        "批准子 Agent 的工具权限申请。"
        "当子 Agent 报告工具不足时，使用此工具批准其申请的工具。"
    ),
    parameters=[
        ToolParameter(
            name="agent_id",
            type="string",
            description="申请工具的子 Agent ID",
            required=True,
        ),
        ToolParameter(
            name="tool_names",
            type="array",
            description="批准的工具名称列表",
            required=True,
            items={"type": "string"},
        ),
    ],
)

DENY_TOOL_REQUEST_DEF = ToolDefinition(
    name="deny_tool_request",
    description="拒绝子 Agent 的工具权限申请。",
    parameters=[
        ToolParameter(
            name="agent_id",
            type="string",
            description="申请工具的子 Agent ID",
            required=True,
        ),
        ToolParameter(
            name="reason",
            type="string",
            description="拒绝原因",
            required=False,
            default="",
        ),
    ],
)


# ---------------------------------------------------------------------------
# 注册入口
# ---------------------------------------------------------------------------

def register_coordinator_tools(master: MasterAgent) -> None:
    """将 6 个协调器工具注册到 MasterAgent 的 ToolRegistry

    注册的工具:
    - create_sub_agent: 创建子 Agent
    - run_sub_agent: 运行子 Agent
    - list_sub_agents: 列出子 Agent
    - list_available_roles: 列出可用角色
    - approve_tool_request: 批准工具申请
    - deny_tool_request: 拒绝工具申请

    Args:
        master: MasterAgent 实例，工具处理函数会绑定到此实例
    """
    registry = master._tool_registry

    async def _create(**kwargs: Any) -> str:
        return await create_sub_agent(master, **kwargs)

    async def _run(**kwargs: Any) -> str:
        return await run_sub_agent(master, **kwargs)

    async def _list(**kwargs: Any) -> str:
        return await list_sub_agents(master, **kwargs)

    async def _roles(**kwargs: Any) -> str:
        return await list_available_roles(master, **kwargs)

    async def _approve(**kwargs: Any) -> str:
        return await approve_tool_request(master, **kwargs)

    async def _deny(**kwargs: Any) -> str:
        return await deny_tool_request(master, **kwargs)

    registry.register(CREATE_SUB_AGENT_DEF, _create)
    registry.register(RUN_SUB_AGENT_DEF, _run)
    registry.register(LIST_SUB_AGENTS_DEF, _list)
    registry.register(LIST_AVAILABLE_ROLES_DEF, _roles)
    registry.register(APPROVE_TOOL_REQUEST_DEF, _approve)
    registry.register(DENY_TOOL_REQUEST_DEF, _deny)

    logger.info(
        "Registered 6 coordinator tools for MasterAgent '%s': "
        "create_sub_agent, run_sub_agent, list_sub_agents, list_available_roles, "
        "approve_tool_request, deny_tool_request",
        master.name,
    )
