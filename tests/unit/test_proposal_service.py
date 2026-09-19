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
from ai_psi.application.metrics_reader import RoundMetricsReader
from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.application.proposal_gate import (
    GateEvidence,
    GateVerdict,
    ProposalGate,
    _find,
)
from ai_psi.application.proposal_service import (
    ProposalEvaluation,
    ProposalService,
    ProposalTransition,
)
from ai_psi.application.round_service import CognitiveRoundService
from ai_psi.domain.enums import ErrorType, EvaluationVerdict, EventType, ProposalStatus
from ai_psi.domain.exceptions import (
    ConstitutionViolationError,
    IllegalStateTransitionError,
    InvalidRequestError,
    NotFoundError,
    OptimisticLockError,
)
from ai_psi.domain.improvement_proposals import ImprovementProposal
from ai_psi.infrastructure.in_memory.store import InMemoryStore
from ai_psi.infrastructure.in_memory.unit_of_work import make_in_memory_unit_of_work_factory
from ai_psi.learning.pattern_detector import ErrorPattern, PatternScan, SuppressedPattern
from ai_psi.learning.promotion_policy import PromotionTrigger
from ai_psi.providers.embeddings import LocalHashingEmbedding
from tests.helpers import RecordingTrigger, forged, seed_learning_evidence

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
        learning=LearningService(
            uow_factory,
            reader,
            gate,
            ProposalService(uow_factory),
            RoundMetricsReader(uow_factory),
        ),
        gate=gate,
        round_service=CognitiveRoundService(uow_factory),
        artifact_service=ArtifactService(uow_factory),
        feedback_service=FeedbackService(uow_factory, memory_service, RecordingTrigger()),
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
    """🔴 不变量 11：不是"没实现"，是**没有可到达的状态**。

    ⚠️ **阶段 6.5 §四.9 删掉了这里原有的两条名字匹配断言。**

    它们一条查方法名集合、一条查名字里有没有 `activate`/`promote`/
    `publish`/... 这些子串。实测：给 `ProposalService` 挂上两个
    **真的会生效**的方法 `go_live()` 与 `ship_it()`，两条断言照样全绿——
    因为 `"go_live"` 里没有 `live` 之外的任何一个词，而 `live` 那条
    当时也不在名单里。**名字扫描挡不住改名。**

    §四.10 要求改用**状态图、服务入口、仓储写入与数据库对抗**证明。
    本类覆盖前三条（第四条需要真实数据库，见
    ``tests/integration/test_proposal_status_constraints.py``）。

    四道防线各自独立，任何一道都不是"以防万一"：

    | 防线 | 挡的是什么 |
    |---|---|
    | 枚举白名单 | 往 `ProposalStatus` 里**加**一个表示"已生效"的值 |
    | 服务出口的行为断言 | 现有出口把提案推到一个不合法状态 |
    | 仓储的成员校验 | `model_construct` 绕开校验造出的裸字符串 |
    | PostgreSQL CHECK | 上面三道全部被绕过时，数据库仍然拒绝 |

    ⚠️ **不在这里断言"公开方法的集合"。** 一个新增的方法如果只是
    把提案推到**已经合法**的状态，那不是威胁；真正的威胁是推到
    一个意为"已生效"的状态，而那个状态**在枚举里不存在**。
    钉住方法名集合会把每次无害的重构都变成一次红灯，
    却对真正的绕过无能为力。
    """

    #: `ProposalStatus` 允许拥有的**全部**成员。
    _LEGAL_STATUSES = frozenset(
        {
            ProposalStatus.DRAFT,
            ProposalStatus.PENDING_EVALUATION,
            ProposalStatus.EVALUATED,
            ProposalStatus.REJECTED,
            ProposalStatus.APPROVED_FOR_MANUAL_TRIAL,
        }
    )

    def test_the_status_enum_is_exactly_what_we_agreed_on(self) -> None:
        """🔴 白名单：**任何新增都要在这里显式登记。**

        这是 §四.3 的直接落地。禁用词黑名单（`active`/`enabled`/`live`
        /`deployed`/`published`）挡不住 `OPERATIONAL = "operational"`
        这样的新词——而白名单挡住一切没登记过的东西。
        """
        assert set(ProposalStatus) == self._LEGAL_STATUSES

    def test_no_status_value_reads_like_a_promotion(self) -> None:
        """在白名单之上再补一道语义闸——新增的名字若像"已生效"会被拦下。

        ⚠️ 这一条**不是**主要防线（它自己就是一次名字扫描）。
        它存在的理由是：白名单只保证"这个值是我们讨论过的"，
        不保证"我们讨论的时候意识到它在说什么"。两道一起才有意义。
        """
        forbidden = ("active", "enabled", "live", "deployed", "published", "applied")
        for status in ProposalStatus:
            assert not any(word in status.value for word in forbidden), status

    async def test_every_exit_leaves_the_proposal_in_a_legal_state(self, rig: _Rig) -> None:
        """🔴 **行为断言，不是名字断言。**

        真实地调用三个出口，收集它们产出的状态，断言全部落在合法集合内。
        这证明的是"这套代码此刻能把提案带到哪里"——
        而名字扫描证明的只是"这些方法叫什么"。
        """
        observed: set[ProposalStatus] = set()

        drafted = await rig.draft(signature="出口一")
        observed.add(drafted.status)

        evaluated = await rig.draft(signature="出口二")
        observed.add(
            (
                await rig.service.evaluate(
                    evaluated.id, evaluation=_evaluation(EvaluationVerdict.IMPROVED)
                )
            ).proposal.status
        )

        approved = await rig.draft(signature="出口三")
        await rig.service.evaluate(approved.id, evaluation=_evaluation(EvaluationVerdict.IMPROVED))
        observed.add(
            (
                await rig.service.approve_for_manual_trial(approved.id, approved_by="甲")
            ).proposal.status
        )

        rejected = await rig.draft(signature="出口四")
        await rig.service.evaluate(rejected.id, evaluation=_evaluation(EvaluationVerdict.IMPROVED))
        observed.add(
            (
                await rig.service.reject(rejected.id, rejected_by="乙", reason="代价太大")
            ).proposal.status
        )

        assert observed <= self._LEGAL_STATUSES, observed
        assert observed == {
            ProposalStatus.DRAFT,
            ProposalStatus.EVALUATED,
            ProposalStatus.APPROVED_FOR_MANUAL_TRIAL,
            ProposalStatus.REJECTED,
        }

    async def test_the_terminal_status_is_the_manual_trial_one(self, rig: _Rig) -> None:
        """终点是"批准做人工试验"，不是"生效"——它必须**看得见**地叫这个名字。"""
        proposal = await _evaluated(rig)
        transition = await rig.service.approve_for_manual_trial(proposal.id, approved_by="甲")
        assert transition.proposal.status is ProposalStatus.APPROVED_FOR_MANUAL_TRIAL
        assert transition.proposal.is_terminal is True
        assert transition.proposal.can_become_active is False
        assert proposal.status is not ProposalStatus.APPROVED_FOR_MANUAL_TRIAL

    async def test_no_status_is_reachable_after_a_terminal_one(self, rig: _Rig) -> None:
        """🔴 状态图：终态是**吸收态**——从它出发没有边。

        上一条证明了"批准之后是 APPROVED_FOR_MANUAL_TRIAL"，
        这一条证明"到了那里就再也动不了"。少了它，
        一个"终态还能继续转"的状态图在外部看起来与前者一样。
        """
        approved = await _evaluated(rig)
        await rig.service.approve_for_manual_trial(approved.id, approved_by="甲")

        with pytest.raises(IllegalStateTransitionError):
            await rig.service.evaluate(
                approved.id, evaluation=_evaluation(EvaluationVerdict.IMPROVED)
            )
        with pytest.raises(IllegalStateTransitionError):
            await rig.service.reject(approved.id, rejected_by="乙", reason="改主意了")
        with pytest.raises(IllegalStateTransitionError):
            await rig.service.approve_for_manual_trial(approved.id, approved_by="丙")


