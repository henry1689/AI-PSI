"""记忆候选的**综合排序**（任务书 §9.3 的检索要求，记忆侧）。

任务书要求检索综合：主题相关性；时间相关性；来源可靠性；个人作用域；
当前有效性；冲突信息；敏感性；Token 预算。

其中**作用域、有效性与敏感性是过滤条件，不是排序权重**——
它们决定"能不能返回"，不决定"排多前"。把它们做成权重是一种
常见但危险的混淆：那等于说"别人的记忆只要分数够低就可以返回一点"。

本模块只负责排序，返回的每一条都**已经**通过了过滤
（见 :mod:`ai_psi.memory.retrieval`）。权重与半衰期都是**常量**，
不做成配置——在没有任何评测数据之前，把三个魔法数字暴露成配置项，
只会制造"看起来可调、实际没人知道该调成多少"的假象。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from ai_psi.domain.memories import Memory
from ai_psi.memory.retrieval import lexical_overlap

__all__ = ["DEFAULT_RANKING_WEIGHTS", "RankingWeights", "ScoredMemory", "rank_candidates"]

#: 相关度权重。
#:
#: 相关度 = 向量相似度与词面重合度的加权和，各占一半。
#: 之所以不把全部权重压给向量：默认的本地向量**本身就是词面的**
#: （见 :mod:`ai_psi.providers.embeddings`），两者高度相关；
#: 而换成真语义向量之后，词面重合仍然是一个有价值的补充信号——
#: 用户刚刚说过的那几个字，往往比"语义上接近"更该排在前面。
_RELEVANCE_VECTOR_WEIGHT: Final[float] = 0.5
_RELEVANCE_LEXICAL_WEIGHT: Final[float] = 0.5

#: 时效的**半衰期**（天）。
#:
#: 180 天是一个保守取值：记忆的类型差异极大——
#: 一条"用户偏好简洁回答"两年后仍然成立，一条"用户这周在准备考试"
#: 三个月后就过期了。用一个统一半衰期只能取折中，
#: 而**真正处理短期记忆的是 ``valid_until`` 与 ``retention_policy``**
#: （见 :mod:`ai_psi.memory.lifecycle`），时效权重只负责在都有效的前提下
#: 让新近的略占优。
_RECENCY_HALF_LIFE_DAYS: Final[float] = 180.0


@dataclass(frozen=True, slots=True)
class RankingWeights:
    """排序权重。

    Attributes:
        relevance: 相关度权重。
        recency: 时效权重。
        conflict_bonus: 存在显式冲突记录时的**加成**。
    """

    relevance: float = 0.9
    recency: float = 0.1
    conflict_bonus: float = 0.0


DEFAULT_RANKING_WEIGHTS: Final[RankingWeights] = RankingWeights()


@dataclass(frozen=True, slots=True)
class ScoredMemory:
    """一条候选记忆及其得分明细。

    明细**必须**能逐项看到：检索结果在事后被质疑时，
    只给一个总分是无法回答"为什么这条排在前面"的。

    Attributes:
        memory: 记忆本体。
        score: 综合得分。
        similarity: 向量相似度，已归一到 ``[0, 1]``。
        lexical: 词面重合度，``[0, 1]``。
        recency: 时效得分，``[0, 1]``。
    """

    memory: Memory
    score: float
    similarity: float
    lexical: float
    recency: float


def rank_candidates(
    candidates: list[tuple[Memory, float]],
    *,
    query: str,
    now: datetime,
    limit: int,
    weights: RankingWeights = DEFAULT_RANKING_WEIGHTS,
) -> list[ScoredMemory]:
    """对候选记忆综合排序并截断。

    🔴 **排序必须是确定的。** 得分相同时按 ``created_at`` 降序、
    再按 ``id`` 升序打破平局。少了最后这一层，
    两条完全同分的记忆在不同运行里会给出不同的顺序——
    而"检索结果不可复现"会让任何一次排查都无从下手。

    Args:
        candidates: ``(记忆, 向量相似度)`` 列表，相似度取值范围 ``[-1, 1]``。
        query: 原始查询文本，用于计算词面重合度。
        now: 当前时间，由调用方传入而不是在这里取——
            这样同一批候选在同一时刻的排序结果可复现。
        limit: 返回条数上限。
        weights: 排序权重。

    Returns:
        按得分降序排列的至多 ``limit`` 条结果。
    """
    scored: list[ScoredMemory] = []
    for memory, similarity in candidates:
        unit_similarity = (similarity + 1.0) / 2.0
        lexical = lexical_overlap(query, memory.content)
        recency = _recency_score(memory.valid_from, now)
        relevance = _RELEVANCE_VECTOR_WEIGHT * unit_similarity + _RELEVANCE_LEXICAL_WEIGHT * lexical
        score = weights.relevance * relevance + weights.recency * recency
        if memory.contradicts_ids:
            # 存在显式冲突记录的记忆**不降权**。
            #
            # 冲突本身是有价值的信息（任务书 §5.11）："关于这件事，
            # 系统同时记着两种说法"比"系统只记得其中一种"更接近事实。
            # 降权会让它在检索里消失，等于把冲突掩盖掉。
            score += weights.conflict_bonus
        scored.append(
            ScoredMemory(
                memory=memory,
                score=score,
                similarity=unit_similarity,
                lexical=lexical,
                recency=recency,
            )
        )

    scored.sort(key=_sort_key)
    return scored[: max(0, limit)]


def _sort_key(item: ScoredMemory) -> tuple[float, float, str]:
    """得分降序 → 创建时间降序 → id 升序。"""
    return (-item.score, -item.memory.created_at.timestamp(), str(item.memory.id))


def _recency_score(valid_from: datetime, now: datetime) -> float:
    """按半衰期折算的时效得分。

    未来的 ``valid_from``（时钟偏移、或预置的生效时间）会被
    截断为满分，而不是得到大于 1 的分数——大于 1 的得分会破坏
    "得分都在 ``[0, 1]``"这个不变量，进而让权重失去意义。

    Args:
        valid_from: 记忆的生效时间。
        now: 当前时间。

    Returns:
        ``(0, 1]`` 的得分。
    """
    age_days = (now - valid_from).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    return float(1.0 / (1.0 + age_days / _RECENCY_HALF_LIFE_DAYS))


def conflicting_ids(memories: Sequence[Memory]) -> frozenset[UUID]:
    """返回一组记忆中互相存在**显式冲突关系**的那些 id。

    供上层在回答里标注"这些记忆之间存在冲突"（任务书 §5.11）——
    冲突要被**呈现**，而不是被排序掩盖。

    ⚠️ 只看 ``contradicts_ids``（显式关系），**不看相似度**。
    相似度高不等于矛盾（见 :mod:`ai_psi.memory.conflict_detection`）：
    把"很像"标成"冲突"是在伪造一个判断。

    🔴 **标注是双向的。** 冲突是一对记忆之间的关系，但
    ``contradicts_ids`` 记在**其中一条**上。如果只标"记了这件事的那一条"，
    用户会在结果里看到一条被标为"有冲突"、却找不到冲突对象的记忆——
    而真正需要他注意的，恰恰是**另一条**。

    集合之外的记忆不算数：用户看不到那一条，标注只会让人困惑。

    Args:
        memories: 记忆集合（通常是同一次检索或列表的结果）。

    Returns:
        与集合内其他成员存在显式冲突关系的记忆 id（**两侧都包含**）。
    """
    ids = {memory.id for memory in memories}
    referenced = {
        target for memory in memories for target in memory.contradicts_ids if target in ids
    }
    return frozenset(
        memory.id
        for memory in memories
        if ids.intersection(memory.contradicts_ids) or memory.id in referenced
    )
