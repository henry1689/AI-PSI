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
    CorrectedArtifactKind,
    EpistemicAction,
    ErrorType,
    FeedbackType,
    RoundState,
    UncertaintyType,
)
from ai_psi.learning.error_classifier import (
    STAGE_ERROR_CATEGORY,
    CorrectionTarget,
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


def _target(kind: CorrectedArtifactKind, **overrides: object) -> CorrectionTarget:
    """构造一个纠正目标。默认是最简单的那种：指到一条证据。"""
    payload: dict[str, object] = {"artifact_kind": kind}
    payload.update(overrides)
    return CorrectionTarget(**payload)  # type: ignore[arg-type]


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
            _signals(
                round_state=RoundState.FAILED,
                failure_category=ErrorType.FACTUAL_ERROR,
                failure_stage="analyze",
            ),
            _signals(budget_exhausted=True),
            _signals(memory_write_rejected=True),
            _signals(scope_drift_detected=True),
            _signals(unsupported_certainty_detected=True),
            _signals(
                uncertainty_type=UncertaintyType.NORMATIVE,
                epistemic_action=EpistemicAction.ANSWER,
            ),
            _signals(missing_counterexample_detected=True),
            _signals(
                feedback_types=(FeedbackType.CORRECTION,),
                correction=_target(CorrectedArtifactKind.HYPOTHESIS),
            ),
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
            _signals(
                round_state=RoundState.FAILED,
                failure_category=ErrorType.SCOPE_ERROR,
                failure_stage="frame",
            )
        )
        assert any("frame" in reason for reason in attribution.reasons)

    def test_missing_stage_still_classifies(self, classifier: ErrorClassifier) -> None:
        """阶段名缺失不该让归因失败——类别是已知的，阶段只是补充信息。"""
        attribution = classifier.classify(
            _signals(round_state=RoundState.FAILED, failure_category=ErrorType.EVIDENCE_ERROR)
        )
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

    def test_a_completed_round_is_not_a_failure(self, classifier: ErrorClassifier) -> None:
        """🔴 **失败类别只在回合真的失败时才算数。**

        ``CognitiveRound`` 只要求 ``FAILED`` 时必填
        ``failure_stage`` / ``error_category``，**没有禁止**其他状态携带它们。
        只看"类别非空"的话，一个终态是 ``COMPLETED`` 却带着失败类别的回合
        会被归成失败——理由栏还会写下「回合在「respond」阶段失败
        （终态 completed）」这种自相矛盾的句子，而它会作为一次真实错误
        进入模式发现。
        """
        attribution = classifier.classify(
            _signals(
                round_state=RoundState.COMPLETED,
                failure_category=ErrorType.EXPRESSION_ERROR,
                failure_stage="respond",
            )
        )
        assert attribution.error_type is None


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


class TestUserCorrectionRule:
    """🔴 用户纠正要形成归因，**两个条件缺一不可**（阶段 6.6，ADR-0023）。

    反馈类型是否定性的（``CORRECTION`` / ``DISAGREEMENT``），
    **并且**它指得出被纠正的是哪一条产物。

    ⚠️ **阶段 6.5 的版本只要求第一条**，一律归成 ``UNKNOWN_ERROR``。
    那个类别诚实但**没有分辨力**：所有被纠正过的回合都会落进同一个
    模式，"三次同类错误"于是退化成"三次被纠正过"——而模式发现
    存在的理由恰恰是分辨"哪一类错在反复发生"。
    """

    @pytest.mark.parametrize("feedback", [FeedbackType.CORRECTION, FeedbackType.DISAGREEMENT])
    def test_negative_feedback_without_a_target_is_not_attributed(
        self, classifier: ErrorClassifier, feedback: FeedbackType
    ) -> None:
        """🔴 只说得出「有错」、说不出「错在哪一条」→ **不归因**。

        这里**不返回 ``UNKNOWN_ERROR``**：那会让"指不出对象"与
        "对象指对了但类别未知"在计数时长得一样，而两者的下一步
        动作完全不同——前者要用户补一个指针，后者要人去看。
        """
        attribution = classifier.classify(_signals(feedback_types=(feedback,)))
        assert attribution.error_type is None
        assert attribution.confidence is ConfidenceBand.VERY_LOW

    def test_the_reason_says_why_it_holds_back(self, classifier: ErrorClassifier) -> None:
        """理由必须说清"为什么没有归因"，否则它看起来像一次失败。"""
        attribution = classifier.classify(_signals(feedback_types=(FeedbackType.CORRECTION,)))
        joined = "".join(attribution.reasons)
        assert "没有指出被纠正的是哪一条产物" in joined
        assert "不归因" in joined

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


