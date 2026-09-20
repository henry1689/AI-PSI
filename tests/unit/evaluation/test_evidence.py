"""证据包契约：定义身份、角色、内容摘要与确定性（阶段 7 · S7）。

对应任务书 §二十 的 A、B、C 组，以及 K、L 的安全部分。

🔴 这一组的存在理由：**一份说得清"这是哪五份文件、它们长什么样"的
记录，与一份说了算的结论，是两回事。** 这里测的全部是前者——S7 从不
宣称任何东西可信，它只宣称"给定这组文件，我核对过什么、结果如何"。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ai_psi.evaluation.evidence import (
    ARTIFACT_ROLE_ORDER,
    EVIDENCE_BUNDLE_SCHEMA_VERSION,
    EVIDENCE_BUNDLE_TOP_LEVEL_FIELDS,
    EVIDENCE_DEFINITION,
    ArtifactDescriptor,
    ArtifactRole,
    EvaluationEvidenceBundle,
    EvidenceInputError,
    VerificationOutcome,
    build_evidence_bundle,
    bundle_digest,
    bundle_payload,
    evidence_definition_digest,
    load_evidence_bundle,
    parse_evidence_bundle,
    verify_evidence_bundle,
    write_bundle,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: 合成运行结果里的回答正文。它**不得**出现在任何 S7 产物里。
_RESPONSE_SENTINEL = "合成的回答文本"


@pytest.fixture
def chain(evidence_chain_factory: Any, tmp_path: Path) -> Any:
    """一条落在磁盘上的、自洽的五文件证据链。"""
    return evidence_chain_factory.write(tmp_path / "chain")


@pytest.fixture
def bundle(chain: Any) -> EvaluationEvidenceBundle:
    return build_evidence_bundle(chain.inputs())


@pytest.fixture
def bundle_file(bundle: EvaluationEvidenceBundle, tmp_path: Path) -> Path:
    path = tmp_path / "bundle.json"
    write_bundle(bundle, path)
    return path


def _mutate_json(path: Path, mutate: Callable[[dict[str, Any]], None]) -> Path:
    """按 ``sort_keys + indent=2`` 重写 JSON——与 :func:`dumps` 逐字一致，
    因此**未经改动的往返不会改变摘要**，测到的只会是那一处改动。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _verify(chain: Any, bundle_path: Path) -> Any:
    from ai_psi.evaluation.evidence import EvidenceVerificationInputs

    return verify_evidence_bundle(
        EvidenceVerificationInputs(bundle=bundle_path, artifacts=chain.inputs())
    )


# ---------------------------------------------------------------------------
# A 组：证据包身份
# ---------------------------------------------------------------------------


