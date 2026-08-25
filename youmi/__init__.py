"""YouMi Agent — 多Agent协作框架"""

from youmi.core.agent import Agent, AgentConfig, AgentStatus
from youmi.core.tool import ToolDefinition, ToolRegistry, ToolVersion, bump_version
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
    ToolVault,
    ToolEntry,
    ToolContextTier,
    ToolSearchResult,
    ToolStore,
    AgentToolContext,
    ApprovalManager,
    ApprovalLevel,
    ApprovalDecision,
    ApprovalRecord,
)
from youmi.coordinator.master import MasterAgent
from youmi.coordinator.tool_guardian import ToolGuardianAgent, ToolModification
from youmi.coordinator.plan import (
    WorkflowPlan, WorkflowStep, WorkflowExecutor, StepResult, StepStatus,
)
from youmi.coordinator.handoff import HandoffProtocol
from youmi.scheduler import HeartbeatScheduler, ScheduledTask
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
from youmi.core.hooks import (
    HookRegistry, HookType, HookContext, HookDecision, HookDecisionType,
)
from youmi.core.plugin import Plugin, PluginManager
from youmi.core.prompt import PromptAssembler, PromptLayer
from youmi.llm.embeddings import EmbeddingClient, EmbeddingError

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
    # P1: WorkflowPlan + Executor
    "WorkflowPlan",
    "WorkflowStep",
    "WorkflowExecutor",
    "StepResult",
    "StepStatus",
    # P1: Handoff
    "HandoffProtocol",
    # P1: Scheduler
    "HeartbeatScheduler",
    "ScheduledTask",
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
    # P2: Hook / Plugin
    "HookRegistry",
    "HookType",
    "HookContext",
    "HookDecision",
    "HookDecisionType",
    "Plugin",
    "PluginManager",
    # P2: Prompt
    "PromptAssembler",
    "PromptLayer",
    # ToolVault (工具发现与向量搜索)
    "ToolVault",
    "ToolEntry",
    "ToolContextTier",
    "ToolSearchResult",
    "EmbeddingClient",
    "EmbeddingError",
    # Phase 4: 工具生命周期
    "ToolStore",
    "AgentToolContext",
    "ApprovalManager",
    "ApprovalLevel",
    "ApprovalDecision",
    "ApprovalRecord",
    "ToolVersion",
    "bump_version",
]

__version__ = "0.1.0"