class TestWhatTheCorrectedArtifactImplies:
    """🔴 类别由**被指产物的结构**推出来（阶段 6.6）。

    ⚠️ 映射规则是 V0.1 的**约定**，不是对错误本质的独立测量。
    正因为如此，每一步都必须能被指出来——下面每条用例断言的
    就是"哪一步推出了哪一个类别"。
    """

    @pytest.mark.parametrize(
        ("kind", "expected"),
        [
            (CorrectedArtifactKind.EVIDENCE, ErrorType.EVIDENCE_ERROR),
            (CorrectedArtifactKind.RESPONSE, ErrorType.EXPRESSION_ERROR),
        ],
    )
    def test_the_kind_alone_decides(
        self, classifier: ErrorClassifier, kind: CorrectedArtifactKind, expected: ErrorType
    ) -> None:
        attribution = classifier.classify(
            _signals(feedback_types=(FeedbackType.CORRECTION,), correction=_target(kind))
        )
        assert attribution.error_type is expected
        assert attribution.confidence is ConfidenceBand.MODERATE

    def test_a_hypothesis_without_support_is_an_evidence_error(
        self, classifier: ErrorClassifier
    ) -> None:
        """用户指出一条**当时就没有证据**的论断 → 问题在证据层。"""
        attribution = classifier.classify(
            _signals(
                feedback_types=(FeedbackType.CORRECTION,),
                correction=_target(CorrectedArtifactKind.HYPOTHESIS, has_supporting_evidence=False),
            )
        )
        assert attribution.error_type is ErrorType.EVIDENCE_ERROR

    def test_a_hypothesis_with_support_is_a_reasoning_error(
        self, classifier: ErrorClassifier
    ) -> None:
        """🔴 同一种产物，结构不同 → 类别不同。这条与上一条**成对**。

        少了它，"假设一律归 evidence_error"的实现也能全绿——
        而那正是本层最该避免的"看起来具体其实笼统"。
        """
        attribution = classifier.classify(
            _signals(
                feedback_types=(FeedbackType.CORRECTION,),
                correction=_target(CorrectedArtifactKind.HYPOTHESIS, has_supporting_evidence=True),
            )
        )
        assert attribution.error_type is ErrorType.REASONING_ERROR

    def test_a_normative_judgment_is_a_value_substitution(
        self, classifier: ErrorClassifier
    ) -> None:
        attribution = classifier.classify(
            _signals(
                feedback_types=(FeedbackType.CORRECTION,),
                correction=_target(
                    CorrectedArtifactKind.JUDGMENT, uncertainty_type=UncertaintyType.NORMATIVE
                ),
            )
        )
        assert attribution.error_type is ErrorType.VALUE_SUBSTITUTION

    def test_an_ordinary_judgment_is_a_reasoning_error(self, classifier: ErrorClassifier) -> None:
        """与上一条成对：普通判断不该被归成价值替换。"""
        attribution = classifier.classify(
            _signals(
                feedback_types=(FeedbackType.CORRECTION,),
                correction=_target(CorrectedArtifactKind.JUDGMENT),
            )
        )
        assert attribution.error_type is ErrorType.REASONING_ERROR

    def test_the_confidence_is_described_as_strategic(self, classifier: ErrorClassifier) -> None:
        """🔴 理由里必须写清它**是策略性归因**，不是两个独立来源的印证。

        `MODERATE` 这个档位本身不说明任何事——说明事的是理由。
        把"用户说了 + 我们查出来它是什么"说成"两个独立来源互相印证"，
        是把一条约定抬高成一次验证。
        """
        attribution = classifier.classify(
            _signals(
                feedback_types=(FeedbackType.CORRECTION,),
                correction=_target(CorrectedArtifactKind.EVIDENCE),
            )
        )
        joined = "".join(attribution.reasons)
        assert "策略性归因" in joined
        assert "不是两个独立来源" in joined

    @pytest.mark.parametrize("kind", [CorrectedArtifactKind.MEMORY, CorrectedArtifactKind.INQUIRY])
    def test_non_correctable_kinds_cannot_even_be_constructed(
        self, kind: CorrectedArtifactKind
    ) -> None:
        """🔴 **唯一执行点在构造处**，不在归因层。

        记忆有自己的纠正入口，问题是用户自己提的。让它们走到归因层，
        评审查到的是一条"这个 id 为什么没有类别"的死分支——
        而真正该说的是"这类产物根本不接受纠正"。
        """
        with pytest.raises(ValueError, match="不是可纠正的产物类别"):
            _target(kind)

    @pytest.mark.parametrize(
        "feedback",
        [
            FeedbackType.AGREEMENT,
            FeedbackType.ACKNOWLEDGEMENT,
            FeedbackType.RATING,
            FeedbackType.CLARIFICATION,
        ],
    )
    def test_a_target_without_negative_feedback_is_not_an_error(
        self, classifier: ErrorClassifier, feedback: FeedbackType
    ) -> None:
        """反向：**指得出对象，但反馈不是否定性的** → 仍然不归因。

        与「有否定反馈但指不出对象」合起来，才是"两者都要"的完整矩阵。
        """
        attribution = classifier.classify(
            _signals(feedback_types=(feedback,), correction=_target(CorrectedArtifactKind.JUDGMENT))
        )
        assert attribution.error_type is None


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
