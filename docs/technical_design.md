# YouMi Agent — 多Agent协作框架 技术方案

> 版本: v0.1.0
> 创建日期: 2026-08-04
> 状态: 草案

---

## 1. 设计原则

| 原则 | 说明 |
|------|------|
| **插件化优先** | 所有核心组件（Agent、Skill、Tool、Memory Backend）均为可替换插件，通过抽象接口解耦 |
| **声明式配置** | Agent角色、Skill、Tool均通过YAML声明，零代码扩展 |
| **异步驱动** | 全栈asyncio，Agent执行、工具调用、消息传递均为非阻塞 |
| **协议标准** | 对外遵循MCP标准协议，对内定义清晰的内部协议 |
| **最小权限** | Agent仅能访问被授权的工具和记忆，默认拒绝一切 |
| **可观测** | 结构化日志 + OpenTelemetry Trace贯穿全链路 |

---

## 2. 整体架构

```
                         ┌─────────────────────┐
                         │     用户接口层        │
                         │  CLI / REST API      │
                         └────────┬────────────┘
                                  │ SubmitTask(task_description)
                                  ▼
              ┌───────────────────────────────────────────┐
              │           Orchestrator 编排层              │
              │                                           │
              │  ┌──────────┐ ┌──────────┐ ┌───────────┐ │
              │  │ Analyzer │ │AgentFac. │ │Coordinator│ │
              │  │ 任务分析  │ │Agent工厂  │ │ 协作调度   │ │
              │  └────┬─────┘ └────┬─────┘ └─────┬─────┘ │
              │       │            │              │       │
              │       ▼            ▼              ▼       │
              │  ┌─────────────────────────────────────┐  │
              │  │          ExecutionPlan (DAG)         │  │
              │  └─────────────────────────────────────┘  │
              └───────────────────┬───────────────────────┘
                                  │ 创建 & 调度 Agent
                                  ▼
              ┌───────────────────────────────────────────┐
              │            Agent Runtime 运行时            │
              │                                           │
              │  ┌─────────────┐ ┌─────────────┐         │
              │  │  Agent 实例  │ │  Agent 实例  │  ...    │
              │  │             │ │             │          │
              │  │ ┌─────────┐│ │ ┌─────────┐│          │
              │  │ │ LLM     ││ │ │ LLM     ││          │
              │  │ │ Client  ││ │ │ Client  ││          │
              │  │ ├─────────┤│ │ ├─────────┤│          │
              │  │ │ Skill   ││ │ │ Skill   ││          │
              │  │ │ Loader  ││ │ │ Loader  ││          │
              │  │ ├─────────┤│ │ ├─────────┤│          │
              │  │ │ Tool    ││ │ │ Tool    ││          │
              │  │ │ Bridge  ││ │ │ Bridge  ││          │
              │  │ ├─────────┤│ │ ├─────────┤│          │
              │  │ │ Memory  ││ │ │ Memory  ││          │
              │  │ │ Adapter ││ │ │ Adapter ││          │
              │  │ └─────────┘│ │ └─────────┘│          │
              │  └─────────────┘ └─────────────┘         │
              └─────┬──────────┬──────────┬───────────────┘
                    │          │          │
          ┌─────────▼──┐  ┌───▼────┐ ┌───▼──────────┐
          │ MCP Client │  │ Memory │ │ Skill/Tool   │
          │ (per agent)│  │ Store  │ │ Registry     │
          └──────┬─────┘  └───┬────┘ └───┬──────────┘
                 │            │          │
                 ▼            ▼          ▼
          ┌────────────┐ ┌────────┐ ┌────────────┐
          │ MCP Server │ │ SQLite │ │ YAML/TOML  │
          │ (统一网关)  │ │ +ChromaDB│ │ 配置目录   │
          └─────┬──────┘ └────────┘ └────────────┘
                │
        ┌───────┼───────────┐
        ▼       ▼           ▼
    ┌──────┐ ┌──────┐ ┌──────────┐
    │Tool A│ │Tool B│ │ Tool ... │  ← MCP Tool Providers
    │(插件) │ │(插件) │ │  (插件)   │
    └──────┘ └──────┘ └──────────┘
```

### 2.1 分层职责

| 层 | 职责 | 核心组件 |
|----|------|---------|
| **用户接口层** | 接收任务、展示结果、管理配置 | CLI, REST API |
| **编排层** | 任务分析、Agent创建、协作调度 | Analyzer, AgentFactory, Coordinator |
| **Agent运行时** | Agent生命周期管理、LLM推理循环、Skill执行 | Agent, LLMClient, SkillLoader, ToolBridge, MemoryAdapter |
| **基础设施层** | 工具服务、记忆存储、配置注册 | MCP Server, MemoryStore, Registry |

---

## 3. 核心模块详细设计

### 3.1 Agent 模块

Agent是框架的核心执行实体，采用 **ReAct (Reasoning + Acting)** 循环驱动。

```
Agent 生命周期:

  [创建] → [初始化] → [装载Skill/Tool] → [运行(ReAct循环)] → [完成/失败] → [销毁]
                                              │
                          ┌───────────────────┘
                          ▼
                    ┌───────────┐
                    │  Observe  │ ← 接收消息、读取记忆
                    ├───────────┤
                    │  Think    │ ← LLM推理，决定下一步行动
                    ├───────────┤
                    │  Act      │ ← 调用Skill/Tool，写入记忆
                    ├───────────┤
                    │  Reflect  │ ← 评估结果，决定是否继续
                    └─────┬─────┘
                          │
                          ▼ (未达成目标则循环)
```

#### 3.1.1 Agent 核心数据结构

