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
from typing import ClassVar

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


class TestTheTimeoutTableIsPinned:
    """🔴 阶段 6.5 §六 的变异测试发现：**超时表的数值没有任何测试保护。**

    `TestGraphInvariants.test_every_state_has_a_timeout_entry` 只检查
    "每个状态**有**条目"，不检查"那个条目是**几**"。
    于是把 `CREATED: 5` 改成 `4`、`ANALYZING: 60` 改成 `59`……
    16 个变异体全部存活——**整张 ADR-0008 的超时表可以被改光而无人察觉**。

    超时不是装饰：它决定一个卡住的回合多久被判超时。
    这张表还写着一条设计决定——元认知超时**不判失败**，
    而是降级为强制 STOP（注释里的原话）。数值被改动时，
    那条决定的前提就没了。

    因此这里把**每一个数值**单独钉住。刻意写成一张独立的字面量表，
    而不是从 `STATE_TIMEOUTS_SECONDS` 抄——抄一遍的测试是自指的，
    改实现时它会跟着一起改。
    """

    #: ADR-0008 的超时表。**独立于实现手写。**
    _EXPECTED: ClassVar[dict[str, int | None]] = {
        "created": 5,
        "triaging": 15,
        "framing": 30,
        "retrieving": 45,
        "analyzing": 60,
        "deliberating": 60,
        "metacognitive_review": 30,
        "synthesizing": 45,
        "responding": 30,
        "waiting_for_evidence": None,
        "completed": None,
        "suspended": None,
        "failed": None,
        "cancelled": None,
    }

    def test_every_value_matches_the_documented_table(self) -> None:
        actual = {state.value: timeout for state, timeout in STATE_TIMEOUTS_SECONDS.items()}
        assert actual == self._EXPECTED

    def test_timeout_for_returns_the_table_value(self) -> None:
        """`timeout_for` 是这张表唯一的出口——它也要走一遍。"""
        for state in RoundState:
            assert timeout_for(state) == self._EXPECTED[state.value]

    def test_terminal_states_have_no_timeout(self) -> None:
        """🔴 终态不设超时：它们已经不流动了，超时对它们没有意义。

        这条与上一条看着重复，但意图不同——上一条防的是"数值被改"，
        这一条防的是"给终态加一个超时"。
        """
        terminal = {state for state in RoundState if not allowed_transitions(state)}
        assert terminal, "至少要有终态，否则这条断言是空转的"
        for state in terminal:
            assert timeout_for(state) is None, state


class TestADecisionTargetNeedsBothConditions:
    """🔴 变异测试发现的**真实逻辑缺口**。

    `is_valid_decision_target` 收尾是：

    ```
    return to_state in _METACOGNITIVE_TARGETS[decision] and can_transition(
        RoundState.METACOGNITIVE_REVIEW, to_state
    )
    ```

    把 `and` 换成 `or` 之后，**只要目标出现在该决策的映射表里就算合法**，
    哪怕状态机根本不允许从 `METACOGNITIVE_REVIEW` 转到那里——
    两层校验塌缩成一层。

    原先的用例只试了"两个条件都满足"与"两个条件都不满足"这两种输入，
    而 `and` 与 `or` 在这两种输入上给出相同答案。
    **它们只在"恰好满足一个"时才分得开。**
    """

    def test_a_target_outside_the_decision_map_is_refused(self) -> None:
        """条件一不满足、条件二满足 → 必须拒绝。"""
        illegal = next(
            state
            for state in RoundState
            if can_transition(RoundState.METACOGNITIVE_REVIEW, state)
            and state not in _metacognitive_targets_for(MetacognitiveDecision.CONTINUE)
        )
        assert not is_valid_decision_target(MetacognitiveDecision.CONTINUE, illegal), illegal

    def test_the_decision_map_is_a_subset_of_the_machine_edges(self) -> None:
        """🔴 **两层校验不是彼此独立的**——这是一个值得钉住的事实。

        实测：每一个元认知决策的映射都是状态机
        「从 ``METACOGNITIVE_REVIEW`` 出发的合法边」的**子集**。
        因此 `and` 与 `or` 只在一种输入上分得开：
        **一个可以从 ``METACOGNITIVE_REVIEW`` 到达、但不在该决策
        映射里的目标**——那正是上面那条用例的输入。

        这条断言把这个子集关系写成可执行的记录。将来若有人给
        某个决策加上一个状态机不允许的目标，它会立刻变红，
        而那时"两层校验"才真的开始各自起作用。
        """
        for decision in MetacognitiveDecision:
            outside = [
                state
                for state in targets_for_decision(decision)
                if not can_transition(RoundState.METACOGNITIVE_REVIEW, state)
            ]
            assert outside == [], (decision.value, outside)

    def test_a_fully_legal_target_is_accepted(self) -> None:
        """反方向：两层都满足时必须放行——否则这条检查退化成"总是拒绝"。"""
        legal = [
            state
            for state in _metacognitive_targets_for(MetacognitiveDecision.CONTINUE)
            if can_transition(RoundState.METACOGNITIVE_REVIEW, state)
        ]
        assert legal, "该决策没有合法目标"
        for state in legal:
            assert is_valid_decision_target(MetacognitiveDecision.CONTINUE, state), state


def _metacognitive_targets_for(decision: MetacognitiveDecision) -> frozenset[RoundState]:
    """取某个元认知决策允许的目标集合。

    ⚠️ 走 `targets_for_decision` 这个公开入口，不碰私有表——
    本文件测的是行为，不是那个表本身。
    """
    return targets_for_decision(decision)
