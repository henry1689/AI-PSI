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

🔴 **反馈会把对应回合的经验抬到有证据的档位。**

阶段 6.5 §二.5：只有用户明确纠正、可靠后续证据或独立评测
才能产生 ``SUPPORTED`` / ``CONFIRMED``。内部元认知最多 ``SUSPECTED``，
而**默认权重下 ``SUSPECTED`` 计 0 次**——因此没有这条路径，
"三次同类错误"这条验收条件在系统里永远凑不满。

本服务把反馈事件与经验评价事件写在**同一个事务**里：
它们表达的是同一件事（"用户此刻指出了这个回合的问题"），
分成两个事务就会出现"纠正留下了、它应该抬高的经验没有抬高"。

⚠️ **事务边界：反馈事件、经验评价、记忆更新现在在同一个事务里。**

阶段 6 曾把它们分成两个事务，理由是"丢用户的话比丢一次记忆更新更严重"。
阶段 6.5 §三.5–6 要求三者原子，因此改成单事务
（ADR-0021：该决定被修正，ADR-0018 §4 原文保留）。

不改的是那条顾虑本身，它由**两条**机制承接：

1. **写入策略拒绝是一条正常返回，不是异常。** 反馈内容命中红线时，
   ``WritePolicy`` 拒绝写入并**照常提交**——用户的反馈与它带出的
   经验评价都留下了，只有记忆没写。这是最常见的情形。
2. **真正的数据库故障会让整体回滚，用户的反馈确实会丢。**
   这是"同一事务"的固有代价，不是实现缺陷：原子性与
   "某个子步骤失败时仍保留其余部分"在定义上互斥。
   区别在于，现在这个代价是**写在 ADR 里的、被选择过的**，
   而不是两事务方案里那个没有被讨论过的副作用。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID, uuid4

from ai_psi.application.experience_reader import (
    evaluation_from_event,
    experience_from_event,
)
from ai_psi.application.memory_service import MemoryService
from ai_psi.application.ports import UnitOfWork, UnitOfWorkFactory
from ai_psi.domain.common import utc_now
from ai_psi.domain.enums import (
    ActorType,
    EventType,
    ExperienceEvaluation,
    ExperienceEvaluator,
    FeedbackType,
    MemoryType,
    SensitivityLevel,
)
from ai_psi.domain.events import Event
from ai_psi.domain.exceptions import NotFoundError
from ai_psi.domain.experiences import (
    Experience,
    ExperienceEvaluationRecord,
    assess_experiences,
)
from ai_psi.domain.memories import Memory
from ai_psi.memory.write_policy import MemoryWriteProposal

__all__ = [
    "EVALUATION_BY_FEEDBACK_TYPE",
    "FEEDBACK_MEMORY_TYPE",
    "MEMORY_UPDATING_FEEDBACK_TYPES",
    "FeedbackOutcome",
    "FeedbackService",
    "MemoryEffect",
]

#: 反馈类型 → 它能把经验抬到的最高评价档位（阶段 6.5 §二.5）。
#:
#: 🔴 **白名单。** 没列进来的反馈类型**不产生任何评价**——
#: 新增一个反馈类型时，默认行为必须是"它不改变经验的可信度"，
#: 而不是"它悄悄地把经验抬高了一档"。
#:
#: 档位的选择依据是**这句话说了什么**：
#:
#: * ``CORRECTION``——"你这里错了，应该是 X"。它指出了具体问题，
#:   是用户能给出的最强确认 → ``CONFIRMED``；
#: * ``DISAGREEMENT``——"我不同意"。它表达了否定但没说对的是什么，
#:   因此强度低一档 → ``SUPPORTED``；
#: * ``CLARIFICATION`` / ``AGREEMENT`` / ``ACKNOWLEDGEMENT`` / ``RATING``
#:   ——补充信息、赞同、"收到"、打分。它们说的是"好不好"或
#:   "还有别的情况"，**不是"这里错了"**，因此不在此表中。
EVALUATION_BY_FEEDBACK_TYPE: Final[dict[FeedbackType, ExperienceEvaluation]] = {
    FeedbackType.CORRECTION: ExperienceEvaluation.CONFIRMED,
    FeedbackType.DISAGREEMENT: ExperienceEvaluation.SUPPORTED,
}

