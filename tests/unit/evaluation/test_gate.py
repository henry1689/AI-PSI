"""结构化质量门禁的契约、规则与聚合（阶段 7 · S6）。

对应任务书 §二十一 的 A、E、F、G、H、I、J、L 组。
B、C、D 组在 ``test_gate_input.py``，K 组在 ``test_gate_cli.py``。

🔴 这里的输入一律由**正式模型**构造：合成对比来自 S5 的真实
``compare_run_results``，策略来自 ``gate_factory``（scope 取自对比的
已验证身份，不硬编码）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ai_psi.evaluation.cli import main as cli_main
from ai_psi.evaluation.comparison import EvaluationComparison, compare_run_results, load_run_result
from ai_psi.evaluation.gate import (
    EVIDENCE_KINDS,
    GATE_DECISION_SCHEMA_VERSION,
    GATE_DEFINITION,
    EvidenceKey,
    EvidenceKind,
    ExpectedEvidence,
    GateDecision,
    GateOutcome,
    GatePolicy,
    ObservedEvidence,
    RuleOutcome,
    RuleReason,
    RuleResult,
    RuleType,
    decide,
    evaluate_policy_applicability,
    gate_definition_digest,
    load_gate_policy,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REAL_POLICY = _REPO_ROOT / "evals" / "policies" / "s6_mock_golden_v1.json"


@pytest.fixture(scope="module")
def golden_comparison(tmp_path_factory: pytest.TempPathFactory) -> EvaluationComparison:
    """跑一次**真实的** S1a 内存评测并自比较。

    🔴 首个策略的 scope 绑定的是真实数据集的摘要，合成夹具对不上。
    要验证"这份策略确实适用于它声明的那类评测"，就必须拿真实的那份来。
    模块级：整个文件只跑一次。
    """
    directory = tmp_path_factory.mktemp("s6-golden")
    raw = directory / "run.json"
    code = cli_main(
        [
            "--dataset",
            str(_REPO_ROOT / "evals" / "datasets"),
            "--output",
            str(raw),
            "--canonical-output",
            str(directory / "canonical.json"),
        ]
    )
    assert code == 0
    result = load_run_result(raw)
    manifest = result.manifest
    assert manifest is not None
    # 🔴 归一化**工作树状态**：开发机上代码正在被改，工作树必然是脏的，
    # 而 S5 会因此报 dirty_worktree、整份对比变成不可比较（实测确认）。
    # "这份策略适不适用于这类评测"与开发者此刻有没有提交无关，
    # 所以这里把那个字段归一到干净——conftest 的 ComparisonFactory
    # 用的是同一套做法。**其余身份一律保留真实值**。
    result = result.model_copy(
        update={
            "manifest": manifest.model_copy(
                update={"code": manifest.code.model_copy(update={"working_tree_clean": True})}
            )
        }
    )
    return compare_run_results(result, result)


@pytest.fixture(scope="module")
def real_policy() -> GatePolicy:
    """首个正式策略（**从仓库里读**，不是就地构造的副本）。"""
    return load_gate_policy(_REAL_POLICY)


def _decide(
    gate_factory: Any, comparison: EvaluationComparison, **policy_overrides: object
) -> GateDecision:
    return decide(comparison, gate_factory.policy(comparison, **policy_overrides))


class TestGateIdentity:
    """A 组：门禁契约身份。"""

    def test_schema_version_is_one(self) -> None:
        assert GATE_DECISION_SCHEMA_VERSION == 1

    def test_definition_digest_is_stable(self) -> None:
        assert gate_definition_digest() == gate_definition_digest()
        assert gate_definition_digest().startswith("sha256:")

    def test_definition_records_the_aggregation_rules(self) -> None:
        """聚合规则必须进摘要——改了聚合而摘要不变，等于宣称两个结论
        是按同一套规则得出的。"""
        aggregation = GATE_DEFINITION["aggregation"]
        assert isinstance(aggregation, dict)
        assert aggregation["all_rules_pass"] == "PASS"
        assert aggregation["any_rule_fail"] == "FAIL"
        assert aggregation["any_rule_not_evaluated"] == "NOT_EVALUATED"
        assert aggregation["not_applicable"] == "NOT_EVALUATED（不评估任何质量规则）"

    def test_definition_records_applicability_and_rule_types(self) -> None:
        assert "applicability" in GATE_DEFINITION
        assert GATE_DEFINITION["rule_types"] == [
            "count_equals",
            "id_set_empty",
            "ratio_equals",
            "transition_count_equals",
        ]
        assert GATE_DEFINITION["operators"] == ["EQ", "EMPTY"]

    def test_definition_records_missing_evidence_policy(self) -> None:
        """缺失证据的处置必须写死在契约里——它是本切片最容易走偏的一处。"""
        policy = str(GATE_DEFINITION["evidence_unavailable"])
        assert "绝不" in policy
        assert "当成 0" in policy

    def test_definition_records_decimal_and_sorting_rules(self) -> None:
        assert "Decimal" in str(GATE_DEFINITION["decimal_comparison"])
        assert "字典序" in str(GATE_DEFINITION["sorting"])

    def test_definition_has_no_volatile_content(self) -> None:
        """摘要输入里不得出现函数地址、绝对路径或时间戳。"""
        text = json.dumps(GATE_DEFINITION, ensure_ascii=False)
        assert "0x" not in text
        assert ":\\" not in text
        assert "/home/" not in text and "/Users/" not in text


class TestRuleRegistry:
    """E 组：规则注册表的闭合性。"""

    def test_every_evidence_key_has_a_declared_kind(self) -> None:
        """证据键与形状**一一对应**。少一条，策略就能引用一个没有形状的键。"""
        assert set(EVIDENCE_KINDS) == set(EvidenceKey)

    def test_unknown_rule_type_is_rejected(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison({"case-001": "pass"})
        payload = gate_factory.policy_payload(comparison)
        payload["rules"][0]["rule_type"] = "run_shell_command"
        with pytest.raises(ValueError):
            GatePolicy.model_validate(payload)

    def test_unknown_evidence_key_is_rejected(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison({"case-001": "pass"})
        payload = gate_factory.policy_payload(comparison)
        payload["rules"][0]["evidence_key"] = "case.__dict__.keys()"
        with pytest.raises(ValueError):
            GatePolicy.model_validate(payload)

    def test_unknown_operator_is_rejected(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison({"case-001": "pass"})
        payload = gate_factory.policy_payload(comparison)
        payload["rules"][2]["operator"] = "REGEX"
        with pytest.raises(ValueError):
            GatePolicy.model_validate(payload)

    def test_rule_type_and_evidence_kind_must_agree(self, gate_factory: Any) -> None:
        """``id_set_empty`` 配上计数键必须被拒绝——否则"通过"的含义就说不清了。"""
        comparison = gate_factory.comparison({"case-001": "pass"})
        payload = gate_factory.policy_payload(comparison)
        payload["rules"][1]["evidence_key"] = "candidate.failed_assertions"
        with pytest.raises(ValueError, match="需要集合型证据"):
            GatePolicy.model_validate(payload)

    def test_transition_rule_rejects_candidate_absolute_key(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison({"case-001": "pass"})
        payload = gate_factory.policy_payload(comparison)
        payload["rules"][0]["evidence_key"] = "candidate.failed_assertions"
        with pytest.raises(ValueError, match="转换"):
            GatePolicy.model_validate(payload)

    def test_count_rule_rejects_transition_key(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison({"case-001": "pass"})
        payload = gate_factory.policy_payload(comparison)
        payload["rules"][3]["evidence_key"] = "case.regression_transition_count"
        with pytest.raises(ValueError, match="transition_count_equals"):
            GatePolicy.model_validate(payload)

    def test_duplicate_rule_id_is_rejected(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison({"case-001": "pass"})
        payload = gate_factory.policy_payload(comparison)
        payload["rules"][1]["rule_id"] = payload["rules"][0]["rule_id"]
        with pytest.raises(ValueError, match="重复的 rule_id"):
            GatePolicy.model_validate(payload)

    def test_empty_rule_set_is_rejected(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison({"case-001": "pass"})
        payload = gate_factory.policy_payload(comparison)
        payload["rules"] = []
        with pytest.raises(ValueError):
            GatePolicy.model_validate(payload)

    def test_unknown_top_level_policy_field_is_rejected(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison({"case-001": "pass"})
        payload = gate_factory.policy_payload(comparison)
        payload["enabled"] = True
        with pytest.raises(ValueError):
            GatePolicy.model_validate(payload)


class TestCountRules:
    """F 组：计数规则。"""

    def test_count_equal_to_expected_passes(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison({"case-001": "pass", "case-002": "pass"})
        decision = _decide(gate_factory, comparison)
        assert decision.outcome is GateOutcome.PASS

    def test_count_not_equal_fails(self, gate_factory: Any) -> None:
        """Candidate 多了一条失败断言 → 计数规则失败。"""
        comparison = gate_factory.comparison_between(
            {"case-001": "pass", "case-002": "pass"},
            {"case-001": "pass", "case-002": "fail"},
        )
        decision = _decide(gate_factory, comparison)
        assert decision.outcome is GateOutcome.FAIL
        assert "candidate_failed_assertions_zero" in decision.failed_rule_ids
        failing = next(
            item
            for item in decision.rule_results
            if item.rule_id == "candidate_failed_assertions_zero"
        )
        assert failing.reason_code is RuleReason.COUNT_MISMATCH
        assert failing.observed.count == 1
        assert failing.expected.count == 0

    def test_count_rule_does_not_read_the_delta(self, gate_factory: Any) -> None:
        """🔴 绝对计数规则看的是 **Candidate 的值**，不是 delta。

        ``failed_assertions`` 的 delta 与 candidate 绝对值在这里恰好都是 1，
        但规则必须报告的是**绝对值**——"比基线少差一点"不是通过条件。
        """
        comparison = gate_factory.comparison_between(
            {"case-001": "fail", "case-002": "fail"},
            {"case-001": "pass", "case-002": "fail"},
        )
        decision = _decide(gate_factory, comparison)
        failing = next(
            item
            for item in decision.rule_results
            if item.rule_id == "candidate_failed_assertions_zero"
        )
        # delta 是 -1（改善），但绝对值仍是 1 → 仍然失败。
        metrics = comparison.metrics_comparison
        assert metrics is not None
        assert metrics.assertions_overall.failed.delta == -1
        assert failing.observed.count == 1
        assert failing.outcome is RuleOutcome.FAIL

    def test_missing_count_evidence_is_not_treated_as_zero(self) -> None:
        """🔴 缺失证据**不得**当成 0。

        直接构造一条"观测值缺失"的规则结果，确认它的观测值留空而不是 0——
        在一条期望值为 0 的规则上把缺失填成 0，会让它**通过**。
        """
        result = RuleResult(
            rule_id="candidate_failed_assertions_zero",
            rule_type=RuleType.COUNT_EQUALS,
            outcome=RuleOutcome.NOT_EVALUATED,
            evidence_key=EvidenceKey.CANDIDATE_FAILED_ASSERTIONS,
            observed=ObservedEvidence(kind=EvidenceKind.COUNT, unavailable=True),
            expected=ExpectedEvidence(kind=EvidenceKind.COUNT, count=0),
            reason_code=RuleReason.EVIDENCE_UNAVAILABLE,
        )
        assert result.observed.unavailable is True
        assert result.observed.count is None
        assert result.observed.count != 0


class TestRatioRules:
    """G 组：比率规则。"""

    def test_full_ratio_passes(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison({"case-001": "pass", "case-002": "pass"})
        decision = _decide(gate_factory, comparison)
        ratio_rule = next(
            item
            for item in decision.rule_results
            if item.rule_id == "candidate_case_pass_rate_full"
        )
        assert ratio_rule.outcome is RuleOutcome.PASS
        assert ratio_rule.observed.ratio is not None
        assert ratio_rule.observed.ratio.numerator == 2
        assert ratio_rule.observed.ratio.denominator == 2
        assert ratio_rule.observed.ratio.value == "1.000000"

    def test_denominator_smaller_than_scope_fails_even_at_value_one(self) -> None:
        """🔴 ``5/5`` 与 ``10/10`` 的 value 都是 ``1.000000``。

        只比 value 会漏掉"分母悄悄缩小了"。所以分母必须**同时**对齐
        作用域里的具名计数。
        """
        from ai_psi.evaluation.gate import RatioEqualsRule, _evaluate_ratio_rule

        rule = RatioEqualsRule.model_validate(
            {
                "rule_id": "candidate_case_pass_rate_full",
                "rule_type": "ratio_equals",
                "description": "x",
                "evidence_key": "candidate.case_pass_rate",
                "operator": "EQ",
                "expected": {
                    "value": "1.000000",
                    "numerator_equals_denominator": True,
                    "denominator_gt_zero": True,
                    "denominator_ref": "dataset_case_count",
                },
            }
        )
        policy = _policy_with_scope_counts(dataset_case_count=10, expected_assertion_count=65)
        shrunk = ObservedEvidence(
            kind=EvidenceKind.RATIO,
            ratio=_ratio(5, 5),
        )
        outcome, reason = _evaluate_ratio_rule(rule, shrunk, policy)
        assert outcome is RuleOutcome.FAIL
        assert reason is RuleReason.DENOMINATOR_MISMATCH

    def test_numerator_not_equal_to_denominator_fails(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison_between(
            {"case-001": "pass", "case-002": "pass"},
            {"case-001": "pass", "case-002": "fail"},
        )
        decision = _decide(gate_factory, comparison)
        failing = next(
            item
            for item in decision.rule_results
            if item.rule_id == "candidate_case_pass_rate_full"
        )
        assert failing.outcome is RuleOutcome.FAIL
        assert failing.reason_code is RuleReason.RATIO_MISMATCH
        assert failing.observed.ratio is not None
        assert failing.observed.ratio.value == "0.500000"

    def test_null_ratio_is_not_evaluated_not_passed(self, gate_factory: Any) -> None:
        """🔴 分母为 0 的比率在 S4 里就是 ``null``——它**不是**"通过了"。

        这里让全部断言都不可观测，于是 ``assertion_pass_rate`` 的
        分子分母都是 0、值为 ``null``。
        """
        comparison = gate_factory.comparison(
            {"case-001": "unobservable", "case-002": "unobservable"}
        )
        metrics = comparison.metrics_comparison
        assert metrics is not None
        assert metrics.assertions_overall.pass_rate.candidate.value is None

        payload = gate_factory.policy_payload(
            comparison,
            rules=[
                {
                    "rule_id": "candidate_assertion_pass_rate_full",
                    "rule_type": "ratio_equals",
                    "description": "every evaluated assertion passes",
                    "evidence_key": "candidate.assertion_pass_rate",
                    "operator": "EQ",
                    "expected": {
                        "value": "1.000000",
                        "numerator_equals_denominator": True,
                        "denominator_gt_zero": True,
                        "denominator_ref": "none",
                    },
                }
            ],
        )
        decision = decide(comparison, GatePolicy.model_validate(payload))
        result = decision.rule_results[0]
        assert result.outcome is RuleOutcome.NOT_EVALUATED
        assert result.reason_code is RuleReason.EVIDENCE_UNAVAILABLE
        # 🔴 观测**发生了**（分子分母都是 0），但比率的值是 null，
        # 因此无从比较——这与"压根没观测到"是两回事，两者都不通过，
        # 但报告出来的东西不同。
        assert result.observed.ratio is not None
        assert result.observed.ratio.value is None
        assert decision.outcome is GateOutcome.NOT_EVALUATED

    def test_ratio_uses_decimal_not_float(self) -> None:
        """阈值与观测值都是固定 6 位小数的**字符串**，比较走 ``Decimal``。"""
        from decimal import Decimal

        from ai_psi.evaluation.gate import RatioEqualsRule, _evaluate_ratio_rule

        rule = RatioEqualsRule.model_validate(
            {
                "rule_id": "r",
                "rule_type": "ratio_equals",
                "description": "x",
                "evidence_key": "candidate.case_pass_rate",
                "operator": "EQ",
                "expected": {"value": "1.000000"},
            }
        )
        policy = _policy_with_scope_counts(dataset_case_count=10, expected_assertion_count=65)
        # ``0.999999`` 差一位也不通过。
        outcome, reason = _evaluate_ratio_rule(
            rule, ObservedEvidence(kind=EvidenceKind.RATIO, ratio=_ratio(999999, 1000000)), policy
        )
        assert outcome is RuleOutcome.FAIL
        assert reason is RuleReason.RATIO_MISMATCH
        assert Decimal("0.999999") < Decimal("1.000000")


class TestIdSetRules:
    """H 组：集合规则。"""

    def test_empty_set_passes(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison({"case-001": "pass", "case-002": "pass"})
        decision = _decide(gate_factory, comparison)
        rule = next(
            item for item in decision.rule_results if item.rule_id == "no_newly_failed_cases"
        )
        assert rule.outcome is RuleOutcome.PASS
        assert rule.observed.ids == ()

    def test_non_empty_set_fails(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison_between(
            {"case-001": "pass", "case-002": "pass"},
            {"case-001": "pass", "case-002": "fail"},
        )
        decision = _decide(gate_factory, comparison)
        rule = next(
            item for item in decision.rule_results if item.rule_id == "no_newly_failed_cases"
        )
        assert rule.outcome is RuleOutcome.FAIL
        assert rule.reason_code is RuleReason.UNEXPECTED_IDS_PRESENT
        assert rule.observed.ids == ("case-002",)

    def test_ids_are_stably_sorted(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison_between(
            {"case-003": "pass", "case-001": "pass"},
            {"case-003": "fail", "case-001": "fail"},
        )
        decision = _decide(gate_factory, comparison)
        rule = next(
            item for item in decision.rule_results if item.rule_id == "no_newly_failed_cases"
        )
        assert list(rule.observed.ids) == sorted(rule.observed.ids)


class TestAggregation:
    """I 组：聚合。"""

    def test_all_rules_pass_is_pass(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison({"case-001": "pass"})
        decision = _decide(gate_factory, comparison)
        assert decision.outcome is GateOutcome.PASS
        assert decision.failed_rule_ids == ()
        assert decision.not_evaluated_rule_ids == ()

    def test_one_failure_makes_it_fail(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison_between(
            {"case-001": "pass"},
            {"case-001": "fail"},
        )
        decision = _decide(gate_factory, comparison)
        assert decision.outcome is GateOutcome.FAIL
        assert decision.failed_rule_ids
        assert "no_case_regressions" in decision.failed_rule_ids

    def test_many_failures_still_fail(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison_between(
            {"case-001": "pass", "case-002": "pass"},
            {"case-001": "fail", "case-002": "fail"},
        )
        decision = _decide(gate_factory, comparison)
        assert decision.outcome is GateOutcome.FAIL
        assert len(decision.failed_rule_ids) >= 3

    def test_not_evaluated_beats_fail(self, gate_factory: Any) -> None:
        """🔴 只要有规则没被评估，结论就是 ``NOT_EVALUATED``——**不降级成 FAIL**。

        这两者对使用者的含义不同：``FAIL`` 是"查过了，确实不合格"，
        ``NOT_EVALUATED`` 是"这次没能完整地查"。把后者说成前者是编造结论。
        """
        comparison = gate_factory.comparison(
            {"case-001": "unobservable", "case-002": "unobservable"}
        )
        payload = gate_factory.policy_payload(
            comparison,
            rules=[
                {
                    "rule_id": "candidate_unobservable_assertions_zero",
                    "rule_type": "count_equals",
                    "description": "no unobservable assertions",
                    "evidence_key": "candidate.unobservable_assertions",
                    "operator": "EQ",
                    "expected": 0,
                },
                {
                    "rule_id": "candidate_assertion_pass_rate_full",
                    "rule_type": "ratio_equals",
                    "description": "every evaluated assertion passes",
                    "evidence_key": "candidate.assertion_pass_rate",
                    "operator": "EQ",
                    "expected": {"value": "1.000000", "denominator_ref": "none"},
                },
            ],
        )
        decision = decide(comparison, GatePolicy.model_validate(payload))
        # 第一条明确失败，第二条无法评估 → 整体是 NOT_EVALUATED。
        assert "candidate_unobservable_assertions_zero" in decision.failed_rule_ids
        assert decision.not_evaluated_rule_ids == ("candidate_assertion_pass_rate_full",)
        assert decision.outcome is GateOutcome.NOT_EVALUATED
        # 🔴 原因在**规则层**：策略是适用的（scope 匹配），只是有一条规则
        # 没能评估。把它写成"策略不适用"会把两件事混成一句话。
        assert decision.policy_applicable is True
        assert decision.applicability_reasons == ()
        assert decision.not_evaluated_rule_ids

    def test_model_rejects_pass_with_a_failing_rule(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison({"case-001": "pass"})
        decision = _decide(gate_factory, comparison)
        payload = decision.model_dump(mode="json")
        payload["rule_results"][0]["outcome"] = "FAIL"
        with pytest.raises(ValueError):
            GateDecision.model_validate(payload)

    def test_model_rejects_fail_without_a_failing_rule(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison({"case-001": "pass"})
        decision = _decide(gate_factory, comparison)
        payload = decision.model_dump(mode="json")
        payload["outcome"] = "FAIL"
        with pytest.raises(ValueError, match="至少一条规则明确失败"):
            GateDecision.model_validate(payload)

    def test_model_rejects_not_evaluated_without_a_reason(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison({"case-001": "pass"})
        decision = _decide(gate_factory, comparison)
        payload = decision.model_dump(mode="json")
        payload["outcome"] = "NOT_EVALUATED"
        with pytest.raises(ValueError, match="必须给出原因"):
            GateDecision.model_validate(payload)

    def test_model_rejects_inapplicable_policy_with_rule_results(self, gate_factory: Any) -> None:
        """🔴 不适用时**一条质量规则都不许有**。"""
        comparison = gate_factory.comparison({"case-001": "pass"})
        decision = _decide(gate_factory, comparison)
        payload = decision.model_dump(mode="json")
        payload["policy_applicable"] = False
        payload["applicability_reasons"] = ["dataset_out_of_scope"]
        payload["outcome"] = "NOT_EVALUATED"
        with pytest.raises(ValueError, match="不得评估任何质量规则"):
            GateDecision.model_validate(payload)


class TestFirstPolicy:
    """J 组：首个正式策略（**从仓库里读那份文件**）。"""

    def test_policy_is_applicable_to_the_real_golden_comparison(
        self, real_policy: GatePolicy, golden_comparison: EvaluationComparison
    ) -> None:
        applicability = evaluate_policy_applicability(golden_comparison, real_policy)
        assert applicability.applicable is True
        assert applicability.reasons == ()

    def test_full_self_comparison_passes(
        self, real_policy: GatePolicy, golden_comparison: EvaluationComparison
    ) -> None:
        decision = decide(golden_comparison, real_policy)
        assert decision.policy_applicable is True
        assert decision.outcome is GateOutcome.PASS
        assert decision.failed_rule_ids == ()
        assert decision.not_evaluated_rule_ids == ()
        assert len(decision.rule_results) == len(real_policy.rules)
        assert all(item.outcome is RuleOutcome.PASS for item in decision.rule_results)

    def test_policy_has_fourteen_rules_covering_all_types(self, real_policy: GatePolicy) -> None:
        assert len(real_policy.rules) == 14
        assert {rule.rule_type for rule in real_policy.rules} == set(RuleType)

    def test_policy_binds_relative_and_absolute_rules(self, real_policy: GatePolicy) -> None:
        """🔴 两类规则**都必须有**。

        只比相对差异，会把"Baseline 本来就很差、Candidate 只是少差一点"
        判成通过；只比 Candidate 绝对值，又看不出它比基线退步了什么。
        """
        keys = {rule.evidence_key for rule in real_policy.rules}
        relative = {
            EvidenceKey.CASE_REGRESSION_TRANSITION_COUNT,
            EvidenceKey.ASSERTION_REGRESSION_TRANSITION_COUNT,
            EvidenceKey.ASSERTION_CHANGED_UNRESOLVED_COUNT,
            EvidenceKey.FAILURE_NEWLY_FAILED_CASE_IDS,
            EvidenceKey.FAILURE_NEWLY_EXECUTION_ERROR_CASE_IDS,
            EvidenceKey.FAILURE_NEWLY_UNOBSERVABLE_CASE_IDS,
        }
        assert relative <= keys
        assert keys - relative, "策略里必须有 Candidate 绝对状态规则"

    def test_policy_is_not_a_universal_pass(self, real_policy: GatePolicy) -> None:
        """它不是万能策略：作用域**逐个字段**都绑死了。"""
        scope = real_policy.scope
        assert scope.dataset_digest.startswith("sha256:")
        assert scope.dataset_case_count == 10
        assert scope.expected_assertion_count == 65
        assert scope.provider == "mock"
        assert scope.execution_mode == "in_memory"
        assert scope.storage_backend == "memory"
        assert scope.migration_revision is None

    def test_policy_rules_carry_independent_evidence(self, real_policy: GatePolicy) -> None:
        """每条规则都有**独立、结构化**的证据键——不存在"等价的重复规则"。"""
        seen: set[tuple[str, str]] = set()
        for rule in real_policy.rules:
            pair = (rule.rule_type.value, rule.evidence_key.value)
            assert pair not in seen, f"规则 {rule.rule_id} 与另一条完全等价"
            seen.add(pair)


class TestDeterminismAndSecurity:
    """L 组：确定性与产物安全。"""

    def test_same_inputs_produce_identical_decision(self, gate_factory: Any) -> None:
        from ai_psi.evaluation.serialization import dumps

        comparison = gate_factory.comparison_between(
            {"case-001": "pass", "case-002": "pass"},
            {"case-001": "pass", "case-002": "fail"},
        )
        policy = gate_factory.policy(comparison)
        first = dumps(decide(comparison, policy).model_dump(mode="json"))
        second = dumps(decide(comparison, policy).model_dump(mode="json"))
        assert first == second

    def test_decision_carries_no_volatile_or_banned_content(self, gate_factory: Any) -> None:
        from ai_psi.evaluation.serialization import dumps

        comparison = gate_factory.comparison_between(
            {"case-001": "pass", "case-002": "pass"},
            {"case-001": "pass", "case-002": "fail"},
        )
        text = dumps(decide(comparison, gate_factory.policy(comparison)).model_dump(mode="json"))
        for banned in (
            "release_allowed",
            "deploy_allowed",
            "deployment_allowed",
            "merge_allowed",
            "production_ready",
            "approved_for_production",
            "recommendation",
            "risk_score",
            "quality_score",
            "confidence_score",
            "generated_at",
            "timestamp",
            "response_text",
            "Traceback",
            "postgresql://",
            "合成的回答文本",
            "合成：终态是 failed",
        ):
            assert banned not in text, banned

    def test_decision_model_rejects_a_release_field(self, gate_factory: Any) -> None:
        comparison = gate_factory.comparison({"case-001": "pass"})
        payload = _decide(gate_factory, comparison).model_dump(mode="json")
        payload["release_allowed"] = True
        with pytest.raises(ValueError):
            GateDecision.model_validate(payload)

    def test_comparison_fingerprint_is_content_addressed(self, gate_factory: Any) -> None:
        from ai_psi.evaluation.gate import comparison_fingerprint

        comparison = gate_factory.comparison({"case-001": "pass"})
        first = comparison_fingerprint(comparison)
        assert first == comparison_fingerprint(comparison)
        assert first.startswith("sha256:")
        # 不同内容的对比必须得到不同的指纹。
        other = gate_factory.comparison({"case-001": "pass", "case-002": "pass"})
        assert comparison_fingerprint(other) != first


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _ratio(numerator: int, denominator: int) -> Any:
    from decimal import Decimal

    from ai_psi.evaluation.metrics import RatioMetric

    value = None if denominator == 0 else f"{Decimal(numerator) / Decimal(denominator):.6f}"
    return RatioMetric(numerator=numerator, denominator=denominator, value=value)


def _policy_with_scope_counts(
    *, dataset_case_count: int, expected_assertion_count: int
) -> GatePolicy:
    """只用于直接调用 ``_evaluate_ratio_rule`` 的最小策略。"""
    return GatePolicy.model_validate(
        {
            "policy_schema_version": 1,
            "policy_id": "probe",
            "policy_revision": 1,
            "policy_digest": "sha256:" + "0" * 64,
            "display_name": "Probe",
            "purpose": "unit-test probe",
            "scope": {
                "dataset_digest": "sha256:" + "1" * 64,
                "dataset_case_count": dataset_case_count,
                "expected_assertion_count": expected_assertion_count,
                "assertion_registry_digest": "sha256:" + "2" * 64,
                "prompt_digest": "sha256:" + "3" * 64,
                "provider": "mock",
                "model": "mock-model-v1",
                "provider_configuration_digest": "sha256:" + "4" * 64,
                "execution_mode": "in_memory",
                "storage_backend": "memory",
                "migration_revision": None,
                "metrics_schema_version": 1,
                "metrics_definition_digest": "sha256:" + "5" * 64,
            },
            "supported_comparison_contract": {
                "comparison_schema_version": 1,
                "comparison_definition_digest": "sha256:" + "6" * 64,
            },
            "rules": [
                {
                    "rule_id": "no_case_regressions",
                    "rule_type": "transition_count_equals",
                    "description": "probe",
                    "evidence_key": "case.regression_transition_count",
                    "operator": "EQ",
                    "expected": 0,
                }
            ],
        }
    )
