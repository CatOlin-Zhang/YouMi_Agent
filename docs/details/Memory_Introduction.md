# 记忆系统详解

> 对应代码：`youmi/memory/`（memory.py / strategies/ / backends/ / compaction.py）

每个 Agent 拥有独立的记忆空间，通过 `MemoryManager` 统一管理会话上下文、持久化存储与语义检索。记忆系统采用**策略（MemoryStrategy）+ 后端（PersistenceBackend）分离**的可插拔设计。

---

## 1. 架构总览

```
Agent
  └─ MemoryManager
        ├─ MemoryStrategy         ← 控制上下文窗口（Full/Summary/LSTM）
        │    └─ messages[]        ← 内存消息队列
        │
        ├─ PersistenceBackend     ← 跨任务持久化（SQLite / File）
        │    └─ sessions/         ← 会话级消息归档
        │
        └─ ContextCompactor       ← 超 token 时自动压缩（独立于策略）
```

---

## 2. MemoryManager 统一接口（memory.py）

| 方法 | 说明 |
|------|------|
| `await initialize()` | 初始化策略与后端（幂等） |
| `await on_message(role, content)` | 记录新消息（写入策略内存+异步持久化） |
| `await get_context()` | 获取当前上下文列表（`list[{role, content}]`），用于 LLM 调用 |
| `await search(query, top_k, embedding_client)` | 语义检索（有 EmbeddingClient 走向量，否则降级关键词） |
| `await clear()` | 清空当前会话记忆 |
| `start_session(session_id="")` | 开启新会话，返回 session_id |
| `await restore_session(session_id)` | 恢复历史会话 |
| `await save_session(messages, session_id)` | 手动持久化 |
| `await on_session_end()` | 会话结束钩子（自动保存到后端） |
| `await close()` | 释放资源 |
| `await snapshot()` | 返回状态快照（调试用） |

属性：`strategy`（当前策略实例）、`strategy_name`（策略名称）、`agent_id`、`persistence`（后端实例）、`current_session_id`。

---

## 3. 记忆策略（youmi/memory/strategies/）

三种内置策略，全部继承 `MemoryStrategy` 基类：

### FullMemoryStrategy（full.py）

完整保留所有消息，不做任何截断或压缩。适合短任务或上下文窗口充足的场景。`get_context()` 直接返回全部消息列表。

`search(query, top_k)` 遍历全部消息做关键词匹配（空格分词 + 中文子串），返回得分最高的 top_k 条。

### SummaryMemoryStrategy（summary.py）

维护**动态摘要**：超过窗口时将早期消息 LLM 压缩为摘要，注入新会话的 system 消息。`get_context()` 返回 `[摘要消息, ...近期消息]`。

`search(query, top_k)` 同时检索摘要文本与近期消息，合并去重后返回。

### LSTMMemoryStrategy（lstm.py）

模拟 LSTM 门控机制维护**长期记忆**与**短期记忆**双通道：

- **短期记忆**：滑动窗口（最近 N 条），逐字写入
- **长期记忆**：重要消息经评分后选择性保留，跨会话累积
- `get_context()` 按 [长期记忆 → 短期记忆] 顺序拼接，LLM 可同时看到跨任务背景与当前上下文
- `search(query, top_k)` 优先检索长期记忆，再检索短期

### MemoryStrategy 基类（strategies/base.py）

```python
class MemoryStrategy(ABC):
    @abstractmethod
    async def on_message(role, content): ...   # 写入消息
    @abstractmethod
    async def get_context(): ...              # 返回上下文列表
    async def initialize(): ...               # 可选初始化
    async def clear(): ...                    # 清空
    async def on_session_end(): ...           # 会话结束钩子
    async def search(query, top_k=5): ...     # 默认空实现，子类可覆盖

    @staticmethod
    def keyword_search(messages, query, top_k): ...  # 通用关键词检索工具
```

