"""假设生成与评估（任务书 §9.6）。

生成器调用模型；**评估器是确定性代码**（偏差登记：ADR-0015）。

为什么评估不交给模型：让同一个模型既提出假设、又给假设打分，
是确认偏差的标准配方。评估要做的事情其实很机械——
"这条假设有几条证据支持、几条反对"——完全可以从证据关系算出来，
而且算出来的结果**可复现、可审计**。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from ai_psi.cognition.base import ModuleOutcome, invocation_context
from ai_psi.domain.enums import HypothesisCategory, HypothesisStatus
from ai_psi.domain.evidence import Evidence
from ai_psi.domain.hypotheses import Hypothesis
from ai_psi.domain.inquiries import Inquiry
from ai_psi.prompts.schemas import HypothesisGeneratorInput, HypothesisGeneratorOutput
from ai_psi.providers.gateway import ModelGateway

__all__ = [
    "HypothesisEvaluation",
    "HypothesisEvaluator",
    "HypothesisGenerator",
]

#: 当模型没有提供任何非人格化解释时补入的兜底候选。
#:
#: 它**不是编造**——"材料不足以支持任何关于意图的判断"永远是一个
#: 真实的认知可能性，而且在证据不足的场景下几乎总是正确的那个。
_FALLBACK_NON_AGENTIC = "当前可获得的材料不足以支持任何关于意图的判断"
_FALLBACK_FALSIFICATION = "出现可核验的一手材料并指向某个具体意图"


class HypothesisGenerator:
    """生成候选假设。"""

    def __init__(self, gateway: ModelGateway) -> None:
        """初始化。

        Args:
            gateway: 模型调用网关。
        """
        self._gateway = gateway

    async def generate(
        self,
        *,
        inquiry: Inquiry,
        evidence_summaries: Sequence[str] = (),
        max_hypotheses: int,
        requires_non_agentic: bool = True,
        user_id: UUID | None = None,
        conversation_id: UUID | None = None,
        cognitive_round_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> ModuleOutcome[list[Hypothesis]]:
        """生成假设。

        Args:
            inquiry: 所属认知问题。
            evidence_summaries: 可用证据摘要。
            max_hypotheses: 数量上限（来自预算）。
            requires_non_agentic: 是否要求至少一个非人格化、非心理化解释。
            user_id: 归属用户。
            conversation_id: 所属会话。
            cognitive_round_id: 当前回合 id。
            correlation_id: 关联链标识。

        Returns:
            假设列表与调用记录，以及必要时补入兜底候选的说明。
        """
        payload = HypothesisGeneratorInput(
            question=inquiry.question,
            scope=list(inquiry.scope),
            key_unknowns=list(inquiry.key_unknowns),
            evidence_summaries=list(evidence_summaries),
            max_hypotheses=max_hypotheses,
            requires_non_agentic=requires_non_agentic,
        )
        call = await self._gateway.structured(
            task_name="hypothesis_generator",
            payload=payload,
            response_model=HypothesisGeneratorOutput,
            context=invocation_context(
                cognitive_round_id=cognitive_round_id,
                conversation_id=conversation_id,
                user_id=user_id,
                correlation_id=correlation_id,
            ),
        )

        hypotheses = [
            Hypothesis(
                created_by="hypothesis_generator",
                inquiry_id=inquiry.id,
                statement=draft.statement,
                category=draft.category,
                predicted_observations=draft.predicted_observations,
                falsification_conditions=draft.falsification_conditions,
                applicability=draft.applicability,
                uncertainty_type=draft.uncertainty_type,
            )
            for draft in call.value.hypotheses
        ][:max_hypotheses]

        notes: list[str] = []
        # 🔴 §9.6：高风险或高深度问题必须保留至少一个非人格化解释。
        # 违反时**补入**而不是失败：结构缺陷是可修复的，
        # 让用户因为模型漏了一个字段而拿不到回答，代价不成比例。
        # 补入的事实会写进事件负载，可审计。
        if requires_non_agentic and not Hypothesis.has_non_agentic_explanation(hypotheses):
            hypotheses.append(_fallback_non_agentic(inquiry))
            notes.append("模型未提供非人格化解释，已补入材料不足类候选（§9.6）")

        return ModuleOutcome(
            value=hypotheses,
            invocations=(call.invocation,),
            notes=tuple(notes),
        )


def _fallback_non_agentic(inquiry: Inquiry) -> Hypothesis:
    """构造兜底的非人格化候选。"""
    return Hypothesis(
        created_by="hypothesis_generator",
        inquiry_id=inquiry.id,
        statement=_FALLBACK_NON_AGENTIC,
        category=HypothesisCategory.NON_AGENTIC,
        falsification_conditions=[_FALLBACK_FALSIFICATION],
        applicability=["证据不足的场景"],
    )


@dataclass(frozen=True, slots=True)
class HypothesisEvaluation:
    """一组假设的评估结果。"""

    hypotheses: tuple[Hypothesis, ...]
    supported_count: int
    rejected_count: int
    unresolved_count: int

    @property
    def has_supported(self) -> bool:
        """是否存在被支持（而非被确认为事实）的假设。"""
        return self.supported_count > 0


class HypothesisEvaluator:
    """依据证据关系评估假设状态。

    🔴 **不变量 1**：评估的结果里**没有"已确认事实"这一档**。
    ``SUPPORTED`` 的语义是"当前证据支持"，不是"事实成立"——
    :class:`~ai_psi.domain.enums.HypothesisStatus` 里根本没有通往事实的状态。

    证据通过 ``supports_claim_ids`` / ``opposes_claim_ids`` 引用假设 id。
    回合内的"论断"就是本回合提出的假设——这个约定写在
    :mod:`ai_psi.domain.evidence` 的字段说明里。
    """

    def evaluate(
        self,
        *,
        hypotheses: Sequence[Hypothesis],
        evidence: Sequence[Evidence],
    ) -> HypothesisEvaluation:
        """评估每条假设的状态。

        Args:
            hypotheses: 待评估的假设。
            evidence: 相关证据。

        Returns:
            评估结果（假设对象已带上新状态）。
        """
        evaluated: list[Hypothesis] = []
        for hypothesis in hypotheses:
            supporting = [item.id for item in evidence if hypothesis.id in item.supports_claim_ids]
            opposing = [item.id for item in evidence if hypothesis.id in item.opposes_claim_ids]
            status = _status_for(supporting=supporting, opposing=opposing)
            evaluated.append(
                hypothesis.bumped(
                    status=status,
                    supporting_evidence_ids=supporting,
                    opposing_evidence_ids=opposing,
                )
            )

        return HypothesisEvaluation(
            hypotheses=tuple(evaluated),
            supported_count=sum(1 for h in evaluated if h.status is HypothesisStatus.SUPPORTED),
            rejected_count=sum(1 for h in evaluated if h.status is HypothesisStatus.REJECTED),
            unresolved_count=sum(1 for h in evaluated if h.status is HypothesisStatus.UNRESOLVED),
        )


def _status_for(*, supporting: list[UUID], opposing: list[UUID]) -> HypothesisStatus:
    """由证据关系推导假设状态。

    Returns:
        ``SUPPORTED`` / ``REJECTED`` / ``UNDER_EVALUATION`` / ``UNRESOLVED``。
        **不存在通往"已确认事实"的分支。**
    """
    if supporting and opposing:
        return HypothesisStatus.UNDER_EVALUATION
    if supporting:
        return HypothesisStatus.SUPPORTED
    if opposing:
        return HypothesisStatus.REJECTED
    return HypothesisStatus.UNRESOLVED


# ⚠️ 这里曾经有一个 ``HypothesisBundle``（"假设及其评估的组合，
# 供下游合成器使用"）。它**全仓零引用**——没有任何"下游合成器"
# 接收它，它也不在任何 ``__all__`` 里。
#
# 阶段 6.5 §八 评审 A 找出来之后按 §四 的三选一删掉了：
# 它的文档描述了一个**并不存在**的调用方，而那种"看起来已经接好了"
# 的描述比没有代码更容易误导人。
