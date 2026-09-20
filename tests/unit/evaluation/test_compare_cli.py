"""对比命令行的行为与退出码（阶段 7 · S5）。

对应任务书 §二十六 的 N 组。

🔴 退出码本身就是**接口**：CI 要能区分"输入读不了""两份结果不能比"
与"比完了但有差异"。全都返回 1 的 CLI 在自动化里等于没有结论。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ai_psi.evaluation.compare_cli import (
    EXIT_INCOMPATIBLE,
    EXIT_INPUT_SCHEMA_ERROR,
    EXIT_OK,
    EXIT_OUTPUT_ERROR,
    EXIT_USAGE_ERROR,
    build_parser,
    main,
)
from ai_psi.evaluation.serialization import raw_payload, write_reports

pytestmark = pytest.mark.unit

SHA_A = "a" * 40
SHA_B = "b" * 40


def _write(result: Any, tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    write_reports(result, raw_path=path, canonical_path=tmp_path / f"{name}.canonical")
    return path


def test_successful_comparison_returns_zero(comparison_factory: Any, tmp_path: Path) -> None:
    dataset = comparison_factory.dataset(["case-001", "case-002"])
    baseline = comparison_factory.run(
        dataset, {"case-001": "pass", "case-002": "pass"}, commit_sha=SHA_A
    )
    candidate = comparison_factory.run(
        dataset, {"case-001": "pass", "case-002": "fail"}, commit_sha=SHA_B
    )
    output = tmp_path / "comparison.json"
    code = main(
        [
            "--baseline",
            str(_write(baseline, tmp_path, "baseline.json")),
            "--candidate",
            str(_write(candidate, tmp_path, "candidate.json")),
            "--comparison-output",
            str(output),
        ]
    )
    assert code == EXIT_OK
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["comparison_eligible"] is True
    assert payload["allowed_differences"] == ["code_revision_differs"]
    assert payload["blockers"] == []
    assert payload["baseline"]["commit_sha"] == SHA_A
    assert payload["candidate"]["commit_sha"] == SHA_B


def test_ineligible_comparison_returns_dedicated_code_and_writes_diagnostics(
    comparison_factory: Any, tmp_path: Path
) -> None:
    """🔴 合法但不可比较：**写出**诊断，返回专用非零。

    诊断里的"为什么不能比"是证据；而五组数值差异必须全为空。
    """
    dataset = comparison_factory.dataset(["case-001"])
    baseline = comparison_factory.run(dataset, {"case-001": "pass"})
    candidate = comparison_factory.patch(baseline, "evaluation", dataset_digest="sha256:other")
    output = tmp_path / "comparison.json"
    code = main(
        [
            "--baseline",
            str(_write(baseline, tmp_path, "baseline.json")),
            "--candidate",
            str(_write(candidate, tmp_path, "candidate.json")),
            "--comparison-output",
            str(output),
        ]
    )
    assert code == EXIT_INCOMPATIBLE
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["comparison_eligible"] is False
    assert "dataset_differs" in payload["blockers"]
    assert payload["metrics_comparison"] is None
    assert payload["case_transitions"] is None
    assert payload["assertion_transitions"] is None
    assert payload["distribution_deltas"] is None
    assert payload["failure_index_delta"] is None


def test_unparsable_input_returns_schema_code_and_writes_nothing(
    comparison_factory: Any, tmp_path: Path
) -> None:
    """🔴 读不成结构的输入**不产生** Comparison JSON。

    一份基于未解析输入的"对比结果"无论长什么样都是误导——
    而"解析失败"尤其不能被静默当成"没有差异"。
    """
    dataset = comparison_factory.dataset(["case-001"])
    baseline = comparison_factory.run(dataset, {"case-001": "pass"})
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json", encoding="utf-8", newline="\n")
    output = tmp_path / "comparison.json"
    code = main(
        [
            "--baseline",
            str(_write(baseline, tmp_path, "baseline.json")),
            "--candidate",
            str(broken),
            "--comparison-output",
            str(output),
        ]
    )
    assert code == EXIT_INPUT_SCHEMA_ERROR
    assert not output.exists()


def test_missing_input_file_returns_usage_code(comparison_factory: Any, tmp_path: Path) -> None:
    """文件不存在是**用法**问题（2），不是"结果坏了"（3）。

    把两者混成一个码，会让 CI 上的"路径写错了"看起来像"评测结果损坏"。
    """
    dataset = comparison_factory.dataset(["case-001"])
    baseline = comparison_factory.run(dataset, {"case-001": "pass"})
    output = tmp_path / "comparison.json"
    code = main(
        [
            "--baseline",
            str(_write(baseline, tmp_path, "baseline.json")),
            "--candidate",
            str(tmp_path / "nope.json"),
            "--comparison-output",
            str(output),
        ]
    )
    assert code == EXIT_USAGE_ERROR
    assert not output.exists()


def test_missing_arguments_exit_with_usage_code(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as info:
        main(["--baseline", str(tmp_path / "a.json")])
    assert info.value.code == EXIT_USAGE_ERROR


def test_same_file_as_both_sides_is_allowed(comparison_factory: Any, tmp_path: Path) -> None:
    """两份输入相同是**合法**的：用于验证重复运行的稳定性。"""
    dataset = comparison_factory.dataset(["case-001", "case-002"])
    result = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
    path = _write(result, tmp_path, "run.json")
    output = tmp_path / "comparison.json"
    code = main(
        [
            "--baseline",
            str(path),
            "--candidate",
            str(path),
            "--comparison-output",
            str(output),
        ]
    )
    assert code == EXIT_OK
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["comparison_eligible"] is True
    assert payload["metrics_comparison"]["cases"]["passed_cases"]["delta"] == 0
    assert payload["case_transition_summary"]["unchanged_pass_count"] == 2
    assert payload["assertion_transition_summary"]["unchanged_count"] == 4


def test_roles_are_taken_from_argument_order_not_file_names(
    comparison_factory: Any, tmp_path: Path
) -> None:
    """🔴 文件名里的字样**不参与**角色判定。

    这里故意把"候选"的那份命名为 ``baseline.json``，把"基线"的命名为
    ``candidate.json``——脚本必须按参数顺序理解角色，否则它是在猜。
    """
    dataset = comparison_factory.dataset(["case-001"])
    baseline = comparison_factory.run(dataset, {"case-001": "pass"}, commit_sha=SHA_A)
    candidate = comparison_factory.run(dataset, {"case-001": "fail"}, commit_sha=SHA_B)
    misleading_baseline = _write(baseline, tmp_path, "baseline.json")
    misleading_candidate = _write(candidate, tmp_path, "candidate.json")
    output = tmp_path / "comparison.json"
    code = main(
        [
            "--baseline",
            str(misleading_baseline),
            "--candidate",
            str(misleading_candidate),
            "--comparison-output",
            str(output),
        ]
    )
    assert code == EXIT_OK
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["baseline"]["commit_sha"] == SHA_A
    assert payload["candidate"]["commit_sha"] == SHA_B
    assert payload["case_transition_summary"]["regression_transition_count"] == 1


def test_output_failure_returns_output_code(comparison_factory: Any, tmp_path: Path) -> None:
    dataset = comparison_factory.dataset(["case-001"])
    result = comparison_factory.run(dataset, {"case-001": "pass"})
    path = _write(result, tmp_path, "run.json")
    directory = tmp_path / "a-directory"
    directory.mkdir()
    code = main(
        [
            "--baseline",
            str(path),
            "--candidate",
            str(path),
            "--comparison-output",
            str(directory),
        ]
    )
    assert code == EXIT_OUTPUT_ERROR


def test_output_parent_directory_is_created(comparison_factory: Any, tmp_path: Path) -> None:
    dataset = comparison_factory.dataset(["case-001"])
    result = comparison_factory.run(dataset, {"case-001": "pass"})
    path = _write(result, tmp_path, "run.json")
    output = tmp_path / "nested" / "deeper" / "comparison.json"
    code = main(
        [
            "--baseline",
            str(path),
            "--candidate",
            str(path),
            "--comparison-output",
            str(output),
        ]
    )
    assert code == EXIT_OK
    assert output.is_file()


def test_parser_offers_no_bypass_switches() -> None:
    """🔴 **没有**任何"让它在不该比的时候还是比了"的开关。

    任何这类参数都会把本切片唯一的价值——差异可信——变成可选项。
    """
    parser = build_parser()
    # 检查**全部**参数（含位置参数与内置的 --help）——绕过开关可能藏在这里。
    options = {action.dest for action in parser._actions}
    forbidden = {
        "force",
        "unsafe",
        "ignore_dataset_difference",
        "ignore_prompt_difference",
        "allow_dirty",
        "skip_integrity",
        "allow_incomparable",
    }
    assert options & forbidden == set()
    assert {"baseline", "candidate", "comparison_output"} <= options


def test_inputs_are_not_modified(comparison_factory: Any, tmp_path: Path) -> None:
    dataset = comparison_factory.dataset(["case-001"])
    baseline = comparison_factory.run(dataset, {"case-001": "pass"}, commit_sha=SHA_A)
    candidate = comparison_factory.run(dataset, {"case-001": "fail"}, commit_sha=SHA_B)
    left = _write(baseline, tmp_path, "baseline.json")
    right = _write(candidate, tmp_path, "candidate.json")
    before = (left.read_bytes(), right.read_bytes())
    main(
        [
            "--baseline",
            str(left),
            "--candidate",
            str(right),
            "--comparison-output",
            str(tmp_path / "comparison.json"),
        ]
    )
    assert (left.read_bytes(), right.read_bytes()) == before


def test_comparison_file_is_byte_identical_across_runs(
    comparison_factory: Any, tmp_path: Path
) -> None:
    dataset = comparison_factory.dataset(["case-001", "case-002"])
    baseline = comparison_factory.run(
        dataset, {"case-001": "pass", "case-002": "fail"}, commit_sha=SHA_A
    )
    candidate = comparison_factory.run(
        dataset, {"case-001": "fail", "case-002": "pass"}, commit_sha=SHA_B
    )
    left = _write(baseline, tmp_path, "baseline.json")
    right = _write(candidate, tmp_path, "candidate.json")
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    for target in (first, second):
        main(
            [
                "--baseline",
                str(left),
                "--candidate",
                str(right),
                "--comparison-output",
                str(target),
            ]
        )
    assert first.read_bytes() == second.read_bytes()


def test_comparison_json_carries_no_secret_or_prose(
    comparison_factory: Any, tmp_path: Path
) -> None:
    """用**哨兵值**确认产物里没有秘密、没有正文、没有路径。"""
    dataset = comparison_factory.dataset(["case-001"])
    baseline = comparison_factory.run(dataset, {"case-001": "pass"}, commit_sha=SHA_A)
    candidate = comparison_factory.run(dataset, {"case-001": "fail"}, commit_sha=SHA_B)
    output = tmp_path / "comparison.json"
    main(
        [
            "--baseline",
            str(_write(baseline, tmp_path, "baseline.json")),
            "--candidate",
            str(_write(candidate, tmp_path, "candidate.json")),
            "--comparison-output",
            str(output),
        ]
    )
    text = output.read_text(encoding="utf-8")
    for sentinel in (
        "synthetic-eval-pw",  # 数据库密码哨兵
        "postgresql://",
        "postgresql+psycopg://",
        "Authorization",
        "api_key",
        "合成的回答文本",  # 回答正文哨兵
        "合成：终态是 failed",  # 断言判定原文（detail）哨兵
        "Traceback",
        str(tmp_path),  # 绝对路径哨兵
    ):
        assert sentinel not in text, sentinel


def test_report_does_not_claim_a_release_decision(
    comparison_factory: Any, tmp_path: Path, capsys: Any
) -> None:
    """终端输出里也**不得**出现"通过/可以发布"这类结论。"""
    dataset = comparison_factory.dataset(["case-001"])
    baseline = comparison_factory.run(dataset, {"case-001": "pass"}, commit_sha=SHA_A)
    candidate = comparison_factory.run(dataset, {"case-001": "fail"}, commit_sha=SHA_B)
    main(
        [
            "--baseline",
            str(_write(baseline, tmp_path, "baseline.json")),
            "--candidate",
            str(_write(candidate, tmp_path, "candidate.json")),
            "--comparison-output",
            str(tmp_path / "comparison.json"),
        ]
    )
    printed = capsys.readouterr().out
    for forbidden in ("release_allowed", "gate_passed", "允许发布", "可以发布", "质量分"):
        assert forbidden not in printed, forbidden
    # 方向词是**纯描述**的，允许出现。
    assert "candidate" in printed


def test_raw_payload_round_trip_is_what_the_cli_compares(
    comparison_factory: Any, tmp_path: Path
) -> None:
    """命令行读的必须是**原始**结果——canonical 会被明确拒绝。"""
    dataset = comparison_factory.dataset(["case-001"])
    result = comparison_factory.run(dataset, {"case-001": "pass"})
    write_reports(
        result, raw_path=tmp_path / "raw.json", canonical_path=tmp_path / "canonical.json"
    )
    assert set(raw_payload(result)) == set(json.loads((tmp_path / "raw.json").read_text("utf-8")))
    code = main(
        [
            "--baseline",
            str(tmp_path / "canonical.json"),
            "--candidate",
            str(tmp_path / "raw.json"),
            "--comparison-output",
            str(tmp_path / "comparison.json"),
        ]
    )
    assert code == EXIT_INPUT_SCHEMA_ERROR
