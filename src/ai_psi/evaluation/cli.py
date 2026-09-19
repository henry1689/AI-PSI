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
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

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
from ai_psi.evaluation.manifest import (
    ReproducibilityError,
    ReproducibilityManifest,
    build_manifest,
    collect_code_identity,
    storage_identity,
)
from ai_psi.evaluation.metrics import compute_metrics
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
    deterministic_settings,
)
from ai_psi.evaluation.serialization import dumps, write_reports
from ai_psi.evaluation.snapshot import take_reference_snapshot
from ai_psi.infrastructure.asyncio_compat import make_selector_loop
from ai_psi.prompts.versions import build_default_registry

__all__ = [
    "EXIT_DATASET_ERROR",
    "EXIT_ISOLATION_ERROR",
    "EXIT_NOT_REPRODUCIBLE",
    "EXIT_OK",
    "EXIT_OUTPUT_ERROR",
    "EXIT_TESTS_FAILED",
    "build_parser",
    "main",
]

EXIT_OK: Final[int] = 0
EXIT_TESTS_FAILED: Final[int] = 1
EXIT_DATASET_ERROR: Final[int] = 2
EXIT_ISOLATION_ERROR: Final[int] = 3
#: ``--require-reproducible`` 要求而身份不可用（阶段 7 · S3）。
#: 🔴 与 ``EXIT_TESTS_FAILED`` 分开：那说明**案例没过**，这说**这次跑的
#: 环境不满足可复现条件**——两者的处置完全不同。
EXIT_NOT_REPRODUCIBLE: Final[int] = 4
#: 报告 / 清单写盘失败。
EXIT_OUTPUT_ERROR: Final[int] = 5

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
    # ---- 可复现性（阶段 7 · S3）----
    parser.add_argument(
        "--manifest-output",
        default=None,
        help=(
            "把可复现性清单单独写到这个路径（UTF-8、键排序、结尾恰好一个换行）。"
            "⚠️ 清单**总是**写进原始报告；这个参数只是额外给一份独立文件"
        ),
    )
    parser.add_argument(
        "--require-reproducible",
        action="store_true",
        help=(
            "要求这次运行具备正式可复现身份：Git 提交可用、工作树干净、"
            "Prompt 注册表完整、PostgreSQL 模式 migrating 到 head。"
            "任一不满足则在**执行案例之前**以退出码 4 失败。"
            "⚠️ 不提供任何绕过它的开关"
        ),
    )
    # ---- 指标（阶段 7 · S4）----
    parser.add_argument(
        "--metrics-output",
        default=None,
        help=(
            "把指标层结果单独写到这个路径（UTF-8、键排序、结尾恰好一个换行）。"
            "⚠️ 指标**总是**写进原始报告与 canonical；这个参数只是额外给一份独立文件"
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


def _provider_parameters(settings: Settings) -> dict[str, object]:
    """Provider 配置摘要用的**白名单**参数。

    🔴 只列会影响生成行为、且**不含秘密**的项。刻意不遍历 ``Settings``
    的所有字段——那样一个新增的密钥字段会在无人察觉时进入摘要，
    而摘要会被写进报告、粘进讨论区。

    ⚠️ 本切片只跑 Mock。而 ``MockProvider.generate_structured`` **忽略**
    ``ModelConfig``，因此采样参数（temperature 等）不影响结果；
    真正影响结果的是 Mock 的实现本身，那由 ``commit_sha`` 覆盖。
    这里记的是**配置层**会影响真实 Provider 行为的那些项。
    """
    return {
        "json_mode": settings.llm_json_mode,
        "max_retries": settings.llm_max_retries,
        "reasoning_headroom_tokens": settings.llm_reasoning_headroom_tokens,
    }


def _require_reproducible_identity() -> None:
    """``--require-reproducible`` 的**执行前**检查。

    Raises:
        ReproducibilityError: 任一条件不满足。**一条案例都不会跑**，
            一个 HTTP 请求都不会发。
    """
    code = collect_code_identity()
    if code.commit_sha is None:
        msg = (
            "拿不到 Git 提交（没有 .git，或 git 不可用），"
            "因此这次运行无法被定位到某个代码版本。"
            "⚠️ 本工具**不会**编造一个提交号来填满清单"
        )
        raise ReproducibilityError(msg)
    if not code.working_tree_clean:
        msg = (
            "工作树不干净：这份结果来自一个**无法从提交号重建**的代码状态。"
            "请先提交或 stash 改动。⚠️ 本工具不会替你清理工作树"
        )
        raise ReproducibilityError(msg)

    if not build_default_registry().task_names():
        msg = "Prompt 注册表为空，无法建立 Prompt 身份"
        raise ReproducibilityError(msg)


def _write_stable_json(path: Path, payload: dict[str, Any]) -> None:
    """把一份产物单独写出去。

    用与报告相同的 :func:`~ai_psi.evaluation.serialization.dumps`：
    UTF-8、键排序、缩进固定、结尾恰好一个换行。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(payload), encoding="utf-8", newline="\n")


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
    del arguments  # S1a 路径不看任何参数；清单里的仓库根用默认值
    settings = deterministic_settings()
    runtime = build_mock_runtime()
    runner = GoldenRunner(runtime)

    def _manifest(prompt_versions: Mapping[str, str]) -> ReproducibilityManifest:
        return build_manifest(
            cases=dataset.cases,
            execution_mode=ExecutionMode.IN_MEMORY.value,
            prompt_versions=prompt_versions,
            provider_name=runtime.provider_name,
            model_id=settings.llm_model or "",
            provider_parameters=_provider_parameters(settings),
            storage=storage_identity(backend="memory", alembic_revision=None),
        )

    return asyncio.run(
        runner.run_dataset(
            dataset.cases,
            execution_mode=ExecutionMode.IN_MEMORY,
            manifest_factory=_manifest,
        )
    )


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
        head = await require_at_head(evaluation_engine, role="评测库")
        await require_at_head(reference_engine, role="参考库")

        before = await take_reference_snapshot(reference_engine)

        async with open_http_evaluation_executor(settings) as executor:
            runner = GoldenRunner(executor)

            def _manifest(prompt_versions: Mapping[str, str]) -> ReproducibilityManifest:
                return build_manifest(
                    cases=dataset.cases,
                    execution_mode=ExecutionMode.POSTGRES_HTTP.value,
                    prompt_versions=prompt_versions,
                    provider_name=executor.provider_name,
                    model_id=settings.llm_model or "",
                    provider_parameters=_provider_parameters(settings),
                    # 🔴 只记 revision，不记库名——评测库的名字每次运行都不同。
                    storage=storage_identity(backend="postgresql", alembic_revision=head),
                )

            result = await runner.run_dataset(
                dataset.cases,
                execution_mode=ExecutionMode.POSTGRES_HTTP,
                manifest_factory=_manifest,
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

    # 🔴 可复现身份的检查排在**执行任何案例之前**：一条案例都不跑，
    # 一个 HTTP 请求都不发。这也是它不提供任何绕过开关的原因。
    if arguments.require_reproducible:
        try:
            _require_reproducible_identity()
        except ReproducibilityError as exc:
            print(f"不可复现：{exc}", file=sys.stderr)
            return EXIT_NOT_REPRODUCIBLE

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

    # 🔴 "实际用到了哪些 Prompt"只有跑完才知道。一个版本都没记到，说明这次
    # 运行拿不出可证明的 Prompt 身份——而 ``--require-reproducible`` 的承诺
    # 正是"身份完整"，所以必须失败，而不是写出一份空身份的清单。
    manifest = result.manifest
    if arguments.require_reproducible and (manifest is None or not manifest.prompts.versions):
        print(
            "不可复现：本次运行没有记录到任何实际使用的 Prompt 版本，"
            "无法证明它是在哪套提示词契约下跑出来的",
            file=sys.stderr,
        )
        return EXIT_NOT_REPRODUCIBLE

    # ---- 指标层（阶段 7 · S4）----
    #
    # 🔴 在这里算而不是在 runner 里：``total_cases`` 要的是**数据集**的
    # 案例总数（含未执行的），而 runner 只看得见执行过的那些。
    # 走得到这一步就说明数据集加载成功了（否则上面早已返回 2），
    # 因此不存在"加载失败却输出伪指标"的路径。
    result = result.model_copy(update={"metrics": compute_metrics(dataset, result)})

    raw_path = Path(arguments.output)
    canonical_path = Path(arguments.canonical_output)
    try:
        write_reports(result, raw_path=raw_path, canonical_path=canonical_path)
        if arguments.manifest_output:
            if manifest is None:
                msg = "本次运行没有采集到可复现性清单"
                raise ReproducibilityError(msg)
            _write_stable_json(Path(arguments.manifest_output), manifest.model_dump(mode="json"))
        if arguments.metrics_output:
            if result.metrics is None:  # pragma: no cover - 上面刚填过
                msg = "本次运行没有算出指标"
                raise ReproducibilityError(msg)
            _write_stable_json(
                Path(arguments.metrics_output), result.metrics.model_dump(mode="json")
            )
    except OSError as exc:
        # 写盘失败必须非零：一次"看起来跑完了却没产物"的运行是最坏的结果。
        print(f"报告写盘失败：{type(exc).__name__}", file=sys.stderr)
        return EXIT_OUTPUT_ERROR
    except ReproducibilityError as exc:
        print(f"产物不可用：{exc}", file=sys.stderr)
        return EXIT_NOT_REPRODUCIBLE

    _report(result, dataset, raw_path, canonical_path)
    return EXIT_OK if result.passed_overall else EXIT_TESTS_FAILED


if __name__ == "__main__":  # pragma: no cover - 进程入口
    raise SystemExit(main())
