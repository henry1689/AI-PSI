"""模式发现（任务书 §11.3 第一条，阶段 6.5 §二.6–7、§二.12）。

🔴 本文件有两个重点，且它们是**分开**的：

1. **"同类"的定义**——模式发现把 N 条经验说成"同一类错误发生了 N 次"。
   这个"同一类"如果没有一个可比较的定义，门槛就无从判定。
2. **"几次"的定义**——计数单位是**独立发生次数 × 评价权重**。
   同一回合被重建、同一请求被重试、同一段事件被重放，
   都不该算成"又发生了一次"；内部元认知的怀疑也不算。

第 2 条在阶段 6.5 之前完全不存在。当时的门槛只数经验对象条数，
于是"系统自己怀疑三次"就能生成一条提案——**系统用自己的假设
给自己发了通行证**。

⚠️ 本文件里的经验**一律按 ``CONFIRMED`` 评价**参与计数。
那是为了把"分组与去重"的机制与"权重策略"分开测：
权重的行为在 :class:`TestEvaluationWeighting` 里单独覆盖，
把它混进每一个用例会让"模式没被发现"永远有两种解释。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from ai_psi.domain.enums import ConfidenceBand, ErrorType, ExperienceEvaluation
from ai_psi.domain.experiences import Experience, ExperienceAssessment
from ai_psi.learning.evaluation_weighting import DEFAULT_WEIGHTING, EvaluationWeighting
from ai_psi.learning.pattern_detector import (
    ErrorPattern,
    PatternDetector,
    PatternScan,
    SuppressedPattern,
)

pytestmark = pytest.mark.unit

#: ``tests/conftest.py`` 的 ``make_experience`` 夹具形状。
Factory = Callable[..., Any]


def _experience(make_experience: Factory, **overrides: object) -> Experience:
    """构造一条**可归因**的经验。默认置信度必须够高，否则会被过滤掉。"""
    payload: dict[str, object] = {
        "error_type": ErrorType.REASONING_ERROR,
        "attribution_confidence": ConfidenceBand.MODERATE,
        "situation_signature": "d1|with_evidence|h2",
    }
    payload.update(overrides)
    experience: Experience = make_experience(**payload)
    return experience


def _assessed(
    experiences: Sequence[Experience],
    *,
    evaluation: ExperienceEvaluation | None = ExperienceEvaluation.CONFIRMED,
) -> list[ExperienceAssessment]:
    """把经验包成"已被外部证据确认过"的评估结果。

    🔴 抽取时刻的评价**不可能**是 ``CONFIRMED``（§二.4 的结构性约束），
    因此这里模拟的是"用户纠正之后"的状态——那才是门槛真正面对的输入。

    ``evaluation=None`` 时保留经验自带的（抽取时刻的）评价，
    用于测权重策略本身。
    """
    return [
        ExperienceAssessment(
            experience=item,
            evaluation=item.evaluation if evaluation is None else evaluation,
        )
        for item in experiences
    ]


def _repeated(
    make_experience: Factory, count: int, **overrides: object
) -> list[ExperienceAssessment]:
    return _assessed([_experience(make_experience, **overrides) for _ in range(count)])


@pytest.fixture
def detector() -> PatternDetector:
    return PatternDetector()


class TestThreshold:
    def test_below_threshold_is_not_a_pattern(self, detector, make_experience) -> None:
        """🔴 不变量 10 的第一道落地：两条经验不构成模式。"""
        assert detector.detect(_repeated(make_experience, 2)).patterns == ()

    def test_threshold_is_inclusive(self, detector, make_experience) -> None:
        patterns = detector.detect(_repeated(make_experience, 3)).patterns
        assert len(patterns) == 1
        assert patterns[0].count == 3

    def test_empty_input_yields_nothing(self, detector) -> None:
        assert detector.detect([]).patterns == ()

    def test_threshold_below_two_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="不变量 10"):
            PatternDetector(threshold=1)

    def test_threshold_is_configurable_upward(self, make_experience) -> None:
        strict = PatternDetector(threshold=4)
        assert strict.detect(_repeated(make_experience, 3)).patterns == ()
        assert strict.threshold == 4


class TestWhatCountsAsTheSameKind:
    """🔴 同类 = （错误类别, 情境签名）。两者缺一不可。"""

    def test_different_signature_is_a_different_pattern(self, detector, make_experience) -> None:
        """只看类别的后果：两类问题里各错两次会被并成"错了四次"。"""
        experiences = _repeated(make_experience, 2, situation_signature="d1|with_evidence|h2")
        experiences += _repeated(make_experience, 2, situation_signature="d3|no_evidence|h0")
        assert detector.detect(experiences).patterns == ()

    def test_different_error_type_is_a_different_pattern(self, detector, make_experience) -> None:
        """只看情境的后果：同一类问题里的不同错误会被并成"这类问题老出错"。"""
        experiences = _repeated(make_experience, 2, error_type=ErrorType.SCOPE_ERROR)
        experiences += _repeated(make_experience, 2, error_type=ErrorType.EVIDENCE_ERROR)
        assert detector.detect(experiences).patterns == ()

    def test_both_dimensions_must_match(self, detector, make_experience) -> None:
        experiences = _repeated(make_experience, 2, error_type=ErrorType.SCOPE_ERROR)
        experiences += _repeated(
            make_experience,
            2,
            error_type=ErrorType.SCOPE_ERROR,
            situation_signature="另一个情境",
        )
        experiences += _repeated(make_experience, 1, error_type=ErrorType.EVIDENCE_ERROR)
        assert detector.detect(experiences).patterns == ()


class TestUnattributableExperiencesAreExcluded:
    """🔴 "不知道错在哪"与"没有错"是两回事。"""

    def test_missing_error_type_does_not_count(self, detector, make_experience) -> None:
        unattributed = _assessed([_experience(make_experience, error_type=None) for _ in range(3)])
        scan = detector.detect(unattributed)
        assert scan.patterns == ()
        # 🔴 而且要**报数**——"一条都没读到"与"三条都判不了"不一样
        assert scan.unattributable_count == 3

    def test_unattributed_experiences_do_not_pad_a_real_pattern(
        self, detector, make_experience
    ) -> None:
        """两条已归因 + 三条未归因 ≠ 五条同类错误。"""
        experiences = _repeated(make_experience, 2)
        experiences += _assessed([_experience(make_experience, error_type=None) for _ in range(3)])
        assert detector.detect(experiences).patterns == ()

    def test_very_low_confidence_is_excluded(self, detector, make_experience) -> None:
        """含糊的猜测凑不成提案——提案是**会被人认真评估**的东西。"""
        shaky = _repeated(make_experience, 3, attribution_confidence=ConfidenceBand.VERY_LOW)
        scan = detector.detect(shaky)
        assert scan.patterns == ()
        assert scan.low_confidence_count == 3

    def test_low_confidence_still_counts(self, detector, make_experience) -> None:
        """LOW 是**可用的下界**，边界值要落在能用的那一侧。"""
        patterns = detector.detect(
            _repeated(make_experience, 3, attribution_confidence=ConfidenceBand.LOW)
        ).patterns
        assert len(patterns) == 1


class TestOccurrenceAccounting:
    """🔴 门槛的计量单位是**"这件事在不同回合里发生过几次"**。

    阶段 6 的初版按 ``Experience.id`` 去重，而 ``Experience.id`` 是
    每次 ``build()`` 新生成的 ``uuid4``——同一个回合构建三次就是
    三条"独立经验"，**一次错误足以凑满三次的门槛**。

    阶段 6.5 把去重键下沉到 ``Experience.independence_group``：
    它由**幂等键或回合 id** 决定，两者都是"发生"的事实，
    而不是"创建"这个动作。
    """

    def test_duplicate_objects_do_not_inflate_the_count(self, detector, make_experience) -> None:
        """同一条经验被重复传入不算两次——否则门槛可以被同一份证据凑满。"""
        experience = _experience(make_experience)
        assert detector.detect(_assessed([experience, experience, experience])).patterns == ()

    def test_the_same_round_rebuilt_is_still_one_occurrence(
        self, detector, make_experience
    ) -> None:
        """🔴 **同一个回合的同一个判断，构建几次都只算一次。**

        这条与上一条看着像，实际差着一个 ``uuid4()``：
        上一条传的是**同一个对象**，这条传的是**同一回合构建出的三个不同对象**。
        后者才是真实会发生的重复（重复消费事件、重跑构建、补数据……）。
        """
        round_id, judgment_id = uuid4(), uuid4()
        rebuilt = [
            _experience(make_experience, cognitive_round_id=round_id, judgment_id=judgment_id)
            for _ in range(3)
        ]
        # 三个对象的 id 互不相同——按 id 去重的话它们会被算成三次
        assert len({item.id for item in rebuilt}) == 3
        assert detector.detect(_assessed(rebuilt)).patterns == ()

    def test_distinct_rounds_do_count(self, detector, make_experience) -> None:
        """不同回合的同类错误仍然正常计数——去重不该把真实重复一起抹掉。"""
        occurrences = [
            _experience(make_experience, cognitive_round_id=uuid4(), judgment_id=uuid4())
            for _ in range(3)
        ]
        patterns = detector.detect(_assessed(occurrences)).patterns
        assert len(patterns) == 1
        assert patterns[0].count == 3

    def test_technical_retries_share_an_independence_group(self, detector, make_experience) -> None:
        """🔴 §二.12：同一 ``Idempotency-Key`` 的**技术重试**只算一次发生。

        这是 ``independence_group`` 存在的直接理由：三个回合的
        ``cognitive_round_id`` 互不相同（网络抖一下，客户端重发，
        服务端确实盖了三个回合），但它们是**同一次请求**。
        """
        key = "client-retry-key"
        retried = [
            _experience(
                make_experience,
                cognitive_round_id=uuid4(),
                judgment_id=uuid4(),
                independence_group=f"idem:{key}",
            )
            for _ in range(3)
        ]
        assert len({item.cognitive_round_id for item in retried}) == 3
        scan = detector.detect(_assessed(retried))
        assert scan.patterns == ()
        # 冗余是可见的：三条经验、一次发生
        assert scan.suppressed[0].experience_count == 1

    def test_same_round_different_judgments_are_one_occurrence(
        self, detector, make_experience
    ) -> None:
        """⚠️ **本条的结论在阶段 6.5 反转了，这是有意的。**

        阶段 6 认为同一回合里的不同判断"确实是不同的认知产物、
        各自算一次"。这句话没错，但它们不是**独立**的：同一个回合
        共享同一份证据、同一次模型调用、同一段推理上下文。
        用它们凑"三次发生"是阶段 6 那个 bug 的弱化版，而不是它的反面。

        现在它们落在同一个 ``independence_group``（那个回合）里，
        因此塌缩成一次。
        """
        round_id = uuid4()
        within_one_round = [
            _experience(make_experience, cognitive_round_id=round_id, judgment_id=uuid4())
            for _ in range(3)
        ]
        assert len({item.judgment_id for item in within_one_round}) == 3
        assert detector.detect(_assessed(within_one_round)).patterns == ()

    def test_three_rebuilds_do_not_reach_the_threshold(self, detector, make_experience) -> None:
        """上面的组合版：两次真实发生 + 一次重建 = **两次**，不够门槛。"""
        shared = [(uuid4(), uuid4()), (uuid4(), uuid4())]
        occurrences: list[Experience] = []
        for round_id, judgment_id in shared:
            occurrences += [
                _experience(make_experience, cognitive_round_id=round_id, judgment_id=judgment_id)
                for _ in range(3)
            ]
        assert detector.detect(_assessed(occurrences)).patterns == ()


class TestEvaluationWeighting:
    """🔴 阶段 6.5 §二.6–7：内部怀疑不是证据。"""

    def test_unassessed_does_not_count(self, detector, make_experience) -> None:
        """§二.6：``UNASSESSED`` 默认不计入门槛。"""
        experiences = [_experience(make_experience, error_type=None) for _ in range(5)]
        assert detector.detect(_assessed(experiences, evaluation=None)).patterns == ()

    def test_suspected_alone_never_reaches_threshold(self, detector, make_experience) -> None:
        """🔴 §二.7：**任意数量**纯 ``SUSPECTED`` 都凑不满门槛。

        这是本节的核心断言。默认权重下它是 0——不是"打折"，
        而是"不计"。打折会让"怀疑 6 次 = 证据 3 次"这种换算
        变成一条可以被调参绕开的口子。
        """
        many = _assessed(
            [_experience(make_experience) for _ in range(50)],
            evaluation=ExperienceEvaluation.SUSPECTED,
        )
        scan = detector.detect(many)
        assert scan.patterns == ()
        assert scan.suppressed[0].occurrence_count == 50
        assert scan.suppressed[0].weighted_count == 0

    def test_supported_counts(self, detector, make_experience) -> None:
        experiences = _assessed(
            [_experience(make_experience) for _ in range(3)],
            evaluation=ExperienceEvaluation.SUPPORTED,
        )
        patterns = detector.detect(experiences).patterns
        assert len(patterns) == 1
        assert patterns[0].weighted_count == 3

    def test_confirmed_counts_like_supported(self, detector, make_experience) -> None:
        """两档都计 1——它们都是"系统之外的证据"，只在理由上区分。"""
        experiences = _assessed(
            [_experience(make_experience) for _ in range(3)],
            evaluation=ExperienceEvaluation.CONFIRMED,
        )
        assert detector.detect(experiences).patterns[0].weighted_count == 3

    def test_suspected_and_supported_do_not_have_equal_weight(
        self, detector, make_experience
    ) -> None:
        """🔴 §二.7 的字面要求：suspected 与 supported **不等权**。"""
        weighting = detector.weighting
        assert weighting.weight_of(ExperienceEvaluation.SUSPECTED) != weighting.weight_of(
            ExperienceEvaluation.SUPPORTED
        )

    def test_a_custom_weighting_can_make_suspected_count(self, make_experience) -> None:
        """权重是**策略**，不是常量——放宽它是可能的，但必须显式。

        这条用例证明的是"权重表真的在起作用"，而不是"它可以随便改"：
        改它需要同时改 ADR-0020 与 ``test_evaluation_weighting.py``。
        """
        permissive = EvaluationWeighting(
            {
                ExperienceEvaluation.UNASSESSED: 0,
                ExperienceEvaluation.SUSPECTED: 1,
                ExperienceEvaluation.SUPPORTED: 1,
                ExperienceEvaluation.CONFIRMED: 1,
            }
        )
        detector = PatternDetector(weighting=permissive)
        many = _assessed([_experience(make_experience) for _ in range(3)])
        assert len(detector.detect(many).patterns) == 1

    def test_suppression_says_it_was_the_weight(self, detector, make_experience) -> None:
        """🔴 被权重挡下时必须**说清**是权重，不是"经验不够多"。

        否则外部看起来与"历史上压根没这些经验"一模一样——
        而这两件事该导致完全不同的下一步动作。
        """
        suspected = _assessed(
            [_experience(make_experience) for _ in range(5)],
            evaluation=ExperienceEvaluation.SUSPECTED,
        )
        reasons = [
            reason for item in detector.detect(suspected).suppressed for reason in item.reasons
        ]
        assert any("权重" in reason for reason in reasons), reasons
        assert any("supported" in reason for reason in reasons), reasons

    def test_weighting_must_be_monotone(self) -> None:
        """权重随评价次序下降是无意义的配置，构造时就拒掉。"""
        with pytest.raises(ValueError, match="下降"):
            EvaluationWeighting(
                {
                    ExperienceEvaluation.UNASSESSED: 0,
                    ExperienceEvaluation.SUSPECTED: 2,
                    ExperienceEvaluation.SUPPORTED: 1,
                    ExperienceEvaluation.CONFIRMED: 1,
                }
            )

    def test_weighting_must_cover_every_state(self) -> None:
        """漏掉一个状态会让它按 0 计——而那是**无声**的。"""
        with pytest.raises(ValueError, match="缺少评价状态"):
            EvaluationWeighting({ExperienceEvaluation.SUPPORTED: 1})


class TestPatternContent:
    def test_experience_ids_are_sorted(self, detector, make_experience) -> None:
        patterns = detector.detect(_repeated(make_experience, 3)).patterns
        ids = patterns[0].experience_ids
        assert list(ids) == sorted(ids, key=str)

    def test_counterexamples_are_counted(self, detector, make_experience) -> None:
        experiences = _repeated(make_experience, 2, counterexamples=["反例"])
        experiences += _repeated(make_experience, 1)
        pattern = detector.detect(experiences).patterns[0]
        assert pattern.counterexample_count == 2

    def test_conditions_are_merged_deduplicated_and_sorted(self, detector, make_experience) -> None:
        experiences = _repeated(
            make_experience, 1, applicable_conditions=["仅限单来源", "中文语料"]
        )
        experiences += _repeated(make_experience, 2, applicable_conditions=["仅限单来源"])
        pattern = detector.detect(experiences).patterns[0]
        assert pattern.applicable_conditions == ("中文语料", "仅限单来源")

    def test_first_and_last_round_follow_time_not_id(self, detector, make_experience) -> None:
        """🔴 ``first``/``last`` 是**时间上**的最早与最晚，不是 id 的最小与最大。

        初版按经验 id 排序（``sorted(unique, key=str)``），而 id 是 uuid4——
        所谓"最早/最晚"实际是随机顺序。下游用 ``last_round_id``
        判断"这个问题还活着吗"会取到错误的回合。
        """
        base = datetime(2026, 3, 1, tzinfo=UTC)
        rounds = [uuid4() for _ in range(3)]
        occurrences = [
            _experience(
                make_experience,
                cognitive_round_id=round_id,
                created_at=base + timedelta(hours=index),
            )
            for index, round_id in enumerate(rounds)
        ]

        pattern = detector.detect(_assessed(list(reversed(occurrences)))).patterns[0]
        assert pattern.first_round_id == rounds[0]
        assert pattern.last_round_id == rounds[-1]

    def test_first_and_last_are_always_populated(self, detector, make_experience) -> None:
        patterns = detector.detect(_repeated(make_experience, 3)).patterns
        assert patterns[0].first_round_id is not None
        assert patterns[0].last_round_id is not None

    def test_pattern_count_matches_members(self, detector, make_experience) -> None:
        pattern = detector.detect(_repeated(make_experience, 4)).patterns[0]
        assert pattern.count == len(pattern.experience_ids) == 4

    def test_evaluations_are_reported(self, detector, make_experience) -> None:
        """模式要能回答"它是由什么档位的证据支持的"。"""
        experiences = _assessed(
            [_experience(make_experience) for _ in range(3)],
            evaluation=ExperienceEvaluation.SUPPORTED,
        )
        assert detector.detect(experiences).patterns[0].evaluations == (
            ExperienceEvaluation.SUPPORTED,
        )

    def test_pattern_is_frozen(self, detector, make_experience) -> None:
        pattern = detector.detect(_repeated(make_experience, 3)).patterns[0]
        assert isinstance(pattern, ErrorPattern)
        with pytest.raises(dataclasses.FrozenInstanceError):
            pattern.error_type = ErrorType.FACTUAL_ERROR  # type: ignore[misc]


class TestSuppressedPatternsAreReported:
    """🔴 只说"发现 0 个模式"的报告无法回答"为什么没有"。"""

    def test_members_below_threshold_are_reported(self, detector, make_experience) -> None:
        scan = detector.detect(_repeated(make_experience, 2))
        assert scan.patterns == ()
        assert len(scan.suppressed) == 1
        assert scan.suppressed[0].occurrence_count == 2
        assert scan.suppressed[0].error_type is ErrorType.REASONING_ERROR

    def test_a_group_with_no_samples_is_not_reported(self, detector) -> None:
        """没有输入就没有分组——"没观测到"不该被写成"有一条被压制了"。"""
        assert detector.detect([]).suppressed == ()

    def test_qualified_and_suppressed_coexist(self, detector, make_experience) -> None:
        experiences = _repeated(make_experience, 3, situation_signature="甲")
        experiences += _repeated(make_experience, 2, situation_signature="乙")
        scan = detector.detect(experiences)
        assert [item.situation_signature for item in scan.patterns] == ["甲"]
        assert [item.situation_signature for item in scan.suppressed] == ["乙"]


class TestDeterministicOrdering:
    """🔴 同一批经验在任何一次运行里都必须得到同样的顺序。

    否则"这次跑出三个提案、下次跑出两个"会变成常态，
    而没有人能解释为什么。
    """

    def test_input_order_does_not_matter(self, detector, make_experience) -> None:
        experiences = _repeated(make_experience, 3)
        forwards = detector.detect(experiences).patterns
        backwards = detector.detect(list(reversed(experiences))).patterns
        assert [item.experience_ids for item in forwards] == [
            item.experience_ids for item in backwards
        ]

    def test_more_frequent_patterns_come_first(self, detector, make_experience) -> None:
        experiences = _repeated(make_experience, 3, situation_signature="少")
        experiences += _repeated(make_experience, 5, situation_signature="多")
        patterns = detector.detect(experiences).patterns
        assert [item.count for item in patterns] == [5, 3]

    def test_ties_are_broken_deterministically(self, detector, make_experience) -> None:
        experiences = _repeated(make_experience, 3, situation_signature="sig-b")
        experiences += _repeated(make_experience, 3, situation_signature="sig-a")
        patterns = detector.detect(experiences).patterns
        assert [item.situation_signature for item in patterns] == ["sig-a", "sig-b"]

    def test_repeated_runs_agree(self, detector, make_experience) -> None:
        experiences = _repeated(make_experience, 3, situation_signature="甲")
        experiences += _repeated(
            make_experience, 3, error_type=ErrorType.SCOPE_ERROR, situation_signature="乙"
        )
        first = [
            (item.error_type, item.situation_signature)
            for item in detector.detect(experiences).patterns
        ]
        second = [
            (item.error_type, item.situation_signature)
            for item in detector.detect(experiences).patterns
        ]
        assert first == second

    def test_frequency_beats_the_signature_order(self, detector, make_experience) -> None:
        """🔴 变异测试发现：排序键里的**第一项**可以被拿掉而无人察觉。

        已有用例的"更频繁的在前"用的是「少」（3 次）与「多」（5 次），
        而这两个字在字典序上恰好也是「多」在前——于是把
        ``-weighted_count`` 换成 ``not weighted_count``（全部并列）
        之后，回退到第二、三项排序，答案**碰巧**仍然是对的。

        这里让次数与字典序**相反**：次数多的签名排在后面。
        排序键的第一项一旦失效，答案就会翻过来。
        """
        experiences = _repeated(make_experience, 5, situation_signature="zzz-更多次")
        experiences += _repeated(make_experience, 3, situation_signature="aaa-更少次")
        patterns = detector.detect(experiences).patterns
        assert [item.count for item in patterns] == [5, 3]

    def test_suppressed_are_sorted_by_the_same_rule(self, make_experience) -> None:
        """🔴 变异测试发现：**``suppressed`` 的排序从来没有任何断言。**

        ``patterns.sort(...)`` 与 ``suppressed.sort(...)`` 用的是同一个
        键表达式，但只有前者被验证过。把后者的 ``-`` 去掉、
        换成 ``+`` 或 ``not``，全部存活。

        未达门槛的分组同样要按加权计数降序——"为什么没有提案"那张
        清单是可交付物的一部分。签名与次数**故意交叉**（次数多的
        排在字典序后面），否则排序键的第一项失效时它会碰巧还对。
        """
        strict = PatternDetector(threshold=10)
        # ⚠️ 错误类别也要**交叉**：只让签名与次数相反还不够——
        #    并列之后回退到「错误类别 → 情境签名」，而
        #    "reasoning_error" 恰好排在 "scope_error" 前面，
        #    于是"次数多的在前"仍然碰巧成立。两次都必须交叉。
        experiences = _repeated(
            make_experience, 4, error_type=ErrorType.SCOPE_ERROR, situation_signature="zzz-更多次"
        )
        experiences += _repeated(make_experience, 2, situation_signature="aaa-更少次")

        scan = strict.detect(experiences)

        assert scan.patterns == ()
        assert [item.experience_count for item in scan.suppressed] == [4, 2]

    def test_suppressed_ties_are_broken_deterministically(self, detector, make_experience) -> None:
        """同一批经验总得到同样的顺序——``suppressed`` 也不例外。"""
        experiences = _repeated(make_experience, 2, situation_signature="sig-b")
        experiences += _repeated(make_experience, 2, situation_signature="sig-a")
        scan = detector.detect(experiences)
        assert [item.situation_signature for item in scan.suppressed] == ["sig-a", "sig-b"]


class TestTheCountingFieldsDefaultToNothing:
    """🔴 变异测试发现：三张结果对象的**计数字段默认值**从未被断言。

    ``occurrence_count: int = 0`` 之类改成 ``1`` 或 ``-1`` 全部存活，
    因为 ``detect`` 与既有的测试 helper 都**显式传了**这些字段——
    默认值一次都没被用到。

    ⚠️ "没被用到"不等于"不重要"：默认值 1 意味着任何一处新的构造
    （比如以后加一个只报错误类别的最小投影）都会凭空带上一次发生。
    ``weighted_count`` 尤其危险——它是门槛比的数。
    """

    def test_an_empty_error_pattern_counts_nothing(self) -> None:
        pattern = ErrorPattern(error_type=ErrorType.REASONING_ERROR, situation_signature="sig")
        assert pattern.occurrence_count == 0
        assert pattern.weighted_count == 0
        assert pattern.experience_count == 0
        assert pattern.counterexample_count == 0
        assert pattern.experience_ids == ()
        assert pattern.first_round_id is None
        assert pattern.last_round_id is None

    def test_an_empty_scan_counts_nothing(self) -> None:
        scan = PatternScan()
        assert scan.patterns == ()
        assert scan.suppressed == ()
        assert scan.unattributable_count == 0
        assert scan.low_confidence_count == 0


class TestTheResultsAreImmutable:
    """🔴 变异测试发现：``frozen=True`` 改成 ``False`` 之后有两条存活。

    这三张对象是**一份结论**：调用方拿到之后再改它的
    ``weighted_count``，等于让"门槛放行了它"与"它当时算出来几"分家，
    而报告仍然按改过的数生成。
    """

    def test_error_pattern_cannot_be_mutated(self) -> None:
        pattern = ErrorPattern(error_type=ErrorType.REASONING_ERROR, situation_signature="sig")
        with pytest.raises(dataclasses.FrozenInstanceError):
            pattern.weighted_count = 99  # type: ignore[misc]

    def test_suppressed_pattern_cannot_be_mutated(self) -> None:
        pattern = SuppressedPattern(
            error_type=ErrorType.REASONING_ERROR,
            situation_signature="sig",
            occurrence_count=1,
            weighted_count=0,
            experience_count=1,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            pattern.weighted_count = 99  # type: ignore[misc]

    def test_pattern_scan_cannot_be_mutated(self) -> None:
        scan = PatternScan()
        with pytest.raises(dataclasses.FrozenInstanceError):
            scan.unattributable_count = 99  # type: ignore[misc]


class TestTheConstructorTakesKeywords:
    """🔴 变异测试发现：``__init__` 的 ``*`` 改成 ``/`` 之后全绿。

    ``PatternDetector(threshold=..., weighting=...)`` 是**按关键字**调用的
    契约——改成位置限定之后，``threshold=2`` 会直接 TypeError。
    既有用例只用无参构造，所以没有任何断言碰到它。
    """

    def test_both_parameters_are_keyword_only(self) -> None:
        built = PatternDetector(threshold=2, weighting=DEFAULT_WEIGHTING)
        assert built.threshold == 2
        assert built.weighting is DEFAULT_WEIGHTING

    def test_a_threshold_of_two_is_accepted(self) -> None:
        """🔴 ``threshold < 2`` 的**边界值**：2 是那道守卫声称的最小合法值。

        改成 ``< 3`` 或 ``<= 2`` 之后，门槛 2 被当成非法——
        而"1 会被拒"那条断言对它照样成立。加上这一条两个方向都钉住了。
        """
        assert PatternDetector(threshold=2).threshold == 2

    def test_one_below_the_boundary_is_still_refused(self) -> None:
        with pytest.raises(ValueError, match="不变量 10"):
            PatternDetector(threshold=0)