`create_strategy(strategy_name, agent_id, config)` 工厂函数（`strategies/__init__.py`）按名称实例化策略，支持 `"full"` / `"summary"` / `"lstm"` 以及自定义策略类注册。

---

## 4. 持久化后端（youmi/memory/backends/）

### PersistenceBackend 基类（base.py）

```python
class PersistenceBackend(ABC):
    async def save_session(session: SessionRecord): ...
    async def load_session(session_id): ...
    async def list_sessions(agent_id): ...
    async def delete_session(session_id): ...
```

核心数据模型：

- `MessageRecord`：单条消息（role / content / timestamp / metadata）
- `SessionRecord`：会话（session_id / agent_id / messages / created_at / updated_at）

### SQLiteBackend（sqlite_backend.py）

基于 `asyncio.to_thread(sqlite3.connect, ...)` 的异步 SQLite 实现，适合生产使用：会话与消息落盘、支持按 agent_id 多会话管理、按时间排序检索。

### FileBackend（file_backend.py）

每个会话存为独立 JSON 文件（`{agent_id}/{session_id}.json`），适合开发调试与低配环境。

---

## 5. 上下文压缩（youmi/memory/compaction.py）

`ContextCompactor` 在记忆策略之上工作，当 token 预算超限时自动压缩：

```python
compactor = ContextCompactor(
    max_tokens=4000,          # 触发压缩的阈值
    llm_call=async_llm_func,  # 用于生成摘要的 LLM 调用函数
)

# 在每轮 _act() 后调用
messages = await compactor.maybe_compact(messages)
# 若超限，early messages 被替换为 LLM 生成的摘要消息
```

属性：`current_summary`（当前累积摘要）、`compaction_count`（已压缩次数）、`max_tokens`。

`needs_compaction(messages)` 可提前检查是否需要压缩。`reset()` 清除摘要状态。

---

## 6. 向量语义检索（P6 扩展）

`MemoryManager.search(query, top_k, embedding_client)` 提供语义级检索：

1. **向量路径**（传入 `embedding_client`）：
   - `embedding_client.embed(all_messages_text)` 批量向量化历史消息
   - `embedding_client.embed_one(query)` 向量化查询
   - 余弦相似度排序，返回 similarity > 0.1 的 top_k 条
   
2. **降级路径**（无 `embedding_client` 或向量化失败）：
   - 委托给 `MemoryStrategy.search(query, top_k)` 做关键词匹配

所有三种策略（full/summary/lstm）均实现了 `search()` 覆盖，各自利用自身存储路径做语义/关键词检索。

---

## 7. MemoryConfig 配置项

```python
@dataclass
class MemoryConfig:
    strategy: str = "full"              # "full" | "summary" | "lstm" | 自定义
    backend: str = "sqlite"             # "sqlite" | "file" | None（不持久化）
    db_path: str = ".youmi_memory.db"   # SQLite 路径（backend="sqlite" 时生效）
    file_dir: str = ".youmi_sessions"   # 文件目录（backend="file" 时生效）
    max_messages: int = 0               # 0=不限制
    compaction_tokens: int = 0          # 0=禁用压缩
    extra: dict = {}                    # 策略专用扩展配置
```

---

## 8. 与全局记忆的区别

| 维度 | MemoryManager（Session 记忆） | GlobalMemory（全局知识库） |
|------|------------------------------|--------------------------|
| 作用域 | 单 Agent + 单会话 | 全局跨任务 |
| 存储对象 | LLM 对话消息序列 | 工具经验知识条目 |
| 消费者 | 该 Agent 的 LLM 上下文 | ToolGuardian 诊断修复用 |
| 注入 SubAgent | ✅ 自动注入对话上下文 | ❌ 不注入（避免容量膨胀） |
| 生命周期 | 与 Agent 绑定 | 跨工作流持久化 |

---

## 9. 相关文档

- [Agent_Introduction.md](Agent_Introduction.md) — MemoryManager 在 Agent 中的使用
- [GlobalMemory_Introduction.md](GlobalMemory_Introduction.md) — 全局工具经验知识库
