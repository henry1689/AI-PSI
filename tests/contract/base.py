"""存储 Port 的契约定义（**不是**可收集的测试类）。

每个类描述一组"任何实现都必须满足"的语义。子类只需提供同名 fixture，
断言语义由基类承担——因此内存实现与 PostgreSQL 实现跑的是**同一组断言**。

⚠️ 基类刻意不以 ``Test`` 开头：pytest 只收集 ``Test*`` 开头的类，
而这几个类自己是不完整的（缺 fixture），被收集会直接报错。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ai_psi.application.ports import IdempotencyOutcome
from ai_psi.domain.cognitive_rounds import CognitiveBudget, CognitiveRound
from ai_psi.domain.enums import (
    CognitiveDepth,
    ErrorType,
    EventType,
    MemoryStatus,
    MemoryType,
    ProposalStatus,
)
from ai_psi.domain.events import Event
from ai_psi.domain.exceptions import (
    ConflictError,
    ConstitutionViolationError,
    NotFoundError,
    OptimisticLockError,
    ProposalPatternConflictError,
)
from ai_psi.domain.improvement_proposals import ImprovementProposal
from ai_psi.domain.memories import Memory

__all__ = [
    "EventStoreContract",
    "IdempotencyContract",
    "MemoryRepositoryContract",
    "ProposalRepositoryContract",
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

    async def test_read_by_event_type_filters(self, event_store) -> None:
        """🔴 模式发现（任务书 §11.3）靠这条查询跨回合统计。

        回合是一段段独立的事件流，按回合读拿不到全局视图。
        """
        round_id = uuid4()
        wanted = [_event(round_id, event_type=EventType.EXPERIENCE_CREATED) for _ in range(3)]
        await event_store.append_many(
            [
                wanted[0],
                _event(round_id, event_type=EventType.HYPOTHESIS_CREATED),
                wanted[1],
                _event(round_id, event_type=EventType.RESPONSE_GENERATED),
                wanted[2],
            ]
        )

        found = await event_store.read_by_event_type(event_type=EventType.EXPERIENCE_CREATED)
        assert [item.id for item in found] == [item.id for item in wanted]

    async def test_read_by_event_type_is_ordered_by_sequence(self, event_store) -> None:
        """顺序由 ``sequence`` 决定——按时间戳排序在同一微秒内是不确定的。"""
        round_id = uuid4()
        events = [_event(round_id, event_type=EventType.MEMORY_APPROVED) for _ in range(4)]
        await event_store.append_many(events)
        found = await event_store.read_by_event_type(event_type=EventType.MEMORY_APPROVED)
        assert [item.id for item in found] == [item.id for item in events]

    async def test_read_by_event_type_limit_takes_the_earliest(self, event_store) -> None:
        """🔴 ``limit`` 取的是**最早的 N 条**（顺序是 sequence 升序）。

        存储层不替调用方猜它要哪一头：要"最近 N 条"就先取
        ``latest_sequence()`` 再自行截断。把两头都做成隐式选项，
        迟早会有人以为自己在取最近的历史、实际拿到的是最早的。
        """
        round_id = uuid4()
        events = [_event(round_id, event_type=EventType.MEMORY_EXPIRED) for _ in range(5)]
        await event_store.append_many(events)
        found = await event_store.read_by_event_type(event_type=EventType.MEMORY_EXPIRED, limit=2)
        assert [item.id for item in found] == [item.id for item in events[:2]]

    async def test_read_by_event_type_unknown_returns_empty(self, event_store) -> None:
        await event_store.append(_event(uuid4()))
        assert (
            await event_store.read_by_event_type(
                event_type=EventType.IMPROVEMENT_PROPOSAL_EVALUATED
            )
            == []
        )


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

    async def test_list_all_is_empty_when_there_are_no_rounds(self, round_repository) -> None:
        assert await round_repository.list_all() == []

    async def test_list_all_returns_rounds_in_time_order(self, round_repository) -> None:
        """🔴 **顺序是契约的一部分**（阶段 6.5 §四新增）。

        离线评测要把历史切成"基线"与"候选"两段；顺序不确定的话，
        同一个输入两次运行会得到不同的对照结果——而那种不一致
        不会报错，只会让每一次评测看起来都略有不同。

        实现被明确定义为 ``created_at`` 升序、同时刻按 id 升序。
        """
        base = datetime(2026, 1, 1, tzinfo=UTC)
        earliest = _round()
        earliest = earliest.model_copy(update={"created_at": base, "updated_at": base})
        latest = _round()
        latest = latest.model_copy(
            update={
                "created_at": base + timedelta(hours=1),
                "updated_at": base + timedelta(hours=1),
            }
        )
        # 刻意**逆序**写入：顺序若来自插入顺序，这条用例会失败
        await round_repository.add(latest)
        await round_repository.add(earliest)

        assert [item.id for item in await round_repository.list_all()] == [
            earliest.id,
            latest.id,
        ]

    async def test_list_all_respects_limit_from_the_earliest_end(self, round_repository) -> None:
        """``limit`` 取的是**最早**的 N 条——与升序顺序一致，不是另一套语义。"""
        base = datetime(2026, 1, 1, tzinfo=UTC)
        rounds = []
        for index in range(3):
            item = _round()
            rounds.append(
                item.model_copy(
                    update={
                        "created_at": base + timedelta(hours=index),
                        "updated_at": base + timedelta(hours=index),
                    }
                )
            )
        for item in reversed(rounds):
            await round_repository.add(item)

        limited = await round_repository.list_all(limit=2)
        assert [item.id for item in limited] == [rounds[0].id, rounds[1].id]

    async def test_list_all_does_not_leak_uncommitted_rounds(self, uow_factory) -> None:
        """🔴 未提交的回合不该出现在列表里——与 ``get`` 的隔离语义一致。

        ⚠️ 内存实现在事务内**看得见**自己暂存的写入（read-your-writes），
        这是有意的；但事务**放弃之后**必须看不见。这条用例钉的是后者。
        """
        round_ = _round()
        async with uow_factory() as uow:
            await uow.rounds.add(round_)
            await uow.rollback()
        async with uow_factory() as uow:
            assert await uow.rounds.list_all() == []

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

        🔴 **用的是"真实存在过"的旧版本号，不是凭空造一个。**

        阶段 6.5 §八 评审 C 指出：本用例此前传的是
        ``round_.version + 5``——一个**从未存在过**的版本。
        它当然也会被拒，但它证明的是"某个够大的数被拒绝了"，
        而不是"判断是**相等**"。把它改成真实旧版本之后，
        ``!=`` 被改成 ``>`` 这类变异体才会真的被杀死
        （``1 > 2`` 为假 → 不抛 → 红灯）。

        ``ProposalRepositoryContract`` 早就改成了这个形状，
        同一份文件里另外两处没跟上——这正是"教训只修了一处"。
        """
        round_ = _round()
        await round_repository.add(round_)

        # 先做一次**成功**的写入，让库里真的推进到 v2
        latest = await round_repository.get(round_.id)
        assert latest is not None
        await round_repository.save(latest.bumped(), expected_version=latest.version)

        # 现在拿"上一次真实存在过的" v1 再写一次
        with pytest.raises(OptimisticLockError):
            await round_repository.save(round_.bumped(), expected_version=round_.version)

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
        """与 `RoundRepositoryContract` 的那条同理（阶段 6.5 §八 评审 C）。

        此前传的是 ``memory.version + 3``——一个**从未存在过**的版本。
        改成真实旧版本之后，判据才真的是"相等"而不是"够大"。
        """
        memory = _memory()
        await memory_repository.add(memory)

        latest = await memory_repository.get(memory.id)
        assert latest is not None
        await memory_repository.save(latest.bumped(), expected_version=latest.version)

        with pytest.raises(OptimisticLockError):
            await memory_repository.save(memory.bumped(), expected_version=memory.version)


