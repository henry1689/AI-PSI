"""资格判定命令行的行为、退出码与产物（阶段 8 · S8）。

对应任务书 §十七 的 F、G、H 组。

🔴 退出码本身就是**接口**：`0`/`1`/`4` 是三种**结论**，`2`/`3`/`5` 是三种
**故障**。把结论和故障混成一个码，自动化里就分不清"判过了，没资格"与
"压根没判成"。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import ai_psi.evaluation.qualification_cli as cli_module
from ai_psi.evaluation.qualification import (
    EvaluationQualificationOutcome,
    qualification_decision_digest,
)
from ai_psi.evaluation.qualification_cli import (
    EXIT_DISQUALIFIED,
    EXIT_INPUT_ERROR,
    EXIT_NOT_EVALUATED,
    EXIT_OK,
    EXIT_OUTPUT_ERROR,
    EXIT_USAGE_ERROR,
    build_parser,
    main,
)

pytestmark = pytest.mark.unit

#: 合成运行结果里的回答正文。它**不得**出现在任何 S8 产物里。
_RESPONSE_SENTINEL = "合成的回答文本"


@pytest.fixture
def chain(qualification_chain_factory: Any, tmp_path: Path) -> Any:
    return qualification_chain_factory.chain(tmp_path / "s8")


def _args(chain: Any, output: Path) -> list[str]:
    return [
        "--bundle",
        str(chain.bundle),
        "--baseline-run",
        str(chain.artifacts.baseline_run),
        "--candidate-run",
        str(chain.artifacts.candidate_run),
        "--comparison",
        str(chain.artifacts.comparison),
        "--gate-policy",
        str(chain.artifacts.policy),
        "--gate-decision",
        str(chain.artifacts.gate_decision),
        "--qualification-policy",
        str(chain.qualification_policy),
        "--output",
        str(output),
    ]


def _read(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


# ---------------------------------------------------------------------------
# F 组：退出码
# ---------------------------------------------------------------------------


class TestExitCodes:
    """F 组：六种退出码与它们的界。"""

    def test_qualified_returns_zero_and_writes_a_decision(self, chain: Any, tmp_path: Path) -> None:
        output = tmp_path / "d.json"
        assert main(_args(chain, output)) == EXIT_OK
        payload = _read(output)
        assert payload["qualification_outcome"] == "QUALIFIED"
        assert payload["verification_outcome"] == "VERIFIED"
        assert payload["identity"]["gate"]["gate_outcome"] == "PASS"

    def test_a_gate_fail_returns_disqualified(
        self, qualification_chain_factory: Any, tmp_path: Path
    ) -> None:
        """🔴 ``1`` 是 ``DISQUALIFIED`` 而**不是**"出错"。"""
        chain = qualification_chain_factory.chain(
            tmp_path / "fail", candidate_outcomes={"case-001": "pass", "case-002": "fail"}
        )
        output = tmp_path / "d.json"
        assert main(_args(chain, output)) == EXIT_DISQUALIFIED
        payload = _read(output)
        assert payload["qualification_outcome"] == "DISQUALIFIED"
        assert payload["verification_outcome"] == "VERIFIED"
        assert payload["identity"]["gate"]["gate_outcome"] == "FAIL"

    def test_a_gate_not_evaluated_returns_not_evaluated(
        self, qualification_chain_factory: Any, tmp_path: Path
    ) -> None:
        chain = qualification_chain_factory.chain(
            tmp_path / "gne", policy_scope={"provider": "另一个 provider"}
        )
        output = tmp_path / "d.json"
        assert main(_args(chain, output)) == EXIT_NOT_EVALUATED
        payload = _read(output)
        assert payload["qualification_outcome"] == "NOT_EVALUATED"
        assert payload["verification_outcome"] == "VERIFIED"

    def test_invalid_evidence_returns_disqualified(self, chain: Any, tmp_path: Path) -> None:
        """🔴 证据链有确定冲突 ⇒ ``1``，**不是**输入解析失败。"""
        chain.artifacts.baseline_run.write_bytes(chain.artifacts.baseline_run.read_bytes() + b" ")
        output = tmp_path / "d.json"
        assert main(_args(chain, output)) == EXIT_DISQUALIFIED
        payload = _read(output)
        assert payload["verification_outcome"] == "INVALID"
        assert payload["qualification_outcome"] == "DISQUALIFIED"

    def test_not_verifiable_evidence_returns_not_evaluated(
        self, chain: Any, tmp_path: Path
    ) -> None:
        chain.artifacts.comparison.unlink()
        output = tmp_path / "d.json"
        assert main(_args(chain, output)) == EXIT_NOT_EVALUATED
        payload = _read(output)
        assert payload["verification_outcome"] == "NOT_VERIFIABLE"
        assert payload["qualification_outcome"] == "NOT_EVALUATED"

    def test_an_inapplicable_policy_returns_not_evaluated(
        self, qualification_chain_factory: Any, tmp_path: Path
    ) -> None:
        chain = qualification_chain_factory.chain(
            tmp_path / "na", qualification_scope={"provider": "openai"}
        )
        output = tmp_path / "d.json"
        assert main(_args(chain, output)) == EXIT_NOT_EVALUATED
        payload = _read(output)
        assert payload["policy_applicability"]["status"] == "NOT_APPLICABLE"
        assert payload["qualification_outcome"] == "NOT_EVALUATED"

    def test_missing_argument_exits_with_usage_code(self) -> None:
        with pytest.raises(SystemExit) as info:
            main(["--bundle", "x.json"])
        assert info.value.code == EXIT_USAGE_ERROR

    def test_no_arguments_exits_with_usage_code(self) -> None:
        with pytest.raises(SystemExit) as info:
            main([])
        assert info.value.code == EXIT_USAGE_ERROR

    def test_a_broken_qualification_policy_returns_input_error(
        self, chain: Any, tmp_path: Path
    ) -> None:
        chain.qualification_policy.write_text("{ not json", encoding="utf-8", newline="\n")
        output = tmp_path / "d.json"
        assert main(_args(chain, output)) == EXIT_INPUT_ERROR
        assert not output.exists()

    def test_a_bundle_without_an_envelope_returns_input_error(
        self, chain: Any, tmp_path: Path
    ) -> None:
        chain.bundle.write_text("{ not json", encoding="utf-8", newline="\n")
        output = tmp_path / "d.json"
        assert main(_args(chain, output)) == EXIT_INPUT_ERROR
        assert not output.exists()

    def test_a_missing_bundle_returns_input_error(self, chain: Any, tmp_path: Path) -> None:
        chain.bundle.unlink()
        output = tmp_path / "d.json"
        assert main(_args(chain, output)) == EXIT_INPUT_ERROR
        assert not output.exists()

    def test_output_failure_returns_output_code(self, chain: Any, tmp_path: Path) -> None:
        """🔴 写盘失败**不得**沿用结论的退出码：那会让"没写出来"看起来像
        "判完了"。"""
        directory = tmp_path / "a-directory"
        directory.mkdir()
        assert main(_args(chain, directory)) == EXIT_OUTPUT_ERROR

    @pytest.mark.parametrize(
        ("label", "setup"),
        [
            ("QUALIFIED", "none"),
            ("DISQUALIFIED", "gate_fail"),
        ],
    )
    def test_every_business_outcome_writes_a_decision(
        self, qualification_chain_factory: Any, tmp_path: Path, label: str, setup: str
    ) -> None:
        chain = qualification_chain_factory.chain(
            tmp_path / f"w-{label}",
            candidate_outcomes=(
                {"case-001": "pass", "case-002": "fail"} if setup == "gate_fail" else None
            ),
        )
        output = tmp_path / "d.json"
        main(_args(chain, output))
        assert output.is_file()
        assert _read(output)["qualification_outcome"] == label

    def test_no_partial_file_is_left_behind(self, chain: Any, tmp_path: Path) -> None:
        """失败的运行**不产生部分产物**。"""
        chain.qualification_policy.write_text("{ not json", encoding="utf-8", newline="\n")
        output = tmp_path / "d.json"
        assert main(_args(chain, output)) == EXIT_INPUT_ERROR
        assert not output.exists()
        assert list(tmp_path.glob(".*tmp*")) == []

    def test_an_existing_decision_is_untouched_by_a_failing_run(
        self, chain: Any, tmp_path: Path
    ) -> None:
        output = tmp_path / "d.json"
        assert main(_args(chain, output)) == EXIT_OK
        before = output.read_bytes()
        chain.qualification_policy.write_text("{ not json", encoding="utf-8", newline="\n")
        assert main(_args(chain, output)) == EXIT_INPUT_ERROR
        assert output.read_bytes() == before

    def test_the_cli_never_modifies_its_inputs(self, chain: Any, tmp_path: Path) -> None:
        before = {name: path.read_bytes() for name, path in chain.by_role().items()}
        main(_args(chain, tmp_path / "d.json"))
        after = {name: path.read_bytes() for name, path in chain.by_role().items()}
        assert before == after


# ---------------------------------------------------------------------------
# G 组：确定性
# ---------------------------------------------------------------------------


class TestCliOutput:
    """G 组：产物层面的确定性与安全。"""

    def test_two_runs_are_byte_identical(self, chain: Any, tmp_path: Path) -> None:
        first = tmp_path / "d1.json"
        second = tmp_path / "d2.json"
        assert main(_args(chain, first)) == main(_args(chain, second)) == EXIT_OK
        assert first.read_bytes() == second.read_bytes()

    def test_a_disqualified_decision_is_also_deterministic(
        self, qualification_chain_factory: Any, tmp_path: Path
    ) -> None:
        chain = qualification_chain_factory.chain(
            tmp_path / "det", candidate_outcomes={"case-001": "pass", "case-002": "fail"}
        )
        first = tmp_path / "d1.json"
        second = tmp_path / "d2.json"
        assert main(_args(chain, first)) == EXIT_DISQUALIFIED
        assert main(_args(chain, second)) == EXIT_DISQUALIFIED
        assert first.read_bytes() == second.read_bytes()

    def test_the_decision_ends_with_exactly_one_newline_and_no_crlf(
        self, chain: Any, tmp_path: Path
    ) -> None:
        output = tmp_path / "d.json"
        main(_args(chain, output))
        raw = output.read_bytes()
        assert b"\r\n" not in raw
        assert raw.endswith(b"\n")
        assert not raw.endswith(b"\n\n")

    def test_the_decision_digest_is_recomputable(self, chain: Any, tmp_path: Path) -> None:
        output = tmp_path / "d.json"
        main(_args(chain, output))
        payload = _read(output)
        assert payload["qualification_decision_digest"] == qualification_decision_digest(payload)

    def test_a_tampered_decision_fails_to_load(self, chain: Any, tmp_path: Path) -> None:
        from ai_psi.evaluation.qualification import (
            QualificationInputError,
            load_qualification_decision,
        )

        output = tmp_path / "d.json"
        main(_args(chain, output))
        payload = _read(output)
        payload["checks"][0]["observed"] = "被人改过"
        output.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        with pytest.raises(QualificationInputError, match="qualification_decision_digest"):
            load_qualification_decision(output)

    def test_the_decision_carries_no_secret_prose_or_path(self, chain: Any, tmp_path: Path) -> None:
        output = tmp_path / "d.json"
        main(_args(chain, output))
        text = output.read_text(encoding="utf-8")
        for sentinel in (
            _RESPONSE_SENTINEL,
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
            "run_id",
            str(tmp_path),
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
        ):
            assert sentinel not in text, sentinel

    def test_terminal_output_survives_a_legacy_codepage(
        self, chain: Any, tmp_path: Path, capsys: Any
    ) -> None:
        """🔴 终端输出必须能在 **GBK** 控制台上编码。

        S6、S7 都实测踩过：一句提示里的 emoji 在 GBK 控制台上抛
        ``UnicodeEncodeError``，而那个异常把进程退出码顶成了 ``1``——
        一条结论被一个终端编码问题顶成了故障码。这里对**每条分支**都测。
        """
        main(_args(chain, tmp_path / "d.json"))
        captured = capsys.readouterr()
        captured.out.encode("gbk")
        captured.err.encode("gbk")

        chain.artifacts.baseline_run.write_bytes(chain.artifacts.baseline_run.read_bytes() + b" ")
        main(_args(chain, tmp_path / "d2.json"))
        capsys.readouterr().out.encode("gbk")

        chain.qualification_policy.write_text("{ not json", encoding="utf-8", newline="\n")
        main(_args(chain, tmp_path / "d3.json"))
        capsys.readouterr().err.encode("gbk")

    def test_terminal_output_claims_no_release_decision(
        self, chain: Any, tmp_path: Path, capsys: Any
    ) -> None:
        main(_args(chain, tmp_path / "d.json"))
        printed = capsys.readouterr().out
        for forbidden in ("允许发布", "可以发布", "建议上线", "可以合并", "release_allowed"):
            assert forbidden not in printed, forbidden
        assert "QUALIFIED" in printed


class TestCliSurface:
    """命令行表面：没有绕过开关，不碰外部世界。"""

    def test_the_parser_offers_no_bypass_or_release_switches(self) -> None:
        """🔴 **没有**任何"让它在不该给资格的时候还是给了"的开关。"""
        options = {action.dest for action in build_parser()._actions}
        forbidden = {
            "force",
            "trust_verification",
            "skip_verification",
            "ignore_policy_scope",
            "allow_invalid_evidence",
            "release",
            "deploy",
            "verification_report",
            "unsafe",
            "allow_inapplicable",
        }
        assert options & forbidden == set()
        assert {
            "bundle",
            "baseline_run",
            "candidate_run",
            "comparison",
            "gate_policy",
            "gate_decision",
            "qualification_policy",
            "output",
        } <= options

    def test_all_eight_arguments_are_required(self) -> None:
        parser = build_parser()
        required = {action.dest for action in parser._actions if getattr(action, "required", False)}
        assert required == {
            "bundle",
            "baseline_run",
            "candidate_run",
            "comparison",
            "gate_policy",
            "gate_decision",
            "qualification_policy",
            "output",
        }

    def test_the_cli_has_no_verification_report_input(self) -> None:
        """🔴 **没有这个入口**——不是"不推荐传"，是传不进来。"""
        source = Path(str(cli_module.__file__)).read_text(encoding="utf-8")
        assert "--verification-report" in source  # 只在文档里说明"没有它"
        options = {action.dest for action in build_parser()._actions}
        assert "verification_report" not in options

    def test_module_reaches_no_network_provider_or_database(self) -> None:
        """🔴 源码级断言：任何一次"顺手加个 fetch"都会让它变红。"""
        source = Path(str(cli_module.__file__)).read_text(encoding="utf-8")
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
        source = Path(str(cli_module.__file__)).read_text(encoding="utf-8")
        assert 'if __name__ == "__main__"' in source

    def test_the_module_never_runs_an_evaluation(self) -> None:
        """不自动运行评测、不生成 S5／S6／S7 产物。"""
        source = Path(str(cli_module.__file__)).read_text(encoding="utf-8")
        for banned in (
            "compare_run_results",
            "build_evidence_bundle",
            "ai_psi.evaluation.runner",
            "ai_psi.evaluation.cli",
        ):
            assert banned not in source, banned


def test_the_exit_codes_are_distinct() -> None:
    codes = {
        EXIT_OK,
        EXIT_DISQUALIFIED,
        EXIT_USAGE_ERROR,
        EXIT_INPUT_ERROR,
        EXIT_NOT_EVALUATED,
        EXIT_OUTPUT_ERROR,
    }
    assert len(codes) == 6


def test_the_outcome_enum_is_closed() -> None:
    assert {item.value for item in EvaluationQualificationOutcome} == {
        "QUALIFIED",
        "DISQUALIFIED",
        "NOT_EVALUATED",
    }
