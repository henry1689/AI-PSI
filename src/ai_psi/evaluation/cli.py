"""Golden Case 评测的命令行入口（阶段 7 · S1a）。

用法::

    uv run python -m ai_psi.evaluation.cli \
        --dataset evals/datasets \
        --output evals/reports/s1a-results.json \
        --canonical-output evals/reports/s1a-canonical.json

退出码：

===== ========================================================
``0`` 全部案例通过
``1`` 有案例未通过（**包含**回合执行抛异常）
``2`` 数据集加载 / 校验失败——**此时一条案例都不执行**
===== ========================================================

🔴 **本 CLI 只有 Mock 模式。** 没有 ``--provider`` 之类的开关：
真实 Provider 的评测属于后续切片，而"提供一个能接真实模型的开关"
会让这个切片从"验证契约"变成"开始搭平台"。
配置里的 provider 由 :func:`~ai_psi.evaluation.runner.deterministic_settings`
写死为 ``mock``，且装配后会**再断言一次**（见 :func:`build_mock_runtime`）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Final

from ai_psi.evaluation.loader import DatasetError, load_dataset
from ai_psi.evaluation.runner import GoldenRunner, build_mock_runtime
from ai_psi.evaluation.serialization import write_reports

__all__ = ["EXIT_DATASET_ERROR", "EXIT_OK", "EXIT_TESTS_FAILED", "build_parser", "main"]

EXIT_OK: Final[int] = 0
EXIT_TESTS_FAILED: Final[int] = 1
EXIT_DATASET_ERROR: Final[int] = 2

_DEFAULT_DATASET: Final[str] = "evals/datasets"
_DEFAULT_OUTPUT: Final[str] = "evals/reports/s1a-results.json"
_DEFAULT_CANONICAL: Final[str] = "evals/reports/s1a-canonical.json"


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m ai_psi.evaluation.cli",
        description="在 Mock Provider 上执行 Golden Case（阶段 7 · S1a）",
    )
    parser.add_argument(
        "--dataset",
        default=_DEFAULT_DATASET,
        help=f"数据集根目录（默认 {_DEFAULT_DATASET}）",
    )
    parser.add_argument(
        "--output",
        default=_DEFAULT_OUTPUT,
        help=f"原始结果输出路径（默认 {_DEFAULT_OUTPUT}）",
    )
    parser.add_argument(
        "--canonical-output",
        default=_DEFAULT_CANONICAL,
        help=f"canonical 结果输出路径（默认 {_DEFAULT_CANONICAL}）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 参数列表；``None`` 时取 ``sys.argv[1:]``。

    Returns:
        退出码，见模块文档。
    """
    arguments = build_parser().parse_args(argv)

    dataset_root = Path(arguments.dataset)
    try:
        dataset = load_dataset(dataset_root)
    except DatasetError as exc:
        print(f"数据集加载失败：{exc.detail}", file=sys.stderr)
        print(f"  出问题的路径：{exc.path}", file=sys.stderr)
        return EXIT_DATASET_ERROR

    runtime = build_mock_runtime()
    runner = GoldenRunner(runtime)
    result = asyncio.run(runner.run_dataset(dataset.cases))

    raw_path = Path(arguments.output)
    canonical_path = Path(arguments.canonical_output)
    write_reports(result, raw_path=raw_path, canonical_path=canonical_path)

    print(f"数据集：{dataset.root}")
    print(f"案例总数：{result.total}  通过：{result.passed}  失败：{result.failed}")
    if result.failed:
        print("失败案例：")
        for case in result.cases:
            if not case.passed:
                print(f"  - {case.case_id}：{case.failure_reason}")
    print(f"原始结果：{raw_path}")
    print(f"canonical：{canonical_path}")
    return EXIT_OK if result.passed_overall else EXIT_TESTS_FAILED


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
