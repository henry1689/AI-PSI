"""存储 Port 的契约定义（**不是**可收集的测试类）。

每个类描述一组"任何实现都必须满足"的语义。子类只需提供同名 fixture，
断言语义由基类承担——因此内存实现与 PostgreSQL 实现跑的是**同一组断言**。

⚠️ 基类刻意不以 ``Test`` 开头：pytest 只收集 ``Test*`` 开头的类，
而这几个类自己是不完整的（缺 fixture），被收集会直接报错。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from ai_psi.application.ports import IdempotencyOutcome
from ai_psi.domain.cognitive_rounds import CognitiveBudget, CognitiveRound
from ai_psi.domain.enums import CognitiveDepth, EventType, MemoryStatus, MemoryType
from ai_psi.domain.events import Event
from ai_psi.domain.exceptions import (
    ConflictError,
    NotFoundError,
    OptimisticLockError,
)
from ai_psi.domain.memories import Memory

__all__ = [
    "EventStoreContract",
    "IdempotencyContract",
    "MemoryRepositoryContract",
    "RoundRepositoryContract",
    "UnitOfWorkContract",
]


def _event(round_id, **overrides: object) -> Event:
    payload: dict[str, object] = {
        "event_type": EventType.USER_MESSAGE_RECEIVED,
        "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
        "actor_type": "system",
        "actor_id": "test",
        "cognitive_round_id": round_id,
    }
    payload.update(overrides)
    return Event(**payload)  # type: ignore[arg-type]


def _round(**overrides: object) -> CognitiveRound:
    payload: dict[str, object] = {
        "created_by": "test",
        "depth_level": CognitiveDepth.D1,
        "budget": CognitiveBudget.for_depth(CognitiveDepth.D1),
    }
    payload.update(overrides)
    return CognitiveRound(**payload)  # type: ignore[arg-type]


def _memory(**overrides: object) -> Memory:
    payload: dict[str, object] = {
        "created_by": "test",
        "memory_type": MemoryType.USER_PREFERENCE,
        "content": "用户偏好简洁回答",
        "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
        "status": MemoryStatus.ACTIVE,
    }
    payload.update(overrides)
    return Memory(**payload)  # type: ignore[arg-type]


class EventStoreContract:
    """事件存储的语义契约。

    🔴 **只追加。** 本契约刻意不测"更新事件"——因为 Port 上根本没有那个方法。
    """

    async def test_append_then_read(self, event_store) -> None:
        round_id = uuid4()
        event = _event(round_id)
        await event_store.append(event)
        assert [item.id for item in await event_store.read_stream(cognitive_round_id=round_id)] == [
            event.id
        ]

    async def test_events_are_ordered_by_sequence(self, event_store) -> None:
        """🔴 顺序由 ``sequence`` 决定，**不是时间戳**。

        同一微秒内的多次写入用时间戳排序会得到不确定结果，
        而"回放可复现"正是建立在顺序确定之上。
        """
        round_id = uuid4()
        events = [_event(round_id) for _ in range(5)]
        await event_store.append_many(events)
        read = await event_store.read_stream(cognitive_round_id=round_id)
        assert [item.id for item in read] == [item.id for item in events]

    async def test_batch_is_atomic_on_duplicate_ids(self, event_store) -> None:
        round_id = uuid4()
        duplicate = _event(round_id)
        with pytest.raises(ConflictError):
            await event_store.append_many([duplicate, duplicate])

    async def test_duplicate_event_id_is_rejected(self, event_store) -> None:
        round_id = uuid4()
        event = _event(round_id)
        await event_store.append(event)
        with pytest.raises(ConflictError):
            await event_store.append(event)

    async def test_read_since_is_incremental(self, event_store) -> None:
        round_id = uuid4()
        await event_store.append(_event(round_id))
        cursor = await event_store.latest_sequence_for_round(cognitive_round_id=round_id)
        await event_store.append(_event(round_id))
        remaining = await event_store.read_stream(
            cognitive_round_id=round_id, after_sequence=cursor
        )
        assert len(remaining) == 1

    async def test_streams_are_isolated_by_round(self, event_store) -> None:
        first, second = uuid4(), uuid4()
        await event_store.append_many([_event(first), _event(second)])
        assert len(await event_store.read_stream(cognitive_round_id=first)) == 1

    async def test_correlation_lookup(self, event_store) -> None:
        correlation = uuid4()
        await event_store.append_many(
            [_event(uuid4(), correlation_id=correlation) for _ in range(3)]
        )
        assert len(await event_store.read_by_correlation(correlation_id=correlation)) == 3

    async def test_latest_sequence_grows(self, event_store) -> None:
        before = await event_store.latest_sequence()
        await event_store.append(_event(uuid4()))
        assert await event_store.latest_sequence() > before

    async def test_latest_sequence_for_unknown_round_is_zero(self, event_store) -> None:
        assert await event_store.latest_sequence_for_round(cognitive_round_id=uuid4()) == 0

    async def test_count_matches_appends(self, event_store) -> None:
        before = await event_store.count()
        await event_store.append(_event(uuid4()))
        assert await event_store.count() == before + 1


class RoundRepositoryContract:
    """回合仓储的语义契约。"""

    async def test_add_and_get(self, round_repository) -> None:
        round_ = _round()
        await round_repository.add(round_)
        stored = await round_repository.get(round_.id)
        assert stored is not None
        assert stored.id == round_.id

    async def test_get_unknown_returns_none(self, round_repository) -> None:
        assert await round_repository.get(uuid4()) is None

    async def test_duplicate_add_is_rejected(self, round_repository) -> None:
        round_ = _round()
        await round_repository.add(round_)
        with pytest.raises(ConflictError):
            await round_repository.add(round_)

    async def test_save_bumps_version(self, round_repository) -> None:
        round_ = _round()
        await round_repository.add(round_)
        updated = round_.bumped(stop_reason=None)
        await round_repository.save(updated, expected_version=round_.version)
        stored = await round_repository.get(round_.id)
        assert stored is not None
        assert stored.version == round_.version + 1

    async def test_stale_version_raises_instead_of_overwriting(self, round_repository) -> None:
        """🔴 **绝不静默覆盖。**

        乐观锁冲突是正常业务路径，必须被明确报告——
        静默覆盖是并发场景下最难排查的一类数据损坏（ADR-0002）。
        """
        round_ = _round()
        await round_repository.add(round_)
        with pytest.raises(OptimisticLockError):
            await round_repository.save(round_.bumped(), expected_version=round_.version + 5)

    async def test_save_unknown_round_raises_not_found(self, round_repository) -> None:
        with pytest.raises(NotFoundError):
            await round_repository.save(_round(), expected_version=1)

    async def test_find_by_idempotency_key(self, round_repository) -> None:
        round_ = _round(idempotency_key="key-1")
        await round_repository.add(round_)
        found = await round_repository.find_by_idempotency_key("key-1")
        assert found is not None
        assert found.id == round_.id

    async def test_find_by_unknown_idempotency_key(self, round_repository) -> None:
        assert await round_repository.find_by_idempotency_key("不存在") is None


class IdempotencyContract:
    """幂等键存储的语义契约（任务书 §13.4）。"""

    async def test_first_reservation_is_reserved(self, idempotency_store) -> None:
        reservation = await idempotency_store.reserve(key="k", request_hash="h")
        assert reservation.outcome is IdempotencyOutcome.RESERVED

    async def test_same_key_different_body_is_conflict(self, idempotency_store) -> None:
        """同一 key 搭配不同请求体是**客户端 bug**，不能静默返回旧结果。"""
        await idempotency_store.reserve(key="k", request_hash="h1")
        reservation = await idempotency_store.reserve(key="k", request_hash="h2")
        assert reservation.outcome is IdempotencyOutcome.CONFLICT

    async def test_unbound_key_is_conflict(self, idempotency_store) -> None:
        """占位成功但尚未绑定回合 → 上一次请求仍在进行中，调用方应退避重试。"""
        await idempotency_store.reserve(key="k", request_hash="h")
        reservation = await idempotency_store.reserve(key="k", request_hash="h")
        assert reservation.outcome is IdempotencyOutcome.CONFLICT

    async def test_bound_key_replays(self, idempotency_store) -> None:
        round_id = uuid4()
        await idempotency_store.reserve(key="k", request_hash="h")
        await idempotency_store.bind(key="k", cognitive_round_id=round_id)
        reservation = await idempotency_store.reserve(key="k", request_hash="h")
        assert reservation.outcome is IdempotencyOutcome.REPLAY
        assert reservation.cognitive_round_id == round_id

    async def test_binding_unknown_key_is_rejected(self, idempotency_store) -> None:
        with pytest.raises(ConflictError):
            await idempotency_store.bind(key="从未占位", cognitive_round_id=uuid4())


class MemoryRepositoryContract:
    """长期记忆仓储的语义契约（不变量 14、15）。"""

    async def test_add_and_get(self, memory_repository) -> None:
        memory = _memory()
        await memory_repository.add(memory)
        assert await memory_repository.get(memory.id) is not None

    async def test_duplicate_add_is_rejected(self, memory_repository) -> None:
        memory = _memory()
        await memory_repository.add(memory)
        with pytest.raises(ConflictError):
            await memory_repository.add(memory)

    async def test_retrieve_is_scoped_to_user(self, memory_repository) -> None:
        """🔴 不变量 14：检索必须遵守 user_id 作用域。"""
        mine, theirs = uuid4(), uuid4()
        await memory_repository.add(_memory(user_id=mine, content="我的偏好是简洁回答"))
        await memory_repository.add(_memory(user_id=theirs, content="他的偏好是简洁回答"))

        mine_results = await memory_repository.retrieve(user_id=mine, query="简洁回答", limit=10)
        assert [item.user_id for item in mine_results] == [mine]

    async def test_retrieve_excludes_non_default_statuses(self, memory_repository) -> None:
        """🔴 不变量 6：被取代的记忆不能作为默认有效记忆返回。"""
        user = uuid4()
        await memory_repository.add(_memory(user_id=user, status=MemoryStatus.SUPERSEDED))
        await memory_repository.add(_memory(user_id=user, status=MemoryStatus.EXPIRED))
        assert await memory_repository.retrieve(user_id=user, query="偏好", limit=10) == []

    async def test_deleted_memory_disappears_from_retrieval(self, memory_repository) -> None:
        """🔴 不变量 15：删除必须同时作用于检索索引。"""
        user = uuid4()
        memory = _memory(user_id=user)
        await memory_repository.add(memory)
        assert await memory_repository.retrieve(user_id=user, query="偏好", limit=10)
        await memory_repository.delete(memory.id)
        assert await memory_repository.retrieve(user_id=user, query="偏好", limit=10) == []

    async def test_deletion_is_marked_not_erased(self, memory_repository) -> None:
        """逻辑删除：本体保留以便追溯"为什么发生过修正"（§10.4）。"""
        memory = _memory()
        await memory_repository.add(memory)
        await memory_repository.delete(memory.id)
        stored = await memory_repository.get(memory.id)
        assert stored is not None
        assert stored.status is MemoryStatus.DELETED

    async def test_delete_unknown_raises(self, memory_repository) -> None:
        with pytest.raises(NotFoundError):
            await memory_repository.delete(uuid4())

    async def test_list_for_user_can_include_inactive(self, memory_repository) -> None:
        user = uuid4()
        await memory_repository.add(_memory(user_id=user, content="当前偏好"))
        old = _memory(user_id=user, content="旧偏好")
        await memory_repository.add(old)
        await memory_repository.delete(old.id)

        assert len(await memory_repository.list_for_user(user_id=user)) == 1
        assert len(await memory_repository.list_for_user(user_id=user, include_inactive=True)) == 2

    async def test_supersede_chain(self, memory_repository) -> None:
        """🔴 不变量 5：用户纠正生成新版本，而不是就地覆盖。"""
        user = uuid4()
        old = _memory(user_id=user, content="喜欢详细回答")
        await memory_repository.add(old)

        replacement = _memory(user_id=user, content="喜欢简洁回答", supersedes_id=old.id)
        await memory_repository.add(replacement)
        await memory_repository.save(
            old.superseded_by(replacement_id=replacement.id),
            expected_version=old.version,
        )

        active = await memory_repository.retrieve(user_id=user, query="回答", limit=10)
        assert [item.content for item in active] == ["喜欢简洁回答"]
        assert replacement.supersedes_id == old.id

    async def test_retrieve_respects_limit(self, memory_repository) -> None:
        user = uuid4()
        for index in range(5):
            await memory_repository.add(_memory(user_id=user, content=f"偏好 {index}"))
        assert len(await memory_repository.retrieve(user_id=user, query="偏好", limit=2)) == 2

    async def test_stale_save_raises(self, memory_repository) -> None:
        memory = _memory()
        await memory_repository.add(memory)
        with pytest.raises(OptimisticLockError):
            await memory_repository.save(memory.bumped(), expected_version=memory.version + 3)


class UnitOfWorkContract:
    """工作单元的语义契约（任务书阶段 2 验收条件）。"""

    async def test_uncommitted_writes_are_discarded(self, uow_factory) -> None:
        """🔴 **事务失败不得留下半成品数据。**"""
        round_id = uuid4()
        async with uow_factory() as uow:
            await uow.events.append(_event(round_id))
            # 不调用 commit()

        async with uow_factory() as uow:
            assert await uow.events.read_stream(cognitive_round_id=round_id) == []

    async def test_exception_discards_everything(self, uow_factory) -> None:
        round_id = uuid4()
        round_ = _round()
        with pytest.raises(RuntimeError):
            async with uow_factory() as uow:
                await uow.rounds.add(round_)
                await uow.events.append(_event(round_.id))
                msg = "中途失败"
                raise RuntimeError(msg)

        async with uow_factory() as uow:
            assert await uow.rounds.get(round_.id) is None
            assert await uow.events.read_stream(cognitive_round_id=round_id) == []

    async def test_commit_persists_everything(self, uow_factory) -> None:
        round_ = _round()
        async with uow_factory() as uow:
            await uow.rounds.add(round_)
            await uow.events.append(_event(round_.id))
            await uow.commit()

        async with uow_factory() as uow:
            assert await uow.rounds.get(round_.id) is not None
            assert len(await uow.events.read_stream(cognitive_round_id=round_.id)) == 1

    async def test_read_your_writes(self, uow_factory) -> None:
        round_ = _round()
        async with uow_factory() as uow:
            await uow.rounds.add(round_)
            assert await uow.rounds.get(round_.id) is not None

    async def test_rollback_discards_staged_writes(self, uow_factory) -> None:
        round_ = _round()
        async with uow_factory() as uow:
            await uow.rounds.add(round_)
            await uow.rollback()
        async with uow_factory() as uow:
            assert await uow.rounds.get(round_.id) is None

    async def test_uncommitted_memory_writes_are_discarded(self, uow_factory) -> None:
        """🔴 记忆也必须在工作单元内（阶段 5 引入）。

        阶段 3 时记忆不在工作单元里，"取代旧记忆 + 写入新记忆 + 记录事件"
        跨在三个事务上；这条断言钉住的是修复之后的语义。
        """
        memory = _memory()
        async with uow_factory() as uow:
            await uow.memories.add(memory)
            # 不调用 commit()

        async with uow_factory() as uow:
            assert await uow.memories.get(memory.id) is None

    async def test_committed_memory_persists_with_its_index(self, uow_factory) -> None:
        """🔴 提交后**本体与向量索引都要在**。

        只提交本体、索引没跟上，会让一条刚刚写好的记忆立刻检索不到——
        而它既没有报错，也没有任何地方能看出索引缺了一行。
        """
        user = uuid4()
        memory = _memory(user_id=user, content="用户偏好简洁回答")
        async with uow_factory() as uow:
            await uow.memories.add(memory)
            await uow.commit()

        async with uow_factory() as uow:
            assert await uow.memories.get(memory.id) is not None
            found = await uow.memories.retrieve(user_id=user, query="简洁回答", limit=5)
            assert [item.id for item in found] == [memory.id]

    async def test_memory_deletion_propagates_to_the_index(self, uow_factory) -> None:
        """🔴 不变量 15 的**直接证据**：索引里真的没有它了。

        ``retrieve`` 返回空可以有很多原因（相似度、limit、状态过滤），
        而这条断言问的是索引本身——"删了还检索得到"必须是不可能的，
        不能只是"查询恰好没查到"。
        """
        user = uuid4()
        memory = _memory(user_id=user)
        async with uow_factory() as uow:
            await uow.memories.add(memory)
            await uow.commit()

        async with uow_factory() as uow:
            assert memory.id in await uow.memories.indexed_ids()
            await uow.memories.delete(memory.id)
            await uow.commit()

        async with uow_factory() as uow:
            assert memory.id not in await uow.memories.indexed_ids()
            # 本体保留（逻辑删除），索引移除——两者生命周期不同
            stored = await uow.memories.get(memory.id)
            assert stored is not None
            assert stored.status is MemoryStatus.DELETED

    async def test_sequence_does_not_rewind_after_rollback(self, uow_factory) -> None:
        """🔴 回滚后序号**跳过而不是回退**。

        真实的数据库序列（PostgreSQL 的 ``serial``）在事务回滚后同样会留下空洞，
        且不会回收那个号。把内存实现写成"回退"会让两个实现
        在最不该有差异的地方产生差异——而那正是契约测试要防的事。

        这里先丢弃一次写入，再提交一次；如果实现会回收序号，
        提交的那条就会拿到 1，而不是 ≥ 2。
        """
        round_id = uuid4()
        async with uow_factory() as uow:
            await uow.events.append(_event(round_id))  # 未提交 → 丢弃

        async with uow_factory() as uow:
            await uow.events.append(_event(round_id))
            await uow.commit()

        assert await _latest_for_round(uow_factory, round_id) >= 2


async def _latest_for_round(uow_factory, round_id) -> int:
    async with uow_factory() as uow:
        value = await uow.events.latest_sequence_for_round(cognitive_round_id=round_id)
    return int(value)