class TestBundleIdentity:
    """A 组：版本、定义摘要、bundle_digest。"""

    def test_schema_version_is_one(self) -> None:
        assert EVIDENCE_BUNDLE_SCHEMA_VERSION == 1

    def test_definition_digest_is_stable(self) -> None:
        assert evidence_definition_digest() == evidence_definition_digest()
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", evidence_definition_digest())

    def test_definition_records_the_role_set_and_order(self) -> None:
        """🔴 角色集合与固定顺序必须进摘要：改顺序而摘要不变，等于宣称
        两份顺序不同的记录是同一套规则下的产物。"""
        assert EVIDENCE_DEFINITION["artifact_roles"] == [
            "BASELINE_RUN",
            "CANDIDATE_RUN",
            "COMPARISON",
            "POLICY",
            "GATE_DECISION",
        ]
        assert isinstance(EVIDENCE_DEFINITION["artifact_order"], str)

    def test_definition_records_digest_and_length_rules(self) -> None:
        for key in ("content_sha256", "byte_length", "bundle_digest"):
            assert key in EVIDENCE_DEFINITION
        assert "原始字节" in str(EVIDENCE_DEFINITION["content_sha256"])

    def test_definition_records_the_recompute_rules(self) -> None:
        for key in ("recompute_comparison", "gate_decision_recompute", "recompute_direction"):
            assert key in EVIDENCE_DEFINITION

    def test_definition_records_the_aggregation_rules(self) -> None:
        aggregation = EVIDENCE_DEFINITION["outcome_aggregation"]
        assert isinstance(aggregation, dict)
        assert aggregation["all_checks_pass"] == "VERIFIED"
        assert aggregation["any_check_fail"] == "INVALID"

    def test_definition_records_the_forbidden_output(self) -> None:
        forbidden = str(EVIDENCE_DEFINITION["forbidden_output"])
        for name in ("response_text", "Prompt 正文", "绝对路径", "当前时间"):
            assert name in forbidden

    def test_definition_has_no_volatile_content(self) -> None:
        """定义里不得出现绝对路径、当前时间或随机值。"""
        text = json.dumps(EVIDENCE_DEFINITION, ensure_ascii=False)
        assert str(_REPO_ROOT) not in text
        assert not re.search(r"[A-Za-z]:\\\\", text)
        assert "inspect.getsource" not in text
        assert evidence_definition_digest() == evidence_definition_digest()

    def test_bundle_digest_is_recomputable(self, bundle: EvaluationEvidenceBundle) -> None:
        assert bundle.bundle_digest == bundle_digest(bundle_payload(bundle))

    def test_bundle_digest_does_not_include_itself(self, bundle: EvaluationEvidenceBundle) -> None:
        """🔴 自排除——含自己的摘要无法被独立重算。"""
        payload = bundle_payload(bundle)
        tampered = dict(payload)
        tampered["bundle_digest"] = "sha256:" + "0" * 64
        assert bundle_digest(tampered) == bundle_digest(payload)

    def test_written_bundle_round_trips_its_digest(
        self, bundle: EvaluationEvidenceBundle, bundle_file: Path
    ) -> None:
        """🔴 写出去的字节与摘要覆盖的必须是同一份。"""
        on_disk = json.loads(bundle_file.read_text(encoding="utf-8"))
        assert bundle_digest(on_disk) == bundle.bundle_digest
        assert load_evidence_bundle(bundle_file) == bundle

    def test_changing_a_descriptor_changes_the_bundle_digest(self, bundle_file: Path) -> None:
        """🔴 改一个描述符 → **按内容重算**的摘要必须变，于是文件里那句
        旧摘要不再相符，加载被拒。"""
        before = bundle_digest(json.loads(bundle_file.read_text(encoding="utf-8")))
        _mutate_json(bundle_file, lambda p: p["artifacts"][0].__setitem__("byte_length", 1))
        after = bundle_digest(json.loads(bundle_file.read_text(encoding="utf-8")))
        assert before != after
        with pytest.raises(EvidenceInputError, match="bundle_digest 与证据包内容不符"):
            load_evidence_bundle(bundle_file)

    def test_changing_the_chain_identity_changes_the_bundle_digest(self, bundle_file: Path) -> None:
        before = bundle_digest(json.loads(bundle_file.read_text(encoding="utf-8")))
        _mutate_json(bundle_file, lambda p: p["chain_identity"].__setitem__("policy_revision", 9))
        after = bundle_digest(json.loads(bundle_file.read_text(encoding="utf-8")))
        assert before != after

    def test_wrong_bundle_digest_is_rejected(self, bundle_file: Path) -> None:
        _mutate_json(bundle_file, lambda p: p.__setitem__("bundle_digest", "sha256:" + "1" * 64))
        with pytest.raises(EvidenceInputError, match="bundle_digest 与证据包内容不符"):
            load_evidence_bundle(bundle_file)

    def test_bundle_has_no_path_or_timestamp_field(self, bundle_file: Path) -> None:
        text = bundle_file.read_text(encoding="utf-8")
        for banned in (
            "generated_at",
            "timestamp",
            "created_at",
            "mtime",
            "inode",
            "hostname",
            "username",
            "run_id",
            str(_REPO_ROOT),
            "\\\\",
        ):
            assert banned not in text, banned

    def test_semantic_identity_only_carries_its_own_role_fields(
        self, bundle: EvaluationEvidenceBundle
    ) -> None:
        """🔴 闭合形状：每个角色**只出现自己那一组**字段。

        一份满是 ``null`` 的身份记录读起来像"这些字段查过了、没有值"，
        而不是"这些字段不属于这个角色"。
        """
        payload = bundle_payload(bundle)
        baseline = payload["artifacts"][0]["semantic_identity"]
        assert set(baseline) == {
            "commit_sha",
            "dataset_digest",
            "assertion_registry_digest",
            "prompt_versions_digest",
            "provider_name",
            "model_id",
            "execution_mode",
            "storage_backend",
        }
        policy_identity = payload["artifacts"][3]["semantic_identity"]
        assert set(policy_identity) == {
            "policy_id",
            "policy_revision",
            "dataset_digest",
            "assertion_registry_digest",
        }


