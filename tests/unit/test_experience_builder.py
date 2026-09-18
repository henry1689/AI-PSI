"""经验构建（任务书 §11.1）。

🔴 本文件有两组不同性质的用例：

* **搬运正确性**——回合的客观事实是否原样进了经验；
* **边界本身**——学习链路**收不收自由文本**。

后者用结构断言而不是行为断言：它检查的是"这些对象有哪些字段"，
因为一个字段一旦被加上，写入它的代码就会出现，而那时再拦已经晚了。
"""

from __future__ import annotations

import dataclasses
from uuid import uuid4

import pytest

from ai_psi.domain.enums import (
    ConfidenceBand,
    ErrorType,
    FeedbackType,
    RoundState,
    SensitivityLevel,
    VerificationStatus,
)
from ai_psi.learning.error_classifier import ErrorAttribution, ErrorClassifier, ErrorSignals
from ai_psi.learning.experience_builder import ExperienceBuilder, RoundRecord

pytestmark = pytest.mark.unit


def _record(**overrides: object) -> RoundRecord:
    payload: dict[str, object] = {
        "cognitive_round_id": uuid4(),
        "judgment_id": uuid4(),
        "situation_signature": "d1|with_evidence|h2",
        "inquiry_type": "factual",
    }
    payload.update(overrides)
    return RoundRecord(**payload)  # type: ignore[arg-type]


def _signals(**overrides: object) -> ErrorSignals:
    payload: dict[str, object] = {"round_state": RoundState.COMPLETED}
    payload.update(overrides)
    return ErrorSignals(**payload)  # type: ignore[arg-type]


@pytest.fixture
def builder() -> ExperienceBuilder:
    return ExperienceBuilder()


class TestCarriesRoundFacts:
    def test_round_identity_is_carried(self) -> None:
        record = _record()
        experience, _ = ExperienceBuilder().build(record=record, signals=_signals())
        assert experience.cognitive_round_id == record.cognitive_round_id
        assert experience.judgment_id == record.judgment_id
        assert experience.situation_signature == record.situation_signature
        assert experience.inquiry_type == record.inquiry_type

    def test_evidence_available_at_time_survives(self) -> None:
        """🔴 经验里最关键的一个字段。

        它区分"当时判断错了"与"当时信息本就不足"。少了它，
        系统会把所有后来被推翻的判断都记成错误，从而学到
        "这类问题要更保守"——而真正的教训是需要先取证。
        """
        evidence = (uuid4(), uuid4())
        experience, _ = ExperienceBuilder().build(
            record=_record(evidence_ids=evidence), signals=_signals()
        )
        assert tuple(experience.evidence_available_at_time) == evidence

    def test_later_evidence_stays_separate_from_available_evidence(self) -> None:
        """后来的证据与当时就有的证据**不能混**——混了归因就失去依据。"""
        at_time, later = uuid4(), uuid4()
        experience, _ = ExperienceBuilder().build(
            record=_record(evidence_ids=(at_time,), later_evidence_ids=(later,)),
            signals=_signals(),
        )
        assert experience.evidence_available_at_time == [at_time]
        assert experience.later_evidence_ids == [later]

    def test_conditions_and_counterexamples_are_carried(self) -> None:
        experience, _ = ExperienceBuilder().build(
            record=_record(
                applicable_conditions=("仅限单来源证据",),
                counterexamples=("多来源时该结论不成立",),
            ),
            signals=_signals(),
        )
        assert experience.applicable_conditions == ["仅限单来源证据"]
        assert experience.counterexamples == ["多来源时该结论不成立"]

    def test_strategy_is_carried_but_effectiveness_stays_unset(self) -> None:
        """``strategy_effectiveness`` 在 V0.1 恒为 ``None``。

        回答"这个策略好不好"需要判据，而 V0.1 没有——
        用"出错没有"凑一个三档评级会把两个问题混成一个数字，
        而那个数字之后会被当成策略有效性来用。
        """
        experience, _ = ExperienceBuilder().build(
            record=_record(strategy_used=("logical_analyzer", "evidence_ranker")),
            signals=_signals(),
        )
        assert experience.strategy_used == ["logical_analyzer", "evidence_ranker"]
        assert experience.strategy_effectiveness is None

    def test_experience_starts_unverified(self) -> None:
        experience, _ = ExperienceBuilder().build(record=_record(), signals=_signals())
        assert experience.verification_status is VerificationStatus.UNVERIFIED

    def test_created_by_is_recorded(self) -> None:
        experience, _ = ExperienceBuilder().build(
            record=_record(), signals=_signals(), created_by="custom_builder"
        )
        assert experience.created_by == "custom_builder"