```python
# youmi/core/agent.py

@dataclass
class AgentConfig:
    """Agent配置，从角色模板实例化"""
    agent_id: str                          # UUID，全局唯一
    name: str                              # Agent名称
    role: str                              # 角色标识 (coder, reviewer, researcher...)
    system_prompt: str                     # 系统提示词
    llm_config: LLMConfig                  # LLM配置 (model, temperature, max_tokens...)
    allowed_skills: list[str]              # 授权的Skill列表
    allowed_tools: list[str]               # 授权的Tool列表
    memory_config: MemoryConfig            # 记忆配置
    max_iterations: int = 20               # ReAct最大迭代次数
    retry_policy: RetryPolicy = field(...) # 重试策略

class AgentStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING = "waiting"      # 等待其他Agent或外部资源
    COMPLETED = "completed"
    FAILED = "failed"
    DESTROYED = "destroyed"

class Agent:
    """Agent核心类"""
    def __init__(self, config: AgentConfig): ...
    
    async def run(self, task: Task, context: ExecutionContext) -> TaskResult: ...
    async def receive_message(self, msg: AgentMessage) -> None: ...
    async def delegate(self, subtask: Task, target_agent_id: str) -> TaskResult: ...
    
    # 内部ReAct循环
    async def _observe(self, context: ExecutionContext) -> Observation: ...
    async def _think(self, observation: Observation) -> Plan: ...
    async def _act(self, plan: Plan) -> ActionResult: ...
    async def _reflect(self, result: ActionResult) -> Reflection: ...
```

#### 3.1.2 角色模板 (YAML声明式)

```yaml
# configs/roles/coder.yaml
role:
  name: coder
  description: "负责代码编写、修改与重构"
  system_prompt: |
    你是一名资深软件工程师。你的任务是根据需求编写高质量代码。
    遵循项目现有的代码风格与规范。在修改代码前先理解上下文。
  llm:
    model: gpt-4o
    temperature: 0.3
    max_tokens: 8192
  skills:
    - code_generation
    - code_review
    - refactoring
  tools:
    - file_read
    - file_write
    - code_execute
    - search_code
  memory:
    short_term:
      backend: sqlite
      max_messages: 50
    long_term:
      backend: chromadb
      enabled: true
  max_iterations: 30
  retry:
    max_retries: 3
    backoff: exponential
```

### 3.2 Skill 模块

Skill是Agent的**能力单元**，描述如何完成特定子任务。Skill可以组合多个Tool调用，形成完整的工作流。

#### 3.2.1 Skill 定义模型

```python
# youmi/core/skill.py

@dataclass
class SkillDefinition:
    """Skill声明式定义"""
    skill_id: str                    # 唯一标识
    name: str                        # 显示名称
    description: str                 # 描述（供LLM理解何时使用）
    version: str                     # 语义化版本
    parameters: JSONSchema           # 输入参数Schema
    returns: JSONSchema              # 输出Schema
    required_tools: list[str]        # 依赖的Tool列表
    steps: list[SkillStep]           # 执行步骤序列
    examples: list[SkillExample]     # 使用示例（few-shot）
    tags: list[str]                  # 标签，用于搜索匹配

@dataclass
class SkillStep:
    """Skill内的单个执行步骤"""
    step_id: str
    description: str
    tool: str | None                 # 调用的Tool名称，None表示纯推理步骤
    tool_params: dict | None         # Tool参数模板，支持变量替换
    prompt_template: str | None      # 步骤提示词模板
    output_key: str                  # 步骤输出在上下文中的键名
    condition: str | None            # 条件表达式，决定是否执行此步骤
```

#### 3.2.2 Skill 声明示例

```yaml
# configs/skills/code_generation.yaml
skill:
  id: code_generation
  name: "代码生成"
  description: "根据需求描述生成代码，包括理解上下文、编写代码、验证结果"
  version: "1.0.0"
  tags: [code, generation, development]
  parameters:
    type: object
    properties:
      requirement:
        type: string
        description: "代码需求描述"
      language:
        type: string
        description: "目标编程语言"
      context_files:
        type: array
        items: { type: string }
        description: "需要参考的上下文文件路径"
    required: [requirement, language]
  returns:
    type: object
    properties:
      code: { type: string }
      explanation: { type: string }
      files_modified: { type: array, items: { type: string } }
  required_tools:
    - file_read
    - file_write
    - search_code
  steps:
    - step_id: understand_context
      description: "阅读相关上下文文件，理解项目结构"
      tool: file_read
      tool_params: { path: "{{context_files}}" }
      output_key: context
    - step_id: search_related
      description: "搜索项目中相关代码，了解现有实现"
      tool: search_code
      tool_params: { query: "{{requirement}}" }
      output_key: related_code
    - step_id: generate
      description: "基于上下文和需求，生成代码"
      prompt_template: |
        基于以下上下文生成{{language}}代码：
        需求：{{requirement}}
        上下文：{{context}}
        相关代码：{{related_code}}
      output_key: generated_code
    - step_id: write_file
      description: "将生成的代码写入文件"
      tool: file_write
      tool_params: { content: "{{generated_code}}" }
      output_key: write_result
```

#### 3.2.3 Skill Registry

```python
# youmi/registry/skill_registry.py

class SkillRegistry:
    """全局Skill注册中心"""
    
    async def register(self, definition: SkillDefinition) -> None: ...
    async def unregister(self, skill_id: str) -> None: ...
    async def get(self, skill_id: str) -> SkillDefinition | None: ...
    async def search(self, query: str, tags: list[str] | None = None) -> list[SkillDefinition]: ...
    async def check_compatibility(self, skill_id: str, agent_config: AgentConfig) -> bool: ...
    async def resolve_dependencies(self, skill_id: str) -> list[str]: ...
    
    # 从配置目录批量加载
    async def load_from_directory(self, path: Path) -> None: ...
```

### 3.3 Tool 模块

Tool是通过MCP协议暴露的**原子能力**。Agent不直接持有Tool实现，而是通过MCP Client远程调用。

#### 3.3.1 Tool 定义与注册

```python
# youmi/core/tool.py

@dataclass
class ToolDefinition:
    """Tool声明式定义"""
    tool_id: str                     # 唯一标识
    name: str                        # Tool名称
    description: str                 # 描述
    version: str                     # 语义化版本
    mcp_endpoint: str                # MCP Server上的工具标识
    input_schema: JSONSchema         # 输入参数Schema
    output_schema: JSONSchema        # 输出Schema
    auth_required: bool = False      # 是否需要认证
    timeout_ms: int = 30000          # 超时时间
    retry_policy: RetryPolicy = field(...)
    tags: list[str] = field(...)
```