# ---------------------------------------------------------------------------
# B 组：角色
# ---------------------------------------------------------------------------


class TestArtifactRoles:
    """B 组：五种角色各一次、固定顺序、不自动交换。"""

    def test_five_roles_in_fixed_order(self, bundle: EvaluationEvidenceBundle) -> None:
        assert tuple(item.role for item in bundle.artifacts) == ARTIFACT_ROLE_ORDER
        assert len(ARTIFACT_ROLE_ORDER) == 5
        assert {role.value for role in ArtifactRole} == {role.value for role in ARTIFACT_ROLE_ORDER}

    def test_every_artifact_records_digest_and_length(
        self, bundle: EvaluationEvidenceBundle
    ) -> None:
        for item in bundle.artifacts:
            assert re.fullmatch(r"sha256:[0-9a-f]{64}", item.content_sha256)
            assert item.byte_length > 0

    def test_self_comparison_keeps_two_separate_roles(
        self, evidence_chain_factory: Any, tmp_path: Path
    ) -> None:
        """🔴 自比较：两边内容可以**逐字节相同**，角色仍必须分别存在，
        且仍由**显式参数**决定谁是基线。"""
        chain = evidence_chain_factory.write(tmp_path / "self", same_commit=True)
        assert chain.baseline_run.read_bytes() == chain.candidate_run.read_bytes()
        built = build_evidence_bundle(chain.inputs())
        roles = [item.role for item in built.artifacts]
        assert roles.count(ArtifactRole.BASELINE_RUN) == 1
        assert roles.count(ArtifactRole.CANDIDATE_RUN) == 1
        digests = {item.role: item.content_sha256 for item in built.artifacts}
        assert digests[ArtifactRole.BASELINE_RUN] == digests[ArtifactRole.CANDIDATE_RUN]

    def test_missing_role_is_reported_as_a_conflict(self, chain: Any, bundle_file: Path) -> None:
        _mutate_json(bundle_file, lambda p: p["artifacts"].pop(2))
        report = _verify(chain, bundle_file)
        assert report.verification_outcome is VerificationOutcome.INVALID
        assert "artifact_roles_complete" in report.failed_check_ids

    def test_duplicate_role_is_reported_as_a_conflict(self, chain: Any, bundle_file: Path) -> None:
        _mutate_json(bundle_file, lambda p: p["artifacts"].append(dict(p["artifacts"][0])))
        report = _verify(chain, bundle_file)
        assert report.verification_outcome is VerificationOutcome.INVALID
        assert "artifact_roles_complete" in report.failed_check_ids

    def test_wrong_order_is_reported_as_a_conflict(self, chain: Any, bundle_file: Path) -> None:
        """🔴 顺序变化**不得被静默接受**：它同时打掉 bundle_digest 与
        顺序检查两项。"""
        _mutate_json(bundle_file, lambda p: p["artifacts"].reverse())
        report = _verify(chain, bundle_file)
        assert report.verification_outcome is VerificationOutcome.INVALID
        assert "artifact_order_valid" in report.failed_check_ids
        assert "bundle_digest_valid" in report.failed_check_ids

    def test_swapping_the_two_runs_is_rejected_at_build(self, chain: Any, tmp_path: Path) -> None:
        """🔴 交换基线／候选后，重算的对比与输入对不上——构建必须失败，
        且**不得**自动换回来。"""
        from ai_psi.evaluation.evidence import EvidenceChainMismatchError, EvidenceInputs

        swapped = EvidenceInputs(
            baseline_run=chain.candidate_run,
            candidate_run=chain.baseline_run,
            comparison=chain.comparison,
            policy=chain.policy,
            gate_decision=chain.gate_decision,
        )
        with pytest.raises(EvidenceChainMismatchError, match="重算的对比与输入不一致"):
            build_evidence_bundle(swapped)

    def test_swapping_the_two_runs_is_reported_at_verify(
        self, chain: Any, bundle_file: Path
    ) -> None:
        from ai_psi.evaluation.evidence import EvidenceInputs, EvidenceVerificationInputs

        swapped = EvidenceInputs(
            baseline_run=chain.candidate_run,
            candidate_run=chain.baseline_run,
            comparison=chain.comparison,
            policy=chain.policy,
            gate_decision=chain.gate_decision,
        )
        report = verify_evidence_bundle(
            EvidenceVerificationInputs(bundle=bundle_file, artifacts=swapped)
        )
        assert report.verification_outcome is VerificationOutcome.INVALID
        assert "baseline_content_digest_valid" in report.failed_check_ids
        assert "candidate_content_digest_valid" in report.failed_check_ids
        assert "baseline_role_valid" in report.failed_check_ids

    def test_unknown_role_in_a_descriptor_is_rejected(
        self, bundle: EvaluationEvidenceBundle
    ) -> None:
        payload = bundle_payload(bundle)
        payload["artifacts"][0]["role"] = "SOMETHING_ELSE"
        with pytest.raises(ValueError, match="role"):
            parse_evidence_bundle(payload)

    def test_descriptor_rejects_a_foreign_identity_field(self) -> None:
        """给基线运行塞一个 ``policy_id`` → 构造失败。"""
        with pytest.raises(ValueError, match="语义身份字段不对"):
            ArtifactDescriptor.model_validate(
                {
                    "role": "BASELINE_RUN",
                    "content_sha256": "sha256:" + "a" * 64,
                    "byte_length": 10,
                    "schema_identity": {"schema_version": 1, "definition_digest": None},
                    "semantic_identity": {"commit_sha": "a" * 40, "policy_id": "x"},
                }
            )


