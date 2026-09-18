"""提案门槛裁决（任务书 §11.3）。

🔴 本文件里最重要的一组用例是**"未评估 ≠ 不成立"**。

把无法评估的条件当成"不成立"，会让系统永远只从最容易计数的那个条件
（同类错误 ≥3 次）产生提案——而"离线评测暴露稳定退化"恰恰是
最有价值、也最不可能被计数捕捉的那一类。因此第 3、5 条要求调用方
**显式提供**观测：没提供就是"未评估"，它必须出现在理由里，而不是消失。
"""

from __future__ import annotations

import dataclasses
from uuid import uuid4

import pytest

from ai_psi.domain.enums import ErrorType
from ai_psi.domain.improvement_proposals import PROPOSAL_ESCALATION_THRESHOLD
from ai_psi.learning.pattern_detector import ErrorPattern
from ai_psi.learning.promotion_policy import (
    MODULE_REGRESSION_STREAK,
    SEVERE_ERROR_TYPES,
    PromotionEvidence,
    PromotionPolicy,
    PromotionTrigger,
)

pytestmark = pytest.mark.unit


def _pattern(error_type: ErrorType = ErrorType.REASONING_ERROR, count: int = 3) -> ErrorPattern:
    return ErrorPattern(
        error_type=error_type,
        situation_signature="d1|with_evidence|h2",
        experience_ids=tuple(uuid4() for _ in range(count)),
        counterexample_count=0,
    )


@pytest.fixture
def policy() -> PromotionPolicy:
    return PromotionPolicy()


class TestThreshold:
    def test_default_threshold_is_the_invariant_value(self) -> None:
        assert PromotionPolicy().threshold == PROPOSAL_ESCALATION_THRESHOLD == 3

    def test_threshold_below_two_is_rejected(self) -> None:
        """🔴 不变量 10 的数值落点：门槛 1 等于允许单次经验推广为全局策略。"""
        with pytest.raises(ValueError, match="不变量 10"):
            PromotionPolicy(threshold=1)

    def test_zero_threshold_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            PromotionPolicy(threshold=0)


class TestConditionOneRepeatedErrors:
    def test_pattern_at_threshold_triggers(self, policy) -> None:
        decision = policy.decide(PromotionEvidence(pattern=_pattern(count=3)))
        assert PromotionTrigger.REPEATED_SAME_ERROR in decision.triggers
        assert decision.allowed is True

    def test_pattern_below_threshold_does_not_trigger(self, policy) -> None:
        decision = policy.decide(PromotionEvidence(pattern=_pattern(count=2)))
        assert decision.allowed is False

    def test_single_experience_never_triggers(self, policy) -> None:
        """🔴 任务书 §11.3 与不变量 10 的直接落地。"""
        assert policy.decide(PromotionEvidence(pattern=_pattern(count=1))).allowed is False

    def test_no_pattern_is_reported_as_such(self, policy) -> None:
        decision = policy.decide(PromotionEvidence())
        assert any("没有达到次数的模式" in reason for reason in decision.reasons)

    def test_repetition_counts_are_named_in_the_reason(self, policy) -> None:
        decision = policy.decide(PromotionEvidence(pattern=_pattern(count=2)))
        joined = "".join(decision.reasons)
        assert "2 次" in joined and "3" in joined


