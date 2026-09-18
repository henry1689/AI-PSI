"""提案生成（任务书 §11.3、§5.12）。

🔴 两组要点：

* **本层不产出改动方案**——模板停在"往哪看"，具体改法留给评审；
* **本层是门槛的第二道保险**——即使调用方忘了先问门槛，
  ``generate`` 也不会产出提案（不变量 10）。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from ai_psi.domain.enums import ApprovalLevel, ErrorType, ProposalStatus
from ai_psi.learning.pattern_detector import ErrorPattern
from ai_psi.learning.promotion_policy import (
    PromotionDecision,
    PromotionEvidence,
    PromotionPolicy,
    PromotionTrigger,
)
from ai_psi.learning.proposal_generator import (
    _OBSERVED_PROBLEM,
    _TARGET_COMPONENT,
    ProposalGenerator,
    triggered_by,
)

pytestmark = pytest.mark.unit

SIGNATURE = "d1|with_evidence|h2"


def _pattern(error_type: ErrorType = ErrorType.REASONING_ERROR, count: int = 3) -> ErrorPattern:
    return ErrorPattern(
        error_type=error_type,
        situation_signature=SIGNATURE,
        experience_ids=tuple(uuid4() for _ in range(count)),
        # 🔴 门槛比的是**加权计数**（阶段 6.5 §二.6–7）：
        # 本文件的模式一律按"已被外部证据确认"构造，因此两者相等。
        occurrence_count=count,
        weighted_count=count,
        experience_count=count,
    )


def _allowed(error_type: ErrorType = ErrorType.REASONING_ERROR) -> PromotionDecision:
    return PromotionPolicy().decide(PromotionEvidence(pattern=_pattern(error_type)))


@pytest.fixture
def generator() -> ProposalGenerator:
    return ProposalGenerator()


class TestGateIsEnforcedHereToo:
    """🔴 不变量 10 的第二道保险。"""

    def test_denied_decision_produces_nothing(self, generator) -> None:
        denied = PromotionPolicy().decide(PromotionEvidence())
        assert denied.allowed is False
        assert generator.generate(pattern=_pattern(), decision=denied) is None

    def test_denied_by_threshold_produces_nothing(self, generator) -> None:
        denied = PromotionPolicy().decide(PromotionEvidence(pattern=_pattern(count=2)))
        assert generator.generate(pattern=_pattern(count=2), decision=denied) is None

    def test_allowed_decision_produces_a_proposal(self, generator) -> None:
        assert generator.generate(pattern=_pattern(), decision=_allowed()) is not None

    def test_a_decision_without_triggers_produces_nothing(self, generator) -> None:
        """``allowed=True`` 但没有任何触发条件——自相矛盾的裁决，不产出。"""
        fabricated = PromotionDecision(allowed=True, triggers=())
        assert generator.generate(pattern=_pattern(), decision=fabricated) is None

    def test_a_fabricated_decision_cannot_promote_a_single_experience(self, generator) -> None:
        """🔴 **不变量 10 在生成侧的第二道保险，此前只是一句转发。**

        初版只检查 ``decision.allowed``，于是手工构造一个
        ``PromotionDecision(allowed=True)`` 加上**一条经验**的模式，
        就能生成一条可落库的提案——单次经验推广为全局策略。

        现在这里会核对"裁决声称命中的条件，证据撑不撑得起"：
        声称命中重复、却只有一条经验，那是被构造出来的裁决，必须响。
        """
        fabricated = PromotionDecision(
            allowed=True,
            triggers=(PromotionTrigger.REPEATED_SAME_ERROR,),
        )
        with pytest.raises(ValueError, match="门槛"):
            generator.generate(pattern=_pattern(count=1), decision=fabricated)

    def test_a_decision_without_a_pattern_produces_nothing(self, generator) -> None:
        """🔴 没有模式就没有支撑证据，提案无从构造。

        §11.3 的条件三（离线评测暴露稳定退化）可以在没有错误模式的情况下
        成立——`PromotionPolicy` 会判它 allowed。但提案的核心是它引用的
        那几条经验；一条没有证据的提案既不能被评估，也不该占用评审时间。

        初版在这里抛 ``AttributeError``（对 ``None`` 取 ``error_type``）。
        """
        decision = PromotionPolicy().decide(PromotionEvidence(offline_regression=True))
        assert decision.allowed is True
        assert PromotionTrigger.OFFLINE_REGRESSION in decision.triggers
        # 调用方手里没有模式可传——这正是 `pattern` 参数要允许 None 的原因
        assert generator.generate(pattern=None, decision=decision) is None


class TestDraftStatus:
    def test_status_is_draft(self, generator) -> None:
        proposal = generator.generate(pattern=_pattern(), decision=_allowed())
        assert proposal is not None
        assert proposal.status is ProposalStatus.DRAFT

    def test_proposal_is_not_terminal_and_cannot_become_active(self, generator) -> None:
        """🔴 不变量 11：生成路径上没有任何通往"生效"的出口。"""
        proposal = generator.generate(pattern=_pattern(), decision=_allowed())
        assert proposal is not None
        assert proposal.is_terminal is False
        assert proposal.can_become_active is False

    def test_no_status_other_than_draft_is_ever_assigned(self, generator) -> None:
        for error_type in ErrorType:
            decision = PromotionPolicy().decide(
                PromotionEvidence(pattern=_pattern(error_type), fix_direction="方向")
            )
            proposal = generator.generate(pattern=_pattern(error_type), decision=decision)
            if proposal is not None:
                assert proposal.status is ProposalStatus.DRAFT


class TestEvidenceIsCarried:
    def test_supporting_experience_ids_come_from_the_pattern(self, generator) -> None:
        pattern = _pattern()
        proposal = generator.generate(pattern=pattern, decision=_allowed())
        assert proposal is not None
        assert tuple(proposal.supporting_experience_ids) == pattern.experience_ids

    def test_counterexamples_are_carried_not_filtered(self, generator) -> None:
        """🔴 只带支持证据的提案，评审看到的是一份被裁剪过的事实。"""
        pattern = ErrorPattern(
            error_type=ErrorType.REASONING_ERROR,
            situation_signature=SIGNATURE,
            experience_ids=(uuid4(), uuid4(), uuid4()),
            occurrence_count=3,
            weighted_count=3,
            experience_count=3,
            counterexample_count=2,
        )
        decision = PromotionPolicy().decide(PromotionEvidence(pattern=pattern))
        proposal = generator.generate(pattern=pattern, decision=decision)
        assert proposal is not None
        assert proposal.counterexamples
        assert any("反例" in item for item in proposal.counterexamples)

    def test_no_counterexamples_yields_empty_list(self, generator) -> None:
        proposal = generator.generate(pattern=_pattern(), decision=_allowed())
        assert proposal is not None
        assert proposal.counterexamples == []

    def test_error_class_matches_the_pattern(self, generator) -> None:
        proposal = generator.generate(
            pattern=_pattern(ErrorType.CALIBRATION_ERROR),
            decision=_allowed(ErrorType.CALIBRATION_ERROR),
        )
        assert proposal is not None
        assert proposal.error_class is ErrorType.CALIBRATION_ERROR


class TestApprovalIsAlwaysRequired:
    def test_approval_level_requires_a_human(self, generator) -> None:
        proposal = generator.generate(pattern=_pattern(), decision=_allowed())
        assert proposal is not None
        assert proposal.approval_level is ApprovalLevel.USER_AND_REVIEW

    def test_approval_level_is_never_none(self, generator) -> None:
        """ "无需审批"的提案在 V0.1 里不存在。"""
        for error_type in ErrorType:
            decision = PromotionPolicy().decide(
                PromotionEvidence(pattern=_pattern(error_type), fix_direction="方向")
            )
            proposal = generator.generate(pattern=_pattern(error_type), decision=decision)
            if proposal is not None:
                assert proposal.approval_level is not ApprovalLevel.NONE


class TestContentPointsAtADirectionNotASolution:
    """🔴 措辞停在**方向**上：具体改法需要看到引用的经验才能定。"""

    def test_without_fix_direction_the_text_says_it_is_directional(self, generator) -> None:
        proposal = generator.generate(pattern=_pattern(), decision=_allowed())
        assert proposal is not None
        assert "方向性建议" in proposal.proposed_change
        assert _TARGET_COMPONENT[ErrorType.REASONING_ERROR] in proposal.proposed_change

    def test_with_fix_direction_it_is_used_verbatim(self, generator) -> None:
        decision = PromotionPolicy().decide(
            PromotionEvidence(
                pattern=_pattern(ErrorType.SCOPE_ERROR), fix_direction="在框定阶段显式重述问题范围"
            )
        )
        proposal = generator.generate(
            pattern=_pattern(ErrorType.SCOPE_ERROR),
            decision=decision,
            fix_direction="在框定阶段显式重述问题范围",
        )
        assert proposal is not None
        assert "在框定阶段显式重述问题范围" in proposal.proposed_change

    def test_unknown_error_asks_for_investigation_instead_of_proposing(self, generator) -> None:
        """类别都归不出来，任何改动方案都是无根据的。"""
        pattern = _pattern(ErrorType.UNKNOWN_ERROR)
        decision = PromotionPolicy().decide(
            PromotionEvidence(
                pattern=pattern, user_corrections=3, user_correction_shows_systemic_issue=True
            )
        )
        proposal = generator.generate(pattern=pattern, decision=decision)
        assert proposal is not None
        assert "待人工调查" in proposal.proposed_change
        assert "无根据" in proposal.proposed_change

    def test_expected_benefit_names_the_situation(self, generator) -> None:
        proposal = generator.generate(pattern=_pattern(), decision=_allowed())
        assert proposal is not None
        assert SIGNATURE in proposal.expected_benefit

    def test_applicability_is_the_situation(self, generator) -> None:
        proposal = generator.generate(pattern=_pattern(), decision=_allowed())
        assert proposal is not None
        assert proposal.applicability == [SIGNATURE]

    def test_component_mapping_is_a_hint_not_a_claim(self, generator) -> None:
        """目标组件由错误类别推出，两者都不足以断定"问题就在这个组件里"。"""
        proposal = generator.generate(pattern=_pattern(), decision=_allowed())
        assert proposal is not None
        # 措辞是「方向性建议」，不是「问题就在这里」
        assert "方向性建议" in proposal.proposed_change


class TestEvidenceBackedPlan:
    """§5.12 要求：成功指标**必须包含目标指标以外的场景**。"""

    def test_success_metrics_include_control_metrics(self, generator) -> None:
        proposal = generator.generate(pattern=_pattern(), decision=_allowed())
        assert proposal is not None
        assert any("对照" in item for item in proposal.success_metrics)
        assert any("目标" in item for item in proposal.success_metrics)

    def test_regressions_are_named(self, generator) -> None:
        proposal = generator.generate(pattern=_pattern(), decision=_allowed())
        assert proposal is not None
        assert any("其他情境" in item for item in proposal.possible_regressions)

    def test_evaluation_plan_compares_baseline_and_candidate(self, generator) -> None:
        proposal = generator.generate(pattern=_pattern(), decision=_allowed())
        assert proposal is not None
        joined = "".join(proposal.evaluation_plan)
        assert "Baseline" in joined and "Candidate" in joined

    def test_rollback_conditions_are_present(self, generator) -> None:
        proposal = generator.generate(pattern=_pattern(), decision=_allowed())
        assert proposal is not None
        assert proposal.rollback_conditions

    def test_evaluation_plan_admits_it_is_not_yet_concrete(self, generator) -> None:
        """阶段 7 之前没有 Golden Cases，计划里必须**说清楚**这一点。"""
        proposal = generator.generate(pattern=_pattern(), decision=_allowed())
        assert proposal is not None
        assert any("阶段 7" in item for item in proposal.evaluation_plan)


class TestTemplateCompleteness:
    """每一条错误类别都必须有落点，否则生成时会 ``KeyError``。"""

    def test_every_error_type_has_a_target_component(self) -> None:
        assert set(_TARGET_COMPONENT) == set(ErrorType)

    def test_every_error_type_has_a_problem_description(self) -> None:
        assert set(_OBSERVED_PROBLEM) == set(ErrorType)

    def test_descriptions_are_non_empty(self) -> None:
        assert all(_OBSERVED_PROBLEM[item].strip() for item in ErrorType)

    def test_generation_works_for_every_error_type(self, generator) -> None:
        for error_type in ErrorType:
            pattern = _pattern(error_type)
            decision = PromotionPolicy().decide(PromotionEvidence(pattern=pattern))
            assert generator.generate(pattern=pattern, decision=decision) is not None, error_type


class TestTriggeredBy:
    def test_reports_the_hit_triggers(self) -> None:
        decision = PromotionPolicy().decide(
            PromotionEvidence(pattern=_pattern(), offline_regression=True)
        )
        assert set(triggered_by(decision)) == {
            PromotionTrigger.REPEATED_SAME_ERROR.value,
            PromotionTrigger.OFFLINE_REGRESSION.value,
        }

    def test_empty_when_nothing_hit(self) -> None:
        assert triggered_by(PromotionPolicy().decide(PromotionEvidence())) == ()

    def test_values_are_plain_strings_for_the_event_payload(self) -> None:
        """事件负载要能只查事件就回答"这条提案为什么会出现"。"""
        decision = PromotionPolicy().decide(PromotionEvidence(pattern=_pattern()))
        assert all(isinstance(item, str) for item in triggered_by(decision))
