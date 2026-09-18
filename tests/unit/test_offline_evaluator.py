"""离线评估（任务书 §11.4）。

🔴 本文件有两条主线：

* **算出来的数字必须能被解释**——每一项都带口径；
* **没算的东西必须被列出来**——``UNAVAILABLE_METRICS`` 是清单不是注释，
  否则"报告里没有这一项"会被当成"这项是零"。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from ai_psi.domain.enums import CognitiveDepth, RoundState
from ai_psi.learning.offline_evaluator import (
    HIGHER_IS_BETTER_METRICS,
    LOWER_IS_BETTER_METRICS,
    UNAVAILABLE_METRICS,
    EvaluationComparison,
    MetricSnapshot,
    OfflineEvaluator,
    RoundMetrics,
    UnavailableMetric,
)

pytestmark = pytest.mark.unit


def _round(**overrides: object) -> RoundMetrics:
    payload: dict[str, object] = {
        "cognitive_round_id": uuid4(),
        "state": RoundState.COMPLETED,
        "depth": CognitiveDepth.D1,
        "model_calls": 4,
        "successful_model_calls": 4,
        "input_tokens": 100,
        "output_tokens": 50,
        "total_latency_ms": 800,
    }
    payload.update(overrides)
    return RoundMetrics(**payload)  # type: ignore[arg-type]


def _values(evaluator: OfflineEvaluator, rounds) -> dict[str, float]:
    return {item.name: item.value for item in evaluator.snapshot(rounds)}


@pytest.fixture
def evaluator() -> OfflineEvaluator:
    return OfflineEvaluator()


class TestSnapshotBasics:
    def test_empty_input_yields_nothing(self, evaluator) -> None:
        """🔴 没有数据时返回**空**，而不是一堆 0.0。

        给出 0.0 会让人以为"这个指标算出来是零"，
        而真相是"没有数据"——两者在报告里会被当成同一件事。
        """
        assert evaluator.snapshot([]) == ()

    def test_every_snapshot_carries_a_note(self, evaluator) -> None:
        """🔴 没有口径的指标在跨版本比较时会被当成同一个东西，而它可能不是。"""
        for item in evaluator.snapshot([_round()]):
            assert item.note, item.name

    def test_snapshot_is_stable_across_runs(self, evaluator) -> None:
        rounds = [_round(), _round(state=RoundState.FAILED)]
        assert [(i.name, i.value) for i in evaluator.snapshot(rounds)] == [
            (i.name, i.value) for i in evaluator.snapshot(rounds)
        ]


class TestComputableMetrics:
    def test_success_rate(self, evaluator) -> None:
        rounds = [_round(), _round(), _round(), _round(state=RoundState.FAILED)]
        assert _values(evaluator, rounds)["cognitive_round_success_rate"] == 0.75

    def test_success_rate_counts_only_completed(self, evaluator) -> None:
        """等待补充证据的回合**不算成功**——它还没给出结论。"""
        rounds = [_round(state=RoundState.WAITING_FOR_EVIDENCE), _round()]
        assert _values(evaluator, rounds)["cognitive_round_success_rate"] == 0.5

    def test_structured_output_parse_rate(self, evaluator) -> None:
        rounds = [_round(successful_model_calls=3, failed_model_calls=1)]
        assert _values(evaluator, rounds)["structured_output_parse_rate"] == 0.75

    def test_parse_rate_without_invocations(self, evaluator) -> None:
        rounds = [_round(successful_model_calls=0, failed_model_calls=0)]
        values = _values(evaluator, rounds)
        assert values["structured_output_parse_rate"] == 0.0
        note = next(
            item.note
            for item in evaluator.snapshot(rounds)
            if item.name == "structured_output_parse_rate"
        )
        assert "没有模型调用" in note

    def test_unsupported_certainty_rate(self, evaluator) -> None:
        rounds = [_round(unsupported_certainty_detected=True), _round(), _round(), _round()]
        assert _values(evaluator, rounds)["unsupported_certainty_rate"] == 0.25

    def test_unsupported_certainty_note_admits_its_source(self, evaluator) -> None:
        """🔴 这个数字是**元认知自己报的**，不是独立判定——报告必须说清。"""
        note = next(
            item.note
            for item in evaluator.snapshot([_round()])
            if item.name == "unsupported_certainty_rate"
        )
        assert "元认知自己报的" in note

    def test_rumination_counts_only_two_or_more_loops(self, evaluator) -> None:
        """一次循环是流水线的**正常一环**；两次及以上才是绕圈。"""
        rounds = [_round(metacognitive_loops=1), _round(metacognitive_loops=2)]
        assert _values(evaluator, rounds)["rumination_rate"] == 0.5

    def test_rumination_threshold_is_a_constant(self) -> None:
        """阈值一旦可调，跨版本比较就失去意义。"""
        assert OfflineEvaluator.RUMINATION_LOOP_THRESHOLD == 2

    def test_metacognitive_stop_rate(self, evaluator) -> None:
        rounds = [_round(metacognitive_stop=True), _round()]
        assert _values(evaluator, rounds)["metacognitive_stop_rate"] == 0.5

    def test_averages(self, evaluator) -> None:
        rounds = [
            _round(model_calls=2, input_tokens=10, output_tokens=10, total_latency_ms=100),
            _round(model_calls=4, input_tokens=30, output_tokens=30, total_latency_ms=300),
        ]
        values = _values(evaluator, rounds)
        assert values["average_model_calls_per_round"] == 3.0
        assert values["average_token_cost_per_round"] == 40.0
        assert values["average_latency_ms"] == 200.0

    def test_metric_names_are_the_ones_this_module_actually_computes(self, evaluator) -> None:
        names = {item.name for item in evaluator.snapshot([_round()])}
        assert {
            "cognitive_round_success_rate",
            "structured_output_parse_rate",
            "unsupported_certainty_rate",
            "rumination_rate",
            "metacognitive_stop_rate",
            "average_model_calls_per_round",
            "average_token_cost_per_round",
            "average_latency_ms",
        } == names


class TestComparison:
    def test_empty_baseline_has_no_comparison(self, evaluator) -> None:
        comparison = evaluator.compare(baseline_rounds=[])
        assert comparison.comparison_available is False
        assert comparison.baseline == ()
        assert any("没有历史回合" in reason for reason in comparison.reasons)

    def test_baseline_only_is_not_a_comparison(self, evaluator) -> None:
        comparison = evaluator.compare(baseline_rounds=[_round()])
        assert comparison.comparison_available is False
        assert comparison.baseline
        assert comparison.deltas == ()

    def test_baseline_only_says_it_cannot_conclude(self, evaluator) -> None:
        """🔴 单侧数据**不能**当作"改动没有退化"的证据。"""
        comparison = evaluator.compare(baseline_rounds=[_round()])
        assert any("不能" in reason and "退化" in reason for reason in comparison.reasons)

    def test_baseline_only_says_where_candidate_data_comes_from(self, evaluator) -> None:
        comparison = evaluator.compare(baseline_rounds=[_round()])
        assert any("阶段 7" in reason for reason in comparison.reasons)

    def test_both_sides_produce_deltas(self, evaluator) -> None:
        baseline = [_round(), _round(state=RoundState.FAILED)]
        candidate = [_round(), _round()]
        comparison = evaluator.compare(baseline_rounds=baseline, candidate_rounds=candidate)
        assert comparison.comparison_available is True
        assert comparison.delta_for("cognitive_round_success_rate") == pytest.approx(0.5)

    def test_delta_is_candidate_minus_baseline(self, evaluator) -> None:
        comparison = evaluator.compare(
            baseline_rounds=[_round(model_calls=2)],
            candidate_rounds=[_round(model_calls=2), _round(model_calls=4)],
        )
        assert comparison.delta_for("average_model_calls_per_round") == pytest.approx(1.0)

    def test_delta_for_unknown_metric_is_none(self, evaluator) -> None:
        comparison = evaluator.compare(baseline_rounds=[_round()])
        assert comparison.delta_for("不存在的指标") is None

    def test_round_counts_are_reported(self, evaluator) -> None:
        comparison = evaluator.compare(
            baseline_rounds=[_round(), _round()], candidate_rounds=[_round()]
        )
        assert comparison.round_count == 2
        assert comparison.candidate_round_count == 1

    def test_an_empty_candidate_run_is_not_a_crash(self, evaluator) -> None:
        """🔴 候选策略跑到一半崩了、一条回合都没产出，是**合法输入**。

        让 ``zip(..., strict=True)`` 抛一个裸 ``ValueError``（"argument 2
        is shorter than argument 1"）等于把"这次评估没有候选数据"
        报成一次崩溃，调用方拿到一句无从解释的话。
        """
        comparison = evaluator.compare(baseline_rounds=[_round(), _round()], candidate_rounds=[])
        assert comparison.comparison_available is False
        assert comparison.deltas == ()
        assert any("没有产出任何回合" in reason for reason in comparison.reasons)

    def test_an_empty_candidate_run_is_not_reported_as_no_regression(self, evaluator) -> None:
        """🔴 它是**失败的候选运行**，不是"对照通过"。"""
        comparison = evaluator.compare(baseline_rounds=[_round()], candidate_rounds=[])
        with pytest.raises(ValueError, match="没有对照数据"):
            evaluator.regressed(comparison)

    def test_an_empty_candidate_run_keeps_the_baseline(self, evaluator) -> None:
        comparison = evaluator.compare(baseline_rounds=[_round()], candidate_rounds=[])
        assert comparison.baseline
        assert comparison.round_count == 1


class TestRegressionDetection:
    def test_no_comparison_raises_instead_of_answering(self, evaluator) -> None:
        """🔴 **"没评估"不返回 False。**

        返回 False 会让调用方把"没查"当成"查了没问题"，
        而那正是 :class:`PromotionEvidence` 用 ``None``
        而不是 ``False`` 表示未评估的原因。
        """
        comparison = evaluator.compare(baseline_rounds=[_round()])
        with pytest.raises(ValueError, match="没有对照数据"):
            evaluator.regressed(comparison)

    def test_quality_drop_is_a_regression(self, evaluator) -> None:
        comparison = evaluator.compare(
            baseline_rounds=[_round(), _round()],
            candidate_rounds=[_round(), _round(state=RoundState.FAILED)],
        )
        assert evaluator.regressed(comparison) is True

    def test_improvement_is_not_a_regression(self, evaluator) -> None:
        comparison = evaluator.compare(
            baseline_rounds=[_round(), _round(state=RoundState.FAILED)],
            candidate_rounds=[_round(), _round()],
        )
        assert evaluator.regressed(comparison) is False

    def test_parse_rate_drop_is_a_regression(self, evaluator) -> None:
        comparison = evaluator.compare(
            baseline_rounds=[_round(successful_model_calls=4, failed_model_calls=0)],
            candidate_rounds=[_round(successful_model_calls=2, failed_model_calls=2)],
        )
        assert evaluator.regressed(comparison) is True

    def test_certainty_regression_is_a_regression(self, evaluator) -> None:
        """🔴 ``unsupported_certainty_rate`` 是**越低越好**，它上升就是退化。

        初版只把三个"越高越好"的指标算作退化，于是置信度失准率从 0
        涨到 1.0 这种货真价实的退化会被报成"未暴露稳定退化"——
        正好是这条规则要防的反方向。
        """
        comparison = evaluator.compare(
            baseline_rounds=[_round(), _round()],
            candidate_rounds=[_round(unsupported_certainty_detected=True), _round()],
        )
        assert comparison.delta_for("unsupported_certainty_rate") == pytest.approx(0.5)
        assert evaluator.regressed(comparison) is True

    def test_rumination_regression_is_a_regression(self, evaluator) -> None:
        comparison = evaluator.compare(
            baseline_rounds=[_round(), _round()],
            candidate_rounds=[
                _round(metacognitive_loops=OfflineEvaluator.RUMINATION_LOOP_THRESHOLD),
                _round(),
            ],
        )
        assert comparison.delta_for("rumination_rate") == pytest.approx(0.5)
        assert evaluator.regressed(comparison) is True

    def test_lower_is_better_metrics_improving_is_not_a_regression(self, evaluator) -> None:
        comparison = evaluator.compare(
            baseline_rounds=[_round(unsupported_certainty_detected=True), _round()],
            candidate_rounds=[_round(), _round()],
        )
        assert evaluator.regressed(comparison) is False

    def test_every_directional_metric_is_classified(self, evaluator) -> None:
        """🔴 每个指标要么在"越高越好"里，要么在"越低越好"里，要么被显式排除。

        把某个指标的两个集合都漏掉，后果是**静默漏判**：它涨跌都不算退化，
        而没有任何地方会报错。这条用例把"排除了哪些"变成一个可见的决定。
        """
        names = {item.name for item in evaluator.snapshot([_round()])}
        classified = HIGHER_IS_BETTER_METRICS | LOWER_IS_BETTER_METRICS
        # 成本类指标方向有争议（可能是"用更多算力换更好结论"），刻意不判
        deliberately_excluded = {
            "average_model_calls_per_round",
            "average_token_cost_per_round",
            "average_latency_ms",
        }
        assert classified | deliberately_excluded == names
        assert classified & deliberately_excluded == set()

    def test_cost_increase_alone_is_not_reported_here(self, evaluator) -> None:
        """⚠️ **这条断言描述的是当前口径的边界，而不是一个理想性质。**

        本方法只把"越高越好"的三项指标当作退化信号。调用成本上升
        不在这里报——而提案模板的 ``success_metrics`` 明确要求
        "平均模型调用数不得显著上升（避免用更多算力换指标）"。

        也就是说：**成本回归由提案的对照指标与人工评审把关，
        不由本方法把关。** 阶段 7 的评测系统应当把这一点补上。
        """
        comparison = evaluator.compare(
            baseline_rounds=[_round(model_calls=2)],
            candidate_rounds=[_round(model_calls=20)],
        )
        assert comparison.delta_for("average_model_calls_per_round") == pytest.approx(18.0)
        assert evaluator.regressed(comparison) is False


class TestUnavailableMetricsAreListed:
    """🔴 "哪些没算"必须一眼可见，而不是靠推断。"""

    def test_the_list_is_not_empty(self) -> None:
        assert UNAVAILABLE_METRICS

    def test_the_list_covers_every_metric_that_is_neither_computed_nor_excluded(
        self, evaluator
    ) -> None:
        """⚠️ 这份清单的价值取决于它**不漏项**。

        它自己的文档说：让人"一眼看到哪些没算，而不是从'报告里没有这一项'
        去推断"。因此"既没算、也没列"是最坏的情况——它会被读成"这一项
        要么算出来了、要么不存在"。

        初版就漏了两处：§11.4 的「其他场景退化程度」与 §16.1 的
        「按深度分组的延迟/调用数」。
        """
        listed = {item.name for item in UNAVAILABLE_METRICS}
        assert "cross_scenario_regression" in listed
        assert "average_latency_per_depth" in listed
        assert "average_model_calls_per_depth" in listed

    def test_section_11_4_metrics_all_have_a_home(self, evaluator) -> None:
        """§11.4 的八项指标，每一项要么被算出来，要么在这份清单里。"""
        computed = {item.name for item in evaluator.snapshot([_round()])}
        listed = {item.name for item in UNAVAILABLE_METRICS}
        section_11_4 = {
            "cognitive_round_success_rate",
            "structured_output_parse_rate",
            "fact_hypothesis_confusion_rate",
            "conflict_preservation_rate",
            "user_correction_recurrence_rate",
            "unsupported_certainty_rate",
            "rumination_rate",
            "cross_scenario_regression",
        }
        assert section_11_4 <= (computed | listed)

    def test_every_entry_says_why_and_who(self) -> None:
        for item in UNAVAILABLE_METRICS:
            assert item.reason.strip()
            assert item.owner_stage.strip()

    def test_reasons_are_specific_not_a_placeholder(self) -> None:
        """笼统的"暂不支持"无法判断什么时候能补上。"""
        for item in UNAVAILABLE_METRICS:
            assert "暂不支持" not in item.reason
            assert len(item.reason) > 20, item.name

    def test_the_hard_indicators_are_declared_out_of_scope(self) -> None:
        """🔴 ``cross_user_memory_leak_rate`` 是**测试断言的性质**，不是度量。

        把它放进评估报告会让人以为"这个数字只要小就行"——
        而它必须恒为 0，由测试保证。
        """
        leak = next(
            item for item in UNAVAILABLE_METRICS if item.name == "cross_user_memory_leak_rate"
        )
        assert "测试" in leak.reason

    def test_list_is_attached_to_every_comparison(self, evaluator) -> None:
        for comparison in (
            evaluator.compare(baseline_rounds=[]),
            evaluator.compare(baseline_rounds=[_round()]),
            evaluator.compare(baseline_rounds=[_round()], candidate_rounds=[_round()]),
        ):
            assert comparison.unavailable == UNAVAILABLE_METRICS

    def test_unavailable_names_do_not_collide_with_computed_names(self, evaluator) -> None:
        """一个名字不能既"算出来了"又"没交付"。"""
        computed = {item.name for item in evaluator.snapshot([_round()])}
        unavailable = {item.name for item in UNAVAILABLE_METRICS}
        assert computed.isdisjoint(unavailable)


class TestValueObjects:
    def test_snapshot_is_frozen(self) -> None:
        import dataclasses

        snapshot = MetricSnapshot(name="x", value=1.0)
        with pytest.raises(dataclasses.FrozenInstanceError):
            snapshot.value = 2.0  # type: ignore[misc]

    def test_default_comparison_is_not_available(self) -> None:
        """默认构造出来的是"什么都没做"，不是"做完了没问题"。"""
        assert EvaluationComparison().comparison_available is False


class TestTheCountingDefaultsAreZero:
    """🔴 变异测试发现：``RoundMetrics`` 的七个计数字段与
    ``EvaluationComparison`` 的两个回合数，默认值**从未被断言**。

    它们的默认值改成 ``1`` 或 ``-1`` 之后全部存活——因为
    ``snapshot`` 与既有测试 helper 都显式传了这些字段，
    默认值一次都没被走到。

    ⚠️ 这九个字段里有两个是**比值**的分母（``model_calls`` 之类经
    ``snapshot`` 求和），剩下的直接出现在报告里。默认值 1 意味着
    "一条度量都没有的回合"看起来调用过一次模型。
    """

    def test_a_round_with_nothing_recorded_counts_nothing(self) -> None:
        metrics = RoundMetrics(
            cognitive_round_id=uuid4(),
            state=RoundState.CREATED,
            depth=CognitiveDepth.D1,
        )
        assert metrics.model_calls == 0
        assert metrics.metacognitive_loops == 0
        assert metrics.successful_model_calls == 0
        assert metrics.failed_model_calls == 0
        assert metrics.input_tokens == 0
        assert metrics.output_tokens == 0
        assert metrics.total_latency_ms == 0

    def test_a_comparison_with_nothing_counts_nothing(self) -> None:
        comparison = EvaluationComparison()
        assert comparison.round_count == 0
        assert comparison.candidate_round_count == 0


class TestTheValueObjectsAreImmutable:
    """🔴 变异测试发现：四处 ``frozen=True`` 改成 ``False`` 全部存活。

    这几张对象都是**一次评估的结论**：``MetricSnapshot`` 的 ``value``
    被改掉之后，报告里的数字与算出来的不一致；``EvaluationComparison``
    的 ``comparison_available`` 被改掉之后，"没对照"能变成"对照了没问题"。
    """

    @pytest.mark.parametrize(
        ("factory", "field", "value"),
        [
            (lambda: MetricSnapshot(name="x", value=1.0), "name", "y"),
            (
                lambda: RoundMetrics(
                    cognitive_round_id=uuid4(), state=RoundState.CREATED, depth=CognitiveDepth.D1
                ),
                "model_calls",
                9,
            ),
            (lambda: EvaluationComparison(), "comparison_available", True),
            (
                lambda: UnavailableMetric(name="x", reason="缺数据", owner_stage="阶段 7"),
                "reason",
                "改过了",
            ),
        ],
    )
    def test_cannot_be_mutated(self, factory, field: str, value: object) -> None:
        import dataclasses

        target = factory()
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(target, field, value)


class TestOnlyCompletedRoundsCountAsCompleted:
    """🔴 变异测试发现：``state is COMPLETED`` 改成 ``<=`` 之后全绿。

    原因是 **StrEnum 的排序比字符串**，而 ``"analyzing"`` 与
    ``"cancelled"`` 恰好都排在 ``"completed"`` **前面**——
    于是"分析到一半就不见了的回合"和"被取消的回合"都会被算成
    **成功完成**。成功率因此会虚高，而报告上看不出任何异常。

    既有用例只用 ``FAILED`` 做过反例，而 ``"failed"`` 排在
    ``"completed"`` 后面，所以那条用例恰好盖不住这个方向。
    """

    @pytest.mark.parametrize("state", [RoundState.ANALYZING, RoundState.CANCELLED])
    def test_a_non_completed_state_is_not_completed(
        self, evaluator: OfflineEvaluator, state: RoundState
    ) -> None:
        rounds = [_round(), _round(state=state)]
        assert _values(evaluator, rounds)["cognitive_round_success_rate"] == 0.5


class TestRuminationCountsAboveTheThreshold:
    """🔴 变异测试发现：反刍判据 ``loops >= 阈值`` 的**上边界**没有被断言。

    阈值是 2。改成 ``==``（或 ``is``）之后，**绕了三次、四次圈**的回合
    不再算反刍——而那恰恰是反刍最严重的情形。既有用例只试了
    1 次与 2 次，刚好是边界的两侧。
    """

    def test_three_loops_is_still_rumination(self, evaluator: OfflineEvaluator) -> None:
        rounds = [_round(metacognitive_loops=1), _round(metacognitive_loops=3)]
        assert _values(evaluator, rounds)["rumination_rate"] == 0.5

    def test_the_threshold_itself_counts(self, evaluator: OfflineEvaluator) -> None:
        rounds = [_round(metacognitive_loops=1), _round(metacognitive_loops=2)]
        assert _values(evaluator, rounds)["rumination_rate"] == 0.5


class TestAveragesAreNotTruncated:
    """🔴 变异测试发现：三个"平均值"指标的除法改成整除之后全绿。

    既有的平均值用例**刻意挑的是能整除的数字**（4 次调用、2 个回合），
    于是 `/` 与 `//` 给出同一个答案。而在真实数据上两者差的是小数部分：
    平均延迟 800 与 850 的真值是 825.0，整除会报成 825——
    一个**看起来像整数**的毫秒数，没有任何迹象说明它被截断过。
    """

    def test_latency_average_keeps_the_fraction(self, evaluator: OfflineEvaluator) -> None:
        rounds = [_round(total_latency_ms=800), _round(total_latency_ms=851)]
        assert _values(evaluator, rounds)["average_latency_ms"] == 825.5

    def test_token_average_keeps_the_fraction(self, evaluator: OfflineEvaluator) -> None:
        # 第一个回合 100+51=151，第二个回合 100+50=150；平均 150.5
        rounds = [
            _round(input_tokens=100, output_tokens=51),
            _round(input_tokens=100, output_tokens=50),
        ]
        assert _values(evaluator, rounds)["average_token_cost_per_round"] == 150.5

    def test_model_call_average_keeps_the_fraction(self, evaluator: OfflineEvaluator) -> None:
        rounds = [_round(model_calls=4), _round(model_calls=5)]
        assert _values(evaluator, rounds)["average_model_calls_per_round"] == 4.5


class TestDeltaLookupFindsTheMetric:
    """``delta_for`` 按**指标名**取差值。"""

    def test_every_snapshot_metric_can_be_looked_up(self, evaluator: OfflineEvaluator) -> None:
        baseline = [_round(), _round()]
        candidate = [_round(model_calls=9), _round(model_calls=9)]
        comparison = evaluator.compare(baseline_rounds=baseline, candidate_rounds=candidate)

        for snapshot in comparison.baseline:
            assert comparison.delta_for(snapshot.name) is not None

    def test_an_unknown_name_returns_none(self, evaluator: OfflineEvaluator) -> None:
        comparison = evaluator.compare(baseline_rounds=[_round()], candidate_rounds=[_round()])
        assert comparison.delta_for("没有这个指标") is None
