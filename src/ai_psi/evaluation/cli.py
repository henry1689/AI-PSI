"""Golden Case 评测的命令行入口（阶段 7）。

两种模式：

**``in-memory``（默认，S1a）** —— 内存后端 + Mock，直接调运行时::

    uv run python -m ai_psi.evaluation.cli \
        --dataset evals/datasets \
        --output evals/reports/s1a-results.json \
        --canonical-output evals/reports/s1a-canonical.json

**``postgres-http``（S2）** —— 专用 PostgreSQL + **正式 HTTP 路由**::

    uv run python -m ai_psi.evaluation.cli \
        --mode postgres-http \
        --dataset evals/datasets \
        --evaluation-database-url <专用评测库> \
        --reference-database-url <专用参考库> \
        --output evals/reports/s2-results.json \
        --canonical-output evals/reports/s2-canonical.json

退出码：

===== ==========================================================
``0`` 全部案例通过
``1`` 有案例未通过（**包含**回合执行抛异常）
``2`` 数据集加载 / 校验失败——**此时一条案例都不执行**
``3`` 隔离预检失败——**此时一条案例都不执行、一个 HTTP 请求都不发**
===== ==========================================================

🔴 **``postgres-http`` 模式没有"回落"这条路。** 评测库 URL 必须显式给出，
且必须通过 :func:`~ai_psi.evaluation.isolation.plan_isolation` 的全部检查。
不从 ``AI_PSI_DATABASE_URL`` 猜、连不上就退回内存、失败就换成开发库——
这三种里的任何一种都会让评测看起来跑通了，而它刚刚写进了不该写的地方。

🔴 **默认模式仍是 ``in-memory``。** 把 S2 变成默认会让每次 ``make eval-golden``
都要求两座可丢弃的 PostgreSQL 库，那是对 S1a 使用者的破坏。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Final

from ai_psi.config import Settings, get_settings
from ai_psi.evaluation.executors import open_http_evaluation_executor
from ai_psi.evaluation.isolation import (
    DatabaseIdentity,
    IsolationError,
    IsolationPlan,
    parse_database_url,
    plan_isolation,
)
from ai_psi.evaluation.loader import DatasetError, GoldenDataset, load_dataset
from ai_psi.evaluation.postgres import (
    create_engine_for,
    drop_database,
    ensure_database,
    require_at_head,
    upgrade_to_head,
)
from ai_psi.evaluation.runner import (
    ExecutionMode,
    GoldenRunner,
    RunResult,
    StorageIsolation,
    build_mock_runtime,
)
from ai_psi.evaluation.serialization import write_reports
from ai_psi.evaluation.snapshot import take_reference_snapshot
from ai_psi.infrastructure.asyncio_compat import make_selector_loop

__all__ = [
    "EXIT_DATASET_ERROR",
    "EXIT_ISOLATION_ERROR",
    "EXIT_OK",
    "EXIT_TESTS_FAILED",
    "build_parser",
    "main",
]

EXIT_OK: Final[int] = 0
EXIT_TESTS_FAILED: Final[int] = 1
EXIT_DATASET_ERROR: Final[int] = 2
EXIT_ISOLATION_ERROR: Final[int] = 3

_DEFAULT_DATASET: Final[str] = "evals/datasets"
_DEFAULT_OUTPUT: Final[str] = "evals/reports/s1a-results.json"
_DEFAULT_CANONICAL: Final[str] = "evals/reports/s1a-canonical.json"

#: CLI 里 ``--mode`` 的取值。用连字符是因为它出现在命令行上，
#: 而枚举值用的是 Python 标识符风格。
_MODE_IN_MEMORY: Final[str] = "in-memory"
_MODE_POSTGRES_HTTP: Final[str] = "postgres-http"

#: 评测唯一允许的 Provider。
_MOCK_PROVIDER: Final[str] = "mock"


def build_parser() -> argparse.ArgumentParser:
    """构造参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m ai_psi.evaluation.cli",
        description="执行 Golden Case（阶段 7）",
    )
    parser.add_argument(
        "--mode",
        choices=[_MODE_IN_MEMORY, _MODE_POSTGRES_HTTP],
        default=_MODE_IN_MEMORY,
        help=(
            f"执行模式（默认 {_MODE_IN_MEMORY}）。"
            f"{_MODE_POSTGRES_HTTP} 走正式 HTTP 路由 + 专用 PostgreSQL"
        ),
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
    # ---- 仅 postgres-http 模式使用 ----
    parser.add_argument(
        "--evaluation-database-url",
        default=None,
        help="专用评测库连接串（postgres-http 模式必填；不会从环境变量推断）",
    )
    parser.add_argument(
        "--reference-database-url",
        default=None,
        help="专用参考库连接串（postgres-http 模式必填）",
    )
    parser.add_argument(
        "--keep-databases",
        action="store_true",
        help=(
            "评测结束后**保留**两座临时库以便诊断。"
            "⚠️ 这不是安全开关——它不放松任何守卫，只是不执行清理"
        ),
    )
    return parser


def _load_dataset_or_exit(dataset_root: Path) -> GoldenDataset | int:
    """加载数据集；失败时打印并返回退出码。"""
    try:
        return load_dataset(dataset_root)
    except DatasetError as exc:
        print(f"数据集加载失败：{exc.detail}", file=sys.stderr)
        print(f"  出问题的路径：{exc.path}", file=sys.stderr)
        return EXIT_DATASET_ERROR


def _report(result: RunResult, dataset: GoldenDataset, raw: Path, canonical: Path) -> None:
    """打印运行摘要。

    🔴 摘要里出现的数据库标识一律是**库名**，不是完整 URL。
    """
    print(f"数据集：{dataset.root}")
    print(f"执行模式：{result.execution_mode}")
    print(f"案例总数：{result.total}  通过：{result.passed}  失败：{result.failed}")
    isolation = result.storage_isolation
    if isolation is not None:
        print(f"评测库：{isolation.evaluation_database}")
        print(f"参考库：{isolation.reference_database}")
        print(f"两个库是同一个：{isolation.same_database}")
        print(f"参考快照未变：{isolation.reference_snapshot_unchanged}")
        print(f"参考学习状态未变：{isolation.reference_learning_state_unchanged}")
    if result.failed:
        print("失败案例：")
        for case in result.cases:
            if not case.passed:
                print(f"  - {case.case_id}：{case.failure_reason}")
    print(f"原始结果：{raw}")
    print(f"canonical：{canonical}")


def _run_in_memory(arguments: argparse.Namespace, dataset: GoldenDataset) -> RunResult:
    """S1a 路径：内存后端 + Mock，直接调运行时。"""
    runner = GoldenRunner(build_mock_runtime())
    return asyncio.run(runner.run_dataset(dataset.cases, execution_mode=ExecutionMode.IN_MEMORY))


def _postgres_settings(identity: DatabaseIdentity) -> Settings:
    """指向**专用评测库**的确定性配置。

    🔴 三件事都不能省：

    * ``database_url`` 来自显式给出的评测库连接串——不是环境变量，不是默认值；
    * ``llm_provider="mock"`` 写死，且装配后还会再确认一次；
    * ``_env_file=None``——不读开发机上的 ``.env``，否则结果会取决于
      "这台机器恰好配了什么"。
    """
    return Settings(
        _env_file=None,
        storage_backend="postgres",
        database_url=identity.normalized_url,
        llm_provider=_MOCK_PROVIDER,
        llm_model="mock-model-v1",
    )


async def _cleanup(plan: IsolationPlan, development: DatabaseIdentity) -> None:
    """删除两座临时库。

    🔴 清理失败**只报告、不改变退出码**：评测结论已经得出，
    把"删库没成功"记成"评测失败"会让结论失真。报告里只出现**库名**。
    """
    for identity in (plan.evaluation, plan.reference):
        try:
            await drop_database(identity, development=development)
        except Exception as exc:
            print(
                f"⚠️ 清理临时库失败：{identity.database}（{type(exc).__name__}）；"
                "请手动确认该库是否残留",
                file=sys.stderr,
            )


async def _run_postgres_http(
    arguments: argparse.Namespace, dataset: GoldenDataset
) -> tuple[RunResult | None, int]:
    """S2 路径：专用 PostgreSQL + 正式 HTTP 路由。

    Returns:
        ``(结果, 退出码)``；预检失败时结果为 ``None``、退出码为
        :data:`EXIT_ISOLATION_ERROR`。
    """
    development_url = get_settings().database_url

    # ---- 预检：在**任何**建库、迁移、HTTP 请求之前跑完 ----
    try:
        plan = plan_isolation(
            evaluation_database_url=arguments.evaluation_database_url,
            reference_database_url=arguments.reference_database_url,
            development_database_url=development_url,
            # CLI **没有** provider 开关：S2 恒为 Mock，所以这里传的是常量。
            # 这不是摆设——plan_isolation 的这条检查拦的正是"有人给 CLI
            # 加了 provider 开关、却忘了同步守卫"的将来。真正有牙齿的那次
            # 确认在装配之后：open_http_evaluation_executor 读的是
            # **装配出来的** provider 身份，而不是配置里写的字符串。
            provider=_MOCK_PROVIDER,
        )
        development = parse_database_url(development_url)
    except IsolationError as exc:
        print(f"隔离预检失败：{exc.message}", file=sys.stderr)
        return None, EXIT_ISOLATION_ERROR

    settings = _postgres_settings(plan.evaluation)

    try:
        # ---- 两座库：建（幂等）→ 迁移到 head ----
        await ensure_database(plan.evaluation, development=development)
        await ensure_database(plan.reference, development=development)
        await upgrade_to_head(plan.evaluation)
        await upgrade_to_head(plan.reference)

        evaluation_engine = create_engine_for(plan.evaluation)
        reference_engine = create_engine_for(plan.reference)
    except Exception as exc:
        print(
            f"评测库准备失败：{type(exc).__name__}；未执行任何案例",
            file=sys.stderr,
        )
        return None, EXIT_ISOLATION_ERROR

    try:
        # ---- 迁移版本必须在案例执行**之前**核对 ----
        await require_at_head(evaluation_engine, role="评测库")
        await require_at_head(reference_engine, role="参考库")

        before = await take_reference_snapshot(reference_engine)

        async with open_http_evaluation_executor(settings) as executor:
            runner = GoldenRunner(executor)
            result = await runner.run_dataset(
                dataset.cases, execution_mode=ExecutionMode.POSTGRES_HTTP
            )

        after = await take_reference_snapshot(reference_engine)
        differences = before.diff(after)
        if differences:
            # 🔴 隔离边界被越过了。**不能**把它记成"评测通过"——
            # 结论的可信度建立在"参考库没被动过"之上。
            for item in differences:
                print(f"🔴 参考库发生变化：{item}", file=sys.stderr)

        isolation = StorageIsolation(
            evaluation_database=plan.evaluation.database,
            reference_database=plan.reference.database,
            same_database=plan.same_database,
            reference_snapshot_unchanged=not differences,
            reference_learning_state_unchanged=(
                before.learning_digest == after.learning_digest
                and before.proposal_event_digest == after.proposal_event_digest
            ),
        )
        result = result.model_copy(update={"storage_isolation": isolation})
    except IsolationError as exc:
        print(f"隔离检查失败：{exc.message}", file=sys.stderr)
        return None, EXIT_ISOLATION_ERROR
    finally:
        await evaluation_engine.dispose()
        await reference_engine.dispose()
        if not arguments.keep_databases:
            await _cleanup(plan, development)

    return result, EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 参数列表；``None`` 时取 ``sys.argv[1:]``。

    Returns:
        退出码，见模块文档。
    """
    arguments = build_parser().parse_args(argv)

    loaded = _load_dataset_or_exit(Path(arguments.dataset))
    if isinstance(loaded, int):
        return loaded
    dataset = loaded

    if arguments.mode == _MODE_POSTGRES_HTTP:
        # 🔴 Windows 默认的 ``ProactorEventLoop`` 不被 psycopg 异步驱动支持
        # （ADR-0014 §1）。这里是**我们自建循环**，因此按那份 ADR 的优先级
        # 用 ``loop_factory`` 而不是设全局策略——后者有进程级副作用，
        # 而这里只想要一个循环。
        result, code = asyncio.run(
            _run_postgres_http(arguments, dataset), loop_factory=make_selector_loop
        )
        if result is None:
            return code
    else:
        result = _run_in_memory(arguments, dataset)

    raw_path = Path(arguments.output)
    canonical_path = Path(arguments.canonical_output)
    write_reports(result, raw_path=raw_path, canonical_path=canonical_path)
    _report(result, dataset, raw_path, canonical_path)
    return EXIT_OK if result.passed_overall else EXIT_TESTS_FAILED


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