```yaml
# configs/tools/file_read.yaml
tool:
  id: file_read
  name: "文件读取"
  description: "读取指定路径文件的内容"
  version: "1.0.0"
  mcp_endpoint: "fs_read"           # MCP Server中注册的工具名
  input_schema:
    type: object
    properties:
      path:
        type: string
        description: "文件路径"
      encoding:
        type: string
        default: "utf-8"
    required: [path]
  output_schema:
    type: object
    properties:
      content: { type: string }
      line_count: { type: integer }
  timeout_ms: 10000
  tags: [filesystem, read]
```

#### 3.3.2 Tool Bridge (Agent侧)

```python
# youmi/core/tool_bridge.py

class ToolBridge:
    """Agent与MCP Server之间的桥梁"""
    
    def __init__(self, agent_id: str, allowed_tools: list[str], mcp_client: MCPClient): ...
    
    async def call_tool(self, tool_name: str, params: dict) -> ToolResult:
        """
        调用工具：
        1. 检查tool_name是否在allowed_tools中 → 权限校验
        2. 从ToolRegistry获取ToolDefinition → Schema校验
        3. 通过MCPClient发送请求 → 调用
        4. 记录调用日志与Trace → 可观测性
        5. 返回结果或异常
        """
        ...
    
    async def list_available_tools(self) -> list[ToolDefinition]: ...
    async def get_tool_schema(self, tool_name: str) -> JSONSchema: ...
```

### 3.4 MCP Server 模块

统一MCP服务器，作为所有工具调用的网关。采用**插件化架构**，每个Tool Provider独立注册。

#### 3.4.1 MCP Server 架构

```
┌──────────────────────────────────────────────────┐
│                  MCP Server                       │
│                                                   │
│  ┌─────────────┐  ┌────────────┐  ┌───────────┐ │
│  │ MCP Protocol │  │  Auth      │  │  Logging   │ │
│  │ Handler      │  │  Middleware│  │  Middleware│ │
│  └──────┬──────┘  └─────┬──────┘  └─────┬─────┘ │
│         │               │               │        │
│         ▼               ▼               ▼        │
│  ┌─────────────────────────────────────────────┐ │
│  │              Tool Router                     │ │
│  │    (路由: tool_name → ToolProvider)           │ │
│  └──────────────────────┬──────────────────────┘ │
│                         │                         │
│         ┌───────────────┼───────────────┐        │
│         ▼               ▼               ▼        │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐   │
│  │ FileSystem│    │  Code    │    │  Search  │   │
│  │ Provider  │    │ Provider │    │ Provider │   │
│  └──────────┘    └──────────┘    └──────────┘   │
│                                                   │
└──────────────────────────────────────────────────┘
```

#### 3.4.2 MCP Server 核心实现

```python
# youmi/mcp/server.py

class MCPServer:
    """统一MCP服务器"""
    
    def __init__(self, config: MCPServerConfig): ...
    
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    
    # Tool Provider注册
    async def register_provider(self, provider: ToolProvider) -> None: ...
    async def unregister_provider(self, provider_id: str) -> None: ...
    
    # MCP协议处理 (内部)
    async def _handle_list_tools(self, request: MCPRequest) -> MCPResponse: ...
    async def _handle_call_tool(self, request: MCPRequest) -> MCPResponse: ...
    async def _handle_tool_describe(self, request: MCPRequest) -> MCPResponse: ...

class ToolProvider(ABC):
    """Tool Provider抽象基类 — 插件化扩展点"""
    
    @abstractmethod
    def provider_id(self) -> str: ...
    
    @abstractmethod
    async def get_tools(self) -> list[ToolDefinition]: ...
    
    @abstractmethod
    async def execute(self, tool_name: str, params: dict, context: ToolContext) -> ToolResult: ...
```

#### 3.4.3 Tool Provider 示例

```python
# youmi/mcp/providers/filesystem.py

class FileSystemProvider(ToolProvider):
    """文件系统工具Provider"""
    
    def provider_id(self) -> str:
        return "filesystem"
    
    async def get_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(tool_id="fs_read", name="文件读取", ...),
            ToolDefinition(tool_id="fs_write", name="文件写入", ...),
            ToolDefinition(tool_id="fs_list_dir", name="目录列表", ...),
            ToolDefinition(tool_id="fs_search", name="文件搜索", ...),
        ]
    
    async def execute(self, tool_name: str, params: dict, context: ToolContext) -> ToolResult:
        match tool_name:
            case "fs_read":
                return await self._read_file(params, context)
            case "fs_write":
                return await self._write_file(params, context)
            # ...
```

#### 3.4.4 MCP 通信协议

采用MCP标准协议，传输层使用 **stdio + JSON-RPC 2.0**（进程内通信）或 **SSE (Server-Sent Events)**（HTTP远程通信）。

```
Agent (MCP Client)                    MCP Server
     │                                    │
     │── tools/list ──────────────────────▶│
     │◀── [ToolDefinition...] ────────────│
     │                                    │
     │── tools/call {name, arguments} ───▶│
     │                                    │── Provider.execute()
     │◀── {result | error} ───────────────│
     │                                    │
```

```json
// 工具调用请求 (JSON-RPC 2.0)
{
  "jsonrpc": "2.0",
  "id": "call-001",
  "method": "tools/call",
  "params": {
    "name": "fs_read",
    "arguments": {
      "path": "/src/main.py",
      "encoding": "utf-8"
    },
    "_meta": {
      "agent_id": "agent-abc-123",
      "task_id": "task-001",
      "trace_id": "trace-xyz"
    }
  }
}

// 工具调用响应
{
  "jsonrpc": "2.0",
  "id": "call-001",
  "result": {
    "content": [
      { "type": "text", "text": "# file content here..." }
    ],
    "isError": false
  }
}
```

