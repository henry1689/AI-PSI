"""模块矩阵、深度路由与预算表的一致性（任务书 §7、§13.1，ADR-0008）。

🔴 **本文件的中心是"三张表不许漂移"。**

预算（`_BUDGET_BY_DEPTH`）、标称调用数（`NOMINAL_MODEL_CALLS_BY_DEPTH`）、
模块矩阵（`MODULE_MATRIX`）各自住在不同的模块里，很容易在改动时只更新一处。
一旦漂移，"循环永不超预算"就会在运行期破功——而且是那种
"平时都没事、某天问题稍复杂就炸"的破功。

因此这里用断言把它们钉在一起。
"""

from __future__ import annotations

import pytest

from ai_psi.cognition.depth_router import (
    EXTRA_LOOP_MODEL_CALLS,
    NOMINAL_MODEL_CALLS_BY_DEPTH,
    DepthRoutingInput,
    DepthRoutingResult,
    route_depth,
)
from ai_psi.cognition.orchestrator import (
    MODULE_MATRIX,
    CognitiveStep,
    StepKind,
    nominal_model_calls,
    plan_for_depth,
    state_for_step,
)
from ai_psi.domain.cognitive_rounds import CognitiveBudget
from ai_psi.domain.enums import CognitiveDepth, OrdinalLevel, RoundState
from ai_psi.prompts.schemas import DepthSignals

pytestmark = pytest.mark.unit


def _signals(**overrides: object) -> DepthSignals:
    payload: dict[str, object] = {
        "simple_fact_with_sufficient_evidence": False,
        "needs_explanation_or_comparison": False,
        "multiple_plausible_interpretations": False,
        "user_explicitly_philosophical": False,
        "framework_conflict": False,
        "estimated_impact": "moderate",
        "ambiguity": "low",
        "evidence_conflict": "low",
        "value_conflict": "low",
        "long_term_relevance": "low",
    }
    payload.update(overrides)
    return DepthSignals.model_validate(payload)


def _route(
    signals: DepthSignals,
    *,
    requested: CognitiveDepth | None = None,
    budget_depth: CognitiveDepth = CognitiveDepth.D4,
) -> DepthRoutingResult:
    return route_depth(
        DepthRoutingInput.from_signals(
            inquiry="测试问题",
            signals=signals,
            user_requested_depth=requested,
            available_budget=CognitiveBudget.for_depth(budget_depth),
        )
    )


# ---------------------------------------------------------------------------
# 三张表的一致性
# ---------------------------------------------------------------------------


class TestTablesDoNotDrift:
    def test_nominal_calls_match_module_matrix(self) -> None:
        """标称调用数必须等于矩阵里 MODEL / MODEL_REQUIRED 的步骤数。"""
        for depth in CognitiveDepth:
            assert nominal_model_calls(depth) == NOMINAL_MODEL_CALLS_BY_DEPTH[depth], depth

    def test_budget_covers_nominal_plan_plus_extra_loops(self) -> None:
        """预算必须同时容得下：标称计划 + 每一轮**额外**循环的代价。

        精确的不变式是
        ``容量 ≥ 标称 + (元认知循环轮数 − 1) × 一轮循环的代价``：
        ``max_metacognitive_loops`` 数的是**复核轮数**，
        第一轮已经包含在标称计划里，从第二轮起才是"额外"。

        🔴 差一次调用就意味着：反刍检测永远等不到第二次复核来比较两轮判断——
        而任务书 §13.3 的防反刍正是建立在"连续两轮"之上。
        """
        for depth in CognitiveDepth:
            budget_config = CognitiveBudget.for_depth(depth)
            extra_loops = max(0, budget_config.max_metacognitive_loops - 1)
            required = NOMINAL_MODEL_CALLS_BY_DEPTH[depth] + extra_loops * EXTRA_LOOP_MODEL_CALLS
            assert budget_config.max_model_calls >= required, depth

    def test_matrix_steps_are_ordered_by_state(self) -> None:
        """矩阵中的步骤必须按状态机顺序排列，否则编排器会来回跳状态。"""
        order = [
            RoundState.TRIAGING,
            RoundState.FRAMING,
            RoundState.RETRIEVING,
            RoundState.ANALYZING,
            RoundState.DELIBERATING,
            RoundState.METACOGNITIVE_REVIEW,
            RoundState.SYNTHESIZING,
            RoundState.RESPONDING,
        ]
        for depth in CognitiveDepth:
            ranks = [order.index(spec.state) for spec in MODULE_MATRIX[depth]]
            assert ranks == sorted(ranks), depth

    def test_every_depth_has_a_matrix(self) -> None:
        assert set(MODULE_MATRIX) == set(CognitiveDepth)

    def test_depth_ladders_are_monotonic(self) -> None:
        """更深的分析不该比更浅的少。"""
        for step_count in (nominal_model_calls,):
            values = [step_count(depth) for depth in CognitiveDepth]
            assert values == sorted(values)
        budgets = [CognitiveBudget.for_depth(depth).max_model_calls for depth in CognitiveDepth]
        assert budgets == sorted(budgets)


