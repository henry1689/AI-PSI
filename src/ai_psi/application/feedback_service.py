"""反馈与纠正的应用服务（任务书 §12.2、§10.4）。

用户对某个回合给出反馈：纠正、澄清、异议、赞同、评分。

🔴 **反馈不得绕过写入策略。**

``allow_memory_update`` 不是"允许直接写记忆"，而是"允许把这次反馈
**提给**记忆写入流程"。真正的写入仍然走
``MemoryWriteProposal → WritePolicy → MemoryService``（ADR-0004）。
把这条路径短路掉——比如在这里直接构造 ``Memory`` 落库——
会让反馈变成一条绕过全部记忆红线的旁路，而它看起来只是一次用户操作。

🔴 **只有 CORRECTION 与 CLARIFICATION 可以带出记忆。**

这两类是**唯一**提供了新信息的反馈："我说的其实是 X"。
``AGREEMENT`` / ``ACKNOWLEDGEMENT`` / ``RATING`` 不含关于世界或用户的
新事实；``DISAGREEMENT`` 只说"你错了"，没说对的应该是什么。
用它们去写记忆，等于把噪声记成事实——而记忆一旦写错，
它会在之后**每一轮**里持续影响判断。

不满足条件时结果里会**显式说明为什么没写**，而不是静默跳过：
用户打开了开关却什么都没发生，必须能从返回值里看出原因。

⚠️ **事务边界：反馈事件先落，记忆更新后做，两步不在同一个事务里。**

这是刻意的。反过来（记忆先写、事件后记）的失败后果是
"改了却没有记录"（ADR-0015 §5）——但在这里，另一个方向的失败更严重：
**用户说过的话丢失了**。记忆没更新上，用户再说一次即可；
反馈正文没记下来，它就真的没了。

两步通过共同的 ``correlation_id`` 关联（记忆提案的 ``source_event_ids``
直接指向那条反馈事件），因此"这条记忆为什么会出现"仍然可以追回来。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from ai_psi.application.memory_service import MemoryService
from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.domain.common import utc_now
from ai_psi.domain.enums import (
    ActorType,
    EventType,
    FeedbackType,
    MemoryType,
    SensitivityLevel,
)
from ai_psi.domain.events import Event
from ai_psi.domain.exceptions import NotFoundError
from ai_psi.domain.memories import Memory
from ai_psi.memory.write_policy import MemoryWriteProposal

__all__ = [
    "FEEDBACK_MEMORY_TYPE",
    "MEMORY_UPDATING_FEEDBACK_TYPES",
    "FeedbackOutcome",
    "FeedbackService",
    "MemoryEffect",
]


#: 能够带出一条记忆的反馈类型。
#:
#: 🔴 白名单，不是黑名单。新增反馈类型时默认**不允许**带出记忆——
#: 一个没想到过的新类型应该先被讨论，而不是先获得写记忆的能力。
MEMORY_UPDATING_FEEDBACK_TYPES: Final[frozenset[FeedbackType]] = frozenset(
    {
        FeedbackType.CORRECTION,
        FeedbackType.CLARIFICATION,
    }
)

#: 反馈带出的记忆使用哪一种类型。
#:
#: ``USER_PREFERENCE`` 是**当前唯一**既可自动写入、又适合承载
#: "用户陈述自己立场与意图"的类型：
#:
#: * ``USER_CONFIRMED_FACT`` 需要人工评审之外还要走白名单，
#:   而它并不在白名单里——用它会得到一个永远不会落库的路径；
#: * ``SEMANTIC``（事实断言）必须人工评审，反馈不该开后门绕过它；
#: * ``EPISODIC`` / ``FAILURE_CASE`` 描述的是回合事实，不是用户陈述。
FEEDBACK_MEMORY_TYPE: Final[MemoryType] = MemoryType.USER_PREFERENCE

#: 回合没有归属用户时的拒绝理由。
#:
#: 无归属用户 → 写下去的会是一条 ``user_id=None`` 的**全局**记忆，
#: 它会出现在所有人的检索结果里——那比不写糟得多（不变量 14）。
_NO_SCOPE_REASON: Final[str] = "该回合没有归属用户，无法确定记忆的作用域（不变量 14）"


class MemoryEffect(StrEnum):
    """一次反馈对长期记忆产生的结果。"""

    NONE = "none"
    """未请求更新记忆。"""

    NOT_ELIGIBLE = "not_eligible"
    """请求了，但这次反馈不满足更新记忆的条件。"""

    WRITTEN = "written"
    """已写入一条新记忆。"""

    DUPLICATE = "duplicate"
    """内容与既有记忆完全相同，未重复写入。"""

    REJECTED_BY_POLICY = "rejected_by_policy"
    """写入策略未放行（含需要额外确认的情形）。"""


@dataclass(frozen=True, slots=True)
class FeedbackOutcome:
    """一次反馈处理的结果。

    Attributes:
        round_id: 被反馈的回合。
        feedback_type: 反馈类型。
        event: 本次反馈留下的审计事件。
        memory_effect: 对长期记忆的实际影响。
        memory: 写入的记忆；没有写入时为 ``None``。
        reasons: 逐条可读的说明——**包括"为什么没写记忆"**。
    """

    round_id: UUID
    feedback_type: FeedbackType
    event: Event
    memory_effect: MemoryEffect = MemoryEffect.NONE
    memory: Memory | None = None
    reasons: tuple[str, ...] = ()

    @property
    def memory_written(self) -> bool:
        """是否真的产生了新记忆。"""
        return self.memory is not None


class FeedbackService:
    """反馈的落地入口。"""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        memory_service: MemoryService,
    ) -> None:
        """初始化。

        Args:
            uow_factory: 工作单元工厂（用于读回合与写反馈事件）。
            memory_service: 记忆服务。**必须是同一个实例**——
                另造一个会让反馈路径与其余路径各持一份写入策略，
                策略一旦被局部替换就会分家。
        """
        self._uow_factory = uow_factory
        self._memory_service = memory_service

    @property
    def memory_service(self) -> MemoryService:
        """当前使用的记忆服务。"""
        return self._memory_service

    async def record(
        self,
        *,
        round_id: UUID,
        feedback_type: FeedbackType,
        content: str,
        related_claim: str | None = None,
        allow_memory_update: bool = False,
        actor_id: str = "user",
    ) -> FeedbackOutcome:
        """记录一次反馈，并在允许时尝试更新长期记忆。

        Args:
            round_id: 被反馈的认知回合。
            feedback_type: 反馈类型。
            content: 反馈正文。
            related_claim: 用户指出的、被纠正的具体说法。
            allow_memory_update: 是否允许本次反馈更新长期记忆。
                🔴 这是"允许提给写入流程"，不是"允许写入"。
            actor_id: 发起者标识。

        Returns:
            处理结果。

        Raises:
            NotFoundError: 回合不存在。
        """
        correlation = uuid4()
        user_id = await self._require_round(round_id)

        # 先算清"这次反馈够不够格写记忆"，再让事件如实记下这个判定。
        ineligible = (
            self._memory_update_blocker(feedback_type=feedback_type, user_id=user_id)
            if allow_memory_update
            else None
        )

        # 🔴 **反馈事件先落库。** 见模块文档：用户说过的话不能丢。
        event = await self._record_feedback(
            round_id=round_id,
            user_id=user_id,
            correlation_id=correlation,
            actor_id=actor_id,
            feedback_type=feedback_type,
            content=content,
            related_claim=related_claim,
            allow_memory_update=allow_memory_update,
            eligible=allow_memory_update and ineligible is None,
            ineligible_reason=ineligible,
        )

        if not allow_memory_update:
            # 🔴 "没请求"与"请求了但不满足"是两回事，不合并成一种结果。
            # 合并之后，调用方无法区分"我忘了开开关"与"我开了但被规则挡住了"。
            return FeedbackOutcome(
                round_id=round_id,
                feedback_type=feedback_type,
                event=event,
                memory_effect=MemoryEffect.NONE,
                reasons=("本次反馈未请求更新记忆（allow_memory_update=false）",),
            )

        if ineligible is not None:
            return FeedbackOutcome(
                round_id=round_id,
                feedback_type=feedback_type,
                event=event,
                memory_effect=MemoryEffect.NOT_ELIGIBLE,
                reasons=(ineligible,),
            )

        # 到此 ``user_id`` 必定非空：匿名回合会被上面那条判定挡住。
        if user_id is None:  # pragma: no cover - 上一条判定已覆盖，防御性保留
            return FeedbackOutcome(
                round_id=round_id,
                feedback_type=feedback_type,
                event=event,
                memory_effect=MemoryEffect.NOT_ELIGIBLE,
                reasons=(_NO_SCOPE_REASON,),
            )

        return await self._update_memory(
            round_id=round_id,
            user_id=user_id,
            correlation_id=correlation,
            actor_id=actor_id,
            feedback_type=feedback_type,
            content=content,
            feedback_event_id=event.id,
            event=event,
        )

    # ------------------------------------------------------------------
    # 记忆更新
    # ------------------------------------------------------------------

    def _memory_update_blocker(
        self,
        *,
        feedback_type: FeedbackType,
        user_id: UUID | None,
    ) -> str | None:
        """判断本次反馈能否带出记忆（调用方已确认确实请求了更新）。

        Returns:
            ``None`` 表示可以；否则是**不能的原因**。
            返回原因而不是布尔，是因为"为什么没写"必须能被读出来。
        """
        if feedback_type not in MEMORY_UPDATING_FEEDBACK_TYPES:
            allowed = "、".join(sorted(item.value for item in MEMORY_UPDATING_FEEDBACK_TYPES))
            return (
                f"反馈类型 {feedback_type.value} 不携带新信息，不作为记忆来源"
                f"（允许的类型：{allowed}）——"
                "赞同与评分说的是「你做得对不对」，不是「事实是什么」"
            )
        if user_id is None:
            return _NO_SCOPE_REASON
        return None

    async def _update_memory(
        self,
        *,
        round_id: UUID,
        user_id: UUID,
        correlation_id: UUID,
        actor_id: str,
        feedback_type: FeedbackType,
        content: str,
        feedback_event_id: UUID,
        event: Event,
    ) -> FeedbackOutcome:
        """把反馈提给记忆写入流程。

        🔴 构造的是 :class:`MemoryWriteProposal`，不是 :class:`Memory`。
        前者没有写入权限，必须经过 ``WritePolicy`` 才能变成后者。

        ``user_confirmed=True`` 在这里是**如实陈述**，不是绕过确认：
        策略要求的"用户在当前系统中明确确认"，指的正是
        "用户此刻在本系统里亲手写下了这句话"——
        而这条反馈本身就是那个动作。
        """
        outcome = await self._memory_service.propose(
            proposal=MemoryWriteProposal(
                user_id=user_id,
                memory_type=FEEDBACK_MEMORY_TYPE,
                content=content,
                sensitivity=SensitivityLevel.PERSONAL,
                # 指向那条反馈事件——"这条记忆为什么会出现"因此可追
                source_event_ids=(feedback_event_id,),
                user_confirmed=True,
            ),
            actor_id=actor_id,
            correlation_id=correlation_id,
        )

        if outcome.written:
            return FeedbackOutcome(
                round_id=round_id,
                feedback_type=feedback_type,
                event=event,
                memory_effect=MemoryEffect.WRITTEN,
                memory=outcome.memory,
                reasons=("反馈已写入长期记忆",),
            )

        if outcome.duplicate_of is not None:
            return FeedbackOutcome(
                round_id=round_id,
                feedback_type=feedback_type,
                event=event,
                memory_effect=MemoryEffect.DUPLICATE,
                reasons=("内容与既有记忆完全相同，未重复写入",),
            )

        return FeedbackOutcome(
            round_id=round_id,
            feedback_type=feedback_type,
            event=event,
            memory_effect=MemoryEffect.REJECTED_BY_POLICY,
            reasons=(
                f"写入策略未放行（{outcome.decision.value}）",
                *outcome.reasons,
            ),
        )

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------

    async def _require_round(self, round_id: UUID) -> UUID | None:
        """确认回合存在，并返回它的归属用户。

        Raises:
            NotFoundError: 回合不存在。
        """
        async with self._uow_factory() as uow:
            round_ = await uow.rounds.get(round_id)
        if round_ is None:
            msg = f"认知回合不存在：{round_id}"
            raise NotFoundError(msg, context={"cognitive_round_id": str(round_id)})
        return round_.user_id

    async def _record_feedback(
        self,
        *,
        round_id: UUID,
        user_id: UUID | None,
        correlation_id: UUID,
        actor_id: str,
        feedback_type: FeedbackType,
        content: str,
        related_claim: str | None,
        allow_memory_update: bool,
        eligible: bool,
        ineligible_reason: str | None,
    ) -> Event:
        """写入 ``user.feedback.received`` 事件。

        🔴 **负载里有 ``content``，而且不能没有。**

        这与记忆审计负载的脱敏规则（§10.5）**不冲突**：那条规则管的是
        "不得保留**被删除内容**的正文"，而反馈正文是**对话记录**——
        事件流里本来就存着 ``user.message.received`` 的正文。
        把反馈正文摘掉，这条事件就只剩「用户点过一次纠正」，
        事后既说不清他纠正了什么，也说不清那条记忆是从哪句话来的。

        ⚠️ 因此它也继承了同一条边界：``DELETE /users/{id}/data``
        只覆盖记忆，**不覆盖事件流**。这一点在接口文档里对用户明说了。

        🔴 ``memory_effect`` **不在这里预测**。事件只记录这次请求是
        什么、以及是否够格提给写入流程；实际结果是记忆层自己的事件
        （``memory.approved`` / ``memory.rejected``）说的。
        写一个猜的结果进不可变更的事件里，就是在制造一条会过期的记录。
        """
        payload: dict[str, object] = {
            "feedback_type": feedback_type.value,
            "content": content,
            "content_length": len(content),
            "related_claim": related_claim,
            "allow_memory_update": allow_memory_update,
            "memory_update_eligible": eligible,
            "memory_update_ineligible_reason": ineligible_reason,
        }
        event = self._event(
            event_type=EventType.USER_FEEDBACK_RECEIVED,
            round_id=round_id,
            user_id=user_id,
            correlation_id=correlation_id,
            actor_id=actor_id,
            payload=payload,
            sensitivity=SensitivityLevel.PERSONAL,
        )
        async with self._uow_factory() as uow:
            await uow.events.append(event)
            await uow.commit()
        return event

    def _event(
        self,
        *,
        event_type: EventType,
        round_id: UUID,
        user_id: UUID | None,
        correlation_id: UUID,
        actor_id: str,
        payload: Mapping[str, object],
        sensitivity: SensitivityLevel,
    ) -> Event:
        """构造一条反馈事件对象（不落库）。"""
        return Event(
            event_type=event_type,
            occurred_at=utc_now(),
            actor_type=ActorType.USER,
            actor_id=actor_id,
            user_id=user_id,
            conversation_id=None,
            # 🔴 挂在被反馈的回合上：§12.2 的反馈是**回合级**的，
            # 因此"这个回合收到了什么反馈"沿事件流就能读出来
            cognitive_round_id=round_id,
            correlation_id=correlation_id,
            payload=dict(payload),
            sensitivity=sensitivity,
        )
