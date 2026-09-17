"""枚举的单元测试。

重点不在"枚举有这些成员"，而在**枚举的形状本身就编码了不变量**：

* ``ProposalStatus`` 没有 ``ACTIVE`` → 不变量 11 获得类型级保证；
* ``UserModelStatus`` 没有 ``CONFIRMED`` → 不变量 13 同理；
* ``EpistemicAction.allows_strong_conclusion`` 只有 ``ANSWER`` 为真
  → 不变量 7 的实现基础。

这些测试的价值在于：**任何人试图给枚举加回这些成员时，测试会失败。**
"""

from __future__ import annotations

import pytest

from ai_psi.domain.enums import (
    CognitiveDepth,
    ConfidenceBand,
    EpistemicAction,
    EventType,
    HypothesisCategory,
    HypothesisStatus,
    MemoryStatus,
    MetacognitiveDecision,
    OrdinalLevel,
    ProposalStatus,
    RoundState,
    SensitivityLevel,
    UserModelStatus,
)

pytestmark = pytest.mark.unit


class TestOrdinalScales:
    def test_ordinal_rank_is_monotonic(self) -> None:
        ranks = [level.rank for level in OrdinalLevel]
        assert ranks == sorted(ranks)
        assert ranks == [0, 1, 2, 3, 4]

    def test_at_least(self) -> None:
        assert OrdinalLevel.HIGH.at_least(OrdinalLevel.MODERATE)
        assert OrdinalLevel.MODERATE.at_least(OrdinalLevel.MODERATE)
        assert not OrdinalLevel.LOW.at_least(OrdinalLevel.MODERATE)

    def test_confidence_band_rank(self) -> None:
        assert ConfidenceBand.VERY_HIGH.rank == 4
        assert ConfidenceBand.VERY_LOW.rank == 0
        assert ConfidenceBand.HIGH.at_least(ConfidenceBand.MODERATE)


class TestProposalStatusHasNoActive:
    """🔴 不变量 11：提案永不自动生效——由类型系统保证。"""

    def test_no_active_like_member_exists(self) -> None:
        forbidden = {"active", "applied", "promoted", "live", "enabled"}
        actual = {status.value for status in ProposalStatus}
        assert not (actual & forbidden), (
            "ProposalStatus 出现了表示'已生效'的成员，这会打开自动生效的路径（不变量 11）"
        )

    def test_terminal_states(self) -> None:
        assert ProposalStatus.REJECTED.is_terminal
        assert ProposalStatus.APPROVED_FOR_MANUAL_TRIAL.is_terminal
        assert not ProposalStatus.DRAFT.is_terminal
        assert not ProposalStatus.PENDING_EVALUATION.is_terminal
        assert not ProposalStatus.EVALUATED.is_terminal

    def test_approved_is_a_trial_approval_not_activation(self) -> None:
        """``APPROVED_FOR_MANUAL_TRIAL`` 的语义是"人工试验"，不是"上线"。"""
        assert ProposalStatus.APPROVED_FOR_MANUAL_TRIAL.value == "approved_for_manual_trial"


class TestUserModelStatusHasNoConfirmed:
    """🔴 不变量 13：用户模型的推测不得标记为确认事实。"""

    def test_no_confirmed_member_exists(self) -> None:
        forbidden = {"confirmed", "verified", "fact", "truth"}
        actual = {status.value for status in UserModelStatus}
        assert not (actual & forbidden), "UserModelStatus 不得包含表示'已确认'的成员（不变量 13）"

    def test_highest_is_user_stated(self) -> None:
        """最高只能到"用户自己这么说过"——这仍不等于关于用户的事实。"""
        assert UserModelStatus.USER_STATED.value == "user_stated"


class TestHypothesisStatusHasNoFactState:
    """🔴 不变量 1：假设不能变成已确认事实。"""

    def test_no_confirmed_fact_member(self) -> None:
        forbidden = {"confirmed", "fact", "verified", "established"}
        actual = {status.value for status in HypothesisStatus}
        assert not (actual & forbidden), "HypothesisStatus 不得包含'已确认事实'类状态（不变量 1）"

    def test_rejected_and_unresolved_exist(self) -> None:
        """任务书 §5.8 要求必须允许 REJECTED 与 UNRESOLVED。"""
        assert HypothesisStatus.REJECTED in HypothesisStatus
        assert HypothesisStatus.UNRESOLVED in HypothesisStatus
        assert HypothesisStatus.CANDIDATE.value == "candidate"


class TestHypothesisCategory:
    """任务书 §9.6：必须能识别"非人格化、非心理化解释"。"""

    def test_non_agentic_categories_recognized(self) -> None:
        assert HypothesisCategory.NON_AGENTIC.is_non_agentic
        assert HypothesisCategory.MECHANISTIC.is_non_agentic
        assert HypothesisCategory.SYSTEMIC.is_non_agentic
        assert HypothesisCategory.CONTEXTUAL.is_non_agentic

    def test_intentional_is_agentic(self) -> None:
        """涉及某方目的的假设**不算**非人格化解释。"""
        assert not HypothesisCategory.INTENTIONAL.is_non_agentic

    def test_at_least_one_non_agentic_category_exists(self) -> None:
        assert any(cat.is_non_agentic for cat in HypothesisCategory)


