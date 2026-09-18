"""改进提案的生命周期（任务书 §12.4、ADR-0005）。

```
DRAFT → PENDING_EVALUATION → EVALUATED
                            ├→ REJECTED
                            └→ APPROVED_FOR_MANUAL_TRIAL
```

🔴 **状态空间里没有 ACTIVE，本模块也没有通往"生效"的方法。**

不是"还没实现"，而是**没有这个概念**：``ProposalStatus`` 里不存在
``ACTIVE``，数据库的 CHECK 由该枚举生成，因此"把提案标记为已生效"
在类型层与存储层都不可能发生（不变量 11）。
:meth:`ProposalService.approve_for_manual_trial` 批准的是
**做一次人工试验**，不是上线。

🔴 **驳回必须在评估之后。**

``REJECTED`` 只能从 ``EVALUATED`` 到达。"我不同意"很容易说，
而一条没有被评估过的提案被驳回，等于把"不想做"记成了
"做不了"——下一个人再看到同类问题时，没有任何信息可用。

🔴 **批准与驳回是并发敏感的。**

两个评审同时对同一条提案点"批准"和"驳回"时，乐观锁在这里
不是性能优化，是正确性要求：静默覆盖会得到一条**同时**被批准
和驳回的提案，而事件流里两条理由都在。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final
from uuid import UUID, uuid4

from ai_psi.application.ports import UnitOfWork, UnitOfWorkFactory
from ai_psi.domain.common import utc_now
from ai_psi.domain.enums import (
    ActorType,
    ErrorType,
    EvaluationVerdict,
    EventType,
    ProposalStatus,
    SensitivityLevel,
)
from ai_psi.domain.events import Event
from ai_psi.domain.exceptions import (
    IllegalStateTransitionError,
    InvalidRequestError,
    NotFoundError,
)
from ai_psi.domain.improvement_proposals import ImprovementProposal

__all__ = [
    "ProposalEvaluation",
    "ProposalService",
    "ProposalTransition",
]


#: 允许进入 ``EVALUATED`` 的状态。
_EVALUATABLE_FROM: Final[frozenset[ProposalStatus]] = frozenset(
    {ProposalStatus.DRAFT, ProposalStatus.PENDING_EVALUATION}
)


@dataclass(frozen=True, slots=True)
class ProposalEvaluation:
    """一次离线评估的结论。

    Attributes:
        verdict: 评估结论。
        evidence: 对照了什么——口径、样本量、参照版本。
            🔴 **不能为空**：没有口径的结论无法被复核，
            也就无法在日后被推翻。
        notes: 评审人的说明。驳回与被批准的决定都要能解释自己。
    """

    verdict: EvaluationVerdict
    evidence: tuple[str, ...] = field(default=())
    notes: str | None = None


@dataclass(frozen=True, slots=True)
class ProposalTransition:
    """一次状态流转的结果。

    Attributes:
        proposal: 流转后的提案（``version`` 已递增）。
        event: 本次流转留下的审计事件。
    """

    proposal: ImprovementProposal
    event: Event


class ProposalService:
    """改进提案的读写入口。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        """初始化。

        Args:
            uow_factory: 工作单元工厂。提案的状态变更与它的事件
                在同一个事务里——"改了状态却没留下审批记录"
                正是这条链路最不能出的一类缺陷。
        """
        self._uow_factory = uow_factory

    # ------------------------------------------------------------------
    # 生成与读取
    # ------------------------------------------------------------------

    async def create(
        self,
        proposal: ImprovementProposal,
        *,
        actor_id: str = "proposal_generator",
        correlation_id: UUID | None = None,
    ) -> ImprovementProposal:
        """写入一条新提案（状态必须是 ``DRAFT``）。

        🔴 不接受已经处于其他状态的提案：一条"生成出来就已评估"
        的提案会让"谁评估的、依据是什么"无从回答。

        Args:
            proposal: 待写入的提案。
            actor_id: 产生者标识。
            correlation_id: 关联链标识。

        Returns:
            写入的提案。

        Raises:
            IllegalStateTransitionError: 提案不是 ``DRAFT``。
        """
        if proposal.status is not ProposalStatus.DRAFT:
            self._raise_transition(
                proposal,
                to_state=ProposalStatus.DRAFT,
                message="新提案只能以 DRAFT 状态写入",
            )

        event = self._event(
            event_type=EventType.IMPROVEMENT_PROPOSAL_CREATED,
            actor_id=actor_id,
            correlation_id=correlation_id or uuid4(),
            payload={
                "proposal_id": str(proposal.id),
                "target_component": proposal.target_component,
                "error_class": proposal.error_class.value,
                "status": proposal.status.value,
                "approval_level": proposal.approval_level.value,
                "supporting_experience_count": len(proposal.supporting_experience_ids),
                "counterexample_count": len(proposal.counterexamples),
            },
        )
        async with self._uow_factory() as uow:
            await uow.proposals.add(proposal)
            await uow.events.append(event)
            await uow.commit()
        return proposal

    async def get(self, proposal_id: UUID) -> ImprovementProposal:
        """按 id 读取提案。

        Raises:
            NotFoundError: 提案不存在。
        """
        async with self._uow_factory() as uow:
            proposal = await uow.proposals.get(proposal_id)
        if proposal is None:
            msg = f"改进提案不存在：{proposal_id}"
            raise NotFoundError(msg, context={"proposal_id": str(proposal_id)})
        return proposal

    async def list_all(
        self,
        *,
        status: ProposalStatus | None = None,
        error_class: ErrorType | None = None,
        limit: int | None = None,
    ) -> list[ImprovementProposal]:
        """列出提案（顺序由仓储保证确定）。"""
        async with self._uow_factory() as uow:
            return await uow.proposals.list_all(status=status, error_class=error_class, limit=limit)

    # ------------------------------------------------------------------
    # 状态流转
    # ------------------------------------------------------------------

    async def evaluate(
        self,
        proposal_id: UUID,
        *,
        evaluation: ProposalEvaluation,
        actor_id: str = "reviewer",
        correlation_id: UUID | None = None,
    ) -> ProposalTransition:
        """记录一次离线评估，把提案推进到 ``EVALUATED``。

        ⚠️ **``PENDING_EVALUATION`` 在 V0.1 是一次原子穿越，不是停留点。**

        它表示"已登记待评估、结论还没出来"。V0.1 没有后台评测器
        （那是阶段 7 的 Eval Runner），因此不存在那个等待窗口——
        登记的下一秒结论就到了。这个状态保留在类型与数据库里，
        是为了阶段 7 接上评测器时**不需要改状态机**，
        而不是因为它在 V0.1 里能被观察到。

        Args:
            proposal_id: 目标提案。
            evaluation: 评估结论。**``evidence`` 不得为空**——
                没有口径的评估结论无法被复核，也因此无法在日后被推翻，
                而不可推翻的结论会永久影响策略。
            actor_id: 评估者标识。
            correlation_id: 关联链标识。

        Returns:
            流转结果。

        Raises:
            NotFoundError: 提案不存在。
            IllegalStateTransitionError: 提案已处于终态。
            InvalidRequestError: ``evidence`` 为空，或含有空白项。
        """
        blank = [index for index, item in enumerate(evaluation.evidence) if not item.strip()]
        if not evaluation.evidence or blank:
            # 🔴 判据是"**每一条都说了点什么**"，不是"列表长度大于零"。
            # `evidence=[""]` 的列表长度是 1，落库后正是这个字段要防的
            # 那类"无法被复核、因而也无法被推翻的记录"。
            msg = (
                "评估必须给出对照口径（evidence 不得为空、也不得全是空白）："
                "「结论：改善」而没有说跟什么比、比了多少个样本，"
                "是一条无法被复核、因而也无法被推翻的记录"
            )
            raise InvalidRequestError(
                msg,
                context={"blank_evidence_indexes": blank},
            )

        async with self._uow_factory() as uow:
            proposal = await self._require(uow, proposal_id)
            self._assert_can_enter(proposal, _EVALUATABLE_FROM, to_state=ProposalStatus.EVALUATED)

            updated = proposal.bumped(status=ProposalStatus.EVALUATED)
            event = self._event(
                event_type=EventType.IMPROVEMENT_PROPOSAL_EVALUATED,
                actor_id=actor_id,
                correlation_id=correlation_id or uuid4(),
                payload={
                    "proposal_id": str(proposal.id),
                    "from_status": proposal.status.value,
                    "to_status": updated.status.value,
                    "verdict": evaluation.verdict.value,
                    # 🔴 口径必须落库。没有口径的结论无法被复核，
                    # 因此也无法在日后被推翻——而不可推翻的结论
                    # 会永久影响策略。
                    "evidence": list(evaluation.evidence),
                    "notes": evaluation.notes,
                    "supporting_experience_count": len(proposal.supporting_experience_ids),
                },
            )
            await uow.proposals.save(updated, expected_version=proposal.version)
            await uow.events.append(event)
            await uow.commit()

        return ProposalTransition(proposal=updated, event=event)

    async def approve_for_manual_trial(
        self,
        proposal_id: UUID,
        *,
        approved_by: str,
        note: str | None = None,
        correlation_id: UUID | None = None,
    ) -> ProposalTransition:
        """批准进行**人工试验**（不变量 11）。

        🔴 这是本模块唯一能"推进"提案的出口，而它的语义是
        **批准做一次试验**，不是"上线"。提案的状态到此终结——
        没有任何后续方法能把它变成生效策略。

        🔴 **要求先评估。** 未经评估的批准等于凭印象拍板，
        而这条链路存在的全部理由就是让那个决定有据可依。

        Args:
            proposal_id: 目标提案。
            approved_by: 批准人标识。
            note: 批准说明。
            correlation_id: 关联链标识。

        Returns:
            流转结果。

        Raises:
            NotFoundError: 提案不存在。
            IllegalStateTransitionError: 提案尚未评估，或已处于终态。
        """
        return await self._transition(
            proposal_id,
            to_state=ProposalStatus.APPROVED_FOR_MANUAL_TRIAL,
            allowed_from=frozenset({ProposalStatus.EVALUATED}),
            event_type=EventType.IMPROVEMENT_PROPOSAL_APPROVED,
            actor_id=approved_by,
            extra_payload={"note": note},
            refusal=(
                "提案必须先经过评估才能批准——"
                "未经评估的批准等于凭印象拍板，"
                "而这条链路存在的全部理由就是让那个决定有据可依"
            ),
            correlation_id=correlation_id,
        )

    async def reject(
        self,
        proposal_id: UUID,
        *,
        rejected_by: str,
        reason: str,
        correlation_id: UUID | None = None,
    ) -> ProposalTransition:
        """驳回一条提案。

        🔴 **``reason`` 是必填的。** 驳回不需要理由的话，
        下一个人再遇到同类问题时只会看到"这条被拒了"，
        然后重新走一遍同样的路——而驳回的成本本该换来一条经验。

        Args:
            proposal_id: 目标提案。
            rejected_by: 驳回人标识。
            reason: 驳回理由。
            correlation_id: 关联链标识。

        Returns:
            流转结果。

        Raises:
            NotFoundError: 提案不存在。
            IllegalStateTransitionError: 提案尚未评估，或已处于终态。
            InvalidRequestError: 理由为空或只有空白。

        🔴 **空理由抛的是领域异常，不是裸 ``ValueError``。**
        裸 ``ValueError`` 不在 HTTP 状态映射表里，会让一个纯空白的
        理由变成 **500 + 一整条堆栈**——把"用户填错了"报成服务端故障，
        还会触发 `api_unexpected_error` 告警。
        """
        if not reason.strip():
            msg = "驳回必须给出理由：「不想做」与「做不了」对后来者是完全不同的信息"
            raise InvalidRequestError(msg, context={"proposal_id": str(proposal_id)})

        return await self._transition(
            proposal_id,
            to_state=ProposalStatus.REJECTED,
            allowed_from=frozenset({ProposalStatus.EVALUATED}),
            event_type=EventType.IMPROVEMENT_PROPOSAL_REJECTED,
            actor_id=rejected_by,
            extra_payload={"reason": reason},
            refusal=(
                "提案必须先经过评估才能驳回——"
                "把未经评估的「不想做」记成「做不了」，"
                "会让后来者重复走一遍同样的路"
            ),
            correlation_id=correlation_id,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _transition(
        self,
        proposal_id: UUID,
        *,
        to_state: ProposalStatus,
        allowed_from: frozenset[ProposalStatus],
        event_type: EventType,
        actor_id: str,
        extra_payload: dict[str, object],
        refusal: str,
        correlation_id: UUID | None,
    ) -> ProposalTransition:
        """通用状态流转：读 → 校验 → 乐观锁保存 → 记录事件。"""
        async with self._uow_factory() as uow:
            proposal = await self._require(uow, proposal_id)
            self._assert_can_enter(proposal, allowed_from, to_state=to_state, refusal=refusal)

            updated = proposal.bumped(status=to_state)
            event = self._event(
                event_type=event_type,
                actor_id=actor_id,
                correlation_id=correlation_id or uuid4(),
                payload={
                    "proposal_id": str(proposal.id),
                    "from_status": proposal.status.value,
                    "to_status": updated.status.value,
                    "approval_level": proposal.approval_level.value,
                    **extra_payload,
                },
            )
            # 🔴 乐观锁：两条并发的流转（批准与驳回同时到达）里
            # 后到的那条会在这里失败，而不是静默覆盖先到的那条。
            await uow.proposals.save(updated, expected_version=proposal.version)
            await uow.events.append(event)
            await uow.commit()

        return ProposalTransition(proposal=updated, event=event)

    async def _require(self, uow: UnitOfWork, proposal_id: UUID) -> ImprovementProposal:
        """从工作单元里读出提案，不存在则抛错。"""
        proposal = await uow.proposals.get(proposal_id)
        if proposal is None:
            msg = f"改进提案不存在：{proposal_id}"
            raise NotFoundError(msg, context={"proposal_id": str(proposal_id)})
        return proposal

    def _assert_can_enter(
        self,
        proposal: ImprovementProposal,
        allowed_from: frozenset[ProposalStatus],
        *,
        to_state: ProposalStatus,
        refusal: str | None = None,
    ) -> None:
        """校验当前状态能否进入目标状态。"""
        if proposal.status in allowed_from:
            return
        if proposal.status.is_terminal:
            message = (
                f"提案已处于终态 {proposal.status.value}，不能再变为 {to_state.value}。"
                "终态是终点：V0.1 没有任何从终态继续演进的路径"
            )
        else:
            message = refusal or f"不允许从 {proposal.status.value} 变为 {to_state.value}"
        self._raise_transition(proposal, to_state=to_state, message=message)

    def _raise_transition(
        self,
        proposal: ImprovementProposal,
        *,
        to_state: ProposalStatus,
        message: str,
    ) -> None:
        """抛出状态流转错误。"""
        raise IllegalStateTransitionError(
            message,
            from_state=proposal.status.value,
            to_state=to_state.value,
            context={"proposal_id": str(proposal.id)},
        )

    def _event(
        self,
        *,
        event_type: EventType,
        actor_id: str,
        correlation_id: UUID,
        payload: dict[str, object],
    ) -> Event:
        """构造一条提案事件对象（不落库）。"""
        return Event(
            event_type=event_type,
            occurred_at=utc_now(),
            actor_type=ActorType.SYSTEM,
            actor_id=actor_id,
            user_id=None,
            conversation_id=None,
            # 提案不属于任何回合：它来自跨回合的**模式**，
            # 挂到某一个回合上会误导读者以为那个回合是来源
            cognitive_round_id=None,
            correlation_id=correlation_id,
            payload=payload,
            sensitivity=SensitivityLevel.INTERNAL,
        )
