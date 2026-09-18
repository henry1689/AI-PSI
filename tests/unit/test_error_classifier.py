"""错误归因（任务书 §11.2）。

🔴 本文件的每一组用例都在验证同一件事：
**归因永远能追回到一条具体的判据。**

归因决定了系统之后学什么，所以"判不了"必须是一个**可以出现**的结论。
一个总在给答案的分类器比一个会说"不知道"的分类器危险得多——
前者会用看起来合理的分类污染此后所有的模式发现。
"""

from __future__ import annotations

import inspect
import re

import pytest

from ai_psi.application import cognitive_runtime
from ai_psi.domain.enums import (
    ConfidenceBand,
    EpistemicAction,
    ErrorType,
    FeedbackType,
    RoundState,
    UncertaintyType,
)
from ai_psi.learning.error_classifier import (
    STAGE_ERROR_CATEGORY,
    ErrorAttribution,
    ErrorClassifier,
    ErrorSignals,
)

pytestmark = pytest.mark.unit

#: 归因输入里"什么都没发生"的基线。各用例只覆盖它关心的那一项。
QUIET = ErrorSignals(round_state=RoundState.COMPLETED)


@pytest.fixture
def classifier() -> ErrorClassifier:
    return ErrorClassifier()


def _signals(**overrides: object) -> ErrorSignals:
    payload: dict[str, object] = {"round_state": RoundState.COMPLETED}
    payload.update(overrides)
    return ErrorSignals(**payload)  # type: ignore[arg-type]


class TestNoAttribution:
    """🔴 "判不了"是一个合法结论，不是一个需要被填满的空位。"""

    def test_quiet_round_is_not_attributed(self, classifier: ErrorClassifier) -> None:
        attribution = classifier.classify(QUIET)
        assert attribution.error_type is None
        assert attribution.attributable is False

    def test_reasons_explain_the_absence(self, classifier: ErrorClassifier) -> None:
        """没有归因时，理由必须说明**是没找到判据**，而不是留空。"""
        assert classifier.classify(QUIET).reasons

    def test_every_attribution_carries_reasons(self, classifier: ErrorClassifier) -> None:
        """带类别的归因不允许空理由——不可解释的归因日后无法被推翻。"""
        samples = [
            _signals(failure_category=ErrorType.FACTUAL_ERROR, failure_stage="analyze"),
            _signals(budget_exhausted=True),
            _signals(memory_write_rejected=True),
            _signals(scope_drift_detected=True),
            _signals(unsupported_certainty_detected=True),
            _signals(
                uncertainty_type=UncertaintyType.NORMATIVE,
                epistemic_action=EpistemicAction.ANSWER,
            ),
            _signals(missing_counterexample_detected=True),
            _signals(feedback_types=(FeedbackType.CORRECTION,)),
        ]
        for signals in samples:
            attribution = classifier.classify(signals)
            assert attribution.error_type is not None
            assert attribution.reasons, attribution


class TestFailureRule:
    """阶段映射已经给出了类别——这是最具体的判据，直接采用。"""

    def test_failure_category_is_used_directly(self, classifier: ErrorClassifier) -> None:
        attribution = classifier.classify(
            _signals(
                round_state=RoundState.FAILED,
                failure_stage="respond",
                failure_category=ErrorType.EXPRESSION_ERROR,
            )
        )
        assert attribution.error_type is ErrorType.EXPRESSION_ERROR
        assert attribution.confidence is ConfidenceBand.MODERATE

    def test_reason_names_the_stage(self, classifier: ErrorClassifier) -> None:
        attribution = classifier.classify(
            _signals(failure_category=ErrorType.SCOPE_ERROR, failure_stage="frame")
        )
        assert any("frame" in reason for reason in attribution.reasons)

    def test_missing_stage_still_classifies(self, classifier: ErrorClassifier) -> None:
        """阶段名缺失不该让归因失败——类别是已知的，阶段只是补充信息。"""
        attribution = classifier.classify(_signals(failure_category=ErrorType.EVIDENCE_ERROR))
        assert attribution.error_type is ErrorType.EVIDENCE_ERROR

    def test_failure_rule_outranks_structural_signals(self, classifier: ErrorClassifier) -> None:
        """判据按具体性排序：已知的失败类别比"元认知检出了什么"更具体。"""
        attribution = classifier.classify(
            _signals(
                round_state=RoundState.FAILED,
                failure_category=ErrorType.EVIDENCE_ERROR,
                scope_drift_detected=True,
                budget_exhausted=True,
            )
        )
        assert attribution.error_type is ErrorType.EVIDENCE_ERROR


