"""YouMi Agent — 多Agent协作框架"""

from youmi.core.agent import Agent, AgentConfig, AgentStatus
from youmi.core.tool import ToolDefinition, ToolRegistry
from youmi.llm.client import LLMClient, LLMResponse
from youmi.memory.memory import MemoryManager
from youmi.memory.strategies.base import MemoryStrategy
from youmi.mcp import (
    MCPServer,
    MCPClient,
    ToolBridge,
    ToolProvider,
    LocalFunctionProvider,
    ToolIssueType,
    ToolIssueReport,
)
from youmi.coordinator.master import MasterAgent
from youmi.coordinator.tool_guardian import ToolGuardianAgent, ToolModification
from youmi.bus import (
    WorkflowMessage,
    WorkflowMessageType,
    BusEnvelope,
    MessageBroker,
    InProcessBroker,
    BusServer,
    BusClient,
)
from youmi.tools import BuiltinToolProvider

__all__ = [
    "Agent",
    "AgentConfig",
    "AgentStatus",
    "ToolDefinition",
    "ToolRegistry",
    "LLMClient",
    "LLMResponse",
    "MemoryManager",
    "MemoryStrategy",
    # MCP
    "MCPServer",
    "MCPClient",
    "ToolBridge",
    "ToolProvider",
    "LocalFunctionProvider",
    "ToolIssueType",
    "ToolIssueReport",
    # Coordinator
    "MasterAgent",
    "ToolGuardianAgent",
    "ToolModification",
    # Message Bus
    "WorkflowMessage",
    "WorkflowMessageType",
    "BusEnvelope",
    "MessageBroker",
    "InProcessBroker",
    "BusServer",
    "BusClient",
    # Built-in Tools
    "BuiltinToolProvider",
]

__version__ = "0.1.0"