class TestTheRepositoryRefusesForgedObjects:
    """🔴 §四.4：仓储**不得信任** ``model_construct`` 构造的对象。

    `model_construct` 会跳过**全部**校验，构造出一个把 ``status``
    写成裸字符串的提案。类型注解拦不住它，`model_validate` 也不会被调用——
    因此"类型里没有 ACTIVE"这句话在那个位置上不成立。

    这条防线必须在**仓储**：它是对象变成持久化数据的那一刻。
    """

    async def test_a_forged_status_cannot_be_persisted(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        bogus = forged(ImprovementProposal, **_forged_payload(status="active"))

        with pytest.raises(ConstitutionViolationError) as excinfo:
            async with uow_factory() as uow:
                await uow.proposals.add(bogus)
                await uow.commit()

        assert excinfo.value.invariant_id == "I11"
        async with uow_factory() as uow:
            assert await uow.proposals.get(bogus.id) is None

    @pytest.mark.parametrize("status", ["active", "ACTIVE", "enabled", "go_live", "deployed"])
    async def test_every_variant_of_a_live_status_is_refused(
        self, uow_factory: UnitOfWorkFactory, status: str
    ) -> None:
        """枚举值、大写、近义词——一视同仁。"""
        bogus = forged(ImprovementProposal, **_forged_payload(status=status))
        with pytest.raises(ConstitutionViolationError):
            async with uow_factory() as uow:
                await uow.proposals.add(bogus)

    async def test_a_forged_status_cannot_be_saved_over_an_existing_one(
        self, uow_factory: UnitOfWorkFactory
    ) -> None:
        """🔴 **更新路径也要挡。**

        只挡 `add` 的话，一条合法提案可以在 `save` 时被换成非法状态——
        而 `save` 是状态流转走的路径，正是最该被守的那一条。
        """
        bogus = forged(ImprovementProposal, **_forged_payload(status="active"))
        with pytest.raises(ConstitutionViolationError):
            async with uow_factory() as uow:
                await uow.proposals.save(bogus, expected_version=1)


def _forged_payload(**overrides: object) -> dict[str, object]:
    """一份**字段齐全**的提案负载，供 ``model_construct`` 使用。

    ⚠️ 必须齐全：`model_construct` 缺失字段时不会报错，
    而是留下一个属性不存在的对象——那会让异常变成 `AttributeError`，
    测试就会因为**错误的原因**通过。
    """
    payload: dict[str, object] = {
        "created_by": "test",
        "target_component": "prompt:logical_analyzer",
        "observed_problem": "同类推理错误反复出现",
        "error_class": ErrorType.REASONING_ERROR,
        "proposed_change": "检查该情境下的反例检查环节",
        "expected_benefit": "降低复发率",
    }
    payload.update(overrides)
    return payload


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


# ---------------------------------------------------------------------------
# §六 补齐：`_find` 的匹配谓词
# ---------------------------------------------------------------------------


class TestTheScanLookupPicksTheRightEntry:
    """🔴 变异测试发现：``proposal_gate._find`` 的两个匹配谓词上有
    **21 个变异体存活**——52 条里的四成。

    那个谓词是「这个键（错误类别 + 情境签名）在扫描结果里长什么样」，
    而它是 §二.13–15 那道「证据不许张冠李戴」防线的落点。

    既有用例全部只在**唯一一个键、且第一个就命中**的扫描上跑过，
    于是谓词的每一个组成部分都可以被改坏而无人察觉：

    * ``error_type`` 那一半换成 ``!=`` / ``<`` / ``>`` / ``is not``；
    * ``situation_signature`` 那一半换成 ``!=`` / ``<`` / ``>`` / ``is not``；
    * 把中间的 ``and`` 换成 ``or``（**任一维相同即命中**，
      于是"同一情境下的另一个错误"会顶替掉真正要找的那一条）；
    * 把遍历 ``suppressed`` 的那个循环清空。

    下面每个用例都构造**多个键**，并要求取回**指定的那一个**。
    """

    @staticmethod
    def _pattern(error_type: ErrorType, signature: str) -> ErrorPattern:
        return ErrorPattern(
            error_type=error_type,
            situation_signature=signature,
            occurrence_count=3,
            weighted_count=3,
            experience_count=3,
        )

    @staticmethod
    def _suppressed(error_type: ErrorType, signature: str, reason: str) -> SuppressedPattern:
        return SuppressedPattern(
            error_type=error_type,
            situation_signature=signature,
            occurrence_count=2,
            weighted_count=1,
            experience_count=2,
            reasons=(reason,),
        )

    @classmethod
    def _scan(cls) -> PatternScan:
        return PatternScan(
            patterns=(
                cls._pattern(ErrorType.SCOPE_ERROR, "sig-a"),
                cls._pattern(ErrorType.REASONING_ERROR, "sig-b"),
            ),
            suppressed=(
                cls._suppressed(ErrorType.FACTUAL_ERROR, "sig-c", "第一条理由"),
                cls._suppressed(ErrorType.MEMORY_ERROR, "sig-d", "第二条理由"),
            ),
        )

    def test_a_pattern_that_is_not_first_is_still_found(self) -> None:
        found, reasons = _find(
            self._scan(), error_type=ErrorType.REASONING_ERROR, signature="sig-b"
        )
        assert found is not None
        assert found.situation_signature == "sig-b"
        assert reasons == ()

    def test_a_suppressed_entry_that_is_not_first_is_still_found(self) -> None:
        found, reasons = _find(self._scan(), error_type=ErrorType.MEMORY_ERROR, signature="sig-d")
        assert found is None
        assert reasons == ("第二条理由",)

    def test_the_reasons_belong_to_the_matching_entry(self) -> None:
        """🔴 取回**另一条**分组的理由，正是"张冠李戴"最直接的形态。"""
        _, reasons = _find(self._scan(), error_type=ErrorType.FACTUAL_ERROR, signature="sig-c")
        assert reasons == ("第一条理由",)

    def test_the_right_error_type_with_the_wrong_signature_is_not_found(self) -> None:
        """🔴 ``and`` → ``or`` 在这一条上会翻车：签名撞上了别的键。"""
        assert _find(self._scan(), error_type=ErrorType.REASONING_ERROR, signature="sig-a") == (
            None,
            (),
        )

    def test_the_right_signature_with_the_wrong_error_type_is_not_found(self) -> None:
        assert _find(self._scan(), error_type=ErrorType.SCOPE_ERROR, signature="sig-b") == (
            None,
            (),
        )

    def test_a_key_that_is_nowhere_returns_nothing(self) -> None:
        assert _find(self._scan(), error_type=ErrorType.CALIBRATION_ERROR, signature="sig-z") == (
            None,
            (),
        )

    def test_an_empty_scan_returns_nothing(self) -> None:
        assert _find(PatternScan(), error_type=ErrorType.REASONING_ERROR, signature="sig") == (
            None,
            (),
        )

    def test_a_key_present_in_both_prefers_the_pattern(self) -> None:
        """合格的那一份优先——门槛已经过了，理由清单是次要信息。"""
        scan = PatternScan(
            patterns=(self._pattern(ErrorType.REASONING_ERROR, "sig"),),
            suppressed=(self._suppressed(ErrorType.REASONING_ERROR, "sig", "不该被取到"),),
        )
        found, reasons = _find(scan, error_type=ErrorType.REASONING_ERROR, signature="sig")
        assert found is not None
        assert reasons == ()


# ---------------------------------------------------------------------------
# §六 补齐：门禁结论本身的形状
# ---------------------------------------------------------------------------


class TestTheGateVerdictIsDerivedNotFilled:
    """🔴 变异测试发现：门禁结论上有 **12 个变异体存活**，全都指向同一件事——
    既有用例**只走过"恰好三条经验"这一种输入**。

    * ``authorised`` 里三个 ``and`` 被换成 ``or``；
    * ``weighted_count >= threshold`` 被换成 ``==`` / ``<=`` / ``is``；
    * ``recomputed_occurrences`` / ``recomputed_weighted_count`` 的
      兜底值从 ``0`` 变成 ``1`` / ``-1``；
    * ``GateEvidence`` 的默认值改成"已判定为系统性问题"。

    "恰好等于门槛"是**一个点**；上面这些改动在其余全部取值上都不同。
    """

    @staticmethod
    async def _seed(rig, *, rounds: int, signature: str) -> None:
        await seed_learning_evidence(
            round_service=rig.round_service,
            artifact_service=rig.artifact_service,
            feedback_service=rig.feedback_service,
            rounds=rounds,
            error_type=ErrorType.REASONING_ERROR,
            situation_signature=signature,
        )

    async def test_a_pattern_well_above_the_threshold_is_authorised(self, rig) -> None:
        """🔴 ``>=`` 的**上边界**。改成 ``==`` / ``<=`` / ``is`` 之后，
        证据**越充分**反而越不授权——而"恰好三条"那一组用例照样通过。"""
        signature = "reasoning|d2|well-above"
        await self._seed(rig, rounds=5, signature=signature)

        verdict = await rig.gate.review(
            error_type=ErrorType.REASONING_ERROR, situation_signature=signature
        )

        assert verdict.recomputed_weighted_count == 5
        assert verdict.authorised is True

    async def test_a_below_threshold_key_is_not_authorised(self, rig) -> None:
        """🔴 三个 ``and`` 换成 ``or`` 之后，这一条会**抛 AttributeError**。

        ``pattern`` 与 ``decision`` 是成对出现的（要么都有、要么都没有），
        于是 ``or`` 不再短路，下一项 ``self.decision.allowed`` 直接炸。
        结论应当是"不授权"，而不是一个异常。
        """
        signature = "reasoning|d2|below"
        await self._seed(rig, rounds=2, signature=signature)

        verdict = await rig.gate.review(
            error_type=ErrorType.REASONING_ERROR, situation_signature=signature
        )

        assert verdict.authorised is False
        assert verdict.pattern is None

    async def test_an_unobserved_key_says_so(self, rig) -> None:
        verdict = await rig.gate.review(
            error_type=ErrorType.SCOPE_ERROR, situation_signature="从来没有出现过"
        )
        assert verdict.authorised is False
        assert any("没有任何达到门槛的独立发生" in reason for reason in verdict.reasons)

    async def test_a_below_threshold_key_gets_its_own_reasons(self, rig) -> None:
        """🔴 ``reasons = list(suppressed) or [...]`` 里的那个 ``or``。

        改成 ``and`` 之后，**未达门槛**的分组会退回那句笼统的
        "没有任何达到门槛的独立发生"——而那正是这一节要消灭的
        "为什么没有"答不上来的情形。
        """
        signature = "reasoning|d2|has-reasons"
        await self._seed(rig, rounds=2, signature=signature)

        verdict = await rig.gate.review(
            error_type=ErrorType.REASONING_ERROR, situation_signature=signature
        )

        assert any("未达门槛" in reason for reason in verdict.reasons)
        assert not any("没有任何达到门槛的独立发生" in reason for reason in verdict.reasons)

    async def test_condition_two_can_still_fire_through_the_gate(self, rig) -> None:
        """🔴 ``error_type in SEVERE_ERROR_TYPES`` 被改成 ``not in`` 之后，
        严重错误的修复方向**传不进** PromotionEvidence，条件二永远不触发。

        这一条是那条通路唯一的观测点：既有用例从来没让门禁走到条件二。

        ⚠️ 必须**达到次数门槛**才能走到那里：没有模式时门禁在
        ``_find`` 那一关就返回了，``decision`` 是 ``None``——
        条件二根本没机会被评估。
        """
        signature = "calibration|d2|severe"
        await seed_learning_evidence(
            round_service=rig.round_service,
            artifact_service=rig.artifact_service,
            feedback_service=rig.feedback_service,
            rounds=3,
            error_type=ErrorType.CALIBRATION_ERROR,
            situation_signature=signature,
        )

        verdict = await rig.gate.review(
            error_type=ErrorType.CALIBRATION_ERROR,
            situation_signature=signature,
            evidence=GateEvidence(fix_direction="在标定阶段显式给出区间"),
        )

        assert verdict.decision is not None
        assert PromotionTrigger.SEVERE_ERROR_WITH_FIX in verdict.decision.triggers

    async def test_the_unavailable_counts_are_zero(self, rig) -> None:
        """没有模式时，两个"重新计算出来的计数"是 **0**。

        改成 1 或 -1 之后，一份**没有证据**的结论会声称发生过一次——
        而这两个数会随结论一起交出去。
        """
        verdict = await rig.gate.review(
            error_type=ErrorType.SCOPE_ERROR, situation_signature="从来没有出现过"
        )
        assert verdict.recomputed_occurrences == 0
        assert verdict.recomputed_weighted_count == 0

    async def test_the_key_is_a_value_not_a_method(self, rig) -> None:
        verdict = await rig.gate.review(
            error_type=ErrorType.SCOPE_ERROR, situation_signature="某个情境"
        )
        assert verdict.key == ("scope_error", "某个情境")
        assert verdict.authorised is False  # 属性而不是绑定方法

    async def test_the_token_stays_out_of_the_repr(self, rig) -> None:
        """🔴 ``_token`` 上的 ``repr=False`` 不是装饰。

        它是**授权凭据对象**：``repr`` 会进日志、进失败信息、进
        ``pytest`` 的断言输出。把它印出来等于把"门禁唯一的那点凭据"
        抄送到每一个看得见日志的地方。
        """
        verdict = await rig.gate.review(
            error_type=ErrorType.SCOPE_ERROR, situation_signature="某个情境"
        )
        assert "_token" not in repr(verdict)

    async def test_the_verdict_is_immutable(self, rig) -> None:
        import dataclasses

        verdict = await rig.gate.review(
            error_type=ErrorType.SCOPE_ERROR, situation_signature="某个情境"
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            verdict.threshold = 99


class TestTheGateEvidenceDefaults:
    def test_the_systemic_flag_defaults_to_false(self) -> None:
        """🔴 一条**触发条件**的默认值，足以让整个裁决翻面。"""
        evidence = GateEvidence()
        assert evidence.user_correction_shows_systemic_issue is False
        assert evidence.fix_direction is None
        assert evidence.offline_regression is None
        assert evidence.module_streak is None
        assert evidence.user_corrections is None

    def test_it_is_immutable(self) -> None:
        import dataclasses

        with pytest.raises(dataclasses.FrozenInstanceError):
            GateEvidence().fix_direction = "改这里"  # type: ignore[misc]


class TestTheGateThresholdBoundary:
    """🔴 变异测试发现：``ProposalGate.__init__`` 的 ``threshold < 2``
    有 **4 个变异体存活**——``< 1``、``< 3``、``<= 2``、``== 2``。

    既有用例只验过"1 会被拒"，而 2 是这道守卫声称的**最小合法值**。
    """

    def test_a_threshold_of_two_is_accepted(self, rig) -> None:
        gate = ProposalGate(rig.gate._reader, threshold=2)
        assert gate.threshold == 2

    def test_a_threshold_below_two_is_refused(self, rig) -> None:
        """🔴 **断言的是门禁自己那句话，不是"抛了 ValueError"。**

        变异测试发现：``< 2`` 改成 ``< 1`` 之后这条仍然通过——因为
        ``threshold=1`` 会落到下面 ``PatternDetector(threshold=1)``
        自己的守卫上，抛出一个同样含"不变量 10"的 ``ValueError``。

        也就是说这道守卫**在行为上是冗余的**，它唯一独有的东西是
        那句说得出"是哪一层拒绝了"的措辞。既然如此，
        要证明它还在，就得断言那句措辞。
        """
        for bad in (0, 1):
            with pytest.raises(ValueError, match="门禁门槛"):
                ProposalGate(rig.gate._reader, threshold=bad)


class TestTheScanLookupIgnoresHalfMatchesInSuppressed:
    """🔴 变异测试发现：``_find`` 的**第二个循环**（遍历 ``suppressed``）
    上的匹配谓词仍有 5 个变异体存活。

    上一个类里的用例全部用的是"两个维度都不撞"的反例，
    而这里的谓词是 ``error_type is X`` **且** ``signature == Y``：

    * 改成 ``or``：任一维相同就命中；
    * ``error_type`` 换成 ``>=`` / ``<=``：**StrEnum 的排序**会把
      ``scope_error`` 之类判成与 ``reasoning_error`` 相等或更大；
    * ``signature`` 换成 ``>=`` / ``<=``：同理。

    因此需要**分别只在错误类别上撞、只在签名上撞**的两条反例——
    它们各自能钉住谓词的一半。
    """

    @staticmethod
    def _suppressed(error_type: ErrorType, signature: str, reason: str) -> SuppressedPattern:
        return SuppressedPattern(
            error_type=error_type,
            situation_signature=signature,
            occurrence_count=2,
            weighted_count=1,
            experience_count=2,
            reasons=(reason,),
        )

    @classmethod
    def _scan(cls) -> PatternScan:
        """查询的键是 ``REASONING_ERROR / sig-target``，而这张表里
        没有任何一条**同时**在两个维度上相等。"""
        return PatternScan(
            suppressed=(
                # 只在错误类别上撞
                cls._suppressed(ErrorType.REASONING_ERROR, "sig-other", "错类别撞"),
                # 只在签名上撞
                cls._suppressed(ErrorType.SCOPE_ERROR, "sig-target", "错签名撞"),
                # 🔴 下面两条专治"把 `==` 换成 `>=` / `<=`"——
                #    它们各自**一个维度相等、另一个维度按字典序落在某侧**，
                #    因此顺序比较会命中，而相等比较不会。
                cls._suppressed(ErrorType.REASONING_ERROR, "sig-zzz", "签名更大，类别相等"),
                cls._suppressed(ErrorType.FACTUAL_ERROR, "sig-target", "类别更小，签名相等"),
            )
        )

    def test_nothing_matches(self) -> None:
        assert _find(
            self._scan(), error_type=ErrorType.REASONING_ERROR, signature="sig-target"
        ) == (None, ())

    def test_the_real_match_is_still_found(self) -> None:
        """正向对照：没有它，上面那条对"一律找不到"的实现也成立。"""
        scan = PatternScan(
            suppressed=(
                self._suppressed(ErrorType.SCOPE_ERROR, "sig-other", "别的"),
                self._suppressed(ErrorType.REASONING_ERROR, "sig-target", "就是它"),
            )
        )
        assert _find(scan, error_type=ErrorType.REASONING_ERROR, signature="sig-target") == (
            None,
            ("就是它",),
        )


class TestEveryVerdictKeepsThePatternAndTheDecisionTogether:
    """🔴 **这条用例守的不是行为，是 `authorised` 里一条等价登记的前提。**

    ``GateVerdict`` 的两条返回路径都是"要么 ``pattern`` 与 ``decision``
    都给、要么都不给"。``authorised`` 的第二个 ``and`` 换成 ``or``
    之所以在所有可达输入上等价，正是**因为**这条耦合成立：
    让两者分叉之后，那个 ``or`` 会放行一份**没有裁决**的结论。

    耦合目前是"两条 return 各写一遍"的结果，没有任何机制保证它。
    这条用例把它变成一条可执行的约定：哪天多出一条只给一半的路径，
    它会先红，而不是让等价表悄悄开始放行真变异。
    """

    async def test_a_qualifying_verdict_has_both(self, rig) -> None:
        verdict = await rig.verdict()
        assert verdict.pattern is not None
        assert verdict.decision is not None
        assert verdict.authorised is True

    async def test_a_non_qualifying_verdict_has_neither(self, rig) -> None:
        verdict = await rig.gate.review(
            error_type=ErrorType.SCOPE_ERROR, situation_signature="从来没有出现过"
        )
        assert verdict.pattern is None
        assert verdict.decision is None
        assert verdict.authorised is False

    async def test_the_two_fields_never_disagree(self, rig) -> None:
        for signature in ("sig-甲", "sig-乙"):
            verdict = await rig.gate.review(
                error_type=ErrorType.SCOPE_ERROR, situation_signature=signature
            )
            assert (verdict.pattern is None) == (verdict.decision is None)
