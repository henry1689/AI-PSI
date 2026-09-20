"""质量门禁的命令行入口（阶段 7 · S6）::

    uv run python -m ai_psi.evaluation.gate_cli \
        --comparison evals/reports/s5-comparison-1.json \
        --policy evals/policies/s6_mock_golden_v1.json \
        --decision-output evals/reports/s6-decision-1.json

## 它做什么

读一份 S5 对比产物、读一份版本化策略、判一个结构化结论，写出来。

## 🔴 它不做什么

不运行评测、不运行 ``compare``、不调用 Provider、不连数据库、不读网络、
不部署、不合并、不发布、不打标签。三个参数全部必填——脚本**不会**去
"找最新的那份"，也**不会**按文件名猜哪份策略配哪份对比。

## 退出码

===== ==================================================================
``0`` ``outcome=PASS``
``1`` ``outcome=FAIL``
``2`` 参数或输入文件错误（缺参数、文件不存在）
``3`` Comparison 或 Policy 的 Schema／完整性错误——**不写** GateDecision
``4`` ``outcome=NOT_EVALUATED``（不可比较、策略不适用、证据缺失…）
``5`` 输出写入失败
===== ==================================================================

🔴 **``1`` 是 ``FAIL`` 而不是"出错"**：这个工具**就是**门禁，
"规则明确没通过"是它的一种正常结论，不是异常。

🔴 **``0`` 不是发布许可。** 它只说明"Candidate 没违反当前这份策略对
当前这个固定评测作用域定义的规则"。产物里没有任何字段能回答
"能不能上生产"——那不是本工具的问题。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

from ai_psi.evaluation.comparison import (
    ComparisonInputError,
    EvaluationComparison,
    load_comparison,
)
from ai_psi.evaluation.gate import (
    GateDecision,
    GateOutcome,
    GatePolicy,
    PolicyInputError,
    decide,
    load_gate_policy,
    write_decision,
)

__all__ = [
    "EXIT_FAIL",
    "EXIT_INPUT_ERROR",
    "EXIT_NOT_EVALUATED",
    "EXIT_OK",
    "EXIT_OUTPUT_ERROR",
    "EXIT_USAGE_ERROR",
    "build_parser",
    "main",
]

EXIT_OK: Final[int] = 0
EXIT_FAIL: Final[int] = 1
EXIT_USAGE_ERROR: Final[int] = 2
EXIT_INPUT_ERROR: Final[int] = 3
EXIT_NOT_EVALUATED: Final[int] = 4
EXIT_OUTPUT_ERROR: Final[int] = 5


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器。

    ⚠️ 这里**没有** ``--force``、``--ignore-scope``、
    ``--allow-unknown-version`` 这类开关，也不会有：任何"让它在不该判的
    时候还是判了"的参数，都会把这份结论变成可以商量的东西。
    """
    parser = argparse.ArgumentParser(
        prog="python -m ai_psi.evaluation.gate_cli",
        description="对一份 S5 对比产物按版本化策略做出门禁结论（阶段 7 · S6）",
    )
    parser.add_argument(
        "--comparison",
        required=True,
        help="S5 对比产物 JSON（**不是**评测结果，也不是 canonical 结果）",
    )
    parser.add_argument(
        "--policy",
        required=True,
        help="版本化 GatePolicy JSON。角色由参数决定，不按文件名推断",
    )
    parser.add_argument(
        "--decision-output",
        required=True,
        help="GateDecision 输出路径（原子写入：要么旧内容，要么完整的新内容）",
    )
    return parser


