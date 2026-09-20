"""证据包构建与验证的命令行入口（阶段 7 · S7）::

    # 构建
    uv run python -m ai_psi.evaluation.evidence_cli build \\
        --baseline-run evals/reports/s7-baseline.json \\
        --candidate-run evals/reports/s7-candidate.json \\
        --comparison evals/reports/s7-comparison.json \\
        --policy evals/policies/s6_mock_golden_v1.json \\
        --gate-decision evals/reports/s7-gate-decision.json \\
        --bundle-output evals/reports/s7-bundle-1.json

    # 验证
    uv run python -m ai_psi.evaluation.evidence_cli verify \\
        --bundle evals/reports/s7-bundle-1.json \\
        --baseline-run evals/reports/s7-baseline.json \\
        --candidate-run evals/reports/s7-candidate.json \\
        --comparison evals/reports/s7-comparison.json \\
        --policy evals/policies/s6_mock_golden_v1.json \\
        --gate-decision evals/reports/s7-gate-decision.json \\
        --verification-output evals/reports/s7-verification-1.json

## 它做什么

`build`：读五个**角色显式**的输入，端到端重算一遍，产出一份证据包。
`verify`：拿一份证据包 + 五个输入，逐项核对摘要、长度、身份与重算结果。

## 🔴 它不做什么

不运行评测、不调用 Provider、不连数据库、不读网络、不通过子进程
驱动 S5／S6 的 CLI、不自动生成缺失的输入、不按文件名猜角色、不自动交换
基线／候选、不部署、不合并、不发布、不打标签、不输出 Markdown 或 HTML。

六个参数（`build`）/ 七个参数（`verify`）**全部必填**：这个工具**不会**
去"找最新的那份"，也**不会**按目录顺序推断哪份是基线。

## 退出码

``build``：

===== ==================================================================
``0`` 证据包构建成功（**与 GateDecision 的 outcome 无关**）
``2`` 参数错误，或某个输入文件不存在
``3`` 输入 Schema／完整性错误——**不写**证据包
``4`` 证据链重算不一致——**不写**证据包
``5`` 输出写入失败
===== ==================================================================

``verify``：

===== ==================================================================
``0`` ``verification_outcome=VERIFIED``
``1`` ``verification_outcome=INVALID``（至少一项确定冲突）
``2`` 参数错误
``3`` ``NOT_VERIFIABLE``，且原因是**连形状都读不出来**（JSON 坏、字段不对）
``4`` ``NOT_VERIFIABLE``，且原因是**证据不足**（缺文件、版本不受支持、无法重算）
``5`` 输出写入失败
===== ==================================================================

🔴 **``gate_outcome`` 不参与退出码。** 一条 ``gate_outcome=FAIL`` 但证据链
完全自洽的记录，`verify` 返回 ``0``：门禁说候选没通过，证据链说那份
"没通过"是真的——这是两个维度。

🔴 **``NOT_VERIFIABLE`` 既不是"没通过"也不是"通过"。** 它说的是
"这次没能完整地判"。把缺文件或版本不受支持判成 ``INVALID``，等于拿
"读不出来"去指控一份记录。

⚠️ 终端输出**只用 GBK 可编码的字符**（中文、ASCII、全角标点），**不用
emoji**：在 Windows 的 GBK 控制台上打印一个 emoji 会抛
``UnicodeEncodeError``，而那个异常会把进程退出码顶成 ``1``——于是
一条结论被一个终端编码问题顶成了故障码。S6 实测踩过。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Final

from ai_psi.evaluation.evidence import (
    EvidenceChainMismatchError,
    EvidenceInputError,
    EvidenceInputs,
    EvidenceVerificationInputs,
    VerificationCheckOutcome,
    VerificationOutcome,
    build_evidence_bundle,
    verify_evidence_bundle,
    write_bundle,
    write_verification_report,
)

__all__ = [
    "EXIT_CHAIN_MISMATCH",
    "EXIT_INPUT_ERROR",
    "EXIT_INVALID",
    "EXIT_NOT_VERIFIABLE",
    "EXIT_OK",
    "EXIT_OUTPUT_ERROR",
    "EXIT_USAGE_ERROR",
    "build_parser",
    "main",
]

EXIT_OK: Final[int] = 0
EXIT_INVALID: Final[int] = 1
EXIT_USAGE_ERROR: Final[int] = 2
#: ``build``：输入 Schema／完整性错误。
#: ``verify``：连形状都读不出来的 ``NOT_VERIFIABLE``。
EXIT_INPUT_ERROR: Final[int] = 3
#: ``build``：证据链重算不一致。
#: ``verify``：证据不足的 ``NOT_VERIFIABLE``。
EXIT_CHAIN_MISMATCH: Final[int] = 4
EXIT_NOT_VERIFIABLE: Final[int] = 4
EXIT_OUTPUT_ERROR: Final[int] = 5


def _add_artifact_arguments(parser: argparse.ArgumentParser) -> None:
    """五个角色显式的输入参数。**全部必填**。"""
    parser.add_argument(
        "--baseline-run",
        required=True,
        help="基线运行结果 JSON（**原始**结果，不是 canonical）",
    )
    parser.add_argument(
        "--candidate-run",
        required=True,
        help="候选运行结果 JSON（**原始**结果，不是 canonical）",
    )
    parser.add_argument("--comparison", required=True, help="S5 对比产物 JSON")
    parser.add_argument("--policy", required=True, help="版本化 GatePolicy JSON")
    parser.add_argument("--gate-decision", required=True, help="S6 门禁结论 JSON")


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器。

    ⚠️ 这里**没有** ``--force``、``--skip-recompute``、``--trust-digests``、
    ``--ignore-role``、``--allow-unknown-version`` 这类开关，也不会有：
    任何"让它在不该信的时候还是信了"的参数，都会把这份核验变成可以商量
    的东西。
    """
    parser = argparse.ArgumentParser(
        prog="python -m ai_psi.evaluation.evidence_cli",
        description="构建与验证可机读的评测证据包（阶段 7 · S7）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    builder = subparsers.add_parser("build", help="读五个输入、端到端重算、产出证据包")
    _add_artifact_arguments(builder)
    builder.add_argument(
        "--bundle-output",
        required=True,
        help="证据包输出路径（原子写入：要么旧内容，要么完整的新内容）",
    )

    verifier = subparsers.add_parser("verify", help="拿证据包逐项核对五个输入")
    verifier.add_argument(
        "--bundle",
        required=True,
        help="要验证的证据包 JSON（由 build 产出）",
    )
    _add_artifact_arguments(verifier)
    verifier.add_argument(
        "--verification-output",
        required=True,
        help="验证报告输出路径（原子写入）",
    )
    return parser


def _inputs(arguments: argparse.Namespace) -> EvidenceInputs:
    return EvidenceInputs(
        baseline_run=Path(arguments.baseline_run),
        candidate_run=Path(arguments.candidate_run),
        comparison=Path(arguments.comparison),
        policy=Path(arguments.policy),
        gate_decision=Path(arguments.gate_decision),
    )


def _missing_inputs(inputs: EvidenceInputs) -> tuple[tuple[str, Path], ...]:
    """**按固定角色顺序**列出不存在的输入文件。"""
    return tuple((role.value, path) for role, path in inputs.by_role() if not path.is_file())


def _run_build(arguments: argparse.Namespace) -> int:
    inputs = _inputs(arguments)
    missing = _missing_inputs(inputs)
    if missing:
        # 🔴 对 build 而言，"某个路径指不到文件"是**用法**问题：它要问的是
        # "按你给的这五份，能不能立一条链"。少给一份是没给全，不是
        # "这条链不成立"。（verify 不同：缺文件是**可以回答的**结论——
        # 证据不足，因此那边返回 NOT_VERIFIABLE。）
        for role, path in missing:
            print(f"{role} 的输入文件不存在：{path}", file=sys.stderr)
        return EXIT_USAGE_ERROR

    try:
        bundle = build_evidence_bundle(inputs)
    except EvidenceInputError as exc:
        print(f"输入不可用：{exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except EvidenceChainMismatchError as exc:
        print(f"证据链重算不一致：{exc}", file=sys.stderr)
        print("  「没有产出证据包：一份重算对不上的记录不是证据。」", file=sys.stderr)
        return EXIT_CHAIN_MISMATCH

    try:
        write_bundle(bundle, Path(arguments.bundle_output))
    except OSError as exc:
        print(f"证据包写盘失败：{type(exc).__name__}", file=sys.stderr)
        return EXIT_OUTPUT_ERROR

    print(f"证据包：{arguments.bundle_output}")
    print(f"  bundle_digest：{bundle.bundle_digest}")
    print(f"  基线提交：{bundle.chain_identity.baseline_commit_sha}")
    print(f"  候选提交：{bundle.chain_identity.candidate_commit_sha}")
    print(f"  门禁结论：{bundle.chain_identity.gate_outcome}")
    for descriptor in bundle.artifacts:
        print(f"  [{descriptor.role}] {descriptor.content_sha256}（{descriptor.byte_length} 字节）")
    return EXIT_OK


def _run_verify(arguments: argparse.Namespace) -> int:
    inputs = EvidenceVerificationInputs(bundle=Path(arguments.bundle), artifacts=_inputs(arguments))
    # 🔴 无论结论是哪一种都会拿到报告：失败路径也要有产物，
    # "没有报告"会让人分不清"没跑"与"跑挂了"。
    report = verify_evidence_bundle(inputs)

    try:
        write_verification_report(report, Path(arguments.verification_output))
    except OSError as exc:
        print(f"验证报告写盘失败：{type(exc).__name__}", file=sys.stderr)
        return EXIT_OUTPUT_ERROR

    print(f"验证报告：{arguments.verification_output}")
    print(f"  证据结论：{report.verification_outcome}")
    print(f"  门禁结论：{report.gate_outcome}（**不参与本次核验的结论**）")
    print(f"  bundle_digest：{report.bundle_digest}")
    failed = [item for item in report.checks if item.outcome is VerificationCheckOutcome.FAIL]
    unevaluated = [
        item for item in report.checks if item.outcome is VerificationCheckOutcome.NOT_EVALUATED
    ]
    passed = len(report.checks) - len(failed) - len(unevaluated)
    print(f"  检查项：{len(report.checks)} 项，通过 {passed} 项")
    for item in failed:
        print(f"  [冲突] {item.check_id}（{item.reason_code}）")
    for item in unevaluated:
        print(f"  [未判定] {item.check_id}（{item.reason_code}）")
    if report.verification_outcome is VerificationOutcome.NOT_VERIFIABLE:
        print("  这不是「记录有问题」，也不是「记录没问题」——这次没能完整地判。")
    # 🔴 只用 GBK 可编码的字符：这一行原本写的是「⚠️」，而 U+26A0 与
    # U+FE0F 在 GBK 控制台上都会抛 ``UnicodeEncodeError``——那个异常会把
    # 进程退出码顶成 1，于是一条结论被一个终端编码问题顶成了故障码。
    print("  注意：VERIFIED 只说明这组文件**内部一致且可重算**；")
    print("  它不是签名，也**不构成**发布、部署或合并的任何依据。")

    outcome = report.verification_outcome
    if outcome is VerificationOutcome.VERIFIED:
        return EXIT_OK
    if outcome is VerificationOutcome.INVALID:
        return EXIT_INVALID
    # NOT_VERIFIABLE：区分"连形状都读不出来"（3）与"证据不足"（4）。
    return EXIT_INPUT_ERROR if report.parse_level_failure() else EXIT_NOT_VERIFIABLE


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 参数列表；``None`` 时取 ``sys.argv[1:]``。

    Returns:
        退出码，见模块文档。
    """
    arguments = build_parser().parse_args(argv)
    if arguments.command == "build":
        return _run_build(arguments)
    return _run_verify(arguments)


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
