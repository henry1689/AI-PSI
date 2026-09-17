"""记忆检索的公共部件（任务书 §10，不变量 14/15）。

本模块提供两个实现（内存 / PostgreSQL）都要用到的**纯函数**：
相似度计算、召回规模、零向量判定。把它们放在这里而不是各自实现一遍，
是因为"两个实现在同一个查询上给出不同结果"正是契约测试要防的事——
而排序是契约测试**看不到**的部分（断言无法覆盖"哪条更相关"）。

检索的整体形状是**两段式**：

1. **召回**：按向量余弦距离取回 ``recall_size(limit)`` 条候选。
   多取几倍是刻意的——召回阶段只看向量，而排序阶段还会加上
   词面重合与时效，只取 ``limit`` 条会让排序阶段无从调整。
2. **排序**：交给 :func:`ai_psi.memory.ranking.rank_candidates`。

🔴 **过滤发生在召回之前，绝不在排序之后。**
"先取前 N 条再过滤掉不属于本用户的"会形成可观测的信息泄漏：
其他用户的高分记忆会挤占名额，本用户真正相关的低分记忆被挤出结果集，
于是**结果条数的变化本身就泄露了别人的记忆是否存在**。
"""

from __future__ import annotations

from typing import Final

from ai_psi.reliability.repetition_detector import jaccard, normalized_ngrams

__all__ = [
    "DEFAULT_RECALL_MULTIPLIER",
    "MIN_RECALL_SIZE",
    "cosine_similarity",
    "is_zero_vector",
    "lexical_overlap",
    "recall_size",
]

#: 召回倍数：候选数 = ``limit × 该倍数``。
DEFAULT_RECALL_MULTIPLIER: Final[int] = 4

#: 召回条数的下限。
#:
#: 单条检索（``limit=1``）时只召回 4 条，样本太少，排序阶段几乎没有
#: 调整余地。20 条在"一个用户的记忆规模"下是廉价的。
MIN_RECALL_SIZE: Final[int] = 20


def recall_size(limit: int) -> int:
    """按结果条数上限推算召回条数。

    Args:
        limit: 调用方要求的返回条数上限。

    Returns:
        召回阶段应取回的候选条数。
    """
    return max(MIN_RECALL_SIZE, max(0, limit) * DEFAULT_RECALL_MULTIPLIER)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """两个向量的余弦相似度。

    🔴 长度不一致时**抛错而不是截断**。维度不同的向量属于不同的向量空间，
    比较它们得到的数字没有任何含义，而"截断到较短的那个"会让它
    看起来像一次正常的比较。

    Args:
        left: 向量 A。
        right: 向量 B。

    Returns:
        ``[-1, 1]`` 的相似度。任一向量为零向量时返回 ``0.0``——
        零向量没有方向，它和任何向量的"夹角"都是未定义的；
        返回 0（无关）比返回 NaN 安全，NaN 会污染整条排序链。
    """
    if len(left) != len(right):
        msg = f"向量维度不一致：{len(left)} 与 {len(right)}，它们属于不同的向量空间"
        raise ValueError(msg)
    if is_zero_vector(left) or is_zero_vector(right):
        return 0.0

    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if left_norm == 0.0 or right_norm == 0.0:  # pragma: no cover - 上面已挡住
        return 0.0
    return float(dot / (left_norm * right_norm))


def is_zero_vector(vector: list[float]) -> bool:
    """判断是否为零向量。

    空文本编码后就是零向量。零向量对 pgvector 的余弦距离是未定义的，
    直接拿去排序会得到 NaN——而 NaN 在 ``ORDER BY`` 里的行为是
    "排在哪都不确定"，结果集看起来正常却毫无意义。
    因此检索路径必须先识别它，退回纯词面召回。
    """
    return all(component == 0.0 for component in vector)


def lexical_overlap(query: str, content: str) -> float:
    """查询与内容的词面重合度（字符 n-gram Jaccard）。

    复用 :mod:`ai_psi.reliability.repetition_detector` 的切分方式——
    两处对"文本像不像"的判断标准不该互相矛盾。

    Args:
        query: 查询文本。
        content: 记忆内容。

    Returns:
        ``[0, 1]`` 的重合度。
    """
    return jaccard(normalized_ngrams(query), normalized_ngrams(content))
