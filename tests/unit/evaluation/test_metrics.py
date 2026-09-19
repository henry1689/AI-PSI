"""指标层（阶段 7 · S4）。

这一组回答两件事：

1. **数字对不对**——分子分母、不变量、分类是否按结构化字段；
2. **模型拒不拒绝错的汇总**——一份"看起来合理但自相矛盾"的指标必须构造失败。

🔴 全是纯单元测试：不跑案例、不连数据库、不调 Provider。
输入是**手工构造**的结果对象，因此每条用例只验证一件事。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_psi.evaluation.assertions import AssertionResult, CaseObservation
from ai_psi.evaluation.loader import GoldenDataset
from ai_psi.evaluation.metrics import (
    METRICS_SCHEMA_VERSION,
    RATIO_PRECISION,
    AssertionGroupMetrics,
    AssertionMetrics,
    AssertionNameMetrics,
    CaseMetrics,
    CategoryMetrics,
    EvaluationMetrics,
    RatioMetric,
    _ratio,
    compute_metrics,
    metrics_definition_digest,
)
from ai_psi.evaluation.models import GoldenCase
from ai_psi.evaluation.runner import CaseResult, ExecutionMode, RunResult

pytestmark = pytest.mark.unit

_ROOT = Path(".")


def _case(case_id: str, category: str = "simple_fact") -> GoldenCase:
    """一条满足加载器校验的最简案例。

    ⚠️ 走 :meth:`GoldenCase.model_validate` 而不是直接构造：直接构造要求
    传 ``CaseType`` / ``CaseCategory`` 的**枚举实例**，而这里想表达的正是
    "从 YAML 里读出来时的那种形状"——与加载器走的是同一条校验路径。
    """
    return GoldenCase.model_validate(
        {
            "schema_version": 1,
            "case_type": "cognitive_behavior",
            "case_id": case_id,
            "category": category,
            "intent": "指标测试用案例",
            "stimulus": {"input": "一个合成问题。"},
            "expectations": {
                "required": [{"assertion": "final_state", "expected": "completed"}],
            },
        }
    )


def _dataset(categories: dict[str, int]) -> GoldenDataset:
    """``{类别: 案例数}`` → 数据集。"""
    cases = [
        _case(f"{category}-{index:03d}", category)
        for category, count in categories.items()
        for index in range(count)
    ]
    return GoldenDataset(root=_ROOT, cases=tuple(cases))


def _observation(
    *, state: str = "completed", depth: str = "d0", stop_reason: str | None = "X"
) -> CaseObservation:
    return CaseObservation(
        state=state,
        depth=depth,
        stop_reason_present=stop_reason is not None,
        stop_reason=stop_reason,
        response_present=True,
        judgment_present=True,
        model_calls_used=4,
        metacognitive_loops=0,
    )


def _assertion(
    name: str = "final_state",
    *,
    mode: str = "required",
    passed: bool = True,
    observation_status: str = "observed",
) -> AssertionResult:
    return AssertionResult(
        name=name,
        mode=mode,
        expected="completed",
        observed=None if observation_status == "unobservable" else "completed",
        passed=passed,
        observation_status=observation_status,
    )


def _result(
    case_id: str,
    *,
    category: str = "simple_fact",
    passed: bool = True,
    failure_kind: str | None = None,
    assertions: tuple[AssertionResult, ...] = (),
    observation: CaseObservation | None = None,
) -> CaseResult:
    return CaseResult(
        case_id=case_id,
        category=category,
        assertions=assertions,
        passed=passed,
        failure_kind=failure_kind,
        observation=observation,
    )


def _run(cases: tuple[CaseResult, ...]) -> RunResult:
    passed = sum(1 for case in cases if case.passed)
    return RunResult(
        case_type="cognitive_behavior",
        provider="mock",
        total=len(cases),
        passed=passed,
        failed=len(cases) - passed,
        passed_overall=passed == len(cases),
        cases=cases,
        execution_mode=ExecutionMode.IN_MEMORY.value,
    )


class TestRatioMetric:
    """A 组：比率的表示、精度与拒绝规则。"""

    @pytest.mark.parametrize(
        ("numerator", "denominator", "value"),
        [
            (0, 1, "0.000000"),
            (1, 1, "1.000000"),
            (1, 3, "0.333333"),
            (2, 3, "0.666667"),
            (1, 8, "0.125000"),
            (7, 8, "0.875000"),
        ],
    )
    def test_fixed_precision_values(self, numerator: int, denominator: int, value: str) -> None:
        metric = RatioMetric(numerator=numerator, denominator=denominator, value=value)
        assert metric.value == value
        assert len(value.split(".")[1]) == RATIO_PRECISION

    def test_zero_denominator_is_null_not_zero(self) -> None:
        """🔴 0/0 写成 0% 是最常见的一种假指标。"""
        metric = RatioMetric(numerator=0, denominator=0, value=None)
        assert metric.value is None
        assert metric.value != "0.000000"

    def test_a_negative_numerator_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RatioMetric(numerator=-1, denominator=1, value="0.000000")

    def test_a_negative_denominator_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RatioMetric(numerator=0, denominator=-1, value=None)

    def test_numerator_greater_than_denominator_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RatioMetric(numerator=3, denominator=2, value="1.500000")

    def test_a_miscalculated_value_is_rejected(self) -> None:
        """🔴 分子分母都对、**值算错了**的那种最难发现，必须构造失败。"""
        with pytest.raises(ValidationError, match="值与分数不一致"):
            RatioMetric(numerator=1, denominator=3, value="0.333334")

    def test_zero_denominator_with_a_non_null_value_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="值与分数不一致"):
            RatioMetric(numerator=0, denominator=0, value="0.000000")

    def test_the_value_is_never_a_float(self) -> None:
        """🔴 二进制浮点会带来平台抖动，而"两次运行逐字节一致"要求没有抖动。"""
        metric = RatioMetric(numerator=1, denominator=3, value="0.333333")
        assert isinstance(metric.value, str)

    def test_rounding_is_half_up(self) -> None:
        """``ROUND_HALF_UP``：恰好落在半位时向绝对值大的方向进位。"""
        # 1/16 = 0.0625 → 第 7 位是 5，HALF_UP 进位
        assert _ratio_value(1, 16) == "0.062500"
        # 3/16 = 0.1875 → 同样
        assert _ratio_value(3, 16) == "0.187500"
        # 1/3 是无限小数，第 7 位是 3，不进位
        assert _ratio_value(1, 3) == "0.333333"
        # 2/3 第 7 位是 6，进位
        assert _ratio_value(2, 3) == "0.666667"

    def test_the_same_fraction_always_gives_the_same_string(self) -> None:
        assert _ratio_value(7, 9) == _ratio_value(7, 9)


def _ratio_value(numerator: int, denominator: int) -> str:
    """分数格式化后的字符串。

    ⚠️ 这里直接调私有的 ``_ratio`` 而不是绕 :class:`RatioMetric`：
    要测的正是"格式化出来的字符串长什么样"，而通过模型构造需要先知道
    答案（模型会拿它自己的计算来校验传入的值），那就成了循环论证。
    随后再用构造好的值建一次 :class:`RatioMetric`，确认它被接受。
    """
    value = _ratio(numerator, denominator)
    assert value is not None
    RatioMetric(numerator=numerator, denominator=denominator, value=value)
    return value


class TestCaseMetrics:
    """B 组：案例统计的分母与分类。"""

    def test_all_passing(self) -> None:
        dataset = _dataset({"simple_fact": 3})
        result = _run(tuple(_result(f"simple_fact-{i:03d}") for i in range(3)))
        metrics = compute_metrics(dataset, result).cases
        assert metrics.total_cases == 3
        assert metrics.executed_cases == 3
        assert metrics.passed_cases == 3
        assert metrics.case_pass_rate.value == "1.000000"
        assert metrics.execution_coverage.value == "1.000000"

    def test_case_pass_rate_uses_executed_as_denominator(self) -> None:
        """🔴 分母是 **executed**，不是 total——用 total 会混淆"失败"与"没跑"。"""
        dataset = _dataset({"simple_fact": 4})
        result = _run(
            (
                _result("simple_fact-000"),
                _result("simple_fact-001"),
                _result("simple_fact-002", passed=False, failure_kind="assertion_failure"),
            )
        )
        metrics = compute_metrics(dataset, result).cases
        assert metrics.total_cases == 4
        assert metrics.executed_cases == 3
        assert metrics.not_executed_cases == 1
        assert metrics.case_pass_rate.numerator == 2
        assert metrics.case_pass_rate.denominator == 3

    def test_execution_coverage_uses_total_as_denominator(self) -> None:
        dataset = _dataset({"simple_fact": 4})
        result = _run((_result("simple_fact-000"),))
        metrics = compute_metrics(dataset, result).cases
        assert metrics.execution_coverage.numerator == 1
        assert metrics.execution_coverage.denominator == 4

    def test_zero_executed_gives_a_null_pass_rate(self) -> None:
        """🔴 一条都没跑，通过率是"不知道"，不是 0%。"""
        dataset = _dataset({"simple_fact": 2})
        metrics = compute_metrics(dataset, _run(())).cases
        assert metrics.executed_cases == 0
        assert metrics.case_pass_rate.value is None
        assert metrics.execution_coverage.value == "0.000000"

    def test_an_execution_error_is_told_apart_from_an_assertion_failure(self) -> None:
        """🔴 两类失败的**对策完全不同**，必须由结构化字段区分。"""
        dataset = _dataset({"simple_fact": 2})
        result = _run(
            (
                _result("simple_fact-000", passed=False, failure_kind="execution_error"),
                _result("simple_fact-001", passed=False, failure_kind="assertion_failure"),
            )
        )
        metrics = compute_metrics(dataset, result).cases
        assert metrics.failed_cases == 2
        assert metrics.execution_error_cases == 1
        assert metrics.assertion_failed_cases == 1

    def test_a_passing_case_carries_no_failure_kind(self) -> None:
        result = _result("simple_fact-000")
        assert result.failure_kind is None


class TestCategoryMetrics:
    """C 组：类别拆分与总和一致性。"""

    def test_categories_are_split_and_sorted(self) -> None:
        dataset = _dataset({"simple_fact": 2, "evidence_conflict": 1})
        result = _run(
            (
                _result("simple_fact-000"),
                _result("simple_fact-001", passed=False, failure_kind="assertion_failure"),
                _result("evidence_conflict-000", category="evidence_conflict"),
            )
        )
        metrics = compute_metrics(dataset, result)
        assert [item.category for item in metrics.categories] == [
            "evidence_conflict",
            "simple_fact",
        ]
        by_name = {item.category: item for item in metrics.categories}
        assert by_name["simple_fact"].total == 2
        assert by_name["simple_fact"].passed == 1
        assert by_name["simple_fact"].failed == 1

    def test_category_totals_match_the_overall_counts(self) -> None:
        dataset = _dataset({"simple_fact": 2, "unable_to_determine": 2})
        result = _run(
            (
                _result("simple_fact-000"),
                _result("unable_to_determine-000", category="unable_to_determine"),
            )
        )
        metrics = compute_metrics(dataset, result)
        assert sum(item.total for item in metrics.categories) == metrics.cases.total_cases
        assert sum(item.passed for item in metrics.categories) == metrics.cases.passed_cases
        assert sum(item.failed for item in metrics.categories) == metrics.cases.failed_cases
        assert (
            sum(item.not_executed for item in metrics.categories)
            == metrics.cases.not_executed_cases
        )

    def test_only_categories_present_in_the_dataset_are_reported(self) -> None:
        """🔴 不输出空类别：一份有 9 行全 0 的表会让读者分不清
        "这个类别没案例"与"这个类别全没过"。"""
        dataset = _dataset({"simple_fact": 1})
        metrics = compute_metrics(dataset, _run((_result("simple_fact-000"),)))
        assert [item.category for item in metrics.categories] == ["simple_fact"]

    def test_a_category_with_no_executed_case_has_a_null_pass_rate(self) -> None:
        dataset = _dataset({"simple_fact": 1, "evidence_conflict": 1})
        result = _run((_result("simple_fact-000"),))
        by_name = {item.category: item for item in compute_metrics(dataset, result).categories}
        assert by_name["evidence_conflict"].executed == 0
        assert by_name["evidence_conflict"].pass_rate.value is None


class TestAssertionMetrics:
    """D 组：断言统计——三个分类与两个分母。"""

    def test_required_and_forbidden_are_counted_separately(self) -> None:
        dataset = _dataset({"simple_fact": 1})
        result = _run(
            (
                _result(
                    "simple_fact-000",
                    assertions=(
                        _assertion("final_state", mode="required", passed=True),
                        _assertion("analysis_module_ran", mode="forbidden", passed=True),
                    ),
                ),
            )
        )
        metrics = compute_metrics(dataset, result).assertions
        assert metrics.required.total == 1
        assert metrics.forbidden.total == 1
        assert metrics.overall.total == 2

    def test_required_plus_forbidden_equals_overall(self) -> None:
        dataset = _dataset({"simple_fact": 1})
        result = _run(
            (
                _result(
                    "simple_fact-000",
                    assertions=(
                        _assertion("a", mode="required"),
                        _assertion("b", mode="required"),
                        _assertion("c", mode="forbidden"),
                    ),
                ),
            )
        )
        metrics = compute_metrics(dataset, result).assertions
        assert metrics.required.total + metrics.forbidden.total == metrics.overall.total

    def test_pass_rate_uses_evaluated_as_denominator(self) -> None:
        """🔴 分母是 **evaluated**。用 total 会把"没观测到"算成"失败"。"""
        dataset = _dataset({"simple_fact": 1})
        result = _run(
            (
                _result(
                    "simple_fact-000",
                    assertions=(
                        _assertion("a", passed=True),
                        _assertion("b", passed=False),
                        _assertion("c", passed=False, observation_status="unobservable"),
                    ),
                ),
            )
        )
        overall = compute_metrics(dataset, result).assertions.overall
        assert overall.total == 3
        assert overall.evaluated == 2
        assert overall.unobservable == 1
        assert overall.pass_rate.numerator == 1
        assert overall.pass_rate.denominator == 2

    def test_observation_coverage_uses_total_as_denominator(self) -> None:
        dataset = _dataset({"simple_fact": 1})
        result = _run(
            (
                _result(
                    "simple_fact-000",
                    assertions=(
                        _assertion("a", passed=True),
                        _assertion("b", passed=False, observation_status="unobservable"),
                    ),
                ),
            )
        )
        overall = compute_metrics(dataset, result).assertions.overall
        assert overall.observation_coverage.numerator == 1
        assert overall.observation_coverage.denominator == 2

    def test_unobservable_is_not_counted_as_a_comparison_failure(self) -> None:
        """🔴 "没读到"与"读到了但对不上"是两回事。"""
        dataset = _dataset({"simple_fact": 1})
        result = _run(
            (
                _result(
                    "simple_fact-000",
                    assertions=(_assertion("a", passed=False, observation_status="unobservable"),),
                ),
            )
        )
        overall = compute_metrics(dataset, result).assertions.overall
        assert overall.failed == 0
        assert overall.unobservable == 1

    def test_an_unobservable_assertion_still_fails_the_case(self) -> None:
        """🔴 指标把两者分开统计，但**案例判定语义没有放宽**——
        不可观测的断言仍然让案例不通过。"""
        assertion = _assertion("a", passed=False, observation_status="unobservable")
        assert assertion.passed is False

    def test_a_forbidden_assertion_that_passes_is_counted_as_passed(self) -> None:
        dataset = _dataset({"simple_fact": 1})
        result = _run(
            (
                _result(
                    "simple_fact-000",
                    assertions=(_assertion("x", mode="forbidden", passed=True),),
                ),
            )
        )
        assert compute_metrics(dataset, result).assertions.forbidden.passed == 1


class TestAssertionNameMetrics:
    """E 组：按断言名统计。"""

    def test_the_same_name_is_split_by_mode(self) -> None:
        """同名断言可以同时以 required 与 forbidden 出现。"""
        dataset = _dataset({"simple_fact": 1})
        result = _run(
            (
                _result(
                    "simple_fact-000",
                    assertions=(
                        _assertion("depth_level", mode="required"),
                        _assertion("depth_level", mode="forbidden"),
                    ),
                ),
            )
        )
        names = compute_metrics(dataset, result).assertion_names
        assert len(names) == 1
        assert names[0].required_total == 1
        assert names[0].forbidden_total == 1
        assert names[0].total == 2

    def test_names_are_sorted(self) -> None:
        dataset = _dataset({"simple_fact": 1})
        result = _run(
            (
                _result(
                    "simple_fact-000",
                    assertions=(
                        _assertion("zzz"),
                        _assertion("aaa"),
                        _assertion("mmm"),
                    ),
                ),
            )
        )
        names = [item.name for item in compute_metrics(dataset, result).assertion_names]
        assert names == sorted(names)

    def test_the_same_assertion_across_cases_is_aggregated(self) -> None:
        dataset = _dataset({"simple_fact": 2})
        result = _run(
            (
                _result("simple_fact-000", assertions=(_assertion("final_state"),)),
                _result(
                    "simple_fact-001",
                    assertions=(_assertion("final_state", passed=False),),
                ),
            )
        )
        names = compute_metrics(dataset, result).assertion_names
        assert names[0].total == 2
        assert names[0].passed == 1
        assert names[0].failed == 1
        assert names[0].pass_rate.value == "0.500000"

    def test_name_totals_match_the_overall_counts(self) -> None:
        dataset = _dataset({"simple_fact": 2})
        result = _run(
            (
                _result(
                    "simple_fact-000",
                    assertions=(_assertion("a"), _assertion("b", mode="forbidden")),
                ),
                _result("simple_fact-001", assertions=(_assertion("a", passed=False),)),
            )
        )
        metrics = compute_metrics(dataset, result)
        overall = metrics.assertions.overall
        for field in ("total", "evaluated", "passed", "failed", "unobservable"):
            assert sum(getattr(item, field) for item in metrics.assertion_names) == getattr(
                overall, field
            )

    def test_a_name_with_zero_total_has_a_null_coverage(self) -> None:
        metric = AssertionNameMetrics(
            name="x",
            total=0,
            required_total=0,
            forbidden_total=0,
            evaluated=0,
            passed=0,
            failed=0,
            unobservable=0,
            pass_rate=RatioMetric(numerator=0, denominator=0, value=None),
            observation_coverage=RatioMetric(numerator=0, denominator=0, value=None),
        )
        assert metric.pass_rate.value is None
        assert metric.observation_coverage.value is None


class TestDistributions:
    """F 组：分布统计。"""

    def test_state_depth_and_stop_reason_are_distributed(self) -> None:
        dataset = _dataset({"simple_fact": 3})
        result = _run(
            (
                _result("simple_fact-000", observation=_observation(state="completed", depth="d0")),
                _result("simple_fact-001", observation=_observation(state="completed", depth="d1")),
                _result("simple_fact-002", observation=_observation(state="failed", depth="d1")),
            )
        )
        distributions = compute_metrics(dataset, result).distributions
        assert distributions.final_state == {"completed": 2, "failed": 1}
        assert distributions.depth == {"d0": 1, "d1": 2}

    def test_depth_follows_the_enum_order_not_the_alphabet(self) -> None:
        """🔴 枚举按**正式顺序**（d0 在 d1 前），不是字典序。"""
        dataset = _dataset({"simple_fact": 2})
        result = _run(
            (
                _result("simple_fact-000", observation=_observation(depth="d2")),
                _result("simple_fact-001", observation=_observation(depth="d0")),
            )
        )
        assert list(compute_metrics(dataset, result).distributions.depth) == ["d0", "d2"]

    def test_a_missing_stop_reason_uses_an_explicit_key(self) -> None:
        """🔴 空值要有一个**明确的名字**，不能是一个恰好为空的字符串。"""
        dataset = _dataset({"simple_fact": 1})
        result = _run((_result("simple_fact-000", observation=_observation(stop_reason=None)),))
        assert compute_metrics(dataset, result).distributions.stop_reason == {"__none__": 1}

    def test_an_unknown_but_real_value_is_not_dropped(self) -> None:
        """出现了一个枚举里没有的状态，那是需要被看见的事实。"""
        dataset = _dataset({"simple_fact": 1})
        result = _run((_result("simple_fact-000", observation=_observation(state="brand_new")),))
        assert compute_metrics(dataset, result).distributions.final_state == {"brand_new": 1}

    def test_only_executed_cases_enter_the_distribution(self) -> None:
        """🔴 没跑起来的案例没有终态、没有深度，不该被计进分布。"""
        dataset = _dataset({"simple_fact": 2})
        result = _run(
            (
                _result("simple_fact-000", observation=_observation()),
                _result("simple_fact-001", passed=False, failure_kind="execution_error"),
            )
        )
        distributions = compute_metrics(dataset, result).distributions
        assert sum(distributions.final_state.values()) == 1
        assert "__none__" not in distributions.final_state

    def test_stop_reasons_are_sorted_lexicographically(self) -> None:
        """停止原因是自由字符串（不是枚举），因此按字典序。"""
        dataset = _dataset({"simple_fact": 2})
        result = _run(
            (
                _result("simple_fact-000", observation=_observation(stop_reason="ZZZ")),
                _result("simple_fact-001", observation=_observation(stop_reason="AAA")),
            )
        )
        assert list(compute_metrics(dataset, result).distributions.stop_reason) == ["AAA", "ZZZ"]


class TestFailureIndex:
    """G 组：失败索引。"""

    def test_failed_case_ids_are_sorted(self) -> None:
        dataset = _dataset({"simple_fact": 3})
        result = _run(
            (
                _result("simple_fact-002", passed=False, failure_kind="assertion_failure"),
                _result("simple_fact-000", passed=False, failure_kind="assertion_failure"),
            )
        )
        index = compute_metrics(dataset, result).failures
        assert index.failed_case_ids == ("simple_fact-000", "simple_fact-002")

    def test_execution_errors_and_assertion_failures_are_indexed_apart(self) -> None:
        dataset = _dataset({"simple_fact": 2})
        result = _run(
            (
                _result("simple_fact-000", passed=False, failure_kind="execution_error"),
                _result("simple_fact-001", passed=False, failure_kind="assertion_failure"),
            )
        )
        index = compute_metrics(dataset, result).failures
        assert index.execution_error_case_ids == ("simple_fact-000",)
        assert index.assertion_failure_case_ids == ("simple_fact-001",)

    def test_unobservable_cases_are_indexed(self) -> None:
        dataset = _dataset({"simple_fact": 1})
        result = _run(
            (
                _result(
                    "simple_fact-000",
                    assertions=(_assertion("a", passed=False, observation_status="unobservable"),),
                ),
            )
        )
        assert compute_metrics(dataset, result).failures.unobservable_case_ids == (
            "simple_fact-000",
        )

    def test_failed_assertions_are_listed_by_case(self) -> None:
        dataset = _dataset({"simple_fact": 1})
        result = _run(
            (
                _result(
                    "simple_fact-000",
                    passed=False,
                    failure_kind="assertion_failure",
                    assertions=(
                        _assertion("final_state", mode="required", passed=False),
                        _assertion("depth_level", mode="forbidden", passed=False),
                        _assertion("ok_one", passed=True),
                    ),
                ),
            )
        )
        by_case = compute_metrics(dataset, result).failures.failed_assertions_by_case
        listed = by_case["simple_fact-000"]
        assert [item.name for item in listed] == ["final_state", "depth_level"]
        assert [item.expectation_kind for item in listed] == ["required", "forbidden"]

    def test_the_index_carries_no_volatile_text(self) -> None:
        """🔴 索引的用途是"去哪查"，不是"把报告再抄一遍"。"""
        dataset = _dataset({"simple_fact": 1})
        result = _run(
            (
                _result(
                    "simple_fact-000",
                    passed=False,
                    failure_kind="assertion_failure",
                    assertions=(_assertion("a", passed=False),),
                ),
            )
        )
        rendered = compute_metrics(dataset, result).failures.model_dump_json()
        assert "response_text" not in rendered
        assert "Traceback" not in rendered
        assert "postgresql" not in rendered
        assert "detail" not in rendered


class TestConsistencyInvariants:
    """H 组：汇总不变量——**错的汇总必须构造失败**。"""

    def test_case_counts_must_add_up(self) -> None:
        with pytest.raises(ValidationError, match="!= executed"):
            CaseMetrics(
                total_cases=3,
                executed_cases=3,
                passed_cases=2,
                failed_cases=0,  # 2 + 0 != 3
                not_executed_cases=0,
                execution_error_cases=0,
                assertion_failed_cases=0,
                case_pass_rate=RatioMetric(numerator=2, denominator=3, value="0.666667"),
                execution_coverage=RatioMetric(numerator=3, denominator=3, value="1.000000"),
            )

    def test_executed_plus_not_executed_must_equal_total(self) -> None:
        with pytest.raises(ValidationError, match="!= total"):
            CaseMetrics(
                total_cases=3,
                executed_cases=2,
                passed_cases=2,
                failed_cases=0,
                not_executed_cases=0,  # 2 + 0 != 3
                execution_error_cases=0,
                assertion_failed_cases=0,
                case_pass_rate=RatioMetric(numerator=2, denominator=2, value="1.000000"),
                execution_coverage=RatioMetric(numerator=2, denominator=3, value="0.666667"),
            )

    def test_failure_kinds_must_be_exhaustive(self) -> None:
        """每个失败案例必须恰好属于 execution_error 或 assertion_failure。"""
        with pytest.raises(ValidationError, match="恰好属于其中一类"):
            CaseMetrics(
                total_cases=1,
                executed_cases=1,
                passed_cases=0,
                failed_cases=1,
                not_executed_cases=0,
                execution_error_cases=0,
                assertion_failed_cases=0,  # 0 + 0 != 1
                case_pass_rate=RatioMetric(numerator=0, denominator=1, value="0.000000"),
                execution_coverage=RatioMetric(numerator=1, denominator=1, value="1.000000"),
            )

    def test_assertion_counts_must_add_up(self) -> None:
        with pytest.raises(ValidationError, match="!= evaluated"):
            AssertionGroupMetrics(
                total=3,
                evaluated=2,
                passed=1,
                failed=0,  # 1 + 0 != 2
                unobservable=1,
                pass_rate=RatioMetric(numerator=1, denominator=2, value="0.500000"),
                observation_coverage=RatioMetric(numerator=2, denominator=3, value="0.666667"),
            )

    def test_evaluated_plus_unobservable_must_equal_total(self) -> None:
        with pytest.raises(ValidationError, match="!= total"):
            AssertionGroupMetrics(
                total=3,
                evaluated=2,
                passed=2,
                failed=0,
                unobservable=0,  # 2 + 0 != 3
                pass_rate=RatioMetric(numerator=2, denominator=2, value="1.000000"),
                observation_coverage=RatioMetric(numerator=2, denominator=3, value="0.666667"),
            )

    def test_required_plus_forbidden_must_equal_overall(self) -> None:
        overall = AssertionGroupMetrics(
            total=3,
            evaluated=3,
            passed=3,
            failed=0,
            unobservable=0,
            pass_rate=RatioMetric(numerator=3, denominator=3, value="1.000000"),
            observation_coverage=RatioMetric(numerator=3, denominator=3, value="1.000000"),
        )
        one = AssertionGroupMetrics(
            total=1,
            evaluated=1,
            passed=1,
            failed=0,
            unobservable=0,
            pass_rate=RatioMetric(numerator=1, denominator=1, value="1.000000"),
            observation_coverage=RatioMetric(numerator=1, denominator=1, value="1.000000"),
        )
        with pytest.raises(ValidationError, match="required \\+ forbidden"):
            AssertionMetrics(overall=overall, required=one, forbidden=one)

    def test_category_totals_must_match_the_overall(self) -> None:
        """伪造一份"类别之和与总体对不上"的指标，必须被拒绝。"""
        dataset = _dataset({"simple_fact": 2})
        result = _run((_result("simple_fact-000"), _result("simple_fact-001")))
        metrics = compute_metrics(dataset, result)
        forged = metrics.model_dump(mode="json")
        # ⚠️ 伪造手法是**删掉整整一个类别条目**，而不是改其中一项计数：
        # 后者会先被 ``CategoryMetrics`` 自己的不变量拦下（``passed + failed
        # != executed``），于是测到的是子模型的校验，而不是这里的汇总校验。
        # 删条目之后每条明细都自洽，只有"加起来对不上"这一件事能被发现——
        # 那正是这条不变量存在的意义。
        forged["categories"] = []
        with pytest.raises(ValidationError, match="类别 total 之和"):
            EvaluationMetrics.model_validate(forged)

    def test_assertion_name_totals_must_match_the_overall(self) -> None:
        dataset = _dataset({"simple_fact": 1})
        result = _run((_result("simple_fact-000", assertions=(_assertion("a"), _assertion("b"))),))
        metrics = compute_metrics(dataset, result)
        forged = metrics.model_dump(mode="json")
        # 同上：删掉一个断言名条目，而不是改它的内部计数。
        forged["assertion_names"] = forged["assertion_names"][:1]
        with pytest.raises(ValidationError, match="按名称的 total 之和"):
            EvaluationMetrics.model_validate(forged)

    def test_category_internals_must_add_up(self) -> None:
        with pytest.raises(ValidationError, match="passed \\+ failed"):
            CategoryMetrics(
                category="simple_fact",
                total=2,
                executed=2,
                passed=1,
                failed=0,
                not_executed=0,
                pass_rate=RatioMetric(numerator=1, denominator=2, value="0.500000"),
            )


class TestMetricsIdentityAndDeterminism:
    """指标契约身份与确定性。"""

    def test_schema_version_is_present(self) -> None:
        dataset = _dataset({"simple_fact": 1})
        metrics = compute_metrics(dataset, _run((_result("simple_fact-000"),)))
        assert metrics.metrics_schema_version == METRICS_SCHEMA_VERSION

    def test_the_definition_digest_is_present_and_stable(self) -> None:
        assert metrics_definition_digest() == metrics_definition_digest()
        assert metrics_definition_digest().startswith("sha256:")

    def test_the_digest_covers_the_ratio_precision(self) -> None:
        """🔴 只写一个 ``version: 1`` 是不够的——它没法说明分母口径。"""
        from ai_psi.evaluation import metrics as metrics_module

        baseline = metrics_definition_digest()
        original = metrics_module.METRICS_DEFINITION["ratio_precision"]
        metrics_module.METRICS_DEFINITION["ratio_precision"] = 99
        try:
            assert metrics_definition_digest() != baseline
        finally:
            metrics_module.METRICS_DEFINITION["ratio_precision"] = original

    def test_the_digest_covers_the_case_pass_rate_denominator(self) -> None:
        from ai_psi.evaluation import metrics as metrics_module

        baseline = metrics_definition_digest()
        original = metrics_module.METRICS_DEFINITION["case_pass_rate_denominator"]
        metrics_module.METRICS_DEFINITION["case_pass_rate_denominator"] = "total_cases"
        try:
            assert metrics_definition_digest() != baseline
        finally:
            metrics_module.METRICS_DEFINITION["case_pass_rate_denominator"] = original

    def test_computing_twice_gives_the_same_object(self) -> None:
        dataset = _dataset({"simple_fact": 2})
        result = _run((_result("simple_fact-000"), _result("simple_fact-001")))
        assert compute_metrics(dataset, result) == compute_metrics(dataset, result)

    def test_serialization_is_byte_identical(self) -> None:
        from ai_psi.evaluation.serialization import dumps

        dataset = _dataset({"simple_fact": 2})
        result = _run((_result("simple_fact-000"), _result("simple_fact-001")))
        first = dumps(compute_metrics(dataset, result).model_dump(mode="json"))
        second = dumps(compute_metrics(dataset, result).model_dump(mode="json"))
        assert first == second

    def test_the_metrics_reject_unknown_fields(self) -> None:
        """🔴 ``extra="forbid"``：多一个没人读的字段是"这份指标说了什么"的常见答案。"""
        with pytest.raises(ValidationError):
            RatioMetric.model_validate(
                {"numerator": 1, "denominator": 1, "value": "1.000000", "surprise": 1}
            )

    def test_computing_does_not_modify_the_result(self) -> None:
        """🔴 计算器是纯的：它读入参，不改入参。"""
        dataset = _dataset({"simple_fact": 1})
        result = _run((_result("simple_fact-000"),))
        before = result.model_dump(mode="json")
        compute_metrics(dataset, result)
        assert result.model_dump(mode="json") == before
