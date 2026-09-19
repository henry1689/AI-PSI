"""反馈与纠正（任务书 §12.2、§10.4）。

🔴 **本文件的中心断言只有一条：反馈不能绕过记忆写入策略。**

其余用例都在为它服务——把"反馈确实会去问策略"与
"策略说不的时候确实没写"分开验证。只测"反馈能写记忆"
会漏掉最要紧的那一半：它什么**不能**写。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from ai_psi.application.feedback_service import (
    FEEDBACK_MEMORY_TYPE,
    MEMORY_UPDATING_FEEDBACK_TYPES,
    FeedbackService,
    MemoryEffect,
)
from ai_psi.application.memory_service import MemoryService, MemoryWriteOutcome
from ai_psi.application.ports import UnitOfWork, UnitOfWorkFactory
from ai_psi.domain.cognitive_rounds import CognitiveBudget, CognitiveRound
from ai_psi.domain.enums import (
    CognitiveDepth,
    EventType,
    FeedbackType,
    MemoryType,
    VerificationStatus,
)
from ai_psi.domain.exceptions import NotFoundError
from ai_psi.domain.memories import Memory
from ai_psi.infrastructure.in_memory.store import InMemoryStore
from ai_psi.infrastructure.in_memory.unit_of_work import make_in_memory_unit_of_work_factory
from ai_psi.memory.write_policy import (
    MemoryWriteProposal,
    WriteDecision,
    WritePolicy,
    WritePolicyDecision,
)
from ai_psi.providers.embeddings import LocalHashingEmbedding
from tests.helpers import RecordingTrigger

pytestmark = pytest.mark.unit

CORRECTION_TEXT = "我说的适应是改变方法，不是放弃原则"


class _DenyAllPolicy(WritePolicy):
    """一律拒绝的写入策略——用来证明反馈确实经过了策略这一关。"""

    def decide(self, proposal: MemoryWriteProposal) -> WritePolicyDecision:
        del proposal
        return WritePolicyDecision(
            decision=WriteDecision.REJECTED,
            reasons=("测试用策略：一律拒绝",),
        )


@pytest.fixture
def embeddings() -> LocalHashingEmbedding:
    return LocalHashingEmbedding()


@pytest.fixture
def uow_factory(embeddings: LocalHashingEmbedding) -> UnitOfWorkFactory:
    return make_in_memory_unit_of_work_factory(InMemoryStore(), embeddings)


@pytest.fixture
def memory_service(
    uow_factory: UnitOfWorkFactory, embeddings: LocalHashingEmbedding
) -> MemoryService:
    return MemoryService(uow_factory, embeddings)


def _service(
    uow_factory: UnitOfWorkFactory,
    memory_service: MemoryService,
    *,
    trigger: RecordingTrigger | None = None,
) -> FeedbackService:
    """构造一个反馈服务。

    🔴 **触发器必须显式给。** `FeedbackService` 的构造参数刻意没有默认值
    （给了 `None` 默认值的症状是"静默地不再触发学习"），
    因此测试也不能靠省略它——见 `tests.helpers.RecordingTrigger`。
    """
    return FeedbackService(uow_factory, memory_service, trigger or RecordingTrigger())


@pytest.fixture
def service(uow_factory: UnitOfWorkFactory, memory_service: MemoryService) -> FeedbackService:
    return _service(uow_factory, memory_service)


async def _make_round(uow_factory: UnitOfWorkFactory, *, user_id: UUID | None) -> UUID:
    """写入一个回合，返回它的 id。"""
    round_ = CognitiveRound(
        created_by="test",
        user_id=user_id,
        depth_level=CognitiveDepth.D1,
        budget=CognitiveBudget.for_depth(CognitiveDepth.D1),
    )
    async with uow_factory() as uow:
        await uow.rounds.add(round_)
        await uow.commit()
    return round_.id


async def _events(uow_factory: UnitOfWorkFactory, round_id: UUID):
    async with uow_factory() as uow:
        return await uow.events.read_stream(cognitive_round_id=round_id)


async def _memories(uow_factory: UnitOfWorkFactory, user_id: UUID) -> list[Memory]:
    async with uow_factory() as uow:
        return await uow.memories.list_for_user(user_id=user_id)


class TestRoundMustExist:
    async def test_unknown_round_is_rejected(self, service: FeedbackService) -> None:
        with pytest.raises(NotFoundError, match="认知回合不存在"):
            await service.record(
                round_id=uuid4(),
                feedback_type=FeedbackType.CORRECTION,
                content=CORRECTION_TEXT,
            )

    async def test_nothing_is_written_for_an_unknown_round(
        self, service: FeedbackService, uow_factory: UnitOfWorkFactory
    ) -> None:
        round_id = uuid4()
        with pytest.raises(NotFoundError):
            await service.record(
                round_id=round_id,
                feedback_type=FeedbackType.CORRECTION,
                content=CORRECTION_TEXT,
            )
        assert await _events(uow_factory, round_id) == []


class TestFeedbackIsRecorded:
    async def test_event_type_and_attachment(
        self, service: FeedbackService, uow_factory: UnitOfWorkFactory
    ) -> None:
        user_id = uuid4()
        round_id = await _make_round(uow_factory, user_id=user_id)
        outcome = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content=CORRECTION_TEXT,
        )

        events = await _events(uow_factory, round_id)
        assert [item.id for item in events] == [outcome.event.id]
        assert outcome.event.event_type is EventType.USER_FEEDBACK_RECEIVED
        assert outcome.event.cognitive_round_id == round_id
        assert outcome.event.user_id == user_id

    async def test_content_is_kept_in_the_conversation_record(
        self, service: FeedbackService, uow_factory: UnitOfWorkFactory
    ) -> None:
        """🔴 反馈正文**必须**存下来。

        它是对话记录（事件流里本来就存着用户消息的正文）。
        摘掉它，这条事件就只剩「用户点过一次纠正」——
        事后既说不清他纠正了什么，也说不清那条记忆是从哪句话来的。
        """
        round_id = await _make_round(uow_factory, user_id=uuid4())
        await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content=CORRECTION_TEXT,
            related_claim="用户将适应环境理解为放弃自我",
        )

        payload = (await _events(uow_factory, round_id))[0].payload
        assert payload["content"] == CORRECTION_TEXT
        assert payload["related_claim"] == "用户将适应环境理解为放弃自我"

    async def test_event_does_not_predict_the_memory_outcome(
        self, service: FeedbackService, uow_factory: UnitOfWorkFactory
    ) -> None:
        """🔴 事件只记录**请求与判定**，不预测结果。

        把猜出来的结果写进不可变更的事件里，就是在制造一条会过期的记录：
        真实结果是记忆层自己的事件说的（``memory.approved`` / ``memory.rejected``）。
        """
        round_id = await _make_round(uow_factory, user_id=uuid4())
        await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content=CORRECTION_TEXT,
            allow_memory_update=True,
        )

        payload = (await _events(uow_factory, round_id))[0].payload
        assert "memory_effect" not in payload
        assert payload["memory_update_eligible"] is True

    async def test_eligibility_and_reason_are_recorded(
        self, service: FeedbackService, uow_factory: UnitOfWorkFactory
    ) -> None:
        round_id = await _make_round(uow_factory, user_id=uuid4())
        await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.AGREEMENT,
            content="同意",
            allow_memory_update=True,
        )

        payload = (await _events(uow_factory, round_id))[0].payload
        assert payload["memory_update_eligible"] is False
        assert payload["memory_update_ineligible_reason"]


class TestNoMemoryIsWrittenUnlessAsked:
    async def test_default_does_not_touch_memory(
        self, service: FeedbackService, uow_factory: UnitOfWorkFactory
    ) -> None:
        user_id = uuid4()
        round_id = await _make_round(uow_factory, user_id=user_id)
        outcome = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content=CORRECTION_TEXT,
        )

        assert outcome.memory_effect is MemoryEffect.NONE
        assert outcome.memory_written is False
        assert await _memories(uow_factory, user_id) == []

    async def test_none_and_not_eligible_are_different_results(
        self, service: FeedbackService, uow_factory: UnitOfWorkFactory
    ) -> None:
        """🔴 "我忘了开开关"与"我开了但被规则挡住"不能长成一个样子。"""
        user_id = uuid4()
        round_id = await _make_round(uow_factory, user_id=user_id)

        not_requested = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content=CORRECTION_TEXT,
        )
        not_eligible = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.AGREEMENT,
            content="同意",
            allow_memory_update=True,
        )

        assert not_requested.memory_effect is MemoryEffect.NONE
        assert not_eligible.memory_effect is MemoryEffect.NOT_ELIGIBLE
        assert not_requested.reasons != not_eligible.reasons

    async def test_reasons_say_why_nothing_happened(
        self, service: FeedbackService, uow_factory: UnitOfWorkFactory
    ) -> None:
        round_id = await _make_round(uow_factory, user_id=uuid4())
        outcome = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content=CORRECTION_TEXT,
        )
        assert outcome.reasons


class TestWhichFeedbackTypesMayWriteMemory:
    @pytest.mark.parametrize("feedback_type", sorted(MEMORY_UPDATING_FEEDBACK_TYPES))
    async def test_informative_types_write(
        self,
        service: FeedbackService,
        uow_factory: UnitOfWorkFactory,
        feedback_type: FeedbackType,
    ) -> None:
        """只有这两类提供了新信息："我说的其实是 X"。"""
        user_id = uuid4()
        round_id = await _make_round(uow_factory, user_id=user_id)
        outcome = await service.record(
            round_id=round_id,
            feedback_type=feedback_type,
            content=CORRECTION_TEXT,
            allow_memory_update=True,
        )

        assert outcome.memory_effect is MemoryEffect.WRITTEN
        assert outcome.memory is not None
        assert len(await _memories(uow_factory, user_id)) == 1

    @pytest.mark.parametrize(
        "feedback_type",
        [
            FeedbackType.AGREEMENT,
            FeedbackType.ACKNOWLEDGEMENT,
            FeedbackType.RATING,
            FeedbackType.DISAGREEMENT,
        ],
    )
    async def test_uninformative_types_never_write(
        self,
        service: FeedbackService,
        uow_factory: UnitOfWorkFactory,
        feedback_type: FeedbackType,
    ) -> None:
        """🔴 赞同与评分说的是「你做得对不对」，不是「事实是什么」。

        拿它们写记忆，等于把噪声记成事实——而记忆一旦写错，
        它会在之后**每一轮**里持续影响判断。
        """
        user_id = uuid4()
        round_id = await _make_round(uow_factory, user_id=user_id)
        outcome = await service.record(
            round_id=round_id,
            feedback_type=feedback_type,
            content="随便写点什么",
            allow_memory_update=True,
        )

        assert outcome.memory_effect is MemoryEffect.NOT_ELIGIBLE
        assert outcome.memory_written is False
        assert await _memories(uow_factory, user_id) == []

    async def test_the_allowlist_covers_exactly_two_types(self) -> None:
        """白名单，不是黑名单：新增反馈类型默认**不允许**带出记忆。"""
        assert {
            FeedbackType.CORRECTION,
            FeedbackType.CLARIFICATION,
        } == MEMORY_UPDATING_FEEDBACK_TYPES

    async def test_anonymous_rounds_cannot_write(
        self, service: FeedbackService, uow_factory: UnitOfWorkFactory
    ) -> None:
        """🔴 无归属用户 → 写下去的会是一条**全局**记忆（不变量 14）。

        那比不写糟得多：它会出现在所有人的检索结果里。
        """
        round_id = await _make_round(uow_factory, user_id=None)
        outcome = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content=CORRECTION_TEXT,
            allow_memory_update=True,
        )

        assert outcome.memory_effect is MemoryEffect.NOT_ELIGIBLE
        assert outcome.memory_written is False
        assert any("作用域" in reason for reason in outcome.reasons)


class TestWrittenMemory:
    async def test_memory_is_scoped_to_the_round_owner(
        self, service: FeedbackService, uow_factory: UnitOfWorkFactory
    ) -> None:
        user_id = uuid4()
        round_id = await _make_round(uow_factory, user_id=user_id)
        outcome = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content=CORRECTION_TEXT,
            allow_memory_update=True,
        )

        assert outcome.memory is not None
        assert outcome.memory.user_id == user_id
        assert await _memories(uow_factory, uuid4()) == []

    async def test_memory_points_back_at_the_feedback_event(
        self, service: FeedbackService, uow_factory: UnitOfWorkFactory
    ) -> None:
        """``source_event_ids`` 指向那条反馈——"这条记忆为什么会出现"可追。"""
        round_id = await _make_round(uow_factory, user_id=uuid4())
        outcome = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content=CORRECTION_TEXT,
            allow_memory_update=True,
        )

        assert outcome.memory is not None
        assert outcome.memory.source_event_ids == [outcome.event.id]

    async def test_content_becomes_the_memory_content(
        self, service: FeedbackService, uow_factory: UnitOfWorkFactory
    ) -> None:
        round_id = await _make_round(uow_factory, user_id=uuid4())
        outcome = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content=CORRECTION_TEXT,
            allow_memory_update=True,
        )
        assert outcome.memory is not None
        assert outcome.memory.content == CORRECTION_TEXT

    async def test_memory_type_is_the_writable_user_statement_type(
        self, service: FeedbackService, uow_factory: UnitOfWorkFactory
    ) -> None:
        round_id = await _make_round(uow_factory, user_id=uuid4())
        outcome = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content=CORRECTION_TEXT,
            allow_memory_update=True,
        )
        assert outcome.memory is not None
        assert outcome.memory.memory_type is FEEDBACK_MEMORY_TYPE
        assert outcome.memory.memory_type is MemoryType.USER_PREFERENCE

    async def test_user_feedback_never_marks_anything_verified(
        self, service: FeedbackService, uow_factory: UnitOfWorkFactory
    ) -> None:
        """🔴 不变量 4：用户的赞同、确认与纠正**都不能**把状态改为已验证。

        已验证只能由证据变更驱动。反馈路径上没有任何入口能设置它。
        """
        round_id = await _make_round(uow_factory, user_id=uuid4())
        outcome = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content=CORRECTION_TEXT,
            allow_memory_update=True,
        )

        assert outcome.memory is not None
        assert outcome.memory.verification_status is not VerificationStatus.VERIFIED

    async def test_repeating_the_same_feedback_does_not_duplicate(
        self, service: FeedbackService, uow_factory: UnitOfWorkFactory
    ) -> None:
        user_id = uuid4()
        round_id = await _make_round(uow_factory, user_id=user_id)
        first = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content=CORRECTION_TEXT,
            allow_memory_update=True,
        )
        second = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content=CORRECTION_TEXT,
            allow_memory_update=True,
        )

        assert first.memory_effect is MemoryEffect.WRITTEN
        assert second.memory_effect is MemoryEffect.DUPLICATE
        assert second.memory_written is False
        assert len(await _memories(uow_factory, user_id)) == 1


class TestFeedbackCannotBypassTheWritePolicy:
    """🔴 本文件的中心：``allow_memory_update`` 是"允许提给策略"，不是"允许写入"。"""

    async def test_a_denying_policy_blocks_the_write(
        self, uow_factory: UnitOfWorkFactory, embeddings: LocalHashingEmbedding
    ) -> None:
        user_id = uuid4()
        round_id = await _make_round(uow_factory, user_id=user_id)
        service = _service(
            uow_factory, MemoryService(uow_factory, embeddings, policy=_DenyAllPolicy())
        )

        outcome = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content=CORRECTION_TEXT,
            allow_memory_update=True,
        )

        assert outcome.memory_effect is MemoryEffect.REJECTED_BY_POLICY
        assert outcome.memory_written is False
        assert await _memories(uow_factory, user_id) == []

    async def test_forbidden_content_is_stopped_by_the_policy(
        self, service: FeedbackService, uow_factory: UnitOfWorkFactory
    ) -> None:
        """内容红线（任务书 §10.3）在反馈路径上同样生效。

        这条用例特意用一个**看起来最正当**的入口——
        用户本人亲手写下的纠正——来撞红线。
        """
        user_id = uuid4()
        round_id = await _make_round(uow_factory, user_id=user_id)
        outcome = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content="他最近确诊了抑郁症",
            allow_memory_update=True,
        )

        assert outcome.memory_effect is MemoryEffect.REJECTED_BY_POLICY
        assert outcome.memory_written is False
        assert any("禁止类别" in reason for reason in outcome.reasons)

    async def test_the_rejection_reason_reaches_the_caller(
        self, service: FeedbackService, uow_factory: UnitOfWorkFactory
    ) -> None:
        round_id = await _make_round(uow_factory, user_id=uuid4())
        outcome = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content="他最近确诊了抑郁症",
            allow_memory_update=True,
        )
        assert outcome.reasons

    async def test_the_same_memory_service_instance_is_used(
        self, service: FeedbackService, memory_service: MemoryService
    ) -> None:
        """另造一个实例会让反馈路径与其余路径各持一份写入策略。"""
        assert service.memory_service is memory_service


class _Exploding(MemoryService):
    """记忆写入时崩溃的替身。"""

    async def propose(
        self,
        *,
        proposal: MemoryWriteProposal,
        actor_id: str = "memory_service",
        correlation_id: UUID | None = None,
        uow: UnitOfWork | None = None,
    ) -> MemoryWriteOutcome:
        del proposal, actor_id, correlation_id, uow
        msg = "模拟记忆写入过程中崩溃"
        raise RuntimeError(msg)


class TestFeedbackIsAtomicWithItsMemoryUpdate:
    """🔴 阶段 6.5 §三.5–6：反馈事件、经验评价、记忆更新在**同一事务**。

    ⚠️ **本类断言的"方向"与阶段 6 相反，这是有意的。**

    阶段 6 的实现让反馈事件先落、记忆更新后做（两个事务），
    理由是"丢用户的话比丢一次记忆更新更严重"。阶段 6.5 §三.5–6
    要求三者原子，用户选择了单事务（ADR-0021），因此：
    **记忆写入崩溃时，反馈事件也不再留下。**

    这是被选择过的代价，不是缺陷——原子性与"某个子步骤失败时
    仍保留其余部分"在定义上互斥。真正承接那条顾虑的是另一条路径：
    **写入策略拒绝是一条正常返回**，那一刻事务照常提交，
    用户的反馈与它带出的经验评价都留下（见下一个类）。
    """

    async def test_a_memory_crash_leaves_no_feedback_behind(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        round_id = await _make_round(uow_factory, user_id=uuid4())
        service = _service(uow_factory, _Exploding(uow_factory, LocalHashingEmbedding()))

        with pytest.raises(RuntimeError, match="模拟记忆写入过程中崩溃"):
            await service.record(
                round_id=round_id,
                feedback_type=FeedbackType.CORRECTION,
                content=CORRECTION_TEXT,
                allow_memory_update=True,
            )

        assert await _events(uow_factory, round_id) == []

    async def test_nothing_is_written_when_the_round_is_missing(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        """回合不存在时同样不留任何东西。"""
        service = _service(uow_factory, MemoryService(uow_factory, LocalHashingEmbedding()))

        with pytest.raises(NotFoundError):
            await service.record(
                round_id=uuid4(),
                feedback_type=FeedbackType.CORRECTION,
                content=CORRECTION_TEXT,
                allow_memory_update=True,
            )

        assert await _events(uow_factory, uuid4()) == []


class TestPolicyRejectionStillKeepsTheFeedback:
    """🔴 "用户的话不能丢"由**这条**路径承接，不是由事务顺序。

    策略拒绝是一次**正常返回**（``decision.allows_write`` 为假）——
    事务照常提交，反馈事件与它带出的经验评价都留下，只有记忆没写。
    这是最常见的情形；真正会丢反馈的只有数据库本身故障。
    """

    async def test_a_policy_rejection_keeps_the_feedback_event(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        round_id = await _make_round(uow_factory, user_id=uuid4())
        service = _service(
            uow_factory,
            MemoryService(uow_factory, LocalHashingEmbedding(), policy=_DenyAllPolicy()),
        )

        outcome = await service.record(
            round_id=round_id,
            feedback_type=FeedbackType.CORRECTION,
            content=CORRECTION_TEXT,
            allow_memory_update=True,
        )

        assert outcome.memory_effect is MemoryEffect.REJECTED_BY_POLICY
        assert outcome.memory is None

        events = await _events(uow_factory, round_id)
        assert [item.event_type for item in events] == [EventType.USER_FEEDBACK_RECEIVED]
        assert events[0].payload["content"] == CORRECTION_TEXT
