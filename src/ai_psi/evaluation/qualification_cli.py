"""评测资格判定的命令行入口（阶段 8 · S8）::

    uv run python -m ai_psi.evaluation.qualification_cli \\
        --bundle evals/reports/s8-bundle.json \\
        --baseline-run evals/reports/s8-baseline.json \\
        --candidate-run evals/reports/s8-candidate.json \\
        --comparison evals/reports/s8-comparison.json \\
        --gate-policy evals/policies/s6_mock_golden_v1.json \\
        --gate-decision evals/reports/s8-gate-decision.json \\
        --qualification-policy evals/policies/s8_mock_golden_qualification_v1.json \\
        --output evals/reports/s8-qualification-decision.json

## 它做什么

读七个**角色显式**的输入，**自己重新执行一次 S7 证据验证**，按一份版本化的
资格策略把「证据是否有效」「门禁结论是否达标」「策略管不管得着」聚合成一个
确定性的资格结论，写出来。

## 🔴 它不做什么

不运行评测、不调用 Provider、不连数据库、不读网络、不通过子进程调用任何
CLI、不生成 S5／S6／S7 的产物。**它不接受 ``--verification-report``**——
那份报告要么是本次刚算出来的，要么就不可信。也不部署、不合并、不发布、
不打标签、不输出 Markdown／HTML。

七个参数**全部必填**：它**不会**去"找最新的那份"，也**不会**按文件名猜角色。

## 退出码

===== ==================================================================
``0`` ``QUALIFIED``
``1`` ``DISQUALIFIED``
``2`` CLI 参数错误（缺参数、不认识的参数）
``3`` 根输入 JSON／Schema／完整性错误，**无法形成结论**——不写产物
``4`` ``NOT_EVALUATED``
``5`` 输出写入失败
===== ==================================================================

🔴 **``0`` 不是发布许可。** 它只说明"在当前这份资格策略的作用域内，这条
证据链已被重新验证，且它的门禁结论满足该策略规定的最低资格条件"。产物里
没有任何字段能回答"能不能上生产"——那不是本工具的问题。

🔴 **``1`` 是 ``DISQUALIFIED`` 而**不是**"出错"。** 证据链被查出确定冲突
（``INVALID``）、或门禁结论不在策略允许集合内，都是它的正常结论。

🔴 **``3`` 与 ``4`` 是两件事。** ``3`` 是"连读都没读成"（策略坏了、证据包
连安全 Envelope 都形不成），**不产生结论**；``4`` 是"读成了，但这次没能
完整地判"（证据不可验证、策略不适用、门禁没判成），**产生结论**。

⚠️ 终端输出**只用 GBK 可编码的字符**（中文、ASCII、全角标点），**不用
emoji**：在 Windows 的 GBK 控制台上打印 emoji 会抛 ``UnicodeEncodeError``，
而那个异常会把进程退出码顶成 ``1``——一条结论被一个终端编码问题顶成了
故障码。S6、S7 都实测踩过。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

from ai_psi.evaluation.qualification import (
    EvaluationQualificationDecision,
    EvaluationQualificationOutcome,
    QualificationCheckOutcome,
    QualificationInputError,
    QualificationInputs,
    build_qualification_decision,
    write_qualification_decision,
)

__all__ = [
    "EXIT_DISQUALIFIED",
    "EXIT_INPUT_ERROR",
    "EXIT_NOT_EVALUATED",
    "EXIT_OK",
    "EXIT_OUTPUT_ERROR",
    "EXIT_USAGE_ERROR",
    "build_parser",
    "main",
]

EXIT_OK: Final[int] = 0
EXIT_DISQUALIFIED: Final[int] = 1
EXIT_USAGE_ERROR: Final[int] = 2
#: 根输入 JSON／Schema／完整性错误——**不产生**资格结论。
EXIT_INPUT_ERROR: Final[int] = 3
EXIT_NOT_EVALUATED: Final[int] = 4
EXIT_OUTPUT_ERROR: Final[int] = 5


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器。

    ⚠️ 这里**没有** ``--force``、``--trust-verification``、
    ``--skip-verification``、``--ignore-policy-scope``、
    ``--allow-invalid-evidence``、``--release``、``--deploy`` 这类开关，
    也不会有：任何"让它在不该给资格的时候还是给了"的参数，都会把这份结论
    变成可以商量的东西。

    🔴 特别注意**没有** ``--verification-report``：S8 从不接受一份现成的
    验证报告，它自己重新验证。
    """
    parser = argparse.ArgumentParser(
        prog="python -m ai_psi.evaluation.qualification_cli",
        description="按版本化资格策略判定一条评测证据链的资格（阶段 8 · S8）",
    )
    parser.add_argument("--bundle", required=True, help="S7 证据包 JSON")
    parser.add_argument("--baseline-run", required=True, help="基线运行结果 JSON（**原始**结果）")
    parser.add_argument("--candidate-run", required=True, help="候选运行结果 JSON（**原始**结果）")
    parser.add_argument("--comparison", required=True, help="S5 对比产物 JSON")
    parser.add_argument("--gate-policy", required=True, help="S6 版本化 GatePolicy JSON")
    parser.add_argument("--gate-decision", required=True, help="S6 门禁结论 JSON")
    parser.add_argument(
        "--qualification-policy",
        required=True,
        help="S8 版本化 EvaluationQualificationPolicy JSON",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="资格结论输出路径（原子写入：要么旧内容，要么完整的新内容）",
    )
    return parser


