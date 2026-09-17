"""元认知的**规则层**裁决（任务书 §9.11、§13.3）。

🔴 规则层是纯函数，因此可以逐个条件地测。
它也是"模型说什么都不算"的那一层——把它的每条分支钉死，
比测一个端到端的回合更能说明问题。
"""

from __future__ import annotations

import pytest

from ai_psi.cognition.metacognition import (
    EXTRA_LOOP_MODEL_CALLS,
    POST_LOOP_TAIL_CALLS,
    RuleDecision,
    StopReason,
    decide,
    resolve_next_state,
    stop_condition_reached,
)
from ai_psi.domain.enums import ConfidenceBand, MetacognitiveDecision, RoundState

pytestmark = pytest.mark.unit

THRESHOLD = 0.85


def _decide(**overrides: object) -> RuleDecision:
    kwargs: dict[str, object] = {
        "repeated_claim_score_value": 0.0,
        "new_evidence_present": True,
        "new_reasoning_path_present": True,
        "scope_drift_detected": False,
        "stop_condition_reached": False,
        "loops_remaining": 1,
        "can_afford_extra_loop": True,
        "proposed": MetacognitiveDecision.CONTINUE,
        "repeat_threshold": THRESHOLD,
    }
    kwargs.update(overrides)
    return decide(**kwargs)  # type: ignore[arg-type]


class TestRuminationRule:
    """反刍硬规则：无新证据 + 无新路径 + 重复度超阈值 → 强制 STOP。"""

    def test_all_three_conditions_force_stop(self) -> None:
        result = _decide(
            new_evidence_present=False,
            new_reasoning_path_present=False,
            repeated_claim_score_value=1.0,
        )
        assert result.decision is MetacognitiveDecision.STOP
        assert result.stop_reason == StopReason.NO_MARGINAL_COGNITIVE_GAIN.value
        assert result.forced is True

    def test_stop_reason_literal_matches_task_book(self) -> None:
        """§13.3 指定了字面量，改动它会破坏指标口径。"""
        assert StopReason.NO_MARGINAL_COGNITIVE_GAIN.value == "NO_MARGINAL_COGNITIVE_GAIN"

    def test_new_evidence_prevents_forced_stop(self) -> None:
        result = _decide(
            new_evidence_present=True,
            new_reasoning_path_present=False,
            repeated_claim_score_value=1.0,
        )
        assert result.forced is False

    def test_new_reasoning_path_prevents_forced_stop(self) -> None:
        result = _decide(
            new_evidence_present=False,
            new_reasoning_path_present=True,
            repeated_claim_score_value=1.0,
        )
        assert result.forced is False

    def test_below_threshold_does_not_force_stop(self) -> None:
        result = _decide(
            new_evidence_present=False,
            new_reasoning_path_present=False,
            repeated_claim_score_value=THRESHOLD - 0.01,
        )
        assert result.forced is False

    def test_rumination_wins_over_everything_else(self) -> None:
        """反刍检查排在最前：等循环次数用尽才发现，预算已经花掉了。"""
        result = _decide(
            new_evidence_present=False,
            new_reasoning_path_present=False,
            repeated_claim_score_value=1.0,
            scope_drift_detected=True,
            loops_remaining=0,
        )
        assert result.stop_reason == StopReason.NO_MARGINAL_COGNITIVE_GAIN.value


class TestOtherForcedStops:
    def test_scope_drift_forces_stop(self) -> None:
        result = _decide(scope_drift_detected=True)
        assert result.decision is MetacognitiveDecision.STOP
        assert result.stop_reason == StopReason.SCOPE_DRIFT_DETECTED.value
        assert result.forced is True

    def test_loop_limit_forces_stop(self) -> None:
        result = _decide(loops_remaining=0)
        assert result.stop_reason == StopReason.METACOGNITIVE_LOOP_LIMIT.value
        assert result.forced is True

    def test_budget_constraint_forces_stop(self) -> None:
        result = _decide(loops_remaining=1, can_afford_extra_loop=False)
        assert result.stop_reason == StopReason.BUDGET_CONSTRAINT.value
        assert result.forced is True

    def test_satisfied_stop_condition_forces_stop(self) -> None:
        result = _decide(stop_condition_reached=True, proposed=MetacognitiveDecision.CONTINUE)
        assert result.stop_reason == StopReason.INQUIRY_STOP_CONDITION_SATISFIED.value
        assert result.forced is True

    def test_satisfied_stop_condition_does_not_block_a_stop_proposal(self) -> None:
        result = _decide(stop_condition_reached=True, proposed=MetacognitiveDecision.STOP)
        assert result.forced is False
        assert result.stop_reason == StopReason.MODEL_DECISION_STOP.value


