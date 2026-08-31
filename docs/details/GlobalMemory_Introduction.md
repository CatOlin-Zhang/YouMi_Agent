# 全局记忆与工具经验沉淀详解（P6）

> 对应代码：`youmi/knowledge/`（global_memory.py / models.py / experience_extractor.py）、`youmi/coordinator/post_task.py`、`youmi/coordinator/tool_guardian.py`、`youmi/coordinator/fix_strategies.py`

全局记忆（GlobalMemory）是跨任务的工具经验知识库，**专供工具管理 Agent（ToolGuardianAgent）诊断修复工具问题**，修复完成后标记 resolved。SubAgent 不注入这些记忆（避免上下文窗口膨胀）。

---

## 1. 设计原则

```
任务执行
  └─ 工具调用（成功/失败）
       ↓
PostTaskPipeline.update_global_memory()   任务结束后自动沉淀
  └─ GlobalMemory.add_experience()        写入知识条目（自动向量化）
                                          ↓
                              ToolGuardian 收到 ToolIssueReport
                                          ↓
                              _load_tool_knowledge()   修复前查询历史经验
                                          ↓
                              修复工具描述 / 生成代码建议
                                          ↓
                              _persist_fix_to_memory()  修复后写回
                                ├─ add_experience(BUG_FIX) → mark_resolved(self)
                                └─ 历史未解决条目 → mark_resolved()
```

SubAgent **不消费**全局记忆；全局记忆仅流向 ToolGuardian 这一个消费者，形成 **沉淀 → 诊断 → 修复 → 标记解决** 闭环。

---

## 2. 数据模型（youmi/knowledge/models.py）

### KnowledgeCategory

```python
class KnowledgeCategory(str, Enum):
    TOOL_EXPERIENCE = "tool_experience"  # 工具使用经验（成功/失败模式）
    TASK_PATTERN    = "task_pattern"     # 任务执行模式
    BUG_FIX         = "bug_fix"          # 修复记录（ToolGuardian 写入）
```

### KnowledgeEntry（主要字段）

| 字段 | 类型 | 说明 |
|------|------|------|
| `entry_id` | str | UUID |
| `category` | KnowledgeCategory | 知识类型 |
| `tool_name` | str | 关联工具名 |
| `content` | str | 经验文本 |
| `embedding` | list[float] \| None | 向量（写入时自动生成） |
| `source_task_id` | str | 来源任务 ID |
| `source_agent_id` | str | 来源 Agent ID |
| `success_rate` | float | 工具调用成功率 |
| `resolved` | bool | 问题是否已解决 |
| `resolution` | str | 修复方案描述 |
| `metadata` | dict | 扩展字段 |
| `created_at / updated_at` | datetime | 时间戳 |

属性 `is_bug` → `category in (TOOL_EXPERIENCE, BUG_FIX) and not resolved`。

### ToolKnowledge（工具经验聚合视图）

```python
class ToolKnowledge(BaseModel):
    tool_name: str
    best_practices: list[str]   # 成功经验提炼
    known_issues: list[str]     # 未解决问题
    resolved_issues: list[str]  # 已解决问题摘要
    fix_history: list[str]      # 历史修复记录
    entry_ids: list[str]        # 来源条目 ID
```

属性 `is_empty` → 四个列表均为空。

---

## 3. GlobalMemory 核心（youmi/knowledge/global_memory.py）

### 存储结构

两张 SQLite 表（默认路径 `.youmi_knowledge.db`）：

```sql
knowledge_entries   -- 主表：12 列含 resolved/resolution
knowledge_vectors   -- 向量索引：entry_id + tool_name + embedding_json
```

索引：`idx_knowledge_tool`（按工具名）、`idx_knowledge_category`、`idx_knowledge_updated`。

### 主要接口

```python
gm = GlobalMemory(db_path=".youmi_knowledge.db", embedding_client=embedder)
await gm.initialize()

# 写入经验（自动向量化；embedding 失败不阻塞写入）
entry = await gm.add_experience(
    tool_name="file_read",
    content="路径参数必须使用绝对路径",
    category=KnowledgeCategory.TOOL_EXPERIENCE,
    source_task_id="task_001",
    success_rate=0.6,
)

# 批量写入（批量向量化，效率更高）
ids = await gm.batch_add([entry1, entry2, ...])

# 语义检索（有 embedding_client → 余弦相似度；无 → 关键词匹配）
results = await gm.search("file_read 路径问题", tool_name="file_read", top_k=5)

# 聚合查询（ToolGuardian 修复前调用）
knowledge = await gm.get_tool_knowledge("file_read")  # 返回 ToolKnowledge

# 修复闭环
updated = await gm.mark_resolved(entry_id, "v0.0.2 修复了路径解析逻辑")

# 列表查询
entries = await gm.list_entries(tool_name="file_read", unresolved_only=True, limit=50)

# 统计
stats = await gm.stats()  # {total, by_category, resolved_count, unresolved_count}
```

---

## 4. ToolExperienceExtractor 经验提取（experience_extractor.py）

