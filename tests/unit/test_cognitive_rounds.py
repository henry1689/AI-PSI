"""认知回合与预算的单元测试。

🔴 覆盖不变量 19 与 20，以及深度预算表（ADR-0008）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from ai_psi.domain.cognitive_rounds import CognitiveBudget
from ai_psi.domain.enums import CognitiveDepth, ErrorType, RoundState

pytestmark = pytest.mark.unit

NOON = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


class TestInvariant19StopReasonRequired:
    """🔴 不变量 19：所有完成回合必须有停止原因。"""

    def test_completed_without_stop_reason_is_rejected(self, make_round) -> None:
        with pytest.raises(ValidationError, match="不变量 19"):
            make_round(state=RoundState.COMPLETED)

    def test_completed_with_stop_reason_is_accepted(self, make_round) -> None:
        r = make_round(state=RoundState.COMPLETED, stop_reason="stop_condition_reached")
        assert r.stop_reason == "stop_condition_reached"

    def test_empty_stop_reason_is_rejected(self, make_round) -> None:
        """空字符串不算"有停止原因"。"""
        with pytest.raises(ValidationError, match="不变量 19"):
            make_round(state=RoundState.COMPLETED, stop_reason="")

    def test_assignment_also_enforced(self, make_round) -> None:
        """``validate_assignment=True``：赋值那一刻就失败，而不是等到持久化。"""
        r = make_round()
        with pytest.raises(ValidationError, match="不变量 19"):
            r.state = RoundState.COMPLETED

    def test_other_states_do_not_require_stop_reason(self, make_round) -> None:
        """只有 COMPLETED 要求停止原因（FAILED 另有其诊断要求，见下个测试类）。"""
        for state in RoundState:
            if state in {RoundState.COMPLETED, RoundState.FAILED}:
                continue
            make_round(state=state)


class TestInvariant20FailureDiagnostics:
    """🔴 不变量 20：失败回合必须能查询失败阶段与错误类别。"""

    def test_failed_without_diagnostics_is_rejected(self, make_round) -> None:
        with pytest.raises(ValidationError, match="不变量 20"):
            make_round(state=RoundState.FAILED)

    def test_failed_with_only_stage_is_rejected(self, make_round) -> None:
        with pytest.raises(ValidationError, match="不变量 20"):
            make_round(state=RoundState.FAILED, failure_stage="analyzing")

    def test_failed_with_only_category_is_rejected(self, make_round) -> None:
        with pytest.raises(ValidationError, match="不变量 20"):
            make_round(state=RoundState.FAILED, error_category=ErrorType.PROCESS_ERROR)

    def test_failed_with_both_is_accepted(self, make_round) -> None:
        r = make_round(
            state=RoundState.FAILED,
            failure_stage="analyzing",
            error_category=ErrorType.PROCESS_ERROR,
        )
        assert r.failure_stage == "analyzing"
        assert r.error_category is ErrorType.PROCESS_ERROR


class TestBudgetCeiling:
    def test_model_calls_may_not_exceed_budget(self, make_round) -> None:
        budget = CognitiveBudget(max_model_calls=5)
        with pytest.raises(ValidationError, match="超过"):
            make_round(budget=budget, model_calls_used=6)

    def test_model_calls_at_budget_is_allowed(self, make_round) -> None:
        r = make_round(budget=CognitiveBudget(max_model_calls=5), model_calls_used=5)
        assert r.remaining_model_calls == 0

    def test_remaining_model_calls(self, make_round) -> None:
        r = make_round(budget=CognitiveBudget(max_model_calls=12), model_calls_used=4)
        assert r.remaining_model_calls == 8


class TestBudgetByDepth:
    """ADR-0008 的深度预算表。"""

    def test_budgets_increase_with_depth(self) -> None:
        calls = [CognitiveBudget.for_depth(d).max_model_calls for d in CognitiveDepth]
        assert calls == sorted(calls)
        assert calls[0] < calls[-1]

    def test_d4_matches_task_book_defaults(self) -> None:
        """D4 应当是任务书 §13.1 给出的默认值上限。"""
        b = CognitiveBudget.for_depth(CognitiveDepth.D4)
        assert b.max_model_calls == 12
        assert b.max_metacognitive_loops == 2
        assert b.max_hypotheses == 4
        assert b.max_retrieved_memories == 20
        assert b.max_context_tokens == 32_000
        assert b.max_duration_seconds == 120

    def test_d0_disables_metacognitive_loops(self) -> None:
        """D0 是直接回答，不需要元认知循环。"""
        assert CognitiveBudget.for_depth(CognitiveDepth.D0).max_metacognitive_loops == 0

    def test_d0_is_cheap(self) -> None:
        b = CognitiveBudget.for_depth(CognitiveDepth.D0)
        assert b.max_model_calls <= 3
        assert b.max_context_tokens <= 4_000

    def test_metacognitive_loops_capped_at_two_for_d2_plus(self) -> None:
        """任务书 §6.3：元认知循环最大 2 次。"""
        for depth in (CognitiveDepth.D2, CognitiveDepth.D3, CognitiveDepth.D4):
            assert CognitiveBudget.for_depth(depth).max_metacognitive_loops == 2

    def test_hypotheses_capped_at_four(self) -> None:
        """任务书 §6.3：假设数量默认最多 4。"""
        for depth in CognitiveDepth:
            assert CognitiveBudget.for_depth(depth).max_hypotheses <= 4

    def test_budget_is_immutable(self) -> None:
        """预算一旦确定不得被就地改写——否则"上限"只是建议。

        用 ``setattr`` 而非直接赋值：直接赋值会让静态类型检查先一步报错，
        那样测的就是类型检查器而不是运行时的不可变性。
        """
        b = CognitiveBudget()
        with pytest.raises(ValidationError):
            setattr(b, "max_model_calls", 999)  # noqa: B010

    def test_every_depth_has_a_budget(self) -> None:
        for depth in CognitiveDepth:
            assert CognitiveBudget.for_depth(depth).max_model_calls >= 1


class TestRoundLifecycle:
    def test_default_state_is_created(self, make_round) -> None:
        assert make_round().state is RoundState.CREATED

    def test_terminal_states_reported(self, make_round) -> None:
        assert make_round(state=RoundState.CANCELLED).is_terminal
        assert not make_round(state=RoundState.ANALYZING).is_terminal

    def test_completed_before_started_is_rejected(self, make_round) -> None:
        with pytest.raises(ValidationError, match="completed_at"):
            make_round(started_at=NOON, completed_at=NOON - timedelta(seconds=1))

    def test_idempotency_key_is_optional_but_supported(self, make_round) -> None:
        """API 重试不得创建重复回合（任务书 §13.4）。"""
        r = make_round(idempotency_key="abc-123")
        assert r.idempotency_key == "abc-123"

    def test_causation_id_supports_reactivation_chain(self, make_round) -> None:
        """重新激活挂起回合时指向原回合，保持审计链完整。"""
        original = make_round(state=RoundState.SUSPENDED)
        reactivated = make_round(causation_id=original.id)
        assert reactivated.causation_id == original.id

    def test_depth_level_defaults_to_d0(self, make_round) -> None:
        assert make_round().depth_level is CognitiveDepth.D0
