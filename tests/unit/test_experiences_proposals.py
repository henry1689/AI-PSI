"""经验与改进提案的单元测试。

🔴 覆盖受控迭代的两条核心不变量：

* **I10**：用户个体经验不能自动升级为全局策略；
* **I11**：ImprovementProposal 不能自动生效。

以及经验归因的关键区分："当时推理错了" vs "当时信息本就不足"。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_psi.domain.enums import ApprovalLevel, ConfidenceBand, ErrorType, ProposalStatus
from ai_psi.domain.improvement_proposals import (
    PROPOSAL_ESCALATION_THRESHOLD,
)

pytestmark = pytest.mark.unit


class TestInvariant11NoAutoPromotion:
    """🔴 不变量 11：ImprovementProposal 不能自动生效。"""

    def test_can_never_become_active(self, make_proposal) -> None:
        for status in ProposalStatus:
            assert make_proposal(status=status).can_become_active is False

    def test_default_status_is_draft(self, make_proposal) -> None:
        assert make_proposal().status is ProposalStatus.DRAFT

    def test_active_status_is_not_assignable(self, make_proposal) -> None:
        """枚举里没有 ACTIVE，赋值会直接失败。"""
        with pytest.raises(ValidationError):
            make_proposal(status="active")

    def test_terminal_statuses_are_final(self, make_proposal) -> None:
        assert make_proposal(status=ProposalStatus.REJECTED).is_terminal
        assert make_proposal(status=ProposalStatus.APPROVED_FOR_MANUAL_TRIAL).is_terminal

    def test_in_flight_statuses_are_not_terminal(self, make_proposal) -> None:
        for status in (
            ProposalStatus.DRAFT,
            ProposalStatus.PENDING_EVALUATION,
            ProposalStatus.EVALUATED,
        ):
            assert not make_proposal(status=status).is_terminal

    def test_default_approval_level_requires_a_human(self, make_proposal) -> None:
        assert make_proposal().approval_level is ApprovalLevel.USER_AND_REVIEW


class TestInvariant10EscalationThreshold:
    """🔴 不变量 10：单次经验不能推广为全局策略。"""

    def test_threshold_is_three(self) -> None:
        assert PROPOSAL_ESCALATION_THRESHOLD == 3

    def test_single_experience_does_not_meet_threshold(self, make_proposal) -> None:
        proposal = make_proposal(supporting_experience_ids=[uuid4()])
        assert not proposal.meets_escalation_threshold()

    def test_two_experiences_do_not_meet_threshold(self, make_proposal) -> None:
        proposal = make_proposal(supporting_experience_ids=[uuid4(), uuid4()])
        assert not proposal.meets_escalation_threshold()

    def test_three_experiences_meet_threshold(self, make_proposal) -> None:
        proposal = make_proposal(supporting_experience_ids=[uuid4(), uuid4(), uuid4()])
        assert proposal.meets_escalation_threshold()

    def test_threshold_below_two_is_rejected(self, make_proposal) -> None:
        """🔴 门槛为 1 等于允许单次经验推广——必须直接拒绝。"""
        proposal = make_proposal(supporting_experience_ids=[uuid4()])
        with pytest.raises(ValueError, match="不得低于 2"):
            proposal.meets_escalation_threshold(threshold=1)

    def test_duplicate_experiences_do_not_inflate_the_count(self, make_proposal) -> None:
        """去重：不能用同一条经验重复计数凑够门槛。"""
        same = uuid4()
        proposal = make_proposal(supporting_experience_ids=[same, same, same])
        assert not proposal.meets_escalation_threshold()

    def test_threshold_is_customizable_upward(self, make_proposal) -> None:
        proposal = make_proposal(supporting_experience_ids=[uuid4() for _ in range(5)])
        assert proposal.meets_escalation_threshold(threshold=5)
        assert not proposal.meets_escalation_threshold(threshold=6)


class TestProposalContent:
    def test_counterexamples_can_be_recorded(self, make_proposal) -> None:
        """改进不能只看支持证据。"""
        proposal = make_proposal(counterexamples=["该模式在简单问题上不成立"])
        assert proposal.counterexamples

    def test_possible_regressions_are_supported(self, make_proposal) -> None:
        """必须能表达"其他场景退化程度"——只改善目标指标可能是净负面。"""
        proposal = make_proposal(possible_regressions=["可能导致回答变长"])
        assert proposal.possible_regressions

    def test_evaluation_plan_is_expressible(self, make_proposal) -> None:
        proposal = make_proposal(
            evaluation_plan=["在历史回合上跑 Baseline 与 Candidate 对比"],
            success_metrics=["reasoning_error 复发率", "其他场景不得退化"],
        )
        assert proposal.success_metrics

    def test_required_fields(self, make_proposal) -> None:
        with pytest.raises(ValidationError):
            make_proposal(target_component="")
        with pytest.raises(ValidationError):
            make_proposal(proposed_change="")

    def test_invalid_error_class_rejected(self, make_proposal) -> None:
        with pytest.raises(ValidationError):
            make_proposal(error_class="bad_vibes")

    def test_all_error_types_accepted(self, make_proposal) -> None:
        for error_type in ErrorType:
            assert make_proposal(error_class=error_type).error_class is error_type


class TestExperience:
    """经验必须能区分"推理错了"与"当时信息不足"。"""

    def test_evidence_available_at_time_is_recorded(self, make_experience) -> None:
        """🔴 关键字段：没有它，系统会把信息缺失误判为推理错误。"""
        evidence = [uuid4(), uuid4()]
        exp = make_experience(evidence_available_at_time=evidence)
        assert exp.evidence_available_at_time == evidence

    def test_later_evidence_is_separate(self, make_experience) -> None:
        exp = make_experience(later_evidence_ids=[uuid4()])
        assert len(exp.later_evidence_ids) == 1

    def test_no_error_is_representable(self, make_experience) -> None:
        """不是每次经验都是错误——成功的经验同样有学习价值。"""
        assert make_experience(error_type=None).error_type is None

    def test_not_attributable_when_confidence_too_low(self, make_experience) -> None:
        exp = make_experience(
            error_type=ErrorType.REASONING_ERROR,
            attribution_confidence=ConfidenceBand.VERY_LOW,
        )
        assert not exp.is_attributable

    def test_not_attributable_without_error_type(self, make_experience) -> None:
        exp = make_experience(error_type=None, attribution_confidence=ConfidenceBand.HIGH)
        assert not exp.is_attributable

    def test_attributable_with_error_and_confidence(self, make_experience) -> None:
        exp = make_experience(
            error_type=ErrorType.REASONING_ERROR,
            attribution_confidence=ConfidenceBand.MODERATE,
        )
        assert exp.is_attributable

    def test_applicable_conditions_guard_against_overgeneralization(self, make_experience) -> None:
        exp = make_experience(
            applicable_conditions=["仅在信息不足的场景"],
            counterexamples=["信息充足时该策略不适用"],
        )
        assert exp.applicable_conditions and exp.counterexamples

    def test_situation_signature_is_required(self, make_experience) -> None:
        """没有签名就无法判定"同类错误"。"""
        with pytest.raises(ValidationError):
            make_experience(situation_signature="")


class TestProposalIsNotAnActivePath:
    """端到端的类型级检查：从任何入口都无法让提案生效。"""

    def test_no_attribute_enables_promotion(self, make_proposal) -> None:
        proposal = make_proposal()
        for attribute in ("activate", "promote", "apply", "deploy", "enable"):
            assert not hasattr(proposal, attribute), (
                f"ImprovementProposal 不应存在 {attribute} 方法——任何通往生效的路径都违反不变量 11"
            )

    def test_module_exposes_no_promotion_helper(self) -> None:
        import ai_psi.domain.improvement_proposals as module

        for name in dir(module):
            assert "activate" not in name.lower()
            assert "promote" not in name.lower()
