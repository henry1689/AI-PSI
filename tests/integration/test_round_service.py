"""认知回合并应用服务的集成测试。

🔴 覆盖阶段 2 的三条验收条件：

1. 可以创建和回放事件；
2. **非法状态转换全部拒绝**；
3. **事务失败不会留下半成品数据**。

以及 §13.4 的幂等与乐观锁。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.application.replay_service import ReplayService
from ai_psi.application.round_service import CognitiveRoundService
from ai_psi.domain.enums import CognitiveDepth, ErrorType, EventType, RoundState
from ai_psi.domain.exceptions import (
    ConflictError,
    IllegalStateTransitionError,
    NotFoundError,
    OptimisticLockError,
)


@pytest.fixture
def service(uow_factory: UnitOfWorkFactory) -> CognitiveRoundService:
    return CognitiveRoundService(uow_factory)


@pytest.fixture
def replay(uow_factory: UnitOfWorkFactory) -> ReplayService:
    return ReplayService(uow_factory)


async def _walk_to(service: CognitiveRoundService, round_id, *states: RoundState):
    """把回合依次推进到指定状态。"""
    for state in states:
        await service.transition(round_id, state, reason="test")


class TestCreateRound:
    async def test_start_round_persists_state_and_event(
        self, service: CognitiveRoundService, uow_factory: UnitOfWorkFactory
    ) -> None:
        result = await service.start_round(depth_level=CognitiveDepth.D2)

        assert result.created is True
        assert result.round.state is RoundState.CREATED

        async with uow_factory() as uow:
            stored = await uow.rounds.get(result.round.id)
            events = await uow.events.read_stream(cognitive_round_id=result.round.id)

        assert stored is not None
        assert stored.state is RoundState.CREATED
        assert [e.event_type for e in events] == [EventType.COGNITIVE_ROUND_STARTED]

    async def test_budget_defaults_from_depth(self, service: CognitiveRoundService) -> None:
        result = await service.start_round(depth_level=CognitiveDepth.D4)
        # 13 是阶段 3 按真实模块成本重新标定后的值（ADR-0008 已修订）
        assert result.round.budget.max_model_calls == 13

    async def test_explicit_budget_wins(self, service: CognitiveRoundService) -> None:
        from ai_psi.domain.cognitive_rounds import CognitiveBudget

        result = await service.start_round(budget=CognitiveBudget(max_model_calls=3))
        assert result.round.budget.max_model_calls == 3


class TestLegalTransitions:
    async def test_happy_path_through_all_states(self, service: CognitiveRoundService) -> None:
        started = await service.start_round()
        round_id = started.round.id

        await _walk_to(
            service,
            round_id,
            RoundState.TRIAGING,
            RoundState.FRAMING,
            RoundState.RETRIEVING,
            RoundState.ANALYZING,
            RoundState.DELIBERATING,
            RoundState.METACOGNITIVE_REVIEW,
            RoundState.SYNTHESIZING,
            RoundState.RESPONDING,
        )
        final = await service.complete_round(round_id, stop_reason="stop_condition_reached")

        assert final.round.state is RoundState.COMPLETED
        assert final.round.stop_reason == "stop_condition_reached"
        assert final.round.completed_at is not None

    async def test_version_increments_per_transition(self, service: CognitiveRoundService) -> None:
        started = await service.start_round()
        v0 = started.round.version

        first = await service.transition(started.round.id, RoundState.TRIAGING, reason="triaged")
        second = await service.transition(started.round.id, RoundState.FRAMING, reason="framed")

        assert first.round.version == v0 + 1
        assert second.round.version == v0 + 2

    async def test_started_at_is_set_when_leaving_created(
        self, service: CognitiveRoundService
    ) -> None:
        started = await service.start_round()
        assert started.round.started_at is None

        moved = await service.transition(started.round.id, RoundState.TRIAGING, reason="go")
        assert moved.round.started_at is not None


class TestInvariant17IllegalTransitions:
    """🔴 阶段 2 验收：非法状态转换全部拒绝。"""

    @pytest.mark.parametrize(
        "target",
        [
            RoundState.ANALYZING,
            RoundState.SYNTHESIZING,
            RoundState.COMPLETED,
            RoundState.RESPONDING,
        ],
    )
    async def test_illegal_jump_from_created_is_rejected(
        self, service: CognitiveRoundService, target: RoundState
    ) -> None:
        started = await service.start_round()
        with pytest.raises(IllegalStateTransitionError):
            await service.transition(started.round.id, target, reason="jump")

    async def test_terminal_state_is_frozen(self, service: CognitiveRoundService) -> None:
        started = await service.start_round()
        await _walk_to(
            service,
            started.round.id,
            RoundState.TRIAGING,
            RoundState.FRAMING,
            RoundState.RETRIEVING,
            RoundState.ANALYZING,
            RoundState.DELIBERATING,
            RoundState.METACOGNITIVE_REVIEW,
            RoundState.SYNTHESIZING,
            RoundState.RESPONDING,
        )
        await service.complete_round(started.round.id, stop_reason="done")

        with pytest.raises(IllegalStateTransitionError, match="终态"):
            await service.transition(started.round.id, RoundState.ANALYZING, reason="revive")

    async def test_rejected_transition_writes_nothing(
        self, service: CognitiveRoundService, uow_factory: UnitOfWorkFactory
    ) -> None:
        """🔴 拒绝必须发生在**任何写入之前**。"""
        started = await service.start_round()
        async with uow_factory() as uow:
            before = await uow.events.read_stream(cognitive_round_id=started.round.id)
            version_before = started.round.version

        with pytest.raises(IllegalStateTransitionError):
            await service.transition(started.round.id, RoundState.COMPLETED, reason="jump")

        async with uow_factory() as uow:
            after = await uow.events.read_stream(cognitive_round_id=started.round.id)
            current = await uow.rounds.get(started.round.id)

        assert len(after) == len(before), "被拒绝的转移不得留下事件"
        assert current is not None
        assert current.state is RoundState.CREATED
        assert current.version == version_before, "被拒绝的转移不得推进版本号"

    async def test_unknown_round_is_not_found(self, service: CognitiveRoundService) -> None:
        with pytest.raises(NotFoundError):
            await service.transition(uuid4(), RoundState.TRIAGING, reason="x")


class TestInvariant19And20TerminalDiagnostics:
    async def test_completing_without_stop_reason_is_rejected_by_domain(
        self, service: CognitiveRoundService
    ) -> None:
        """不变量 19 的第一道防线：领域模型校验器。"""
        started = await service.start_round()
        await _walk_to(
            service,
            started.round.id,
            RoundState.TRIAGING,
            RoundState.FRAMING,
            RoundState.RETRIEVING,
            RoundState.ANALYZING,
            RoundState.DELIBERATING,
            RoundState.METACOGNITIVE_REVIEW,
            RoundState.SYNTHESIZING,
            RoundState.RESPONDING,
        )
        with pytest.raises(Exception, match="不变量 19"):
            await service.transition(
                started.round.id, RoundState.COMPLETED, reason="no reason given"
            )

    async def test_fail_round_records_diagnostics(self, service: CognitiveRoundService) -> None:
        started = await service.start_round()
        failed = await service.fail_round(
            started.round.id,
            stage="analyzing",
            error_category=ErrorType.PROCESS_ERROR,
            reason="provider timeout",
        )
        assert failed.round.state is RoundState.FAILED
        assert failed.round.failure_stage == "analyzing"
        assert failed.round.error_category is ErrorType.PROCESS_ERROR

    async def test_failure_does_not_destroy_prior_events(
        self, service: CognitiveRoundService, uow_factory: UnitOfWorkFactory
    ) -> None:
        """任务书 §6.3：回合失败不得破坏已写入事件。"""
        started = await service.start_round()
        await service.transition(started.round.id, RoundState.TRIAGING, reason="triaged")
        await service.fail_round(
            started.round.id,
            stage="triaging",
            error_category=ErrorType.PROCESS_ERROR,
            reason="boom",
        )

        async with uow_factory() as uow:
            events = await uow.events.read_stream(cognitive_round_id=started.round.id)

        # 🔴 终态会额外写一条**具名**事件（阶段 3 补齐，见 ADR-0015）：
        # 只有 state_changed 的话，"找出所有失败回合"必须解析每条负载。
        # 关键是：先前的事件一条都没少，也没有被改写。
        assert [e.event_type for e in events] == [
            EventType.COGNITIVE_ROUND_STARTED,
            EventType.COGNITIVE_ROUND_STATE_CHANGED,
            EventType.COGNITIVE_ROUND_STATE_CHANGED,
            EventType.COGNITIVE_ROUND_FAILED,
        ]


class TestOptimisticLocking:
    async def test_stale_save_is_rejected(
        self, uow_factory: UnitOfWorkFactory, service: CognitiveRoundService
    ) -> None:
        """🔴 绝不静默覆盖：版本不匹配必须报错。"""
        started = await service.start_round()
        round_id = started.round.id

        async with uow_factory() as uow:
            stale = await uow.rounds.get(round_id)
            assert stale is not None

            # 另一路写入者先提交了一次变更
            await service.transition(round_id, RoundState.TRIAGING, reason="first")

            # 持有旧版本的一方再提交 → 必须被拒绝
            with pytest.raises(OptimisticLockError) as excinfo:
                await uow.rounds.save(
                    stale.bumped(state=RoundState.FRAMING), expected_version=stale.version
                )
            await uow.rollback()

        assert excinfo.value.expected_version == stale.version

    async def test_missing_round_raises_not_found(self, uow_factory: UnitOfWorkFactory) -> None:
        from ai_psi.domain.cognitive_rounds import CognitiveRound

        ghost = CognitiveRound(created_by="test")
        async with uow_factory() as uow:
            with pytest.raises(NotFoundError):
                await uow.rounds.save(ghost, expected_version=1)
            await uow.rollback()


class TestIdempotency:
    """任务书 §13.4：API 重试不得创建重复回合。"""

    async def test_same_key_same_body_returns_existing_round(
        self, service: CognitiveRoundService, uow_factory: UnitOfWorkFactory
    ) -> None:
        first = await service.start_round(idempotency_key="key-1", request_hash="hash-a")
        second = await service.start_round(idempotency_key="key-1", request_hash="hash-a")

        assert first.created is True
        assert second.created is False
        assert second.round.id == first.round.id
        assert second.event is None

        async with uow_factory() as uow:
            events = await uow.events.read_stream(cognitive_round_id=first.round.id)
        assert len(events) == 1, "重放不得追加第二个 started 事件"

    async def test_same_key_different_body_conflicts(self, service: CognitiveRoundService) -> None:
        await service.start_round(idempotency_key="key-2", request_hash="hash-a")
        with pytest.raises(ConflictError, match="幂等键冲突"):
            await service.start_round(idempotency_key="key-2", request_hash="hash-b")

    async def test_different_keys_create_different_rounds(
        self, service: CognitiveRoundService
    ) -> None:
        a = await service.start_round(idempotency_key="key-3", request_hash="h")
        b = await service.start_round(idempotency_key="key-4", request_hash="h")
        assert a.round.id != b.round.id

    async def test_no_key_creates_independent_rounds(self, service: CognitiveRoundService) -> None:
        a = await service.start_round()
        b = await service.start_round()
        assert a.round.id != b.round.id


class TestReplay:
    """🔴 阶段 2 验收：可以创建和回放事件。"""

    async def test_replay_reconstructs_current_state(
        self, service: CognitiveRoundService, replay: ReplayService
    ) -> None:
        started = await service.start_round()
        round_id = started.round.id
        await _walk_to(
            service,
            round_id,
            RoundState.TRIAGING,
            RoundState.FRAMING,
            RoundState.RETRIEVING,
        )

        result = await replay.replay_round(round_id)

        assert result.projection.state is RoundState.RETRIEVING
        assert result.projection.transition_count == 3
        assert result.differs_from_projection is False

    async def test_replay_reproduces_transition_chain(
        self, service: CognitiveRoundService, replay: ReplayService
    ) -> None:
        started = await service.start_round()
        await _walk_to(service, started.round.id, RoundState.TRIAGING, RoundState.FRAMING)

        result = await replay.replay_round(started.round.id)
        chain = [(t.from_state, t.to_state) for t in result.projection.transitions]

        assert chain == [
            (RoundState.CREATED, RoundState.TRIAGING),
            (RoundState.TRIAGING, RoundState.FRAMING),
        ]

    async def test_replay_recovers_terminal_diagnostics(
        self, service: CognitiveRoundService, replay: ReplayService
    ) -> None:
        started = await service.start_round()
        await service.fail_round(
            started.round.id,
            stage="analyzing",
            error_category=ErrorType.REASONING_ERROR,
            reason="bad inference",
        )

        result = await replay.replay_round(started.round.id)
        assert result.projection.state is RoundState.FAILED
        assert result.projection.failure_stage == "analyzing"
        assert result.projection.error_category is ErrorType.REASONING_ERROR

    async def test_replay_is_read_only(
        self, service: CognitiveRoundService, replay: ReplayService, uow_factory: UnitOfWorkFactory
    ) -> None:
        """回放不产生新事件，也不改动状态。"""
        started = await service.start_round()
        await _walk_to(service, started.round.id, RoundState.TRIAGING)

        async with uow_factory() as uow:
            before = await uow.events.count()

        await replay.replay_round(started.round.id)
        await replay.replay_round(started.round.id)

        async with uow_factory() as uow:
            after = await uow.events.count()
        assert after == before

    async def test_replay_of_empty_stream_is_not_found(self, replay: ReplayService) -> None:
        with pytest.raises(NotFoundError, match="没有事件"):
            await replay.replay_round(uuid4())

    async def test_replay_reports_a_consistent_projection(
        self, service: CognitiveRoundService, replay: ReplayService
    ) -> None:
        """🔴 **回放同时是一致性检查**（阶段 6.5 §四接通了这条告警）。

        ``differs_from_projection`` 说的是"从事件流重建出的状态
        与当前状态表是否一致"。它此前**算出来了但没有任何出口**——
        路由内联调 `project_round`，把这个信号丢掉了。
        一条实现了却读不到的告警，与没有这条告警是一样的。

        正常路径下它必须恒为 ``False``：为 ``True`` 意味着
        有写入绕过了应用服务。
        """
        started = await service.start_round()
        round_id = started.round.id
        await service.transition(round_id, RoundState.TRIAGING, reason="go")

        result = await replay.replay_round(round_id)

        assert result.differs_from_projection is False
        assert result.projection.state is RoundState.TRIAGING
        assert result.projection.transition_count == 1


class TestTransactionAtomicity:
    """🔴 阶段 2 验收：事务失败不会留下半成品数据。"""

    async def test_uncommitted_uow_writes_nothing(
        self,
        uow_factory: UnitOfWorkFactory,
        session_factory: async_sessionmaker[AsyncSession],
        engine: AsyncEngine,
    ) -> None:
        """忘记 commit → 什么都不落库。"""
        from ai_psi.domain.cognitive_rounds import CognitiveRound

        ghost = CognitiveRound(created_by="test")

        async with uow_factory() as uow:
            await uow.rounds.add(ghost)
            # 故意不调用 commit()

        async with uow_factory() as uow:
            assert await uow.rounds.get(ghost.id) is None

    async def test_exception_inside_uow_rolls_back(self, uow_factory: UnitOfWorkFactory) -> None:
        """异常路径一律回滚——这是"不留半成品"的全部实现。"""
        from ai_psi.domain.cognitive_rounds import CognitiveRound

        ghost = CognitiveRound(created_by="test")

        with pytest.raises(RuntimeError, match="boom"):
            async with uow_factory() as uow:
                await uow.rounds.add(ghost)
                raise RuntimeError("boom")

        async with uow_factory() as uow:
            assert await uow.rounds.get(ghost.id) is None

    async def test_failed_transition_does_not_corrupt_prior_commits(
        self, service: CognitiveRoundService, uow_factory: UnitOfWorkFactory
    ) -> None:
        """已提交的回合不受后续失败操作影响。"""
        started = await service.start_round()
        await service.transition(started.round.id, RoundState.TRIAGING, reason="ok")

        with pytest.raises(IllegalStateTransitionError):
            await service.transition(started.round.id, RoundState.COMPLETED, reason="bad")

        async with uow_factory() as uow:
            current = await uow.rounds.get(started.round.id)
            events = await uow.events.read_stream(cognitive_round_id=started.round.id)

        assert current is not None
        assert current.state is RoundState.TRIAGING
        assert len(events) == 2

    async def test_round_and_event_are_written_together(
        self, service: CognitiveRoundService, uow_factory: UnitOfWorkFactory
    ) -> None:
        """事件与投影在同一事务中提交（ADR-0002）。"""
        started = await service.start_round()
        await service.transition(started.round.id, RoundState.TRIAGING, reason="go")

        async with uow_factory() as uow:
            current = await uow.rounds.get(started.round.id)
            events = await uow.events.read_stream(cognitive_round_id=started.round.id)

        # 投影推进了 1 步，事件也恰好多了 1 条——两者不会各自漂移
        assert current is not None
        assert current.version == started.round.version + 1
        assert len(events) == 2


class TestDatabaseLevelInvariants:
    """不变量在**数据库层**再次强制——应用层有 bug 时的最后一道防线。"""

    async def test_db_rejects_completed_without_stop_reason(self, engine: AsyncEngine) -> None:
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        async with engine.begin() as conn:
            with pytest.raises(IntegrityError, match="completed_requires_stop_reason"):
                await conn.execute(
                    text(
                        "INSERT INTO cognitive_rounds (id, created_at, updated_at, version,"
                        " created_by, schema_version, state, depth_level, budget,"
                        " model_calls_used, metacognitive_loops)"
                        " VALUES (:id, now(), now(), 1, 'test', '1.0.0', 'completed', 'd0',"
                        " '{\"max_model_calls\": 3}'::jsonb, 0, 0)"
                    ),
                    {"id": uuid4()},
                )

    async def test_db_rejects_failed_without_diagnostics(self, engine: AsyncEngine) -> None:
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        async with engine.begin() as conn:
            with pytest.raises(IntegrityError, match="failed_requires_diagnostics"):
                await conn.execute(
                    text(
                        "INSERT INTO cognitive_rounds (id, created_at, updated_at, version,"
                        " created_by, schema_version, state, depth_level, budget,"
                        " model_calls_used, metacognitive_loops)"
                        " VALUES (:id, now(), now(), 1, 'test', '1.0.0', 'failed', 'd0',"
                        " '{\"max_model_calls\": 3}'::jsonb, 0, 0)"
                    ),
                    {"id": uuid4()},
                )

    async def test_db_rejects_over_budget_row(self, engine: AsyncEngine) -> None:
        from sqlalchemy import text
        from sqlalchemy.exc import IntegrityError

        async with engine.begin() as conn:
            with pytest.raises(IntegrityError, match="model_calls_within_budget"):
                await conn.execute(
                    text(
                        "INSERT INTO cognitive_rounds (id, created_at, updated_at, version,"
                        " created_by, schema_version, state, depth_level, budget,"
                        " model_calls_used, metacognitive_loops)"
                        " VALUES (:id, now(), now(), 1, 'test', '1.0.0', 'created', 'd0',"
                        " '{\"max_model_calls\": 3}'::jsonb, 99, 0)"
                    ),
                    {"id": uuid4()},
                )