#: 用户纠正这条评价路径的版本。
EVALUATOR_VERSION: Final[str] = "user-correction/1"


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
        evaluations: 本次反馈对经验评价的**实际改动**（阶段 6.5 §二.5）。
            空元组表示这次反馈没有改变任何经验的评价——可能是反馈类型
            本就不携带"这里错了"的信息，也可能是对应经验的档位已经更高。
            两者的区别在 ``reasons`` 里。
        reasons: 逐条可读的说明——**包括"为什么没写记忆"**。
    """

    round_id: UUID
    feedback_type: FeedbackType
    event: Event
    memory_effect: MemoryEffect = MemoryEffect.NONE
    memory: Memory | None = None
    evaluations: tuple[ExperienceEvaluationRecord, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def memory_written(self) -> bool:
        """是否真的产生了新记忆。"""
        return self.memory is not None

    @property
    def evaluation_changed(self) -> bool:
        """本次反馈是否提高了至少一条经验的评价。"""
        return bool(self.evaluations)


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

        # 🔴 **整段在一个事务里**（阶段 6.5 §三.5–6）：
        # 反馈事件、经验评价、记忆更新要么都留下，要么都不留。
        async with self._uow_factory() as uow:
            round_ = await uow.rounds.get(round_id)
            if round_ is None:
                msg = f"认知回合不存在：{round_id}"
                raise NotFoundError(msg, context={"cognitive_round_id": str(round_id)})
            user_id = round_.user_id

            # 先算清"这次反馈够不够格写记忆"，再让事件如实记下这个判定。
            ineligible = (
                self._memory_update_blocker(feedback_type=feedback_type, user_id=user_id)
                if allow_memory_update
                else None
            )

            event = self._feedback_event(
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
            await uow.events.append(event)

            # 🔴 反馈对**经验评价**的影响，与反馈事件同事务（§二.5）。
            evaluations, evaluation_reasons = await self._evaluate_experiences(
                uow=uow,
                round_id=round_id,
                user_id=user_id,
                feedback_type=feedback_type,
                correlation_id=correlation,
                actor_id=actor_id,
                feedback_event_id=event.id,
            )

            effect, memory, memory_reasons = await self._memory_step(
                uow=uow,
                round_id=round_id,
                user_id=user_id,
                correlation_id=correlation,
                actor_id=actor_id,
                feedback_type=feedback_type,
                content=content,
                feedback_event_id=event.id,
                allow_memory_update=allow_memory_update,
                ineligible=ineligible,
            )

            await uow.commit()

        return FeedbackOutcome(
            round_id=round_id,
            feedback_type=feedback_type,
            event=event,
            memory_effect=effect,
            memory=memory,
            evaluations=evaluations,
            reasons=(*evaluation_reasons, *memory_reasons),
        )

    async def _memory_step(
        self,
        *,
        uow: UnitOfWork,
        round_id: UUID,
        user_id: UUID | None,
        correlation_id: UUID,
        actor_id: str,
        feedback_type: FeedbackType,
        content: str,
        feedback_event_id: UUID,
        allow_memory_update: bool,
        ineligible: str | None,
    ) -> tuple[MemoryEffect, Memory | None, tuple[str, ...]]:
        """在**已开启的事务**里走记忆写入流程。

        🔴 **返回原因而不是抛异常。** 记忆被策略拒绝是这条链路的
        正常分支之一（用户说了句命中红线的话），它不该让整个事务
        回滚——那样连反馈本身都留不下。

        Returns:
            ``(影响, 写入的记忆或 None, 理由)``。
        """
        if not allow_memory_update:
            # 🔴 "没请求"与"请求了但不满足"是两回事，不合并成一种结果。
            # 合并之后，调用方无法区分"我忘了开开关"与"我开了但被规则挡住了"。
            return (
                MemoryEffect.NONE,
                None,
                ("本次反馈未请求更新记忆（allow_memory_update=false）",),
            )

        if ineligible is not None:
            return (MemoryEffect.NOT_ELIGIBLE, None, (ineligible,))

        # 到此 ``user_id`` 必定非空：匿名回合会被上面那条判定挡住。
        if user_id is None:  # pragma: no cover - 上一条判定已覆盖，防御性保留
            return (MemoryEffect.NOT_ELIGIBLE, None, (_NO_SCOPE_REASON,))

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
            # 🔴 把事务交出去：记忆写入长在反馈的事务上（§三.5–6）
            uow=uow,
        )

        if outcome.written:
            return (MemoryEffect.WRITTEN, outcome.memory, ("反馈已写入长期记忆",))
        if outcome.duplicate_of is not None:
            return (MemoryEffect.DUPLICATE, None, ("内容与既有记忆完全相同，未重复写入",))
        return (
            MemoryEffect.REJECTED_BY_POLICY,
            None,
            (f"写入策略未放行（{outcome.decision.value}）", *outcome.reasons),
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

    # ------------------------------------------------------------------
    # 经验评价（阶段 6.5 §二.5）
    # ------------------------------------------------------------------

    async def _evaluate_experiences(
        self,
        *,
        uow: UnitOfWork,
        round_id: UUID,
        user_id: UUID | None,
        feedback_type: FeedbackType,
        correlation_id: UUID,
        actor_id: str,
        feedback_event_id: UUID,
    ) -> tuple[tuple[ExperienceEvaluationRecord, ...], tuple[str, ...]]:
        """把本回合的经验抬到这次反馈所支持的档位。

        🔴 **这是"内部元认知最多 SUSPECTED"这条规则的出口。**

        没有它，生产里能产出的经验永远停在 ``SUSPECTED``，
        而 ``SUSPECTED`` 在默认权重下计 0 次——"三次同类错误
        生成提案"因此在系统里**永远不可能发生**。

        档位映射（§二.5）：

        * ``CORRECTION``——用户**指出了哪里不对**，这是明确的确认，
          抬到 ``CONFIRMED``；
        * ``DISAGREEMENT``——用户只说"不同意"，没说对的是什么，
          抬到 ``SUPPORTED``；
        * 其余类型（赞同 / 确认收到 / 评分 / 澄清）**不产生评价**。
          澄清与评分说的是"补充信息"和"好不好"，不是"这里错了"。

        ⚠️ **只在真的会抬高时才写。** 用户对同一个回合点两次纠正
        不该产生两条评价记录——那不是"更确认"，只是重复。

        Args:
            uow: 已开启的工作单元（与反馈事件同一个事务）。
            round_id: 被反馈的回合。
            user_id: 归属用户。
            feedback_type: 反馈类型。
            correlation_id: 关联链标识。
            actor_id: 发起者标识。
            feedback_event_id: 反馈事件 id，作为评价的证据引用。

        Returns:
            ``(写入的评价记录, 理由)``。
        """
        target = EVALUATION_BY_FEEDBACK_TYPE.get(feedback_type)
        if target is None:
            return (), ()

        events = await uow.events.read_stream(cognitive_round_id=round_id)
        created = [item for item in events if item.event_type is EventType.EXPERIENCE_CREATED]
        if not created:
            return (
                (),
                (
                    "本回合没有产出经验（失败回合、或该回合关闭了长期记忆），"
                    "本次反馈没有被记到任何经验上",
                ),
            )

        experiences = [
            item for item in (experience_from_event(event) for event in created) if item is not None
        ]
        current = await self._current_evaluations(uow, experiences)

        written: list[ExperienceEvaluationRecord] = []
        skipped = 0
        for experience in experiences:
            if current.get(experience.id, experience.evaluation).rank >= target.rank:
                skipped += 1
                continue
            record = ExperienceEvaluationRecord(
                created_by=actor_id,
                experience_id=experience.id,
                experience_canonical_key=experience.canonical_key,
                evaluation=target,
                evaluator_type=ExperienceEvaluator.USER_CORRECTION,
                evaluator_version=EVALUATOR_VERSION,
                evidence_refs=[feedback_event_id],
                reasons=[
                    f"用户在回合 {round_id} 上给出了 {feedback_type.value} 反馈",
                    f"评价由 {self._describe_prior(current.get(experience.id))} "
                    f"抬到 {target.value}",
                ],
                evaluated_at=utc_now(),
            )
            await uow.events.append(
                Event(
                    event_type=EventType.EXPERIENCE_EVALUATED,
                    occurred_at=utc_now(),
                    actor_type=ActorType.USER,
                    actor_id=actor_id,
                    user_id=user_id,
                    conversation_id=None,
                    cognitive_round_id=round_id,
                    correlation_id=correlation_id,
                    payload={"evaluation": record.model_dump(mode="json")},
                    sensitivity=SensitivityLevel.INTERNAL,
                )
            )
            written.append(record)

        reasons: list[str] = []
        if written:
            reasons.append(
                f"{len(written)} 条经验的评价被抬到 {target.value}"
                f"（依据：{feedback_type.value} 反馈）"
            )
        if skipped:
            reasons.append(f"{skipped} 条经验的评价已不低于 {target.value}，未重复记")
        return tuple(written), tuple(reasons)

    async def _current_evaluations(
        self,
        uow: UnitOfWork,
        experiences: list[Experience],
    ) -> dict[UUID, ExperienceEvaluation]:
        """本回合这些经验**当前**的有效评价。"""
        if not experiences:
            return {}
        known = {item.id for item in experiences}
        records = [
            record
            for event in await uow.events.read_by_event_type(
                event_type=EventType.EXPERIENCE_EVALUATED
            )
            if (record := evaluation_from_event(event)) is not None
            and record.experience_id in known
        ]
        return {
            item.experience.id: item.evaluation for item in assess_experiences(experiences, records)
        }

    @staticmethod
    def _describe_prior(evaluation: ExperienceEvaluation | None) -> str:
        """把"抬升前是什么"写进理由。"""
        return evaluation.value if evaluation is not None else "unassessed"

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------

    def _feedback_event(
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
        """构造 ``user.feedback.received`` 事件（**不落库**）。

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
        return Event(
            event_type=EventType.USER_FEEDBACK_RECEIVED,
            occurred_at=utc_now(),
            actor_type=ActorType.USER,
            actor_id=actor_id,
            user_id=user_id,
            conversation_id=None,
            # 🔴 挂在被反馈的回合上：§12.2 的反馈是**回合级**的，
            # 因此"这个回合收到了什么反馈"沿事件流就能读出来
            cognitive_round_id=round_id,
            correlation_id=correlation_id,
            payload=payload,
            sensitivity=SensitivityLevel.PERSONAL,
        )
