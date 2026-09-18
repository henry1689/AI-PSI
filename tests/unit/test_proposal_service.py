"""改进提案的生命周期（任务书 §12.4、ADR-0005）。

🔴 本文件有三条主线：

* **状态机不被绕过**——未经评估不能批准，终态不会再次流动；
* **每一次流转都留痕**——状态变了而事件没写，是这条链路最不能出的缺陷；
* **没有通往"生效"的路**——不是没实现，是类型里根本没有那个值。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest

from ai_psi.application.artifact_service import ArtifactService
from ai_psi.application.experience_reader import ExperienceReader
from ai_psi.application.feedback_service import FeedbackService
from ai_psi.application.learning_service import LearningService
from ai_psi.application.memory_service import MemoryService
from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.application.proposal_gate import GateVerdict, ProposalGate
from ai_psi.application.proposal_service import (
    ProposalEvaluation,
    ProposalService,
    ProposalTransition,
)
from ai_psi.application.round_service import CognitiveRoundService
from ai_psi.domain.enums import ErrorType, EvaluationVerdict, EventType, ProposalStatus
from ai_psi.domain.exceptions import (
    IllegalStateTransitionError,
    InvalidRequestError,
    NotFoundError,
    OptimisticLockError,
)
from ai_psi.domain.improvement_proposals import ImprovementProposal
from ai_psi.infrastructure.in_memory.store import InMemoryStore
from ai_psi.infrastructure.in_memory.unit_of_work import make_in_memory_unit_of_work_factory
from ai_psi.providers.embeddings import LocalHashingEmbedding
from tests.helpers import seed_learning_evidence

pytestmark = pytest.mark.unit


@pytest.fixture
def uow_factory() -> UnitOfWorkFactory:
    return make_in_memory_unit_of_work_factory(InMemoryStore(), LocalHashingEmbedding())


@dataclass
class _Rig:
    """本文件需要的最小装配。

    🔴 **为什么不直接构造一条 ``ImprovementProposal`` 塞进服务。**

    阶段 6.5 §二.15 起，落库必须携带**门禁结论**，而门禁结论只能由
    ``ProposalGate`` 在从存储重查经验、重算门槛之后产出
    （构造凭据是模块私有的）。这是有意的：它让"先造一条提案再测
    它的生命周期"这条路**在类型上就走不通**。

    因此本夹具走的是真实链路——三个回合、三条经验、三条纠正、
    一次学习运行——只是回合是由辅助函数合成出来的，不是跑完
    认知流水线的。这条边界写在
    :func:`tests.helpers.seed_learning_evidence` 的文档里。
    """

    service: ProposalService
    learning: LearningService
    gate: ProposalGate
    round_service: CognitiveRoundService
    artifact_service: ArtifactService
    feedback_service: FeedbackService
    drafts: int = 0
    """已经造过几条提案——用于给每次调用一个**不同的**情境签名。"""

    async def verdict(
        self,
        *,
        error_type: ErrorType = ErrorType.REASONING_ERROR,
        signature: str = "reasoning|d2|multi_source",
    ) -> GateVerdict:
        """备好证据，然后让门禁复核出一个结论。

        用于测试**落库关**本身（授权与提案是否对得上），
        不经过学习链路——那里有它自己的端到端测试。
        """
        await seed_learning_evidence(
            round_service=self.round_service,
            artifact_service=self.artifact_service,
            feedback_service=self.feedback_service,
            error_type=error_type,
            situation_signature=signature,
        )
        return await self.gate.review(error_type=error_type, situation_signature=signature)

    async def draft(
        self,
        *,
        error_type: ErrorType | None = None,
        signature: str | None = None,
    ) -> ImprovementProposal:
        """经真实学习链路造一条 DRAFT 提案。

        ⚠️ **同一个 ``(错误类别, 情境签名)`` 只会有一条未终结的提案**——
        这是链路的有意行为（重复运行不该造出重复提案）。
        因此不指定签名时，每次调用自动换一个：需要两条提案的测试
        不必自己去想名字，也不会因为签名撞上而悄悄只造出一条。
        """
        self.drafts += 1
        resolved_signature = signature or f"auto-{self.drafts}|d2|multi_source"
        await seed_learning_evidence(
            round_service=self.round_service,
            artifact_service=self.artifact_service,
            feedback_service=self.feedback_service,
            error_type=error_type,
            situation_signature=resolved_signature,
        )
        run = await self.learning.review()
        assert len(run.created) == 1, run.summary()
        return run.created[0].proposal


@pytest.fixture
def rig(uow_factory: UnitOfWorkFactory) -> _Rig:
    memory_service = MemoryService(uow_factory, LocalHashingEmbedding())
    reader = ExperienceReader(uow_factory)
    gate = ProposalGate(reader)
    return _Rig(
        service=ProposalService(uow_factory),
        learning=LearningService(uow_factory, reader, gate, ProposalService(uow_factory)),
        gate=gate,
        round_service=CognitiveRoundService(uow_factory),
        artifact_service=ArtifactService(uow_factory),
        feedback_service=FeedbackService(uow_factory, memory_service),
    )


@pytest.fixture
def service(rig: _Rig) -> ProposalService:
    return rig.service


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


async def _drafted(rig: _Rig) -> ImprovementProposal:
    """一条真实生成的 DRAFT 提案。"""
    return await rig.draft()


async def _evaluated(rig: _Rig) -> ImprovementProposal:
    proposal = await _drafted(rig)
    return (
        await rig.service.evaluate(
            proposal.id,
            evaluation=_evaluation(EvaluationVerdict.IMPROVED),
        )
    ).proposal


class TestCreate:
    async def test_created_as_draft(self, rig: _Rig) -> None:
        proposal = await _drafted(rig)
        assert proposal.status is ProposalStatus.DRAFT
        assert (await rig.service.get(proposal.id)).status is ProposalStatus.DRAFT

    async def test_creation_leaves_an_event(
        self, rig: _Rig, uow_factory: UnitOfWorkFactory
    ) -> None:
        proposal = await _drafted(rig)
        events = await _events(uow_factory, EventType.IMPROVEMENT_PROPOSAL_CREATED)
        assert [event.payload["proposal_id"] for event in events] == [str(proposal.id)]

    async def test_creation_event_records_the_evidence_count(
        self, rig: _Rig, uow_factory: UnitOfWorkFactory
    ) -> None:
        """条数对应不变量 10 的门槛——"它凭什么成为提案"要能只查事件就答出来。"""
        await rig.draft()
        payload = (await _events(uow_factory, EventType.IMPROVEMENT_PROPOSAL_CREATED))[0].payload
        assert payload["supporting_experience_count"] == 3

    async def test_creation_event_records_what_the_gate_saw(
        self, rig: _Rig, uow_factory: UnitOfWorkFactory
    ) -> None:
        """🔴 门禁**当时看到的**必须随事件落库（阶段 6.5 §二.13）。

        事后重跑门禁未必得到同样结果（经验会继续累积），
        因此"这条提案凭什么被放行"只能靠事件里那几个数字回答。
        """
        await rig.draft()
        payload = (await _events(uow_factory, EventType.IMPROVEMENT_PROPOSAL_CREATED))[0].payload
        assert payload["gate_threshold"] == 3
        assert payload["gate_weighted_count"] == 3
        assert payload["gate_occurrence_count"] == 3
        assert "经验 3 条" in str(payload["gate_data_quality"])

    @pytest.mark.parametrize("status", [ProposalStatus.EVALUATED, ProposalStatus.REJECTED])
    async def test_non_draft_proposals_are_rejected(
        self, rig: _Rig, status: ProposalStatus
    ) -> None:
        """🔴 一条"生成出来就已评估"的提案，会让"谁评估的、依据是什么"无从回答。"""
        with pytest.raises(IllegalStateTransitionError):
            await rig.service.create(
                _proposal(status=status),
                verdict=await rig.verdict(),
            )


class TestReads:
    async def test_unknown_proposal_is_not_found(self, rig: _Rig) -> None:
        with pytest.raises(NotFoundError, match="改进提案不存在"):
            await rig.service.get(uuid4())

    async def test_list_starts_empty(self, rig: _Rig) -> None:
        assert await rig.service.list_all() == []

    async def test_list_returns_created_proposals(self, rig: _Rig) -> None:
        first = await _drafted(rig)
        second = await _drafted(rig)
        assert {item.id for item in await rig.service.list_all()} == {first.id, second.id}

    async def test_list_filters_by_status(self, rig: _Rig) -> None:
        draft = await _drafted(rig)
        evaluated = await _evaluated(rig)

        drafts = await rig.service.list_all(status=ProposalStatus.DRAFT)
        assert [item.id for item in drafts] == [draft.id]
        assert [
            item.id for item in await rig.service.list_all(status=ProposalStatus.EVALUATED)
        ] == [evaluated.id]

    async def test_list_filters_by_error_class(self, rig: _Rig) -> None:
        reasoning = await _drafted(rig)
        await rig.draft(error_type=ErrorType.SCOPE_ERROR)
        found = await rig.service.list_all(error_class=ErrorType.SCOPE_ERROR)
        assert reasoning.id not in {item.id for item in found}
        assert len(found) == 1

    async def test_list_respects_limit(self, rig: _Rig) -> None:
        for _ in range(3):
            await _drafted(rig)
        assert len(await rig.service.list_all(limit=2)) == 2


class TestEvaluation:
    async def test_draft_becomes_evaluated(self, rig: _Rig) -> None:
        proposal = await _drafted(rig)
        transition = await rig.service.evaluate(
            proposal.id,
            evaluation=_evaluation(EvaluationVerdict.NO_CHANGE),
        )
        assert transition.proposal.status is ProposalStatus.EVALUATED
        assert transition.proposal.version == proposal.version + 1

    async def test_pending_evaluation_can_be_evaluated(self, rig: _Rig) -> None:
        """``PENDING_EVALUATION`` 是待评估状态；它能进入 ``EVALUATED``。"""
        proposal = await _drafted(rig)
        async with rig.service._uow_factory() as uow:
            await uow.proposals.save(
                proposal.bumped(status=ProposalStatus.PENDING_EVALUATION),
                expected_version=proposal.version,
            )
            await uow.commit()

        transition = await rig.service.evaluate(
            proposal.id, evaluation=_evaluation(EvaluationVerdict.IMPROVED)
        )
        assert transition.proposal.status is ProposalStatus.EVALUATED

    async def test_verdict_and_evidence_are_recorded(
        self, rig: _Rig, uow_factory: UnitOfWorkFactory
    ) -> None:
        """🔴 口径必须落库：没有口径的结论无法被复核，也因此无法被推翻。"""
        proposal = await _drafted(rig)
        await rig.service.evaluate(
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

    async def test_inconclusive_is_a_real_verdict(self, rig: _Rig) -> None:
        """🔴 「看不出」与「没差」是两回事，不能被合并成一个值。"""
        proposal = await _drafted(rig)
        transition = await rig.service.evaluate(
            proposal.id,
            evaluation=_evaluation(EvaluationVerdict.INCONCLUSIVE),
        )
        assert transition.proposal.status is ProposalStatus.EVALUATED

    async def test_terminal_proposal_cannot_be_evaluated(self, rig: _Rig) -> None:
        proposal = await _evaluated(rig)
        await rig.service.reject(proposal.id, rejected_by="reviewer", reason="不值得做")

        with pytest.raises(IllegalStateTransitionError, match="终态"):
            await rig.service.evaluate(
                proposal.id, evaluation=_evaluation(EvaluationVerdict.IMPROVED)
            )

    async def test_unknown_proposal_is_not_found(self, rig: _Rig) -> None:
        with pytest.raises(NotFoundError):
            await rig.service.evaluate(
                uuid4(),
                evaluation=ProposalEvaluation(
                    verdict=EvaluationVerdict.IMPROVED, evidence=("历史回放 200 回合",)
                ),
            )

    async def test_evidence_is_required(self, rig: _Rig) -> None:
        """🔴 「结论：改善」而没说跟什么比、比了多少样本，不是一条可复核的记录。

        没有口径的结论无法被复核，因此也无法在日后被推翻——
        而不可推翻的结论会永久影响策略。
        """
        proposal = await _drafted(rig)
        with pytest.raises(InvalidRequestError, match="对照口径"):
            await rig.service.evaluate(
                proposal.id,
                evaluation=ProposalEvaluation(verdict=EvaluationVerdict.IMPROVED),
            )

    async def test_a_refused_evaluation_leaves_nothing_behind(
        self, rig: _Rig, uow_factory: UnitOfWorkFactory
    ) -> None:
        proposal = await _drafted(rig)
        with pytest.raises(InvalidRequestError):
            await rig.service.evaluate(
                proposal.id,
                evaluation=ProposalEvaluation(verdict=EvaluationVerdict.IMPROVED),
            )
        assert (await rig.service.get(proposal.id)).status is ProposalStatus.DRAFT
        assert await _events(uow_factory, EventType.IMPROVEMENT_PROPOSAL_EVALUATED) == []


class TestApprovalRequiresEvaluation:
    """🔴 未经评估的批准等于凭印象拍板。"""

    async def test_draft_cannot_be_approved(self, rig: _Rig) -> None:
        proposal = await _drafted(rig)
        with pytest.raises(IllegalStateTransitionError, match="必须先经过评估"):
            await rig.service.approve_for_manual_trial(proposal.id, approved_by="reviewer")

    async def test_pending_cannot_be_approved(self, rig: _Rig) -> None:
        proposal = await _drafted(rig)
        async with rig.service._uow_factory() as uow:
            await uow.proposals.save(
                proposal.bumped(status=ProposalStatus.PENDING_EVALUATION),
                expected_version=proposal.version,
            )
            await uow.commit()
        with pytest.raises(IllegalStateTransitionError):
            await rig.service.approve_for_manual_trial(proposal.id, approved_by="reviewer")

    async def test_evaluated_can_be_approved(self, rig: _Rig) -> None:
        proposal = await _evaluated(rig)
        transition = await rig.service.approve_for_manual_trial(
            proposal.id, approved_by="reviewer", note="试验范围限于该情境"
        )
        assert transition.proposal.status is ProposalStatus.APPROVED_FOR_MANUAL_TRIAL

    async def test_approval_leaves_an_event(
        self, rig: _Rig, uow_factory: UnitOfWorkFactory
    ) -> None:
        """🔴 人类做出的那个批准决定本身必须留痕——否则整条链路白建。"""
        proposal = await _evaluated(rig)
        transition = await rig.service.approve_for_manual_trial(proposal.id, approved_by="审阅者甲")

        events = await _events(uow_factory, EventType.IMPROVEMENT_PROPOSAL_APPROVED)
        assert [event.id for event in events] == [transition.event.id]
        assert events[0].payload["proposal_id"] == str(proposal.id)
        assert events[0].actor_id == "审阅者甲"

    async def test_rejected_cannot_be_approved(self, rig: _Rig) -> None:
        proposal = await _evaluated(rig)
        await rig.service.reject(proposal.id, rejected_by="reviewer", reason="不值得做")
        with pytest.raises(IllegalStateTransitionError, match="终态"):
            await rig.service.approve_for_manual_trial(proposal.id, approved_by="另一个审阅者")

    async def test_approving_twice_is_refused(self, rig: _Rig) -> None:
        proposal = await _evaluated(rig)
        await rig.service.approve_for_manual_trial(proposal.id, approved_by="甲")
        with pytest.raises(IllegalStateTransitionError):
            await rig.service.approve_for_manual_trial(proposal.id, approved_by="乙")


class TestRejection:
    async def test_draft_cannot_be_rejected(self, rig: _Rig) -> None:
        """🔴 把未经评估的「不想做」记成「做不了」，会让后来者重复走一遍同样的路。"""
        proposal = await _drafted(rig)
        with pytest.raises(IllegalStateTransitionError, match="必须先经过评估"):
            await rig.service.reject(proposal.id, rejected_by="reviewer", reason="不感兴趣")

    async def test_evaluated_can_be_rejected(self, rig: _Rig) -> None:
        proposal = await _evaluated(rig)
        transition = await rig.service.reject(
            proposal.id, rejected_by="审阅者乙", reason="对照指标预计会退化"
        )
        assert transition.proposal.status is ProposalStatus.REJECTED

    async def test_rejection_reason_is_required(self, rig: _Rig) -> None:
        proposal = await _evaluated(rig)
        with pytest.raises(InvalidRequestError, match="理由"):
            await rig.service.reject(proposal.id, rejected_by="reviewer", reason="   ")

    async def test_rejection_leaves_an_event(
        self, rig: _Rig, uow_factory: UnitOfWorkFactory
    ) -> None:
        proposal = await _evaluated(rig)
        await rig.service.reject(proposal.id, rejected_by="审阅者乙", reason="对照指标预计会退化")

        payload = (await _events(uow_factory, EventType.IMPROVEMENT_PROPOSAL_REJECTED))[0].payload
        assert payload["reason"] == "对照指标预计会退化"
        assert payload["from_status"] == "evaluated"

    async def test_rejected_is_terminal(self, rig: _Rig) -> None:
        proposal = await _evaluated(rig)
        await rig.service.reject(proposal.id, rejected_by="reviewer", reason="不值得做")
        assert (await rig.service.get(proposal.id)).is_terminal is True

    async def test_rejecting_twice_is_refused(self, rig: _Rig) -> None:
        proposal = await _evaluated(rig)
        await rig.service.reject(proposal.id, rejected_by="甲", reason="不值得做")
        with pytest.raises(IllegalStateTransitionError):
            await rig.service.reject(proposal.id, rejected_by="乙", reason="还是不值得做")


class TestConcurrentTransitions:
    """🔴 乐观锁在这里不是性能优化，是正确性要求。"""

    async def test_a_stale_writer_cannot_overwrite(
        self, rig: _Rig, uow_factory: UnitOfWorkFactory
    ) -> None:
        """模拟"批准与驳回同时到达"里的**第二个写入者**。

        它在读到提案之后、写回之前，第一条流转已经提交了。
        没有乐观锁的话，它会安静地覆盖掉那个决定，
        结果是提案**同时**被批准和驳回，而事件流里两条理由都在。

        ⚠️ 这条用例是**顺序**执行的——它验证的是"带着过期版本号的写入
        会被拒"，而不是真的并发调度。后者依赖事件循环的调度时机，
        写成断言会变成一条时灵时不灵的测试。
        """
        proposal = await _evaluated(rig)
        stale = await rig.service.get(proposal.id)  # 记下当时的版本

        await rig.service.approve_for_manual_trial(proposal.id, approved_by="甲")

        with pytest.raises(OptimisticLockError):
            async with uow_factory() as uow:
                await uow.proposals.save(
                    stale.bumped(status=ProposalStatus.REJECTED),
                    expected_version=stale.version,
                )

    async def test_the_lost_update_is_visible_in_the_event_log(
        self, rig: _Rig, uow_factory: UnitOfWorkFactory
    ) -> None:
        """🔴 落败的那个写入者**不留事件**。

        状态与事件同生共死：被乐观锁挡下的那次流转根本走不到写事件那一步。
        反过来说，事件流里出现的每一条流转记录，对应的状态变更都真的发生过。
        """
        proposal = await _evaluated(rig)
        stale = await rig.service.get(proposal.id)
        await rig.service.approve_for_manual_trial(proposal.id, approved_by="甲")

        with pytest.raises(OptimisticLockError):
            async with uow_factory() as uow:
                await uow.proposals.save(
                    stale.bumped(status=ProposalStatus.REJECTED),
                    expected_version=stale.version,
                )

        assert await _events(uow_factory, EventType.IMPROVEMENT_PROPOSAL_REJECTED) == []
        assert (
            await rig.service.get(proposal.id)
        ).status is ProposalStatus.APPROVED_FOR_MANUAL_TRIAL


class TestNoPathToActive:
    """🔴 不变量 11：不是"没实现"，是类型里根本没有那个值。

    ⚠️ **这里必须是白名单，不能是"名字里没有 activate"。**

    初版的两条断言全是名字匹配：一条查 `activate_create`/`apply_create`
    这类**根本不可能存在**的名字（恒真），另一条查名字里有没有
    `activate`/`promote`/`publish`/`deploy`/`apply` 五个子串。
    实测：给 `ProposalService` 挂上两个**真的会生效**的方法
    `go_live()` 与 `ship_it()`，两条断言照样全绿。

    现在钉的是**公开方法的集合**：任何新增公开方法都要在这里登记，
    而"悄悄加一条通往生效的路"再也做不到。
    """

    #: `ProposalService` 允许拥有的全部公开成员。
    #:
    #: 新增任何一项都必须是有意的决定，并在这里同步。
    _PUBLIC_SURFACE = frozenset(
        {
            "create",
            "get",
            "list_all",
            "evaluate",
            "approve_for_manual_trial",
            "reject",
        }
    )

    def test_the_public_surface_is_exactly_what_we_agreed_on(self) -> None:
        public = {name for name in dir(ProposalService) if not name.startswith("_")}
        assert public == self._PUBLIC_SURFACE

    def test_no_public_method_reads_like_a_promotion(self) -> None:
        """白名单之外再补一道语义闸——新增的名字若像"上线"会被拦下。"""
        forbidden = (
            "activate",
            "promote",
            "publish",
            "deploy",
            "apply",
            "live",
            "ship",
            "enable",
            "effective",
        )
        for name in self._PUBLIC_SURFACE:
            assert not any(word in name for word in forbidden), name

    async def test_the_terminal_status_is_the_manual_trial_one(self, rig: _Rig) -> None:
        """终点是"批准做人工试验"，不是"生效"——它必须**看得见**地叫这个名字。"""
        proposal = await _evaluated(rig)
        transition = await rig.service.approve_for_manual_trial(proposal.id, approved_by="甲")
        assert transition.proposal.status is ProposalStatus.APPROVED_FOR_MANUAL_TRIAL
        assert transition.proposal.is_terminal is True
        assert transition.proposal.can_become_active is False
        assert proposal.status is not ProposalStatus.APPROVED_FOR_MANUAL_TRIAL


class TestTransitionShape:
    async def test_transition_carries_the_proposal_and_the_event(self, rig: _Rig) -> None:
        proposal = await _evaluated(rig)
        transition = await rig.service.approve_for_manual_trial(proposal.id, approved_by="甲")
        assert isinstance(transition, ProposalTransition)
        assert transition.proposal.id == proposal.id
        assert transition.event.event_type is EventType.IMPROVEMENT_PROPOSAL_APPROVED

    async def test_events_are_not_attached_to_a_round(self, rig: _Rig) -> None:
        """提案来自**跨回合的模式**，挂到某一个回合上会误导读者。"""
        proposal = await _drafted(rig)
        transition = await rig.service.evaluate(
            proposal.id, evaluation=_evaluation(EvaluationVerdict.IMPROVED)
        )
        assert transition.event.cognitive_round_id is None

    async def test_each_transition_leaves_exactly_one_event(
        self, rig: _Rig, uow_factory: UnitOfWorkFactory
    ) -> None:
        """状态变了却没留痕，正是这条链路最不能出的一类缺陷。"""
        proposal = await _evaluated(rig)
        await rig.service.approve_for_manual_trial(proposal.id, approved_by="甲")

        for event_type in (
            EventType.IMPROVEMENT_PROPOSAL_CREATED,
            EventType.IMPROVEMENT_PROPOSAL_EVALUATED,
            EventType.IMPROVEMENT_PROPOSAL_APPROVED,
        ):
            events = await _events(uow_factory, event_type)
            assert len(events) == 1, event_type
