"""置信度上限的计算与钳制。

🔴 **置信度的上限由代码算，不由模型决定。**

任务书不变量 3 要求"存在高可信冲突时，不得输出无保留的确定结论"，
不变量 7 要求"最终回答不能比内部判断更确定"。
两条都指向同一件事：**结论强度必须被外部约束**。

如果让模型同时负责"下结论"和"决定自己有多确定"，
这两条不变量就变成了自我承诺。因此这里的做法是：

1. :func:`derive_ceiling` 依据**证据结构**（数量、独立性、冲突、未解未知）
   计算一个上限；
2. :func:`clamp` 把模型给出的档位**下调**到不超上限（**绝不上调**）。

模型给出比上限更高的置信度不会被拒绝，而是被改写并留痕——
拒绝会让回合失败，改写既保住了不变量，也保住了用户的回答。
"""

from __future__ import annotations

from ai_psi.domain.enums import ConfidenceBand

__all__ = ["clamp", "derive_ceiling", "lower_band"]

_ORDER: tuple[ConfidenceBand, ...] = (
    ConfidenceBand.VERY_LOW,
    ConfidenceBand.LOW,
    ConfidenceBand.MODERATE,
    ConfidenceBand.HIGH,
    ConfidenceBand.VERY_HIGH,
)


def lower_band(left: ConfidenceBand, right: ConfidenceBand) -> ConfidenceBand:
    """返回两个档位中**较低**的一个。"""
    return left if left.rank <= right.rank else right


def clamp(proposed: ConfidenceBand, ceiling: ConfidenceBand) -> ConfidenceBand:
    """把提议档位钳制到不超过上限。

    Args:
        proposed: 模型给出的档位。
        ceiling: 代码计算的上限。

    Returns:
        ``min(proposed, ceiling)``。
    """
    return lower_band(proposed, ceiling)


def derive_ceiling(
    *,
    evidence_count: int,
    independent_source_count: int,
    has_high_trust_conflict: bool,
    unresolved_unknown_count: int,
) -> ConfidenceBand:
    """依据证据结构计算置信度上限。

    规则（顺序无关，取最严）：

    | 条件 | 上限 |
    |---|---|
    | 完全没有证据 | ``LOW`` |
    | 只有单一来源（含全部证据同源） | ``MODERATE`` |
    | 存在高可信冲突 | ``LOW`` |
    | 存在未解决未知 | ``MODERATE`` |
    | 高可信冲突 **且** 有未解未知 | ``VERY_LOW`` |

    默认上限是 ``HIGH``，不是 ``VERY_HIGH``——
    ``VERY_HIGH`` 保留给经过独立核验的多源一致证据，
    V0.1 中没有任何路径能自动达到它（不变量 4：用户赞同不能提升核验状态）。

    Args:
        evidence_count: 证据条数。
        independent_source_count: 其中的**独立来源**组数（同源只算一组）。
        has_high_trust_conflict: 高可信材料之间是否存在冲突。
        unresolved_unknown_count: 未解决未知的条数。

    Returns:
        置信度上限。
    """
    ceiling = ConfidenceBand.HIGH

    if evidence_count <= 0:
        ceiling = lower_band(ceiling, ConfidenceBand.LOW)
    elif independent_source_count <= 1:
        ceiling = lower_band(ceiling, ConfidenceBand.MODERATE)

    if has_high_trust_conflict:
        ceiling = lower_band(ceiling, ConfidenceBand.LOW)

    if unresolved_unknown_count > 0:
        ceiling = lower_band(ceiling, ConfidenceBand.MODERATE)

    if has_high_trust_conflict and unresolved_unknown_count > 0:
        ceiling = lower_band(ceiling, ConfidenceBand.VERY_LOW)

    return ceiling