# ---------------------------------------------------------------------------
# C 组：内容摘要
# ---------------------------------------------------------------------------


class TestArtifactContentDigest:
    """C 组：字节摘要与长度。"""

    def test_content_digest_matches_the_raw_bytes(
        self, chain: Any, bundle: EvaluationEvidenceBundle
    ) -> None:
        for item, (_, path) in zip(bundle.artifacts, chain.inputs().by_role(), strict=True):
            data = path.read_bytes()
            assert item.content_sha256 == "sha256:" + hashlib.sha256(data).hexdigest()
            assert item.byte_length == len(data)

    def test_content_digest_is_over_bytes_not_the_parsed_object(self, chain: Any) -> None:
        """🔴 同一个**解析后对象**，只要字节不同，摘要就必须不同。"""
        original = chain.comparison.read_bytes()
        payload = json.loads(original.decode("utf-8"))
        reformatted = json.dumps(payload, ensure_ascii=False, indent=4).encode("utf-8")
        assert json.loads(reformatted) == payload
        assert reformatted != original
        assert hashlib.sha256(reformatted).hexdigest() != hashlib.sha256(original).hexdigest()

    def test_reformatting_only_is_still_invalid(self, chain: Any, bundle_file: Path) -> None:
        """把对比产物**只改缩进**：语义没变，字节变了——结论仍是 INVALID。

        🔴 这正是"字节摘要"与"语义重算"必须**同时**保留的理由：只做后者
        会放过这类改动，只做前者会放过"格式没变但数值被改"。
        """
        text = json.loads(chain.comparison.read_text(encoding="utf-8"))
        chain.comparison.write_text(
            json.dumps(text, ensure_ascii=False, indent=4), encoding="utf-8", newline="\n"
        )
        report = _verify(chain, bundle_file)
        assert report.verification_outcome is VerificationOutcome.INVALID
        assert "comparison_content_digest_valid" in report.failed_check_ids
        assert "comparison_byte_length_valid" in report.failed_check_ids

    @pytest.mark.parametrize("role_index", [0, 1, 2, 3, 4])
    def test_one_changed_byte_is_invalid(
        self, chain: Any, bundle_file: Path, role_index: int
    ) -> None:
        """五个角色各改一个**等长**的字节：长度不变，摘要必须变。"""
        _, path = chain.inputs().by_role()[role_index]
        data = path.read_bytes()
        replacement = b"Z" if data.find(b"m") < 0 else b"z"
        index = next(i for i, byte in enumerate(data) if byte in (ord("m"), ord("z"), ord("0")))
        mutated = data[:index] + replacement + data[index + 1 :]
        assert len(mutated) == len(data)
        path.write_bytes(mutated)

        report = _verify(chain, bundle_file)
        assert report.verification_outcome is VerificationOutcome.INVALID
        assert report.failed_check_ids, "改了一个字节却没有任何检查报冲突"

    def test_trailing_newline_change_is_invalid(self, chain: Any, bundle_file: Path) -> None:
        chain.baseline_run.write_bytes(chain.baseline_run.read_bytes() + b"\n")
        report = _verify(chain, bundle_file)
        assert report.verification_outcome is VerificationOutcome.INVALID
        assert "baseline_content_digest_valid" in report.failed_check_ids
        assert "baseline_byte_length_valid" in report.failed_check_ids

    def test_empty_input_is_rejected_at_build(self, chain: Any) -> None:
        chain.baseline_run.write_bytes(b"")
        with pytest.raises(EvidenceInputError, match="空文件"):
            build_evidence_bundle(chain.inputs())

    def test_non_utf8_input_is_rejected_at_build(self, chain: Any) -> None:
        chain.policy.write_bytes(b"\xff\xfe\x00\x00 not utf-8")
        with pytest.raises(EvidenceInputError, match="UTF-8"):
            build_evidence_bundle(chain.inputs())

    def test_missing_input_is_rejected_at_build(self, chain: Any) -> None:
        chain.comparison.unlink()
        with pytest.raises(EvidenceInputError, match="COMPARISON 读取失败"):
            build_evidence_bundle(chain.inputs())


