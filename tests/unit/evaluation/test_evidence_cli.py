"""证据包命令行的行为、退出码与产物（阶段 7 · S7）。

对应任务书 §二十 的 I、J、K 组。

🔴 退出码本身就是**接口**：`0`/`1`/`3`/`4` 是四种**结论**，`2`/`5` 是两种
**故障**。把结论和故障混成一个码，自动化里就分不清"验过了，是坏的"与
"压根没验成"。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

import ai_psi.evaluation.evidence_cli as cli_module
from ai_psi.evaluation.evidence import build_evidence_bundle, write_bundle
from ai_psi.evaluation.evidence_cli import (
    EXIT_CHAIN_MISMATCH,
    EXIT_INPUT_ERROR,
    EXIT_INVALID,
    EXIT_NOT_VERIFIABLE,
    EXIT_OK,
    EXIT_OUTPUT_ERROR,
    EXIT_USAGE_ERROR,
    build_parser,
    main,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def chain(evidence_chain_factory: Any, tmp_path: Path) -> Any:
    return evidence_chain_factory.write(tmp_path / "chain")


@pytest.fixture
def bundle_file(chain: Any, tmp_path: Path) -> Path:
    path = tmp_path / "bundle.json"
    write_bundle(build_evidence_bundle(chain.inputs()), path)
    return path


def _build_args(chain: Any, output: Path) -> list[str]:
    return [
        "build",
        "--baseline-run",
        str(chain.baseline_run),
        "--candidate-run",
        str(chain.candidate_run),
        "--comparison",
        str(chain.comparison),
        "--policy",
        str(chain.policy),
        "--gate-decision",
        str(chain.gate_decision),
        "--bundle-output",
        str(output),
    ]


def _verify_args(chain: Any, bundle_path: Path, output: Path) -> list[str]:
    return [
        "verify",
        "--bundle",
        str(bundle_path),
        "--baseline-run",
        str(chain.baseline_run),
        "--candidate-run",
        str(chain.candidate_run),
        "--comparison",
        str(chain.comparison),
        "--policy",
        str(chain.policy),
        "--gate-decision",
        str(chain.gate_decision),
        "--verification-output",
        str(output),
    ]


def _subcommands() -> dict[str, argparse.ArgumentParser]:
    """`build` / `verify` 两个子解析器。

    ⚠️ 从 ``argparse`` 的内部结构里取：这两个用例要问的正是"**参数表面**上
    有没有绕过开关"，而不是"某个函数有没有返回它们"。
    """
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("没有子命令")


def _read(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def _mutate(path: Path, mutate: Any) -> None:
    payload = _read(path)
    mutate(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


# ---------------------------------------------------------------------------
# I 组：build
# ---------------------------------------------------------------------------


class TestBuildCli:
    """I 组：构建的退出码与产物。"""

    def test_success_returns_zero_and_writes_a_bundle(self, chain: Any, tmp_path: Path) -> None:
        output = tmp_path / "bundle.json"
        assert main(_build_args(chain, output)) == EXIT_OK
        payload = _read(output)
        assert payload["evidence_bundle_schema_version"] == 1
        assert len(payload["artifacts"]) == 5

    def test_missing_argument_exits_with_usage_code(self) -> None:
        with pytest.raises(SystemExit) as info:
            main(["build", "--baseline-run", "x.json"])
        assert info.value.code == EXIT_USAGE_ERROR

    def test_missing_subcommand_exits_with_usage_code(self) -> None:
        with pytest.raises(SystemExit) as info:
            main([])
        assert info.value.code == EXIT_USAGE_ERROR

    def test_missing_input_file_returns_usage_code(self, chain: Any, tmp_path: Path) -> None:
        """🔴 对 ``build`` 而言，"某个路径指不到文件"是**用法**问题（2），
        不是"这条链不成立"——它要问的正是"按你给的这五份，能不能立一条链"。"""
        chain.policy.unlink()
        output = tmp_path / "bundle.json"
        assert main(_build_args(chain, output)) == EXIT_USAGE_ERROR
        assert not output.exists()

    def test_broken_input_returns_input_error_and_writes_nothing(
        self, chain: Any, tmp_path: Path
    ) -> None:
        chain.comparison.write_text("{ not json", encoding="utf-8", newline="\n")
        output = tmp_path / "bundle.json"
        assert main(_build_args(chain, output)) == EXIT_INPUT_ERROR
        assert not output.exists()

    def test_inconsistent_chain_returns_mismatch_code_and_writes_nothing(
        self, chain: Any, tmp_path: Path
    ) -> None:
        _mutate(chain.comparison, lambda p: p.__setitem__("allowed_differences", []))
        output = tmp_path / "bundle.json"
        assert main(_build_args(chain, output)) == EXIT_CHAIN_MISMATCH
        assert not output.exists()

    def test_output_failure_returns_output_code(self, chain: Any, tmp_path: Path) -> None:
        directory = tmp_path / "a-directory"
        directory.mkdir()
        assert main(_build_args(chain, directory)) == EXIT_OUTPUT_ERROR

    def test_a_failing_gate_still_builds(self, evidence_chain_factory: Any, tmp_path: Path) -> None:
        """🔴 **门禁判 FAIL 不影响构建成功**：链路自洽就够了。"""
        chain = evidence_chain_factory.write(
            tmp_path / "fail", candidate_outcomes={"case-001": "pass", "case-002": "fail"}
        )
        output = tmp_path / "bundle.json"
        assert main(_build_args(chain, output)) == EXIT_OK
        assert _read(output)["chain_identity"]["gate_outcome"] == "FAIL"

    def test_a_not_evaluated_gate_still_builds(
        self, evidence_chain_factory: Any, tmp_path: Path
    ) -> None:
        chain = evidence_chain_factory.write(tmp_path / "ne", policy_scope={"provider": "other"})
        output = tmp_path / "bundle.json"
        assert main(_build_args(chain, output)) == EXIT_OK
        assert _read(output)["chain_identity"]["gate_outcome"] == "NOT_EVALUATED"

    def test_a_failed_build_leaves_the_previous_bundle_untouched(
        self, chain: Any, tmp_path: Path
    ) -> None:
        """🔴 不产生部分产物：失败的构建不得动到已有的那份。"""
        output = tmp_path / "bundle.json"
        assert main(_build_args(chain, output)) == EXIT_OK
        before = output.read_bytes()

        _mutate(chain.comparison, lambda p: p.__setitem__("allowed_differences", []))
        assert main(_build_args(chain, output)) == EXIT_CHAIN_MISMATCH
        assert output.read_bytes() == before

    def test_build_creates_the_parent_directory(self, chain: Any, tmp_path: Path) -> None:
        output = tmp_path / "nested" / "deeper" / "bundle.json"
        assert main(_build_args(chain, output)) == EXIT_OK
        assert output.is_file()

    def test_build_does_not_modify_the_inputs(self, chain: Any, tmp_path: Path) -> None:
        before = {path: path.read_bytes() for _, path in chain.inputs().by_role()}
        main(_build_args(chain, tmp_path / "bundle.json"))
        assert {path: path.read_bytes() for _, path in chain.inputs().by_role()} == before


# ---------------------------------------------------------------------------
# J 组：verify
# ---------------------------------------------------------------------------


class TestVerifyCli:
    """J 组：验证的退出码与报告。"""

    def test_verified_returns_zero(self, chain: Any, bundle_file: Path, tmp_path: Path) -> None:
        output = tmp_path / "verification.json"
        assert main(_verify_args(chain, bundle_file, output)) == EXIT_OK
        payload = _read(output)
        assert payload["verification_outcome"] == "VERIFIED"
        assert payload["gate_outcome"] == "PASS"

    def test_gate_fail_and_verified_returns_zero(
        self, evidence_chain_factory: Any, tmp_path: Path
    ) -> None:
        """🔴 ``gate_outcome=FAIL`` + ``verification_outcome=VERIFIED``
        **必须**返回 0。门禁结论不参与本次核验的结论。"""
        chain = evidence_chain_factory.write(
            tmp_path / "fail", candidate_outcomes={"case-001": "pass", "case-002": "fail"}
        )
        bundle_path = tmp_path / "bundle.json"
        write_bundle(build_evidence_bundle(chain.inputs()), bundle_path)
        output = tmp_path / "verification.json"

        assert main(_verify_args(chain, bundle_path, output)) == EXIT_OK
        payload = _read(output)
        assert payload["gate_outcome"] == "FAIL"
        assert payload["verification_outcome"] == "VERIFIED"

    def test_invalid_returns_one_and_writes_a_report(
        self, chain: Any, bundle_file: Path, tmp_path: Path
    ) -> None:
        chain.baseline_run.write_bytes(chain.baseline_run.read_bytes() + b"\n")
        output = tmp_path / "verification.json"
        assert main(_verify_args(chain, bundle_file, output)) == EXIT_INVALID
        payload = _read(output)
        assert payload["verification_outcome"] == "INVALID"
        assert payload["failed_check_ids"]

    def test_unparsable_bundle_returns_input_error_code(
        self, chain: Any, bundle_file: Path, tmp_path: Path
    ) -> None:
        """🔴 "连形状都读不出来"是 ``3``，与"证据不足"（``4``）分开。"""
        bundle_file.write_text("{ not json", encoding="utf-8", newline="\n")
        output = tmp_path / "verification.json"
        assert main(_verify_args(chain, bundle_file, output)) == EXIT_INPUT_ERROR
        payload = _read(output)
        assert payload["verification_outcome"] == "NOT_VERIFIABLE"

    def test_missing_input_returns_not_verifiable_code(
        self, chain: Any, bundle_file: Path, tmp_path: Path
    ) -> None:
        chain.policy.unlink()
        output = tmp_path / "verification.json"
        assert main(_verify_args(chain, bundle_file, output)) == EXIT_NOT_VERIFIABLE
        payload = _read(output)
        assert payload["verification_outcome"] == "NOT_VERIFIABLE"
        assert payload["failed_check_ids"] == []

    def test_unsupported_version_returns_not_verifiable_code(
        self, chain: Any, bundle_file: Path, tmp_path: Path
    ) -> None:
        _mutate(chain.comparison, lambda p: p.__setitem__("comparison_schema_version", 2))
        chain.refresh_descriptors(bundle_file)
        output = tmp_path / "verification.json"
        assert main(_verify_args(chain, bundle_file, output)) == EXIT_NOT_VERIFIABLE
        assert _read(output)["verification_outcome"] == "NOT_VERIFIABLE"

    def test_missing_argument_exits_with_usage_code(self) -> None:
        with pytest.raises(SystemExit) as info:
            main(["verify", "--bundle", "x.json"])
        assert info.value.code == EXIT_USAGE_ERROR

    def test_output_failure_returns_output_code(
        self, chain: Any, bundle_file: Path, tmp_path: Path
    ) -> None:
        directory = tmp_path / "a-directory"
        directory.mkdir()
        assert main(_verify_args(chain, bundle_file, directory)) == EXIT_OUTPUT_ERROR

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda p: p["artifacts"].pop(0),
            lambda p: p["artifacts"].insert(0, dict(p["artifacts"][0])),
            lambda p: p["artifacts"].reverse(),
            lambda p: p.__setitem__("bundle_digest", "sha256:" + "5" * 64),
            lambda p: p["chain_identity"].__setitem__("policy_revision", 77),
            lambda p: p["artifacts"][0].__setitem__("content_sha256", "sha256:" + "8" * 64),
        ],
    )
    def test_never_emits_a_fake_verified(
        self, chain: Any, bundle_file: Path, tmp_path: Path, mutate: Any
    ) -> None:
        """🔴 无论证据包坏成什么样，都不得出现 ``VERIFIED``。"""
        _mutate(bundle_file, mutate)
        output = tmp_path / "verification.json"
        assert main(_verify_args(chain, bundle_file, output)) == EXIT_INVALID
        assert _read(output)["verification_outcome"] == "INVALID"

    def test_verify_does_not_modify_the_bundle_or_inputs(
        self, chain: Any, bundle_file: Path, tmp_path: Path
    ) -> None:
        before_bundle = bundle_file.read_bytes()
        before = {path: path.read_bytes() for _, path in chain.inputs().by_role()}
        main(_verify_args(chain, bundle_file, tmp_path / "v.json"))
        assert bundle_file.read_bytes() == before_bundle
        assert {path: path.read_bytes() for _, path in chain.inputs().by_role()} == before


# ---------------------------------------------------------------------------
# K 组：确定性与安全
# ---------------------------------------------------------------------------


class TestCliOutput:
    """K 组：产物层面的确定性与安全。"""

    def test_two_builds_are_byte_identical(self, chain: Any, tmp_path: Path) -> None:
        first = tmp_path / "one.json"
        second = tmp_path / "two.json"
        main(_build_args(chain, first))
        main(_build_args(chain, second))
        assert first.read_bytes() == second.read_bytes()

    def test_two_reports_are_byte_identical(
        self, chain: Any, bundle_file: Path, tmp_path: Path
    ) -> None:
        first = tmp_path / "v1.json"
        second = tmp_path / "v2.json"
        main(_verify_args(chain, bundle_file, first))
        main(_verify_args(chain, bundle_file, second))
        assert first.read_bytes() == second.read_bytes()

    def test_outputs_end_with_exactly_one_newline_and_no_crlf(
        self, chain: Any, bundle_file: Path, tmp_path: Path
    ) -> None:
        bundle_out = tmp_path / "b.json"
        report_out = tmp_path / "v.json"
        main(_build_args(chain, bundle_out))
        main(_verify_args(chain, bundle_file, report_out))
        for path in (bundle_out, report_out):
            raw = path.read_bytes()
            assert b"\r\n" not in raw, path
            assert raw.endswith(b"\n"), path
            assert not raw.endswith(b"\n\n"), path

    def test_outputs_carry_no_secret_volatile_or_path_content(
        self, chain: Any, bundle_file: Path, tmp_path: Path
    ) -> None:
        bundle_out = tmp_path / "b.json"
        report_out = tmp_path / "v.json"
        main(_build_args(chain, bundle_out))
        main(_verify_args(chain, bundle_file, report_out))
        for path in (bundle_out, report_out):
            text = path.read_text(encoding="utf-8")
            for sentinel in (
                "合成的回答文本",
                "api_key",
                "Authorization",
                "password",
                "postgresql://",
                "postgresql+psycopg://",
                "response_text",
                "failure_detail",
                "Traceback",
                "generated_at",
                "timestamp",
                "release_allowed",
                "deploy_allowed",
                "merge_allowed",
                "production_ready",
                "quality_score",
                "risk_score",
                "recommendation",
                str(tmp_path),
            ):
                assert sentinel not in text, (path.name, sentinel)

    def test_terminal_output_survives_a_legacy_codepage(
        self, chain: Any, bundle_file: Path, tmp_path: Path, capsys: Any
    ) -> None:
        """🔴 终端输出必须能在 **GBK** 控制台上编码。

        S6 实测踩过：一句提示里的 emoji 在 GBK 控制台上抛
        ``UnicodeEncodeError``，而那个异常把进程退出码顶成了 ``1``——
        一条结论被一个终端编码问题顶成了故障码。这里对**每一条分支**都测。
        """
        bundle_out = tmp_path / "b.json"
        main(_build_args(chain, bundle_out))
        capsys.readouterr().out.encode("gbk")

        chain.baseline_run.write_bytes(chain.baseline_run.read_bytes() + b"\n")
        _mutate(chain.comparison, lambda p: p.__setitem__("allowed_differences", []))
        main(_build_args(chain, tmp_path / "bad.json"))
        capsys.readouterr().err.encode("gbk")

        for bundle_path, name in ((bundle_file, "ok"), (tmp_path / "missing.json", "missing")):
            main(_verify_args(chain, bundle_path, tmp_path / f"v-{name}.json"))
            captured = capsys.readouterr()
            captured.out.encode("gbk")
            captured.err.encode("gbk")

    def test_terminal_output_claims_no_release_decision(
        self, chain: Any, bundle_file: Path, tmp_path: Path, capsys: Any
    ) -> None:
        main(_verify_args(chain, bundle_file, tmp_path / "v.json"))
        printed = capsys.readouterr().out
        for forbidden in ("允许发布", "可以发布", "建议上线", "release_allowed", "质量分"):
            assert forbidden not in printed, forbidden
        assert "VERIFIED" in printed


class TestCliSurface:
    """命令行表面：没有绕过开关，不碰外部世界。"""

    def test_parser_offers_no_bypass_switches(self) -> None:
        """🔴 **没有**任何"让它在不该信的时候还是信了"的开关。"""
        options: set[str] = set()
        for subparser in _subcommands().values():
            options |= {action.dest for action in subparser._actions}
        forbidden = {
            "force",
            "skip_recompute",
            "trust_digests",
            "ignore_role",
            "allow_unknown_version",
            "unsafe",
            "ignore_scope",
            "allow_inapplicable",
        }
        assert options & forbidden == set()
        assert {"baseline_run", "candidate_run", "comparison", "policy", "gate_decision"} <= options

    def test_all_artifact_arguments_are_required(self) -> None:
        subcommands = _subcommands()
        assert set(subcommands) == {"build", "verify"}
        for subparser in subcommands.values():
            required = {
                action.dest for action in subparser._actions if getattr(action, "required", False)
            }
            assert {
                "baseline_run",
                "candidate_run",
                "comparison",
                "policy",
                "gate_decision",
            } <= required

    def test_module_reaches_no_network_provider_or_database(self) -> None:
        """🔴 源码级断言：任何一次"顺手加个 fetch"都会让它变红。"""
        source = Path(cli_module.__file__).read_text(encoding="utf-8")
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

    def test_module_has_a_main_guard(self) -> None:
        source = Path(cli_module.__file__).read_text(encoding="utf-8")
        assert 'if __name__ == "__main__"' in source