class TestConditionTwoSevereErrorWithFix:
    @pytest.mark.parametrize("error_type", sorted(SEVERE_ERROR_TYPES, key=str))
    def test_severe_error_with_direction_triggers(self, policy, error_type: ErrorType) -> None:
        decision = policy.decide(
            PromotionEvidence(
                pattern=_pattern(error_type, count=1), fix_direction="加一条显式反例检查"
            )
        )
        assert PromotionTrigger.SEVERE_ERROR_WITH_FIX in decision.triggers

    def test_severe_error_without_direction_does_not_trigger(self, policy) -> None:
        """🔴 "再看看"不是修复方向——没有方向的严重错误只说明"这里有问题"。"""
        decision = policy.decide(
            PromotionEvidence(pattern=_pattern(ErrorType.CALIBRATION_ERROR, count=1))
        )
        assert PromotionTrigger.SEVERE_ERROR_WITH_FIX not in decision.triggers
        assert any("明确修复方向" in reason for reason in decision.reasons)

    def test_non_severe_error_with_direction_does_not_trigger(self, policy) -> None:
        """普通错误即使有方向，也仍需达到次数门槛——否则等于绕开了门槛。"""
        decision = policy.decide(
            PromotionEvidence(
                pattern=_pattern(ErrorType.FACTUAL_ERROR, count=1), fix_direction="改这里"
            )
        )
        assert decision.allowed is False

    def test_unknown_error_is_not_severe(self, policy) -> None:
        """🔴 ``UNKNOWN_ERROR`` 不是一个类别，而是"我们不知道类别"。

        从它出发的"明确修复方向"必然是无根据的。
        """
        assert ErrorType.UNKNOWN_ERROR not in SEVERE_ERROR_TYPES
        decision = policy.decide(
            PromotionEvidence(
                pattern=_pattern(ErrorType.UNKNOWN_ERROR, count=1),
                fix_direction="大概是这里的问题",
            )
        )
        assert decision.allowed is False

    def test_severe_alone_is_not_enough(self, policy) -> None:
        decision = policy.decide(
            PromotionEvidence(pattern=_pattern(ErrorType.MEMORY_ERROR, count=1))
        )
        assert decision.allowed is False


class TestConditionThreeOfflineRegression:
    """🔴 "未评估"与"未退化"必须能区分开。"""

    def test_regression_triggers(self, policy) -> None:
        decision = policy.decide(PromotionEvidence(offline_regression=True))
        assert PromotionTrigger.OFFLINE_REGRESSION in decision.triggers
        assert decision.allowed is True

    def test_no_regression_does_not_trigger(self, policy) -> None:
        decision = policy.decide(PromotionEvidence(offline_regression=False))
        assert decision.allowed is False
        assert any("未暴露稳定退化" in reason for reason in decision.reasons)

    def test_not_evaluated_is_not_reported_as_no_regression(self, policy) -> None:
        """这是本文件的核心用例。

        ``None`` 与 ``False`` 走了两条不同的分支：
        前者说"未评估"，后者说"未退化"。把它们写成同一个理由，
        就等于把"没查"说成"查了没问题"。
        """
        decision = policy.decide(PromotionEvidence(offline_regression=None))
        joined = "".join(decision.reasons)
        assert "未评估" in joined
        assert "未暴露稳定退化" not in joined

    def test_not_evaluated_explains_the_consequence(self, policy) -> None:
        """理由要说清**为什么**不把未评估当作不成立，而不只是标一个状态。"""
        decision = policy.decide(PromotionEvidence(offline_regression=None))
        assert any("最容易计数" in reason for reason in decision.reasons)


class TestConditionFourUserCorrections:
    def test_below_threshold_does_not_trigger(self, policy) -> None:
        decision = policy.decide(
            PromotionEvidence(user_corrections=2, user_correction_shows_systemic_issue=True)
        )
        assert decision.allowed is False

    def test_threshold_without_systemic_issue_does_not_trigger(self, policy) -> None:
        """🔴 同一用户在不同问题上的单次纠正加起来，不构成系统性问题。"""
        decision = policy.decide(
            PromotionEvidence(user_corrections=3, user_correction_shows_systemic_issue=False)
        )
        assert decision.allowed is False
        assert any("系统性问题" in reason for reason in decision.reasons)

    def test_threshold_with_systemic_issue_triggers(self, policy) -> None:
        decision = policy.decide(
            PromotionEvidence(user_corrections=3, user_correction_shows_systemic_issue=True)
        )
        assert PromotionTrigger.USER_CORRECTION_PATTERN in decision.triggers

    def test_the_default_is_not_an_observation(self) -> None:
        """默认构造出来的证据里，条件 3/4/5 都是"未评估"。"""
        evidence = PromotionEvidence()
        assert evidence.offline_regression is None
        assert evidence.module_streak is None
        assert evidence.user_corrections is None

    def test_not_observed_is_reported(self, policy) -> None:
        """🔴 ``None`` 是"未观测"，与 ``0``（观测过、没有）不是一回事。

        条件 4 的计数与条件 5 的连续次数是同一类东西（"观测到的次数"），
        两者对"未观测"的表达必须一致。
        """
        decision = policy.decide(PromotionEvidence())
        condition_four = next(reason for reason in decision.reasons if reason.startswith("条件四"))
        assert "未观测" in condition_four

    def test_zero_corrections_is_an_observation_not_an_absence(self, policy) -> None:
        decision = policy.decide(PromotionEvidence(user_corrections=0))
        condition_four = next(reason for reason in decision.reasons if reason.startswith("条件四"))
        assert "未观测" not in condition_four
        assert "未达门槛" in condition_four

    def test_defaults_do_not_trigger(self, policy) -> None:
        assert policy.decide(PromotionEvidence()).allowed is False