### 3.5 Memory 模块

每个Agent拥有独立记忆空间，支持短期记忆（对话上下文）和长期记忆（向量检索）。

#### 3.5.1 Memory 架构

```
┌─────────────────────────────────────────────┐
│              Agent Memory                    │
│                                              │
│  ┌───────────────────────────────────────┐  │
│  │         Memory Adapter (API层)        │  │
│  │  write() read() search() archive()    │  │
│  └────────┬──────────────┬───────────────┘  │
│           │              │                   │
│           ▼              ▼                   │
│  ┌──────────────┐ ┌──────────────────┐      │
│  │ ShortTerm    │ │  LongTerm        │      │
│  │ Memory       │ │  Memory          │      │
│  │              │ │                  │      │
│  │ 对话上下文    │ │  知识积累         │      │
│  │ 中间推理结果  │ │  经验总结         │      │
│  │ 当前任务状态  │ │  用户偏好         │      │
│  │              │ │                  │      │
│  │ SQLite后端   │ │  ChromaDB后端     │      │
│  └──────────────┘ └──────────────────┘      │
│                                              │
│  ┌───────────────────────────────────────┐  │
│  │         Shared Memory (可选)           │  │
│  │  Agent间共享的协作上下文                │  │
│  └───────────────────────────────────────┘  │
└─────────────────────────────────────────────┘
```

#### 3.5.2 Memory 核心接口

```python
# youmi/memory/adapter.py

@dataclass
class MemoryConfig:
    short_term: ShortTermConfig
    long_term: LongTermConfig
    shared: SharedConfig | None = None

@dataclass
class MemoryEntry:
    entry_id: str
    agent_id: str
    memory_type: Literal["short_term", "long_term", "shared"]
    content: str
    metadata: dict                        # 自定义元数据
    embedding: list[float] | None         # 向量（长期记忆）
    created_at: datetime
    updated_at: datetime
    ttl: int | None                       # 过期时间(秒)，None=永不过期

class MemoryAdapter:
    """Agent记忆适配器 — 每个Agent实例一个"""
    
    def __init__(self, agent_id: str, config: MemoryConfig): ...
    
    # 短期记忆操作
    async def add_message(self, role: str, content: str, metadata: dict | None = None) -> str: ...
    async def get_conversation(self, limit: int = 50) -> list[MemoryEntry]: ...
    async def clear_conversation(self) -> None: ...
    
    # 长期记忆操作
    async def store_knowledge(self, content: str, metadata: dict | None = None) -> str: ...
    async def search_knowledge(self, query: str, top_k: int = 5) -> list[MemoryEntry]: ...
    async def update_knowledge(self, entry_id: str, content: str) -> None: ...
    async def delete_knowledge(self, entry_id: str) -> None: ...
    
    # 共享记忆操作（多Agent协作时使用）
    async def write_shared(self, key: str, content: str, visibility: list[str]) -> None: ...
    async def read_shared(self, key: str) -> MemoryEntry | None: ...
    
    # 归档
    async def archive_session(self, task_id: str) -> None:
        """将当前短期记忆中的有价值信息归档到长期记忆"""
        ...
```

#### 3.5.3 Memory Backend 接口（可插拔）

```python
# youmi/memory/backends/base.py

class ShortTermBackend(ABC):
    """短期记忆后端抽象"""
    @abstractmethod
    async def put(self, entry: MemoryEntry) -> None: ...
    @abstractmethod
    async def get_latest(self, agent_id: str, limit: int) -> list[MemoryEntry]: ...
    @abstractmethod
    async def clear(self, agent_id: str) -> None: ...

class LongTermBackend(ABC):
    """长期记忆后端抽象（支持向量检索）"""
    @abstractmethod
    async def store(self, entry: MemoryEntry) -> None: ...
    @abstractmethod
    async def search(self, agent_id: str, query: str, query_embedding: list[float], top_k: int) -> list[MemoryEntry]: ...
    @abstractmethod
    async def update(self, entry_id: str, entry: MemoryEntry) -> None: ...
    @abstractmethod
    async def delete(self, entry_id: str) -> None: ...

# 内置实现
class SQLiteShortTermBackend(ShortTermBackend): ...
class ChromaDBLongTermBackend(LongTermBackend): ...
```

### 3.6 Orchestrator 编排层

#### 3.6.1 任务分析器 (Analyzer)

```python
# youmi/orchestrator/analyzer.py

@dataclass
class TaskAnalysisResult:
    """任务分析结果"""
    task_id: str
    original_description: str
    subtasks: list[Subtask]              # 子任务列表
    execution_dag: ExecutionDAG          # 执行DAG
    required_agents: list[AgentRequirement]  # 所需Agent清单
    shared_context: dict                 # 全局共享上下文

@dataclass
class AgentRequirement:
    """分析得出的Agent需求"""
    role: str                            # 推荐角色
    reason: str                          # 为什么需要这个角色
    skills_needed: list[str]
    tools_needed: list[str]

@dataclass
class ExecutionDAG:
    """执行有向无环图"""
    nodes: list[DAGNode]                 # 节点 = 子任务
    edges: list[DAGEdge]                 # 边 = 依赖关系

@dataclass
class DAGNode:
    node_id: str
    subtask: Subtask
    agent_requirement: AgentRequirement
    parallel_group: int                  # 并行组编号

@dataclass
class DAGEdge:
    from_node: str                       # 依赖的节点
    to_node: str                         # 被依赖的节点
    condition: str | None                # 条件边（条件分支）
    data_mapping: dict | None            # 数据传递映射

class Analyzer:
    """任务分析器 — 利用LLM进行任务拆解"""
    
    def __init__(self, llm_client: LLMClient, skill_registry: SkillRegistry, role_registry: RoleRegistry): ...
    
    async def analyze(self, task_description: str) -> TaskAnalysisResult:
        """
        分析流程：
        1. 调用LLM，给出任务描述 + 可用角色/Skill列表 → LLM输出拆解方案
        2. 解析LLM输出为结构化的TaskAnalysisResult
        3. 构建ExecutionDAG（拓扑排序验证无环）
        4. 校验所需Skill/Tool是否都在Registry中可用
        5. 返回分析结果
        """
        ...
```