class TestStructuralSignals:
    """这些信号**指名了类别**，因此排在用户反馈之前。"""

    def test_budget_exhaustion_is_a_process_error(self, classifier: ErrorClassifier) -> None:
        """🔴 没跑完 ≠ 判断错了。两者的改进方向完全相反。"""
        attribution = classifier.classify(_signals(budget_exhausted=True))
        assert attribution.error_type is ErrorType.PROCESS_ERROR

    def test_memory_rejection_is_a_memory_error(self, classifier: ErrorClassifier) -> None:
        attribution = classifier.classify(_signals(memory_write_rejected=True))
        assert attribution.error_type is ErrorType.MEMORY_ERROR

    def test_scope_drift_is_a_scope_error(self, classifier: ErrorClassifier) -> None:
        attribution = classifier.classify(_signals(scope_drift_detected=True))
        assert attribution.error_type is ErrorType.SCOPE_ERROR

    def test_unsupported_certainty_is_a_calibration_error(
        self, classifier: ErrorClassifier
    ) -> None:
        attribution = classifier.classify(_signals(unsupported_certainty_detected=True))
        assert attribution.error_type is ErrorType.CALIBRATION_ERROR


class TestValueSubstitution:
    """判据是**结构性的**：只看两个字段是否自相矛盾，不看措辞。"""

    def test_normative_with_answer_is_substitution(self, classifier: ErrorClassifier) -> None:
        attribution = classifier.classify(
            _signals(
                uncertainty_type=UncertaintyType.NORMATIVE,
                epistemic_action=EpistemicAction.ANSWER,
            )
        )
        assert attribution.error_type is ErrorType.VALUE_SUBSTITUTION

    def test_normative_with_out_of_scope_is_not_an_error(self, classifier: ErrorClassifier) -> None:
        """🔴 诚实地说"这不是认知系统该回答的"**不是错误**。

        这正是任务书 §9.10 要求的行为。把它归成错误等于教系统
        去回答它本该拒绝回答的价值问题。
        """
        attribution = classifier.classify(
            _signals(
                uncertainty_type=UncertaintyType.NORMATIVE,
                epistemic_action=EpistemicAction.OUT_OF_SCOPE,
            )
        )
        assert attribution.error_type is None

    def test_alethic_with_answer_is_not_substitution(self, classifier: ErrorClassifier) -> None:
        """证据不足但答了，是另一类问题（证据类），不是价值替换。"""
        attribution = classifier.classify(
            _signals(
                uncertainty_type=UncertaintyType.ALETHIC,
                epistemic_action=EpistemicAction.ANSWER,
            )
        )
        assert attribution.error_type is None


class TestReasoningSignals:
    def test_missing_counterexample_is_moderate(self, classifier: ErrorClassifier) -> None:
        attribution = classifier.classify(_signals(missing_counterexample_detected=True))
        assert attribution.error_type is ErrorType.REASONING_ERROR
        assert attribution.confidence is ConfidenceBand.MODERATE

    def test_bias_alone_is_low_confidence(self, classifier: ErrorClassifier) -> None:
        """偏差是风险信号，不是已发生的事实——因此置信度更低，但仍在可归因区间。"""
        attribution = classifier.classify(_signals(high_confirmation_bias=True))
        assert attribution.error_type is ErrorType.REASONING_ERROR
        assert attribution.confidence is ConfidenceBand.LOW
        assert attribution.attributable is True

    def test_all_signals_are_listed_in_reasons(self, classifier: ErrorClassifier) -> None:
        attribution = classifier.classify(
            _signals(
                missing_counterexample_detected=True,
                high_confirmation_bias=True,
                high_user_pleasing_bias=True,
            )
        )
        joined = "".join(attribution.reasons)
        assert "反例" in joined
        assert "确认偏差" in joined
        assert "迎合" in joined

    def test_reasoning_outranks_user_feedback(self, classifier: ErrorClassifier) -> None:
        """🔴 用户说"错了"（类别未知）与元认知说"错在哪"同时出现时，取后者。

        反过来排序会让所有"用户纠正过"的回合都归成同一个笼统类别，
        模式发现因此失去分辨力。
        """
        attribution = classifier.classify(
            _signals(
                missing_counterexample_detected=True,
                feedback_types=(FeedbackType.CORRECTION,),
            )
        )
        assert attribution.error_type is ErrorType.REASONING_ERROR