class TestEpistemicAction:
    """ADR-0010 与不变量 7。"""

    def test_only_answer_allows_strong_conclusion(self) -> None:
        strong = {a for a in EpistemicAction if a.allows_strong_conclusion}
        assert strong == {EpistemicAction.ANSWER}

    def test_cautious_actions_do_not_allow_strong_conclusion(self) -> None:
        for action in (
            EpistemicAction.ANSWER_WITH_CAVEAT,
            EpistemicAction.REQUEST_EVIDENCE,
            EpistemicAction.WAIT,
            EpistemicAction.DEFER,
            EpistemicAction.OUT_OF_SCOPE,
        ):
            assert not action.allows_strong_conclusion

    def test_out_of_scope_exists(self) -> None:
        """任务书 §9.10：'事实无法决定价值选择'必须是合法的认知结论。"""
        assert EpistemicAction.OUT_OF_SCOPE.value == "out_of_scope"


class TestMetacognitiveDecision:
    def test_eight_members(self) -> None:
        assert len(MetacognitiveDecision) == 8

    def test_stop_and_escalate_exist(self) -> None:
        assert MetacognitiveDecision.STOP in MetacognitiveDecision
        assert MetacognitiveDecision.ESCALATE_TO_RESEARCH in MetacognitiveDecision


class TestRoundState:
    def test_four_terminal_states(self) -> None:
        terminal = {state for state in RoundState if state.is_terminal}
        assert terminal == {
            RoundState.COMPLETED,
            RoundState.SUSPENDED,
            RoundState.FAILED,
            RoundState.CANCELLED,
        }

    def test_fourteen_states(self) -> None:
        assert len(RoundState) == 14

    def test_degraded_is_not_a_round_state(self) -> None:
        """🔴 ADR-0012：DEGRADED 是系统/Provider 健康状态，不进回合状态机。"""
        assert "degraded" not in {state.value for state in RoundState}


class TestMemoryStatus:
    """🔴 不变量 6：被取代的记忆不能作为默认有效记忆返回。"""

    def test_superseded_not_default_retrievable(self) -> None:
        assert not MemoryStatus.SUPERSEDED.is_default_retrievable

    def test_deleted_not_default_retrievable(self) -> None:
        assert not MemoryStatus.DELETED.is_default_retrievable

    def test_expired_not_default_retrievable(self) -> None:
        assert not MemoryStatus.EXPIRED.is_default_retrievable

    def test_proposed_not_default_retrievable(self) -> None:
        """未获批准的提案不得被检索到。"""
        assert not MemoryStatus.PROPOSED.is_default_retrievable

    def test_active_and_disputed_are_retrievable(self) -> None:
        assert MemoryStatus.ACTIVE.is_default_retrievable
        assert MemoryStatus.DISPUTED.is_default_retrievable


class TestSensitivityLevel:
    def test_sensitive_requires_user_confirmation(self) -> None:
        assert SensitivityLevel.SENSITIVE.requires_user_confirmation
        assert SensitivityLevel.HIGHLY_SENSITIVE.requires_user_confirmation

    def test_lower_levels_do_not(self) -> None:
        assert not SensitivityLevel.PUBLIC.requires_user_confirmation
        assert not SensitivityLevel.INTERNAL.requires_user_confirmation
        assert not SensitivityLevel.PERSONAL.requires_user_confirmation


class TestCognitiveDepth:
    def test_levels_are_sequential(self) -> None:
        assert [d.level for d in CognitiveDepth] == [0, 1, 2, 3, 4]

    def test_at_least(self) -> None:
        assert CognitiveDepth.D4.at_least(CognitiveDepth.D2)
        assert not CognitiveDepth.D1.at_least(CognitiveDepth.D3)


class TestEventType:
    def test_member_count_is_pinned(self) -> None:
        """条数是**刻意钉死**的：多一条少一条都应当是有意的决定。

        任务书 §5.2 列了 31 条，本系统有 34 条，多出的三条各有原因
        （``cognitive_round.cancelled`` 见 ADR-0012，
        ``cognition.analysis.completed`` 见 ADR-0015，
        ``memory.exported`` 见 ADR-0017：导出同样是数据访问，必须有痕迹）。
        """
        assert len(EventType) == 34

    def test_values_are_dotted_namespaced(self) -> None:
        for event_type in EventType:
            assert "." in event_type.value
            assert event_type.value == event_type.value.lower()

    def test_every_terminal_round_state_has_an_event_type(self) -> None:
        """⚠️ 补齐 ``cognitive_round.cancelled`` 的原因（ADR-0012）。

        没有它，取消回合将不留任何审计轨迹。
        """
        terminal_event_values = {
            "completed": EventType.COGNITIVE_ROUND_COMPLETED,
            "suspended": EventType.COGNITIVE_ROUND_SUSPENDED,
            "failed": EventType.COGNITIVE_ROUND_FAILED,
            "cancelled": EventType.COGNITIVE_ROUND_CANCELLED,
        }
        for state in RoundState:
            if state.is_terminal:
                assert state.value in terminal_event_values, (
                    f"终态 {state.value} 没有对应的事件类型"
                )
