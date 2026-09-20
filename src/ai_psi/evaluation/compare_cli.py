"""Baseline / Candidate 对比的命令行入口（阶段 7 · S5）::

    uv run python -m ai_psi.evaluation.compare_cli \
        --baseline evals/reports/s5-source-run-1.json \
        --candidate evals/reports/s5-source-run-2.json \
        --comparison-output evals/reports/s5-comparison-1.json

## 为什么是独立的模块入口

``ai_psi.evaluation.cli`` 是**扁平参数**结构（``--mode`` + 一串 ``--xxx``），
不是 subcommand。为一个新操作把全部既有命令改写成 subparser，
是拿 S1a—S4 的兼容性去换一点命令行美观——本切片不做这件事。
因此 S5 有自己的入口，``cli.py`` **一行未改**。

## 角色是显式参数，不是猜出来的

* ``--baseline`` 与 ``--candidate`` **都必填**；
* 两个路径**相同**是合法的（用于重复运行稳定性验证）；
* 脚本**不会**因为没有 ``candidate`` 就把它当成 ``baseline``，
  也不会按文件名里的 ``baseline`` / ``candidate`` 字样推断角色；
* 方向恒为 ``candidate - baseline``。

## 退出码

===== ==================================================================
``0`` 比较完成（**不代表任何一方更好**）
``2`` 参数或输入文件错误（缺参数、文件不存在）
``3`` 输入 Schema / 完整性错误——**JSON 损坏、字段不认识、清单或指标缺失**。
      🔴 此时**不写** Comparison JSON：基于未解析输入的"对比结果"是误导
``4`` 比较条件不兼容——输入本身合法，但存在阻塞性身份差异。
      诊断 Comparison JSON **会**写出（那是"为什么不能比"的证据）
``5`` 输出写入失败
===== ==================================================================

🔴 **``0`` 不是"通过"。** 它只说明"两份结果按规则比完了"——
差异属于谁、要不要发布，本工具不回答，也没有字段能回答。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

from ai_psi.evaluation.comparison import (
    ComparisonInputError,
    EvaluationComparison,
    compare_run_results,
    load_run_result,
    write_comparison,
)
from ai_psi.evaluation.runner import RunResult

__all__ = [
    "EXIT_INCOMPATIBLE",
    "EXIT_INPUT_SCHEMA_ERROR",
    "EXIT_OK",
    "EXIT_OUTPUT_ERROR",
    "EXIT_USAGE_ERROR",
    "build_parser",
    "main",
]

EXIT_OK: Final[int] = 0
#: 参数或输入文件错误。与 argparse 自己的用法错误退出码一致。
EXIT_USAGE_ERROR: Final[int] = 2
#: 🔴 与 ``EXIT_INCOMPATIBLE`` 分开：那说明"两份结果不能比"，
#: 这说"这份结果根本读不成一个结构"——前者的输入是合法的，后者不是。
EXIT_INPUT_SCHEMA_ERROR: Final[int] = 3
EXIT_INCOMPATIBLE: Final[int] = 4
EXIT_OUTPUT_ERROR: Final[int] = 5

_DEFAULT_COMPARISON_OUTPUT: Final[str] = "evals/reports/comparison.json"


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器。

    ⚠️ 这里**没有** ``--force``、``--ignore-dataset-difference``、
    ``--allow-dirty`` 这类开关，也不会有：任何"让它在不该比的时候还是比了"
    的参数，都会把本切片唯一的价值——差异可信——变成可选项。
    """
    parser = argparse.ArgumentParser(
        prog="python -m ai_psi.evaluation.compare_cli",
        description="比较两份评测结果（Baseline / Candidate，阶段 7 · S5）",
    )
    parser.add_argument(
        "--baseline",
        required=True,
        help="基线结果（**原始**评测结果 JSON，含完整清单与指标）",
    )
    parser.add_argument(
        "--candidate",
        required=True,
        help="候选结果（同上）。与 --baseline 传同一个文件是合法的",
    )
    parser.add_argument(
        "--comparison-output",
        default=_DEFAULT_COMPARISON_OUTPUT,
        help=f"对比结果输出路径（默认 {_DEFAULT_COMPARISON_OUTPUT}）",
    )
    return parser


