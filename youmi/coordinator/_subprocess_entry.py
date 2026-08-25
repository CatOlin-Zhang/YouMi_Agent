"""
子进程 Agent 入口脚本

此脚本由 SubProcessAgentRunner 通过 asyncio.create_subprocess_exec 启动。
通过命令行参数接收 AgentConfig JSON 和 WebSocket URL。

流程:
1. 解析命令行参数 (config_json, ws_url)
2. 从 JSON 创建 AgentConfig
3. 创建 Agent 实例
4. 通过 BusClient (WebSocket) 连接到 BusServer
5. 初始化 Agent
6. 监听 TASK 消息 → 执行 run() → 发送 FEEDBACK 结果
7. 退出

用法 (内部使用，不直接调用)::

    python -m youmi.coordinator._subprocess_entry '{"agent_id": "...", ...}' ws://localhost:8765
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import traceback

logger = logging.getLogger(__name__)


async def _run_agent(config_json: str, ws_url: str) -> None:
    """子进程主逻辑"""
    from youmi.core.agent import Agent, AgentConfig, AgentStatus
    from youmi.bus.ws_client import BusClient
    from youmi.bus.message import (
        WorkflowMessage,
        WorkflowMessageType,
        BusEnvelope,
    )

    # 1. 解析配置
    config_data = json.loads(config_json)
    config = AgentConfig(**config_data)
    agent_id = config.agent_id

    logging.basicConfig(
        level=logging.INFO,
        format=f"[SubProcess:{agent_id[:8]}] %(levelname)s %(message)s",
    )
    logger.info("SubProcess agent starting: name=%s id=%s", config.name, agent_id)

    # 2. 创建 Agent
    agent = Agent(config)

    # 3. 通过 WebSocket 连接到 BusServer
    bus_client = BusClient(agent_id=agent_id, url=ws_url)
    await bus_client.connect(workflow_id="")
    agent.connect_bus(bus_client)

    # 4. 初始化
    await agent.initialize()
    logger.info("SubProcess agent initialized: %s", agent.name)

    # 5. 监听 TASK 消息
    logger.info("SubProcess agent waiting for task...")
    task_msg = await bus_client.wait_for_message(agent_id, timeout=300.0)

    if task_msg is None:
        logger.warning("SubProcess agent timed out waiting for task")
        await bus_client.close()
        return

    if task_msg.msg_type != WorkflowMessageType.TASK:
        logger.warning(
            "SubProcess agent received non-task message: %s",
            task_msg.msg_type.value,
        )
        await bus_client.close()
        return

    logger.info(
        "SubProcess agent received task from '%s': %s",
        task_msg.from_agent_id,
        task_msg.content[:80],
    )

    # 6. 执行任务
    try:
        result = await agent.run(task=task_msg.content, task_id=agent_id)

        # 7. 发送 FEEDBACK 结果
        feedback = WorkflowMessage(
            workflow_id=task_msg.workflow_id,
            from_agent_id=agent_id,
            to_agent_id=task_msg.from_agent_id,
            msg_type=WorkflowMessageType.FEEDBACK,
            content=json.dumps({
                "status": result.status.value,
                "output": str(result.output) if result.output else "",
                "iterations": result.iterations,
                "error": result.error,
            }, ensure_ascii=False),
            metadata={
                "subprocess": True,
                "success": result.success,
            },
        )
        await bus_client.publish(feedback)
        logger.info(
            "SubProcess agent finished: status=%s iterations=%d",
            result.status.value, result.iterations,
        )

    except Exception as exc:
        tb_str = traceback.format_exc()
        logger.exception("SubProcess agent failed: %s", exc)

        # 发送错误 FEEDBACK
        error_feedback = WorkflowMessage(
            workflow_id=task_msg.workflow_id,
            from_agent_id=agent_id,
            to_agent_id=task_msg.from_agent_id,
            msg_type=WorkflowMessageType.FEEDBACK,
            content=json.dumps({
                "status": "failed",
                "output": "",
                "iterations": 0,
                "error": f"{type(exc).__name__}: {exc}",
            }, ensure_ascii=False),
            metadata={"subprocess": True, "success": False},
        )
        await bus_client.publish(error_feedback)

    finally:
        # 清理
        try:
            await agent.destroy()
        except Exception:
            pass
        try:
            await bus_client.close()
        except Exception:
            pass

    logger.info("SubProcess agent exiting: %s", agent.name)


def main() -> None:
    """子进程入口"""
    if len(sys.argv) < 3:
        print("Usage: python -m youmi.coordinator._subprocess_entry <config_json> <ws_url>",
              file=sys.stderr)
        sys.exit(1)

    config_json = sys.argv[1]
    ws_url = sys.argv[2]

    asyncio.run(_run_agent(config_json, ws_url))


if __name__ == "__main__":
    main()