class TestConditionFiveModuleStreak:
    def test_streak_at_limit_triggers(self, policy) -> None:
        decision = policy.decide(PromotionEvidence(module_streak=MODULE_REGRESSION_STREAK))
        assert PromotionTrigger.MODULE_BELOW_THRESHOLD in decision.triggers

    def test_shorter_streak_does_not_trigger(self, policy) -> None:
        decision = policy.decide(PromotionEvidence(module_streak=MODULE_REGRESSION_STREAK - 1))
        assert decision.allowed is False

    def test_zero_streak_is_an_observation_not_an_absence(self, policy) -> None:
        """``0`` 是"看了，没低于阈值"，它与 ``None``（没看）不同。"""
        decision = policy.decide(PromotionEvidence(module_streak=0))
        condition_five = next(reason for reason in decision.reasons if reason.startswith("条件五"))
        assert "未观测" not in condition_five
        assert "未达门槛" in condition_five

    def test_not_observed_is_reported(self, policy) -> None:
        decision = policy.decide(PromotionEvidence(module_streak=None))
        assert any("未观测" in reason for reason in decision.reasons)


class TestDecisionShape:
    def test_no_triggers_means_not_allowed(self, policy) -> None:
        decision = policy.decide(PromotionEvidence())
        assert decision.allowed is False
        assert decision.triggers == ()

    def test_reasons_cover_all_five_conditions(self, policy) -> None:
        """🔴 一份只有"为什么可以"的裁决，日后被质疑时回答不了"那另外四条呢"。"""
        decision = policy.decide(PromotionEvidence())
        joined = "".join(decision.reasons)
        for index in ("一", "二", "三", "四", "五"):
            assert f"条件{index}" in joined

    def test_multiple_triggers_are_all_reported(self, policy) -> None:
        decision = policy.decide(
            PromotionEvidence(
                pattern=_pattern(ErrorType.SCOPE_ERROR, count=3),
                fix_direction="收紧范围检查",
                offline_regression=True,
            )
        )
        assert set(decision.triggers) == {
            PromotionTrigger.REPEATED_SAME_ERROR,
            PromotionTrigger.SEVERE_ERROR_WITH_FIX,
            PromotionTrigger.OFFLINE_REGRESSION,
        }

    def test_counterexamples_are_passed_through_with_a_warning(self, policy) -> None:
        """反例**不被过滤**——只看支持证据的改进是不成立的。"""
        pattern = ErrorPattern(
            error_type=ErrorType.REASONING_ERROR,
            situation_signature="sig",
            experience_ids=(uuid4(), uuid4(), uuid4()),
            counterexample_count=2,
        )
        decision = policy.decide(PromotionEvidence(pattern=pattern))
        assert decision.counterexample_count == 2
        assert any("反例" in reason for reason in decision.reasons)

    def test_counterexample_count_is_zero_without_a_pattern(self, policy) -> None:
        assert policy.decide(PromotionEvidence()).counterexample_count == 0

    def test_no_warning_when_there_are_no_counterexamples(self, policy) -> None:
        decision = policy.decide(PromotionEvidence(pattern=_pattern(count=3)))
        assert not any("带有反例" in reason for reason in decision.reasons)

    def test_decision_is_frozen(self, policy) -> None:
        decision = policy.decide(PromotionEvidence())
        with pytest.raises(dataclasses.FrozenInstanceError):
            decision.allowed = True


class TestTriggerVocabulary:
    def test_all_five_triggers_exist(self) -> None:
        assert len(list(PromotionTrigger)) == 5

    def test_every_trigger_has_a_stable_value(self) -> None:
        assert {item.value for item in PromotionTrigger} == {
            "repeated_same_error",
            "severe_error_with_fix",
            "offline_regression",
            "user_correction_pattern",
            "module_below_threshold",
        }

    def test_module_streak_is_three(self) -> None:
        assert MODULE_REGRESSION_STREAK == 3