class TestAttributionIsCarried:
    def test_error_type_and_confidence_come_from_attribution(self) -> None:
        experience, attribution = ExperienceBuilder().build(
            record=_record(), signals=_signals(scope_drift_detected=True)
        )
        assert experience.error_type is ErrorType.SCOPE_ERROR
        assert experience.attribution_confidence is attribution.confidence

    def test_unattributable_round_is_stored_as_unknown(self) -> None:
        """🔴 ``None`` 表示**不知道**，不是"没有错误"。

        两者在模式发现里必须区分对待：混在一起会让"未归因"
        积累成一个看起来像模式的东西。
        """
        experience, attribution = ExperienceBuilder().build(record=_record(), signals=_signals())
        assert experience.error_type is None
        assert experience.attribution_confidence is ConfidenceBand.VERY_LOW
        assert experience.is_attributable is False
        assert attribution.attributable is False

    def test_attribution_reasons_are_returned_to_the_caller(self) -> None:
        """只把 ``error_type`` 存进经验，理由就丢了。

        理由一并返回，是为了让审计与调试能回答"为什么是这么归的"。
        """
        _, attribution = ExperienceBuilder().build(
            record=_record(), signals=_signals(budget_exhausted=True)
        )
        assert attribution.reasons

    def test_classifier_is_injectable(self) -> None:
        class AlwaysReasoning(ErrorClassifier):
            def classify(self, signals: ErrorSignals) -> ErrorAttribution:
                return super().classify(_signals(missing_counterexample_detected=True))

        experience, _ = ExperienceBuilder(classifier=AlwaysReasoning()).build(
            record=_record(), signals=_signals()
        )
        assert experience.error_type is ErrorType.REASONING_ERROR

    def test_default_classifier_is_used_when_none_given(self) -> None:
        assert isinstance(ExperienceBuilder().classifier, ErrorClassifier)


class TestFeedbackIsSummarisedWithoutItsText:
    """🔴 反馈以**类型**参与学习，不以**内容**参与。"""

    def test_summary_contains_only_types(self) -> None:
        summary = ExperienceBuilder().summarise_feedback(
            _signals(feedback_types=(FeedbackType.CORRECTION, FeedbackType.CLARIFICATION))
        )
        assert summary == ("feedback:clarification", "feedback:correction")

    def test_summary_is_sorted_and_deduplicated(self) -> None:
        """顺序必须确定——模式发现按它分组，随措辞或顺序变化会分错组。"""
        summary = ExperienceBuilder().summarise_feedback(
            _signals(
                feedback_types=(
                    FeedbackType.CORRECTION,
                    FeedbackType.CORRECTION,
                    FeedbackType.DISAGREEMENT,
                )
            )
        )
        assert summary == ("feedback:correction", "feedback:disagreement")

    def test_no_feedback_yields_empty_summary(self) -> None:
        assert ExperienceBuilder().summarise_feedback(_signals()) == ()


class TestNoFreeTextEntersTheLearningChain:
    """🔴 这两条是**边界断言**，不是行为断言。

    它们检查的是"这些对象有哪些字段"。一个人往 ``RoundRecord`` 里
    加 ``user_message`` 时，看着它失败会停下来想一秒——
    而这一秒正是这个测试存在的全部意义。
    """

    _ROUND_RECORD_FIELDS = frozenset(
        {
            "cognitive_round_id",
            "judgment_id",
            "situation_signature",
            "inquiry_type",
            "evidence_ids",
            "strategy_used",
            "later_evidence_ids",
            "applicable_conditions",
            "counterexamples",
            "sensitivity",
        }
    )

    _ERROR_SIGNAL_FIELDS = frozenset(
        {
            "round_state",
            "failure_stage",
            "failure_category",
            "budget_exhausted",
            "memory_write_rejected",
            "scope_drift_detected",
            "unsupported_certainty_detected",
            "missing_counterexample_detected",
            "high_confirmation_bias",
            "high_user_pleasing_bias",
            "uncertainty_type",
            "epistemic_action",
            "feedback_types",
        }
    )

    def test_round_record_carries_no_free_text(self) -> None:
        assert {item.name for item in dataclasses.fields(RoundRecord)} == self._ROUND_RECORD_FIELDS

    def test_error_signals_carry_no_free_text(self) -> None:
        assert {item.name for item in dataclasses.fields(ErrorSignals)} == self._ERROR_SIGNAL_FIELDS

    def test_records_are_immutable(self) -> None:
        """冻结对象：经验一旦构建就不该被就地改写。"""
        record = _record()
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.situation_signature = "改掉了"  # type: ignore[misc]

    def test_sensitivity_defaults_to_internal(self) -> None:
        """默认不是 ``PUBLIC``：学习记录的默认去向必须是**不外发**。"""
        assert _record().sensitivity is SensitivityLevel.INTERNAL