def _report(decision: EvaluationQualificationDecision, output_path: str) -> None:
    """把结论打到终端。

    🔴 这里**不打印**任何"可以合并／可以发布／建议上线"之类的判断。打印的是
    结论本身、检查明细，以及**为什么**得到这个结论。

    ⚠️ 只用 GBK 可编码的字符，理由见模块文档。
    """
    print(f"资格结论：{output_path}")
    print(f"  结论    ：{decision.qualification_outcome}")
    print(f"  证据    ：{decision.verification_outcome}（S7 本次重新验证的结果）")
    print(f"  门禁    ：{decision.identity.gate.gate_outcome}")
    identity = decision.identity.qualification_policy
    print(
        f"  策略    ：{identity.qualification_policy_id}@{identity.qualification_policy_revision}"
    )
    print(f"  策略适用：{decision.policy_applicability.status}")
    for reason in decision.policy_applicability.reasons:
        print(f"    - {reason}")
    failed = [item for item in decision.checks if item.outcome is QualificationCheckOutcome.FAIL]
    unevaluated = [
        item for item in decision.checks if item.outcome is QualificationCheckOutcome.NOT_EVALUATED
    ]
    passed = len(decision.checks) - len(failed) - len(unevaluated)
    print(f"  检查项  ：{len(decision.checks)} 项，通过 {passed} 项")
    for item in failed:
        print(f"    [不合格] {item.check_id}（{item.reason_code}）")
    for item in unevaluated:
        print(f"    [未判定] {item.check_id}（{item.reason_code}）")
    if decision.qualification_outcome is EvaluationQualificationOutcome.NOT_EVALUATED:
        print("  这不是「没有资格」，也不是「有资格」——这次没能完整地判。")
    # 🔴 只用 GBK 可编码的字符。
    print("  注意：QUALIFIED 只说明该策略作用域内这条链已被重新验证、门禁结论达标；")
    print("  它不是签名，也**不构成**发布、部署或合并的任何依据。")


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 参数列表；``None`` 时取 ``sys.argv[1:]``。

    Returns:
        退出码，见模块文档。
    """
    arguments = build_parser().parse_args(argv)
    inputs = QualificationInputs(
        bundle=Path(arguments.bundle),
        baseline_run=Path(arguments.baseline_run),
        candidate_run=Path(arguments.candidate_run),
        comparison=Path(arguments.comparison),
        gate_policy=Path(arguments.gate_policy),
        gate_decision=Path(arguments.gate_decision),
        qualification_policy=Path(arguments.qualification_policy),
    )

    try:
        decision = build_qualification_decision(inputs)
    except QualificationInputError as exc:
        # 🔴 根输入读不成 → **不写**结论。"看起来判完了却没有产物"与
        # "写了一份基于坏输入的错误结论"，后者危险得多。
        print(f"根输入不可用，未产生资格结论：{exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR

    try:
        write_qualification_decision(decision, Path(arguments.output))
    except OSError as exc:
        # 写盘失败**不得**沿用结论的退出码：那会让"没写出来"看起来像
        # "判完了"。它是另一件事，有自己的码。
        print(f"资格结论写盘失败：{type(exc).__name__}", file=sys.stderr)
        return EXIT_OUTPUT_ERROR

    _report(decision, arguments.output)

    outcome = decision.qualification_outcome
    if outcome is EvaluationQualificationOutcome.QUALIFIED:
        return EXIT_OK
    if outcome is EvaluationQualificationOutcome.DISQUALIFIED:
        return EXIT_DISQUALIFIED
    return EXIT_NOT_EVALUATED


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
