"""策略适用性与 S7 重新验证（阶段 8 · S8）。

对应任务书 §十七 的 B、D 组，以及 H 组的安全部分。

🔴 这一组的存在理由：**S8 的权威只能来自它自己刚跑出来的那次验证。**
任何"读一份现成的报告"的路径都必须不存在——不是"不推荐"，是**没有这个入口**。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import ai_psi.evaluation.qualification as qualification_module
from ai_psi.evaluation.comparison import compare_run_results
from ai_psi.evaluation.evidence import verify_evidence_bundle as real_verify_evidence_bundle
from ai_psi.evaluation.qualification import (
    QualificationApplicabilityStatus,
    QualificationInputs,
    QualificationPolicyApplicabilityReason,
    build_qualification_decision,
    evaluate_policy_applicability,
    load_qualification_policy,
)

pytestmark = pytest.mark.unit

#: 合成运行结果里的回答正文。它**不得**出现在任何 S8 产物里。
_RESPONSE_SENTINEL = "合成的回答文本"


@pytest.fixture
def chain(qualification_chain_factory: Any, tmp_path: Path) -> Any:
    return qualification_chain_factory.chain(tmp_path / "s8")


def _reasons(decision: Any) -> set[str]:
    return {reason.value for reason in decision.policy_applicability.reasons}


# ---------------------------------------------------------------------------
# B 组：策略适用性
# ---------------------------------------------------------------------------


class TestPolicyApplicability:
    """B 组：逐项作用域对照。"""

    def test_the_synthetic_chain_is_applicable(self, chain: Any) -> None:
        decision = build_qualification_decision(chain.inputs())
        assert decision.policy_applicability.status is QualificationApplicabilityStatus.APPLICABLE
        assert decision.policy_applicability.reasons == ()

    @pytest.mark.parametrize(
        ("field", "value", "reason"),
        [
            ("evidence_bundle_schema_version", 2, "evidence_bundle_schema_out_of_scope"),
            (
                "evidence_bundle_definition_digest",
                "sha256:" + "a" * 64,
                "evidence_bundle_definition_out_of_scope",
            ),
            (
                "verification_definition_digest",
                "sha256:" + "b" * 64,
                "verification_definition_out_of_scope",
            ),
            ("comparison_schema_version", 2, "comparison_schema_out_of_scope"),
            (
                "comparison_definition_digest",
                "sha256:" + "c" * 64,
                "comparison_definition_out_of_scope",
            ),
            ("gate_decision_schema_version", 2, "gate_schema_out_of_scope"),
            ("gate_definition_digest", "sha256:" + "d" * 64, "gate_definition_out_of_scope"),
            ("gate_policy_id", "another_gate", "gate_policy_out_of_scope"),
            ("gate_policy_revision", 9, "gate_policy_out_of_scope"),
            ("gate_policy_digest", "sha256:" + "e" * 64, "gate_policy_out_of_scope"),
            ("dataset_digest", "sha256:" + "f" * 64, "dataset_out_of_scope"),
            ("assertion_registry_digest", "sha256:" + "1" * 64, "assertion_registry_out_of_scope"),
            ("provider", "openai", "provider_out_of_scope"),
            ("model", "gpt-4", "model_out_of_scope"),
            ("execution_mode", "postgres_http", "execution_mode_out_of_scope"),
            ("storage_backend", "postgresql", "storage_backend_out_of_scope"),
        ],
    )
    def test_each_scope_mismatch_makes_it_inapplicable(
        self,
        qualification_chain_factory: Any,
        tmp_path: Path,
        field: str,
        value: object,
        reason: str,
    ) -> None:
        chain = qualification_chain_factory.chain(
            tmp_path / f"scope-{field}", qualification_scope={field: value}
        )
        decision = build_qualification_decision(chain.inputs())
        assert (
            decision.policy_applicability.status is QualificationApplicabilityStatus.NOT_APPLICABLE
        )
        assert reason in _reasons(decision), (field, _reasons(decision))
        assert decision.policy_applicability.reasons

    def test_scope_mismatch_is_never_reported_as_disqualified(
        self, qualification_chain_factory: Any, tmp_path: Path
    ) -> None:
        """🔴 不适用**不得**判成 ``DISQUALIFIED``——那是拿"管不着"去指控候选。"""
        chain = qualification_chain_factory.chain(
            tmp_path / "na", qualification_scope={"provider": "openai"}
        )
        decision = build_qualification_decision(chain.inputs())
        assert decision.qualification_outcome.value == "NOT_EVALUATED"
        assert decision.qualification_outcome.value != "DISQUALIFIED"
        for item in decision.checks:
            assert item.reason_code.value != "gate_outcome_not_eligible"

    def test_a_partial_run_is_inapplicable(self, comparison_factory: Any, tmp_path: Path) -> None:
        """🔴 部分运行 ⇒ 策略不适用（不是"不合格"）。

        ⚠️ 这里直接调**适用性纯函数**：造一条"对比里带 partial_run 阻塞"的链
        要重建对比、门禁结论与证据包三样，而那三样都有各自的专项测试。这条
        只问"看到 partial_run 时适用性怎么判"。
        """
        runs = comparison_factory
        dataset = runs.dataset(["case-001", "case-002"])
        full = runs.run(dataset, {"case-001": "pass", "case-002": "pass"})
        partial = runs.revise(full, dataset, cases=full.cases[:1])
        comparison = compare_run_results(partial, partial)
        assert "partial_run" in comparison.blockers

        policy = _synthetic_policy_for(comparison, tmp_path)
        applicability = evaluate_policy_applicability(
            policy,
            evidence_bundle_schema_version=policy.scope.evidence_bundle_schema_version,
            evidence_bundle_definition_digest=policy.scope.evidence_bundle_definition_digest,
            verification_definition_digest=policy.scope.verification_definition_digest,
            gate_decision=_stub_gate_decision(policy),
            comparison=comparison,
        )
        assert applicability.status is QualificationApplicabilityStatus.NOT_APPLICABLE
        assert QualificationPolicyApplicabilityReason.PARTIAL_RUN in applicability.reasons

    def test_missing_identities_are_not_applicable_but_unassessable(
        self, chain: Any, tmp_path: Path
    ) -> None:
        """🔴 身份读不出来时，结论是 ``NOT_EVALUATED`` 而不是 ``NOT_APPLICABLE``。

        "这次判不了它管不管得着"与"它不归我管"是两件事：后者是一句无法支撑的
        断言，前者才是实情。
        """
        policy = load_qualification_policy(chain.qualification_policy)
        applicability = evaluate_policy_applicability(
            policy,
            evidence_bundle_schema_version=None,
            evidence_bundle_definition_digest=None,
            verification_definition_digest=None,
            gate_decision=None,
            comparison=None,
        )
        assert applicability.status is QualificationApplicabilityStatus.NOT_EVALUATED
        assert applicability.reasons == (
            QualificationPolicyApplicabilityReason.ASSESSMENT_NOT_REACHED,
        )

    def test_invalid_evidence_never_yields_applicable(
        self, qualification_chain_factory: Any, tmp_path: Path
    ) -> None:
        """🔴 证据本身 INVALID 时，适用性**不得**从不可信证据里推出来。"""
        chain = qualification_chain_factory.chain(tmp_path / "inv")
        chain.artifacts.baseline_run.write_bytes(chain.artifacts.baseline_run.read_bytes() + b" ")
        decision = build_qualification_decision(chain.inputs())
        assert (
            decision.policy_applicability.status is QualificationApplicabilityStatus.NOT_EVALUATED
        )
        assert decision.policy_applicability.reasons == (
            QualificationPolicyApplicabilityReason.ASSESSMENT_NOT_REACHED,
        )

    def test_applicability_reasons_are_a_closed_set(self) -> None:
        listed = {member.value for member in QualificationPolicyApplicabilityReason}
        assert listed == set(QualificationPolicyApplicabilityReason)
        assert "partial_run" in listed
        assert "assessment_not_reached" in listed


def _synthetic_policy_for(comparison: Any, tmp_path: Path) -> Any:
    """造一份作用域与给定对比匹配的资格策略（只有适用性测试用得到）。"""
    from ai_psi.evaluation.qualification import (
        parse_qualification_policy,
        qualification_policy_digest,
    )

    side = comparison.baseline
    payload: dict[str, Any] = {
        "qualification_policy_schema_version": 1,
        "qualification_policy_id": "synthetic",
        "qualification_policy_revision": 1,
        "display_name": "Synthetic",
        "purpose": "applicability-only fixture",
        "scope": {
            "evidence_bundle_schema_version": 1,
            "evidence_bundle_definition_digest": "sha256:" + "0" * 64,
            "verification_definition_digest": "sha256:" + "0" * 64,
            "comparison_schema_version": comparison.comparison_schema_version,
            "comparison_definition_digest": comparison.comparison_definition_digest,
            "gate_decision_schema_version": 1,
            "gate_definition_digest": "sha256:" + "0" * 64,
            "gate_policy_schema_version": 1,
            "gate_policy_id": "synthetic_gate",
            "gate_policy_revision": 1,
            "gate_policy_digest": "sha256:" + "0" * 64,
            "dataset_digest": side.dataset_digest,
            "assertion_registry_digest": side.assertion_registry_digest,
            "provider": side.provider_name,
            "model": side.model_id,
            "execution_mode": side.manifest_execution_mode,
            "storage_backend": side.storage_backend,
        },
        "allowed_outcomes": {
            "verification_outcomes": ["VERIFIED"],
            "gate_outcomes": ["PASS"],
        },
    }
    payload["qualification_policy_digest"] = qualification_policy_digest(payload)
    del tmp_path
    return parse_qualification_policy(payload)


def _stub_gate_decision(policy: Any) -> Any:
    """一份只为"身份对得上"而存在的门禁结论替身。

    ⚠️ 它**只**用于适用性纯函数：那条路径只读 ``identity`` 与 ``outcome``，
    不读规则明细。真正的聚合测试一律用正式的 ``decide`` 产物。
    """
    from ai_psi.evaluation.gate import GateDecision, GateDecisionIdentity, GateOutcome

    scope = policy.scope
    return GateDecision(
        gate_definition_digest=scope.gate_definition_digest,
        identity=GateDecisionIdentity(
            policy_schema_version=scope.gate_policy_schema_version,
            policy_id=scope.gate_policy_id,
            policy_revision=scope.gate_policy_revision,
            policy_digest=scope.gate_policy_digest,
            comparison_schema_version=scope.comparison_schema_version,
            comparison_definition_digest=scope.comparison_definition_digest,
            comparison_fingerprint="sha256:" + "0" * 64,
            baseline_commit_sha=None,
            candidate_commit_sha=None,
        ),
        policy_applicable=False,
        applicability_reasons=("comparison_not_eligible",),
        outcome=GateOutcome.NOT_EVALUATED,
        rule_results=(),
        failed_rule_ids=(),
        not_evaluated_rule_ids=(),
    )


# ---------------------------------------------------------------------------
# D 组：S7 重新验证
# ---------------------------------------------------------------------------


class TestReverification:
    """D 组：S8 自己重新验证，且只用自己的结果。"""

    def test_it_calls_the_official_s7_verifier_exactly_once(
        self, chain: Any, monkeypatch: Any
    ) -> None:
        """🔴 调用次数准确：**一次**，且是正式入口。"""
        calls: list[str] = []
        real = real_verify_evidence_bundle

        def counting(*args: Any, **kwargs: Any) -> Any:
            calls.append("verified")
            return real(*args, **kwargs)

        monkeypatch.setattr(qualification_module, "verify_evidence_bundle", counting)
        decision = build_qualification_decision(chain.inputs())
        assert len(calls) == 1
        assert decision.verification_outcome.value == "VERIFIED"

    def test_a_tampered_s7_verdict_cannot_yield_qualified(
        self, chain: Any, monkeypatch: Any
    ) -> None:
        """🔴 就算有人**把 S7 的结论改掉**，改的也是本次调用那一个对象——
        结论照它聚合，而它仍然逃不过模型的不变量。"""
        real = real_verify_evidence_bundle

        from ai_psi.evaluation.evidence import VerificationOutcome

        def flipping(*args: Any, **kwargs: Any) -> Any:
            report = real(*args, **kwargs)
            # ⚠️ 必须传**枚举**：``model_copy`` 按设计不重跑校验器，传字符串
            # 会造出一个校验器永远不会产出的对象，那测的就不是真实路径了。
            return report.model_copy(
                update={"verification_outcome": VerificationOutcome.NOT_VERIFIABLE}
            )

        monkeypatch.setattr(qualification_module, "verify_evidence_bundle", flipping)
        decision = build_qualification_decision(chain.inputs())
        assert decision.verification_outcome.value == "NOT_VERIFIABLE"
        assert decision.qualification_outcome.value == "NOT_EVALUATED"

    def test_the_inputs_dataclass_has_no_verification_report_slot(self) -> None:
        """🔴 **没有这个入口**——不是"不推荐传"，是传不进来。"""
        import dataclasses

        names = {field.name for field in dataclasses.fields(QualificationInputs)}
        assert names == {
            "bundle",
            "baseline_run",
            "candidate_run",
            "comparison",
            "gate_policy",
            "gate_decision",
            "qualification_policy",
        }
        assert not any("report" in name for name in names)
        assert not any("verification" in name for name in names)

    def test_the_module_reaches_no_network_provider_or_database(self) -> None:
        """🔴 源码级断言：任何一次"顺手加个 fetch"都会让它变红。"""
        for module in (qualification_module,):
            source = Path(str(module.__file__)).read_text(encoding="utf-8")
            for banned in (
                "httpx",
                "psycopg",
                "sqlalchemy",
                "socket",
                "urllib",
                "requests",
                "subprocess",
                "ai_psi.container",
                "ai_psi.providers",
                "ai_psi.infrastructure",
            ):
                assert banned not in source, banned

    def test_invalid_evidence_is_disqualified_not_an_input_error(self, chain: Any) -> None:
        """🔴 INVALID 是**业务结论**，不是"输入解析失败"——它必须产生结论。"""
        chain.artifacts.candidate_run.write_bytes(chain.artifacts.candidate_run.read_bytes() + b" ")
        decision = build_qualification_decision(chain.inputs())
        assert decision.verification_outcome.value == "INVALID"
        assert decision.qualification_outcome.value == "DISQUALIFIED"

    def test_a_broken_qualification_policy_is_a_root_input_error(self, chain: Any) -> None:
        """策略读不出来 ⇒ 根输入错误（没有策略就没有可聚合的规则）。"""
        from ai_psi.evaluation.qualification import QualificationInputError

        chain.qualification_policy.write_text("{ not json", encoding="utf-8", newline="\n")
        with pytest.raises(QualificationInputError, match="JSON 解析失败"):
            build_qualification_decision(chain.inputs())

    def test_a_bundle_without_a_safe_envelope_is_a_root_input_error(self, chain: Any) -> None:
        """🔴 连安全 Envelope 都形不成 ⇒ 根输入错误，**不伪造结论**。"""
        from ai_psi.evaluation.qualification import QualificationInputError

        chain.bundle.write_text("{ not json", encoding="utf-8", newline="\n")
        with pytest.raises(QualificationInputError, match="证据包根输入无法解析"):
            build_qualification_decision(chain.inputs())

    def test_a_missing_bundle_file_is_a_root_input_error(self, chain: Any) -> None:
        from ai_psi.evaluation.qualification import QualificationInputError

        chain.bundle.unlink()
        with pytest.raises(QualificationInputError, match="证据包根输入无法解析"):
            build_qualification_decision(chain.inputs())

    def test_a_structurally_broken_bundle_is_not_a_root_input_error(self, chain: Any) -> None:
        """读得出来、但不是严格 Bundle ⇒ **不是**根输入错误，交给 S7 判 INVALID。"""
        import json

        payload = json.loads(chain.bundle.read_text(encoding="utf-8"))
        payload["artifacts"].pop(2)
        chain.bundle.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        decision = build_qualification_decision(chain.inputs())
        assert decision.verification_outcome.value == "INVALID"
        assert decision.qualification_outcome.value == "DISQUALIFIED"


# ---------------------------------------------------------------------------
# H 组（部分）：安全
# ---------------------------------------------------------------------------


class TestSafety:
    """H 组：产物边界。"""

    def test_the_decision_carries_no_secret_prose_or_path(self, chain: Any, tmp_path: Path) -> None:
        from ai_psi.evaluation.qualification import write_qualification_decision

        decision = build_qualification_decision(chain.inputs())
        path = tmp_path / "d.json"
        write_qualification_decision(decision, path)
        text = path.read_text(encoding="utf-8")
        for sentinel in (
            _RESPONSE_SENTINEL,
            "response_text",
            "failure_detail",
            "Traceback",
            "postgresql://",
            "postgresql+psycopg://",
            "api_key",
            "Authorization",
            "password",
            str(tmp_path),
            "release_allowed",
            "deploy_allowed",
            "merge_allowed",
            "production_ready",
            "recommendation",
        ):
            assert sentinel not in text, sentinel

    def test_unknown_bundle_fields_are_rejected_not_copied(self, chain: Any) -> None:
        """🔴 证据包多了个未知字段 ⇒ 连安全 Envelope 都形不成 ⇒ 根输入错误。

        它既不会被抄进结论，也不会被"顺手忽略"——两种都不是。这与
        ``evidence_cli verify`` 对同一份文件的处置一致（那边也是退出码 3）。
        """
        import json

        from ai_psi.evaluation.qualification import QualificationInputError

        payload = json.loads(chain.bundle.read_text(encoding="utf-8"))
        payload["a_marker_field"] = "不该出现在任何产物里"
        chain.bundle.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(QualificationInputError, match="证据包根输入无法解析"):
            build_qualification_decision(chain.inputs())

    def test_input_content_is_data_not_code(self, chain: Any, tmp_path: Path) -> None:
        """🔴 输入里的内容**只是数据**：不 ``eval``、不导入、不执行。"""
        import json

        trace = tmp_path / "executed.marker"
        marker = f"__import__('pathlib').Path({str(trace)!r}).write_text('pwned')"
        payload = json.loads(chain.qualification_policy.read_text(encoding="utf-8"))
        payload["purpose"] = marker
        from ai_psi.evaluation.qualification import qualification_policy_digest

        payload["qualification_policy_digest"] = qualification_policy_digest(payload)
        chain.qualification_policy.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        build_qualification_decision(chain.inputs())
        assert not trace.exists()