#### 3.6.2 Agent 工厂 (AgentFactory)

```python
# youmi/orchestrator/factory.py

class AgentFactory:
    """Agent工厂 — 根据分析结果创建Agent实例"""
    
    def __init__(self, role_registry: RoleRegistry, skill_registry: SkillRegistry,
                 tool_registry: ToolRegistry, mcp_client: MCPClient): ...
    
    async def create_agent(self, requirement: AgentRequirement, task_id: str) -> Agent:
        """
        创建流程：
        1. 从RoleRegistry加载角色模板
        2. 根据requirement覆盖配置（skills、tools等）
        3. 创建AgentConfig
        4. 实例化Agent
        5. 通过ToolBridge绑定授权工具
        6. 通过SkillLoader装载授权技能
        7. 初始化MemoryAdapter
        8. 返回就绪的Agent实例
        """
        ...
    
    async def create_agents(self, requirements: list[AgentRequirement], task_id: str) -> list[Agent]: ...
    async def destroy_agent(self, agent: Agent) -> None: ...
```

#### 3.6.3 协作调度器 (Coordinator)

```python
# youmi/orchestrator/coordinator.py

class Coordinator:
    """协作调度器 — 按DAG调度Agent执行"""
    
    def __init__(self, factory: AgentFactory): ...
    
    async def execute(self, plan: TaskAnalysisResult) -> TaskResult:
        """
        执行流程：
        1. 根据DAG拓扑排序确定执行顺序
        2. 按并行组分批创建Agent
        3. 同组内Agent并行执行，组间顺序执行
        4. 节点完成后，通过data_mapping传递数据到下游节点
        5. 处理条件分支：根据上游结果决定是否执行下游
        6. 异常处理：节点失败 → 重试 → 降级 → 终止
        7. 所有节点完成 → 汇总结果
        """
        ...
    
    async def _execute_parallel_group(self, group: list[DAGNode], context: ExecutionContext) -> dict: ...
    async def _handle_node_failure(self, node: DAGNode, error: Exception, context: ExecutionContext) -> FailureAction: ...
    async def _pass_data(self, from_node: DAGNode, to_node: DAGNode, result: TaskResult) -> dict: ...
```

---

## 4. LLM 客户端模块

```python
# youmi/llm/client.py

@dataclass
class LLMConfig:
    provider: str                        # "openai" | "anthropic" | "local"
    model: str                           # "gpt-4o" | "claude-sonnet-4-20250514" | ...
    api_key: str                         # API密钥（从环境变量读取）
    base_url: str | None = None          # 自定义API地址
    temperature: float = 0.7
    max_tokens: int = 4096
    timeout_s: int = 120

class LLMClient:
    """统一LLM客户端 — 支持多Provider"""
    
    async def chat(self, messages: list[Message], config: LLMConfig,
                   tools: list[ToolDefinition] | None = None) -> LLMResponse:
        """
        发送聊天请求：
        1. 将内部Message格式转为Provider特定格式
        2. 如果有tools参数，构造function calling schema
        3. 调用API
        4. 解析响应（含tool_calls解析）
        5. 返回统一的LLMResponse
        """
        ...
    
    async def chat_stream(self, messages: list[Message], config: LLMConfig,
                          tools: list[ToolDefinition] | None = None) -> AsyncIterator[LLMChunk]: ...
```

```python
# LLM响应结构
@dataclass
class LLMResponse:
    content: str | None                  # 文本响应
    tool_calls: list[ToolCall] | None    # 工具调用请求
    usage: TokenUsage                    # Token使用统计
    finish_reason: str                   # "stop" | "tool_calls" | "length"

@dataclass
class ToolCall:
    call_id: str
    tool_name: str
    arguments: dict
```

---

## 5. 关键数据模型汇总

```
┌──────────────┐     ┌──────────────┐     ┌─────────────────┐
│     Task     │     │  Subtask     │     │  TaskResult     │
├──────────────┤     ├──────────────┤     ├─────────────────┤
│ task_id      │     │ subtask_id   │     │ task_id         │
│ description  │1───*│ description  │     │ status          │
│ status       │     │ agent_role   │     │ output          │
│ created_at   │     │ skills[]     │     │ agent_results[] │
│ context      │     │ tools[]      │     │ execution_time  │
│ priority     │     │ depends_on[] │     │ token_usage     │
└──────────────┘     └──────────────┘     │ trace_log       │
                                          └─────────────────┘

┌──────────────┐     ┌──────────────────┐     ┌──────────────┐
│ AgentMessage │     │ ExecutionContext  │     │  TraceLog    │
├──────────────┤     ├──────────────────┤     ├──────────────┤
│ msg_id       │     │ task_id          │     │ trace_id     │
│ from_agent   │     │ shared_data      │     │ agent_id     │
│ to_agent     │     │ agent_results    │     │ span_type    │
│ msg_type     │     │ active_agents    │     │ operation    │
│ content      │     │ memory_bus       │     │ input        │
│ metadata     │     │ event_log        │     │ output       │
│ timestamp    │     └──────────────────┘     │ duration_ms  │
└──────────────┘                              │ parent_id    │
                                              └──────────────┘
```

---

## 6. 关键流程

### 6.1 端到端任务执行流程

