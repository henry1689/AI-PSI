"""🔴 R72 的并发验收：**两个并发学习运行针对同一个模式，至多成功创建一个活跃提案**。

真实 HTTP × 真实 PostgreSQL × 人为扩大的竞态窗口 × **20 轮**。

## 这条用例要证的是什么

阶段 6.6 的独立评审把机制讲清楚了：``LearningService._covered_keys()``
是"先读已存在的提案、再生成"，读与写之间没有锁。两个并发的纠正请求
各自读到"还没有提案"的旧快照，各走完门禁与生成，然后在**写入**时
才分胜负——结果是两条内容完全相同的 DRAFT（实测 3/3 复现，间隔
< ~50ms 必中）。

**"20 轮不翻车"本身不是证据**：靠碰运气的东西也能连过 20 次。
因此这条用例不靠时间窗口，而是**把两个运行按在同一个点上**：

    gate.arm()
    asyncio.gather(纠正一, 纠正二)

``gate`` 挂在 ``_covered_keys`` 上——那正是"读已完成、写尚未开始"
的那个点。armed 之后，先到的那个运行**等**后到的，两边都越过读取点
才一起放行。竞态窗口因此不是"碰巧重叠"，而是**被构造出来的**。

## 四条不能省的断言（少任何一条，用例都可能"通过"而什么都没验）

1. ``arrivals == 2``——两个学习运行**都**走到了那个点。只有一个到，
   说明另一个压根没触发学习（例如归因没形成），那时"只创建了一条"
   是**前提不成立**的假通过。
2. ``bypassed is False``——没有任何一轮是**被快速路径拦住**的。
   若某一轮 ``_covered_keys`` 返回非空，那个运行会直接 ``continue``，
   竞态根本没发生。
3. ``observed == [∅, ∅]``——两边**在写入前都观察到"未覆盖"**。
   这是"两个运行拿着同一份过期快照"的**直接**证据，
   而不是"应该如此"。
4. 两边都是 ``201`` + ``learning.status == "succeeded"`` +
   ``error_code is None``。

🔴 **第 4 条里的 ``succeeded`` 不是凑数的**：冲突若逃到了
``FeedbackService`` 的兜底 ``except Exception``，响应会是
``status="failed"`` + 非空 ``error_code``。断言 ``succeeded``
把"走的是 already_covered 语义"与"被兜底吞了"**区分开**——
两者都表现为 201，只看状态码是分不出来的。

## 为什么每轮要清库

情境签名是 ``f"{depth}|{evidence_bucket}|h{hypothesis_count}"``
（``cognition/.../_situation_signature``），而 HTTP 侧**改不动**
这三个分量：``SubmitMessageRequest`` 没有 evidence 字段、深度由路由器
按问题结构选档、假设数由 mock 决定。所以"换个新回合"**不等于**
"换个新签名"——20 轮会得到同一个 ``d2|no_evidence|h2``。

第 2 轮起，第一轮留下的提案会让 ``_covered_keys`` 直接命中，
运行根本走不到 ``create()``，用例会"通过"但什么都没验。
因此每轮从**空库**开始（TRUNCATE 全部表，与套件自身的用例间隔离
同一套机制——是清场，不是造数据）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_psi.api.app import create_app
from ai_psi.application.learning_service import LearningService
from ai_psi.config import Environment, Settings
from ai_psi.container import build_container

# 🔴 **复用黑盒用例的 HTTP 辅助，不复制一份。**
# 回合怎么跑、产物 id 怎么从摘要里读回来、纠正怎么发——三件事的定义
# 只能有一处；复制一份之后，两边的"什么算一次真实纠正"迟早会分家，
# 而分家的表现是这条用例在验一个**别的东西**。
from tests.integration.test_black_box_acceptance import (
    _artifacts_of,
    _correct_at,
    _run_round,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

#: 并发轮数。
#:
#: 🔴 **轮数是"够不够"的问题，不是"碰运气"的问题**：每一轮都有 barrier
#: 把两个运行按在同一个点上，因此每一轮都是**确定性地**进入竞态。
#: 20 轮的作用是覆盖"每轮之间是否互相污染""状态是否会累积"这类
#: 只有重复才暴露的问题，而不是"多试几次总会有一次撞上"。
_ITERATIONS = 20

#: barrier 的等待上限。
#:
#: 只有一个运行到达时**快速失败**，不让用例挂死——真实原因由
#: ``arrivals`` 那条断言报出来（"只有 1 个运行到达 barrier"），
#: 而不是变成一个"测试卡住了"。
_BARRIER_TIMEOUT_SECONDS = 10.0

#: 并发纠正打在**第三个**回合上。
#:
#: 前两个回合各纠正一次建立 2 次发生，第三次纠正把它顶到门槛 3——
#: 于是两个并发的学习运行**同时**看到"这个模式达标了、还没有提案"。
_RACE_ROUND_INDEX = 2

#: 终端状态的字面量，用来数"活跃"提案。
#: 与 ``ProposalStatus.is_terminal`` 对应；这里写 SQL 所以只能字面量，
#: 一致性由 ``tests/unit/test_in_memory_pattern_uniqueness.py`` 与
#: 迁移用例共同守住。
_TERMINAL_SQL = "('rejected', 'approved_for_manual_trial')"


class CoverageGate:
    """挂在 ``_covered_keys`` 上的会合点。

    armed 之前**直接放行**——否则前两个回合的纠正也会被卡住等对手，
    而那时根本没有对手。
    """

    def __init__(self) -> None:
        self.armed = False
        self.arrivals = 0
        self.observed: list[frozenset[tuple[str, str]]] = []
        self.bypassed = False
        self._both_arrived = asyncio.Event()

    def arm(self) -> None:
        """从这一刻起，运行必须成对到达。"""
        self.armed = True

    async def pass_through(self, covered: set[tuple[str, str]]) -> None:
        """一个学习运行越过了读取点。"""
        if not self.armed:
            return
        if covered:
            # 🔴 **快速路径命中了**——竞态根本没发生。记下来，
            # 让用例变红，而不是让它悄悄"通过"。
            self.bypassed = True
            return
        self.arrivals += 1
        self.observed.append(frozenset(covered))
        if self.arrivals >= 2:
            self._both_arrived.set()
        await asyncio.wait_for(self._both_arrived.wait(), timeout=_BARRIER_TIMEOUT_SECONDS)


@pytest.fixture
async def black_box(
    test_settings: Settings,
    truncate: Callable[[], Awaitable[None]],
) -> AsyncIterator[tuple[httpx.AsyncClient, AsyncEngine]]:
    """真实 PostgreSQL 上的 HTTP 客户端 + 引擎。

    🔴 ``raise_app_exceptions=False`` 与黑盒用例同一个理由：
    Starlette 在生成 500 之后会重新抛出，而真实客户端看到的是 500。
    这条用例要断言"**没有**未处理的 500"——让传输层抛异常会让
    "500"这件事在测试里与在线上长得不一样。

    ⚠️ 对应地，应用抛异常时用例**不会自动变红**；补偿是每一处都断言了
    具体的状态码，多出来的 500 会在那些断言上被抓住并打印响应体。
    """
    del truncate
    settings = test_settings.model_copy(
        update={
            "storage_backend": "postgres",
            "llm_provider": "mock",
            "env": Environment.TESTING,
        }
    )
    app = create_app(settings)
    container = build_container(settings)
    app.state.container = container
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    assert container.engine is not None, "postgres 后端必须给出引擎"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, container.engine
    await container.aclose()


async def _active_proposals_for(
    engine: AsyncEngine, *, error_class: str | None, signature: str | None
) -> int:
    """数"活跃"提案：给定了键就只数那个键，否则数全部。"""
    if error_class is None or signature is None:
        sql = f"SELECT count(*) FROM improvement_proposals WHERE status NOT IN {_TERMINAL_SQL}"
        params: dict[str, Any] = {}
    else:
        sql = (
            "SELECT count(*) FROM improvement_proposals "
            " WHERE status NOT IN "
            + _TERMINAL_SQL
            + " AND error_class = :c AND applicability[1] = :s"
        )
        params = {"c": error_class, "s": signature}
    async with engine.begin() as conn:
        return int((await conn.execute(text(sql), params)).scalar_one())


async def _one_race(
    client: httpx.AsyncClient,
    engine: AsyncEngine,
    gate: CoverageGate,
) -> None:
    """跑一轮：三个回合 → 两次铺垫纠正 → 并发两条纠正。"""
    rounds = [await _run_round(client, depth="d2") for _ in range(3)]
    for round_id in rounds[:_RACE_ROUND_INDEX]:
        artifacts = await _artifacts_of(client, round_id)
        response = await _correct_at(client, round_id, artifact_id=artifacts["hypothesis"])
        assert response.status_code == 201, response.text

    # 🔴 **并发前：该业务键下活跃提案数必须是 0。**
    # 清库之后这一点对所有键都成立，因此断言写成"整个表没有活跃提案"
    # ——它比"某个键为 0"更强，而且不依赖我们先知道签名是什么。
    assert await _active_proposals_for(engine, error_class=None, signature=None) == 0

    race_round = rounds[_RACE_ROUND_INDEX]
    artifacts = await _artifacts_of(client, race_round)
    pointer = artifacts["hypothesis"]

    gate.arm()
    first, second = await asyncio.gather(
        _correct_at(client, race_round, artifact_id=pointer),
        _correct_at(client, race_round, artifact_id=pointer),
    )

    # 🔴 **三条关于"竞态真的发生了"的断言，放在最前面。**
    # 它们的失败原因比"提案数不对"具体得多：只创建一个提案也可能是因为
    # 第二个运行压根没触发学习——那是**前提不成立**的假通过。
    assert gate.arrivals == 2, f"只有 {gate.arrivals} 个学习运行到达 barrier"
    assert gate.bypassed is False, "有一轮命中了 _covered_keys 快速路径，竞态没有发生"
    assert gate.observed == [frozenset(), frozenset()], gate.observed

    # 🔴 **两条响应都必须成功，且都不是被兜底异常处理器吞掉的。**
    bodies = []
    for response in (first, second):
        assert response.status_code == 201, response.text
        body = response.json()
        bodies.append(body)
        assert body["learning"] is not None, body
        assert body["learning"]["status"] == "succeeded", body["learning"]
        assert body["learning"]["error_code"] is None, body["learning"]

    created = [
        proposal_id for body in bodies for proposal_id in body["learning"]["created_proposal_ids"]
    ]
    assert len(created) == 1, f"两个运行合计创建了 {len(created)} 条提案：{created}"
    assert len(set(created)) == 1

    # 🔴 **数据库里恰好一条活跃提案。**
    assert await _active_proposals_for(engine, error_class=None, signature=None) == 1

    # 再按业务键数一次：证明那条活跃提案**就是**这个模式的，
    # 而且**只有一个**业务键上挂着活跃提案。
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT error_class, applicability[1], count(*) FROM improvement_proposals "
                    f" WHERE status NOT IN {_TERMINAL_SQL}"
                    " GROUP BY error_class, applicability[1]"
                )
            )
        ).all()
    assert len(row) == 1, row
    assert row[0][2] == 1, row


class TestTwoConcurrentRunsCreateAtMostOneProposal:
    """🔴 R72 的验收：并发下唯一的裁决者是 PostgreSQL。"""

    @pytest.mark.parametrize("iteration", range(_ITERATIONS))
    async def test_the_race_is_created_and_only_one_proposal_survives(
        self,
        black_box: tuple[httpx.AsyncClient, AsyncEngine],
        truncate: Callable[[], Awaitable[None]],
        monkeypatch: pytest.MonkeyPatch,
        iteration: int,
    ) -> None:
        """第 ``iteration`` 轮。每轮从**空库**开始（理由见模块文档）。"""
        client, engine = black_box
        await truncate()

        gate = CoverageGate()
        original = LearningService._covered_keys

        async def gated(self: LearningService) -> set[tuple[str, str]]:
            # 先真的算出结果，再决定要不要会合——顺序不能反：
            # 先会合的话，"两边看到什么"就成了一句空话。
            covered = await original(self)
            await gate.pass_through(covered)
            return covered

        monkeypatch.setattr(LearningService, "_covered_keys", gated)
        # ⚠️ monkeypatch 是**类**级别的，容器构造在它之后（夹具已建好容器，
        # 但方法查的是类属性，因此同样生效）。

        await _one_race(client, engine, gate)

    async def test_a_single_run_still_creates_the_proposal(
        self,
        black_box: tuple[httpx.AsyncClient, AsyncEngine],
        truncate: Callable[[], Awaitable[None]],
    ) -> None:
        """反向对照：**没有并发时照常生成**。

        少了这一条，一个"任何情况下都写不进去"的实现在上面 20 轮里
        也会全绿——但提案能力就此消失。
        """
        client, engine = black_box
        await truncate()

        rounds = [await _run_round(client, depth="d2") for _ in range(3)]
        created: list[str] = []
        for round_id in rounds:
            artifacts = await _artifacts_of(client, round_id)
            response = await _correct_at(client, round_id, artifact_id=artifacts["hypothesis"])
            assert response.status_code == 201, response.text
            created += response.json()["learning"]["created_proposal_ids"]

        assert len(created) == 1, created
        assert await _active_proposals_for(engine, error_class=None, signature=None) == 1


class TestTheConflictNamesTheIndexItHit:
    """🔴 **唯一索引的冲突报文里，``CONSTRAINT NAME`` 填的是索引名。**

    这是 ``unique_violation_constraint`` 能把"业务键撞车"与"主键撞车"
    分开的**全部依据**。若 PostgreSQL 或驱动哪天不再这么填，
    那个函数会永远返回 ``None``，于是两类冲突又混成一类——
    而表现是"并发下本该被翻译成 already_covered 的冲突，
    变成了主键冲突"，或者反过来的静默吞掉真 bug。

    与其让这件事**悄悄**退化，不如在这里钉住它：填法一变，这条先红。
    """

    async def test_the_pattern_conflict_names_the_index(
        self, black_box: tuple[httpx.AsyncClient, AsyncEngine]
    ) -> None:
        from sqlalchemy.exc import IntegrityError as SqlIntegrityError

        from ai_psi.infrastructure.db.errors import (
            is_unique_violation,
            unique_violation_constraint,
        )

        _client, engine = black_box
        signature = f"d2|no_evidence|c{uuid4().hex[:8]}"
        statement = text(
            """
            INSERT INTO improvement_proposals (
                id, created_at, updated_at, version, created_by, schema_version,
                target_component, observed_problem, error_class,
                supporting_experience_ids, counterexamples, proposed_change,
                expected_benefit, possible_regressions, applicability,
                evaluation_plan, success_metrics, rollback_conditions,
                approval_level, status
            ) VALUES (
                :id, now(), now(), 1, 'test', '1',
                'prompt:logical_analyzer', '冲突名验收', 'reasoning_error',
                ARRAY[]::uuid[], ARRAY[]::text[], '改动', '收益',
                ARRAY[]::text[], ARRAY[:signature], ARRAY[]::text[],
                ARRAY[]::text[], ARRAY[]::text[], 'user_and_review', 'draft'
            )
            """
        )
        async with engine.begin() as conn:
            await conn.execute(statement, {"id": uuid4(), "signature": signature})

        with pytest.raises(SqlIntegrityError) as caught:
            async with engine.begin() as conn:
                await conn.execute(statement, {"id": uuid4(), "signature": signature})

        assert is_unique_violation(caught.value)
        assert (
            unique_violation_constraint(caught.value) == "uq_improvement_proposals_active_pattern"
        ), "唯一索引的冲突没有带出索引名——分流依据失效了"


class TestTheDatabaseIsTheArbiter:
    """🔴 唯一性由 PostgreSQL 裁决——应用层只是快速路径。"""

    async def test_the_index_exists_and_is_partial(
        self, black_box: tuple[httpx.AsyncClient, AsyncEngine]
    ) -> None:
        """索引必须**真的**在库里，且形状与设计一致。

        这条不是形式主义：整个 R72 的保证都挂在它身上。
        谓词写错一个字（比如漏掉终态那一半），并发防线就会
        在某个状态下静默失效——而那种失效只在恰好撞上时可见。
        """
        _client, engine = black_box
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT pg_get_indexdef(indexrelid) FROM pg_index "
                        "WHERE indexrelid = 'uq_improvement_proposals_active_pattern'::regclass"
                    )
                )
            ).scalar_one()
        assert "(applicability[1])" in row, row
        assert "cardinality(applicability) > 0" in row, row
        assert "'rejected'" in row and "'approved_for_manual_trial'" in row, row

    async def test_the_unique_constraint_alone_refuses_a_second_rows(
        self, black_box: tuple[httpx.AsyncClient, AsyncEngine]
    ) -> None:
        """绕过整个应用层，**直接 INSERT 两条同键活跃提案**——必须被数据库拒绝。

        这条把"谁在挡"这件事钉死：不是应用层某段代码在检查，
        而是数据库自己。少了它，一个"应用层碰巧挡住了"的实现也能
        让上面 20 轮全绿。
        """
        _client, engine = black_box
        # 用一个**只属于本条用例**的签名：它跑在并发那 20 轮之外，
        # 不该因为共享测试库而被别的用例的残留影响。
        signature = f"d2|no_evidence|{uuid4().hex[:8]}"
        statement = text(
            """
            INSERT INTO improvement_proposals (
                id, created_at, updated_at, version, created_by, schema_version,
                target_component, observed_problem, error_class,
                supporting_experience_ids, counterexamples, proposed_change,
                expected_benefit, possible_regressions, applicability,
                evaluation_plan, success_metrics, rollback_conditions,
                approval_level, status
            ) VALUES (
                :id, now(), now(), 1, 'test', '1',
                'prompt:logical_analyzer', '并发验收', 'reasoning_error',
                ARRAY[]::uuid[], ARRAY[]::text[], '改动', '收益',
                ARRAY[]::text[], ARRAY[:signature], ARRAY[]::text[],
                ARRAY[]::text[], ARRAY[]::text[], 'user_and_review', 'draft'
            )
            """
        )
        # ⚠️ 两条 INSERT 必须各自一个事务：第一条成功之后，若在**同一个**
        # 事务里撞唯一冲突，那个事务会被 PostgreSQL 置为 aborted，
        # 退出 `engine.begin()` 时的 COMMIT 会以
        # `InFailedSqlTransaction` 失败——报出来的错就变成了
        # "事务已失效"，而不是"唯一性拦住了它"。
        async with engine.begin() as conn:
            await conn.execute(statement, {"id": uuid4(), "signature": signature})

        with pytest.raises(IntegrityError) as caught:
            async with engine.begin() as conn:
                await conn.execute(statement, {"id": uuid4(), "signature": signature})
        assert "uq_improvement_proposals_active_pattern" in str(caught.value)
