"""Baseline / Candidate 结构化对比（阶段 7 · S5）。

对应任务书 §二十六 的 A、C–O 组（B 组在 ``test_comparison_input.py``，
N 组的 CLI 部分在 ``test_compare_cli.py``）。

🔴 全部输入由 ``comparison_factory`` **按正式模型构造**，
指标由 ``compute_metrics`` 重算——没有一份是手拼的 JSON。
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from ai_psi.evaluation.comparison import (
    ALLOWED_DIFFERENCE_CODES,
    BLOCKER_CODES,
    BLOCKER_PARTIAL_RUN,
    COMPARISON_SCHEMA_VERSION,
    DELTA_PRECISION,
    AssertionStatus,
    AssertionTransitionType,
    CaseTransitionType,
    ComparisonEligibility,
    ComparisonInputError,
    ComparisonRole,
    CountDelta,
    DeltaDirection,
    EvaluationComparison,
    RatioDelta,
    assertion_key,
    compare_run_results,
    comparison_definition_digest,
    evaluate_comparison_eligibility,
    load_run_result,
    write_comparison,
)
from ai_psi.evaluation.loader import GoldenDataset
from ai_psi.evaluation.metrics import RatioMetric

pytestmark = pytest.mark.unit

#: 测试用的两个合成提交号。与 conftest 里的是同一对值——
#: 这里重复写一遍只是为了不必从 conftest 导入常量（那不是它的公开面）。
SHA_A = "a" * 40
SHA_B = "b" * 40


def _metrics(comparison: EvaluationComparison) -> Any:
    assert comparison.metrics_comparison is not None
    return comparison.metrics_comparison


def _cases(comparison: EvaluationComparison) -> tuple[Any, ...]:
    assert comparison.case_transitions is not None
    return comparison.case_transitions


def _assertions(comparison: EvaluationComparison) -> tuple[Any, ...]:
    assert comparison.assertion_transitions is not None
    return comparison.assertion_transitions


def _distributions(comparison: EvaluationComparison) -> Any:
    assert comparison.distribution_deltas is not None
    return comparison.distribution_deltas


def _failures(comparison: EvaluationComparison) -> Any:
    assert comparison.failure_index_delta is not None
    return comparison.failure_index_delta


class TestComparisonIdentity:
    """A 组：契约身份与定义摘要。"""

    def test_schema_version_is_one(self) -> None:
        assert COMPARISON_SCHEMA_VERSION == 1

    def test_definition_digest_is_stable_across_calls(self) -> None:
        """摘要必须是**纯函数**：同一个进程里连着算两次也得一样。"""
        assert comparison_definition_digest() == comparison_definition_digest()
        assert comparison_definition_digest().startswith("sha256:")

    def test_definition_records_the_direction_rule(self) -> None:
        """方向必须**进摘要**：改了方向而摘要不变，等于宣称两份按不同
        方向算出来的差异可以放在一起看。"""
        from ai_psi.evaluation.comparison import COMPARISON_DEFINITION

        assert COMPARISON_DEFINITION["delta_direction"] == "candidate - baseline"
        assert COMPARISON_DEFINITION["count_delta"] == "delta = candidate - baseline"

    def test_definition_records_all_blocker_codes(self) -> None:
        """阻塞规则表进摘要。少一条，摘要就覆盖不到那条规则。"""
        from ai_psi.evaluation.comparison import COMPARISON_DEFINITION

        assert COMPARISON_DEFINITION["blockers"] == list(BLOCKER_CODES)
        assert COMPARISON_DEFINITION["allowed_differences"] == list(ALLOWED_DIFFERENCE_CODES)

    def test_definition_records_ordering_and_precision(self) -> None:
        from ai_psi.evaluation.comparison import COMPARISON_DEFINITION

        assert "ordering" in COMPARISON_DEFINITION
        assert COMPARISON_DEFINITION["delta_precision"] == DELTA_PRECISION
        assert COMPARISON_DEFINITION["delta_rounding"] == "ROUND_HALF_UP"

    def test_definition_records_both_transition_tables(self) -> None:
        from ai_psi.evaluation.comparison import COMPARISON_DEFINITION

        assert len(COMPARISON_DEFINITION["case_transitions"]) == 4  # type: ignore[arg-type]
        assert len(COMPARISON_DEFINITION["assertion_transitions"]) == 9  # type: ignore[arg-type]

    def test_definition_has_no_volatile_content(self) -> None:
        """🔴 摘要里不得出现函数地址、绝对路径或时间戳。

        出现即意味着"同一个契约在不同机器/不同时刻摘要不同"——
        那它就不是契约，只是一个每次都不一样的内存快照。
        """
        from ai_psi.evaluation.comparison import COMPARISON_DEFINITION

        text = json.dumps(COMPARISON_DEFINITION, ensure_ascii=False)
        assert "0x" not in text, "摘要输入里出现了疑似函数内存地址"
        assert ":\\" not in text, "摘要输入里出现了 Windows 绝对路径"
        assert "/home/" not in text and "/Users/" not in text

    def test_digest_follows_the_code_revision_policy(self) -> None:
        """``code_revision_differs`` 的允许规则必须写在摘要里。"""
        from ai_psi.evaluation.comparison import COMPARISON_DEFINITION

        assert ALLOWED_DIFFERENCE_CODES == ("code_revision_differs",)
        policy = str(COMPARISON_DEFINITION["code_revision_policy"])
        assert "允许比较" in policy
        assert "40 位" in policy


class TestEligibility:
    """D 组：可比较性判定。"""

    def test_identical_runs_are_eligible(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        left = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        comparison = compare_run_results(left, left)
        assert comparison.comparison_eligible is True
        assert comparison.manifest_identity_equal is True
        assert comparison.blockers == ()
        assert comparison.allowed_differences == ()

    def test_only_code_revision_differs_is_still_eligible(self, comparison_factory: Any) -> None:
        """🔴 **S5 的核心行为**（任务书 §三 的例子 A）。

        代码提交不同正是版本对比的主题——拒绝它等于拒绝做这件事。
        但 S3 必须照旧说"它们不是同一个代码版本"。
        """
        dataset = comparison_factory.dataset(["case-001"])
        outcomes = {"case-001": "pass"}
        left = comparison_factory.run(dataset, outcomes, commit_sha=SHA_A)
        right = comparison_factory.run(dataset, outcomes, commit_sha=SHA_B)

        eligibility = evaluate_comparison_eligibility(left, right)
        assert eligibility.comparison_eligible is True
        assert eligibility.blockers == ()
        # 🔴 允许 ≠ 没发生：差异仍然被**记录**下来。
        assert eligibility.allowed_differences == ("code_revision_differs",)
        # 🔴 S3 的结论原样保留：身份契约**不**相同。
        assert eligibility.manifest_identity_equal is False

    def test_s3_verdict_is_reused_not_reimplemented(self, comparison_factory: Any) -> None:
        """``manifest_identity_equal`` 就是 S3 的 ``comparable``。"""
        from ai_psi.evaluation.manifest import compare_manifests

        dataset = comparison_factory.dataset(["case-001"])
        outcomes = {"case-001": "pass"}
        left = comparison_factory.run(dataset, outcomes, commit_sha=SHA_A)
        right = comparison_factory.run(dataset, outcomes, commit_sha=SHA_B)
        s3 = compare_manifests(left.manifest, right.manifest)
        assert s3.comparable is False
        assert "code_revision_differs" in s3.reasons
        assert evaluate_comparison_eligibility(left, right).manifest_identity_equal is s3.comparable

    def test_identical_sha_reports_code_revision_agreement(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        outcomes = {"case-001": "pass"}
        left = comparison_factory.run(dataset, outcomes, commit_sha=SHA_A)
        right = comparison_factory.run(dataset, outcomes, commit_sha=SHA_A)
        eligibility = evaluate_comparison_eligibility(left, right)
        assert eligibility.manifest_identity_equal is True
        assert eligibility.allowed_differences == ()

    @pytest.mark.parametrize(
        ("section", "changes", "expected"),
        [
            ("evaluation", {"dataset_digest": "sha256:other"}, "dataset_differs"),
            (
                "evaluation",
                {"assertion_registry_digest": "sha256:other"},
                "assertion_registry_differs",
            ),
            ("evaluation", {"case_schema_version": 2}, "manifest_schema_differs"),
            ("prompts", {"digest": "sha256:other"}, "prompt_versions_differ"),
            ("provider", {"provider_name": "other"}, "provider_differs"),
            ("provider", {"model_id": "other"}, "model_differs"),
            (
                "provider",
                {"configuration_digest": "sha256:other"},
                "provider_configuration_differs",
            ),
            ("provider", {"network_allowed": True}, "network_policy_differs"),
            ("storage", {"backend": "postgresql"}, "storage_backend_differs"),
            ("storage", {"alembic_revision": "b7f1c9d4e2a3"}, "migration_revision_differs"),
            ("code", {"working_tree_clean": False}, "dirty_worktree"),
            ("code", {"commit_sha": None}, "identity_unavailable"),
            ("code", {"package_version": "9.9.9"}, "package_version_differs"),
            ("runtime", {"python_version": "3.12.0"}, "python_version_differs"),
        ],
    )
    def test_identity_difference_blocks(
        self, comparison_factory: Any, section: str, changes: dict[str, Any], expected: str
    ) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        outcomes = {"case-001": "pass"}
        left = comparison_factory.run(dataset, outcomes)
        right = comparison_factory.patch(left, section, **changes)
        eligibility = evaluate_comparison_eligibility(left, right)
        assert eligibility.comparison_eligible is False
        assert expected in eligibility.blockers

    @pytest.mark.parametrize(
        "changes",
        [
            {"commit_sha": "abc123"},  # 短 SHA
            {"commit_sha": "z" * 40},  # 非十六进制
            {"commit_sha": ""},
        ],
    )
    def test_incomplete_sha_blocks(self, comparison_factory: Any, changes: dict[str, Any]) -> None:
        """🔴 SHA 缺失或不是**完整 40 位十六进制**都阻塞。"""
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        right = comparison_factory.patch(left, "code", **changes)
        eligibility = evaluate_comparison_eligibility(left, right)
        assert eligibility.comparison_eligible is False
        assert "identity_unavailable" in eligibility.blockers

    def test_execution_mode_difference_blocks(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        right = comparison_factory.set_execution_mode(left, "postgres_http")
        eligibility = evaluate_comparison_eligibility(left, right)
        assert "execution_mode_differs" in eligibility.blockers

    def test_python_patch_difference_does_not_block(self, comparison_factory: Any) -> None:
        """补丁版本是**运行环境**的属性，不是结果语义的属性。"""
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        right = comparison_factory.patch(left, "runtime", python_version="3.13.99")
        eligibility = evaluate_comparison_eligibility(left, right)
        assert eligibility.comparison_eligible is True

    def test_python_minor_difference_blocks(self, comparison_factory: Any) -> None:
        """major/minor 不同是真差异：语言行为可能不一样。"""
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        right = comparison_factory.patch(left, "runtime", python_version="3.14.0")
        assert "python_version_differs" in evaluate_comparison_eligibility(left, right).blockers

    def test_unparseable_python_version_blocks_as_unavailable(
        self, comparison_factory: Any
    ) -> None:
        """读不懂的版本号不是"没差异"，是**身份不可用**。"""
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        right = comparison_factory.patch(left, "runtime", python_version="unknown")
        assert "identity_unavailable" in evaluate_comparison_eligibility(left, right).blockers

    def test_blockers_are_sorted_and_deduplicated(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        right = comparison_factory.patch(left, "evaluation", dataset_digest="sha256:other")
        right = comparison_factory.patch(right, "provider", model_id="other")
        blockers = evaluate_comparison_eligibility(left, right).blockers
        assert list(blockers) == sorted(blockers)
        assert len(set(blockers)) == len(blockers)

    def test_eligibility_model_rejects_inconsistent_flags(self) -> None:
        """可比较与"没有阻塞项"必须等价——否则这条判据会在每个调用点被重写。"""
        with pytest.raises(ValueError, match="不自洽"):
            ComparisonEligibility(
                manifest_identity_equal=True,
                comparison_eligible=True,
                allowed_differences=(),
                blockers=("dataset_differs",),
            )

    def test_s3_comparability_semantics_are_untouched(self, comparison_factory: Any) -> None:
        """🔴 S5 **不得**修改 S3：不同 SHA 时 S3 仍然说"不可比"。"""
        from ai_psi.evaluation.manifest import compare_manifests

        dataset = comparison_factory.dataset(["case-001"])
        outcomes = {"case-001": "pass"}
        left = comparison_factory.run(dataset, outcomes, commit_sha=SHA_A)
        right = comparison_factory.run(dataset, outcomes, commit_sha=SHA_B)
        s3 = compare_manifests(left.manifest, right.manifest)
        assert s3.comparable is False
        assert "code_revision_differs" in s3.reasons


class TestCaseSet:
    """E 组：案例集合完整性。"""

    def test_same_case_set_passes(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        outcomes = {"case-001": "pass", "case-002": "fail"}
        left = comparison_factory.run(dataset, outcomes)
        comparison = compare_run_results(left, left)
        assert comparison.comparison_eligible is True
        assert comparison.added_case_ids == ()
        assert comparison.removed_case_ids == ()

    def test_added_case_blocks_and_is_reported(self, comparison_factory: Any) -> None:
        """🔴 同一数据集下案例**多了**是完整性错误，不是性能变化。"""
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        outcomes = {"case-001": "pass", "case-002": "pass"}
        left = comparison_factory.run(dataset, outcomes)
        right = comparison_factory.run(dataset, outcomes)
        # 只留一条案例：模拟"候选结果少了一条"。
        right = right.model_copy(update={"cases": right.cases[:1]})
        comparison = compare_run_results(left, right)
        assert comparison.comparison_eligible is False
        assert "removed_case_ids" in comparison.model_dump()
        assert comparison.removed_case_ids == ("case-002",)
        assert comparison.added_case_ids == ()

    def test_duplicate_case_id_blocks(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        duplicated = left.model_copy(update={"cases": (left.cases[0], left.cases[0])})
        eligibility = evaluate_comparison_eligibility(duplicated, left)
        assert "duplicate_case_id" in eligibility.blockers
        assert "case_set_inconsistent" in eligibility.blockers

    def test_category_change_blocks(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001"], category="simple_fact")
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        broken_case = left.cases[0].model_copy(update={"category": "evidence_conflict"})
        right = left.model_copy(update={"cases": (broken_case,)})
        assert "case_set_inconsistent" in evaluate_comparison_eligibility(left, right).blockers

    def test_ineligible_comparison_yields_no_partial_numbers(self, comparison_factory: Any) -> None:
        """🔴 不可比较时**一个数字都不出**——这是本模型存在的核心理由。"""
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        right = comparison_factory.patch(left, "evaluation", dataset_digest="sha256:other")
        comparison = compare_run_results(left, right)
        assert comparison.comparison_eligible is False
        assert comparison.metrics_comparison is None
        assert comparison.case_transitions is None
        assert comparison.assertion_transitions is None
        assert comparison.distribution_deltas is None
        assert comparison.failure_index_delta is None
        assert comparison.case_transition_summary is None
        assert comparison.assertion_transition_summary is None


class TestCaseTransitions:
    """F 组：案例转换。"""

    def test_four_transition_types(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001", "case-002", "case-003", "case-004"])
        left = comparison_factory.run(
            dataset,
            {"case-001": "pass", "case-002": "pass", "case-003": "fail", "case-004": "fail"},
        )
        right = comparison_factory.run(
            dataset,
            {"case-001": "pass", "case-002": "fail", "case-003": "pass", "case-004": "fail"},
        )
        comparison = compare_run_results(left, right)
        assert comparison.comparison_eligible is True
        by_id = {item.case_id: item for item in _cases(comparison)}
        assert by_id["case-001"].transition is CaseTransitionType.UNCHANGED_PASS
        assert by_id["case-002"].transition is CaseTransitionType.REGRESSION
        assert by_id["case-003"].transition is CaseTransitionType.IMPROVEMENT
        assert by_id["case-004"].transition is CaseTransitionType.UNCHANGED_FAIL

        summary = comparison.case_transition_summary
        assert summary is not None
        assert summary.total_cases == 4
        assert summary.unchanged_pass_count == 1
        assert summary.regression_transition_count == 1
        assert summary.improvement_transition_count == 1
        assert summary.unchanged_fail_count == 1

    def test_case_transitions_are_sorted_by_case_id(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-003", "case-001", "case-002"])
        outcomes = {"case-001": "pass", "case-002": "fail", "case-003": "pass"}
        left = comparison_factory.run(dataset, outcomes)
        comparison = compare_run_results(left, left)
        ids = [item.case_id for item in _cases(comparison)]
        assert ids == sorted(ids)

    def test_case_transition_carries_structured_context(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        right = comparison_factory.run(dataset, {"case-001": "fail"})
        transition = _cases(compare_run_results(left, right))[0]
        assert transition.category == "simple_fact"
        assert transition.baseline_passed is True
        assert transition.candidate_passed is False
        assert transition.baseline_failure_kind is None
        assert transition.candidate_failure_kind == "assertion_failure"
        assert transition.baseline_final_state == "completed"
        assert transition.candidate_final_state == "failed"
        assert transition.baseline_depth == "d0"
        assert transition.baseline_stop_reason == "DIRECT_ANSWER"

    def test_transition_type_must_match_the_verdicts(self, comparison_factory: Any) -> None:
        from ai_psi.evaluation.comparison import CaseTransition

        with pytest.raises(ValueError, match="与双方判定不一致"):
            CaseTransition(
                case_id="case-001",
                category="simple_fact",
                baseline_passed=True,
                candidate_passed=False,
                transition=CaseTransitionType.UNCHANGED_PASS,
                baseline_failure_kind=None,
                candidate_failure_kind=None,
                baseline_final_state=None,
                candidate_final_state=None,
                baseline_depth=None,
                candidate_depth=None,
                baseline_stop_reason=None,
                candidate_stop_reason=None,
                changed_assertion_keys=(),
            )


class TestAssertionIdentityAndTransitions:
    """G、H 组：断言身份与转换。"""

    def test_assertion_key_is_stable_and_readable(self) -> None:
        key = assertion_key("case-001", 2, mode="required", name="final_state")
        assert key == "case-001#2#required#final_state"
        assert assertion_key("case-001", 2, mode="required", name="final_state") == key

    def test_assertion_key_does_not_depend_on_observed_values(self) -> None:
        """🔴 键里**没有** ``observed`` / ``expected`` / ``detail``。

        用观测值或判定原文当键，会让"改了一句措辞"变成"断言数量变了"。
        """
        key = assertion_key("case-001", 0, mode="required", name="final_state")
        assert "completed" not in key
        assert "0x" not in key

    def test_same_name_may_appear_twice_without_collision(self, comparison_factory: Any) -> None:
        """🔴 同名、同 mode 的多值断言**不得被静默覆盖**。

        ``analysis_module_ran`` 是 ``single_valued=False``：一个案例里
        "逻辑**和**因果都要跑"是完全正常的期望。
        """
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        case = left.cases[0]
        # 在 forbidden 之后追加一条同名断言（多值断言的正常用法）。
        extra = case.assertions[0].model_copy(
            update={"name": "analysis_module_ran", "expected": "logical", "observed": True}
        )
        grown = case.model_copy(update={"assertions": (*case.assertions, extra)})
        result = left.model_copy(update={"cases": (grown,)})
        from ai_psi.evaluation.comparison import _assertion_entries

        entries = _assertion_entries(result)
        assert len(entries) == 3, "同名断言被覆盖了"

    def test_assertion_states_cover_three_values(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001", "case-002", "case-003"])
        left = comparison_factory.run(
            dataset, {"case-001": "pass", "case-002": "fail", "case-003": "unobservable"}
        )
        comparison = compare_run_results(left, left)
        statuses = {item.baseline_status for item in _assertions(comparison)}
        assert statuses == {
            AssertionStatus.PASSED,
            AssertionStatus.FAILED,
            AssertionStatus.UNOBSERVABLE,
        }

    def test_all_nine_transitions_are_mapped(self) -> None:
        from ai_psi.evaluation.comparison import _ASSERTION_TRANSITIONS

        assert len(_ASSERTION_TRANSITIONS) == 9
        expected = {
            (AssertionStatus.PASSED, AssertionStatus.FAILED): AssertionTransitionType.REGRESSION,
            (AssertionStatus.PASSED, AssertionStatus.UNOBSERVABLE): (
                AssertionTransitionType.REGRESSION
            ),
            (AssertionStatus.FAILED, AssertionStatus.PASSED): AssertionTransitionType.IMPROVEMENT,
            (AssertionStatus.UNOBSERVABLE, AssertionStatus.PASSED): (
                AssertionTransitionType.IMPROVEMENT
            ),
            (AssertionStatus.FAILED, AssertionStatus.UNOBSERVABLE): (
                AssertionTransitionType.CHANGED_UNRESOLVED
            ),
            (AssertionStatus.UNOBSERVABLE, AssertionStatus.FAILED): (
                AssertionTransitionType.CHANGED_UNRESOLVED
            ),
        }
        for pair, transition in expected.items():
            assert _ASSERTION_TRANSITIONS[pair] is transition, pair

    def test_unchanged_transitions_are_unchanged(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        transitions = _assertions(compare_run_results(left, left))
        assert transitions
        assert all(item.transition is AssertionTransitionType.UNCHANGED for item in transitions)

    def test_assertion_transition_summary_sums_to_total(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        left = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        right = comparison_factory.run(dataset, {"case-001": "fail", "case-002": "pass"})
        comparison = compare_run_results(left, right)
        summary = comparison.assertion_transition_summary
        assert summary is not None
        total = (
            summary.unchanged_count
            + summary.regression_transition_count
            + summary.improvement_transition_count
            + summary.changed_unresolved_count
        )
        assert total == summary.total_assertions
        assert summary.regression_transition_count > 0

    def test_changed_assertion_keys_are_recorded_on_the_case(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        right = comparison_factory.run(dataset, {"case-001": "fail"})
        transition = _cases(compare_run_results(left, right))[0]
        assert transition.changed_assertion_keys
        assert list(transition.changed_assertion_keys) == sorted(transition.changed_assertion_keys)

    def test_assertion_set_mismatch_blocks(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        case = left.cases[0]
        shrunk = case.model_copy(update={"assertions": case.assertions[:1]})
        right = left.model_copy(update={"cases": (shrunk,)})
        comparison = compare_run_results(left, right)
        assert comparison.comparison_eligible is False
        assert "assertion_set_inconsistent" in comparison.blockers
        assert comparison.removed_assertion_keys


class TestCountDelta:
    """I 组：计数差异。"""

    @pytest.mark.parametrize(
        ("baseline", "candidate", "delta", "direction"),
        [
            (2, 3, 1, DeltaDirection.INCREASED),
            (3, 2, -1, DeltaDirection.DECREASED),
            (2, 2, 0, DeltaDirection.UNCHANGED),
            (0, 5, 5, DeltaDirection.INCREASED),
        ],
    )
    def test_delta_is_candidate_minus_baseline(
        self, baseline: int, candidate: int, delta: int, direction: DeltaDirection
    ) -> None:
        item = CountDelta(baseline=baseline, candidate=candidate, delta=delta, direction=direction)
        assert item.delta == item.candidate - item.baseline

    def test_inconsistent_delta_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="不一致"):
            CountDelta(baseline=2, candidate=3, delta=99, direction=DeltaDirection.INCREASED)

    def test_negative_counts_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            CountDelta(baseline=-1, candidate=3, delta=4, direction=DeltaDirection.INCREASED)

    def test_wrong_direction_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="direction"):
            CountDelta(baseline=2, candidate=3, delta=1, direction=DeltaDirection.DECREASED)

    def test_case_count_deltas_are_zero_for_self_comparison(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        left = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        cases = _metrics(compare_run_results(left, left)).cases
        for field in (
            "total_cases",
            "executed_cases",
            "passed_cases",
            "failed_cases",
            "not_executed_cases",
            "execution_error_cases",
            "assertion_failed_cases",
        ):
            assert getattr(cases, field).delta == 0, field


class TestRatioDelta:
    """J 组：比率差异。"""

    @staticmethod
    def _ratio(numerator: int, denominator: int) -> RatioMetric:
        return RatioMetric(
            numerator=numerator,
            denominator=denominator,
            value=(
                None if denominator == 0 else f"{Decimal(numerator) / Decimal(denominator):.6f}"
            ),
        )

    def test_positive_delta(self) -> None:
        item = RatioDelta(
            baseline=self._ratio(8, 10),
            candidate=self._ratio(9, 10),
            delta="0.100000",
            direction=DeltaDirection.INCREASED,
        )
        assert item.delta == "0.100000"

    def test_negative_delta(self) -> None:
        item = RatioDelta(
            baseline=self._ratio(9, 10),
            candidate=self._ratio(8, 10),
            delta="-0.100000",
            direction=DeltaDirection.DECREASED,
        )
        assert item.delta == "-0.100000"

    def test_zero_delta_is_never_negative_zero(self) -> None:
        item = RatioDelta(
            baseline=self._ratio(1, 2),
            candidate=self._ratio(1, 2),
            delta="0.000000",
            direction=DeltaDirection.UNCHANGED,
        )
        assert item.delta == "0.000000"
        assert not str(item.delta).startswith("-")

    def test_thirds_use_fixed_precision(self) -> None:
        """``1/3 → 2/3`` 必须走 Decimal，不是二进制浮点。

        ⚠️ 差是 ``0.333334`` 而**不是** ``0.333333``：两侧各自先舍入到 6 位
        （``0.333333`` 与 ``0.666667``，后者进位了），相减后带上了两次舍入的
        痕迹。这正是"保留分子分母"的价值所在——只看差值会以为变化恰好是
        三分之一，其实它在 6 位精度上不是。
        """
        item = RatioDelta(
            baseline=self._ratio(1, 3),
            candidate=self._ratio(2, 3),
            delta="0.333334",
            direction=DeltaDirection.INCREASED,
        )
        assert item.delta == "0.333334"
        assert item.baseline.value == "0.333333"
        assert item.candidate.value == "0.666667"

    @pytest.mark.parametrize(
        ("left", "right"),
        [((0, 0), (1, 2)), ((1, 2), (0, 0)), ((0, 0), (0, 0))],
    )
    def test_null_on_either_side_yields_null_delta(
        self, left: tuple[int, int], right: tuple[int, int]
    ) -> None:
        """🔴 ``None`` **不是** ``"0.000000"``。

        一份"0/0"与一份"两边都是 0%"是两回事，把前者写成零差异
        正是本模块要防的那种假指标。
        """
        item = RatioDelta(
            baseline=self._ratio(*left),
            candidate=self._ratio(*right),
            delta=None,
            direction=DeltaDirection.UNAVAILABLE,
        )
        assert item.delta is None
        assert item.direction is DeltaDirection.UNAVAILABLE

    def test_wrong_delta_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="不一致"):
            RatioDelta(
                baseline=self._ratio(8, 10),
                candidate=self._ratio(9, 10),
                delta="0.900000",
                direction=DeltaDirection.INCREASED,
            )

    def test_denominators_are_preserved(self) -> None:
        """🔴 只给差值没法回答"是分子涨了还是分母缩了"。"""
        item = RatioDelta(
            baseline=self._ratio(10, 10),
            candidate=self._ratio(9, 9),
            delta="0.000000",
            direction=DeltaDirection.UNCHANGED,
        )
        assert item.baseline.denominator == 10
        assert item.candidate.denominator == 9
        assert item.baseline.numerator == 10
        assert item.candidate.numerator == 9

    def test_self_comparison_has_zero_ratio_deltas(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        left = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        cases = _metrics(compare_run_results(left, left)).cases
        assert cases.case_pass_rate.delta == "0.000000"
        assert cases.execution_coverage.delta == "0.000000"
        assert cases.case_pass_rate.direction is DeltaDirection.UNCHANGED


class TestCategoryAndNameMetrics:
    """K 组：类别与断言名指标。"""

    def test_categories_are_sorted_and_matched_by_name(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001"], category="simple_fact")
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        categories = _metrics(compare_run_results(left, left)).categories
        assert [item.category for item in categories] == sorted(
            item.category for item in categories
        )

    def test_category_delta_tracks_the_case_delta(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        left = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        right = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "fail"})
        metrics = _metrics(compare_run_results(left, right))
        category = metrics.categories[0]
        assert category.passed.delta == -1
        assert category.failed.delta == 1
        assert category.total.delta == 0
        assert category.pass_rate.delta == "-0.500000"
        assert category.pass_rate.direction is DeltaDirection.DECREASED

    def test_assertion_names_are_sorted(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        names = _metrics(compare_run_results(left, left)).assertion_names
        assert [item.name for item in names] == sorted(item.name for item in names)

    def test_pass_rate_and_observation_coverage_are_not_confused(
        self, comparison_factory: Any
    ) -> None:
        """🔴 两者分母不同：``evaluated`` 与 ``total``。

        一次"不可观测"会同时压低 coverage，却**不**改变 pass_rate 的分母。
        把它们并成一个数字，等于把两种不同的失败混为一谈。
        """
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        right = comparison_factory.run(dataset, {"case-001": "unobservable"})
        metrics = _metrics(compare_run_results(left, right))
        by_name = {item.name: item for item in metrics.assertion_names}
        final_state = by_name["final_state"]
        assert final_state.unobservable.delta == 1
        assert final_state.observation_coverage.direction is DeltaDirection.DECREASED
        assert final_state.pass_rate.delta is None or isinstance(final_state.pass_rate.delta, str)

    def test_required_and_forbidden_are_reported_separately(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        metrics = _metrics(compare_run_results(left, left))
        assert metrics.assertions_required.total.baseline == 1
        assert metrics.assertions_forbidden.total.baseline == 1
        assert metrics.assertions_overall.total.baseline == 2

    def test_name_totals_match_the_overall(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        left = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "fail"})
        right = comparison_factory.run(dataset, {"case-001": "fail", "case-002": "fail"})
        metrics = _metrics(compare_run_results(left, right))
        for field in ("total", "evaluated", "passed", "failed", "unobservable"):
            parts = sum(getattr(item, field).delta for item in metrics.assertion_names)
            assert parts == getattr(metrics.assertions_overall, field).delta, field


class TestDistributions:
    """L 组：分布差异。"""

    def test_distribution_keys_union_missing_as_zero(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        left = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        right = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "error"})
        distributions = _distributions(compare_run_results(left, right))
        # 未执行的案例不进分布，因此 candidate 侧的 completed 少一个。
        assert distributions.final_state["completed"].baseline == 2
        assert distributions.final_state["completed"].candidate == 1
        assert distributions.final_state["completed"].delta == -1

    def test_missing_keys_are_treated_as_zero(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        left = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        right = comparison_factory.run(dataset, {"case-001": "fail", "case-002": "fail"})
        distributions = _distributions(compare_run_results(left, right))
        assert distributions.final_state["failed"].baseline == 0
        assert distributions.final_state["failed"].candidate == 2
        assert distributions.final_state["completed"].candidate == 0

    def test_stop_reason_distribution_uses_the_stable_none_key(
        self, comparison_factory: Any
    ) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        distributions = _distributions(compare_run_results(left, left))
        assert "DIRECT_ANSWER" in distributions.stop_reason
        assert distributions.stop_reason["DIRECT_ANSWER"].delta == 0

    def test_depth_distribution_delta(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        right = comparison_factory.run(dataset, {"case-001": "pass"})
        observation = right.cases[0].observation
        assert observation is not None
        deeper = right.cases[0].model_copy(
            update={"observation": observation.model_copy(update={"depth": "d2"})}
        )
        # 🔴 改完案例必须**重算指标**，否则输入内部先矛盾了，
        # 测试会因为"指标对不上"而失败，与分布本身无关。
        mutated = comparison_factory.revise(right, dataset, cases=(deeper,))
        distributions = _distributions(compare_run_results(left, mutated))
        assert distributions.depth["d0"].delta == -1
        assert distributions.depth["d2"].delta == 1


class TestFailureIndexDelta:
    """M 组：失败索引差异。"""

    def test_all_seven_sets(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001", "case-002", "case-003", "case-004"])
        left = comparison_factory.run(
            dataset,
            {
                "case-001": "pass",
                "case-002": "unobservable",
                "case-003": "error",
                "case-004": "pass",
            },
        )
        right = comparison_factory.run(
            dataset,
            {
                "case-001": "unobservable",
                "case-002": "pass",
                "case-003": "error",
                "case-004": "pass",
            },
        )
        failures = _failures(compare_run_results(left, right))
        assert failures.newly_failed_case_ids == ("case-001",)
        assert failures.resolved_failed_case_ids == ("case-002",)
        assert failures.persistently_failed_case_ids == ("case-003",)
        assert failures.newly_execution_error_case_ids == ()
        assert failures.resolved_execution_error_case_ids == ()
        # 🔴 "新出现不可观测"看的是 ``observation_status``，不是"新失败"——
        # ``case-001`` 两侧都没通过，但只有候选侧是**读不到值**的。
        assert failures.newly_unobservable_case_ids == ("case-001",)
        assert failures.resolved_unobservable_case_ids == ("case-002",)

    def test_new_and_resolved_execution_errors(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        left = comparison_factory.run(dataset, {"case-001": "fail", "case-002": "pass"})
        right = comparison_factory.run(dataset, {"case-001": "error", "case-002": "pass"})
        failures = _failures(compare_run_results(left, right))
        assert failures.newly_execution_error_case_ids == ("case-001",)
        assert failures.resolved_execution_error_case_ids == ()

    def test_ids_are_sorted(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-003", "case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass", "case-003": "pass"})
        right = comparison_factory.run(dataset, {"case-001": "fail", "case-003": "fail"})
        failures = _failures(compare_run_results(left, right))
        assert list(failures.newly_failed_case_ids) == sorted(failures.newly_failed_case_ids)

    def test_self_comparison_has_only_persistent_sets(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        left = comparison_factory.run(dataset, {"case-001": "fail", "case-002": "pass"})
        failures = _failures(compare_run_results(left, left))
        assert failures.newly_failed_case_ids == ()
        assert failures.resolved_failed_case_ids == ()
        assert failures.persistently_failed_case_ids == ("case-001",)


class TestDeterminism:
    """O 组：确定性与方向性。"""

    def test_same_inputs_produce_byte_identical_output(
        self, comparison_factory: Any, tmp_path: Any
    ) -> None:
        from ai_psi.evaluation.serialization import dumps

        dataset = comparison_factory.dataset(["case-001", "case-002"])
        left = comparison_factory.run(
            dataset, {"case-001": "pass", "case-002": "fail"}, commit_sha=SHA_A
        )
        right = comparison_factory.run(
            dataset, {"case-001": "fail", "case-002": "fail"}, commit_sha=SHA_B
        )
        first = dumps(compare_run_results(left, right).model_dump(mode="json"))
        second = dumps(compare_run_results(left, right).model_dump(mode="json"))
        assert first == second

    def test_written_file_ends_with_one_newline_and_uses_lf(
        self, comparison_factory: Any, tmp_path: Any
    ) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        path = tmp_path / "comparison.json"
        write_comparison(compare_run_results(left, left), path)
        raw = path.read_bytes()
        assert b"\r\n" not in raw
        assert raw.endswith(b"\n")
        assert not raw.endswith(b"\n\n")

    def test_swapping_roles_inverts_every_delta(self, comparison_factory: Any) -> None:
        """🔴 **方向专项**：交换双方后，每一条差异都必须反向。

        不建立这条测试，"方向是 candidate - baseline"就只是一句注释。
        """
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        left = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        right = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "fail"})

        forward = _metrics(compare_run_results(left, right))
        backward = _metrics(compare_run_results(right, left))
        assert forward.cases.passed_cases.delta == -1
        assert backward.cases.passed_cases.delta == 1
        assert forward.cases.failed_cases.delta == 1
        assert backward.cases.failed_cases.delta == -1

    def test_swapping_roles_swaps_transitions(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        right = comparison_factory.run(dataset, {"case-001": "fail"})

        forward = {item.case_id: item for item in _cases(compare_run_results(left, right))}
        backward = {item.case_id: item for item in _cases(compare_run_results(right, left))}
        assert forward["case-001"].transition is CaseTransitionType.REGRESSION
        assert backward["case-001"].transition is CaseTransitionType.IMPROVEMENT

        f_assert = [item.transition for item in _assertions(compare_run_results(left, right))]
        b_assert = [item.transition for item in _assertions(compare_run_results(right, left))]
        assert AssertionTransitionType.REGRESSION in f_assert
        assert AssertionTransitionType.IMPROVEMENT in b_assert

    def test_swapping_roles_swaps_failure_index(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        left = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "fail"})
        right = comparison_factory.run(dataset, {"case-001": "fail", "case-002": "fail"})

        forward = _failures(compare_run_results(left, right))
        backward = _failures(compare_run_results(right, left))
        assert forward.newly_failed_case_ids == ("case-001",)
        assert backward.resolved_failed_case_ids == ("case-001",)
        assert forward.newly_failed_case_ids == backward.resolved_failed_case_ids

    def test_swapping_roles_swaps_identities(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        outcomes = {"case-001": "pass"}
        left = comparison_factory.run(dataset, outcomes, commit_sha=SHA_A)
        right = comparison_factory.run(dataset, outcomes, commit_sha=SHA_B)

        forward = compare_run_results(left, right)
        backward = compare_run_results(right, left)
        assert forward.baseline.commit_sha == SHA_A
        assert backward.baseline.commit_sha == SHA_B
        assert forward.baseline.role is ComparisonRole.BASELINE
        assert backward.candidate.role is ComparisonRole.CANDIDATE

    def test_ratio_deltas_invert_cleanly(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        left = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        right = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "fail"})
        forward = _metrics(compare_run_results(left, right)).cases.case_pass_rate
        backward = _metrics(compare_run_results(right, left)).cases.case_pass_rate
        assert forward.delta == "-0.500000"
        assert backward.delta == "0.500000"


class TestComparisonModelGuards:
    """模型级守卫：这些不是"约定"，是**构造失败**。"""

    def test_comparison_rejects_release_conclusion_fields(self, comparison_factory: Any) -> None:
        """🔴 发布结论字段**根本不存在**——多写一个就是校验失败。"""
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        payload = compare_run_results(left, left).model_dump(mode="json")
        for forbidden in (
            "release_allowed",
            "gate_passed",
            "quality_score",
            "risk_score",
            "recommendation",
        ):
            assert forbidden not in payload
            payload[forbidden] = True
        with pytest.raises(ValueError):
            EvaluationComparison.model_validate(payload)

    def test_comparison_has_neither_timestamp_nor_paths(self, comparison_factory: Any) -> None:
        from ai_psi.evaluation.serialization import dumps

        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        text = dumps(compare_run_results(left, left).model_dump(mode="json"))
        assert "generated_at" not in text
        assert "timestamp" not in text
        assert ":\\" not in text
        assert "postgresql://" not in text

    def test_comparison_carries_no_response_text(self, comparison_factory: Any) -> None:
        from ai_psi.evaluation.serialization import dumps

        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        text = dumps(compare_run_results(left, left).model_dump(mode="json"))
        assert "合成的回答文本" not in text
        assert "response_text" not in text
        assert "detail" not in text


class TestPartialRuns:
    """部分运行（S5 补丁）：合法但证据不全，必须阻塞，且**不得**被误诊。

    🔴 它既不是"数据损坏"也不是"数据集变了"。把它报成
    ``case_set_inconsistent`` 会把使用者引向"去查数据集是不是被改过"
    这个错误方向；而要求类别集合相等，会让每一次部分运行都被误报成
    "指标文件坏了"。
    """

    @staticmethod
    def _partial(factory: Any, dataset: Any, full: Any, indices: Any) -> Any:
        """取 ``full`` 的一个子集，并**重算指标**（保持输入自洽）。"""
        return factory.revise(full, dataset, cases=tuple(full.cases[i] for i in indices))

    def _fixture(self, comparison_factory: Any) -> tuple[Any, Any, Any]:
        dataset = comparison_factory.dataset([f"case-{index:03d}" for index in range(1, 5)])
        outcomes = {case.case_id: "pass" for case in dataset.cases}
        full = comparison_factory.run(dataset, outcomes)
        return dataset, full, self._partial(comparison_factory, dataset, full, range(0, 2))

    def test_any_partial_run_blocks(self, comparison_factory: Any) -> None:
        """🔴 四个场景全部阻塞：任一侧没跑完数据集就不做差异分析。"""
        dataset, full, partial = self._fixture(comparison_factory)
        other = self._partial(comparison_factory, dataset, full, range(2, 4))

        scenarios = (
            ("完整 vs 部分", full, partial),
            ("部分 vs 完整", partial, full),
            ("双方部分（同一子集）", partial, partial),
            ("双方部分（不同子集）", partial, other),
        )
        for label, left, right in scenarios:
            comparison = compare_run_results(left, right)
            assert comparison.comparison_eligible is False, label
            assert BLOCKER_PARTIAL_RUN in comparison.blockers, label
            # 不可比较 ⇒ 一个数字都不出。
            assert comparison.metrics_comparison is None, label
            assert comparison.case_transitions is None, label
            assert comparison.assertion_transitions is None, label
            assert comparison.distribution_deltas is None, label
            assert comparison.failure_index_delta is None, label

    def test_both_partial_with_different_subsets_also_reports_the_set_problem(
        self, comparison_factory: Any
    ) -> None:
        """执行集合确实不同时，两个原因都要报——它们是**两件事**。"""
        dataset, full, partial = self._fixture(comparison_factory)
        other = self._partial(comparison_factory, dataset, full, range(2, 4))
        comparison = compare_run_results(partial, other)
        assert BLOCKER_PARTIAL_RUN in comparison.blockers
        assert "case_set_inconsistent" in comparison.blockers
        assert "assertion_set_inconsistent" in comparison.blockers

    def test_both_partial_with_the_same_subset_reports_only_partial_run(
        self, comparison_factory: Any
    ) -> None:
        """两侧执行的是同一批案例：集合问题不存在，**只有**"没跑完"这件事。"""
        _, _, partial = self._fixture(comparison_factory)
        assert compare_run_results(partial, partial).blockers == (BLOCKER_PARTIAL_RUN,)

    def test_partial_run_is_never_reported_as_corrupt_metrics(
        self, comparison_factory: Any
    ) -> None:
        """🔴 **回归**：部分运行不得被报成 ``input_metrics_mismatch``。

        存储的类别来自**数据集**，部分运行时它会包含"一条都没跑"的类别；
        而重算的类别只能来自执行过的案例——那些类别在结果里根本不存在。
        要求两者**相等**，就会把每一次部分运行都诬告成"指标文件坏了"。
        这里用**两个类别**构造，正是为了让那个错误暴露出来。
        """
        split = comparison_factory.dataset(["case-001"], category="simple_fact")
        other = comparison_factory.dataset(["case-002"], category="evidence_conflict")
        dataset = GoldenDataset(root=split.root, cases=split.cases + other.cases)
        full = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        # 只跑了 simple_fact 那一类：evidence_conflict 的 executed 为 0。
        partial = comparison_factory.revise(full, dataset, cases=full.cases[:1])

        comparison = compare_run_results(partial, partial)
        assert comparison.blockers == (BLOCKER_PARTIAL_RUN,)
        # 🔴 具体不一致项为空：没有任何一项指标与重算结果对不上。
        assert comparison.integrity_mismatches == ()
        assert comparison.comparison_eligible is False

    def test_partial_run_still_reports_what_it_could_not_recheck(
        self, comparison_factory: Any
    ) -> None:
        """``integrity_notes`` 的角色：它说"这一项我没能独立复核"，
        **不是**阻塞原因。两者同时存在，各说各的事。"""
        _, _, partial = self._fixture(comparison_factory)
        comparison = compare_run_results(partial, partial)
        assert comparison.integrity_notes
        assert any("部分运行" in note for note in comparison.integrity_notes)
        assert comparison.integrity_mismatches == ()

    def test_complete_run_is_not_flagged_as_partial(self, comparison_factory: Any) -> None:
        """反向：跑完了就不该出现这个码——否则它只是一个恒真的噪声。"""
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        full = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        comparison = compare_run_results(full, full)
        assert comparison.comparison_eligible is True
        assert BLOCKER_PARTIAL_RUN not in comparison.blockers


class TestInputHandling:
    """与加载器交互的边界（B 组的补充，加载本身在另一个文件里细测）。"""

    def test_round_trip_through_the_report_format(
        self, comparison_factory: Any, tmp_path: Any
    ) -> None:
        from ai_psi.evaluation.serialization import raw_payload, write_reports

        dataset = comparison_factory.dataset(["case-001", "case-002"])
        left = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        raw = tmp_path / "run.json"
        write_reports(left, raw_path=raw, canonical_path=tmp_path / "canonical.json")
        loaded = load_run_result(raw)
        assert loaded == left
        assert raw_payload(loaded) == raw_payload(left)

    def test_canonical_result_is_rejected_with_a_clear_message(
        self, comparison_factory: Any, tmp_path: Any
    ) -> None:
        """🔴 canonical **刻意**不含 ``working_tree_clean``，而那是
        "能不能当基线"的判据——拿它做对比会让那条校验永远无从执行。"""
        import json as jsonlib

        from ai_psi.evaluation.serialization import write_reports

        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        canonical = tmp_path / "canonical.json"
        write_reports(left, raw_path=tmp_path / "raw.json", canonical_path=canonical)
        with pytest.raises(ComparisonInputError, match="canonical"):
            load_run_result(canonical)
        assert jsonlib.loads(canonical.read_text(encoding="utf-8"))["cases"]

    def test_swapped_files_are_not_silently_corrected(
        self, comparison_factory: Any, tmp_path: Any
    ) -> None:
        """同样的两份文件，参数顺序决定方向——工具**不会**替调用方判断。"""
        from ai_psi.evaluation.serialization import write_reports

        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"}, commit_sha=SHA_A)
        right = comparison_factory.run(dataset, {"case-001": "fail"}, commit_sha=SHA_B)
        left_path = tmp_path / "a.json"
        right_path = tmp_path / "b.json"
        write_reports(left, raw_path=left_path, canonical_path=tmp_path / "c1.json")
        write_reports(right, raw_path=right_path, canonical_path=tmp_path / "c2.json")

        forward = compare_run_results(load_run_result(left_path), load_run_result(right_path))
        backward = compare_run_results(load_run_result(right_path), load_run_result(left_path))
        assert forward.baseline.commit_sha == SHA_A
        assert backward.baseline.commit_sha == SHA_B
        assert forward.blockers == () and backward.blockers == ()
