"""事件存储的集成测试。

🔴 覆盖阶段 2 验收条件之一：**可以创建和回放事件。**
以及 ADR-0002 的核心约束：**事件只追加，不可静默覆盖。**
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.domain.enums import ActorType, EventType
from ai_psi.domain.events import Event
from ai_psi.domain.exceptions import ConflictError
from tests.helpers import construct

pytestmark = pytest.mark.integration

NOON = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _event(round_id: object, **overrides: object) -> Event:
    """构造一条属于指定回合的测试事件。

    用 :func:`tests.helpers.construct` 而不是直接调用构造器：
    字段来自异构字典，静态类型看不出这些值都是合法的。
    """
    fields: dict[str, object] = {
        "event_type": EventType.COGNITIVE_ROUND_STARTED,
        "occurred_at": NOON,
        "actor_type": ActorType.SYSTEM,
        "actor_id": "test",
        "cognitive_round_id": round_id,
    }
    fields.update(overrides)
    return construct(Event, **fields)


class TestAppendAndRead:
    async def test_append_then_read_roundtrip(self, uow_factory: UnitOfWorkFactory) -> None:
        round_id = uuid4()
        async with uow_factory() as uow:
            await uow.events.append(_event(round_id))
            await uow.commit()

        async with uow_factory() as uow:
            events = await uow.events.read_stream(cognitive_round_id=round_id)

        assert len(events) == 1
        assert events[0].cognitive_round_id == round_id
        assert events[0].event_type is EventType.COGNITIVE_ROUND_STARTED

    async def test_roundtrip_preserves_all_fields(self, uow_factory: UnitOfWorkFactory) -> None:
        round_id = uuid4()
        user_id = uuid4()
        original = _event(
            round_id,
            user_id=user_id,
            payload={"state": "created", "nested": {"a": [1, 2, 3]}},
            evidence_refs=[uuid4(), uuid4()],
        )

        async with uow_factory() as uow:
            await uow.events.append(original)
            await uow.commit()

        async with uow_factory() as uow:
            restored = (await uow.events.read_stream(cognitive_round_id=round_id))[0]

        assert restored.id == original.id
        assert restored.user_id == user_id
        assert restored.payload["nested"] == {"a": [1, 2, 3]}
        assert len(restored.evidence_refs) == 2
        assert restored.actor_type is ActorType.SYSTEM

    async def test_append_many_is_ordered(self, uow_factory: UnitOfWorkFactory) -> None:
        round_id = uuid4()
        events = [_event(round_id, event_type=EventType.EVIDENCE_ATTACHED) for _ in range(5)]

        async with uow_factory() as uow:
            await uow.events.append_many(events)
            await uow.commit()

        async with uow_factory() as uow:
            read_back = await uow.events.read_stream(cognitive_round_id=round_id)

        assert [e.id for e in read_back] == [e.id for e in events], (
            "读取顺序必须与写入顺序一致——回放依赖这一点"
        )

    async def test_duplicate_ids_in_batch_are_rejected(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        round_id = uuid4()
        event = _event(round_id)

        async with uow_factory() as uow:
            with pytest.raises(ConflictError, match="重复的事件 id"):
                await uow.events.append_many([event, event])


class TestStreamOrdering:
    """回放顺序由 ``sequence`` 决定，不是时间戳。"""

    async def test_order_follows_insertion_not_timestamps(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        """后写入但时间戳更早的事件，读取时仍然排在后面。

        这正是双时间戳存在的理由：乱序到达的事件（如迟到的外部资料）
        必须按**写入顺序**回放，否则重建出的状态链会自相矛盾。
        """
        round_id = uuid4()
        late_arriving = _event(
            round_id,
            event_type=EventType.EVIDENCE_ATTACHED,
            occurred_at=NOON - timedelta(days=3),
        )
        later = _event(round_id, event_type=EventType.CONCERN_CREATED, occurred_at=NOON)

        async with uow_factory() as uow:
            await uow.events.append(late_arriving)
            await uow.events.append(later)
            await uow.commit()

        async with uow_factory() as uow:
            events = await uow.events.read_stream(cognitive_round_id=round_id)

        assert events[0].id == late_arriving.id
        assert events[1].id == later.id

    async def test_after_sequence_supports_incremental_read(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        round_id = uuid4()
        async with uow_factory() as uow:
            await uow.events.append_many([_event(round_id) for _ in range(3)])
            await uow.commit()

        async with uow_factory() as uow:
            cursor = await uow.events.latest_sequence_for_round(cognitive_round_id=round_id)
            assert cursor > 0

            await uow.events.append(_event(round_id))
            await uow.commit()

        async with uow_factory() as uow:
            incremental = await uow.events.read_stream(
                cognitive_round_id=round_id, after_sequence=cursor
            )
        assert len(incremental) == 1

    async def test_streams_are_isolated_per_round(self, uow_factory: UnitOfWorkFactory) -> None:
        round_a, round_b = uuid4(), uuid4()
        async with uow_factory() as uow:
            await uow.events.append(_event(round_a))
            await uow.events.append_many([_event(round_b) for _ in range(3)])
            await uow.commit()

        async with uow_factory() as uow:
            assert len(await uow.events.read_stream(cognitive_round_id=round_a)) == 1
            assert len(await uow.events.read_stream(cognitive_round_id=round_b)) == 3

    async def test_correlation_read_spans_rounds(self, uow_factory: UnitOfWorkFactory) -> None:
        correlation_id = uuid4()
        async with uow_factory() as uow:
            await uow.events.append(_event(uuid4(), correlation_id=correlation_id))
            await uow.events.append(_event(uuid4(), correlation_id=correlation_id))
            await uow.commit()

        async with uow_factory() as uow:
            events = await uow.events.read_by_correlation(correlation_id=correlation_id)
        assert len(events) == 2


class TestAppendOnly:
    """🔴 ADR-0002：事件只追加，不可静默覆盖。"""

    def test_event_store_protocol_has_no_mutators(self) -> None:
        """Port 上不存在 update / delete —— 这不是遗漏，是设计。"""
        from ai_psi.application.ports import EventStore

        forbidden = {"update", "delete", "remove", "replace", "save"}
        assert not (forbidden & set(dir(EventStore))), (
            "EventStore 出现了修改类方法。事件是真相来源，修改它等于销毁审计证据"
        )

    def test_implementation_exposes_no_mutators(self) -> None:
        """实现同样不得暴露修改入口，否则可以绕过 Port 私下改事件。

        本用例刻意**不请求任何异步夹具**——它只检查类的方法名，
        请求异步夹具反而会让同步测试走上另一条事件循环创建路径。
        """
        from ai_psi.infrastructure.event_store import SqlAlchemyEventStore

        forbidden = {"update", "delete", "remove", "replace"}
        assert not (forbidden & set(dir(SqlAlchemyEventStore)))

    async def test_primary_key_prevents_overwrite(
        self, uow_factory: UnitOfWorkFactory, engine: AsyncEngine
    ) -> None:
        """同一 id 二次写入被主键拒绝——物理上无法覆盖。"""
        from sqlalchemy.exc import IntegrityError

        round_id = uuid4()
        event = _event(round_id)

        async with uow_factory() as uow:
            await uow.events.append(event)
            await uow.commit()

        async with engine.begin() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(
                    text(
                        "INSERT INTO events (id, event_type, occurred_at, recorded_at,"
                        " actor_type, actor_id, correlation_id, payload, evidence_refs,"
                        " trust_level, sensitivity, schema_version)"
                        " VALUES (:id, 'user.message.received', now(), now(), 'user', 'x',"
                        " :corr, '{}'::jsonb, '{}', 'medium', 'internal', '1.0.0')"
                    ),
                    {"id": event.id, "corr": uuid4()},
                )


class TestDatabaseConstraints:
    """数据库级防线：即使应用层被绕过，非法数据也进不来。"""

    async def test_recorded_before_occurred_is_rejected_by_db(self, engine: AsyncEngine) -> None:
        from sqlalchemy.exc import IntegrityError

        async with engine.begin() as conn:
            with pytest.raises(IntegrityError, match="recorded_not_before_occurred"):
                await conn.execute(
                    text(
                        "INSERT INTO events (id, event_type, occurred_at, recorded_at,"
                        " actor_type, actor_id, correlation_id, payload, evidence_refs,"
                        " trust_level, sensitivity, schema_version)"
                        " VALUES (:id, 'user.message.received', now(),"
                        " now() - interval '1 hour', 'user', 'x', :corr, '{}'::jsonb,"
                        " '{}', 'medium', 'internal', '1.0.0')"
                    ),
                    {"id": uuid4(), "corr": uuid4()},
                )

    async def test_invalid_actor_type_is_rejected_by_db(self, engine: AsyncEngine) -> None:
        from sqlalchemy.exc import IntegrityError

        async with engine.begin() as conn:
            with pytest.raises(IntegrityError, match="actor_type_valid"):
                await conn.execute(
                    text(
                        "INSERT INTO events (id, event_type, occurred_at, recorded_at,"
                        " actor_type, actor_id, correlation_id, payload, evidence_refs,"
                        " trust_level, sensitivity, schema_version)"
                        " VALUES (:id, 'user.message.received', now(), now(), 'robot', 'x',"
                        " :corr, '{}'::jsonb, '{}', 'medium', 'internal', '1.0.0')"
                    ),
                    {"id": uuid4(), "corr": uuid4()},
                )
