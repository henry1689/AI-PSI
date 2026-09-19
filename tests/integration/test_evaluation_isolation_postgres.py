"""S2：真实 PostgreSQL + 正式 HTTP 路径的评测隔离验证。

这个文件回答的是 S1a 回答不了的那个问题：**当评测真的碰到数据库时，
它会不会写错地方。**

## 两座库

* **reference** —— 预置了哨兵状态（事件、回合、经验、提案）。它是"必须保持
  不变的既有状态"的替身。评测**前后逐字段比对**；
* **evaluation** —— 只接收本次评测的写入。

两座都是**专用、可丢弃**的临时库（库名分别以 ``_reference_test`` /
``_eval_test`` 结尾），跑完删掉。**全程不碰任何真实数据库**。

## 怎么证明"没写错地方"

不是靠"我们小心"，而是靠四条可失败的断言：

1. 参考库的前后快照逐项相等（行数 + 主键集合 + 内容哈希 + 事件顺序哈希）；
2. 评测产生的回合 id 在**评测库**里查得到、在**参考库**里查不到；
3. 两座库各有各的 engine，不共享连接池；
4. 危险配置在**任何 HTTP 请求之前**就被拒绝，且两侧都没有落下数据。

## 不做什么

🔴 **不通过真实污染来证明隔离。** 没有"先关掉守卫、真的往参考库写一次、
再断言写进去了"这种用例——那会为了拿证据而制造一次真实污染。
对抗性用例全部走"构造危险配置 → 断言预检拒绝 → 断言请求数为 0",
见 :class:`TestFailClosed`。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_psi.config import Settings, get_settings
from ai_psi.domain.enums import ActorType, EventType
from ai_psi.evaluation.cli import EXIT_ISOLATION_ERROR, EXIT_OK, main
from ai_psi.evaluation.executors import open_http_evaluation_executor
from ai_psi.evaluation.isolation import (
    DatabaseRole,
    IsolationPlan,
    derive_database_url,
    parse_database_url,
    plan_isolation,
)
from ai_psi.evaluation.loader import load_dataset
from ai_psi.evaluation.postgres import (
    create_engine_for,
    drop_database,
    ensure_database,
    require_at_head,
    upgrade_to_head,
)
from ai_psi.evaluation.runner import ExecutionMode, GoldenRunner
from ai_psi.evaluation.snapshot import take_reference_snapshot
from ai_psi.infrastructure.db.session import create_session_factory
from ai_psi.infrastructure.db.unit_of_work import make_unit_of_work_factory
from ai_psi.providers.embeddings import LocalHashingEmbedding

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

Factory = Callable[..., Any]
EngineFixture = AsyncIterator[AsyncEngine]

#: 仓库自带的正式数据集。
_REPO = Path(__file__).resolve().parents[2]
_DATASET = _REPO / "evals" / "datasets"

#: 与 ``tests/integration/conftest.py`` 同一套清库范围（依赖倒序）。
_TABLES = (
    "events",
    "cognitive_rounds",
    "idempotency_keys",
    "memory_embeddings",
    "memories",
    "improvement_proposals",
)


def _plan_for(settings: Settings) -> IsolationPlan:
    """按测试库派生两座专用库，并跑一遍真实预检。

    🔴 走的是**正式预检函数**，不是测试自造的近似规则——
    否则"守卫通过"这件事就没有被验证。
    """
    return plan_isolation(
        evaluation_database_url=derive_database_url(settings.database_url, DatabaseRole.EVALUATION),
        reference_database_url=derive_database_url(settings.database_url, DatabaseRole.REFERENCE),
        development_database_url=get_settings().database_url,
        provider="mock",
    )


async def _prepare(plan: IsolationPlan) -> None:
    """建库 + 迁移到 head。"""
    development = parse_database_url(get_settings().database_url)
    for identity in (plan.evaluation, plan.reference):
        await ensure_database(identity, development=development)
        await upgrade_to_head(identity)


async def _drop(plan: IsolationPlan) -> None:
    """删除两座临时库。"""
    development = parse_database_url(get_settings().database_url)
    for identity in (plan.evaluation, plan.reference):
        await drop_database(identity, development=development)


@pytest.fixture(scope="module")
def prepared_databases(test_settings: Settings) -> Iterator[IsolationPlan]:
    """两座专用临时库（module 级：建库与迁移慢，但只需一次）。

    ⚠️ **同步夹具**：建库与 alembic 迁移都是同步流程（ADR-0014 §4），
    放进 ``asyncio.run`` 里各自建循环，不与其他用例的循环纠缠。
    """
    plan = _plan_for(test_settings)
    asyncio.run(_prepare(plan))
    try:
        yield plan
    finally:
        asyncio.run(_drop(plan))


@pytest.fixture
async def reference_engine(prepared_databases: IsolationPlan) -> EngineFixture:
    """参考库引擎（``NullPool``，每个用例自建自销）。"""
    engine = create_engine_for(prepared_databases.reference)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def evaluation_engine(prepared_databases: IsolationPlan) -> EngineFixture:
    """评测库引擎。

    🔴 与 :func:`reference_engine` 是**两个独立的 engine**——
    不共享连接池、不共享 session。共享池会让"这条查询打到哪个库"
    取决于池里恰好还回哪条连接，而隔离证据正是建立在
    "某个写入一定落在评测库"之上的。
    """
    engine = create_engine_for(prepared_databases.evaluation)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _truncate(engine: AsyncEngine) -> None:
    """清空一座库的全部业务表。"""
    async with engine.begin() as connection:
        await connection.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def clean_databases(reference_engine: AsyncEngine, evaluation_engine: AsyncEngine) -> None:
    """每个用例从两座空库开始。"""
    await _truncate(reference_engine)
    await _truncate(evaluation_engine)


@pytest.fixture
def http_calls(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """记录经过 ``AsyncClient`` 的每一个请求。

    ``httpx`` 的所有请求——包括 ``ASGITransport`` 的——都经过
    ``AsyncClient.send``，因此"这个列表是空的"就是"一个 HTTP 请求都没发出去"
    的直接证据。fail-closed 的用例靠它把「拒绝了」与「拒绝了、但拒绝之前
    已经发过请求」区分开。

    🔴 定义在**模块级**：``TestFailClosed`` 与 ``TestTheCliEndToEnd`` 都要用它。
    """
    calls: list[httpx.Request] = []
    original = httpx.AsyncClient.send

    async def _record(
        self: httpx.AsyncClient,
        request: httpx.Request,
        **kwargs: Any,
    ) -> httpx.Response:
        calls.append(request)
        return await original(self, request, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", _record)
    return calls


@pytest.fixture
async def seeded_reference(
    reference_engine: AsyncEngine,
    clean_databases: None,
    embeddings: LocalHashingEmbedding,
    make_event: Factory,
    make_round: Factory,
    make_proposal: Factory,
    make_experience: Factory,
) -> None:
    """在参考库里预置最小但可验证的哨兵状态。

    🔴 **全部走正式 repository 与正式事件存储**，没有一句手写 INSERT——
    手写的行会在下一次 schema 变化时与真实结构悄悄分叉，
    而那时"前后一致"仍然成立（因为它比的是自己写的东西）。

    ⚠️ **经验没有独立表**（``infrastructure/db/models.py``）：它以
    ``experience.created`` 事件的形式存在于 ``events``。因此哨兵经验
    是一条事件，而"学习状态有没有变"这个检查必须按事件类型去问。
    """
    factory = make_unit_of_work_factory(create_session_factory(reference_engine), embeddings)
    round_ = make_round()
    experience = make_experience(cognitive_round_id=round_.id)
    proposal = make_proposal()
    correlation = uuid4()

    experience_payload = experience.model_dump(mode="json")
    experience_payload["stop_reason"] = "goal_satisfied"
    experience_payload["attribution_reasons"] = ["哨兵数据，不由评测产生"]

    async with factory() as uow:
        await uow.rounds.add(round_)
        await uow.events.append_many(
            [
                make_event(
                    event_type=EventType.USER_MESSAGE_RECEIVED,
                    actor_type=ActorType.USER,
                    actor_id="sentinel-user",
                    cognitive_round_id=round_.id,
                    correlation_id=correlation,
                ),
                make_event(
                    event_type=EventType.COGNITIVE_ROUND_STARTED,
                    actor_id="sentinel-runtime",
                    cognitive_round_id=round_.id,
                    correlation_id=correlation,
                ),
                make_event(
                    event_type=EventType.EXPERIENCE_CREATED,
                    actor_id="sentinel-runtime",
                    cognitive_round_id=round_.id,
                    correlation_id=correlation,
                    payload={"experience": experience_payload},
                ),
            ]
        )
        await uow.proposals.add(proposal)
        await uow.commit()


async def _count(engine: AsyncEngine, table: str) -> int:
    """数一张表的行数。表名是本模块常量。"""
    async with engine.connect() as connection:
        result = await connection.execute(text(f"SELECT count(*) FROM {table}"))
    return int(result.scalar() or 0)


async def _round_ids(engine: AsyncEngine) -> set[str]:
    """一座库里全部认知回合的 id。"""
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT id FROM cognitive_rounds"))
    return {str(row[0]) for row in result.fetchall()}


async def _run_evaluation(plan: IsolationPlan, dataset_root: Path = _DATASET) -> list[UUID]:
    """通过**正式 HTTP 路径**在评测库上跑一遍数据集。

    🔴 走的是 ``create_app`` → lifespan → ``build_container`` → FastAPI 路由，
    不是"直接调运行时"。这正是 S2 要证明的那条路径。

    Returns:
        每个案例对应回合的 id，**按案例顺序**。
    """
    settings = Settings(
        _env_file=None,
        storage_backend="postgres",
        database_url=plan.evaluation.normalized_url,
        llm_provider="mock",
        llm_model="mock-model-v1",
    )
    dataset = load_dataset(dataset_root)
    async with open_http_evaluation_executor(settings) as executor:
        runner = GoldenRunner(executor)
        result = await runner.run_dataset(dataset.cases, execution_mode=ExecutionMode.POSTGRES_HTTP)
        assert result.passed == result.total, [
            (case.case_id, case.failure_reason) for case in result.cases if not case.passed
        ]
        return [case.cognitive_round_id for case in result.cases if case.cognitive_round_id]


class TestTheOfficialHttpPath:
    """C 组：10 个案例真的经过了正式 HTTP 路由 + 真实 PostgreSQL。"""

    async def test_the_ten_golden_cases_pass_over_http(
        self, prepared_databases: IsolationPlan, clean_databases: None
    ) -> None:
        round_ids = await _run_evaluation(prepared_databases)
        assert len(round_ids) == 10

    async def test_the_rounds_land_in_the_evaluation_database(
        self,
        prepared_databases: IsolationPlan,
        clean_databases: None,
        evaluation_engine: AsyncEngine,
    ) -> None:
        """评测库里有回合、有事件——证明走的是**真实持久化**而不是内存替身。"""
        round_ids = await _run_evaluation(prepared_databases)
        assert await _round_ids(evaluation_engine) == {str(item) for item in round_ids}
        assert await _count(evaluation_engine, "events") > 0

    async def test_no_external_network_request_is_attempted(
        self,
        prepared_databases: IsolationPlan,
        clean_databases: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """🔴 只堵**真实网络传输**：``ASGITransport`` 不走它，因此不受影响。

        如果哪天有人把评测接到真实 Provider 上，这条会立刻变红。
        """
        attempts: list[str] = []

        async def _forbidden(
            self: httpx.AsyncHTTPTransport, request: httpx.Request
        ) -> httpx.Response:
            attempts.append(str(request.url))
            msg = f"评测发起了外部网络请求：{request.url}"
            raise AssertionError(msg)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _forbidden)
        await _run_evaluation(prepared_databases)
        assert attempts == []

    async def test_an_unexpressible_case_is_refused_rather_than_silently_trimmed(
        self, prepared_databases: IsolationPlan
    ) -> None:
        """🔴 请求模型表达不了的刺激要被**拒绝**，不是丢掉一部分照跑。

        静默丢弃会让一条"输入不完整"的案例看起来通过了。
        """
        from ai_psi.evaluation.executors import EvaluationExecutionError, HttpEvaluationExecutor

        dataset = load_dataset(_DATASET)
        case = dataset.cases[0].model_copy(
            update={
                "stimulus": dataset.cases[0].stimulus.model_copy(
                    update={
                        "user_context": dataset.cases[0].stimulus.user_context.model_copy(
                            update={"conversation_summary": ["一段 HTTP 请求模型装不下的上下文"]}
                        )
                    }
                )
            }
        )
        settings = Settings(
            _env_file=None,
            storage_backend="postgres",
            database_url=prepared_databases.evaluation.normalized_url,
            llm_provider="mock",
            llm_model="mock-model-v1",
        )
        async with open_http_evaluation_executor(settings) as executor:
            assert isinstance(executor, HttpEvaluationExecutor)
            with pytest.raises(EvaluationExecutionError, match="无法表达"):
                await executor.execute(case)


class TestReferenceSnapshotOnARealDatabase:
    """D 组：快照读的是真库，且三种写入都检测得到。"""

    async def test_the_reference_snapshot_is_identical_after_an_evaluation_run(
        self,
        prepared_databases: IsolationPlan,
        seeded_reference: None,
        reference_engine: AsyncEngine,
    ) -> None:
        """🔴 S2 的核心断言：跑完 10 个案例，参考库逐字节没变。"""
        before = await take_reference_snapshot(reference_engine)
        await _run_evaluation(prepared_databases)
        after = await take_reference_snapshot(reference_engine)

        assert before.diff(after) == ()
        assert before.learning_digest == after.learning_digest
        assert before.event_order_digest == after.event_order_digest

    async def test_the_seeded_reference_actually_has_rows(
        self, seeded_reference: None, reference_engine: AsyncEngine
    ) -> None:
        """哨兵真的写进去了——否则上面那条"没变"是在比两座空库。"""
        assert await _count(reference_engine, "events") >= 3
        assert await _count(reference_engine, "cognitive_rounds") == 1
        assert await _count(reference_engine, "improvement_proposals") == 1

    async def test_an_unchanged_database_produces_an_empty_diff(
        self, seeded_reference: None, reference_engine: AsyncEngine
    ) -> None:
        first = await take_reference_snapshot(reference_engine)
        second = await take_reference_snapshot(reference_engine)
        assert first.diff(second) == ()

    async def test_an_insert_is_detected(
        self,
        seeded_reference: None,
        reference_engine: AsyncEngine,
        embeddings: LocalHashingEmbedding,
        make_event: Factory,
    ) -> None:
        """🔴 这一条**故意往参考库里写一行**——但那是测试自己的临时库，
        验的是"快照有没有牙齿"，不是"评测会不会污染参考库"。

        ⚠️ 用的是**正式事件存储**而不是手写 ``INSERT``：手写的那一行会在
        下一次 schema 变化时与真实结构悄悄分叉。
        """
        before = await take_reference_snapshot(reference_engine)
        factory = make_unit_of_work_factory(create_session_factory(reference_engine), embeddings)
        async with factory() as uow:
            await uow.events.append(make_event(correlation_id=uuid4()))
            await uow.commit()
        after = await take_reference_snapshot(reference_engine)
        assert any("行数" in item for item in before.diff(after))

    async def test_an_update_is_detected(
        self, seeded_reference: None, reference_engine: AsyncEngine
    ) -> None:
        before = await take_reference_snapshot(reference_engine)
        async with reference_engine.begin() as connection:
            await connection.execute(text("UPDATE cognitive_rounds SET updated_at = now()"))
        after = await take_reference_snapshot(reference_engine)
        differences = before.diff(after)
        # 行数与主键都没变，只有内容哈希变了——正是只比行数会漏掉的那种。
        assert not any("行数" in item for item in differences)
        assert any("UPDATE" in item for item in differences)

    async def test_a_delete_is_detected(
        self, seeded_reference: None, reference_engine: AsyncEngine
    ) -> None:
        before = await take_reference_snapshot(reference_engine)
        async with reference_engine.begin() as connection:
            await connection.execute(text("DELETE FROM improvement_proposals"))
        after = await take_reference_snapshot(reference_engine)
        assert any("行数" in item for item in before.diff(after))

    async def test_a_deleted_learning_state_is_detected(
        self, seeded_reference: None, reference_engine: AsyncEngine
    ) -> None:
        """经验活在事件流里——删掉它必须被**学习状态**摘要发现。"""
        before = await take_reference_snapshot(reference_engine)
        async with reference_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM events WHERE event_type = 'experience.created'")
            )
        after = await take_reference_snapshot(reference_engine)
        assert before.learning_digest != after.learning_digest


class TestWriteIsolation:
    """E 组：写入只落在评测库。"""

    async def test_the_evaluation_rounds_are_absent_from_the_reference_database(
        self,
        prepared_databases: IsolationPlan,
        seeded_reference: None,
        reference_engine: AsyncEngine,
    ) -> None:
        round_ids = await _run_evaluation(prepared_databases)
        assert round_ids
        reference_ids = await _round_ids(reference_engine)
        assert reference_ids.isdisjoint({str(item) for item in round_ids})

    async def test_the_two_databases_do_not_share_an_engine(
        self, reference_engine: AsyncEngine, evaluation_engine: AsyncEngine
    ) -> None:
        """🔴 两个 engine 是两个对象，连接池各自独立。"""
        assert reference_engine is not evaluation_engine
        assert reference_engine.pool is not evaluation_engine.pool

    async def test_the_evaluation_engine_points_at_the_evaluation_database(
        self, prepared_databases: IsolationPlan, evaluation_engine: AsyncEngine
    ) -> None:
        async with evaluation_engine.connect() as connection:
            result = await connection.execute(text("SELECT current_database()"))
        assert result.scalar() == prepared_databases.evaluation.database

    async def test_the_reference_engine_points_at_the_reference_database(
        self, prepared_databases: IsolationPlan, reference_engine: AsyncEngine
    ) -> None:
        async with reference_engine.connect() as connection:
            result = await connection.execute(text("SELECT current_database()"))
        assert result.scalar() == prepared_databases.reference.database

    async def test_an_evaluation_run_leaves_no_trace_in_the_reference_database(
        self,
        prepared_databases: IsolationPlan,
        seeded_reference: None,
        reference_engine: AsyncEngine,
    ) -> None:
        """事件数、回合数与提案数在评测前后逐一相等。"""
        before = (
            await _count(reference_engine, "events"),
            await _count(reference_engine, "cognitive_rounds"),
            await _count(reference_engine, "improvement_proposals"),
        )
        await _run_evaluation(prepared_databases)
        after = (
            await _count(reference_engine, "events"),
            await _count(reference_engine, "cognitive_rounds"),
            await _count(reference_engine, "improvement_proposals"),
        )
        assert before == after


class TestMigrationState:
    """B 组：两座库都真的迁移到了 head。"""

    async def test_both_databases_are_at_the_alembic_head(
        self, reference_engine: AsyncEngine, evaluation_engine: AsyncEngine
    ) -> None:
        assert await require_at_head(evaluation_engine, role="评测库")
        assert await require_at_head(reference_engine, role="参考库")

    async def test_a_database_below_head_is_rejected(self, evaluation_engine: AsyncEngine) -> None:
        """🔴 版本对不上就拒绝——守卫在**跑案例之前**发问。

        ⚠️ 评测库是 module 级共享的，因此这一条必须在 ``finally`` 里把
        版本号放回去；否则它会顺手把后面所有用例的前提改掉，
        而那类失败看起来会像是别的地方坏了。
        """
        from ai_psi.evaluation.isolation import IsolationError

        async with evaluation_engine.begin() as connection:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            head = str(result.scalar())
            await connection.execute(text("DELETE FROM alembic_version"))
        try:
            with pytest.raises(IsolationError, match="不是 head"):
                await require_at_head(evaluation_engine, role="评测库")
        finally:
            async with evaluation_engine.begin() as connection:
                await connection.execute(
                    text("INSERT INTO alembic_version (version_num) VALUES (:head)"),
                    {"head": head},
                )
        assert await require_at_head(evaluation_engine, role="评测库") == head


class TestFailClosed:
    """F 组：危险配置在**第一个 HTTP 请求之前**被拒绝。

    🔴 **全部走 CLI 入口**，因此验证的是"整条命令什么都不做"，
    而不只是"某个函数抛了异常"。
    """

    async def _run_cli(
        self,
        tmp_path: Path,
        *,
        evaluation: str | None,
        reference: str | None,
    ) -> tuple[int, Path]:
        """跑一次 postgres-http 模式的 CLI，返回退出码与原始报告路径。"""
        raw = tmp_path / "s2-run.json"
        argv = [
            "--mode",
            "postgres-http",
            "--dataset",
            str(_DATASET),
            "--output",
            str(raw),
            "--canonical-output",
            str(tmp_path / "s2-canonical.json"),
        ]
        if evaluation is not None:
            argv += ["--evaluation-database-url", evaluation]
        if reference is not None:
            argv += ["--reference-database-url", reference]
        code = await asyncio.to_thread(main, argv)
        return code, raw

    async def test_missing_urls_run_nothing(
        self, tmp_path: Path, http_calls: list[httpx.Request]
    ) -> None:
        code, raw = await self._run_cli(tmp_path, evaluation=None, reference=None)
        assert code == EXIT_ISOLATION_ERROR
        assert http_calls == []
        assert not raw.exists()

    async def test_the_same_database_for_both_roles_runs_nothing(
        self,
        prepared_databases: IsolationPlan,
        seeded_reference: None,
        reference_engine: AsyncEngine,
        tmp_path: Path,
        http_calls: list[httpx.Request],
    ) -> None:
        """🔴 最危险的一种配错：评测写进参考库。"""
        same = prepared_databases.reference.normalized_url
        before = await take_reference_snapshot(reference_engine)

        code, raw = await self._run_cli(tmp_path, evaluation=same, reference=same)

        assert code == EXIT_ISOLATION_ERROR
        assert http_calls == []
        assert not raw.exists()
        assert before.diff(await take_reference_snapshot(reference_engine)) == ()

    async def test_a_system_database_name_runs_nothing(
        self,
        prepared_databases: IsolationPlan,
        evaluation_engine: AsyncEngine,
        tmp_path: Path,
        http_calls: list[httpx.Request],
    ) -> None:
        dangerous = prepared_databases.evaluation.with_database("postgres")
        code, raw = await self._run_cli(
            tmp_path, evaluation=dangerous, reference=prepared_databases.reference.normalized_url
        )
        assert code == EXIT_ISOLATION_ERROR
        assert http_calls == []
        assert not raw.exists()
        # 评测库没有被写过
        assert await _count(evaluation_engine, "cognitive_rounds") == 0

    async def test_a_missing_dedicated_suffix_runs_nothing(
        self,
        prepared_databases: IsolationPlan,
        evaluation_engine: AsyncEngine,
        tmp_path: Path,
        http_calls: list[httpx.Request],
    ) -> None:
        """普通测试库不是专用评测库——没有可丢弃的标记就不许写。"""
        plain = prepared_databases.evaluation.with_database("ai_psi_test")
        code, raw = await self._run_cli(
            tmp_path, evaluation=plain, reference=prepared_databases.reference.normalized_url
        )
        assert code == EXIT_ISOLATION_ERROR
        assert http_calls == []
        assert not raw.exists()
        assert await _count(evaluation_engine, "cognitive_rounds") == 0

    async def test_the_error_message_never_carries_credentials(
        self,
        prepared_databases: IsolationPlan,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """🔴 预检失败的消息会打到终端——那里不该出现密码。"""
        same = prepared_databases.reference.normalized_url
        code, _ = await self._run_cli(tmp_path, evaluation=same, reference=same)
        assert code == EXIT_ISOLATION_ERROR
        captured = capsys.readouterr()
        # ⚠️ 只断言**密码**与**凭据对**不出现。
        # 用户名恰好是每个库名的前缀（`ai_psi` ← `ai_psi_reference_test`），
        # `"ai_psi" not in err` 永远为假——那种断言测的是库名，不是凭据。
        # 真正的泄漏形式是 `user:password@`，或者带密码的完整 URL。
        username = prepared_databases.reference.username
        password = prepared_databases.reference.password
        for stream in (captured.err, captured.out):
            assert password not in stream
            assert f"{username}:{password}" not in stream
            assert f"//{username}@" not in stream

    async def test_no_fallback_to_a_default_database_happens(
        self, tmp_path: Path, http_calls: list[httpx.Request], evaluation_engine: AsyncEngine
    ) -> None:
        """🔴 缺 URL 时**不回落**到环境里的默认库，也不回落到内存。

        默认库此刻是可连的（集成测试正在用它），如果存在回落路径，
        评测就会写进去——因此这条断言的是"什么都没写"，而不是"连不上"。
        """
        before = await _count(evaluation_engine, "cognitive_rounds")
        code, _ = await self._run_cli(tmp_path, evaluation=None, reference=None)
        assert code == EXIT_ISOLATION_ERROR
        assert http_calls == []
        assert await _count(evaluation_engine, "cognitive_rounds") == before


class TestTheCliEndToEnd:
    """两条路径都能从 CLI 走通，且默认仍是 S1a 的内存模式。"""

    async def test_the_default_mode_is_still_in_memory(
        self, tmp_path: Path, http_calls: list[httpx.Request]
    ) -> None:
        """🔴 不加 ``--mode`` 时**不碰数据库**——S1a 的用法必须保持可用。"""
        raw = tmp_path / "s1a.json"
        code = await asyncio.to_thread(
            main,
            [
                "--dataset",
                str(_DATASET),
                "--output",
                str(raw),
                "--canonical-output",
                str(tmp_path / "s1a-canonical.json"),
            ],
        )
        assert code == EXIT_OK
        assert raw.exists()
        # 日志里也没有任何数据库交互
        assert http_calls == []

    async def test_the_postgres_mode_reports_its_isolation_evidence(
        self,
        prepared_databases: IsolationPlan,
        seeded_reference: None,
        reference_engine: AsyncEngine,
        tmp_path: Path,
    ) -> None:
        """跑完 10 个案例，报告里带上三条布尔结论与两座库名。"""
        import json

        raw = tmp_path / "s2.json"
        code = await asyncio.to_thread(
            main,
            [
                "--mode",
                "postgres-http",
                "--dataset",
                str(_DATASET),
                "--evaluation-database-url",
                prepared_databases.evaluation.normalized_url,
                "--reference-database-url",
                prepared_databases.reference.normalized_url,
                "--output",
                str(raw),
                "--canonical-output",
                str(tmp_path / "s2-canonical.json"),
                # 🔴 CLI 默认会把两座临时库删掉——而它们是**本模块共享**的
                # fixture。这里保留，否则这一条跑完，后面所有用例连同
                # teardown 都会面对"库不存在"。
                # （清理本身由 fixture 的 teardown 负责。）
                "--keep-databases",
            ],
        )
        assert code == EXIT_OK
        payload = json.loads(raw.read_text(encoding="utf-8"))
        assert payload["execution_mode"] == "postgres_http"
        isolation = payload["storage_isolation"]
        assert isolation["same_database"] is False
        assert isolation["reference_snapshot_unchanged"] is True
        assert isolation["reference_learning_state_unchanged"] is True
        assert isolation["evaluation_database"] == prepared_databases.evaluation.database
        assert isolation["reference_database"] == prepared_databases.reference.database
        # 报告里不该出现完整 URL（那含密码）
        assert prepared_databases.evaluation.password not in raw.read_text(encoding="utf-8")
        # 参考库确实没被动过
        assert await _count(reference_engine, "cognitive_rounds") == 1
