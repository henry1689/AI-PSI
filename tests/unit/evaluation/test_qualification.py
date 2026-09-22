"""资格策略身份、聚合矩阵与结论模型（阶段 8 · S8）。

对应任务书 §十七 的 A、C、E、G 组。

🔴 这一组的存在理由：**资格层最容易变成那个最糟的工具**——一个"因为证据
坏了所以候选不合格"的层，或者一个"策略管不着却给了合格"的层。这里钉住的
就是这两条边界。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ai_psi.evaluation.evidence import EVIDENCE_BUNDLE_SCHEMA_VERSION
from ai_psi.evaluation.gate import load_gate_policy
from ai_psi.evaluation.qualification import (
    QUALIFICATION_CHECKS,
    QUALIFICATION_DECISION_SCHEMA_VERSION,
    QUALIFICATION_DECISION_TOP_LEVEL_FIELDS,
    QUALIFICATION_DEFINITION,
    QUALIFICATION_POLICY_DEFINITION,
    QUALIFICATION_POLICY_SCHEMA_VERSION,
    EvaluationQualificationDecision,
    EvaluationQualificationOutcome,
    EvaluationQualificationPolicy,
    QualificationApplicabilityStatus,
    QualificationCheckOutcome,
    QualificationInputError,
    build_qualification_decision,
    load_qualification_decision,
    load_qualification_policy,
    parse_qualification_decision,
    qualification_decision_digest,
    qualification_decision_payload,
    qualification_definition_digest,
    qualification_policy_definition_digest,
    qualification_policy_digest,
    write_qualification_decision,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REAL_POLICY = _REPO_ROOT / "evals" / "policies" / "s8_mock_golden_qualification_v1.json"
_REAL_GATE_POLICY = _REPO_ROOT / "evals" / "policies" / "s6_mock_golden_v1.json"

#: 合成运行结果里的回答正文。它**不得**出现在任何 S8 产物里。
_RESPONSE_SENTINEL = "合成的回答文本"


@pytest.fixture
def chain(qualification_chain_factory: Any, tmp_path: Path) -> Any:
    """一条落在磁盘上的、自洽的七文件资格链。"""
    return qualification_chain_factory.chain(tmp_path / "s8")


@pytest.fixture
def decision(chain: Any) -> EvaluationQualificationDecision:
    return build_qualification_decision(chain.inputs())


def _read(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def _reseal(payload: dict[str, Any]) -> dict[str, Any]:
    """重算结论摘要——好让**别处**的那处改动浮上来。"""
    payload["qualification_decision_digest"] = qualification_decision_digest(payload)
    return payload


def _mutate_artifact(path: Path, mutate: Callable[[str], str]) -> None:
    path.write_text(mutate(path.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# A 组：策略身份
# ---------------------------------------------------------------------------


class TestPolicyIdentity:
    """A 组：Schema、定义摘要、策略摘要。"""

    def test_schema_versions_are_one(self) -> None:
        assert QUALIFICATION_POLICY_SCHEMA_VERSION == 1
        assert QUALIFICATION_DECISION_SCHEMA_VERSION == 1

    def test_policy_definition_digest_is_stable(self) -> None:
        assert qualification_policy_definition_digest() == qualification_policy_definition_digest()
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", qualification_policy_definition_digest())

    def test_qualification_definition_digest_is_stable(self) -> None:
        assert qualification_definition_digest() == qualification_definition_digest()
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", qualification_definition_digest())

    def test_policy_definition_records_applicability_rules(self) -> None:
        text = json.dumps(QUALIFICATION_POLICY_DEFINITION, ensure_ascii=False)
        assert "NOT_APPLICABLE" in text
        assert "no_scope_relaxation" in text
        assert "scope_identity_binding" in text

    def test_qualification_definition_records_the_check_order(self) -> None:
        """🔴 检查项顺序必须进摘要：改顺序而摘要不变，等于宣称两份结论是
        按同一套规则得出的。"""
        assert QUALIFICATION_DEFINITION["check_order"] == list(QUALIFICATION_CHECKS)
        assert len(QUALIFICATION_CHECKS) == 13

    def test_qualification_definition_records_the_aggregation(self) -> None:
        aggregation = QUALIFICATION_DEFINITION["aggregation"]
        assert isinstance(aggregation, dict)
        assert aggregation["all_checks_pass"] == "QUALIFIED"
        assert aggregation["any_check_fail"] == "DISQUALIFIED"

    def test_qualification_definition_records_the_reverification_rule(self) -> None:
        text = json.dumps(QUALIFICATION_DEFINITION, ensure_ascii=False)
        assert "verify_evidence_bundle" in text
        assert "--verification-report" in text

    def test_definition_materials_carry_no_volatile_content(self) -> None:
        for material in (QUALIFICATION_POLICY_DEFINITION, QUALIFICATION_DEFINITION):
            text = json.dumps(material, ensure_ascii=False)
            assert str(_REPO_ROOT) not in text
            assert not re.search(r"[A-Za-z]:\\\\", text)
            assert "inspect.getsource" not in text

    def test_policy_digest_is_recomputable(self, chain: Any) -> None:
        payload = _read(chain.qualification_policy)
        assert payload["qualification_policy_digest"] == qualification_policy_digest(payload)
        policy = load_qualification_policy(chain.qualification_policy)
        assert policy.qualification_policy_digest == payload["qualification_policy_digest"]

    def test_policy_digest_does_not_include_itself(self, chain: Any) -> None:
        """🔴 自排除——含自己的摘要无法被独立重算。"""
        payload = _read(chain.qualification_policy)
        without = {k: v for k, v in payload.items() if k != "qualification_policy_digest"}
        assert qualification_policy_digest(payload) == qualification_policy_digest(without)
        payload["qualification_policy_digest"] = "sha256:" + "0" * 64
        assert qualification_policy_digest(payload) == qualification_policy_digest(without)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda p: p["scope"].__setitem__("provider", "另一个 provider"),
            lambda p: p["scope"].__setitem__("model", "another-model"),
            lambda p: p["scope"].__setitem__("dataset_digest", "sha256:" + "a" * 64),
            lambda p: p["scope"].__setitem__("gate_policy_revision", 2),
            lambda p: p["scope"].__setitem__("evidence_bundle_schema_version", 2),
            lambda p: p["scope"].__setitem__("storage_backend", "postgresql"),
        ],
    )
    def test_changing_the_scope_changes_the_digest(self, chain: Any, mutate: Any) -> None:
        payload = _read(chain.qualification_policy)
        before = qualification_policy_digest(payload)
        mutate(payload)
        assert qualification_policy_digest(payload) != before

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda p: p["allowed_outcomes"].__setitem__("verification_outcomes", ["INVALID"]),
            lambda p: p["allowed_outcomes"].__setitem__("gate_outcomes", ["PASS", "FAIL"]),
            lambda p: p["allowed_outcomes"].__setitem__("gate_outcomes", []),
        ],
    )
    def test_changing_allowed_outcomes_changes_the_digest(self, chain: Any, mutate: Any) -> None:
        payload = _read(chain.qualification_policy)
        before = qualification_policy_digest(payload)
        mutate(payload)
        assert qualification_policy_digest(payload) != before

    def test_wrong_policy_digest_is_rejected(self, chain: Any) -> None:
        payload = _read(chain.qualification_policy)
        payload["qualification_policy_digest"] = "sha256:" + "1" * 64
        chain.qualification_policy.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(
            QualificationInputError, match="qualification_policy_digest 与策略内容不符"
        ):
            load_qualification_policy(chain.qualification_policy)

    def test_unknown_policy_field_is_rejected(self, chain: Any) -> None:
        payload = _read(chain.qualification_policy)
        payload["release_allowed"] = True
        payload["qualification_policy_digest"] = qualification_policy_digest(payload)
        chain.qualification_policy.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(QualificationInputError, match="顶层字段"):
            load_qualification_policy(chain.qualification_policy)

    def test_the_policy_model_forbids_a_release_field(self) -> None:
        """🔴 发布许可不是"暂时没填"，是**写进去就校验失败**。"""
        for banned in (
            "release_allowed",
            "deploy_allowed",
            "merge_allowed",
            "production_ready",
            "auto_merge",
            "auto_deploy",
            "approved_for_release",
            "release_recommendation",
            "quality_score",
            "risk_score",
            "trust_score",
        ):
            with pytest.raises(ValidationError):
                EvaluationQualificationPolicy.model_validate(
                    {
                        "qualification_policy_schema_version": 1,
                        "qualification_policy_id": "x",
                        "qualification_policy_revision": 1,
                        "qualification_policy_digest": "sha256:" + "0" * 64,
                        "display_name": "x",
                        "purpose": "x",
                        "scope": {},
                        "allowed_outcomes": {
                            "verification_outcomes": ["VERIFIED"],
                            "gate_outcomes": ["PASS"],
                        },
                        banned: True,
                    }
                )


class TestTheRealPolicy:
    """首个正式策略（**从仓库里读**，不是就地构造的副本）。"""

    def test_it_loads(self) -> None:
        policy = load_qualification_policy(_REAL_POLICY)
        assert policy.qualification_policy_id == "s8_mock_golden_qualification"
        assert policy.qualification_policy_revision == 1
        assert policy.qualification_policy_schema_version == 1

    def test_it_accepts_only_verified_evidence_and_a_passing_gate(self) -> None:
        policy = load_qualification_policy(_REAL_POLICY)
        assert [item.value for item in policy.allowed_outcomes.verification_outcomes] == [
            "VERIFIED"
        ]
        assert [item.value for item in policy.allowed_outcomes.gate_outcomes] == ["PASS"]

    def test_it_binds_the_sealed_gate_policy_identity(self) -> None:
        """🔴 策略**逐字**绑定已封存的 S6 ``s6_mock_golden`` revision 1。"""
        policy = load_qualification_policy(_REAL_POLICY)
        gate_policy = load_gate_policy(_REAL_GATE_POLICY)
        assert policy.scope.gate_policy_schema_version == gate_policy.policy_schema_version
        assert policy.scope.gate_policy_id == gate_policy.policy_id
        assert policy.scope.gate_policy_revision == gate_policy.policy_revision
        assert policy.scope.gate_policy_digest == gate_policy.policy_digest

    def test_it_binds_the_current_contracts(
        self,
    ) -> None:
        from ai_psi.evaluation.comparison import comparison_definition_digest
        from ai_psi.evaluation.evidence import (
            evidence_definition_digest,
            evidence_verification_definition_digest,
        )
        from ai_psi.evaluation.gate import gate_definition_digest

        policy = load_qualification_policy(_REAL_POLICY)
        assert policy.scope.evidence_bundle_schema_version == EVIDENCE_BUNDLE_SCHEMA_VERSION
        assert policy.scope.evidence_bundle_definition_digest == evidence_definition_digest()
        assert (
            policy.scope.verification_definition_digest == evidence_verification_definition_digest()
        )
        assert policy.scope.comparison_definition_digest == comparison_definition_digest()
        assert policy.scope.gate_definition_digest == gate_definition_digest()

    def test_it_does_not_accept_a_real_provider(self) -> None:
        """首个策略只认 Mock Golden 的 Provider，不接受真实 Provider。"""
        policy = load_qualification_policy(_REAL_POLICY)
        assert policy.scope.provider == "mock"
        assert policy.scope.execution_mode == "in_memory"
        assert policy.scope.storage_backend == "memory"


# ---------------------------------------------------------------------------
# C 组：聚合矩阵
# ---------------------------------------------------------------------------


class TestAggregationMatrix:
    """C 组：九行矩阵，**逐行**测。"""

    def test_verified_applicable_pass_is_qualified(self, decision: Any) -> None:
        assert decision.verification_outcome.value == "VERIFIED"
        assert decision.identity.gate.gate_outcome is not None
        assert decision.identity.gate.gate_outcome.value == "PASS"
        assert decision.policy_applicability.status is QualificationApplicabilityStatus.APPLICABLE
        assert decision.qualification_outcome is EvaluationQualificationOutcome.QUALIFIED
        assert decision.reason_codes == ()

    def test_verified_applicable_fail_is_disqualified(
        self, qualification_chain_factory: Any, tmp_path: Path
    ) -> None:
        chain = qualification_chain_factory.chain(
            tmp_path / "fail", candidate_outcomes={"case-001": "pass", "case-002": "fail"}
        )
        decision = build_qualification_decision(chain.inputs())
        assert decision.identity.gate.gate_outcome is not None
        assert decision.identity.gate.gate_outcome.value == "FAIL"
        assert decision.policy_applicability.status is QualificationApplicabilityStatus.APPLICABLE
        assert decision.qualification_outcome is EvaluationQualificationOutcome.DISQUALIFIED

    def test_verified_applicable_gate_not_evaluated_is_not_evaluated(
        self, qualification_chain_factory: Any, tmp_path: Path
    ) -> None:
        chain = qualification_chain_factory.chain(
            tmp_path / "gne", policy_scope={"provider": "另一个 provider"}
        )
        decision = build_qualification_decision(chain.inputs())
        assert decision.identity.gate.gate_outcome is not None
        assert decision.identity.gate.gate_outcome.value == "NOT_EVALUATED"
        assert decision.policy_applicability.status is QualificationApplicabilityStatus.APPLICABLE
        assert decision.qualification_outcome is EvaluationQualificationOutcome.NOT_EVALUATED

    @pytest.mark.parametrize(
        ("label", "outcomes"),
        [
            ("对候选全过", None),
            ("候选有回归", {"case-001": "pass", "case-002": "fail"}),
        ],
    )
    def test_verified_not_applicable_is_never_qualified(
        self, qualification_chain_factory: Any, tmp_path: Path, label: str, outcomes: Any
    ) -> None:
        """🔴 **策略管不着时，门禁判什么都推到 NOT_EVALUATED。**

        哪怕门禁自己判了 ``FAIL`` 也不能借它推出 ``DISQUALIFIED``——§三 规则 3
        是无条件的，而那正是"拿策略不适用去指控候选"的反面。
        """
        chain = qualification_chain_factory.chain(
            tmp_path / f"na-{label}",
            candidate_outcomes=outcomes,
            qualification_scope={"provider": "一个完全不同的 provider"},
        )
        decision = build_qualification_decision(chain.inputs())
        assert (
            decision.policy_applicability.status is QualificationApplicabilityStatus.NOT_APPLICABLE
        )
        assert decision.qualification_outcome is EvaluationQualificationOutcome.NOT_EVALUATED
        assert "gate_outcome_eligible" in decision.__dict__.get("_", ()) or any(
            item.check_id == "gate_outcome_eligible"
            and item.outcome is QualificationCheckOutcome.NOT_EVALUATED
            for item in decision.checks
        )

    @pytest.mark.parametrize(
        ("label", "candidate"),
        [("门禁 PASS", None), ("门禁 FAIL", {"case-001": "pass", "case-002": "fail"})],
    )
    def test_invalid_evidence_is_always_disqualified(
        self, qualification_chain_factory: Any, tmp_path: Path, label: str, candidate: Any
    ) -> None:
        """🔴 证据链被查出确定冲突时，**无论门禁判什么都没资格**。"""
        chain = qualification_chain_factory.chain(
            tmp_path / f"inv-{label}", candidate_outcomes=candidate
        )
        chain.artifacts.baseline_run.write_bytes(chain.artifacts.baseline_run.read_bytes() + b" ")
        decision = build_qualification_decision(chain.inputs())
        assert decision.verification_outcome.value == "INVALID"
        assert decision.qualification_outcome is EvaluationQualificationOutcome.DISQUALIFIED
        assert "evidence_verification_verified" in [
            item.check_id
            for item in decision.checks
            if item.outcome is QualificationCheckOutcome.FAIL
        ]

    @pytest.mark.parametrize(
        ("label", "candidate"),
        [("门禁 PASS", None), ("门禁 FAIL", {"case-001": "pass", "case-002": "fail"})],
    )
    def test_not_verifiable_evidence_is_never_qualified(
        self, qualification_chain_factory: Any, tmp_path: Path, label: str, candidate: Any
    ) -> None:
        chain = qualification_chain_factory.chain(
            tmp_path / f"nv-{label}", candidate_outcomes=candidate
        )
        chain.artifacts.comparison.unlink()
        decision = build_qualification_decision(chain.inputs())
        assert decision.verification_outcome.value == "NOT_VERIFIABLE"
        assert decision.qualification_outcome is EvaluationQualificationOutcome.NOT_EVALUATED

    def test_the_aggregation_is_exhaustive(self, decision: Any) -> None:
        """九行矩阵之外**不得**出现别的隐式结果。"""
        seen = {decision.qualification_outcome}
        assert seen <= set(EvaluationQualificationOutcome)
        assert len(EvaluationQualificationOutcome) == 3


# ---------------------------------------------------------------------------
# E 组：结论模型
# ---------------------------------------------------------------------------


class TestDecisionModel:
    """E 组：模型必须拒绝自相矛盾的组合。"""

    def test_a_qualified_decision_round_trips(self, decision: Any, tmp_path: Path) -> None:
        path = tmp_path / "decision.json"
        write_qualification_decision(decision, path)
        assert load_qualification_decision(path) == decision

    def test_the_decision_digest_is_recomputable(self, decision: Any) -> None:
        assert decision.qualification_decision_digest == qualification_decision_digest(
            qualification_decision_payload(decision)
        )

    def test_the_decision_digest_does_not_include_itself(self, decision: Any) -> None:
        payload = qualification_decision_payload(decision)
        tampered = dict(payload)
        tampered["qualification_decision_digest"] = "sha256:" + "0" * 64
        assert qualification_decision_digest(tampered) == qualification_decision_digest(payload)

    def test_a_wrong_decision_digest_is_rejected(self, decision: Any) -> None:
        payload = qualification_decision_payload(decision)
        payload["qualification_decision_digest"] = "sha256:" + "9" * 64
        with pytest.raises(ValidationError, match="qualification_decision_digest 与结论内容不符"):
            EvaluationQualificationDecision.model_validate(payload)

    def test_a_tampered_check_changes_the_decision_digest(self, decision: Any) -> None:
        payload = qualification_decision_payload(decision)
        before = qualification_decision_digest(payload)
        payload["checks"][0]["observed"] = "被人改过"
        assert qualification_decision_digest(payload) != before

    def test_a_tampered_identity_changes_the_decision_digest(self, decision: Any) -> None:
        payload = qualification_decision_payload(decision)
        before = qualification_decision_digest(payload)
        payload["identity"]["qualification_policy"]["qualification_policy_revision"] = 77
        assert qualification_decision_digest(payload) != before

    def test_a_changed_outcome_changes_the_decision_digest(self, decision: Any) -> None:
        payload = qualification_decision_payload(decision)
        before = qualification_decision_digest(payload)
        payload["qualification_outcome"] = "DISQUALIFIED"
        assert qualification_decision_digest(payload) != before

    def test_unknown_top_level_field_is_rejected(self, decision: Any) -> None:
        payload = qualification_decision_payload(decision)
        payload["release_allowed"] = True
        with pytest.raises(QualificationInputError, match="顶层字段"):
            parse_qualification_decision(payload)

    def test_missing_top_level_field_is_rejected(self, decision: Any) -> None:
        payload = qualification_decision_payload(decision)
        del payload["policy_applicability"]
        with pytest.raises(QualificationInputError, match="顶层字段"):
            parse_qualification_decision(payload)

    def test_the_top_level_field_set_is_exactly_as_declared(self, decision: Any) -> None:
        assert set(qualification_decision_payload(decision)) == set(
            QUALIFICATION_DECISION_TOP_LEVEL_FIELDS
        )

    @pytest.mark.parametrize(
        ("label", "mutate", "message"),
        [
            (
                "证据 INVALID + QUALIFIED",
                lambda p: p.__setitem__("verification_outcome", "INVALID"),
                "证据没有被验证为 VERIFIED",
            ),
            (
                "门禁 FAIL + QUALIFIED",
                lambda p: p["identity"]["gate"].__setitem__("gate_outcome", "FAIL"),
                "不得给出 QUALIFIED",
            ),
            (
                "门禁 NOT_EVALUATED + QUALIFIED",
                lambda p: p["identity"]["gate"].__setitem__("gate_outcome", "NOT_EVALUATED"),
                "不得给出 QUALIFIED",
            ),
            (
                "策略不适用 + QUALIFIED",
                lambda p: p["policy_applicability"].update(
                    {"status": "NOT_APPLICABLE", "reasons": ["dataset_out_of_scope"]}
                ),
                "策略不适用",
            ),
            (
                "策略判不了 + QUALIFIED",
                lambda p: p["policy_applicability"].update(
                    {"status": "NOT_EVALUATED", "reasons": ["assessment_not_reached"]}
                ),
                "策略不适用",
            ),
            (
                "QUALIFIED 但有 FAIL",
                lambda p: p["checks"][1].update(
                    {"outcome": "FAIL", "reason_code": "evidence_verification_invalid"}
                ),
                "QUALIFIED 要求",
            ),
            (
                "QUALIFIED 但检查没查全",
                lambda p: p["checks"][5].update(
                    {"outcome": "NOT_EVALUATED", "reason_code": "not_reached"}
                ),
                "QUALIFIED 要求",
            ),
            (
                "证据身份不全 + QUALIFIED",
                lambda p: p["identity"]["evidence"].pop("bundle_digest"),
                "证据身份齐备",
            ),
            (
                "门禁身份不全 + QUALIFIED",
                lambda p: p["identity"]["gate"].pop("gate_policy_digest"),
                "门禁身份齐备",
            ),
        ],
    )
    def test_contradictory_combinations_are_rejected(
        self, decision: Any, label: str, mutate: Any, message: str
    ) -> None:
        """🔴 把"自相矛盾的资格结论"变成**构造失败**，而不是一条纪律。"""
        payload = qualification_decision_payload(decision)
        mutate(payload)
        payload["reason_codes"] = sorted(
            {item["reason_code"] for item in payload["checks"] if item["outcome"] != "PASS"}
        )
        _reseal(payload)
        with pytest.raises(ValidationError, match=message):
            EvaluationQualificationDecision.model_validate(payload)

    def test_a_disqualified_decision_without_a_failure_is_rejected(self, decision: Any) -> None:
        payload = qualification_decision_payload(decision)
        payload["qualification_outcome"] = "DISQUALIFIED"
        _reseal(payload)
        with pytest.raises(ValidationError, match="DISQUALIFIED 要求"):
            EvaluationQualificationDecision.model_validate(payload)

    def test_a_not_evaluated_decision_with_all_checks_passing_is_rejected(
        self, decision: Any
    ) -> None:
        payload = qualification_decision_payload(decision)
        payload["qualification_outcome"] = "NOT_EVALUATED"
        _reseal(payload)
        with pytest.raises(ValidationError, match="NOT_EVALUATED 要求"):
            EvaluationQualificationDecision.model_validate(payload)

    def test_a_missing_check_is_rejected(self, decision: Any) -> None:
        payload = qualification_decision_payload(decision)
        payload["checks"].pop(0)
        _reseal(payload)
        with pytest.raises(ValidationError, match="检查项必须完整"):
            EvaluationQualificationDecision.model_validate(payload)

    def test_a_reordered_check_list_is_rejected(self, decision: Any) -> None:
        payload = qualification_decision_payload(decision)
        payload["checks"].reverse()
        _reseal(payload)
        with pytest.raises(ValidationError, match="检查项必须完整"):
            EvaluationQualificationDecision.model_validate(payload)

    def test_reason_codes_must_match_the_checks(self, decision: Any) -> None:
        payload = qualification_decision_payload(decision)
        payload["reason_codes"] = ["something_else"]
        _reseal(payload)
        with pytest.raises(ValidationError, match="reason_codes"):
            EvaluationQualificationDecision.model_validate(payload)


# ---------------------------------------------------------------------------
# G 组：确定性
# ---------------------------------------------------------------------------


class TestDeterminism:
    """G 组：两次运行逐字节一致，不含易变内容。"""

    def test_two_decisions_are_byte_identical(self, chain: Any, tmp_path: Path) -> None:
        first = tmp_path / "d1.json"
        second = tmp_path / "d2.json"
        write_qualification_decision(build_qualification_decision(chain.inputs()), first)
        write_qualification_decision(build_qualification_decision(chain.inputs()), second)
        assert first.read_bytes() == second.read_bytes()

    def test_two_policy_dumps_are_byte_identical(self, chain: Any) -> None:
        policy = load_qualification_policy(chain.qualification_policy)
        from ai_psi.evaluation.serialization import dumps

        assert dumps(policy.model_dump(mode="json")) == dumps(policy.model_dump(mode="json"))

    def test_the_decision_ends_with_exactly_one_newline_and_no_crlf(
        self, decision: Any, tmp_path: Path
    ) -> None:
        path = tmp_path / "d.json"
        write_qualification_decision(decision, path)
        raw = path.read_bytes()
        assert b"\r\n" not in raw
        assert raw.endswith(b"\n")
        assert not raw.endswith(b"\n\n")

    def test_the_decision_carries_no_volatile_or_secret_content(
        self, decision: Any, tmp_path: Path
    ) -> None:
        path = tmp_path / "d.json"
        write_qualification_decision(decision, path)
        text = path.read_text(encoding="utf-8")
        for banned in (
            "generated_at",
            "timestamp",
            "created_at",
            "mtime",
            "inode",
            "hostname",
            "username",
            "run_id",
            "duration",
            str(_REPO_ROOT),
            str(tmp_path),
            "\\\\",
            _RESPONSE_SENTINEL,
            "api_key",
            "Authorization",
            "password",
            "postgresql://",
            "response_text",
            "failure_detail",
            "Traceback",
            "release_allowed",
            "deploy_allowed",
            "merge_allowed",
            "production_ready",
            "auto_merge",
            "auto_deploy",
            "approved_for_release",
            "release_recommendation",
            "quality_score",
            "risk_score",
            "trust_score",
            "recommendation",
            "signed",
            "signature",
            "attested",
        ):
            assert banned not in text, banned

    def test_the_decision_does_not_embed_its_inputs(self, decision: Any, chain: Any) -> None:
        """🔴 不嵌入完整 VerificationReport／EvidenceBundle／五个输入 Artifact。"""
        payload = qualification_decision_payload(decision)
        text = json.dumps(payload, ensure_ascii=False)
        assert "checks" in payload  # 只有 S8 自己的 13 项检查
        assert len(payload["checks"]) == 13
        assert "artifact" not in text
        assert "rule_results" not in text
        assert "cases" not in text
        # 它比它描述的那份 Bundle 小得多。
        assert len(text) < chain.bundle.stat().st_size
