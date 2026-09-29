"""
sqlite-vec 向量检索工具函数

提供 sqlite-vec 扩展加载、向量归一化、距离转换等共享辅助功能。
三个持久化模块（ToolStore、GlobalMemory、PlanMemory）共用此处的工具函数。

用法::

    from youmi._vec_utils import try_load_sqlite_vec, normalize_vector, l2_to_cosine

    conn = sqlite3.connect(db_path)
    vec_available = try_load_sqlite_vec(conn)

    if vec_available:
        normalized = normalize_vector(embedding)
        conn.execute(
            "INSERT INTO vec_idx(rowid, embedding) VALUES (?, ?)",
            (row_id, json.dumps(normalized)),
        )
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# sqlite-vec 扩展加载
# ---------------------------------------------------------------------------

def try_load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """尝试加载 sqlite-vec 扩展到指定连接。

    成功时连接将获得 vec0 虚拟表、vec_normalize()、vec_distance_cosine()
    等 sqlite-vec 函数；失败时连接不受影响，调用方应降级到纯 Python 方案。

    Args:
        conn: 已打开的 SQLite 连接

    Returns:
        True 表示加载成功，False 表示不可用
    """
    try:
        import sqlite_vec  # type: ignore[import-untyped]
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except Exception as exc:
        logger.info("sqlite-vec 扩展不可用，降级为纯 Python 向量检索: %s", exc)
        return False


# ---------------------------------------------------------------------------
# 向量工具函数
# ---------------------------------------------------------------------------

def normalize_vector(vec: list[float]) -> list[float]:
    """L2 归一化向量（单位化）。

    零向量原样返回，避免除零。

    Args:
        vec: 浮点向量

    Returns:
        归一化后的向量（模长为 1）
    """
    if not vec:
        return vec
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def l2_to_cosine(distance: float) -> float:
    """归一化向量的 L2 距离转换为余弦相似度。

    数学关系：对于两个归一化向量 a, b：
        L2_distance² = 2 × (1 - cosine_similarity)
    因此：
        cosine_similarity = 1 - distance² / 2

    Args:
        distance: 归一化向量的 L2 距离 (≥ 0)

    Returns:
        余弦相似度 [-1, 1]
    """
    return 1.0 - (distance * distance) / 2.0


def cosine_to_l2(similarity: float) -> float:
    """余弦相似度转换为归一化向量的 L2 距离。

    用于将 min_score 阈值转换为 KNN 查询的 distance 过滤条件。

    Args:
        similarity: 余弦相似度

    Returns:
        L2 距离 (≥ 0)
    """
    val = 2.0 * (1.0 - similarity)
    if val < 0.0:
        return 0.0
    return math.sqrt(val)


def vec_to_json(vec: list[float]) -> str:
    """向量序列化为 JSON 字符串。

    sqlite-vec 接受 JSON 数组格式的向量输入（如 '[0.1, 0.2, 0.3]'）。

    Args:
        vec: 浮点向量

    Returns:
        JSON 字符串
    """
    return json.dumps(vec)


def cosine_similarity_python(a: list[float], b: list[float]) -> float:
    """纯 Python 余弦相似度计算（降级用）。

    与 sqlite-vec 无关，作为扩展不可用时的降级实现。

    Args:
        a: 向量 A
        b: 向量 B

    Returns:
        余弦相似度 [-1, 1]，零向量或维度不匹配时返回 0.0
    """
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# normalize_vector_inplace 是 normalize_vector 的语义别名
normalize_vector_inplace = normalize_vector