class TestModuleMatrix:
    def test_philosophy_only_at_d4(self) -> None:
        """🔴 禁止为了展示能力而自动提升到 D4——**分析模块也一样**。"""
        for depth in CognitiveDepth:
            steps = {spec.step for spec in plan_for_depth(depth)}
            assert (CognitiveStep.PHILOSOPHICAL_ANALYSIS in steps) is (depth is CognitiveDepth.D4)

    def test_dialectics_only_at_d3_and_above(self) -> None:
        for depth in CognitiveDepth:
            steps = {spec.step for spec in plan_for_depth(depth)}
            expected = depth.level >= 3
            assert (CognitiveStep.DIALECTICAL_ANALYSIS in steps) is expected

    def test_hypotheses_only_at_d2_and_above(self) -> None:
        for depth in CognitiveDepth:
            steps = {spec.step for spec in plan_for_depth(depth)}
            assert (CognitiveStep.HYPOTHESIS_GENERATION in steps) is (depth.level >= 2)

    def test_mandatory_steps_are_judgment_and_renderer(self) -> None:
        """强制步骤只有判断合成与回答渲染——少了它们，回合拿不出结论。"""
        for depth in CognitiveDepth:
            mandatory = {
                spec.step for spec in plan_for_depth(depth) if spec.kind is StepKind.MODEL_REQUIRED
            }
            assert mandatory == {
                CognitiveStep.JUDGMENT_SYNTHESIS,
                CognitiveStep.RESPONSE_RENDERING,
            }

    def test_every_model_step_declares_a_prompt_task(self) -> None:
        """需要模型的步骤必须声明 Prompt 任务名（不变量 18 的前提）。"""
        for depth in CognitiveDepth:
            for spec in plan_for_depth(depth):
                if spec.kind is StepKind.DETERMINISTIC:
                    assert spec.prompt_task is None
                else:
                    assert spec.prompt_task

    def test_state_lookup(self) -> None:
        assert (
            state_for_step(CognitiveStep.CONCERN_DETECTION, CognitiveDepth.D0)
            is RoundState.TRIAGING
        )
        with pytest.raises(KeyError):
            state_for_step(CognitiveStep.PHILOSOPHICAL_ANALYSIS, CognitiveDepth.D0)


# ---------------------------------------------------------------------------
# 深度路由
# ---------------------------------------------------------------------------


class TestDepthRouting:
    def test_simple_fact_routes_to_d0(self) -> None:
        result = _route(_signals(simple_fact_with_sufficient_evidence=True))
        assert result.depth is CognitiveDepth.D0

    def test_explanation_routes_to_d1(self) -> None:
        result = _route(_signals(needs_explanation_or_comparison=True))
        assert result.depth is CognitiveDepth.D1

    def test_multiple_interpretations_routes_to_d2(self) -> None:
        result = _route(_signals(multiple_plausible_interpretations=True))
        assert result.depth is CognitiveDepth.D2

    def test_evidence_conflict_routes_to_d2(self) -> None:
        result = _route(_signals(evidence_conflict="high"))
        assert result.depth is CognitiveDepth.D2

    def test_value_conflict_routes_to_d3(self) -> None:
        result = _route(_signals(value_conflict="high"))
        assert result.depth is CognitiveDepth.D3

    def test_long_term_relevance_routes_to_d3(self) -> None:
        result = _route(_signals(long_term_relevance="high"))
        assert result.depth is CognitiveDepth.D3

    def test_philosophical_question_routes_to_d4(self) -> None:
        result = _route(_signals(user_explicitly_philosophical=True))
        assert result.depth is CognitiveDepth.D4

    def test_framework_conflict_routes_to_d4(self) -> None:
        result = _route(_signals(framework_conflict=True))
        assert result.depth is CognitiveDepth.D4

    def test_deeper_signals_win_over_shallow_ones(self) -> None:
        """一个"看起来像简单事实"的问题，只要涉及价值冲突就不该被压到 D1。"""
        result = _route(
            _signals(
                simple_fact_with_sufficient_evidence=True,
                value_conflict="very_high",
            )
        )
        assert result.depth is CognitiveDepth.D3

    def test_no_signal_defaults_to_d1(self) -> None:
        assert _route(_signals()).depth is CognitiveDepth.D1

    def test_high_ambiguity_blocks_d0(self) -> None:
        """措辞歧义显著时，"简单事实"的标签不足以让它走 D0。"""
        result = _route(
            _signals(
                simple_fact_with_sufficient_evidence=True,
                ambiguity="high",
            )
        )
        assert result.depth is not CognitiveDepth.D0

    def test_explicit_user_request_is_honoured(self) -> None:
        result = _route(_signals(), requested=CognitiveDepth.D3)
        assert result.depth is CognitiveDepth.D3
        assert result.honoured_user_request is True

    def test_user_request_is_degraded_by_budget(self) -> None:
        """🔴 用户请求 D4 但预算只有 D0 时，**降级并说明**，
        而不是悄悄超支，也不是直接拒绝（ADR-0008）。"""
        result = _route(
            _signals(),
            requested=CognitiveDepth.D4,
            budget_depth=CognitiveDepth.D0,
        )
        assert result.depth is CognitiveDepth.D0
        assert result.degraded_from is CognitiveDepth.D4
        assert result.is_degraded is True
        assert "降级" in result.reason

    def test_model_signals_cannot_force_d4(self) -> None:
        """把一切都标成 very_high 不会让系统自动升到 D4。"""
        result = _route(
            _signals(
                estimated_impact="very_high",
                ambiguity="very_high",
                evidence_conflict="very_high",
                long_term_relevance="low",
                value_conflict="very_high",
            )
        )
        assert result.depth is CognitiveDepth.D3

    def test_ordinal_ordering(self) -> None:
        assert OrdinalLevel.VERY_HIGH.rank > OrdinalLevel.LOW.rank
        assert OrdinalLevel.HIGH.at_least(OrdinalLevel.MODERATE)
        assert not OrdinalLevel.LOW.at_least(OrdinalLevel.HIGH)
