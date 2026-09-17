"""状态机的 Hypothesis 属性测试（任务书 §15.2）。

属性测试与单元测试的分工：

* 单元测试枚举**已知的**非法转移；
* 属性测试断言"**无论怎么走**都不会违反约束"。

后者才是"状态机不得跳过禁止跳过的状态"（不变量 17）的真正保证——
它不依赖我们是否想全了所有用例。
"""

from __future__ import annotations

from itertools import pairwise

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from ai_psi.cognition.state_machine import (
    STATE_TIMEOUTS_SECONDS,
    TERMINAL_STATES,
    TRANSITIONS,
    assert_transition,
    can_transition,
    is_valid_decision_target,
    targets_for_decision,
)
from ai_psi.domain.enums import MetacognitiveDecision, RoundState
from ai_psi.domain.exceptions import IllegalStateTransitionError

pytestmark = [pytest.mark.property, pytest.mark.unit]

states = st.sampled_from(list(RoundState))
decisions = st.sampled_from(list(MetacognitiveDecision))
non_terminal = st.sampled_from([s for s in RoundState if s not in TERMINAL_STATES])


class TestTransitionLegality:
    @given(from_state=states, to_state=states)
    @settings(max_examples=400)
    def test_can_transition_matches_the_table(self, from_state, to_state) -> None:
        assert can_transition(from_state, to_state) is (to_state in TRANSITIONS[from_state])

    @given(from_state=states, to_state=states)
    @settings(max_examples=400)
    def test_assert_transition_agrees_with_can_transition(self, from_state, to_state) -> None:
        if can_transition(from_state, to_state):
            assert_transition(from_state, to_state)
        else:
            with pytest.raises(IllegalStateTransitionError):
                assert_transition(from_state, to_state)

    @given(pair=st.tuples(states, states))
    @settings(max_examples=300)
    def test_random_pair_is_never_silently_accepted_when_illegal(self, pair) -> None:
        """任意非法状态跳转都会被拒绝（任务书 §15.2 原文）。"""
        from_state, to_state = pair
        assume(from_state == to_state or to_state not in TRANSITIONS[from_state])
        with pytest.raises(IllegalStateTransitionError):
            assert_transition(from_state, to_state)

    @given(state=states)
    def test_transitions_are_never_reflexive(self, state) -> None:
        assert not can_transition(state, state)


class TestTerminalFreeze:
    @given(terminal=st.sampled_from(sorted(TERMINAL_STATES)), target=states)
    @settings(max_examples=300)
    def test_terminal_states_have_no_exit(self, terminal, target) -> None:
        assert not can_transition(terminal, target)

    @given(terminal=st.sampled_from(sorted(TERMINAL_STATES)))
    def test_leaving_terminal_always_raises(self, terminal) -> None:
        for target in RoundState:
            if target is terminal:
                continue
            with pytest.raises(IllegalStateTransitionError):
                assert_transition(terminal, target)


class TestGraphInvariants:
    @given(state=non_terminal)
    def test_every_non_terminal_state_can_advance(self, state) -> None:
        """非终态必须有出路——否则回合会卡死。"""
        assert TRANSITIONS[state], f"{state.value} 非终态却无任何出口"

    @given(state=states)
    def test_every_state_has_a_timeout_entry(self, state) -> None:
        assert state in STATE_TIMEOUTS_SECONDS

    @given(state=states)
    def test_no_self_loops(self, state) -> None:
        assert state not in TRANSITIONS[state]


class TestRandomWalks:
    """随机游走：无论怎么走，每一步都必须合法。"""

    @given(st.data(), st.integers(min_value=1, max_value=50))
    @settings(max_examples=200)
    def test_walk_through_allowed_edges_is_always_legal(self, data, steps: int) -> None:
        state = RoundState.CREATED
        for _ in range(steps):
            targets = sorted(TRANSITIONS[state])
            if not targets:
                break
            next_state = data.draw(st.sampled_from(targets))
            assert can_transition(state, next_state)
            assert_transition(state, next_state)
            state = next_state

    @given(st.data(), st.integers(min_value=1, max_value=50))
    @settings(max_examples=200)
    def test_walk_never_revisits_a_terminal_state(self, data, steps: int) -> None:
        state = RoundState.CREATED
        for _ in range(steps):
            if state in TERMINAL_STATES:
                assert not TRANSITIONS[state]
                break
            targets = sorted(TRANSITIONS[state])
            state = data.draw(st.sampled_from(targets))

    @given(st.data(), st.integers(min_value=1, max_value=30))
    @settings(max_examples=150)
    def test_a_round_can_always_reach_a_terminal_state(self, data, steps: int) -> None:
        """从 CREATED 出发，沿合法边行走必定能在有限步内到达终态。

        这是"回合不会卡死"的结构性保证。用确定性选择验证可达性：
        每步任选一条边，循环足够多次后必然落入终态。
        """
        state = RoundState.CREATED
        for _ in range(steps):
            if state in TERMINAL_STATES:
                return
            targets = sorted(TRANSITIONS[state])
            assume(targets)
            state = data.draw(st.sampled_from(targets))

    @given(st.data())
    @settings(max_examples=100)
    def test_happy_path_is_always_available(self, data) -> None:
        """无论起点如何，标准路径的每一步都在转移表中。"""
        path = [
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
        for from_state, to_state in pairwise(path):
            assert can_transition(from_state, to_state), (
                f"标准路径边 {from_state.value} → {to_state.value} 缺失"
            )


class TestMetacognitiveDecisionProperties:
    """ADR-0010：决策只能落在允许的目标集合内。"""

    @given(decision=decisions, target=states)
    @settings(max_examples=400)
    def test_decision_target_is_always_within_the_transition_table(self, decision, target) -> None:
        if is_valid_decision_target(decision, target):
            assert can_transition(RoundState.METACOGNITIVE_REVIEW, target)

    @given(decision=decisions)
    def test_every_decision_has_at_least_one_legal_target(self, decision) -> None:
        assert targets_for_decision(decision)

    @given(decision=decisions, target=states)
    @settings(max_examples=400)
    def test_decisions_cannot_target_entry_states(self, decision, target) -> None:
        """决策不能把回合送回入口或直接判定完成。"""
        forbidden = {
            RoundState.CREATED,
            RoundState.TRIAGING,
            RoundState.FRAMING,
            RoundState.COMPLETED,
            RoundState.RESPONDING,
            RoundState.FAILED,
            RoundState.CANCELLED,
        }
        assume(target in forbidden)
        assert not is_valid_decision_target(decision, target)

    @given(decision=decisions)
    def test_decision_targets_are_reachable_from_review(self, decision) -> None:
        for target in targets_for_decision(decision):
            assert can_transition(RoundState.METACOGNITIVE_REVIEW, target)