```
用户: "帮我写一个Python Web API，包含用户认证和CRUD操作"
  │
  ▼
[Orchestrator] Analyzer.analyze(task_description)
  │  LLM调用 → 分析任务需求
  │  输出:
  │    - Agent A: architect (设计API结构)
  │    - Agent B: coder (编写代码)  
  │    - Agent C: reviewer (代码审查)
  │    - DAG: A → B → C (顺序执行)
  │
  ▼
[Orchestrator] AgentFactory.create_agents([A, B, C])
  │  为每个Agent:
  │    ├─ 加载角色模板 (YAML)
  │    ├─ 装载Skills (从Registry)
  │    ├─ 绑定Tools (通过ToolBridge → MCP)
  │    └─ 初始化Memory (隔离空间)
  │
  ▼
[Coordinator] 按DAG顺序执行
  │
  ├─ Step 1: Agent A (architect) 执行
  │   │  ReAct循环:
  │   │  Observe → 读取任务描述
  │   │  Think   → LLM推理，设计API结构
  │   │  Act     → 调用 skill:api_design → tool:file_write (via MCP)
  │   │  Reflect → 检查产出完整性 → 完成
  │   │  Memory  → 将设计文档存入短期记忆
  │   │
  │   ▼ 输出: api_design.yaml
  │
  ├─ Step 2: Agent B (coder) 执行
  │   │  接收: Agent A的设计文档 (通过data_mapping)
  │   │  ReAct循环:
  │   │  Observe → 读取设计文档 + 从Memory搜索项目规范
  │   │  Think   → LLM推理，规划代码实现
  │   │  Act     → 调用 skill:code_generation
  │   │            → tool:file_read, tool:file_write, tool:search_code (via MCP)
  │   │  Reflect → 代码语法检查 → 迭代修复
  │   │  Memory  → 存储编码经验到长期记忆
  │   │
  │   ▼ 输出: 源代码文件
  │
  ├─ Step 3: Agent C (reviewer) 执行
  │   │  接收: Agent B的代码 (通过data_mapping)
  │   │  ReAct循环:
  │   │  Observe → 读取代码 + 设计文档
  │   │  Think   → LLM推理，逐项审查
  │   │  Act     → 调用 skill:code_review → tool:code_execute (via MCP)
  │   │  Reflect → 汇总审查结果
  │   │
  │   ▼ 输出: review_report.md
  │
  ▼
[Coordinator] 汇总结果 → TaskResult
  │  合并所有Agent输出
  │  记录执行时间线、Token消耗
  │
  ▼
返回给用户: 项目代码 + API设计文档 + 审查报告
```

### 6.2 Agent Tool调用流程 (via MCP)

```
Agent._act()
  │  LLM决定调用 tool: file_write
  │
  ▼
ToolBridge.call_tool("file_write", {path: "...", content: "..."})
  │
  ├─ 1. 权限检查: "file_write" in allowed_tools? → ✓
  ├─ 2. Schema校验: 从ToolRegistry获取input_schema → 验证params → ✓
  ├─ 3. 构造MCP请求:
  │     {
  │       "method": "tools/call",
  │       "params": {"name": "fs_write", "arguments": {...}},
  │       "_meta": {"agent_id": "...", "trace_id": "..."}
  │     }
  │
  ▼
MCPClient.send(request)  ──────▶  MCP Server
  │                                  │
  │                              ToolRouter.route("fs_write")
  │                                  │
  │                              FileSystemProvider.execute("fs_write", params)
  │                                  │
  │                                  ▼
  │                              执行文件写入操作
  │                                  │
  │  ◀──── MCPResponse ─────────────┘
  │
  ├─ 4. 记录TraceLog: {tool, params, result, duration, trace_id}
  ├─ 5. 写入短期记忆: "调用了file_write，结果: success"
  │
  ▼
返回 ToolResult 给Agent._act()
```

### 6.3 动态Skill装载流程

```
AgentFactory.create_agent(requirement)
  │
  ▼
SkillLoader.load_skills(agent, required_skills=["code_generation", "code_review"])
  │
  ├─ for skill_id in required_skills:
  │   │
  │   ├─ SkillRegistry.get(skill_id)
  │   │   └─ 返回 SkillDefinition
  │   │
  │   ├─ 依赖解析: skill.required_tools → [file_read, file_write, ...]
  │   │   ├─ ToolRegistry.get(tool_id) → ToolDefinition
  │   │   ├─ 检查tool是否在agent.allowed_tools中
  │   │   └─ 添加到agent的可用工具列表
  │   │
  │   ├─ 版本兼容检查: skill.version vs agent.runtime_version
  │   │
  │   ├─ 构造Skill实例:
  │   │   ├─ 将steps序列绑定到实际ToolBridge调用
  │   │   └─ 注册到agent的skill_map
  │   │
  │   └─ 将skill描述注入agent的system_prompt (供LLM理解可用技能)
  │
  ▼
Agent.skill_map = {
    "code_generation": SkillInstance(definition=..., executor=...),
    "code_review": SkillInstance(definition=..., executor=...)
}
```

---

## 7. 项目目录结构

