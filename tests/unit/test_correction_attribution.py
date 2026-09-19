"""纠正 → 归因的应用层（阶段 6.6，ADR-0023）。

三块在这里被钉住：

1. **指针怎么解析**（``_resolve_correction_target``）——只在本回合的事件流里
   找，找到了但类型不接受纠正时，理由要与"找不到"**分开说**；
2. **重复纠正不写第二条**（``_existing_attributions``）；
3. **学习触发失败时的对外形状**——稳定错误码 + ``trace_id``，
   **异常原文一个字都不出去**。

第 3 条是安全问题，不是风格问题：异常消息里可能带 SQL、文件路径、
连接串片段与堆栈。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from ai_psi.application.feedback_service import (
    FeedbackService,
    _existing_attributions,
    _resolve_correction_target,
)
from ai_psi.application.memory_service import MemoryService
from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.domain.cognitive_rounds import CognitiveRound
from ai_psi.domain.common import utc_now
from ai_psi.domain.enums import (
    ActorType,
    CorrectedArtifactKind,
    ErrorType,
    EventType,
    FeedbackType,
)
from ai_psi.domain.events import Event
from ai_psi.domain.experiences import Experience
from ai_psi.infrastructure.in_memory.store import InMemoryStore
from ai_psi.infrastructure.in_memory.unit_of_work import make_in_memory_unit_of_work_factory
from ai_psi.providers.embeddings import LocalHashingEmbedding
from tests.helpers import RecordingTrigger

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 9, 19, tzinfo=UTC)


def _event(event_type: EventType, payload: dict[str, object]) -> Event:
    return Event(
        event_type=event_type,
        occurred_at=_NOW,
        actor_type=ActorType.SYSTEM,
        actor_id="test",
        correlation_id=uuid4(),
        cognitive_round_id=uuid4(),
        payload=payload,
    )


def _hypothesis_event(artifact_id: UUID, *, supported: bool) -> Event:
    return _event(
        EventType.HYPOTHESIS_CREATED,
        {
            "hypothesis": {
                "id": str(artifact_id),
                "supporting_evidence_ids": ([str(uuid4())] if supported else []),
            }
        },
    )


@pytest.fixture
def uow_factory() -> UnitOfWorkFactory:
    return make_in_memory_unit_of_work_factory(InMemoryStore(), LocalHashingEmbedding())


@pytest.fixture
def service_factory(
    uow_factory: UnitOfWorkFactory,
) -> Callable[[RecordingTrigger], FeedbackService]:
    """按给定的触发器造一个反馈服务。"""

    def _make(trigger: RecordingTrigger) -> FeedbackService:
        memory_service = MemoryService(uow_factory, LocalHashingEmbedding())
        return FeedbackService(uow_factory, memory_service, trigger)

    return _make


async def _seed_round(
    uow_factory: UnitOfWorkFactory, make_experience: Callable[..., Any]
) -> tuple[UUID, UUID]:
    """造一个回合，里面有**一条经验**和**一条假设**。

    ⚠️ 这里直接写事件，是**单元层**的取舍：本文件测的是
    "指针解析 / 去重 / 失败上报"这三段逻辑，它们的输入就是"事件流里有什么"。
    真实的端到端路径由黑盒 A–H 覆盖——**那里不许这么造数据**。
    """
    hypothesis_id = uuid4()

    async with uow_factory() as uow:
        round_ = CognitiveRound(created_by="test")
        await uow.rounds.add(round_)
        await uow.commit()

    # 经验必须挂在这个回合上：`_attribute_experiences` 按回合读事件流，
    # 因此这里显式把回合 id 传进去，而不是用 `make_experience` 的随机回合。
    scoped: Experience = make_experience(cognitive_round_id=round_.id)
    async with uow_factory() as uow:
        await uow.events.append(
            Event(
                event_type=EventType.EXPERIENCE_CREATED,
                occurred_at=utc_now(),
                actor_type=ActorType.SYSTEM,
                actor_id="test",
                correlation_id=uuid4(),
                cognitive_round_id=round_.id,
                payload={"experience": scoped.model_dump(mode="json")},
            )
        )
        await uow.events.append(
            Event(
                event_type=EventType.HYPOTHESIS_CREATED,
                occurred_at=utc_now(),
                actor_type=ActorType.SYSTEM,
                actor_id="test",
                correlation_id=uuid4(),
                cognitive_round_id=round_.id,
                payload={"hypothesis": {"id": str(hypothesis_id), "supporting_evidence_ids": []}},
            )
        )
        await uow.commit()

    return round_.id, hypothesis_id


class TestResolvingThePointer:
    """🔴 指针只在本回合的事件流里解析——绑定是**解析方式本身**带来的。"""

    def test_a_hypothesis_resolves_with_its_structure(self) -> None:
        artifact_id = uuid4()
        target, why = _resolve_correction_target(
            [_hypothesis_event(artifact_id, supported=True)], artifact_id
        )
        assert why == ""
        assert target is not None
        assert target.artifact_kind is CorrectedArtifactKind.HYPOTHESIS
        assert target.has_supporting_evidence is True

    def test_an_unknown_id_is_not_found(self) -> None:
        """⚠️ 其余回合的产物、别的用户的数据、随机 UUID 都落在这里。"""
        events = [_hypothesis_event(uuid4(), supported=False)]
        target, why = _resolve_correction_target(events, uuid4())
        assert target is None
        assert "解析不到" in why

    def test_a_non_correctable_kind_says_something_different(self) -> None:
        """🔴 **"找到了但不能纠正" 与 "找不到" 是两件事，理由必须分开。**

        合并成一句话，调用方就无从知道该去改指针，还是该换个入口
        （记忆有它自己的纠正接口）。
        """
        inquiry_id = uuid4()
        events = [
            _event(
                EventType.INQUIRY_CREATED,
                {"inquiry": {"id": str(inquiry_id), "question": "随便问点什么"}},
            )
        ]
        target, why = _resolve_correction_target(events, inquiry_id)
        assert target is None
        assert "不接受纠正" in why
        assert "解析不到" not in why

    def test_the_resolver_only_looks_at_the_events_it_is_given(self) -> None:
        """换一条事件流（= 换一个回合），同一个 id 就找不到了。

        这正是"跨回合指针不产生归因"的实现方式——不需要额外过滤条件，
        因此也不会因为忘写一个 ``WHERE`` 而漏。
        """
        artifact_id = uuid4()
        round_a = [_hypothesis_event(artifact_id, supported=False)]
        round_b: Sequence[Event] = []

        assert _resolve_correction_target(round_a, artifact_id)[0] is not None
        assert _resolve_correction_target(round_b, artifact_id)[0] is None


class TestDuplicateAttributionsAreNotWrittenTwice:
    """🔴 同一件事说两遍不是"更确认"，只是重复。"""

    def _attribution_event(self, experience_id: UUID, artifact_id: UUID) -> Event:
        return _event(
            EventType.EXPERIENCE_ATTRIBUTED,
            {
                "attribution": {
                    "experience_id": str(experience_id),
                    "error_type": ErrorType.EVIDENCE_ERROR.value,
                    "related_artifact_id": str(artifact_id),
                }
            },
        )

    def test_a_recorded_triple_is_seen(self) -> None:
        experience_id, artifact_id = uuid4(), uuid4()
        seen = _existing_attributions([self._attribution_event(experience_id, artifact_id)])
        assert (experience_id, ErrorType.EVIDENCE_ERROR, artifact_id) in seen

    def test_other_event_types_do_not_count(self) -> None:
        assert _existing_attributions([_hypothesis_event(uuid4(), supported=False)]) == set()

    def test_a_corrupt_payload_does_not_break_the_read(self) -> None:
        """⚠️ 一条坏负载不该让整次反馈失败——跳过它，其余照常。"""
        experience_id, artifact_id = uuid4(), uuid4()
        events = [
            _event(
                EventType.EXPERIENCE_ATTRIBUTED, {"attribution": {"experience_id": "不是 uuid"}}
            ),
            self._attribution_event(experience_id, artifact_id),
        ]
        assert (experience_id, ErrorType.EVIDENCE_ERROR, artifact_id) in _existing_attributions(
            events
        )


class TestTheTriggerOutcomeIsSafeToReturn:
    """🔴 学习触发失败时，**异常原文一个字都不出去**。

    这是安全问题：异常消息里可能带 SQL、文件路径、连接串片段与堆栈。
    对外只给一个稳定错误码 + 一个 ``trace_id``，两者能在服务端日志里对上。
    """

    async def test_the_failure_is_reported_without_the_exception_text(
        self,
        uow_factory: UnitOfWorkFactory,
        service_factory: Callable[[RecordingTrigger], FeedbackService],
        make_experience: Callable[..., Any],
    ) -> None:
        secret = 'psycopg.errors.UndefinedTable: relation "events" does not exist'
        trigger = RecordingTrigger(error=RuntimeError(secret))
        service = service_factory(trigger)
        round_id, hypothesis_id = await _seed_round(uow_factory, make_experience)

        outcome = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content="你这里判断错了：单一观察不该推出意图",
            related_artifact_id=hypothesis_id,
        )

        learning = outcome.learning
        assert learning is not None
        assert learning.status.value == "failed"
        assert learning.error_code == "learning_trigger_failed"
        assert learning.trace_id, "失败必须给出能与日志对上的 trace_id"

        # 🔴 **原文（以及它的任何片段）都不许出现在对外结果里**
        joined = " ".join(outcome.reasons)
        assert secret not in joined
        assert "UndefinedTable" not in joined
        assert "relation" not in joined
        # 但**必须说清发生了什么**，而不是静默
        assert "learning_trigger_failed" in joined

    async def test_a_trigger_that_never_fires_says_so(
        self,
        uow_factory: UnitOfWorkFactory,
        service_factory: Callable[[RecordingTrigger], FeedbackService],
        make_experience: Callable[..., Any],
    ) -> None:
        """反向：没有归因时不触发，而"没触发"要能被读出来（不是 `None`）。"""
        trigger = RecordingTrigger()
        service = service_factory(trigger)
        round_id, _ = await _seed_round(uow_factory, make_experience)

        outcome = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content="你这里判断错了",
            related_artifact_id=None,  # 不给指针 → 不归因 → 不触发
        )

        assert outcome.attributions == ()
        assert outcome.learning is not None
        assert outcome.learning.status.value == "not_triggered"
        assert trigger.calls == 0

    async def test_an_attributed_correction_does_fire(
        self,
        uow_factory: UnitOfWorkFactory,
        service_factory: Callable[[RecordingTrigger], FeedbackService],
        make_experience: Callable[..., Any],
    ) -> None:
        """正向：给出了能解析的指针 → 真的触发了，且结果如实回传。"""
        trigger = RecordingTrigger(created_proposal_ids=(uuid4(),))
        service = service_factory(trigger)
        round_id, hypothesis_id = await _seed_round(uow_factory, make_experience)

        outcome = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content="你这里判断错了",
            related_artifact_id=hypothesis_id,
        )

        assert len(outcome.attributions) == 1
        assert outcome.attributions[0].error_type is ErrorType.EVIDENCE_ERROR
        assert trigger.calls == 1
        assert outcome.learning is not None
        assert outcome.learning.status.value == "succeeded"
        assert len(outcome.learning.created_proposal_ids) == 1
