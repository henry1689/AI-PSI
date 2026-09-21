"""证据链的严格加载、端到端重算与结论聚合（阶段 7 · S7）。

对应任务书 §二十 的 C（加载部分）、D、E、F、G、H 组。

🔴 这一组的存在理由：**"摘要对得上"与"重算对得上"是两条独立的防线，
而"读不出来"与"确定冲突"是两种不同的结论。** 把它们混成一种，就会
得到一个"因为没法判断所以判冲突"的工具——那正是 S7 最不能变成的样子。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import ai_psi.evaluation.evidence as evidence_module
from ai_psi.evaluation.comparison import compare_run_results
from ai_psi.evaluation.evidence import (
    VERIFICATION_CHECK_ORDER,
    EvidenceChainMismatchError,
    EvidenceInputError,
    EvidenceVerificationInputs,
    EvidenceVerificationReport,
    VerificationCheck,
    VerificationCheckOutcome,
    VerificationOutcome,
    VerificationReason,
    build_evidence_bundle,
    bundle_digest,
    load_evidence_bundle,
    verify_evidence_bundle,
    write_bundle,
)
from ai_psi.evaluation.gate import decide, policy_digest
from ai_psi.evaluation.serialization import dumps

pytestmark = pytest.mark.unit


@pytest.fixture
def chain(evidence_chain_factory: Any, tmp_path: Path) -> Any:
    return evidence_chain_factory.write(tmp_path / "chain")


@pytest.fixture
def bundle_file(chain: Any, tmp_path: Path) -> Path:
    path = tmp_path / "bundle.json"
    write_bundle(build_evidence_bundle(chain.inputs()), path)
    return path


def _verify(chain: Any, bundle_path: Path) -> EvidenceVerificationReport:
    return verify_evidence_bundle(
        EvidenceVerificationInputs(bundle=bundle_path, artifacts=chain.inputs())
    )


def _read_json(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(dumps(payload), encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# E 组：证据包加载
# ---------------------------------------------------------------------------


class TestBundleLoading:
    """E 组：严格加载。"""

    def test_valid_bundle_loads(self, bundle_file: Path) -> None:
        bundle = load_evidence_bundle(bundle_file)
        assert bundle.evidence_bundle_schema_version == 1
        assert len(bundle.artifacts) == 5

    def test_unknown_top_level_field_is_rejected(self, bundle_file: Path) -> None:
        payload = _read_json(bundle_file)
        payload["release_allowed"] = True
        _write_json(bundle_file, payload)
        with pytest.raises(EvidenceInputError, match="顶层字段"):
            load_evidence_bundle(bundle_file)

    def test_missing_top_level_field_is_rejected(self, bundle_file: Path) -> None:
        payload = _read_json(bundle_file)
        del payload["chain_identity"]
        _write_json(bundle_file, payload)
        with pytest.raises(EvidenceInputError, match="顶层字段"):
            load_evidence_bundle(bundle_file)

    def test_broken_json_is_rejected(self, bundle_file: Path) -> None:
        bundle_file.write_text('{"artifacts": ', encoding="utf-8", newline="\n")
        with pytest.raises(EvidenceInputError, match="JSON 解析失败"):
            load_evidence_bundle(bundle_file)

    def test_truncated_json_is_rejected(self, bundle_file: Path) -> None:
        text = bundle_file.read_text(encoding="utf-8")
        bundle_file.write_text(text[: len(text) // 2], encoding="utf-8", newline="\n")
        with pytest.raises(EvidenceInputError, match="JSON 解析失败"):
            load_evidence_bundle(bundle_file)

    @pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_numbers_are_rejected(self, bundle_file: Path, constant: str) -> None:
        """⚠️ 写成**裸常量**（不加引号）才测得到拒绝逻辑：Python 的 ``json``
        默认接受这三个，而它们一旦进了产物就是灾难（``NaN != NaN``）。"""
        text = bundle_file.read_text(encoding="utf-8")
        marker = '"evidence_bundle_schema_version": 1'
        assert marker in text
        bundle_file.write_text(
            text.replace(marker, f'"evidence_bundle_schema_version": {constant}', 1),
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(EvidenceInputError, match=constant):
            load_evidence_bundle(bundle_file)

    def test_duplicate_json_key_is_rejected(self, bundle_file: Path) -> None:
        """🔴 默认 JSON 会**后者覆盖前者**，于是"链里写了两份 chain_identity"
        会静默变成一份。"""
        text = bundle_file.read_text(encoding="utf-8")
        marker = '"evidence_bundle_schema_version": 1'
        assert marker in text
        bundle_file.write_text(
            text.replace(marker, marker + ', "evidence_bundle_schema_version": 1', 1),
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(EvidenceInputError, match="重复的键"):
            load_evidence_bundle(bundle_file)

    def test_non_utf8_bundle_is_rejected(self, bundle_file: Path) -> None:
        bundle_file.write_bytes(b"\xff\xfe\x00\x00 not utf-8")
        with pytest.raises(EvidenceInputError, match="UTF-8"):
            load_evidence_bundle(bundle_file)

    def test_illegal_digest_shape_is_rejected(self, bundle_file: Path) -> None:
        payload = _read_json(bundle_file)
        payload["bundle_digest"] = "not-a-digest"
        _write_json(bundle_file, payload)
        with pytest.raises(EvidenceInputError):
            load_evidence_bundle(bundle_file)

    def test_empty_artifact_list_is_rejected(self, bundle_file: Path) -> None:
        payload = _read_json(bundle_file)
        payload["artifacts"] = []
        _write_json(bundle_file, payload)
        with pytest.raises(EvidenceInputError):
            load_evidence_bundle(bundle_file)


# ---------------------------------------------------------------------------
# C/H 组：输入的严格加载与"用正式入口读"
# ---------------------------------------------------------------------------


class TestStrictArtifactLoading:
    """C 组（加载部分）+ H 组：五份输入都被严格读进来。"""

    def test_duplicate_json_key_in_an_input_is_rejected(self, chain: Any) -> None:
        """🔴 S5 的对比加载器**不拒重复键**；S7 在它之前补了这道关。"""
        text = chain.comparison.read_text(encoding="utf-8")
        marker = '"comparison_schema_version": 1'
        assert marker in text
        chain.comparison.write_text(
            text.replace(marker, marker + ', "comparison_schema_version": 1', 1),
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(EvidenceInputError, match="重复的键"):
            build_evidence_bundle(chain.inputs())

    def test_nan_in_an_input_is_rejected(self, chain: Any) -> None:
        text = chain.comparison.read_text(encoding="utf-8")
        chain.comparison.write_text(
            text.replace('"comparison_schema_version": 1', '"comparison_schema_version": NaN', 1),
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(EvidenceInputError, match="NaN"):
            build_evidence_bundle(chain.inputs())

    def test_canonical_result_is_not_accepted_as_a_run_result(self, chain: Any) -> None:
        """canonical 是"两次运行逐字节比对"用的稳定子集，**刻意**不含
        ``working_tree_clean``——拿它当证据输入必须被拒绝。"""
        canonical = chain.baseline_run.parent / "baseline-canonical.json"
        assert canonical.is_file()
        chain.baseline_run.write_bytes(canonical.read_bytes())
        with pytest.raises(EvidenceInputError, match="canonical"):
            build_evidence_bundle(chain.inputs())

    def test_a_run_result_is_not_accepted_as_a_comparison(self, chain: Any) -> None:
        chain.comparison.write_bytes(chain.baseline_run.read_bytes())
        with pytest.raises(EvidenceInputError, match="对比产物不可用"):
            build_evidence_bundle(chain.inputs())

    def test_input_content_is_data_not_code(
        self, chain: Any, bundle_file: Path, tmp_path: Path
    ) -> None:
        """🔴 输入里的内容**只是数据**：不 ``eval``、不导入、不执行。

        这里用的哨兵是"执行了就会留下痕迹"的那种：真跑起来会创建下面这个
        文件。断言它**没有**被创建，比断言"没有抛异常"有力得多。
        """
        trace = tmp_path / "executed.marker"
        marker = f"__import__('pathlib').Path({str(trace)!r}).write_text('pwned')"
        payload = _read_json(chain.comparison)
        payload["baseline"]["package_version"] = marker
        _write_json(chain.comparison, payload)
        chain.refresh_descriptors(bundle_file)

        verify_evidence_bundle(
            EvidenceVerificationInputs(bundle=bundle_file, artifacts=chain.inputs())
        )
        assert not trace.exists()
        # 哨兵被原样读回来了 —— 它是**数据**，不是被执行的代码。
        assert _read_json(chain.comparison)["baseline"]["package_version"] == marker

    def test_module_reaches_no_network_provider_or_database(self) -> None:
        """🔴 用**源码级**断言钉住：任何一次"顺手加个 fetch"都会让它变红。"""
        for module in (evidence_module,):
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

    def test_uses_the_official_recompute_entry_points(self, chain: Any, bundle_file: Path) -> None:
        """重算用的是 S5／S6 的**纯函数**，结果与它们逐字一致。"""
        from ai_psi.evaluation.comparison import (
            compare_run_results,
            load_comparison,
            load_run_result,
        )
        from ai_psi.evaluation.gate import decide, load_gate_policy

        baseline = load_run_result(chain.baseline_run)
        candidate = load_run_result(chain.candidate_run)
        comparison = load_comparison(chain.comparison)
        assert compare_run_results(baseline, candidate) == comparison

        policy = load_gate_policy(chain.policy)
        from ai_psi.evaluation.gate import load_gate_decision

        assert decide(comparison, policy) == load_gate_decision(chain.gate_decision)


# ---------------------------------------------------------------------------
# D 组：构建链路
# ---------------------------------------------------------------------------


class TestBuildChain:
    """D 组：端到端重算与拒绝构建。"""

    def test_valid_chain_builds(self, chain: Any) -> None:
        bundle = build_evidence_bundle(chain.inputs())
        assert len(bundle.artifacts) == 5
        assert bundle.chain_identity.gate_outcome == "PASS"

    def test_gate_fail_chain_builds(self, evidence_chain_factory: Any, tmp_path: Path) -> None:
        """🔴 门禁判 ``FAIL`` **不影响构建**：只要链是自洽的。"""
        chain = evidence_chain_factory.write(
            tmp_path / "fail",
            candidate_outcomes={"case-001": "pass", "case-002": "fail"},
        )
        bundle = build_evidence_bundle(chain.inputs())
        assert bundle.chain_identity.gate_outcome == "FAIL"
        bundle_file = tmp_path / "fail-bundle.json"
        write_bundle(bundle, bundle_file)
        report = _verify(chain, bundle_file)
        assert report.verification_outcome is VerificationOutcome.VERIFIED
        assert report.gate_outcome is not None
        assert report.gate_outcome.value == "FAIL"

    def test_gate_not_evaluated_chain_builds(
        self, evidence_chain_factory: Any, tmp_path: Path
    ) -> None:
        """策略不适用 ⇒ 门禁 ``NOT_EVALUATED``；链仍然自洽，仍可构建。"""
        chain = evidence_chain_factory.write(
            tmp_path / "ne", policy_scope={"provider": "完全不同的 provider"}
        )
        bundle = build_evidence_bundle(chain.inputs())
        assert bundle.chain_identity.gate_outcome == "NOT_EVALUATED"
        bundle_file = tmp_path / "ne-bundle.json"
        write_bundle(bundle, bundle_file)
        report = _verify(chain, bundle_file)
        assert report.verification_outcome is VerificationOutcome.VERIFIED
        assert report.gate_outcome is not None
        assert report.gate_outcome.value == "NOT_EVALUATED"

    def test_comparison_inconsistent_with_the_runs_is_rejected(self, chain: Any) -> None:
        """🔴 只比 ``comparison_eligible``、只比提交号、只比摘要的实现都会
        放过这一处改动——所以这里改的正是那种实现看不见的字段。"""
        payload = _read_json(chain.comparison)
        payload["integrity_notes"] = ["合成注入的说明"]
        _write_json(chain.comparison, payload)
        with pytest.raises(EvidenceChainMismatchError, match="重算的对比与输入不一致") as info:
            build_evidence_bundle(chain.inputs())
        assert "integrity_notes" in info.value.mismatches

    def test_decision_inconsistent_with_the_comparison_is_rejected(self, chain: Any) -> None:
        """同样地，只比 outcome 的实现会放过这里。"""
        payload = _read_json(chain.gate_decision)
        payload["identity"]["comparison_fingerprint"] = "sha256:" + "9" * 64
        _write_json(chain.gate_decision, payload)
        with pytest.raises(EvidenceChainMismatchError, match="重算的门禁结论与输入不一致") as info:
            build_evidence_bundle(chain.inputs())
        assert "identity" in info.value.mismatches

    def test_policy_changed_after_the_decision_is_rejected(self, chain: Any) -> None:
        """策略改了 ``policy_id``（结论的 outcome 完全没变）——链已断。"""
        payload = _read_json(chain.policy)
        payload["policy_id"] = "another_policy"
        payload["policy_digest"] = policy_digest(payload)
        _write_json(chain.policy, payload)
        with pytest.raises(EvidenceChainMismatchError, match="重算的门禁结论与输入不一致"):
            build_evidence_bundle(chain.inputs())

    def test_build_never_rewrites_the_inputs(self, chain: Any) -> None:
        before = {path: path.read_bytes() for _, path in chain.inputs().by_role()}
        build_evidence_bundle(chain.inputs())
        after = {path: path.read_bytes() for _, path in chain.inputs().by_role()}
        assert before == after

    def test_build_does_not_emit_a_corrected_comparison(self, chain: Any, tmp_path: Path) -> None:
        """不一致时**不产出**任何东西——尤其不产出"修正后"的对比。"""
        payload = _read_json(chain.comparison)
        payload["integrity_notes"] = ["合成注入的说明"]
        _write_json(chain.comparison, payload)
        target = tmp_path / "should-not-exist.json"
        with pytest.raises(EvidenceChainMismatchError):
            build_evidence_bundle(chain.inputs())
        assert not target.exists()


# ---------------------------------------------------------------------------
# F 组：结论聚合
# ---------------------------------------------------------------------------


class TestVerificationAggregation:
    """F 组：VERIFIED / INVALID / NOT_VERIFIABLE 的聚合。"""

    def test_all_checks_pass_is_verified(self, chain: Any, bundle_file: Path) -> None:
        report = _verify(chain, bundle_file)
        assert report.verification_outcome is VerificationOutcome.VERIFIED
        assert report.failed_check_ids == ()
        assert report.not_evaluated_check_ids == ()
        assert [item.check_id for item in report.checks] == list(VERIFICATION_CHECK_ORDER)

    def test_missing_input_is_not_verifiable(self, chain: Any, bundle_file: Path) -> None:
        chain.policy.unlink()
        report = _verify(chain, bundle_file)
        assert report.verification_outcome is VerificationOutcome.NOT_VERIFIABLE
        assert report.failed_check_ids == ()
        assert "policy_content_digest_valid" in report.not_evaluated_check_ids

    def test_unreadable_input_is_not_verifiable(self, chain: Any, bundle_file: Path) -> None:
        """把输入换成**目录**——读不出字节，但不是"内容冲突"。"""
        chain.policy.unlink()
        chain.policy.mkdir()
        report = _verify(chain, bundle_file)
        assert report.verification_outcome is VerificationOutcome.NOT_VERIFIABLE

    def test_unsupported_contract_version_is_not_verifiable(
        self, chain: Any, bundle_file: Path
    ) -> None:
        """🔴 版本不受支持时结论是 **NOT_VERIFIABLE**，不是 ``INVALID``：
        我们没法按这份版本重算，所以"对不上"这个判断压根没有依据。"""
        payload = _read_json(chain.comparison)
        payload["comparison_schema_version"] = 2
        _write_json(chain.comparison, payload)
        chain.refresh_descriptors(bundle_file)

        report = _verify(chain, bundle_file)
        assert report.verification_outcome is VerificationOutcome.NOT_VERIFIABLE
        assert report.failed_check_ids == ()
        assert "comparison_schema_valid" in report.not_evaluated_check_ids
        assert not report.parse_level_failure()

    def test_unsupported_bundle_version_is_not_verifiable(
        self, chain: Any, bundle_file: Path
    ) -> None:
        payload = _read_json(bundle_file)
        payload["evidence_bundle_schema_version"] = 99
        _write_json(bundle_file, payload)
        report = _verify(chain, bundle_file)
        assert report.verification_outcome is VerificationOutcome.NOT_VERIFIABLE
        assert report.failed_check_ids == ()
        assert not report.parse_level_failure()

    def test_unparsable_bundle_is_not_verifiable_and_parse_level(
        self, chain: Any, bundle_file: Path
    ) -> None:
        bundle_file.write_text("{ not json", encoding="utf-8", newline="\n")
        report = _verify(chain, bundle_file)
        assert report.verification_outcome is VerificationOutcome.NOT_VERIFIABLE
        assert report.failed_check_ids == ()
        assert report.parse_level_failure() is True

    def test_missing_bundle_is_not_verifiable(self, chain: Any, tmp_path: Path) -> None:
        report = _verify(chain, tmp_path / "nope.json")
        assert report.verification_outcome is VerificationOutcome.NOT_VERIFIABLE
        assert report.bundle_digest is None
        assert report.gate_outcome is None

    def test_verification_never_reports_verified_when_a_check_is_unevaluated(
        self, chain: Any, bundle_file: Path
    ) -> None:
        chain.policy.unlink()
        report = _verify(chain, bundle_file)
        assert report.verification_outcome is not VerificationOutcome.VERIFIED
        assert any(item.outcome is VerificationCheckOutcome.NOT_EVALUATED for item in report.checks)

    def test_report_model_rejects_contradictory_combinations(
        self, chain: Any, bundle_file: Path
    ) -> None:
        """🔴 把"结论与明细必须一致"变成**构造失败**。"""
        payload = _read_json(bundle_file)
        payload["artifacts"].pop(2)
        _write_json(bundle_file, payload)
        broken = _verify(chain, bundle_file)
        assert broken.verification_outcome is VerificationOutcome.INVALID

        dumped = broken.model_dump(mode="json")
        for bad_outcome, message in (
            ("VERIFIED", "VERIFIED 要求"),
            ("NOT_VERIFIABLE", "必须是 INVALID"),
        ):
            contradictory = dict(dumped)
            contradictory["verification_outcome"] = bad_outcome
            with pytest.raises(ValidationError, match=message):
                EvidenceVerificationReport.model_validate(contradictory)

    def test_check_model_rejects_contradictory_outcomes(self) -> None:
        with pytest.raises(ValidationError, match="读不出来的东西不构成冲突"):
            VerificationCheck.model_validate(
                {
                    "check_id": "x",
                    "outcome": "FAIL",
                    "reason_code": VerificationReason.ARTIFACT_UNAVAILABLE,
                }
            )
        with pytest.raises(ValidationError, match="通过了，原因码却是"):
            VerificationCheck.model_validate(
                {
                    "check_id": "x",
                    "outcome": "PASS",
                    "reason_code": VerificationReason.CONTENT_DIGEST_MISMATCH,
                }
            )
        with pytest.raises(ValidationError, match="未得到结论，原因码却是"):
            VerificationCheck.model_validate(
                {
                    "check_id": "x",
                    "outcome": "NOT_EVALUATED",
                    "reason_code": VerificationReason.SATISFIED,
                }
            )

    def test_reason_codes_partition_the_enum(self) -> None:
        """三个原因码集合必须**恰好划分**整个枚举——漏一个就会在运行期
        冒出一个"既不是通过也不是冲突"的检查。"""
        everything = set(VerificationReason)
        union = (
            evidence_module._SATISFIED_REASONS
            | evidence_module._MISMATCH_REASONS
            | evidence_module._UNAVAILABLE_REASONS
        )
        assert union == everything
        assert not (evidence_module._SATISFIED_REASONS & evidence_module._MISMATCH_REASONS)
        assert not (evidence_module._SATISFIED_REASONS & evidence_module._UNAVAILABLE_REASONS)
        assert not (evidence_module._MISMATCH_REASONS & evidence_module._UNAVAILABLE_REASONS)
        assert evidence_module._PARSE_LEVEL_REASONS <= evidence_module._UNAVAILABLE_REASONS


# ---------------------------------------------------------------------------
# G 组：门禁结论与证据结论分离
# ---------------------------------------------------------------------------


class TestGateAndEvidenceSeparation:
    """G 组：两个独立维度。"""

    def test_gate_pass_with_verified(self, chain: Any, bundle_file: Path) -> None:
        report = _verify(chain, bundle_file)
        assert report.gate_outcome is not None
        assert report.gate_outcome.value == "PASS"
        assert report.verification_outcome is VerificationOutcome.VERIFIED

    def test_gate_fail_with_verified(self, evidence_chain_factory: Any, tmp_path: Path) -> None:
        """🔴 **合法且必要**的结果：门禁说候选没通过，证据链说那份"没通过"
        是真的、没被改过。"""
        chain = evidence_chain_factory.write(
            tmp_path / "fail", candidate_outcomes={"case-001": "pass", "case-002": "fail"}
        )
        bundle_file = tmp_path / "bundle.json"
        write_bundle(build_evidence_bundle(chain.inputs()), bundle_file)
        report = _verify(chain, bundle_file)
        assert report.gate_outcome is not None
        assert report.gate_outcome.value == "FAIL"
        assert report.verification_outcome is VerificationOutcome.VERIFIED

    def test_gate_not_evaluated_with_verified(
        self, evidence_chain_factory: Any, tmp_path: Path
    ) -> None:
        chain = evidence_chain_factory.write(tmp_path / "ne", policy_scope={"provider": "other"})
        bundle_file = tmp_path / "bundle.json"
        write_bundle(build_evidence_bundle(chain.inputs()), bundle_file)
        report = _verify(chain, bundle_file)
        assert report.gate_outcome is not None
        assert report.gate_outcome.value == "NOT_EVALUATED"
        assert report.verification_outcome is VerificationOutcome.VERIFIED

    def test_changed_content_makes_evidence_invalid_under_gate_pass(
        self, chain: Any, bundle_file: Path
    ) -> None:
        chain.baseline_run.write_bytes(chain.baseline_run.read_bytes() + b" ")
        report = _verify(chain, bundle_file)
        assert report.gate_outcome is not None
        assert report.gate_outcome.value == "PASS"
        assert report.verification_outcome is VerificationOutcome.INVALID

    def test_report_never_carries_a_release_field(self, chain: Any, bundle_file: Path) -> None:
        text = dumps(_verify(chain, bundle_file).model_dump(mode="json"))
        for banned in (
            "release_allowed",
            "deploy_allowed",
            "merge_allowed",
            "production_ready",
            "quality_score",
            "risk_score",
            "recommendation",
            "signed",
            "signature",
            "attested",
            "tamper-proof",
        ):
            assert banned not in text, banned

    def test_report_digest_does_not_depend_on_gate_outcome(
        self, evidence_chain_factory: Any, tmp_path: Path
    ) -> None:
        """证据结论由**检查明细**决定，不由门禁结论推出来。"""
        pass_chain = evidence_chain_factory.write(tmp_path / "p")
        fail_chain = evidence_chain_factory.write(
            tmp_path / "f", candidate_outcomes={"case-001": "pass", "case-002": "fail"}
        )
        pass_bundle = tmp_path / "p.json"
        fail_bundle = tmp_path / "f.json"
        write_bundle(build_evidence_bundle(pass_chain.inputs()), pass_bundle)
        write_bundle(build_evidence_bundle(fail_chain.inputs()), fail_bundle)

        pass_report = _verify(pass_chain, pass_bundle)
        fail_report = _verify(fail_chain, fail_bundle)
        assert pass_report.verification_outcome is VerificationOutcome.VERIFIED
        assert fail_report.verification_outcome is VerificationOutcome.VERIFIED
        assert pass_report.gate_outcome != fail_report.gate_outcome


# ---------------------------------------------------------------------------
# H 组：重算的独立性与准确性
# ---------------------------------------------------------------------------


class TestRecompute:
    """H 组：重算确实做了，而且用的是正式入口。"""

    def test_recomputed_comparison_is_compared_field_by_field(
        self, chain: Any, bundle_file: Path
    ) -> None:
        """🔴 **只有重算能发现这一处**：内容摘要、字节长度、结构、五方身份
        全部对得上（描述符已按新文件重算），唯独重算出来的对比与它不同。

        这条钉住了"只比摘要不重算"是不够的。

        ⚠️ 两处重算会同时报冲突，而且**应当如此**：门禁结论里绑着对比的
        指纹，对比变了，重算出来的结论自然也不同——这正是一条"链"的
        应有之义。
        """
        payload = _read_json(chain.comparison)
        payload["allowed_differences"] = []
        _write_json(chain.comparison, payload)
        chain.refresh_descriptors(bundle_file)

        report = _verify(chain, bundle_file)
        assert report.verification_outcome is VerificationOutcome.INVALID
        assert report.failed_check_ids == (
            "comparison_recomputed_equal",
            "gate_decision_recomputed_equal",
        )
        assert "comparison_content_digest_valid" not in report.failed_check_ids
        assert "comparison_schema_valid" not in report.not_evaluated_check_ids

    def test_tampered_decision_is_detected_by_recomputation(
        self, chain: Any, bundle_file: Path
    ) -> None:
        payload = _read_json(chain.gate_decision)
        payload["identity"]["policy_revision"] = 77
        _write_json(chain.gate_decision, payload)
        chain.refresh_descriptors(bundle_file)
        report = _verify(chain, bundle_file)
        assert report.verification_outcome is VerificationOutcome.INVALID
        assert "gate_decision_recomputed_equal" in report.failed_check_ids

    def test_bundle_digest_in_the_report_is_the_recomputed_one(
        self, chain: Any, bundle_file: Path
    ) -> None:
        payload = _read_json(bundle_file)
        payload["chain_identity"]["policy_revision"] = 42
        _write_json(bundle_file, payload)
        report = _verify(chain, bundle_file)
        assert report.bundle_digest == bundle_digest(payload)
        assert report.verification_outcome is VerificationOutcome.INVALID

    def test_a_tampered_chain_identity_blocks_promotion(
        self, chain: Any, bundle_file: Path
    ) -> None:
        """🔴 改了 ``chain_identity`` 却没重算摘要 ⇒ 这份东西**不再是**一份
        严格的正式 Bundle，因此**不提提升**，后续检查一律 ``NOT_EVALUATED``。

        ⚠️ 这正是"摘要自洽"被放进严格模型的代价，也是它的意义：一份连
        自身摘要都对不上的记录，不该被当作"某几项恰好没问题"的正式 Bundle
        继续往下走。它的冲突由 ``bundle_digest_valid`` 明确报出。
        """
        payload = _read_json(bundle_file)
        payload["chain_identity"]["dataset_digest"] = "sha256:" + "7" * 64
        _write_json(bundle_file, payload)
        report = _verify(chain, bundle_file)
        assert report.verification_outcome is VerificationOutcome.INVALID
        assert report.failed_check_ids == ("bundle_digest_valid",)
        assert "chain_identity_valid" in report.not_evaluated_check_ids
        assert "comparison_recomputed_equal" in report.not_evaluated_check_ids

    def test_chain_identity_is_recomputed_from_the_inputs(
        self, chain: Any, bundle_file: Path
    ) -> None:
        """角色与摘要都自洽，但链身份与五个输入对不上 ⇒ 重算仍会发现它。"""
        payload = _read_json(bundle_file)
        payload["chain_identity"]["dataset_digest"] = "sha256:" + "7" * 64
        _write_json(bundle_file, payload)
        chain.refresh_descriptors(bundle_file)
        # refresh_descriptors 只按文件重算描述符摘要与 bundle_digest，
        # 因此 chain_identity 的那处改动仍然是"自洽但不对"的。
        report = _verify(chain, bundle_file)
        assert "chain_identity_valid" in report.failed_check_ids

    def test_verification_is_deterministic(self, chain: Any, bundle_file: Path) -> None:
        first = _verify(chain, bundle_file).model_dump(mode="json")
        second = _verify(chain, bundle_file).model_dump(mode="json")
        assert dumps(first) == dumps(second)

    def test_verification_does_not_modify_the_bundle_or_inputs(
        self, chain: Any, bundle_file: Path
    ) -> None:
        before = {path: path.read_bytes() for _, path in chain.inputs().by_role()}
        before_bundle = bundle_file.read_bytes()
        _verify(chain, bundle_file)
        assert bundle_file.read_bytes() == before_bundle
        assert {path: path.read_bytes() for _, path in chain.inputs().by_role()} == before

    def test_verification_report_carries_no_secret_prose_or_path(
        self, chain: Any, bundle_file: Path, tmp_path: Path
    ) -> None:
        text = dumps(_verify(chain, bundle_file).model_dump(mode="json"))
        for sentinel in (
            "合成的回答文本",
            "api_key",
            "Authorization",
            "password",
            "postgresql://",
            "response_text",
            "failure_detail",
            "Traceback",
            str(tmp_path),
        ):
            assert sentinel not in text, sentinel


# ---------------------------------------------------------------------------
# 补救 · D 组：角色异常 → INVALID，且**不进入** S5／S6 重算
# ---------------------------------------------------------------------------


def _drop(payload: dict[str, Any], index: int) -> None:
    payload["artifacts"].pop(index)


def _mutate_payload(bundle_path: Path, mutate: Any) -> dict[str, Any]:
    """读一份 Bundle payload、改一处、**不重算摘要**。

    ⚠️ 摘要故意留着不改：角色异常那一侧要看的正是"结构不对"本身，
    而不是先撞在摘要上。
    """
    payload = _read_json(bundle_path)
    mutate(payload)
    return payload


_ROLE_ANOMALIES: tuple[tuple[str, Any, str, bool], ...] = (
    ("缺失 COMPARISON", lambda p: _drop(p, 2), "artifact_role_missing", True),
    ("缺失 BASELINE_RUN", lambda p: _drop(p, 0), "artifact_role_missing", True),
    (
        "重复 BASELINE_RUN",
        lambda p: p["artifacts"].append(dict(p["artifacts"][0])),
        "artifact_role_duplicated",
        True,
    ),
    (
        "重复 GATE_DECISION",
        lambda p: p["artifacts"].append(dict(p["artifacts"][4])),
        "artifact_role_duplicated",
        True,
    ),
    ("打乱顺序", lambda p: p["artifacts"].reverse(), "artifact_order_invalid", False),
)


@pytest.fixture
def recompute_counter(monkeypatch: Any) -> dict[str, int]:
    """数一数 S5／S6 的重算入口到底被调了几次。

    🔴 只有一个计数器能证明"角色异常**没有**进入重算"——断言报告里
    "那两项是 NOT_EVALUATED"只是从结论倒推，而结论恰好可能因为别的原因
    长成那样。
    """
    counter = {"comparison": 0, "gate_decision": 0}

    def counting_compare(*args: Any, **kwargs: Any) -> Any:
        counter["comparison"] += 1
        return compare_run_results(*args, **kwargs)

    def counting_decide(*args: Any, **kwargs: Any) -> Any:
        counter["gate_decision"] += 1
        return decide(*args, **kwargs)

    monkeypatch.setattr(evidence_module, "compare_run_results", counting_compare)
    monkeypatch.setattr(evidence_module, "decide", counting_decide)
    return counter


class TestRoleAnomalies:
    """D 组：缺失／重复／错序 → INVALID，且一次重算都不发生。"""

    @pytest.mark.parametrize(
        ("label", "mutate", "reason", "order_not_evaluated"),
        _ROLE_ANOMALIES,
        ids=[item[0] for item in _ROLE_ANOMALIES],
    )
    def test_role_anomaly_is_invalid_and_skips_recompute(
        self,
        chain: Any,
        bundle_file: Path,
        recompute_counter: dict[str, int],
        label: str,
        mutate: Any,
        reason: str,
        order_not_evaluated: bool,
    ) -> None:
        _write_json(bundle_file, _mutate_payload(bundle_file, mutate))
        report = _verify(chain, bundle_file)

        assert report.verification_outcome is VerificationOutcome.INVALID

        by_id = {item.check_id: item for item in report.checks}
        assert by_id["artifact_roles_complete"].outcome is (
            VerificationCheckOutcome.PASS
            if reason == "artifact_order_invalid"
            else VerificationCheckOutcome.FAIL
        )
        if reason != "artifact_order_invalid":
            assert by_id["artifact_roles_complete"].reason_code.value == reason
        else:
            assert by_id["artifact_order_valid"].reason_code.value == reason

        if order_not_evaluated:
            assert by_id["artifact_order_valid"].outcome is (VerificationCheckOutcome.NOT_EVALUATED)

        # 🔴 后续所有依赖唯一角色映射的检查都是 NOT_EVALUATED。
        for check_id in (
            "baseline_content_digest_valid",
            "comparison_schema_valid",
            "chain_identity_valid",
            "baseline_role_valid",
            "comparison_recomputed_equal",
            "gate_decision_recomputed_equal",
        ):
            assert by_id[check_id].outcome is VerificationCheckOutcome.NOT_EVALUATED, check_id

        # 🔴 最关键的一条：重算入口**一次都没被调用**。
        assert recompute_counter == {"comparison": 0, "gate_decision": 0}, label

    def test_a_healthy_chain_does_call_both_recompute_entries(
        self, chain: Any, bundle_file: Path, recompute_counter: dict[str, int]
    ) -> None:
        """反面对照：同一条链在健康时**确实**会调用它们。

        没有这一条，"计数器是 0"也可能只是因为计数器本身坏了。
        """
        report = _verify(chain, bundle_file)
        assert report.verification_outcome is VerificationOutcome.VERIFIED
        assert recompute_counter == {"comparison": 1, "gate_decision": 1}

    @pytest.mark.parametrize(
        ("label", "mutate", "reason", "order_not_evaluated"),
        _ROLE_ANOMALIES,
        ids=[item[0] for item in _ROLE_ANOMALIES],
    )
    def test_role_anomaly_reports_are_byte_identical(
        self,
        chain: Any,
        bundle_file: Path,
        label: str,
        mutate: Any,
        reason: str,
        order_not_evaluated: bool,
    ) -> None:
        """🔴 缺失／重复／错序三种报告的确定性各测一遍。"""
        _write_json(bundle_file, _mutate_payload(bundle_file, mutate))
        first = dumps(_verify(chain, bundle_file).model_dump(mode="json"))
        second = dumps(_verify(chain, bundle_file).model_dump(mode="json"))
        assert first == second, label

    @pytest.mark.parametrize(
        ("label", "mutate", "reason", "order_not_evaluated"),
        _ROLE_ANOMALIES,
        ids=[item[0] for item in _ROLE_ANOMALIES],
    )
    def test_role_anomaly_reports_leak_nothing(
        self,
        chain: Any,
        bundle_file: Path,
        label: str,
        mutate: Any,
        reason: str,
        order_not_evaluated: bool,
    ) -> None:
        _write_json(bundle_file, _mutate_payload(bundle_file, mutate))
        text = dumps(_verify(chain, bundle_file).model_dump(mode="json"))
        for sentinel in (
            "合成的回答文本",
            "response_text",
            "Traceback",
            "postgresql://",
            str(bundle_file.parent),
        ):
            assert sentinel not in text, (label, sentinel)

    def test_a_duplicated_role_is_never_collapsed_into_one(
        self, chain: Any, bundle_file: Path
    ) -> None:
        """🔴 重复角色不得被 dict 转换静默覆盖成"最后一个"。

        观察值里必须**两个都在**——那才是"看见了重复"，而不是"挑了一个"。
        """
        payload = _mutate_payload(
            bundle_file, lambda p: p["artifacts"].append(dict(p["artifacts"][0]))
        )
        _write_json(bundle_file, payload)
        report = _verify(chain, bundle_file)
        observed = next(
            item.observed for item in report.checks if item.check_id == "artifact_roles_complete"
        )
        assert observed == (
            "BASELINE_RUN,CANDIDATE_RUN,COMPARISON,POLICY,GATE_DECISION,BASELINE_RUN"
        )
