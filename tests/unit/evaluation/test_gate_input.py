"""门禁输入的严格加载、策略身份与适用性（阶段 7 · S6）。

对应任务书 §二十一 的 B、C、D 组，以及 H 组的策略损坏部分。

🔴 这一组的存在理由：**一份读错的门禁结论比没有结论更危险**——
它看起来像一次判断。所以两份输入都必须先证明自己是它们自称的东西。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ai_psi.evaluation.comparison import (
    ComparisonInputError,
    EvaluationComparison,
    compare_run_results,
    load_comparison,
    parse_comparison,
    write_comparison,
)
from ai_psi.evaluation.gate import (
    ApplicabilityReason,
    GateOutcome,
    GatePolicy,
    PolicyInputError,
    decide,
    evaluate_policy_applicability,
    load_gate_policy,
    policy_digest,
)
from ai_psi.evaluation.serialization import raw_payload

pytestmark = pytest.mark.unit


def _write_policy(payload: dict[str, Any], tmp_path: Path, name: str = "policy.json") -> Path:
    path = tmp_path / name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _comparison(gate_factory: Any, outcomes: dict[str, str] | None = None) -> EvaluationComparison:
    # 显式标注：``gate_factory`` 是 Any，不标注会让返回值悄悄变成 Any。
    built: EvaluationComparison = gate_factory.comparison(
        outcomes or {"case-001": "pass", "case-002": "pass"}
    )
    return built


def _scope_with(
    gate_factory: Any, comparison: EvaluationComparison, **changes: Any
) -> dict[str, Any]:
    """把策略的 scope 改掉一项，并**重算摘要**（否则测的会是摘要不符）。"""
    payload: dict[str, Any] = gate_factory.policy_payload(comparison)
    scope = dict(payload["scope"])
    scope.update(changes)
    payload["scope"] = scope
    payload["policy_digest"] = policy_digest(payload)
    return payload


class TestPolicyIdentity:
    """B 组：策略身份与摘要。"""

    def test_digest_is_recomputable(self, gate_factory: Any, tmp_path: Path) -> None:
        """摘要是**按内容算出来的**，不是人工填的常量。"""
        comparison = _comparison(gate_factory)
        payload = gate_factory.policy_payload(comparison)
        assert payload["policy_digest"] == policy_digest(payload)
        policy = load_gate_policy(_write_policy(payload, tmp_path))
        assert policy.policy_digest == payload["policy_digest"]
        assert policy.policy_schema_version == 1

    def test_digest_does_not_include_itself(self, gate_factory: Any) -> None:
        """🔴 摘要不得自包含——否则它无法被独立重算。"""
        comparison = _comparison(gate_factory)
        payload = gate_factory.policy_payload(comparison)
        without = {key: value for key, value in payload.items() if key != "policy_digest"}
        assert policy_digest(payload) == policy_digest(without)
        # 改掉摘要本身**不会**改变算出来的摘要（只改变它是否相符）。
        payload["policy_digest"] = "sha256:" + "0" * 64
        assert policy_digest(payload) == policy_digest(without)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda p: p["rules"][0].__setitem__("expected", 5),
            lambda p: p["rules"][0].__setitem__("description", "different wording"),
            lambda p: p["rules"].pop(),
            lambda p: p["scope"].__setitem__("dataset_case_count", 11),
            lambda p: p["policy_revision"] and p.__setitem__("policy_revision", 2),
            lambda p: p["supported_comparison_contract"].__setitem__(
                "comparison_schema_version", 2
            ),
        ],
    )
    def test_any_content_change_changes_the_digest(self, gate_factory: Any, mutate: Any) -> None:
        """改规则、改 scope、改版本、甚至改一句 description，摘要都必须变。"""
        comparison = _comparison(gate_factory)
        payload = gate_factory.policy_payload(comparison)
        before = policy_digest(payload)
        mutate(payload)
        assert policy_digest(payload) != before

    def test_stale_digest_is_rejected(self, gate_factory: Any, tmp_path: Path) -> None:
        """改了内容却沿用旧摘要 → 拒绝，且**明确说清**是摘要不符。"""
        comparison = _comparison(gate_factory)
        payload = gate_factory.policy_payload(comparison)
        payload["rules"][0]["expected"] = 7
        with pytest.raises(PolicyInputError, match="policy_digest 与策略内容不符"):
            load_gate_policy(_write_policy(payload, tmp_path))

    def test_float_threshold_is_rejected(self, gate_factory: Any, tmp_path: Path) -> None:
        """🔴 比率阈值必须是固定 6 位小数的**字符串**。"""
        comparison = _comparison(gate_factory)
        payload = gate_factory.policy_payload(comparison)
        payload["rules"][2]["expected"]["value"] = 1.0
        payload["policy_digest"] = policy_digest(payload)
        with pytest.raises(PolicyInputError):
            load_gate_policy(_write_policy(payload, tmp_path))

    def test_unknown_field_is_rejected(self, gate_factory: Any, tmp_path: Path) -> None:
        comparison = _comparison(gate_factory)
        payload = gate_factory.policy_payload(comparison)
        payload["rules"][0]["weight"] = 0.5
        payload["policy_digest"] = policy_digest(payload)
        with pytest.raises(PolicyInputError):
            load_gate_policy(_write_policy(payload, tmp_path))

    def test_missing_scope_field_is_rejected(self, gate_factory: Any, tmp_path: Path) -> None:
        comparison = _comparison(gate_factory)
        payload = gate_factory.policy_payload(comparison)
        del payload["scope"]["provider"]
        payload["policy_digest"] = policy_digest(payload)
        with pytest.raises(PolicyInputError):
            load_gate_policy(_write_policy(payload, tmp_path))

    def test_unknown_schema_version_is_rejected(self, gate_factory: Any, tmp_path: Path) -> None:
        comparison = _comparison(gate_factory)
        payload = gate_factory.policy_payload(comparison)
        payload["policy_schema_version"] = 99
        payload["policy_digest"] = policy_digest(payload)
        with pytest.raises(PolicyInputError, match="policy_schema_version"):
            load_gate_policy(_write_policy(payload, tmp_path))

    def test_duplicate_json_key_is_rejected(self, gate_factory: Any, tmp_path: Path) -> None:
        """同一对象里写两遍同一个键，默认 JSON 会**后者覆盖前者**。

        对一份要扮演契约的文件而言，那种宽容是有害的：一条被悄悄覆盖的
        规则看起来像是从来没写过。
        """
        comparison = _comparison(gate_factory)
        payload = gate_factory.policy_payload(comparison)
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        text = text.replace(
            '"policy_id": "test_gate",', '"policy_id": "a", "policy_id": "test_gate",', 1
        )
        path = tmp_path / "dup.json"
        path.write_text(text + "\n", encoding="utf-8", newline="\n")
        with pytest.raises(PolicyInputError, match="重复的键"):
            load_gate_policy(path)

    def test_nan_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "nan.json"
        path.write_text('{"policy_revision": NaN}', encoding="utf-8", newline="\n")
        with pytest.raises(PolicyInputError, match="NaN"):
            load_gate_policy(path)

    def test_broken_json_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.json"
        path.write_text('{"policy_id": ', encoding="utf-8", newline="\n")
        with pytest.raises(PolicyInputError, match="JSON 解析失败"):
            load_gate_policy(path)

    def test_missing_policy_file_reports_a_read_failure(self, tmp_path: Path) -> None:
        with pytest.raises(PolicyInputError, match="读取失败"):
            load_gate_policy(tmp_path / "nope.json")

    def test_policy_content_is_data_not_code(self, gate_factory: Any, tmp_path: Path) -> None:
        """策略里的内容**只是数据**：不 eval、不导入、不执行。"""
        comparison = _comparison(gate_factory)
        payload = gate_factory.policy_payload(comparison)
        marker = "__import__('os').system('echo pwned')"
        payload["purpose"] = marker
        payload["policy_digest"] = policy_digest(payload)
        policy = load_gate_policy(_write_policy(payload, tmp_path))
        assert policy.purpose == marker


class TestComparisonInput:
    """C 组：对比产物的严格加载。"""

    def test_valid_comparison_round_trips(self, gate_factory: Any, tmp_path: Path) -> None:
        comparison = _comparison(gate_factory)
        path = tmp_path / "comparison.json"
        write_comparison(comparison, path)
        assert load_comparison(path) == comparison

    def test_run_result_is_not_accepted_as_a_comparison(
        self, gate_factory: Any, comparison_factory: Any
    ) -> None:
        """🔴 拿一份**评测结果**当对比产物必须被拒绝。

        它们的顶层字段完全不同——接受它等于让门禁去评一份它看不懂的东西。
        """
        dataset = comparison_factory.dataset(["case-001"])
        result = comparison_factory.run(dataset, {"case-001": "pass"})
        with pytest.raises(ComparisonInputError, match="顶层字段"):
            parse_comparison(raw_payload(result))

    def test_unknown_top_level_field_is_rejected(self, gate_factory: Any) -> None:
        comparison = _comparison(gate_factory)
        payload = comparison.model_dump(mode="json")
        payload["release_allowed"] = True
        with pytest.raises(ComparisonInputError, match="顶层字段"):
            parse_comparison(payload)

    def test_missing_top_level_field_is_rejected(self, gate_factory: Any) -> None:
        comparison = _comparison(gate_factory)
        payload = comparison.model_dump(mode="json")
        del payload["integrity_notes"]
        with pytest.raises(ComparisonInputError, match="顶层字段"):
            parse_comparison(payload)

    def test_truncated_json_is_rejected(self, gate_factory: Any, tmp_path: Path) -> None:
        comparison = _comparison(gate_factory)
        path = tmp_path / "comparison.json"
        write_comparison(comparison, path)
        text = path.read_text(encoding="utf-8")
        path.write_text(text[: len(text) // 2], encoding="utf-8", newline="\n")
        with pytest.raises(ComparisonInputError, match="JSON 解析失败"):
            load_comparison(path)

    def test_non_utf8_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "comparison.json"
        path.write_bytes(b"\xff\xfe\x00\x00 not utf-8")
        with pytest.raises(ComparisonInputError, match="UTF-8"):
            load_comparison(path)

    @pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_numbers_are_rejected(self, tmp_path: Path, constant: str) -> None:
        path = tmp_path / f"{constant}.json"
        path.write_text(
            '{"comparison_schema_version": ' + constant + "}", encoding="utf-8", newline="\n"
        )
        with pytest.raises(ComparisonInputError, match=constant):
            load_comparison(path)

    def test_model_invariants_are_rechecked_on_load(
        self, gate_factory: Any, tmp_path: Path
    ) -> None:
        """🔴 模型的内部不变量在**加载时重跑一遍**。

        把 ``comparison_eligible`` 改成 False 却留着五组差异结构，
        是一份自相矛盾的产物——它必须读不进来。
        """
        comparison = _comparison(gate_factory)
        payload = comparison.model_dump(mode="json")
        payload["comparison_eligible"] = False
        payload["blockers"] = ["dataset_differs"]
        # 走**加载入口**（而不是 parse_comparison）：前者把 pydantic 的错误
        # 包装成门禁认识的输入错误，后者是给已经拿到 dict 的调用方用的。
        path = tmp_path / "inconsistent.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(ComparisonInputError, match="结构校验失败"):
            load_comparison(path)

    def test_comparison_content_is_data_not_code(self, gate_factory: Any) -> None:
        comparison = _comparison(gate_factory)
        payload = comparison.model_dump(mode="json")
        marker = "__import__('os').system('echo pwned')"
        payload["baseline"]["package_version"] = marker
        restored = parse_comparison(payload)
        assert restored.baseline.package_version == marker


class TestApplicability:
    """D 组：策略适用性。"""

    def test_matching_identity_is_applicable(self, gate_factory: Any, tmp_path: Path) -> None:
        comparison = _comparison(gate_factory)
        policy = load_gate_policy(_write_policy(gate_factory.policy_payload(comparison), tmp_path))
        applicability = evaluate_policy_applicability(comparison, policy)
        assert applicability.applicable is True
        assert applicability.reasons == ()

    def test_not_eligible_comparison_is_not_evaluated(self, gate_factory: Any) -> None:
        """🔴 不可比较的输入**没有合法的指标 delta**，因此谈不上"适用"。"""
        comparison = _comparison(gate_factory)
        broken = comparison.model_copy(
            update={
                "comparison_eligible": False,
                "blockers": ("dataset_differs",),
                "metrics_comparison": None,
                "case_transitions": None,
                "assertion_transitions": None,
                "distribution_deltas": None,
                "failure_index_delta": None,
                "case_transition_summary": None,
                "assertion_transition_summary": None,
            }
        )
        policy = gate_factory.policy(comparison)
        decision = decide(broken, policy)
        assert decision.policy_applicable is False
        assert decision.outcome is GateOutcome.NOT_EVALUATED
        assert "comparison_not_eligible" in decision.applicability_reasons
        # 一条质量规则都不许评估。
        assert decision.rule_results == ()

    def test_partial_run_is_not_evaluated(self, gate_factory: Any, comparison_factory: Any) -> None:
        """🔴 S5 的 ``partial_run`` 传到这里就是"不可比较" ⇒ 不适用 ⇒ NOT_EVALUATED。

        S6 **不**重新判断部分运行；它照单接收 S5 的 ``comparison_eligible``。
        """
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        full = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        partial = comparison_factory.revise(full, dataset, cases=full.cases[:1])
        comparison = compare_run_results(partial, partial)
        # S5 那一侧：确实被 partial_run 挡住了。
        assert comparison.comparison_eligible is False
        assert "partial_run" in comparison.blockers

        # 用一份**匹配的**策略（否则测的会是 scope 不匹配）。
        matching = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        policy = gate_factory.policy(compare_run_results(matching, matching))
        decision = decide(comparison, policy)
        assert decision.policy_applicable is False
        assert decision.outcome is GateOutcome.NOT_EVALUATED
        assert "comparison_not_eligible" in decision.applicability_reasons
        assert decision.rule_results == ()

    @pytest.mark.parametrize(
        ("field", "value", "reason"),
        [
            ("dataset_digest", "sha256:" + "a" * 64, "dataset_out_of_scope"),
            ("dataset_case_count", 99, "dataset_case_count_out_of_scope"),
            ("assertion_registry_digest", "sha256:" + "b" * 64, "assertion_registry_out_of_scope"),
            ("prompt_digest", "sha256:" + "c" * 64, "prompt_out_of_scope"),
            ("provider", "openai", "provider_out_of_scope"),
            ("model", "gpt-4", "model_out_of_scope"),
            (
                "provider_configuration_digest",
                "sha256:" + "d" * 64,
                "provider_configuration_out_of_scope",
            ),
            ("execution_mode", "postgres_http", "execution_mode_out_of_scope"),
            ("storage_backend", "postgresql", "storage_backend_out_of_scope"),
            ("migration_revision", "b7f1c9d4e2a3", "migration_revision_out_of_scope"),
            ("metrics_schema_version", 2, "metrics_contract_out_of_scope"),
            ("metrics_definition_digest", "sha256:" + "e" * 64, "metrics_contract_out_of_scope"),
        ],
    )
    def test_each_scope_field_mismatch_is_not_evaluated(
        self, gate_factory: Any, field: str, value: Any, reason: str
    ) -> None:
        comparison = _comparison(gate_factory)
        payload = _scope_with(gate_factory, comparison, **{field: value})
        decision = decide(comparison, GatePolicy.model_validate(payload))
        assert decision.policy_applicable is False
        assert decision.outcome is GateOutcome.NOT_EVALUATED
        assert reason in decision.applicability_reasons

    def test_comparison_contract_mismatch_is_not_evaluated(self, gate_factory: Any) -> None:
        """契约版本对不上时结论是**不适用**，不是 ``FAIL``。

        契约换了说明这份策略的规则可能已经不对口，而不是 Candidate 变差了。
        """
        comparison = _comparison(gate_factory)
        payload = gate_factory.policy_payload(comparison)
        payload["supported_comparison_contract"]["comparison_definition_digest"] = (
            "sha256:" + "f" * 64
        )
        payload["policy_digest"] = policy_digest(payload)
        decision = decide(comparison, GatePolicy.model_validate(payload))
        assert decision.policy_applicable is False
        assert decision.outcome is GateOutcome.NOT_EVALUATED
        assert "comparison_contract_unsupported" in decision.applicability_reasons

    def test_multiple_reasons_are_stably_sorted(self, gate_factory: Any) -> None:
        comparison = _comparison(gate_factory)
        payload = _scope_with(
            gate_factory,
            comparison,
            dataset_digest="sha256:" + "a" * 64,
            provider="openai",
            model="gpt-4",
        )
        applicability = evaluate_policy_applicability(
            comparison, GatePolicy.model_validate(payload)
        )
        assert list(applicability.reasons) == sorted(applicability.reasons)
        assert len(applicability.reasons) >= 3
        assert list(applicability.reasons) == sorted(set(applicability.reasons))

    def test_inapplicable_policy_never_evaluates_quality_rules(self, gate_factory: Any) -> None:
        """🔴 不适用时**一条质量规则都不跑**——跑了就会产出一份
        "看起来评估过了"的结论，而它回答的不是这份策略有权回答的问题。"""
        comparison = _comparison(gate_factory)
        payload = _scope_with(gate_factory, comparison, provider="openai")
        decision = decide(comparison, GatePolicy.model_validate(payload))
        assert decision.rule_results == ()
        assert decision.failed_rule_ids == ()
        assert decision.not_evaluated_rule_ids == ()

    def test_scope_mismatch_is_never_reported_as_fail(self, gate_factory: Any) -> None:
        """🔴 不适用**不得**判成 ``FAIL``——那是拿规则不适用去指控候选。"""
        comparison = _comparison(gate_factory)
        for field, value in (
            ("dataset_digest", "sha256:" + "a" * 64),
            ("provider", "openai"),
            ("execution_mode", "postgres_http"),
        ):
            payload = _scope_with(gate_factory, comparison, **{field: value})
            decision = decide(comparison, GatePolicy.model_validate(payload))
            assert decision.outcome is GateOutcome.NOT_EVALUATED, field

    def test_applicability_reasons_are_a_closed_set(self) -> None:
        """原因码是闭合集合；每一条都必须在登记表里。"""
        listed = {member.value for member in ApplicabilityReason}
        assert listed == set(ApplicabilityReason)
        assert "comparison_not_eligible" in listed
        assert "rule_evidence_unavailable" not in listed  # 见枚举文档：当前契约下不可达


def test_comparison_never_reaches_the_gate_in_a_mutated_form(
    gate_factory: Any, tmp_path: Path
) -> None:
    """🔴 S6 **不修改**对比产物：读进来什么样，判的就是什么样。"""
    comparison = _comparison(gate_factory)
    path = tmp_path / "c.json"
    write_comparison(comparison, path)
    before = path.read_bytes()
    loaded = load_comparison(path)
    decide(loaded, gate_factory.policy(loaded))
    assert path.read_bytes() == before