class TestModelProposalIsHonoured:
    def test_stop_proposal_is_accepted(self) -> None:
        result = _decide(proposed=MetacognitiveDecision.STOP)
        assert result.decision is MetacognitiveDecision.STOP
        assert result.forced is False

    def test_continue_is_allowed_when_affordable(self) -> None:
        result = _decide(proposed=MetacognitiveDecision.CONTINUE)
        assert result.decision is MetacognitiveDecision.CONTINUE
        assert result.stop_reason is None
        assert result.forced is False

    def test_wait_maps_to_awaiting_evidence(self) -> None:
        result = _decide(proposed=MetacognitiveDecision.WAIT)
        assert result.stop_reason == StopReason.AWAITING_EVIDENCE.value

    def test_escalation_maps_to_research(self) -> None:
        result = _decide(proposed=MetacognitiveDecision.ESCALATE_TO_RESEARCH)
        assert result.stop_reason == StopReason.ESCALATED_TO_RESEARCH.value

    def test_lower_confidence_still_synthesizes(self) -> None:
        result = _decide(proposed=MetacognitiveDecision.LOWER_CONFIDENCE)
        assert result.decision is MetacognitiveDecision.LOWER_CONFIDENCE
        assert result.stop_reason == StopReason.LOWERED_CONFIDENCE.value


class TestNextStateMapping:
    def test_continue_returns_to_deliberating(self) -> None:
        """默认路径是"用现有材料重新合成"，而不是把所有分析重跑一遍——
        后者会瞬间吃掉整个预算。"""
        assert resolve_next_state(MetacognitiveDecision.CONTINUE) is RoundState.DELIBERATING

    def test_stop_reaches_synthesis(self) -> None:
        assert resolve_next_state(MetacognitiveDecision.STOP) is RoundState.SYNTHESIZING

    def test_lower_confidence_reaches_synthesis(self) -> None:
        assert resolve_next_state(MetacognitiveDecision.LOWER_CONFIDENCE) is RoundState.SYNTHESIZING

    def test_wait_reaches_waiting_state(self) -> None:
        assert resolve_next_state(MetacognitiveDecision.WAIT) is RoundState.WAITING_FOR_EVIDENCE

    def test_escalation_reaches_suspended(self) -> None:
        assert (
            resolve_next_state(MetacognitiveDecision.ESCALATE_TO_RESEARCH) is RoundState.SUSPENDED
        )

    def test_every_decision_has_a_legal_target(self) -> None:
        """每一个决策都必须落在状态机的合法转移里（不变量 17）。"""
        from ai_psi.cognition.state_machine import can_transition

        for decision in MetacognitiveDecision:
            target = resolve_next_state(decision)
            assert can_transition(RoundState.METACOGNITIVE_REVIEW, target), decision

    def test_no_decision_completes_the_round_directly(self) -> None:
        """元认知决策**不能**直接把回合判定为完成——合成与渲染才是收尾。

        唯一的例外是 ``ESCALATE_TO_RESEARCH`` → ``SUSPENDED``：
        那是"超出当前能力、挂起等待"，语义上确实是终态，
        但它写不出回答，也无法从自身继续——重新激活要创建新回合
        并以 ``causation_id`` 指回原回合（保持审计链完整）。
        """
        for decision in MetacognitiveDecision:
            target = resolve_next_state(decision)
            if decision is MetacognitiveDecision.ESCALATE_TO_RESEARCH:
                assert target is RoundState.SUSPENDED
                continue
            assert not target.is_terminal, decision


class TestStopCondition:
    def test_no_unknowns_and_decent_confidence_is_satisfied(self, make_judgment) -> None:
        judgment = make_judgment(
            unresolved_unknowns=[],
            confidence_band=ConfidenceBand.MODERATE,
            recommended_epistemic_action="answer_with_caveat",
        )
        assert stop_condition_reached(judgment) is True

    def test_unresolved_unknowns_block_it(self, make_judgment) -> None:
        judgment = make_judgment(unresolved_unknowns=["某个未知"], confidence_band="high")
        assert stop_condition_reached(judgment) is False

    def test_low_confidence_blocks_it(self, make_judgment) -> None:
        judgment = make_judgment(unresolved_unknowns=[], confidence_band=ConfidenceBand.LOW)
        assert stop_condition_reached(judgment) is False


class TestLoopCosts:
    def test_extra_loop_cost_is_judgment_plus_metacognition(self) -> None:
        assert EXTRA_LOOP_MODEL_CALLS == 2

    def test_post_loop_tail_is_only_the_renderer(self) -> None:
        """额外一轮之后判断已经产出，尾部只剩渲染。

        把它写成 2 会让每一轮可负担的循环都少一次，
        从而把"能继续"误判成"必须停止"。
        """
        assert POST_LOOP_TAIL_CALLS == 1