# ---------------------------------------------------------------------------
# 安全：产物里不得出现这些东西
# ---------------------------------------------------------------------------


class TestBundleSecurity:
    """L 组：secret、正文、路径、时间戳。"""

    def test_bundle_carries_no_secret_prose_or_path(
        self, bundle_file: Path, tmp_path: Path
    ) -> None:
        text = bundle_file.read_text(encoding="utf-8")
        for sentinel in (
            _RESPONSE_SENTINEL,
            "api_key",
            "Authorization",
            "password",
            "postgresql://",
            "postgresql+psycopg://",
            "Traceback",
            str(tmp_path),
            "failure_detail",
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
            assert sentinel not in text, sentinel

    def test_bundle_does_not_embed_the_input_files(self, bundle_file: Path, chain: Any) -> None:
        """🔴 不嵌入全文：Bundle 要比它记录的任何一份输入都小得多。"""
        assert bundle_file.stat().st_size < chain.comparison.stat().st_size

    def test_bundle_has_exactly_one_trailing_newline_and_no_crlf(self, bundle_file: Path) -> None:
        raw = bundle_file.read_bytes()
        assert b"\r\n" not in raw
        assert raw.endswith(b"\n")
        assert not raw.endswith(b"\n\n")

    def test_two_builds_are_byte_identical(self, chain: Any, tmp_path: Path) -> None:
        first = tmp_path / "one.json"
        second = tmp_path / "two.json"
        write_bundle(build_evidence_bundle(chain.inputs()), first)
        write_bundle(build_evidence_bundle(chain.inputs()), second)
        assert first.read_bytes() == second.read_bytes()

    def test_parse_requires_the_exact_top_level_field_set(
        self, bundle: EvaluationEvidenceBundle
    ) -> None:
        payload = bundle_payload(bundle)
        assert set(payload) == set(EVIDENCE_BUNDLE_TOP_LEVEL_FIELDS)
        payload["release_allowed"] = True
        with pytest.raises(EvidenceInputError, match="顶层字段"):
            parse_evidence_bundle(payload)

    def test_bundle_definition_digest_is_bound_to_the_schema_version(
        self, bundle: EvaluationEvidenceBundle
    ) -> None:
        assert bundle.evidence_bundle_definition_digest == evidence_definition_digest()
        assert bundle.evidence_bundle_schema_version == EVIDENCE_BUNDLE_SCHEMA_VERSION