def _proposal(**overrides: object) -> ImprovementProposal:
    payload: dict[str, object] = {
        "created_by": "test",
        "target_component": "prompt:logical_analyzer",
        "observed_problem": "同类推理错误反复出现",
        "error_class": ErrorType.REASONING_ERROR,
        "proposed_change": "检查该情境下的反例检查环节",
        "expected_benefit": "降低复发率",
    }
    payload.update(overrides)
    return ImprovementProposal(**payload)  # type: ignore[arg-type]


def _forged_proposal_payload(status: str) -> dict[str, object]:
    """构造一份给 ``model_construct`` 用的完整负载。

    ``model_construct`` 不填默认值，因此每个字段都得给。
    ``status`` 刻意传**裸字符串**——这正是它要模拟的绕过方式。
    """
    base = _proposal()
    payload = base.model_dump()
    payload["status"] = status
    return payload


#: 契约测试里显式指定的时间戳基准。
#:
#: 🔴 **不能依赖"创建时自然产生的 created_at"。**
#: 一个事务里连着写三条提案时，数据库的 ``now()`` 会给出**同一个**
#: 时间戳，而内存实现拿到的是三个略有差异的时刻——两边的排序结果
#: 会在"时间戳相同"这个分支上分道扬镳，而契约测试正是用来发现这种事的。
_T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)