```
YouMi_Agent/
├── docs/                          # 文档
│   ├── requirements.md            # 需求文档
│   └── technical_design.md        # 技术方案 (本文档)
│
├── youmi/                         # 主包
│   ├── __init__.py
│   ├── app.py                     # 应用入口 & 启动逻辑
│   │
│   ├── core/                      # 核心模块
│   │   ├── __init__.py
│   │   ├── agent.py               # Agent类、AgentConfig、AgentStatus
│   │   ├── skill.py               # SkillDefinition、SkillStep、SkillInstance
│   │   ├── tool.py                # ToolDefinition
│   │   ├── tool_bridge.py         # ToolBridge (Agent↔MCP桥梁)
│   │   ├── skill_loader.py        # SkillLoader (动态装载Skill)
│   │   ├── task.py                # Task、Subtask、TaskResult
│   │   └── message.py             # AgentMessage、MessageBus
│   │
│   ├── llm/                       # LLM客户端
│   │   ├── __init__.py
│   │   ├── client.py              # LLMClient统一接口
│   │   ├── providers/             # LLM Provider实现
│   │   │   ├── __init__.py
│   │   │   ├── openai_provider.py
│   │   │   └── anthropic_provider.py
│   │   └── types.py               # LLMConfig、LLMResponse、ToolCall等
│   │
│   ├── memory/                    # 记忆系统
│   │   ├── __init__.py
│   │   ├── adapter.py             # MemoryAdapter (Agent记忆API)
│   │   ├── types.py               # MemoryEntry、MemoryConfig
│   │   └── backends/              # 可插拔后端
│   │       ├── __init__.py
│   │       ├── base.py            # ShortTermBackend、LongTermBackend抽象基类
│   │       ├── sqlite_backend.py  # SQLite短期记忆后端
│   │       └── chroma_backend.py  # ChromaDB长期记忆后端
│   │
│   ├── mcp/                       # MCP服务器
│   │   ├── __init__.py
│   │   ├── server.py              # MCPServer核心
│   │   ├── client.py              # MCPClient (Agent侧)
│   │   ├── protocol.py            # MCP协议类型定义
│   │   ├── router.py              # ToolRouter (路由分发)
│   │   └── providers/             # Tool Provider插件
│   │       ├── __init__.py
│   │       ├── base.py            # ToolProvider抽象基类
│   │       ├── filesystem.py      # 文件系统工具
│   │       ├── code_exec.py       # 代码执行工具
│   │       └── search.py          # 代码搜索工具
│   │
│   ├── orchestrator/              # 编排层
│   │   ├── __init__.py
│   │   ├── analyzer.py            # Analyzer (任务分析器)
│   │   ├── factory.py             # AgentFactory (Agent工厂)
│   │   ├── coordinator.py         # Coordinator (协作调度器)
│   │   └── dag.py                 # ExecutionDAG、DAGNode、DAGEdge
│   │
│   ├── registry/                  # 注册中心
│   │   ├── __init__.py
│   │   ├── skill_registry.py      # SkillRegistry
│   │   ├── tool_registry.py       # ToolRegistry
│   │   └── role_registry.py       # RoleRegistry (Agent角色模板)
│   │
│   └── utils/                     # 工具函数
│       ├── __init__.py
│       ├── config.py              # 配置加载 (YAML/TOML)
│       ├── logging.py             # 结构化日志
│       └── tracing.py             # OpenTelemetry追踪
│
├── configs/                       # 声明式配置
│   ├── roles/                     # Agent角色模板
│   │   ├── coder.yaml
│   │   ├── reviewer.yaml
│   │   ├── researcher.yaml
│   │   └── architect.yaml
│   ├── skills/                    # Skill定义
│   │   ├── code_generation.yaml
│   │   ├── code_review.yaml
│   │   ├── api_design.yaml
│   │   └── refactoring.yaml
│   ├── tools/                     # Tool定义
│   │   ├── file_read.yaml
│   │   ├── file_write.yaml
│   │   ├── code_execute.yaml
│   │   └── search_code.yaml
│   └── settings.yaml              # 全局设置
│
├── tests/                         # 测试
│   ├── __init__.py
│   ├── unit/
│   │   ├── test_agent.py
│   │   ├── test_skill_loader.py
│   │   ├── test_tool_bridge.py
│   │   ├── test_memory.py
│   │   └── test_mcp_server.py
│   ├── integration/
│   │   ├── test_single_agent.py
│   │   └── test_multi_agent.py
│   └── fixtures/                  # 测试用配置和数据
│
├── examples/                      # 示例
│   ├── single_agent_demo.py
│   └── multi_agent_demo.py
│
├── pyproject.toml                 # 项目配置 & 依赖
├── .env.example                   # 环境变量模板
└── README.md
```

---

## 8. 技术选型最终方案

| 组件 | 技术选型 | 版本要求 | 理由 |
|------|---------|---------|------|
| **语言** | Python | >=3.10 | match/case语法、类型提示完善、AI生态最佳 |
| **异步运行时** | asyncio + uvloop | - | 标准异步、高性能事件循环 |
| **LLM接口** | httpx + 自定义适配 | httpx>=0.24 | 异步原生、支持stream、不绑定特定SDK |
| **MCP协议** | mcp (官方Python SDK) | >=1.0 | 标准实现，减少自建协议成本 |
| **短期记忆** | aiosqlite | >=0.19 | 异步SQLite、零部署、足够轻量 |
| **长期记忆** | chromadb | >=0.4 | 内嵌向量库、支持多种Embedding、Python原生 |
| **Embedding模型** | sentence-transformers | >=2.2 | 本地运行、无需API调用、模型可替换 |
| **配置管理** | PyYAML + Pydantic | - | YAML人类可读 + Pydantic校验 |
| **日志** | structlog | >=23.0 | 结构化日志、JSON输出、与Trace集成 |
| **追踪** | opentelemetry-python | >=1.20 | 标准可观测性方案、支持多种Exporter |
| **测试** | pytest + pytest-asyncio | - | 异步测试标准方案 |
| **依赖管理** | uv / pip + pyproject.toml | - | 现代Python项目管理 |

---

## 9. 核心依赖关系图

```
                    ┌──────────┐
                    │  app.py  │ ← 入口，组装所有组件
                    └────┬─────┘
                         │
          ┌──────────────┼──────────────┐
          ▼              ▼              ▼
   ┌────────────┐ ┌───────────┐ ┌────────────┐
   │Orchestrator│ │MCP Server │ │  Registry   │
   └──────┬─────┘ └─────┬─────┘ └──────┬─────┘
          │              │              │
    ┌─────┼─────┐        │        ┌─────┼─────┐
    ▼     ▼     ▼        ▼        ▼     ▼     ▼
 Analyzer│Coordinator   MCP     Skill  Tool  Role
    │    │     │       Router   Reg.   Reg.  Reg.
    │    │     │        │
    │    │     ▼        ▼
    │    │  AgentFactory│
    │    │     │    ToolProvider(s)
    │    │     ▼
    │    │  ┌──────┐
    └────┴──▶Agent │
          └───┬────┘
              │
       ┌──────┼──────┬──────────┐
       ▼      ▼      ▼          ▼
    LLM    Skill   Tool      Memory
   Client  Loader  Bridge    Adapter
              │      │          │
              │      ▼          ▼
              │   MCPClient  Backends
              │      │       (SQLite/
              │      │       ChromaDB)
              ▼      ▼
          ┌────────────┐
          │ MCP Server │
          └────────────┘
```