从 Agent 对话记录提取工具使用经验：

```python
extractor = ToolExperienceExtractor(llm_call=async_fn, max_samples=3)
experience = await extractor.extract(conversation, tool_name)
```

**双模式分析失败根因**：

1. **LLM 增强模式**（有 `llm_call`）：将失败对话片段发给 LLM，获取结构化失败分析（根因/错误类型/建议）
2. **规则降级模式**（无 LLM 或 LLM 失败）：基于 `_ERROR_RULES` 六类模板匹配：

| 规则类型 | 关键词 | 模板内容 |
|---------|--------|---------|
| `missing_target` | file not found / path does not exist | 目标资源不存在，需先确认 |
| `permission` | permission denied / access denied | 权限不足 |
| `timeout` | timeout / timed out | 超时，考虑增加 timeout 参数 |
| `invalid_params` | invalid / unexpected keyword | 参数格式或取值范围错误 |
| `encoding` | unicode / codec / encoding | 编码问题，使用 utf-8 |
| `network` | connection refused / connection error | 网络连接失败 |

`to_experience_content(experience)` 静态方法将提取结果转为结构化经验文本（供 `add_experience` 写入）。

---

## 5. PostTaskPipeline 阶段4（post_task.py）

`update_global_memory(master, experiences)` 三步逻辑：

```python
# 步骤1：写入工具使用统计经验（每个工具一条 TOOL_EXPERIENCE 记录）
content = ToolExperienceExtractor.to_experience_content(exp)
await global_memory.add_experience(tool_name, content, TOOL_EXPERIENCE, success_rate=...)

# 步骤2：高失败率工具（success_rate < 0.7）→ 失败根因分析 → 写入
experience = await extractor.extract(conversation, tool_name)
await global_memory.add_experience(tool_name, failure_analysis, TOOL_EXPERIENCE)

# 步骤3：版本更新触发（累计失败 ≥3 且成功率 <0.5）
if self._failure_counts[tool_name] >= 3 and success_rate < 0.5:
    ok = await trigger_tool_version_update(tool_name, store)
    if ok:
        await global_memory.add_experience(tool_name, ..., BUG_FIX)
        self._failure_counts[tool_name] = 0   # 重置计数
```

`_failure_counts` 跨 `run()` 调用在 pipeline 实例级累计（同一 MasterAgent 生命周期内保持）。

---

## 6. ToolGuardianAgent 全局记忆闭环（tool_guardian.py）

```python
guardian = ToolGuardianAgent(
    mcp_server=server,
    global_memory=global_memory,   # 注入后开启 P6 闭环
)
```

修复前查询历史经验：

```python
async def _load_tool_knowledge(tool_name) -> ToolKnowledge | None:
    # global_memory.get_tool_knowledge(tool_name)
    # 失败返回 None → 降级（不影响修复流程）
```

修复后写回：

```python
async def _persist_fix_to_memory(tool_name, modification, fix_summary):
    # 1. add_experience(BUG_FIX) → 立即 mark_resolved(self)
    # 2. list_entries(tool_name, unresolved_only=True) → 逐一 mark_resolved()
```

内置工具 `search_tool_experience`（`tool_guardian.py` 注册）：ToolGuardian LLM 可主动调用，在全局记忆中语义检索指定工具的历史经验（失败/异常时返回明确状态信息）。

---

## 7. FixStrategiesMixin 历史经验注入（fix_strategies.py）

`_generate_fix(tool_name, ..., tool_knowledge)` 支持注入 `ToolKnowledge`：

**LLM 路径**：prompt 包含「历史经验」段：

```
## 历史经验 (来自全局记忆)
### 已知未解决问题 (重复出现，优先根治)
  - ...（最多5条，截取前150字）
### 历史修复记录 (不要重复同样的修复)
  - ...
### 已解决的历史问题
  - ...
注意：已知未解决问题与本次汇报可能同源，请给出根治性修复。
```

**规则路径**：已知未解决问题经去重（前60字对比）后追加到描述末尾：

```
[全局记忆] 历史已知未解决问题（本次一并修正）:
  - ...
```

---

## 8. MasterAgent 集成

```python
master = MasterAgent(config, global_memory=GlobalMemory(...))
```

`on_stop()` 中自动发现 ToolStore（`self._tool_bridge.vault.store`）并传入 PostTaskPipeline：

```python
pipeline = PostTaskPipeline(tool_store=tool_store, global_memory=self._global_memory)
await pipeline.run(master, task_results)
```

---

## 9. 顶层导出

```python
from youmi import (
    GlobalMemory,
    KnowledgeCategory,
    KnowledgeEntry,
    ToolKnowledge,
    ToolExperienceExtractor,
)
```

---

## 10. 相关文档

- [MCP_Introduction.md](MCP_Introduction.md) — ToolStore 版本更新与工具守护
- [Memory_Introduction.md](Memory_Introduction.md) — Session 记忆与全局记忆的区别
- [Master_Introduction.md](Master_Introduction.md) — PostTaskPipeline 在任务结束时的触发
