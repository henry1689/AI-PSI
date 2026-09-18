"""评价状态如何折算成门槛计数（阶段 6.5 §二.6–7）。

🔴 **本模块存在的理由：让"内部怀疑"在结构上凑不满门槛。**

阶段 6 的门槛只数经验条数。而生产里能产出的经验，其评价状态
**永远**是内部元认知给出的 ``SUSPECTED``——用户纠正与后续证据
都在回合之后才到。于是"三次同类错误 → 提案"这条验收条件
在语义上等于"系统自己怀疑三次就能给自己开一张改进单"。

§二.6 与 §二.7 把这条堵死：``UNASSESSED`` 与 ``SUSPECTED`` 的
默认权重都是 **0**，只有 ``SUPPORTED`` / ``CONFIRMED`` 计 1。

⚠️ **不是"把 SUSPECTED 判为不成立"。** 它仍然被记录、仍然出现在
报告里的"为什么没达门槛"那一段。差别在于：一条 ``SUSPECTED``
经验会告诉你"这里可能有问题"，但它**不能**单独支撑一个提案。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from ai_psi.domain.enums import ExperienceEvaluation

__all__ = ["DEFAULT_WEIGHTING", "EvaluationWeighting"]


@dataclass(frozen=True, slots=True)
class EvaluationWeighting:
    """评价状态 → 门槛计数权重。

    🔴 **权重必须与次序一致（非递减）。**

    允许 ``CONFIRMED`` 比 ``SUSPECTED`` 权重还低是没有意义的配置，
    而它一旦出现，症状是"越确凿的经验越不容易触发提案"——
    一个不会报错、只会让系统悄悄变盲的配置。构造时就拒掉它。

    Attributes:
        weights: 每个评价状态对应的整数权重。
    """

    weights: Mapping[ExperienceEvaluation, int]

    def __post_init__(self) -> None:
        """校验权重表完整且单调。

        Raises:
            ValueError: 缺少某个评价状态，或权重随次序下降。
        """
        missing = sorted(
            item.value for item in ExperienceEvaluation if item not in self.weights
        )
        if missing:
            msg = (
                f"权重表缺少评价状态：{'、'.join(missing)}。"
                "漏掉一个状态的后果是它按 0 计——而那是**无声**的"
            )
            raise ValueError(msg)

        ordered = sorted(ExperienceEvaluation, key=lambda item: item.rank)
        previous: int | None = None
        for evaluation in ordered:
            weight = self.weights[evaluation]
            if weight < 0:
                msg = f"{evaluation.value} 的权重不得为负：{weight}"
                raise ValueError(msg)
            if previous is not None and weight < previous:
                msg = (
                    f"权重随评价次序下降：{evaluation.value} 为 {weight}，"
                    f"低于它前一档的 {previous}。"
                    "这会让越确凿的经验越不容易触发提案"
                )
                raise ValueError(msg)
            previous = weight

    def weight_of(self, evaluation: ExperienceEvaluation) -> int:
        """取某个评价状态的计数权重。"""
        return self.weights[evaluation]

    def counts_toward_threshold(self, evaluation: ExperienceEvaluation) -> bool:
        """该状态的经验能否**单独**为门槛做出贡献。

        ⚠️ 权重为 0 的状态不是"不成立"，是"不足以支撑提案"。
        两者的区别见 :mod:`ai_psi.learning.promotion_policy` 对
        ``None`` 与 ``False`` 的处理。
        """
        return self.weights[evaluation] > 0


#: 默认权重表。
#:
#: 🔴 **``UNASSESSED`` 与 ``SUSPECTED`` 都是 0，这是本节的核心决定。**
#:
#: * ``UNASSESSED``——没有任何评估者看过它。它连"这里可能有问题"
#:   都没说，只是被记录下来了。
#: * ``SUSPECTED``——只有内部元认知怀疑过。**系统的怀疑不是证据**；
#:   让它计数，等于允许系统用自己的假设给自己发通行证。
#: * ``SUPPORTED`` / ``CONFIRMED``——有系统之外的东西支持
#:   （用户纠正、后续证据、独立评测）。只有它们计数。
#:
#: 两条被钉死的推论（`tests/unit/test_evaluation_weighting.py`）：
#: 任意数量的纯 ``SUSPECTED`` 凑不满任何 ≥2 的门槛；
#: ``SUSPECTED`` 与 ``SUPPORTED`` 的权重**不相等**。
#:
#: ⚠️ 改动它需要同时改 ADR-0020 与上面的测试——这是有意的摩擦。
DEFAULT_WEIGHTING: Final[EvaluationWeighting] = EvaluationWeighting(
    MappingProxyType(
        {
            ExperienceEvaluation.UNASSESSED: 0,
            ExperienceEvaluation.SUSPECTED: 0,
            ExperienceEvaluation.SUPPORTED: 1,
            ExperienceEvaluation.CONFIRMED: 1,
        }
    )
)
