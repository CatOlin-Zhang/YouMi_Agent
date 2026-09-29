"""
任务注册表 (gateway.registry)

内存态任务记录存储，提供：
- 按 task_id 查询 / 更新
- 按 tenant / status 过滤列表（新→旧）
- 状态统计（供 /stats 类端点使用）
- 容量保护：超过上限时优先淘汰最老的**已完结**记录
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from youmi.gateway.models import TaskRecord, TaskStatus


class TaskRegistry:
    """内存任务记录注册表。

    Args:
        max_records: 记录上限；超出时从最老记录开始淘汰已完结任务
    """

    def __init__(self, max_records: int = 1000) -> None:
        self._records: OrderedDict[str, TaskRecord] = OrderedDict()
        self._max = max_records

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def put(self, record: TaskRecord) -> None:
        """写入 / 更新任务记录（保持插入顺序）"""
        self._records[record.task_id] = record
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        """超限时优先淘汰最老的已完结记录（全为运行中时暂不淘汰）"""
        if self._max <= 0 or len(self._records) <= self._max:
            return
        overflow = len(self._records) - self._max
        for task_id in list(self._records.keys()):
            if overflow <= 0:
                break
            record = self._records[task_id]
            if record.status.finished:
                del self._records[task_id]
                overflow -= 1

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def get(self, task_id: str) -> TaskRecord | None:
        return self._records.get(task_id)

    def list(
        self,
        *,
        tenant: str | None = None,
        status: TaskStatus | None = None,
        limit: int = 100,
    ) -> list[TaskRecord]:
        """列出任务记录（新→旧）。

        Args:
            tenant: 租户过滤（None = 不过滤）
            status: 状态过滤（None = 不过滤）
            limit: 返回上限（<=0 表示不限制）
        """
        records = list(reversed(self._records.values()))
        if tenant is not None:
            records = [r for r in records if r.tenant == tenant]
        if status is not None:
            records = [r for r in records if r.status == status]
        if limit > 0:
            records = records[:limit]
        return records

    def stats(self) -> dict[str, Any]:
        """状态统计：总数 + 各状态计数 + 队列运行中数"""
        counts: dict[str, int] = {s.value: 0 for s in TaskStatus}
        for r in self._records.values():
            counts[r.status.value] += 1
        return {"total": len(self._records), "by_status": counts}

    def __len__(self) -> int:
        return len(self._records)


__all__ = ["TaskRegistry"]