---

## 10. 实施路线图

### Phase 1: 基础骨架 (M1)

**目标**: 项目可运行，单Agent能完成简单任务

| 任务 | 产出 | 优先级 |
|------|------|-------|
| 项目初始化：pyproject.toml、目录结构、CI | 可安装的空包 | P0 |
| 实现 `core/task.py`、`core/message.py` 数据模型 | 基础数据结构 | P0 |
| 实现 `llm/client.py` + OpenAI Provider | LLM调用能力 | P0 |
| 实现 `core/agent.py` ReAct循环骨架 | 可运行的Agent | P0 |
| 实现 `utils/config.py` YAML加载 | 配置读取 | P0 |
| 实现 `registry/` 三个Registry基础版 | 注册中心 | P0 |
| 编写 configs/ 中的基础配置文件 | 角色/Skill/Tool配置 | P1 |
| 单Agent端到端Demo | 验证M1成果 | P0 |

### Phase 2: MCP集成 (M2)

**目标**: Agent通过MCP协议调用工具

| 任务 | 产出 | 优先级 |
|------|------|-------|
| 实现 `mcp/protocol.py` 协议类型 | MCP协议定义 | P0 |
| 实现 `mcp/server.py` MCP Server核心 | 统一工具网关 | P0 |
| 实现 `mcp/router.py` ToolRouter | 工具路由 | P0 |
| 实现 `mcp/client.py` MCP Client | Agent侧MCP客户端 | P0 |
| 实现 `core/tool_bridge.py` | Agent↔MCP桥梁 | P0 |
| 实现 FileSystem Provider (读写文件) | 首个Tool Provider | P0 |
| 实现 CodeExec Provider (代码执行) | 第二个Tool Provider | P1 |
| MCP端到端测试 | 验证M2成果 | P0 |

### Phase 3: Skill & 动态装载 (M3)

**目标**: Skill可声明、可装载、可执行

| 任务 | 产出 | 优先级 |
|------|------|-------|
| 实现 `core/skill.py` Skill数据模型与执行引擎 | Skill运行时 | P0 |
| 实现 `core/skill_loader.py` 动态装载逻辑 | Skill装载器 | P0 |
| Skill YAML → SkillDefinition 解析 | 声明式Skill | P0 |
| 依赖解析与兼容性检查 | 装载校验 | P1 |
| 编写4-5个核心Skill配置 | Skill库 | P1 |
| Agent + Skill + Tool 集成测试 | 验证M3成果 | P0 |

### Phase 4: 记忆系统 (M4)

**目标**: Agent拥有独立记忆，支持检索

| 任务 | 产出 | 优先级 |
|------|------|-------|
| 实现 `memory/types.py` 记忆数据模型 | 基础定义 | P0 |
| 实现 `memory/backends/sqlite_backend.py` | 短期记忆 | P0 |
| 实现 `memory/backends/chroma_backend.py` | 长期记忆+向量检索 | P0 |
| 实现 `memory/adapter.py` MemoryAdapter | Agent记忆API | P0 |
| Agent记忆隔离验证 | 隔离测试 | P0 |
| 会话归档到长期记忆 | 记忆持久化 | P1 |
| 共享记忆实现 | Agent间知识共享 | P2 |

### Phase 5: 多Agent协作 (M5)

**目标**: 多Agent按DAG协作完成复杂任务

| 任务 | 产出 | 优先级 |
|------|------|-------|
| 实现 `orchestrator/dag.py` DAG数据结构 | 执行计划模型 | P0 |
| 实现 `orchestrator/analyzer.py` 任务分析器 | LLM驱动的任务拆解 | P0 |
| 实现 `orchestrator/factory.py` Agent工厂 | Agent自动创建 | P0 |
| 实现 `orchestrator/coordinator.py` 协作调度器 | DAG调度执行 | P0 |
| Agent间消息传递与数据流转 | 协作通信 | P0 |
| 异常处理、重试、降级机制 | 可靠性 | P1 |
| 多Agent端到端Demo | 验证M5成果 | P0 |

### Phase 6: 打磨与优化 (M6)

| 任务 | 优先级 |
|------|-------|
| 结构化日志 + OpenTelemetry追踪 | P1 |
| MCP Server鉴权与安全加固 | P1 |
| 记忆加密存储 | P2 |
| CLI入口与基础API | P1 |
| 性能基准测试与优化 | P2 |
| 完整文档与使用指南 | P1 |

---

## 11. 开放问题决策记录

| 问题 | 决策 | 理由 |
|------|------|------|
| Agent角色模板格式？ | YAML声明式，包含system_prompt/llm/skills/tools/memory配置 | 人类可读、版本控制友好、易于扩展 |
| 向量检索Embedding模型？ | sentence-transformers (本地) + 可配置远程API | 默认零依赖本地运行，生产可切换 |
| MCP Server多实例？ | M1-M5单实例，M6评估是否需要多实例 | 初期复杂度控制，按需扩展 |
| Agent间通信模式？ | 异步消息传递 (asyncio.Queue + EventBus) | 不阻塞Agent执行，支持扇出/广播 |
| Agent暂停/恢复？ | M6再考虑，初期不支持 | 增加状态序列化复杂度，非MVP必须 |
| 资源竞争？ | Coordinator统一管理共享资源，Agent不直接竞争 | 简化并发模型，避免死锁 |

---

## 12. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| LLM输出不稳定导致任务分析失败 | 高 | Analyzer输出严格Schema校验 + JSON Mode + 重试 + 降级到人工确认 |
| MCP Server单点故障 | 高 | 进程内模式作为fallback + 健康检查 + 自动重启 |
| Agent无限循环(ReAct不收敛) | 中 | max_iterations硬限制 + Token预算 + 循环检测 |
| 记忆数据膨胀 | 中 | TTL自动过期 + 归档压缩 + 存储配额 |
| Tool执行超时阻塞Agent | 中 | 工具调用超时 + 异步取消 + 超时降级 |
