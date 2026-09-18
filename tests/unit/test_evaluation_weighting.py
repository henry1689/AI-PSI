"""评价权重表（阶段 6.5 §二.6–7）。

🔴 **本文件是对**默认权重**的钉子，不是对权重机制的测试。**

机制（缺项、负值、单调性、按表取值）在
:mod:`tests.unit.test_pattern_detector` 的 ``TestEvaluationWeighting``
里覆盖。这里只钉一件事：**当前生效的那张表长什么样。**

钉死它的理由：把 ``SUSPECTED`` 从 0 改成 1 是**一行代码的改动**，
而它的后果是"系统自己的怀疑可以自我确认成规律"这条性质
无声地消失——没有任何测试会自然变红，因为所有组件仍然自洽。
这条测试让那次改动必须先改一个显式的断言，从而必须被看见。
"""

from __future__ import annotations

import pytest

from ai_psi.domain.enums import ExperienceEvaluation
from ai_psi.learning.evaluation_weighting import DEFAULT_WEIGHTING

pytestmark = pytest.mark.unit


class TestDefaultWeightingIsPinned:
    def test_unassessed_does_not_count(self) -> None:
        """§二.6：没有任何评估者看过它 → 不计入门槛。"""
        assert DEFAULT_WEIGHTING.weight_of(ExperienceEvaluation.UNASSESSED) == 0

    def test_suspected_does_not_count(self) -> None:
        """🔴 §二.7 的核心：**内部元认知的怀疑不是证据。**

        把这里改成非 0 的后果是：三条同类的内部怀疑就能生成一条提案，
        而提案的去向是**被人认真评估**。用系统自己的假设去占用
        评审的时间，比不生成提案更糟。
        """
        assert DEFAULT_WEIGHTING.weight_of(ExperienceEvaluation.SUSPECTED) == 0

    def test_supported_and_confirmed_count_once_each(self) -> None:
        """有系统之外的证据支持 → 计 1。

        两档**同权**是有意的：``SUPPORTED`` 与 ``CONFIRMED`` 的区别
        是"依据的类型不同"（后续证据 vs 用户直接指出），不是
        "一个算半个"。把用户的话打对折，会让"用户纠正三次才够"
        这种换算变成一条隐形的规则。
        """
        assert DEFAULT_WEIGHTING.weight_of(ExperienceEvaluation.SUPPORTED) == 1
        assert DEFAULT_WEIGHTING.weight_of(ExperienceEvaluation.CONFIRMED) == 1

    def test_the_table_covers_exactly_the_enum(self) -> None:
        """权重表的键集合必须恰好等于枚举——多一个少一个都是漂移。"""
        assert set(DEFAULT_WEIGHTING.weights) == set(ExperienceEvaluation)

    def test_suspected_never_counts_toward_the_threshold(self) -> None:
        """按默认表，"能不能计数"这个问题对 ``SUSPECTED`` 的答案是**不能**。"""
        assert not DEFAULT_WEIGHTING.counts_toward_threshold(ExperienceEvaluation.SUSPECTED)
        assert not DEFAULT_WEIGHTING.counts_toward_threshold(ExperienceEvaluation.UNASSESSED)

    def test_no_quantity_of_suspicion_reaches_a_threshold_of_two(self) -> None:
        """🔴 任意条数纯 ``SUSPECTED`` 都凑不满**任何** ≥2 的门槛。

        这条是 §七.4 要的黑盒结论在单元层的对应物：
        权重为 0 时，计数与条数**无关**。
        """
        for count in (2, 3, 50, 10_000):
            total = count * DEFAULT_WEIGHTING.weight_of(ExperienceEvaluation.SUSPECTED)
            assert total < 2
