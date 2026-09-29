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
from youmi.knowledge import (
    GlobalMemory,
    KnowledgeCategory,
    KnowledgeEntry,
    ToolKnowledge,
    ToolExperienceExtractor,
)
from youmi.core.resilience import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerRegistry,
    CircuitOpenError,
    retry_async,
)
from youmi.observability import (
    AuditEvent,
    AuditLogger,
    get_audit_logger,
    get_tracer,
    record_span_error,
    set_span_attributes,
    setup_tracing,
    shutdown_tracing,
    span,
)
from youmi.security import (
    AuthManager,
    AuthRole,
    AuthToken,
    Principal,
    Sandbox,
    SandboxPolicy,
    SandboxViolation,
    configure_auth_manager,
    configure_sandbox,
    get_auth_manager,
    get_sandbox,
)
from youmi.gateway import (
    TaskStatus,
    TaskRecord,
    new_task_id,
    TaskQueue,
    InMemoryTaskQueue,
    TaskRegistry,
    TaskEventHub,
    task_update_event,
    TaskExecutor,
    TaskOutcome,
    MasterTaskExecutor,
    WorkerPool,
    GatewayService,
)
from youmi.eval import (
    EvalDataset,
    EvalStep,
    EvalTask,
    builtin_dataset,
    EvalRunner,
    TaskRun,
    TaskScore,
    EvalSummary,
    score_task,
    summarize,
)

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
    # Phase 6: 全局记忆
    "GlobalMemory",
    "KnowledgeCategory",
    "KnowledgeEntry",
    "ToolKnowledge",
    "ToolExperienceExtractor",
    # M1: 可靠性（重试退避 + 熔断器）
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerRegistry",
    "CircuitOpenError",
    "retry_async",
    # M1: 可观测性（审计 + OTel 追踪）
    "AuditEvent",
    "AuditLogger",
    "get_audit_logger",
    "get_tracer",
    "record_span_error",
    "set_span_attributes",
    "setup_tracing",
    "shutdown_tracing",
    "span",
    # M1: 安全（认证 + 沙箱）
    "AuthManager",
    "AuthRole",
    "AuthToken",
    "Principal",
    "Sandbox",
    "SandboxPolicy",
    "SandboxViolation",
    "configure_auth_manager",
    "configure_sandbox",
    "get_auth_manager",
    "get_sandbox",
    # P1: 任务网关（提交 / 查询 / SSE 事件流 / Worker 池）
    "TaskStatus",
    "TaskRecord",
    "new_task_id",
    "TaskQueue",
    "InMemoryTaskQueue",
    "TaskRegistry",
    "TaskEventHub",
    "task_update_event",
    "TaskExecutor",
    "TaskOutcome",
    "MasterTaskExecutor",
    "WorkerPool",
    "GatewayService",
    # P1: 质量保障（Eval 基准）
    "EvalDataset",
    "EvalStep",
    "EvalTask",
    "builtin_dataset",
    "EvalRunner",
    "TaskRun",
    "TaskScore",
    "EvalSummary",
    "score_task",
    "summarize",
]

__version__ = "0.1.0"
