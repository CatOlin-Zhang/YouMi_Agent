"""
子进程隔离 Agent 运行器 (SubProcessAgentRunner + SubProcessHandle)

将 SubAgent 运行在独立子进程中，通过 WebSocket 消息总线与 MasterAgent 通信。
子进程崩溃不影响 MasterAgent 进程。

架构::

    MasterAgent ──WebSocket──▶ BusServer ──▶ SubProcess (Agent)

用法::

    from youmi.coordinator.subprocess_agent import SubProcessAgentRunner

    runner = SubProcessAgentRunner(ws_url="ws://localhost:8765")
    handle = await runner.launch(agent_config)
    await handle.send_task("执行排序任务")
    result = await handle.wait_result(timeout=60.0)
    await handle.terminate()
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from youmi.core.agent import AgentConfig
    from youmi.bus.broker import MessageBroker

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 子进程结果
# ---------------------------------------------------------------------------

class SubProcessResult(BaseModel):
    """子进程 Agent 执行结果"""
    agent_id: str = ""
    status: str = "unknown"
    output: str = ""
    iterations: int = 0
    error: str | None = None
    success: bool = False


# ---------------------------------------------------------------------------
# 子进程句柄
# ---------------------------------------------------------------------------

class SubProcessHandle:
    """子进程 Agent 句柄 — 管理子进程生命周期和通信

    持有 asyncio.subprocess.Process 引用，提供:
    - send_task(): 通过消息总线发送任务
    - wait_result(): 等待子进程返回结果
    - terminate(): 终止子进程
    - is_alive: 子进程是否仍在运行
    """

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        agent_id: str,
        broker: Any,  # MessageBroker
        workflow_id: str = "",
    ) -> None:
        self._process = process
        self._agent_id = agent_id
        self._broker = broker
        self._workflow_id = workflow_id

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def is_alive(self) -> bool:
        """子进程是否仍在运行"""
        return self._process.returncode is None

    @property
    def pid(self) -> int | None:
        """子进程 PID"""
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        """子进程返回码（None 表示仍在运行）"""
        return self._process.returncode

    async def send_task(self, task: str) -> None:
        """通过消息总线向子进程 Agent 发送任务

        Args:
            task: 任务描述
        """
        from youmi.bus.message import WorkflowMessage, WorkflowMessageType
        from youmi.core.types import MessageRole

        wf_msg = WorkflowMessage(
            workflow_id=self._workflow_id,
            from_agent_id="master",  # MasterAgent 的标识
            to_agent_id=self._agent_id,
            msg_type=WorkflowMessageType.TASK,
            role=MessageRole.AGENT,
            content=task,
            metadata={"subprocess": True},
        )
        await self._broker.publish(wf_msg)
        logger.info(
            "SubProcessHandle: sent task to '%s' (pid=%s)",
            self._agent_id, self._process.pid,
        )

    async def wait_result(self, timeout: float = 120.0) -> SubProcessResult:
        """等待子进程 Agent 返回执行结果

        通过消息总线等待 FEEDBACK 消息。

        Args:
            timeout: 超时秒数，默认 120 秒

        Returns:
            SubProcessResult 包含执行状态和输出
        """
        from youmi.bus.message import WorkflowMessageType

        # 等待来自子进程的 FEEDBACK
        feedback = await self._broker.wait_for_message(
            "master",  # MasterAgent 接收子进程的 feedback
            timeout=timeout,
        )

        if feedback is None:
            # 超时 — 检查子进程是否还在运行
            if self.is_alive:
                logger.warning(
                    "SubProcess '%s' timed out (still alive, pid=%s)",
                    self._agent_id, self._process.pid,
                )
            return SubProcessResult(
                agent_id=self._agent_id,
                status="timeout",
                error=f"等待结果超时 ({timeout}s)",
            )

        # 解析 FEEDBACK 内容
        try:
            data = json.loads(feedback.content)
            return SubProcessResult(
                agent_id=self._agent_id,
                status=data.get("status", "unknown"),
                output=data.get("output", ""),
                iterations=data.get("iterations", 0),
                error=data.get("error"),
                success=data.get("success", False),
            )
        except (json.JSONDecodeError, TypeError):
            return SubProcessResult(
                agent_id=self._agent_id,
                status="unknown",
                output=feedback.content,
                success=feedback.msg_type == WorkflowMessageType.FEEDBACK,
            )

    async def terminate(self) -> None:
        """终止子进程

        先尝试优雅关闭（发送 SIGTERM），等待 3 秒后强制杀死（SIGKILL）。
        """
        if not self.is_alive:
            return

        logger.info(
            "Terminating subprocess '%s' (pid=%s)",
            self._agent_id, self._process.pid,
        )

        try:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "Subprocess '%s' did not exit gracefully, killing",
                    self._agent_id,
                )
                self._process.kill()
                await self._process.wait()
        except ProcessLookupError:
            pass  # 进程已经退出

    async def wait_exit(self, timeout: float = 30.0) -> int | None:
        """等待子进程退出

        Args:
            timeout: 超时秒数

        Returns:
            子进程返回码，超时返回 None
        """
        try:
            await asyncio.wait_for(self._process.wait(), timeout=timeout)
            return self._process.returncode
        except asyncio.TimeoutError:
            return None


# ---------------------------------------------------------------------------
# 子进程启动器
# ---------------------------------------------------------------------------

class SubProcessAgentRunner:
    """子进程 Agent 启动器

    使用 asyncio.create_subprocess_exec 在独立进程中运行 Agent，
    通过 WebSocket 消息总线与 MasterAgent 通信。

    Args:
        ws_url: WebSocket 服务地址 (如 ws://localhost:8765)
        python_executable: Python 可执行文件路径，默认使用当前 Python

    用法::

        runner = SubProcessAgentRunner(ws_url="ws://localhost:8765")
        handle = await runner.launch(agent_config)
        await handle.send_task("执行任务")
        result = await handle.wait_result(timeout=60.0)
        await handle.terminate()
    """

    def __init__(
        self,
        ws_url: str = "ws://localhost:8765",
        python_executable: str = "",
    ) -> None:
        self._ws_url = ws_url
        self._python = python_executable or sys.executable

    async def launch(
        self,
        config: AgentConfig,
        broker: Any,  # MessageBroker
        workflow_id: str = "",
    ) -> SubProcessHandle:
        """启动子进程 Agent

        将 AgentConfig 序列化为 JSON，通过命令行参数传给子进程。
        子进程启动后通过 WebSocket 连接到 BusServer。

        Args:
            config: AgentConfig 实例
            broker: MessageBroker 实例（用于发送任务和接收结果）
            workflow_id: 工作流 ID

        Returns:
            SubProcessHandle 子进程句柄

        Raises:
            RuntimeError: 子进程启动失败
        """
        config_json = config.model_dump_json()

        logger.info(
            "Launching subprocess agent: name=%s id=%s ws=%s",
            config.name, config.agent_id, self._ws_url,
        )

        # 启动子进程
        process = await asyncio.create_subprocess_exec(
            self._python,
            "-m", "youmi.coordinator._subprocess_entry",
            config_json,
            self._ws_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # 等待一小段时间确保子进程启动成功
        await asyncio.sleep(0.5)

        if process.returncode is not None:
            # 子进程已经退出（启动失败）
            stderr = await process.stderr.read() if process.stderr else b""
            raise RuntimeError(
                f"Subprocess agent '{config.name}' failed to start "
                f"(returncode={process.returncode}): {stderr.decode(errors='replace')}"
            )

        handle = SubProcessHandle(
            process=process,
            agent_id=config.agent_id,
            broker=broker,
            workflow_id=workflow_id,
        )

        logger.info(
            "Subprocess agent launched: name=%s pid=%s id=%s",
            config.name, process.pid, config.agent_id,
        )

        return handle

    async def launch_and_run(
        self,
        config: AgentConfig,
        task: str,
        broker: Any,  # MessageBroker
        workflow_id: str = "",
        timeout: float = 120.0,
    ) -> SubProcessResult:
        """启动子进程、发送任务、等待结果（便捷方法）

        Args:
            config: AgentConfig 实例
            task: 任务描述
            broker: MessageBroker 实例
            workflow_id: 工作流 ID
            timeout: 等待结果超时秒数

        Returns:
            SubProcessResult
        """
        handle = await self.launch(config, broker, workflow_id)

        try:
            # 等待子进程连接就绪
            await asyncio.sleep(1.0)

            # 发送任务
            await handle.send_task(task)

            # 等待结果
            result = await handle.wait_result(timeout=timeout)

            return result

        finally:
            # 确保子进程退出
            await handle.terminate()
