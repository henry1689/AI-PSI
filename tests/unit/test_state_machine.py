"""认知回合状态机的单元测试。

🔴 本文件逐条覆盖 ``docs/state_machine.md``：

* 每条**合法**转移被接受；
* 每条**禁止**转移被拒绝（不变量 17）；
* 终态冻结；
* 元认知决策只能落在允许的目标集合内（ADR-0010）；
* 每个状态都有超时定义（ADR-0008）。

文档与代码不一致时，本测试会失败——这正是它的价值。
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from ai_psi.cognition.state_machine import (
    STATE_TIMEOUTS_SECONDS,
    TERMINAL_STATES,
    TRANSITIONS,
    allowed_transitions,
    assert_transition,
    can_transition,
    is_valid_decision_target,
    targets_for_decision,
    timeout_for,
)
from ai_psi.domain.enums import MetacognitiveDecision, RoundState
from ai_psi.domain.exceptions import IllegalStateTransitionError

pytestmark = pytest.mark.unit

HAPPY_PATH = [
    RoundState.CREATED,
    RoundState.TRIAGING,
    RoundState.FRAMING,
    RoundState.RETRIEVING,
    RoundState.ANALYZING,
    RoundState.DELIBERATING,
    RoundState.METACOGNITIVE_REVIEW,
    RoundState.SYNTHESIZING,
    RoundState.RESPONDING,
    RoundState.COMPLETED,
]


class TestTransitionTableShape:
    def test_every_state_has_an_entry(self) -> None:
        """转移表必须覆盖全部状态——遗漏的状态会让校验抛 KeyError。"""
        assert set(TRANSITIONS) == set(RoundState)

    def test_every_state_has_a_timeout_entry(self) -> None:
        """任务书 §6.3：每个状态有超时（WAITING 之外由 reopen 条件驱动）。"""
        assert set(STATE_TIMEOUTS_SECONDS) == set(RoundState)

    def test_terminal_states_have_no_outgoing_edges(self) -> None:
        for state in TERMINAL_STATES:
            assert TRANSITIONS[state] == frozenset()

    def test_four_terminal_states(self) -> None:
        assert len(TERMINAL_STATES) == 4

    def test_no_self_loops(self) -> None:
        """状态转移不得原地打转——那会让超时与预算失去意义。"""
        for state, targets in TRANSITIONS.items():
            assert state not in targets, f"{state.value} 存在自环"


class TestHappyPath:
    def test_full_standard_path_is_legal(self) -> None:
        for from_state, to_state in pairwise(HAPPY_PATH):
            assert can_transition(from_state, to_state), (
                f"标准路径上的转移 {from_state.value} → {to_state.value} 被拒绝"
            )

    def test_assert_transition_does_not_raise_on_happy_path(self) -> None:
        for from_state, to_state in pairwise(HAPPY_PATH):
            assert_transition(from_state, to_state)


class TestForbiddenTransitions:
    """🔴 不变量 17：状态机不得跳过禁止跳过的状态。"""

    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            (RoundState.CREATED, RoundState.ANALYZING),
            (RoundState.CREATED, RoundState.SYNTHESIZING),
            (RoundState.CREATED, RoundState.COMPLETED),
            (RoundState.CREATED, RoundState.RESPONDING),
            (RoundState.TRIAGING, RoundState.ANALYZING),
            (RoundState.FRAMING, RoundState.ANALYZING),
            (RoundState.FRAMING, RoundState.SYNTHESIZING),
            (RoundState.RETRIEVING, RoundState.DELIBERATING),
            (RoundState.ANALYZING, RoundState.SYNTHESIZING),
            (RoundState.ANALYZING, RoundState.COMPLETED),
            (RoundState.DELIBERATING, RoundState.RESPONDING),
            (RoundState.SYNTHESIZING, RoundState.ANALYZING),
            (RoundState.RESPONDING, RoundState.ANALYZING),
            (RoundState.RESPONDING, RoundState.SYNTHESIZING),
            (RoundState.WAITING_FOR_EVIDENCE, RoundState.ANALYZING),
            (RoundState.WAITING_FOR_EVIDENCE, RoundState.SYNTHESIZING),
        ],
    )
    def test_forbidden_pair_is_rejected(self, from_state: RoundState, to_state: RoundState) -> None:
        assert not can_transition(from_state, to_state)
        with pytest.raises(IllegalStateTransitionError):
            assert_transition(from_state, to_state)

    def test_completed_must_be_reached_through_responding(self) -> None:
        for state in RoundState:
            if state is RoundState.RESPONDING:
                continue
            assert not can_transition(state, RoundState.COMPLETED), (
                f"{state.value} 不应能直接跳到 COMPLETED"
            )


#: 终态按值排序（RoundState 是 StrEnum，可直接按字符串比较），
#: 让 parametrize 的用例顺序稳定、可读。
TERMINAL_STATES_SORTED: list[RoundState] = sorted(TERMINAL_STATES)


class TestTerminalFreeze:
    @pytest.mark.parametrize("terminal", TERMINAL_STATES_SORTED)
    def test_no_transition_out_of_terminal(self, terminal: RoundState) -> None:
        for target in RoundState:
            assert not can_transition(terminal, target)

    def test_error_message_explains_reactivation(self) -> None:
        with pytest.raises(IllegalStateTransitionError, match="causation_id"):
            assert_transition(RoundState.COMPLETED, RoundState.ANALYZING)

    def test_error_carries_states(self) -> None:
        try:
            assert_transition(RoundState.CREATED, RoundState.COMPLETED)
        except IllegalStateTransitionError as exc:
            assert exc.from_state == "created"
            assert exc.to_state == "completed"
        else:  # pragma: no cover
            pytest.fail("应当抛出 IllegalStateTransitionError")

    def test_error_lists_allowed_targets(self) -> None:
        with pytest.raises(IllegalStateTransitionError, match="triaging"):
            assert_transition(RoundState.CREATED, RoundState.COMPLETED)


class TestTriageShortcut:
    """关切识别为空时的快捷路径。"""

    def test_triaging_may_jump_to_synthesizing(self) -> None:
        assert can_transition(RoundState.TRIAGING, RoundState.SYNTHESIZING)

    def test_only_triaging_has_this_shortcut(self) -> None:
        """其他状态不得直接跳到 SYNTHESIZING。"""
        sources = [state for state in RoundState if can_transition(state, RoundState.SYNTHESIZING)]
        assert set(sources) == {
            RoundState.TRIAGING,
            RoundState.METACOGNITIVE_REVIEW,
        }


class TestWaitingForEvidence:
    def test_only_evidence_or_cancel_can_wake_it(self) -> None:
        assert allowed_transitions(RoundState.WAITING_FOR_EVIDENCE) == frozenset(
            {RoundState.RETRIEVING, RoundState.CANCELLED}
        )

    def test_it_has_no_timeout(self) -> None:
        """等待由 reopen_conditions 或用户驱动，不设超时。"""
        assert timeout_for(RoundState.WAITING_FOR_EVIDENCE) is None


class TestMetacognitiveDecisionTargets:
    """ADR-0010：元认知决策只能落在允许的目标集合内。"""

    def test_stop_leads_to_synthesizing(self) -> None:
        assert is_valid_decision_target(MetacognitiveDecision.STOP, RoundState.SYNTHESIZING)

    def test_wait_leads_to_waiting(self) -> None:
        assert is_valid_decision_target(MetacognitiveDecision.WAIT, RoundState.WAITING_FOR_EVIDENCE)

    def test_escalate_leads_to_suspended(self) -> None:
        assert is_valid_decision_target(
            MetacognitiveDecision.ESCALATE_TO_RESEARCH, RoundState.SUSPENDED
        )

    def test_continue_leads_to_analyzing_or_deliberating(self) -> None:
        targets = targets_for_decision(MetacognitiveDecision.CONTINUE)
        assert targets == {RoundState.ANALYZING, RoundState.DELIBERATING}

    def test_change_method_leads_to_retrieving_or_analyzing(self) -> None:
        targets = targets_for_decision(MetacognitiveDecision.CHANGE_METHOD)
        assert targets == {RoundState.RETRIEVING, RoundState.ANALYZING}

    def test_narrow_scope_leads_to_deliberating(self) -> None:
        assert is_valid_decision_target(MetacognitiveDecision.NARROW_SCOPE, RoundState.DELIBERATING)

    def test_lower_confidence_leads_to_synthesizing(self) -> None:
        assert is_valid_decision_target(
            MetacognitiveDecision.LOWER_CONFIDENCE, RoundState.SYNTHESIZING
        )

    def test_request_evidence_leads_to_retrieving(self) -> None:
        assert is_valid_decision_target(
            MetacognitiveDecision.REQUEST_EVIDENCE, RoundState.RETRIEVING
        )

    def test_decision_cannot_target_an_arbitrary_state(self) -> None:
        """决策不能指定任意状态——只有子集合法。"""
        for decision in MetacognitiveDecision:
            assert not is_valid_decision_target(decision, RoundState.CREATED)
            assert not is_valid_decision_target(decision, RoundState.COMPLETED)
            assert not is_valid_decision_target(decision, RoundState.RESPONDING)

    def test_every_decision_has_at_least_one_target(self) -> None:
        for decision in MetacognitiveDecision:
            assert targets_for_decision(decision)

    def test_every_decision_target_is_reachable_from_review(self) -> None:
        """决策给出的目标必须同时是 METACOGNITIVE_REVIEW 的合法出口。"""
        for decision in MetacognitiveDecision:
            for target in targets_for_decision(decision):
                assert can_transition(RoundState.METACOGNITIVE_REVIEW, target), (
                    f"{decision.value} 指向 {target.value}，"
                    "但该状态不能从 METACOGNITIVE_REVIEW 到达"
                )


class TestTimeouts:
    """ADR-0008 的状态超时表。"""

    def test_active_states_have_positive_timeouts(self) -> None:
        for state in RoundState:
            timeout = timeout_for(state)
            if timeout is not None:
                assert timeout > 0, f"{state.value} 的超时必须为正数"

    def test_terminal_states_have_no_timeout(self) -> None:
        for state in TERMINAL_STATES:
            assert timeout_for(state) is None

    def test_metacognitive_review_has_a_timeout(self) -> None:
        """该状态超时不判失败，而是强制 STOP（ADR-0008）。"""
        assert timeout_for(RoundState.METACOGNITIVE_REVIEW) is not None

    def test_analyzing_has_a_bounded_timeout(self) -> None:
        assert timeout_for(RoundState.ANALYZING) == 60
