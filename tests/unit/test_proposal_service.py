"""改进提案的生命周期（任务书 §12.4、ADR-0005）。

🔴 本文件有三条主线：

* **状态机不被绕过**——未经评估不能批准，终态不会再次流动；
* **每一次流转都留痕**——状态变了而事件没写，是这条链路最不能出的缺陷；
* **没有通往"生效"的路**——不是没实现，是类型里根本没有那个值。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.application.proposal_service import (
    ProposalEvaluation,
    ProposalService,
    ProposalTransition,
)
from ai_psi.domain.enums import ErrorType, EvaluationVerdict, EventType, ProposalStatus
from ai_psi.domain.exceptions import (
    IllegalStateTransitionError,
    NotFoundError,
    OptimisticLockError,
)
from ai_psi.domain.improvement_proposals import ImprovementProposal
from ai_psi.infrastructure.in_memory.store import InMemoryStore
from ai_psi.infrastructure.in_memory.unit_of_work import make_in_memory_unit_of_work_factory
from ai_psi.providers.embeddings import LocalHashingEmbedding

pytestmark = pytest.mark.unit


@pytest.fixture
def uow_factory() -> UnitOfWorkFactory:
    return make_in_memory_unit_of_work_factory(InMemoryStore(), LocalHashingEmbedding())


@pytest.fixture
def service(uow_factory: UnitOfWorkFactory) -> ProposalService:
    return ProposalService(uow_factory)


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


async def _events(uow_factory: UnitOfWorkFactory, event_type: EventType):
    async with uow_factory() as uow:
        return await uow.events.read_by_event_type(event_type=event_type)


def _evaluation(verdict: EvaluationVerdict) -> ProposalEvaluation:
    """构造一条**带口径**的评估——没有口径的评估会被服务拒绝。"""
    return ProposalEvaluation(verdict=verdict, evidence=("历史回放 200 回合",))


async def _drafted(service: ProposalService) -> ImprovementProposal:
    return await service.create(_proposal())


async def _evaluated(service: ProposalService) -> ImprovementProposal:
    proposal = await _drafted(service)
    return (
        await service.evaluate(
            proposal.id,
            evaluation=_evaluation(EvaluationVerdict.IMPROVED),
        )
    ).proposal


class TestCreate:
    async def test_created_as_draft(self, service: ProposalService) -> None:
        proposal = await _drafted(service)
        assert proposal.status is ProposalStatus.DRAFT
        assert (await service.get(proposal.id)).status is ProposalStatus.DRAFT

    async def test_creation_leaves_an_event(
        self, service: ProposalService, uow_factory: UnitOfWorkFactory
    ) -> None:
        proposal = await _drafted(service)
        events = await _events(uow_factory, EventType.IMPROVEMENT_PROPOSAL_CREATED)
        assert [event.payload["proposal_id"] for event in events] == [str(proposal.id)]

    async def test_creation_event_records_the_evidence_count(
        self, service: ProposalService, uow_factory: UnitOfWorkFactory
    ) -> None:
        """条数对应不变量 10 的门槛——"它凭什么成为提案"要能只查事件就答出来。"""
        experiences = [uuid4() for _ in range(3)]
        await service.create(_proposal(supporting_experience_ids=experiences))
        payload = (await _events(uow_factory, EventType.IMPROVEMENT_PROPOSAL_CREATED))[0].payload
        assert payload["supporting_experience_count"] == 3

    @pytest.mark.parametrize("status", [ProposalStatus.EVALUATED, ProposalStatus.REJECTED])
    async def test_non_draft_proposals_are_rejected(
        self, service: ProposalService, status: ProposalStatus
    ) -> None:
        """🔴 一条"生成出来就已评估"的提案，会让"谁评估的、依据是什么"无从回答。"""
        with pytest.raises(IllegalStateTransitionError):
            await service.create(_proposal(status=status))


class TestReads:
    async def test_unknown_proposal_is_not_found(self, service: ProposalService) -> None:
        with pytest.raises(NotFoundError, match="改进提案不存在"):
            await service.get(uuid4())

    async def test_list_starts_empty(self, service: ProposalService) -> None:
        assert await service.list_all() == []

    async def test_list_returns_created_proposals(self, service: ProposalService) -> None:
        first = await _drafted(service)
        second = await _drafted(service)
        assert {item.id for item in await service.list_all()} == {first.id, second.id}

    async def test_list_filters_by_status(self, service: ProposalService) -> None:
        draft = await _drafted(service)
        evaluated = await _evaluated(service)

        drafts = await service.list_all(status=ProposalStatus.DRAFT)
        assert [item.id for item in drafts] == [draft.id]
        assert [item.id for item in await service.list_all(status=ProposalStatus.EVALUATED)] == [
            evaluated.id
        ]

    async def test_list_filters_by_error_class(self, service: ProposalService) -> None:
        reasoning = await _drafted(service)
        await service.create(_proposal(error_class=ErrorType.SCOPE_ERROR))
        found = await service.list_all(error_class=ErrorType.SCOPE_ERROR)
        assert reasoning.id not in {item.id for item in found}
        assert len(found) == 1

    async def test_list_respects_limit(self, service: ProposalService) -> None:
        for _ in range(3):
            await _drafted(service)
        assert len(await service.list_all(limit=2)) == 2


class TestEvaluation:
    async def test_draft_becomes_evaluated(self, service: ProposalService) -> None:
        proposal = await _drafted(service)
        transition = await service.evaluate(
            proposal.id,
            evaluation=_evaluation(EvaluationVerdict.NO_CHANGE),
        )
        assert transition.proposal.status is ProposalStatus.EVALUATED
        assert transition.proposal.version == proposal.version + 1

    async def test_pending_evaluation_can_be_evaluated(self, service: ProposalService) -> None:
        """``PENDING_EVALUATION`` 是待评估状态；它能进入 ``EVALUATED``。"""
        proposal = await _drafted(service)
        async with service._uow_factory() as uow:
            await uow.proposals.save(
                proposal.bumped(status=ProposalStatus.PENDING_EVALUATION),
                expected_version=proposal.version,
            )
            await uow.commit()

        transition = await service.evaluate(
            proposal.id, evaluation=_evaluation(EvaluationVerdict.IMPROVED)
        )
        assert transition.proposal.status is ProposalStatus.EVALUATED

    async def test_verdict_and_evidence_are_recorded(
        self, service: ProposalService, uow_factory: UnitOfWorkFactory
    ) -> None:
        """🔴 口径必须落库：没有口径的结论无法被复核，也因此无法被推翻。"""
        proposal = await _drafted(service)
        await service.evaluate(
            proposal.id,
            evaluation=ProposalEvaluation(
                verdict=EvaluationVerdict.REGRESSED,
                evidence=("历史回放 200 回合", "对照：其他情境未退化"),
                notes="目标情境改善但对照组持平",
            ),
        )

        payload = (await _events(uow_factory, EventType.IMPROVEMENT_PROPOSAL_EVALUATED))[0].payload
        assert payload["verdict"] == "regressed"
        assert payload["evidence"] == ["历史回放 200 回合", "对照：其他情境未退化"]
        assert payload["from_status"] == "draft"
        assert payload["to_status"] == "evaluated"

    async def test_inconclusive_is_a_real_verdict(self, service: ProposalService) -> None:
        """🔴 「看不出」与「没差」是两回事，不能被合并成一个值。"""
        proposal = await _drafted(service)
        transition = await service.evaluate(
            proposal.id,
            evaluation=_evaluation(EvaluationVerdict.INCONCLUSIVE),
        )
        assert transition.proposal.status is ProposalStatus.EVALUATED

    async def test_terminal_proposal_cannot_be_evaluated(self, service: ProposalService) -> None:
        proposal = await _evaluated(service)
        await service.reject(proposal.id, rejected_by="reviewer", reason="不值得做")

        with pytest.raises(IllegalStateTransitionError, match="终态"):
            await service.evaluate(proposal.id, evaluation=_evaluation(EvaluationVerdict.IMPROVED))

    async def test_unknown_proposal_is_not_found(self, service: ProposalService) -> None:
        with pytest.raises(NotFoundError):
            await service.evaluate(
                uuid4(),
                evaluation=ProposalEvaluation(
                    verdict=EvaluationVerdict.IMPROVED, evidence=("历史回放 200 回合",)
                ),
            )

    async def test_evidence_is_required(self, service: ProposalService) -> None:
        """🔴 「结论：改善」而没说跟什么比、比了多少样本，不是一条可复核的记录。

        没有口径的结论无法被复核，因此也无法在日后被推翻——
        而不可推翻的结论会永久影响策略。
        """
        proposal = await _drafted(service)
        with pytest.raises(ValueError, match="对照口径"):
            await service.evaluate(
                proposal.id,
                evaluation=ProposalEvaluation(verdict=EvaluationVerdict.IMPROVED),
            )

    async def test_a_refused_evaluation_leaves_nothing_behind(
        self, service: ProposalService, uow_factory: UnitOfWorkFactory
    ) -> None:
        proposal = await _drafted(service)
        with pytest.raises(ValueError):
            await service.evaluate(
                proposal.id,
                evaluation=ProposalEvaluation(verdict=EvaluationVerdict.IMPROVED),
            )
        assert (await service.get(proposal.id)).status is ProposalStatus.DRAFT
        assert await _events(uow_factory, EventType.IMPROVEMENT_PROPOSAL_EVALUATED) == []


class TestApprovalRequiresEvaluation:
    """🔴 未经评估的批准等于凭印象拍板。"""

    async def test_draft_cannot_be_approved(self, service: ProposalService) -> None:
        proposal = await _drafted(service)
        with pytest.raises(IllegalStateTransitionError, match="必须先经过评估"):
            await service.approve_for_manual_trial(proposal.id, approved_by="reviewer")

    async def test_pending_cannot_be_approved(self, service: ProposalService) -> None:
        proposal = await _drafted(service)
        async with service._uow_factory() as uow:
            await uow.proposals.save(
                proposal.bumped(status=ProposalStatus.PENDING_EVALUATION),
                expected_version=proposal.version,
            )
            await uow.commit()
        with pytest.raises(IllegalStateTransitionError):
            await service.approve_for_manual_trial(proposal.id, approved_by="reviewer")

    async def test_evaluated_can_be_approved(self, service: ProposalService) -> None:
        proposal = await _evaluated(service)
        transition = await service.approve_for_manual_trial(
            proposal.id, approved_by="reviewer", note="试验范围限于该情境"
        )
        assert transition.proposal.status is ProposalStatus.APPROVED_FOR_MANUAL_TRIAL

    async def test_approval_leaves_an_event(
        self, service: ProposalService, uow_factory: UnitOfWorkFactory
    ) -> None:
        """🔴 人类做出的那个批准决定本身必须留痕——否则整条链路白建。"""
        proposal = await _evaluated(service)
        transition = await service.approve_for_manual_trial(proposal.id, approved_by="审阅者甲")

        events = await _events(uow_factory, EventType.IMPROVEMENT_PROPOSAL_APPROVED)
        assert [event.id for event in events] == [transition.event.id]
        assert events[0].payload["proposal_id"] == str(proposal.id)
        assert events[0].actor_id == "审阅者甲"

    async def test_rejected_cannot_be_approved(self, service: ProposalService) -> None:
        proposal = await _evaluated(service)
        await service.reject(proposal.id, rejected_by="reviewer", reason="不值得做")
        with pytest.raises(IllegalStateTransitionError, match="终态"):
            await service.approve_for_manual_trial(proposal.id, approved_by="另一个审阅者")

    async def test_approving_twice_is_refused(self, service: ProposalService) -> None:
        proposal = await _evaluated(service)
        await service.approve_for_manual_trial(proposal.id, approved_by="甲")
        with pytest.raises(IllegalStateTransitionError):
            await service.approve_for_manual_trial(proposal.id, approved_by="乙")


class TestRejection:
    async def test_draft_cannot_be_rejected(self, service: ProposalService) -> None:
        """🔴 把未经评估的「不想做」记成「做不了」，会让后来者重复走一遍同样的路。"""
        proposal = await _drafted(service)
        with pytest.raises(IllegalStateTransitionError, match="必须先经过评估"):
            await service.reject(proposal.id, rejected_by="reviewer", reason="不感兴趣")

    async def test_evaluated_can_be_rejected(self, service: ProposalService) -> None:
        proposal = await _evaluated(service)
        transition = await service.reject(
            proposal.id, rejected_by="审阅者乙", reason="对照指标预计会退化"
        )
        assert transition.proposal.status is ProposalStatus.REJECTED

    async def test_rejection_reason_is_required(self, service: ProposalService) -> None:
        proposal = await _evaluated(service)
        with pytest.raises(ValueError, match="理由"):
            await service.reject(proposal.id, rejected_by="reviewer", reason="   ")

    async def test_rejection_leaves_an_event(
        self, service: ProposalService, uow_factory: UnitOfWorkFactory
    ) -> None:
        proposal = await _evaluated(service)
        await service.reject(proposal.id, rejected_by="审阅者乙", reason="对照指标预计会退化")

        payload = (await _events(uow_factory, EventType.IMPROVEMENT_PROPOSAL_REJECTED))[0].payload
        assert payload["reason"] == "对照指标预计会退化"
        assert payload["from_status"] == "evaluated"

    async def test_rejected_is_terminal(self, service: ProposalService) -> None:
        proposal = await _evaluated(service)
        await service.reject(proposal.id, rejected_by="reviewer", reason="不值得做")
        assert (await service.get(proposal.id)).is_terminal is True

    async def test_rejecting_twice_is_refused(self, service: ProposalService) -> None:
        proposal = await _evaluated(service)
        await service.reject(proposal.id, rejected_by="甲", reason="不值得做")
        with pytest.raises(IllegalStateTransitionError):
            await service.reject(proposal.id, rejected_by="乙", reason="还是不值得做")


class TestConcurrentTransitions:
    """🔴 乐观锁在这里不是性能优化，是正确性要求。"""

    async def test_a_stale_writer_cannot_overwrite(
        self, service: ProposalService, uow_factory: UnitOfWorkFactory
    ) -> None:
        """模拟"批准与驳回同时到达"里的**第二个写入者**。

        它在读到提案之后、写回之前，第一条流转已经提交了。
        没有乐观锁的话，它会安静地覆盖掉那个决定，
        结果是提案**同时**被批准和驳回，而事件流里两条理由都在。

        ⚠️ 这条用例是**顺序**执行的——它验证的是"带着过期版本号的写入
        会被拒"，而不是真的并发调度。后者依赖事件循环的调度时机，
        写成断言会变成一条时灵时不灵的测试。
        """
        proposal = await _evaluated(service)
        stale = await service.get(proposal.id)  # 记下当时的版本

        await service.approve_for_manual_trial(proposal.id, approved_by="甲")

        with pytest.raises(OptimisticLockError):
            async with uow_factory() as uow:
                await uow.proposals.save(
                    stale.bumped(status=ProposalStatus.REJECTED),
                    expected_version=stale.version,
                )

    async def test_the_lost_update_is_visible_in_the_event_log(
        self, service: ProposalService, uow_factory: UnitOfWorkFactory
    ) -> None:
        """🔴 落败的那个写入者**不留事件**。

        状态与事件同生共死：被乐观锁挡下的那次流转根本走不到写事件那一步。
        反过来说，事件流里出现的每一条流转记录，对应的状态变更都真的发生过。
        """
        proposal = await _evaluated(service)
        stale = await service.get(proposal.id)
        await service.approve_for_manual_trial(proposal.id, approved_by="甲")

        with pytest.raises(OptimisticLockError):
            async with uow_factory() as uow:
                await uow.proposals.save(
                    stale.bumped(status=ProposalStatus.REJECTED),
                    expected_version=stale.version,
                )

        assert await _events(uow_factory, EventType.IMPROVEMENT_PROPOSAL_REJECTED) == []
        assert (await service.get(proposal.id)).status is ProposalStatus.APPROVED_FOR_MANUAL_TRIAL


class TestNoPathToActive:
    """🔴 不变量 11：不是"没实现"，是类型里根本没有那个值。"""

    @pytest.mark.parametrize(
        "method", ["create", "get", "list_all", "evaluate", "approve_for_manual_trial", "reject"]
    )
    def test_the_service_exposes_no_promotion_method(
        self, service: ProposalService, method: str
    ) -> None:
        assert not hasattr(ProposalService, f"activate_{method}")
        assert not hasattr(ProposalService, f"apply_{method}")

    def test_no_public_method_mentions_activating(self, service: ProposalService) -> None:
        public = [name for name in dir(ProposalService) if not name.startswith("_")]
        forbidden = ("activate", "promote", "publish", "deploy", "apply")
        for name in public:
            assert not any(word in name for word in forbidden), name

    async def test_every_reachable_status_is_a_real_proposal_status(
        self, service: ProposalService
    ) -> None:
        proposal = await _evaluated(service)
        transition = await service.approve_for_manual_trial(proposal.id, approved_by="甲")
        assert transition.proposal.status in set(ProposalStatus)
        assert transition.proposal.can_become_active is False


class TestTransitionShape:
    async def test_transition_carries_the_proposal_and_the_event(
        self, service: ProposalService
    ) -> None:
        proposal = await _evaluated(service)
        transition = await service.approve_for_manual_trial(proposal.id, approved_by="甲")
        assert isinstance(transition, ProposalTransition)
        assert transition.proposal.id == proposal.id
        assert transition.event.event_type is EventType.IMPROVEMENT_PROPOSAL_APPROVED

    async def test_events_are_not_attached_to_a_round(self, service: ProposalService) -> None:
        """提案来自**跨回合的模式**，挂到某一个回合上会误导读者。"""
        proposal = await _drafted(service)
        transition = await service.evaluate(
            proposal.id, evaluation=_evaluation(EvaluationVerdict.IMPROVED)
        )
        assert transition.event.cognitive_round_id is None

    async def test_each_transition_leaves_exactly_one_event(
        self, service: ProposalService, uow_factory: UnitOfWorkFactory
    ) -> None:
        """状态变了却没留痕，正是这条链路最不能出的一类缺陷。"""
        proposal = await _evaluated(service)
        await service.approve_for_manual_trial(proposal.id, approved_by="甲")

        for event_type in (
            EventType.IMPROVEMENT_PROPOSAL_CREATED,
            EventType.IMPROVEMENT_PROPOSAL_EVALUATED,
            EventType.IMPROVEMENT_PROPOSAL_APPROVED,
        ):
            events = await _events(uow_factory, event_type)
            assert len(events) == 1, event_type