#: 业务模式测试用的情境签名。
#:
#: ⚠️ 形状刻意与 ``_situation_signature()`` 的真实输出一致
#: （``f"{depth}|{evidence_bucket}|h{n}"``），因为它是
#: ``applicability[0]`` 的取值——用"sig-1"这种占位符会掩盖
#: "索引表达式到底比的是哪一段、下标从几起"这类错误。
_SIGNATURE = "d2|no_evidence|h2"


class ProposalRepositoryContract:
    """改进提案仓储的语义契约（任务书 §5.12、§12.4）。

    🔴 **本契约里没有任何"让提案生效"的断言——因为 Port 上没有那个方法。**
    不是漏了，是 ``ProposalStatus`` 里根本不存在 ``ACTIVE`` 成员（不变量 11）。
    """

    async def test_add_and_get(self, proposal_repository) -> None:
        proposal = _proposal()
        await proposal_repository.add(proposal)
        stored = await proposal_repository.get(proposal.id)
        assert stored is not None
        assert stored.id == proposal.id

    async def test_get_unknown_returns_none(self, proposal_repository) -> None:
        assert await proposal_repository.get(uuid4()) is None

    async def test_duplicate_add_is_rejected(self, proposal_repository) -> None:
        proposal = _proposal()
        await proposal_repository.add(proposal)
        with pytest.raises(ConflictError):
            await proposal_repository.add(proposal)

    # ------------------------------------------------------------------
    # 阶段 7 · R72：同一个业务模式至多一条**活跃**提案
    # ------------------------------------------------------------------

    async def test_a_second_active_proposal_for_the_same_pattern_is_refused(
        self, proposal_repository
    ) -> None:
        """🔴 **这是 R72 的核心断言。**

        两个并发的学习运行会各自读到"还没有提案"的旧快照，各走完门禁与
        生成，然后在写入时才分胜负。持久化层必须放一个进来、把另一个
        挡在门外——不是"通常会挡住"，而是**只能进来一个**。

        这里用同一个事务里的两次 `add` 模拟"两个运行都读完了才写"：
        第二次必须抛 :class:`ProposalPatternConflictError`。
        """
        await proposal_repository.add(_proposal(applicability=[_SIGNATURE]))
        with pytest.raises(ProposalPatternConflictError) as caught:
            await proposal_repository.add(_proposal(applicability=[_SIGNATURE]))
        assert caught.value.context["situation_signature"] == _SIGNATURE

    async def test_the_conflict_is_also_a_plain_conflict(self, proposal_repository) -> None:
        """它继承 :class:`ConflictError`——更粗粒度的调用方仍然拦得住。"""
        await proposal_repository.add(_proposal(applicability=[_SIGNATURE]))
        with pytest.raises(ConflictError):
            await proposal_repository.add(_proposal(applicability=[_SIGNATURE]))

    async def test_a_terminal_proposal_releases_the_pattern(self, proposal_repository) -> None:
        """🔴 **钉住"只对活跃状态唯一"这个选择。**

        数据库裁决的是"至多一条**活跃**提案"。前一条进了终态之后，
        同一个模式可以再建——这正是"驳回后可重开"能成为一条策略变更
        而不是一次迁移的原因（见迁移模块文档第 3 点）。

        ⚠️ 注意它与应用层的关系：``LearningService._covered_keys()``
        **含终态**，因此系统今天仍然不会自动重新提议（R55）。
        这条断言说的是**存储层**允许它，不是"系统会这么做"。
        """
        first = _proposal(applicability=[_SIGNATURE])
        await proposal_repository.add(first)
        await proposal_repository.save(
            first.bumped(status=ProposalStatus.REJECTED), expected_version=first.version
        )

        await proposal_repository.add(_proposal(applicability=[_SIGNATURE]))

    async def test_a_different_signature_is_a_different_pattern(self, proposal_repository) -> None:
        await proposal_repository.add(_proposal(applicability=["d1|with_evidence|h2"]))
        await proposal_repository.add(_proposal(applicability=["d2|no_evidence|h1"]))

    async def test_a_different_error_class_is_a_different_pattern(
        self, proposal_repository
    ) -> None:
        await proposal_repository.add(_proposal(applicability=[_SIGNATURE]))
        await proposal_repository.add(
            _proposal(applicability=[_SIGNATURE], error_class=ErrorType.SCOPE_ERROR)
        )

    async def test_proposals_without_applicability_do_not_collide(
        self, proposal_repository
    ) -> None:
        """🔴 **空 applicability 不参与唯一性。**

        空数组下标越界得到 NULL，而 PostgreSQL 的唯一索引**允许多个
        NULL**。若不把这类行排除在索引之外，就会留下一个"看起来唯一、
        实际对它们毫无约束"的约束。这里的断言与部分谓词是同一件事的
        两侧：谓词排除它们，本用例证明排除是对的（两条可以共存），
        而方向与应用层一致——``_covered_keys()`` 同样跳过它们。
        """
        await proposal_repository.add(_proposal())
        await proposal_repository.add(_proposal())

    async def test_find_active_for_pattern_hits(self, proposal_repository) -> None:
        proposal = _proposal(applicability=[_SIGNATURE])
        await proposal_repository.add(proposal)
        found = await proposal_repository.find_active_for_pattern(
            error_class=ErrorType.REASONING_ERROR, situation_signature=_SIGNATURE
        )
        assert found is not None
        assert found.id == proposal.id

    async def test_find_active_for_pattern_ignores_terminal(self, proposal_repository) -> None:
        first = _proposal(applicability=[_SIGNATURE])
        await proposal_repository.add(first)
        await proposal_repository.save(
            first.bumped(status=ProposalStatus.REJECTED), expected_version=first.version
        )
        assert (
            await proposal_repository.find_active_for_pattern(
                error_class=ErrorType.REASONING_ERROR, situation_signature=_SIGNATURE
            )
            is None
        )

    async def test_find_active_for_pattern_unknown_returns_none(self, proposal_repository) -> None:
        assert (
            await proposal_repository.find_active_for_pattern(
                error_class=ErrorType.REASONING_ERROR, situation_signature="d9|nope|h0"
            )
            is None
        )

    async def test_save_bumps_version(self, proposal_repository) -> None:
        proposal = _proposal()
        await proposal_repository.add(proposal)

        updated = proposal.bumped(status=ProposalStatus.PENDING_EVALUATION)
        await proposal_repository.save(updated, expected_version=proposal.version)

        stored = await proposal_repository.get(proposal.id)
        assert stored is not None
        assert stored.status is ProposalStatus.PENDING_EVALUATION
        assert stored.version == proposal.version + 1

    async def test_stale_version_raises_instead_of_overwriting(self, proposal_repository) -> None:
        """🔴 **绝不静默覆盖。**

        两条并发的状态流转（"批准"与"驳回"同时到达）如果后者覆盖了前者，
        结果是一条提案**同时**被批准和驳回，而事件流里两条理由都在。
        乐观锁在这里不是性能优化，是正确性要求。

        ⚠️ **"过期"必须是真实存在过的那个版本，不能是凭空造的数。**

        初版用的是 ``expected_version=proposal.version + 5``（1 → 6），
        那是一个**从未存在过**的版本号。把 ``save`` 改成
        ``if expected_version > current.version: raise`` 之后，
        真实的过期写入（``expected=1`` 而库里已是 2）会被静默接受、
        覆盖掉先提交的那次流转——而这条用例照绿。

        这里走真实路径：先读（v1）→ 别人写成功（v2）→ 拿 v1 去写。
        """
        proposal = _proposal()
        await proposal_repository.add(proposal)

        stale = proposal  # 第一个写入者读到的版本（v1）
        await proposal_repository.save(
            proposal.bumped(status=ProposalStatus.PENDING_EVALUATION),
            expected_version=proposal.version,
        )

        with pytest.raises(OptimisticLockError):
            await proposal_repository.save(
                stale.bumped(status=ProposalStatus.REJECTED),
                expected_version=stale.version,
            )

    async def test_a_stale_write_does_not_change_the_stored_status(
        self, proposal_repository
    ) -> None:
        """过期的写入失败之后，库里必须仍是**先到**的那个状态。"""
        proposal = _proposal()
        await proposal_repository.add(proposal)
        stale = proposal
        await proposal_repository.save(
            proposal.bumped(status=ProposalStatus.PENDING_EVALUATION),
            expected_version=proposal.version,
        )

        with pytest.raises(OptimisticLockError):
            await proposal_repository.save(
                stale.bumped(status=ProposalStatus.REJECTED),
                expected_version=stale.version,
            )

        stored = await proposal_repository.get(proposal.id)
        assert stored is not None
        assert stored.status is ProposalStatus.PENDING_EVALUATION

    async def test_a_status_that_is_not_an_enum_member_is_refused(
        self, proposal_repository
    ) -> None:
        """🔴 ``model_construct`` 绕得过 pydantic，绕不过仓储。

        ``ImprovementProposal.model_construct(status="active")`` 造出的对象
        的 ``status`` 是一个**裸字符串**——类型注解拦不住它
        （`model_construct` 跳过全部校验）。内存实现此前会把它原样存下
        并读回；SQL 实现会以 ``AttributeError: 'str' object has no attribute
        'value'`` 崩溃。**两种都不是"有意拦截"。**
        """
        forged = ImprovementProposal.model_construct(**_forged_proposal_payload("active"))  # type: ignore[arg-type]
        with pytest.raises(ConstitutionViolationError):
            await proposal_repository.add(forged)

    async def test_a_forged_status_cannot_be_saved_over_an_existing_one(
        self, proposal_repository
    ) -> None:
        proposal = _proposal()
        await proposal_repository.add(proposal)
        payload = _forged_proposal_payload("active")
        payload["id"] = proposal.id
        payload["version"] = proposal.version + 1
        forged = ImprovementProposal.model_construct(**payload)  # type: ignore[arg-type]

        with pytest.raises(ConstitutionViolationError):
            await proposal_repository.save(forged, expected_version=proposal.version)
        stored = await proposal_repository.get(proposal.id)
        assert stored is not None
        assert stored.status is ProposalStatus.DRAFT

    async def test_save_unknown_proposal_raises_not_found(self, proposal_repository) -> None:
        with pytest.raises(NotFoundError):
            await proposal_repository.save(_proposal(), expected_version=1)

    async def test_list_all_is_empty_before_anything_is_written(self, proposal_repository) -> None:
        assert await proposal_repository.list_all() == []

    async def test_list_all_filters_by_status(self, proposal_repository) -> None:
        draft = _proposal(created_at=_T0)
        evaluated = _proposal(created_at=_T0 + timedelta(seconds=1))
        await proposal_repository.add(draft)
        await proposal_repository.add(evaluated)
        await proposal_repository.save(
            evaluated.bumped(status=ProposalStatus.EVALUATED),
            expected_version=evaluated.version,
        )

        assert [
            item.id for item in await proposal_repository.list_all(status=ProposalStatus.DRAFT)
        ] == [draft.id]
        assert [
            item.id for item in await proposal_repository.list_all(status=ProposalStatus.EVALUATED)
        ] == [evaluated.id]

    async def test_list_all_filters_by_error_class(self, proposal_repository) -> None:
        reasoning = _proposal(created_at=_T0)
        scope = _proposal(created_at=_T0 + timedelta(seconds=1), error_class=ErrorType.SCOPE_ERROR)
        await proposal_repository.add(reasoning)
        await proposal_repository.add(scope)

        found = await proposal_repository.list_all(error_class=ErrorType.SCOPE_ERROR)
        assert [item.id for item in found] == [scope.id]

    async def test_list_all_orders_newest_first(self, proposal_repository) -> None:
        oldest = _proposal(created_at=_T0)
        middle = _proposal(created_at=_T0 + timedelta(seconds=1))
        newest = _proposal(created_at=_T0 + timedelta(seconds=2))
        await proposal_repository.add(oldest)
        await proposal_repository.add(newest)
        await proposal_repository.add(middle)

        assert [item.id for item in await proposal_repository.list_all()] == [
            newest.id,
            middle.id,
            oldest.id,
        ]

    async def test_ties_are_broken_by_id(self, proposal_repository) -> None:
        """同一时间戳的提案顺序由 id 决定——**两个实现必须一致**。

        这条断言看着琐碎，但它钉住的正是"内存按插入顺序、数据库按返回顺序"
        这类只能靠对比两个实现才发现的差异。
        """
        tied = [_proposal(created_at=_T0) for _ in range(3)]
        for item in tied:
            await proposal_repository.add(item)

        expected = [item.id for item in sorted(tied, key=lambda proposal: str(proposal.id))]
        assert [item.id for item in await proposal_repository.list_all()] == expected

    async def test_list_all_order_is_reproducible(self, proposal_repository) -> None:
        for index in range(4):
            await proposal_repository.add(_proposal(created_at=_T0 + timedelta(seconds=index)))

        first = [item.id for item in await proposal_repository.list_all()]
        second = [item.id for item in await proposal_repository.list_all()]
        assert first == second

    async def test_list_all_respects_limit(self, proposal_repository) -> None:
        for index in range(4):
            await proposal_repository.add(_proposal(created_at=_T0 + timedelta(seconds=index)))

        everything = await proposal_repository.list_all()
        limited = await proposal_repository.list_all(limit=2)
        assert [item.id for item in limited] == [item.id for item in everything[:2]]

    async def test_limit_zero_returns_nothing(self, proposal_repository) -> None:
        await proposal_repository.add(_proposal())
        assert await proposal_repository.list_all(limit=0) == []

    async def test_status_is_written_as_a_plain_string(self, proposal_repository) -> None:
        """两个实现在**存储层**必须对状态值有一致的理解。"""
        proposal = _proposal(status=ProposalStatus.PENDING_EVALUATION)
        await proposal_repository.add(proposal)
        stored = await proposal_repository.get(proposal.id)
        assert stored is not None
        assert stored.status is ProposalStatus.PENDING_EVALUATION


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

    async def test_uncommitted_proposal_writes_are_discarded(self, uow_factory) -> None:
        """🔴 提案也必须挂在工作单元上（阶段 6 引入）。

        "生成提案 + 记录 ``improvement_proposal.created`` 事件"
        必须同生共死——只落了事件而没落提案，会留下一条
        指向不存在提案的审计记录。
        """
        proposal = _proposal()
        async with uow_factory() as uow:
            await uow.proposals.add(proposal)
            # 不调用 commit()

        async with uow_factory() as uow:
            assert await uow.proposals.get(proposal.id) is None

    async def test_committed_proposal_persists(self, uow_factory) -> None:
        proposal = _proposal()
        async with uow_factory() as uow:
            await uow.proposals.add(proposal)
            await uow.commit()

        async with uow_factory() as uow:
            stored = await uow.proposals.get(proposal.id)
            assert stored is not None
            assert stored.target_component == proposal.target_component


async def _latest_for_round(uow_factory, round_id) -> int:
    async with uow_factory() as uow:
        value = await uow.events.latest_sequence_for_round(cognitive_round_id=round_id)
    return int(value)
