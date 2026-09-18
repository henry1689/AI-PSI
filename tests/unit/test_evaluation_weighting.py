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

import dataclasses

import pytest

from ai_psi.domain.enums import ExperienceEvaluation
from ai_psi.learning.evaluation_weighting import DEFAULT_WEIGHTING, EvaluationWeighting

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


class TestCountsTowardThreshold:
    """🔴 ``counts_toward_threshold`` 的**两个方向**都要钉住。

    ⚠️ 阶段 6.5 的变异测试发现：这里原先**只有反方向**的断言
    （``SUSPECTED`` / ``UNASSESSED`` 不算），因此
    ``return self.weights[evaluation] > 0`` 这一行被改成
    ``< 0``、``!= 0``、``> 1`` 时**三个变异体全部存活**——
    一个恒返回 ``False`` 的实现与真实现给出一样的测试结果。

    恒假的 ``counts_toward_threshold`` 意味着"任何经验都不计数"，
    系统会**永远不生成提案**——而它不会报任何错。
    """

    def test_supported_and_confirmed_do_count(self) -> None:
        """正方向：有权重的状态必须返回 ``True``。"""
        assert DEFAULT_WEIGHTING.counts_toward_threshold(ExperienceEvaluation.SUPPORTED) is True
        assert DEFAULT_WEIGHTING.counts_toward_threshold(ExperienceEvaluation.CONFIRMED) is True

    def test_zero_weight_does_not_count(self) -> None:
        """反方向：权重恰好为 0 时不算。"""
        assert DEFAULT_WEIGHTING.counts_toward_threshold(ExperienceEvaluation.UNASSESSED) is False
        assert DEFAULT_WEIGHTING.counts_toward_threshold(ExperienceEvaluation.SUSPECTED) is False

    def test_a_weight_above_one_still_counts(self) -> None:
        """🔴 权重 > 1 也算数——判据是"不等于 0"的**正半轴**。

        没有这条的话，``> 0`` 被改成 ``> 1`` 会存活，
        而那样的实现会让一个故意加权到 2 的状态**反而不计数**：
        越重视它、它越不影响门槛。
        """
        heavy = EvaluationWeighting(
            {
                ExperienceEvaluation.UNASSESSED: 0,
                ExperienceEvaluation.SUSPECTED: 0,
                ExperienceEvaluation.SUPPORTED: 1,
                ExperienceEvaluation.CONFIRMED: 9,
            }
        )
        assert heavy.counts_toward_threshold(ExperienceEvaluation.CONFIRMED) is True
        assert heavy.weight_of(ExperienceEvaluation.CONFIRMED) == 9


class TestTheWeightingIsAValueObject:
    """🔴 权重表必须是**不可变**的。

    变异测试发现 ``@dataclass(frozen=True)`` 被改成 ``frozen=False``
    时存活——即没有任何测试检查过这一点。

    一个可变的权重表意味着**门槛策略可以在运行期被就地改写**：
    某个请求路径只要拿到 ``DEFAULT_WEIGHTING`` 就能把 ``SUSPECTED``
    的权重从 0 改成 1，此后所有门槛判断都失效，且没有痕迹。
    """

    def test_it_cannot_be_mutated(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            DEFAULT_WEIGHTING.weights = {}  # type: ignore[misc]

    def test_it_has_no_instance_dict(self) -> None:
        """``slots=True``：多一个 ``__dict__`` 就多一条绕过冻结的路径。"""
        assert not hasattr(DEFAULT_WEIGHTING, "__dict__")

    def test_two_equal_weightings_compare_equal(self) -> None:
        """值相等即相等——否则"两份配置是否一致"这个问题无从回答。"""
        same = EvaluationWeighting(dict(DEFAULT_WEIGHTING.weights))
        assert same == DEFAULT_WEIGHTING


class TestNegativeWeightsAreRejected:
    """🔴 负权重必须被拒——而**边界值 −1** 尤其要紧。

    变异测试发现：`if weight < 0` 被改成 `if weight < -1` 时存活，
    因为原先的用例只用了一个**远低于边界**的负值（-5），
    而 `-5 < -1` 与 `-5 < 0` 都为真。

    权重 −1 放行的后果是：一个状态每出现一次就**倒扣一次**门槛。
    系统会得出"这类错误越少越好"的结论，而它不会报任何错——
    只会让某些模式**永远**凑不满门槛，且理由栏里写的是
    "发生 N 次，加权计数 −N，未达门槛"。
    """

    @pytest.mark.parametrize("weight", [-1, -2, -5])
    def test_a_negative_weight_is_rejected(self, weight: int) -> None:
        with pytest.raises(ValueError, match="不得为负"):
            EvaluationWeighting(
                {
                    ExperienceEvaluation.UNASSESSED: 0,
                    ExperienceEvaluation.SUSPECTED: weight,
                    ExperienceEvaluation.SUPPORTED: 1,
                    ExperienceEvaluation.CONFIRMED: 1,
                }
            )

    def test_zero_is_the_lower_bound_and_is_allowed(self) -> None:
        """边界值 0 **合法**——它是本文件主题的一半。

        ⚠️ 这条与上一条合起来才把边界钉死在 0：只测负值会让
        `< 0` 与 `<= 0` 无法区分，而后者会把整套设计（SUPECTED 权重 0）
        直接废掉。
        """
        assert DEFAULT_WEIGHTING.weight_of(ExperienceEvaluation.SUSPECTED) == 0
