"""门禁命令行的行为与退出码（阶段 7 · S6）。

对应任务书 §二十一 的 K 组。

🔴 退出码本身就是**接口**：``0``/``1``/``4`` 是三种**结论**，
``2``/``3``/``5`` 是三种**故障**。把结论和故障混成一个码，
自动化里就没法区分"判过了，没通过"与"压根没判成"。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import ai_psi.evaluation.gate_cli as gate_cli_module
from ai_psi.evaluation.comparison import EvaluationComparison, write_comparison
from ai_psi.evaluation.gate import GateOutcome, policy_digest
from ai_psi.evaluation.gate_cli import (
    EXIT_FAIL,
    EXIT_INPUT_ERROR,
    EXIT_NOT_EVALUATED,
    EXIT_OK,
    EXIT_OUTPUT_ERROR,
    EXIT_USAGE_ERROR,
    build_parser,
    main,
)

pytestmark = pytest.mark.unit


def _write_comparison(comparison: EvaluationComparison, tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    write_comparison(comparison, path)
    return path


def _write_policy(payload: dict[str, Any], tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _run(tmp_path: Path, comparison: Path, policy: Path, out_name: str = "decision.json") -> Any:
    from ai_psi.evaluation.gate_cli import main as cli_main

    output = tmp_path / out_name
    code = cli_main(
        [
            "--comparison",
            str(comparison),
            "--policy",
            str(policy),
            "--decision-output",
            str(output),
        ]
    )
    return code, output


class TestExitCodes:
    """K 组：六种退出码。"""

    def test_pass_returns_zero_and_writes_a_decision(
        self, gate_factory: Any, tmp_path: Path
    ) -> None:
        comparison = gate_factory.comparison({"case-001": "pass", "case-002": "pass"})
        code, output = _run(
            tmp_path,
            _write_comparison(comparison, tmp_path, "comparison.json"),
            _write_policy(gate_factory.policy_payload(comparison), tmp_path, "policy.json"),
        )
        assert code == EXIT_OK
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert payload["outcome"] == GateOutcome.PASS.value
        assert payload["policy_applicable"] is True
        assert payload["failed_rule_ids"] == []
        assert payload["not_evaluated_rule_ids"] == []

    def test_fail_returns_one_and_writes_a_decision(
        self, gate_factory: Any, tmp_path: Path
    ) -> None:
        """🔴 ``1`` 是 ``FAIL`` 而**不是**"出错"：这个工具就是门禁，
        "规则明确没通过"是它的一种正常结论。"""
        comparison = gate_factory.comparison_between(
            {"case-001": "pass", "case-002": "pass"},
            {"case-001": "pass", "case-002": "fail"},
        )
        code, output = _run(
            tmp_path,
            _write_comparison(comparison, tmp_path, "comparison.json"),
            _write_policy(gate_factory.policy_payload(comparison), tmp_path, "policy.json"),
        )
        assert code == EXIT_FAIL
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert payload["outcome"] == GateOutcome.FAIL.value
        assert payload["policy_applicable"] is True
        assert payload["failed_rule_ids"]
        assert "no_case_regressions" in payload["failed_rule_ids"]

    def test_not_evaluated_returns_four_and_writes_a_decision(
        self, gate_factory: Any, tmp_path: Path
    ) -> None:
        comparison = gate_factory.comparison({"case-001": "pass"})
        payload = gate_factory.policy_payload(comparison)
        payload["scope"]["provider"] = "openai"
        payload["policy_digest"] = policy_digest(payload)
        code, output = _run(
            tmp_path,
            _write_comparison(comparison, tmp_path, "comparison.json"),
            _write_policy(payload, tmp_path, "policy.json"),
        )
        assert code == EXIT_NOT_EVALUATED
        written = json.loads(output.read_text(encoding="utf-8"))
        assert written["outcome"] == GateOutcome.NOT_EVALUATED.value
        assert written["policy_applicable"] is False
        assert "provider_out_of_scope" in written["applicability_reasons"]
        # 🔴 不适用时一条质量规则都不评估。
        assert written["rule_results"] == []

    def test_missing_arguments_exit_with_usage_code(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as info:
            main(["--comparison", str(tmp_path / "a.json")])
        assert info.value.code == EXIT_USAGE_ERROR

    def test_missing_input_file_returns_usage_code(self, gate_factory: Any, tmp_path: Path) -> None:
        """文件不存在是**用法**问题（2），不是"产物坏了"（3）。"""
        comparison = gate_factory.comparison({"case-001": "pass"})
        policy_path = _write_policy(
            gate_factory.policy_payload(comparison), tmp_path, "policy.json"
        )
        code, output = _run(tmp_path, tmp_path / "nope.json", policy_path)
        assert code == EXIT_USAGE_ERROR
        assert not output.exists()

    def test_broken_policy_returns_input_error_and_writes_nothing(
        self, gate_factory: Any, tmp_path: Path
    ) -> None:
        """🔴 策略解析失败**不产生伪 GateDecision**。

        把一份坏策略判成 ``FAIL``，等于拿使用者的策略错误去指控候选。
        """
        comparison = gate_factory.comparison({"case-001": "pass"})
        broken = tmp_path / "policy.json"
        broken.write_text('{"policy_id": ', encoding="utf-8", newline="\n")
        code, output = _run(
            tmp_path,
            _write_comparison(comparison, tmp_path, "comparison.json"),
            broken,
        )
        assert code == EXIT_INPUT_ERROR
        assert not output.exists()

    def test_broken_comparison_returns_input_error_and_writes_nothing(
        self, gate_factory: Any, tmp_path: Path
    ) -> None:
        comparison = gate_factory.comparison({"case-001": "pass"})
        broken = tmp_path / "comparison.json"
        broken.write_text("{ not json", encoding="utf-8", newline="\n")
        code, output = _run(
            tmp_path,
            broken,
            _write_policy(gate_factory.policy_payload(comparison), tmp_path, "policy.json"),
        )
        assert code == EXIT_INPUT_ERROR
        assert not output.exists()

    def test_stale_policy_digest_returns_input_error(
        self, gate_factory: Any, tmp_path: Path
    ) -> None:
        comparison = gate_factory.comparison({"case-001": "pass"})
        payload = gate_factory.policy_payload(comparison)
        payload["rules"][0]["expected"] = 9
        code, output = _run(
            tmp_path,
            _write_comparison(comparison, tmp_path, "comparison.json"),
            _write_policy(payload, tmp_path, "policy.json"),
        )
        assert code == EXIT_INPUT_ERROR
        assert not output.exists()

    def test_output_failure_returns_output_code(self, gate_factory: Any, tmp_path: Path) -> None:
        """🔴 写盘失败**不得**沿用 outcome 的退出码：那会让"没写出来"
        看起来像"判完了"。"""
        comparison = gate_factory.comparison({"case-001": "pass"})
        directory = tmp_path / "a-directory"
        directory.mkdir()
        code = main(
            [
                "--comparison",
                str(_write_comparison(comparison, tmp_path, "comparison.json")),
                "--policy",
                str(
                    _write_policy(gate_factory.policy_payload(comparison), tmp_path, "policy.json")
                ),
                "--decision-output",
                str(directory),
            ]
        )
        assert code == EXIT_OUTPUT_ERROR


class TestCliSurface:
    """命令行表面：没有绕过开关，不碰外部世界。"""

    def test_parser_offers_no_bypass_switches(self) -> None:
        """🔴 **没有**任何"让它在不该判的时候还是判了"的开关。"""
        options = {action.dest for action in build_parser()._actions}
        forbidden = {
            "force",
            "unsafe",
            "ignore_scope",
            "allow_unknown_version",
            "skip_policy_digest",
            "allow_inapplicable",
        }
        assert options & forbidden == set()
        assert {"comparison", "policy", "decision_output"} <= options

    def test_all_three_arguments_are_required(self) -> None:
        parser = build_parser()
        required = {action.dest for action in parser._actions if getattr(action, "required", False)}
        assert required == {"comparison", "policy", "decision_output"}

    def test_module_reaches_no_network_provider_or_database(self) -> None:
        """🔴 门禁 CLI 不读网络、不调 Provider、不连数据库——

        这条用**源码级**断言钉住：任何一次"顺手加个 fetch"都会让它变红。
        """
        source = Path(gate_cli_module.__file__).read_text(encoding="utf-8")
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
        source = Path(gate_cli_module.__file__).read_text(encoding="utf-8")
        assert 'if __name__ == "__main__"' in source


class TestCliOutput:
    """产物层面的确定性与安全。"""

    def test_two_runs_are_byte_identical(self, gate_factory: Any, tmp_path: Path) -> None:
        comparison = gate_factory.comparison_between(
            {"case-001": "pass", "case-002": "pass"},
            {"case-001": "pass", "case-002": "fail"},
        )
        comparison_path = _write_comparison(comparison, tmp_path, "comparison.json")
        policy_path = _write_policy(
            gate_factory.policy_payload(comparison), tmp_path, "policy.json"
        )
        first_code, first = _run(tmp_path, comparison_path, policy_path, "one.json")
        second_code, second = _run(tmp_path, comparison_path, policy_path, "two.json")
        assert first_code == second_code == EXIT_FAIL
        assert first.read_bytes() == second.read_bytes()

    def test_decision_ends_with_exactly_one_newline_and_no_crlf(
        self, gate_factory: Any, tmp_path: Path
    ) -> None:
        comparison = gate_factory.comparison({"case-001": "pass"})
        _, output = _run(
            tmp_path,
            _write_comparison(comparison, tmp_path, "comparison.json"),
            _write_policy(gate_factory.policy_payload(comparison), tmp_path, "policy.json"),
        )
        raw = output.read_bytes()
        assert b"\r\n" not in raw
        assert raw.endswith(b"\n")
        assert not raw.endswith(b"\n\n")

    def test_decision_carries_no_secret_prose_or_path(
        self, gate_factory: Any, tmp_path: Path
    ) -> None:
        """用**哨兵值**确认产物里没有秘密、没有正文、没有路径、没有时间戳。"""
        comparison = gate_factory.comparison_between(
            {"case-001": "pass", "case-002": "pass"},
            {"case-001": "pass", "case-002": "fail"},
        )
        _, output = _run(
            tmp_path,
            _write_comparison(comparison, tmp_path, "comparison.json"),
            _write_policy(gate_factory.policy_payload(comparison), tmp_path, "policy.json"),
        )
        text = output.read_text(encoding="utf-8")
        for sentinel in (
            "api_key",
            "Authorization",
            "password",
            "postgresql://",
            "postgresql+psycopg://",
            "response_text",
            "合成的回答文本",
            "合成：终态是 failed",
            "Traceback",
            str(tmp_path),
            "generated_at",
            "timestamp",
            "release_allowed",
            "deploy_allowed",
            "merge_allowed",
            "production_ready",
            "quality_score",
            "risk_score",
            "recommendation",
        ):
            assert sentinel not in text, sentinel

    def test_inputs_are_not_modified(self, gate_factory: Any, tmp_path: Path) -> None:
        comparison = gate_factory.comparison({"case-001": "pass"})
        comparison_path = _write_comparison(comparison, tmp_path, "comparison.json")
        policy_path = _write_policy(
            gate_factory.policy_payload(comparison), tmp_path, "policy.json"
        )
        before = (comparison_path.read_bytes(), policy_path.read_bytes())
        _run(tmp_path, comparison_path, policy_path)
        assert (comparison_path.read_bytes(), policy_path.read_bytes()) == before

    def test_output_parent_directory_is_created(self, gate_factory: Any, tmp_path: Path) -> None:
        comparison = gate_factory.comparison({"case-001": "pass"})
        output = tmp_path / "nested" / "deeper" / "decision.json"
        code = main(
            [
                "--comparison",
                str(_write_comparison(comparison, tmp_path, "comparison.json")),
                "--policy",
                str(
                    _write_policy(gate_factory.policy_payload(comparison), tmp_path, "policy.json")
                ),
                "--decision-output",
                str(output),
            ]
        )
        assert code == EXIT_OK
        assert output.is_file()

    def test_terminal_output_claims_no_release_decision(
        self, gate_factory: Any, tmp_path: Path, capsys: Any
    ) -> None:
        """终端输出里也**不得**出现"可以发布/建议上线"这类判断。"""
        comparison = gate_factory.comparison({"case-001": "pass"})
        _run(
            tmp_path,
            _write_comparison(comparison, tmp_path, "comparison.json"),
            _write_policy(gate_factory.policy_payload(comparison), tmp_path, "policy.json"),
        )
        printed = capsys.readouterr().out
        for forbidden in ("允许发布", "可以发布", "建议上线", "release_allowed", "质量分"):
            assert forbidden not in printed, forbidden
        assert "PASS" in printed
