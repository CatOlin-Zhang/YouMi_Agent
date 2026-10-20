#!/usr/bin/env python
"""为 A/B 热库阶段灌入种子数据。

设计要点：

- 计划（Plan）的 task_text 直接取 ab/tasks.mjs 里的 prompt 原文。
  sidecar 在未配置 embedding 时走关键词降级（`_keyword_score`，阈值 0.85），
  只有原文一致才能拿满分命中，这也对应真实场景里「重复执行同一任务」。
- SOP（Skill）与失败经验（Experience）一并写入，代表能力库已有存量；
  但要注意 SkillStore 的关键词降级是 `describe LIKE %query%`（整串子串），
  自然语言 query 基本命中不了，因此热库阶段的可测量收益主要来自计划复用。
  这是当前实现的真实瓶颈，不是实验缺陷，报告中会单独说明。

用法::

    node scripts/ab-run.mjs --dump-tasks .ab/tasks.json
    D:\\ANACONDA\\envs\\AI\\python.exe scripts/ab-seed.py --reset
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from youmi.knowledge.global_memory import GlobalMemory  # noqa: E402
from youmi.knowledge.models import KnowledgeCategory  # noqa: E402
from youmi.mcp.skill_store import SkillEntry, SkillStore  # noqa: E402
from youmi.plan.models import WorkflowPlan, WorkflowStep  # noqa: E402
from youmi.plan.plan_memory import PlanMemory  # noqa: E402


# ---------------------------------------------------------------------------
# 种子知识：按任务 ID 给出可复用的步骤骨架与踩坑记录
# ---------------------------------------------------------------------------

PLANS: dict[str, dict] = {
    't1-count-todo': {
        'name': '统计文件内关键词出现次数',
        'steps': [
            ('read', '读取目标文件的完整内容'),
            ('count', '按行统计，包含目标关键词的行各计一次'),
            ('write', '只把统计出的数字写入结果文件，不要附加任何说明文字'),
        ],
        'pitfall': '结果文件要求「只写数字」时，不要附带单位、解释或 Markdown 标记，'
                   '否则按去空白后完全相等比较会失败。',
    },
    't2-merge-json': {
        'name': '合并两个 JSON 配置并统计顶层键',
        'steps': [
            ('read', '分别读取两个 JSON 文件的全部键值'),
            ('merge', '以第二个文件为准覆盖同名键，其余键保留'),
            ('count', '统计合并结果的顶层键数量并写入计数文件'),
        ],
        'pitfall': '用 PowerShell 统计对象属性个数时，`$obj.Count` 对非集合对象会返回 1 或空；'
                   '应改用 `($obj.PSObject.Properties | Measure-Object).Count`，或直接用 Node 处理 JSON。',
    },
    't3-fix-range': {
        'name': '定位并修复失败的单元测试',
        'steps': [
            ('run', '先运行测试拿到失败的断言与期望值'),
            ('locate', '对照断言反推实现里的边界错误'),
            ('fix', '只改实现代码，保持测试文件不动'),
            ('verify', '重新运行测试确认退出码为 0'),
        ],
        'pitfall': '测试断言要求 0 起始的序列时，循环应为 `for (let i = 0; i < n; i += 1)`；'
                   '修复后必须重跑测试验证，不要只凭阅读判断。',
    },
    't4-rename-symbol': {
        'name': '跨文件重命名符号',
        'steps': [
            ('search', '先列出全部出现位置（定义 + 导入 + 调用点）'),
            ('edit', '逐文件替换，保持作用域与导出语句不变'),
            ('verify', '全局复查旧名称已无残留'),
        ],
        'pitfall': '重命名要同时覆盖定义处、`import { ... }` 的解构名与所有调用点；'
                   '只改定义处会导致导入失败。',
    },
    't5-todo-report': {
        'name': '多文件扫描生成统计报表',
        'steps': [
            ('scan', '遍历目标目录下所有匹配扩展名的文件'),
            ('count', '逐文件统计目标注释的条数'),
            ('report', '按文件名升序输出两列 Markdown 表格，没有命中的文件计 0'),
        ],
        'pitfall': '统计表要按文件名升序排列，且表头与分隔行要完整，'
                   '断言会用正则匹配「文件名 + 数字」的相邻关系。',
    },
    't6-space-path': {
        'name': '处理含空格的文件名',
        'steps': [
            ('read', '把带空格的路径整体作为一个字符串参数读取'),
            ('count', '统计行数（按换行切分，忽略末尾空行）'),
            ('write', '写入「行数: N」格式的摘要文件'),
        ],
        'pitfall': '带空格的路径在 shell 里必须整体加引号；'
                   '更稳妥的做法是用文件读写工具而不是 shell 重定向。',
    },
    't7-scaffold': {
        'name': '按规范创建 Python 包结构',
        'steps': [
            ('layout', '创建包目录与 tests 目录'),
            ('impl', '在模块里定义函数，并在 __init__.py 中显式导出'),
            ('test', '用 unittest 编写断言用例，不需要安装依赖'),
        ],
        'pitfall': '__init__.py 必须显式 `from .模块 import 函数` 才算「导出」，'
                   '仅创建空文件不满足要求。',
    },
    't8-parse-log': {
        'name': '解析访问日志做聚合统计',
        'steps': [
            ('read', '逐行解析日志，切出请求路径与状态码'),
            ('aggregate', '统计 404 次数，并按路径聚合出现次数'),
            ('pick', '取出现次数最多的路径，并列时取字典序最小'),
            ('write', '输出 JSON 对象，键名与要求一致'),
        ],
        'pitfall': '「并列时取字典序最小」需要显式处理，'
                   '直接依赖排序稳定性会得到不可预期的结果。',
    },
}

# 与具体任务无关、但在这套环境里反复出现的通用经验
GENERAL_EXPERIENCES: list[tuple[str, str]] = [
    (
        '文件输出',
        '在 Windows 上通过 PowerShell 写文件可能带 UTF-8 BOM，'
        '导致下游按纯文本解析时首字符异常。用文件写入工具直写，或显式指定无 BOM 编码。',
    ),
    (
        'shell 调用',
        'PowerShell 内联命令里的双引号容易被剥离，拼接复杂表达式时改用脚本文件承载，不要塞进一行命令。',
    ),
    (
        '结果校验',
        '任务要求写入具体格式时，写完应当再读一遍核对内容，'
        '避免出现「已写入但格式不符」的隐性失败。',
    ),
]


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def build_plan(task: dict, spec: dict) -> WorkflowPlan:
    steps = []
    for index, (step_id, task_text) in enumerate(spec['steps']):
        steps.append(
            WorkflowStep(
                step_id=step_id,
                role='executor',
                task=task_text,
                depends_on=[spec['steps'][index - 1][0]] if index else [],
            )
        )
    return WorkflowPlan(name=spec['name'], steps=steps, description=task['title'])


def build_skill(task: dict, spec: dict) -> SkillEntry:
    body = '\n'.join(f'{i + 1}. {text}' for i, (_, text) in enumerate(spec['steps']))
    describe = f"{task['title']}：{spec['name']} 的标准步骤。"
    return SkillEntry(
        skill_name=f"sop-{task['id']}",
        describe=describe,
        summary=describe[:80],
        summary_l2=spec['name'][:30],
        content_json=f"## {spec['name']}\n\n{body}\n\n注意事项：{spec['pitfall']}\n",
        tags=['sop', 'coding'],
    )


async def seed(tasks: list[dict], data_dir: Path, reset: bool) -> dict:
    if reset and data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    skills = SkillStore(db_path=str(data_dir / 'skills.db'))
    plans = PlanMemory(db_path=str(data_dir / 'plans.db'))
    memory = GlobalMemory(db_path=str(data_dir / 'knowledge.db'))
    for component in (skills, plans, memory):
        await component.initialize()

    try:
        skill_ids = []
        plan_ids = []
        for task in tasks:
            spec = PLANS.get(task['id'])
            if spec is None:
                continue
            skill_ids.append(await skills.upsert_skill(build_skill(task, spec)))
            # 关键：task_text 用 prompt 原文，关键词降级检索才能精确命中
            plan_ids.append(
                await plans.save_plan(task['prompt'], build_plan(task, spec), success=True)
            )
            await memory.add_experience(
                tool_name='',
                content=f"【{task['title']}】{spec['pitfall']}",
                category=KnowledgeCategory.TASK_PATTERN,
                metadata={'task_id': task['id'], 'kind': 'pitfall'},
            )

        for topic, content in GENERAL_EXPERIENCES:
            await memory.add_experience(
                tool_name=topic,
                content=content,
                category=KnowledgeCategory.TOOL_EXPERIENCE,
                success_rate=0.9,
                metadata={'kind': 'environment'},
            )

        return {
            'skills': len(skill_ids),
            'plans': len(plan_ids),
            'experiences': len(tasks) + len(GENERAL_EXPERIENCES),
        }
    finally:
        for component in (plans, memory, skills):
            await component.close()


async def verify(tasks: list[dict], data_dir: Path) -> None:
    """回检：确认每个任务都能检索到自己刚灌的计划/SOP。

    走的是 sidecar 真实调用的同一个检索函数，因此未命中就说明
    热库阶段该能力不会带来任何收益，需要先修检索再重跑。
    """
    plans = PlanMemory(db_path=str(data_dir / 'plans.db'))
    skills = SkillStore(db_path=str(data_dir / 'skills.db'))
    await plans.initialize()
    await skills.initialize()

    print('\n回检（plan / skill 命中数）')
    try:
        for task in tasks:
            plan_hits = await plans.search_plan(task['prompt'], top_k=3)
            skill_hits = await skills.search_cone(task['prompt'], top_k=3)
            flag = 'OK' if plan_hits else 'MISS'
            print(f"  {task['id']:<18} plan={len(plan_hits)} skill={len(skill_hits)}  {flag}")
    finally:
        await plans.close()
        await skills.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description='灌入 A/B 热库阶段种子数据')
    parser.add_argument('--tasks', default=str(ROOT / '.ab' / 'tasks.json'))
    parser.add_argument('--data-dir', default=str(ROOT / '.ab' / 'youmi-data' / 'warm'))
    parser.add_argument('--reset', action='store_true', help='先清空目标数据目录')
    parser.add_argument('--verify', action='store_true', help='灌完后回检检索命中')
    args = parser.parse_args()

    tasks_path = Path(args.tasks)
    if not tasks_path.exists():
        print(f'找不到任务清单 {tasks_path}，先执行：node scripts/ab-run.mjs --dump-tasks .ab/tasks.json')
        return 1

    tasks = json.loads(tasks_path.read_text(encoding='utf-8'))
    data_dir = Path(args.data_dir)
    stats = await seed(tasks, data_dir, args.reset)
    print(f"种子完成 → {data_dir}")
    print(f"  skills={stats['skills']} plans={stats['plans']} experiences={stats['experiences']}")
    if args.verify:
        await verify(tasks, data_dir)
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