def _load(path_text: str, role: str) -> tuple[RunResult | None, int]:
    """按角色加载一份结果。

    Returns:
        ``(结果, 退出码)``；成功时退出码为 :data:`EXIT_OK`。
    """
    path = Path(path_text)
    if not path.is_file():
        # 🔴 "文件不存在"是**用法**问题（2），不是 schema 问题（3）：
        # 把两者混成一个码，会让 CI 上的"路径写错了"看起来像"评测结果坏了"。
        print(f"{role}结果文件不存在：{path}", file=sys.stderr)
        return None, EXIT_USAGE_ERROR
    try:
        return load_run_result(path), EXIT_OK
    except ComparisonInputError as exc:
        print(f"{role}结果不可用：{path}", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        return None, EXIT_INPUT_SCHEMA_ERROR


def _report(comparison: EvaluationComparison) -> None:
    """把结论打到终端。

    🔴 这里**不打印**任何"更好/更差/可以发布"的判断。可比较时打印的是
    差异本身（含方向词 ``increased`` / ``decreased``，那是纯描述）；
    不可比较时只打印阻塞原因。
    """
    print(f"baseline ：{comparison.baseline.commit_sha}")
    print(f"candidate：{comparison.candidate.commit_sha}")
    print(f"身份契约一致：{comparison.manifest_identity_equal}")
    print(f"可比较：{comparison.comparison_eligible}")
    if comparison.allowed_differences:
        print(f"允许的差异：{'、'.join(comparison.allowed_differences)}")
    if comparison.blockers:
        print("阻塞项：")
        for blocker in comparison.blockers:
            print(f"  - {blocker}")
    if comparison.integrity_notes:
        print("未独立复核的项：")
        for note in comparison.integrity_notes:
            print(f"  - {note}")
    if comparison.integrity_mismatches:
        print("重算与存储不一致的项：")
        for mismatch in comparison.integrity_mismatches:
            print(f"  - {mismatch}")

    summary = comparison.case_transition_summary
    if summary is not None:
        print(
            f"案例：{summary.total_cases} 条"
            f"（通过→通过 {summary.unchanged_pass_count}"
            f"、通过→未通过 {summary.regression_transition_count}"
            f"、未通过→通过 {summary.improvement_transition_count}"
            f"、未通过→未通过 {summary.unchanged_fail_count}）"
        )
    assertion_summary = comparison.assertion_transition_summary
    if assertion_summary is not None:
        print(
            f"断言：{assertion_summary.total_assertions} 条"
            f"（不变 {assertion_summary.unchanged_count}"
            f"、转差 {assertion_summary.regression_transition_count}"
            f"、转好 {assertion_summary.improvement_transition_count}"
            f"、转为未决 {assertion_summary.changed_unresolved_count}）"
        )


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 参数列表；``None`` 时取 ``sys.argv[1:]``。

    Returns:
        退出码，见模块文档。
    """
    arguments = build_parser().parse_args(argv)

    baseline, code = _load(arguments.baseline, "基线")
    if baseline is None:
        return code
    candidate, code = _load(arguments.candidate, "候选")
    if candidate is None:
        return code

    # 🔴 到这里两份输入都已经通过严格结构校验。角色由参数位置显式给出：
    # 第一个是基线，第二个是候选。**不自动交换。**
    comparison = compare_run_results(baseline, candidate)

    try:
        write_comparison(comparison, Path(arguments.comparison_output))
    except OSError as exc:
        print(f"对比结果写盘失败：{type(exc).__name__}", file=sys.stderr)
        return EXIT_OUTPUT_ERROR

    _report(comparison)
    print(f"对比结果：{arguments.comparison_output}")
    return EXIT_OK if comparison.comparison_eligible else EXIT_INCOMPATIBLE


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