class TestUserFeedbackRule:
    """用户纠正是最强的"确实错了"证据，但它**不告诉我们错在哪**。"""

    @pytest.mark.parametrize("feedback", [FeedbackType.CORRECTION, FeedbackType.DISAGREEMENT])
    def test_negative_feedback_is_unknown_error(
        self, classifier: ErrorClassifier, feedback: FeedbackType
    ) -> None:
        attribution = classifier.classify(_signals(feedback_types=(feedback,)))
        assert attribution.error_type is ErrorType.UNKNOWN_ERROR
        assert attribution.confidence is ConfidenceBand.LOW

    def test_reason_admits_it_cannot_name_the_category(self, classifier: ErrorClassifier) -> None:
        """理由必须说清"为什么给不出具体类别"，否则它看起来像一次失败。"""
        attribution = classifier.classify(_signals(feedback_types=(FeedbackType.CORRECTION,)))
        joined = "".join(attribution.reasons)
        assert "语义" in joined

    @pytest.mark.parametrize(
        "feedback",
        [FeedbackType.AGREEMENT, FeedbackType.ACKNOWLEDGEMENT, FeedbackType.RATING],
    )
    def test_positive_feedback_is_not_an_error(
        self, classifier: ErrorClassifier, feedback: FeedbackType
    ) -> None:
        """🔴 赞同**不是**错误信号，也绝不能变成"已验证"（不变量 4）。"""
        assert classifier.classify(_signals(feedback_types=(feedback,))).error_type is None

    def test_clarification_alone_is_not_an_error(self, classifier: ErrorClassifier) -> None:
        """澄清只是补充信息，不代表之前判断有误。"""
        assert (
            classifier.classify(_signals(feedback_types=(FeedbackType.CLARIFICATION,))).error_type
            is None
        )


class TestAttributableBoundary:
    def test_very_low_confidence_is_not_attributable(self) -> None:
        attribution = ErrorAttribution(
            error_type=ErrorType.REASONING_ERROR, confidence=ConfidenceBand.VERY_LOW
        )
        assert attribution.attributable is False

    def test_low_confidence_is_attributable(self) -> None:
        """LOW 是**可归因的下界**——边界值必须落在能用的那一侧。"""
        attribution = ErrorAttribution(
            error_type=ErrorType.REASONING_ERROR, confidence=ConfidenceBand.LOW
        )
        assert attribution.attributable is True

    def test_attributable_requires_a_category(self) -> None:
        attribution = ErrorAttribution(error_type=None, confidence=ConfidenceBand.VERY_HIGH)
        assert attribution.attributable is False


class TestStageMapping:
    """🔴 阶段映射**只有一份**（阶段 6 从运行时合并到这里）。

    两份各自维护的映射一旦漂移，"同一个失败在两个地方得到不同类别"
    就会发生，而且没有任何地方会报错。
    """

    #: 运行时里 `stage = "..."` 的赋值语句。
    _STAGE_ASSIGNMENT = re.compile(r'^\s*stage = "([a-z_]+)"', re.MULTILINE)

    def test_mapping_covers_every_stage_the_runtime_assigns(self) -> None:
        """从**源码里读出来**，而不是抄一份阶段名清单。

        抄一份清单的话，新增一个阶段时这份清单不会变，
        它会继续通过——而新阶段在运行期悄悄得到 ``UNKNOWN_ERROR``。
        """
        assigned = set(self._STAGE_ASSIGNMENT.findall(inspect.getsource(cognitive_runtime)))
        assert assigned, "没能从运行时源码里找到任何阶段赋值，测试本身失效了"
        assert assigned == set(STAGE_ERROR_CATEGORY)

    def test_runtime_lookup_agrees_with_the_table(self) -> None:
        for stage, expected in STAGE_ERROR_CATEGORY.items():
            assert cognitive_runtime._category_for(stage) is expected, stage

    def test_unknown_stage_falls_back_to_unknown_error(self) -> None:
        assert cognitive_runtime._category_for("没有这个阶段") is ErrorType.UNKNOWN_ERROR

    def test_every_category_is_a_real_error_type(self) -> None:
        assert all(isinstance(item, ErrorType) for item in STAGE_ERROR_CATEGORY.values())

    def test_no_stage_maps_to_unknown_error(self) -> None:
        """映射表里的阶段都是**已知**阶段，给它一个"未知"类别等于没映射。"""
        assert ErrorType.UNKNOWN_ERROR not in STAGE_ERROR_CATEGORY.values()