def _load_comparison(path_text: str) -> tuple[EvaluationComparison | None, int]:
    """加载对比产物。"""
    path = Path(path_text)
    if not path.is_file():
        # 🔴 "文件不存在"是**用法**问题（2），不是"产物坏了"（3）。
        print(f"对比产物文件不存在：{path}", file=sys.stderr)
        return None, EXIT_USAGE_ERROR
    try:
        return load_comparison(path), EXIT_OK
    except ComparisonInputError as exc:
        print(f"对比产物不可用：{path}", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        return None, EXIT_INPUT_ERROR


def _load_policy(path_text: str) -> tuple[GatePolicy | None, int]:
    """加载策略。"""
    path = Path(path_text)
    if not path.is_file():
        print(f"策略文件不存在：{path}", file=sys.stderr)
        return None, EXIT_USAGE_ERROR
    try:
        return load_gate_policy(path), EXIT_OK
    except PolicyInputError as exc:
        print(f"策略不可用：{path}", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        return None, EXIT_INPUT_ERROR


def _report(decision: GateDecision, comparison_path: str, policy_path: str) -> None:
    """把结论打到终端。

    🔴 这里**不打印**任何"可以发布/建议上线"之类的判断。打印的是结论本身、
    规则明细，以及在没判成 PASS/FAIL 时说清楚**为什么没判**。
    """
    identity = decision.identity
    print(f"对比产物：{comparison_path}")
    print(f"策略    ：{policy_path}")
    print(f"  policy_id={identity.policy_id}  revision={identity.policy_revision}")
    print(f"  baseline ={identity.baseline_commit_sha}")
    print(f"  candidate={identity.candidate_commit_sha}")
    print(f"策略适用：{decision.policy_applicable}")
    if decision.applicability_reasons:
        print("适用性原因：")
        for reason in decision.applicability_reasons:
            print(f"  - {reason}")

    outcome = decision.outcome
    print(f"结论    ：{outcome}")
    if outcome is GateOutcome.NOT_EVALUATED:
        # ⚠️ 这句话很重要：NOT_EVALUATED 既不是「没通过」，也不是「通过」。
        #
        # 🔴 终端输出**只用 GBK 可编码的字符**（中文、ASCII、全角标点），
        # **不用 emoji**：在 Windows 的 GBK 控制台上打印一个 ``⚠`` 会抛
        # ``UnicodeEncodeError``，而那个异常会把进程退出码变成 1 ——
        # 于是一条 NOT_EVALUATED 的**结论**被一个**终端编码问题**顶成了
        # 故障退出码。实测踩过：11 个不适用场景全部返回 1 而不是 4。
        print("  这不是「没通过」，也不是「通过」——这次没能完整地判。")

    for result in decision.rule_results:
        print(
            f"  [{result.outcome}] {result.rule_id}（{result.evidence_key}，{result.reason_code}）"
        )
    if decision.failed_rule_ids:
        print(f"未通过的规则：{'、'.join(decision.failed_rule_ids)}")
    if decision.not_evaluated_rule_ids:
        print(f"未评估的规则：{'、'.join(decision.not_evaluated_rule_ids)}")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 参数列表；``None`` 时取 ``sys.argv[1:]``。

    Returns:
        退出码，见模块文档。
    """
    arguments = build_parser().parse_args(argv)

    comparison, code = _load_comparison(arguments.comparison)
    if comparison is None:
        return code
    policy, code = _load_policy(arguments.policy)
    if policy is None:
        return code

    # 🔴 到这里两份输入都已经通过严格加载。角色由参数显式给出。
    decision = decide(comparison, policy)

    try:
        write_decision(decision, Path(arguments.decision_output))
    except OSError as exc:
        # 写盘失败**不得**沿用 outcome 的退出码：那会让"没写出来"看起来像
        # "判完了"。它是另一件事，有自己的码。
        print(f"GateDecision 写盘失败：{type(exc).__name__}", file=sys.stderr)
        return EXIT_OUTPUT_ERROR

    _report(decision, arguments.comparison, arguments.policy)
    print(f"GateDecision：{arguments.decision_output}")

    if decision.outcome is GateOutcome.PASS:
        return EXIT_OK
    if decision.outcome is GateOutcome.FAIL:
        return EXIT_FAIL
    return EXIT_NOT_EVALUATED


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
