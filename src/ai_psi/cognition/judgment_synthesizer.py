"""判断合成（任务书 §9.10）。

这是认知回合的**结论**，也是回答渲染的唯一依据。

🔴 **本模块是"模型说要怎样"与"系统允许怎样"的交界处**，
因此三处硬约束都在这里落地：

1. **置信度上限由代码算**（:func:`~ai_psi.reliability.confidence.derive_ceiling`），
   模型给出的档位只能被**下调**，永不上调；
2. **不变量 3**：存在高可信冲突或未解决未知时，不得输出无保留结论。
   模型的输出若违反，会被**改写**并留痕，而不是让回合失败——
   改写保住了不变量，也保住了用户的回答；
3. **不变量 1**：被采纳的假设仍是 ``SUPPORTED``，不是"已确认事实"。

所有被改写的地方都记在 :attr:`SynthesizedJudgment.adjustments` 里，
由应用层写进 ``judgment.created`` 事件负载——**静默改写不可接受**。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from ai_psi.cognition.base import ModuleOutcome, invocation_context
from ai_psi.cognition.context_builder import ContextBundle
from ai_psi.domain.enums import ConfidenceBand, EpistemicAction, HypothesisStatus
from ai_psi.domain.events import ModelInvocationInfo
from ai_psi.domain.hypotheses import Hypothesis
from ai_psi.domain.inquiries import Inquiry
from ai_psi.domain.judgments import Judgment
from ai_psi.prompts.schemas import (
    CausalAnalysisOutput,
    DialecticalAnalysisOutput,
    JudgmentDraft,
    JudgmentSynthesizerInput,
    JudgmentSynthesizerOutput,
    LogicalAnalysisOutput,
    PhilosophicalAnalysisOutput,
)
from ai_psi.providers.gateway import ModelGateway
from ai_psi.reliability.confidence import clamp, derive_ceiling

__all__ = ["JudgmentSynthesizer", "SynthesizedJudgment"]


@dataclass(frozen=True, slots=True)
class SynthesizedJudgment:
    """判断合成的结果。

    Attributes:
        judgment: 领域判断对象。
        adjustments: 系统对模型输出所做的改写，**逐条留痕**。
        confidence_ceiling: 本次计算出的置信上限。
        invocations: 模型调用记录。
    """

    judgment: Judgment
    adjustments: tuple[str, ...] = ()
    confidence_ceiling: ConfidenceBand = ConfidenceBand.LOW
    invocations: tuple[ModelInvocationInfo, ...] = ()


class JudgmentSynthesizer:
    """把假设、证据与未知合成为一个暂定判断。"""

    def __init__(self, gateway: ModelGateway) -> None:
        """初始化。

        Args:
            gateway: 模型调用网关。
        """
        self._gateway = gateway

    async def synthesize(
        self,
        *,
        inquiry: Inquiry,
        bundle: ContextBundle,
        hypotheses: Sequence[Hypothesis] = (),
        logical: LogicalAnalysisOutput | None = None,
        causal: CausalAnalysisOutput | None = None,
        dialectical: DialecticalAnalysisOutput | None = None,
        philosophical: PhilosophicalAnalysisOutput | None = None,
        user_id: UUID | None = None,
        conversation_id: UUID | None = None,
        cognitive_round_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> ModuleOutcome[SynthesizedJudgment]:
        """合成判断。

        Args:
            inquiry: 所属认知问题。
            bundle: 上下文（提供证据数量、来源独立性、冲突信号）。
            hypotheses: 候选假设（带评估后的状态）。
            logical: 逻辑分析结果。
            causal: 因果分析结果。
            dialectical: 辩证分析结果。
            philosophical: 哲理分析结果。
            user_id: 归属用户。
            conversation_id: 所属会话。
            cognitive_round_id: 当前回合 id。
            correlation_id: 关联链标识。

        Returns:
            判断、改写记录与调用记录。
        """
        ceiling = derive_ceiling(
            evidence_count=bundle.evidence_count,
            independent_source_count=bundle.independent_source_count,
            has_high_trust_conflict=bundle.has_high_trust_conflict,
            unresolved_unknown_count=len(inquiry.key_unknowns),
        )

        payload = JudgmentSynthesizerInput(
            question=inquiry.question,
            scope=list(inquiry.scope),
            evidence_summaries=list(bundle.summarize_for_prompt()),
            hypothesis_summaries=[_hypothesis_summary(item) for item in hypotheses],
            key_unknowns=list(inquiry.key_unknowns),
            logical_analysis=logical,
            causal_analysis=causal,
            dialectical_analysis=dialectical,
            philosophical_analysis=philosophical,
            max_confidence_band=ceiling,
        )
        call = await self._gateway.structured(
            task_name="judgment_synthesizer",
            payload=payload,
            response_model=JudgmentSynthesizerOutput,
            context=invocation_context(
                cognitive_round_id=cognitive_round_id,
                conversation_id=conversation_id,
                user_id=user_id,
                correlation_id=correlation_id,
            ),
        )

        draft, adjustments = _apply_safety_adjustments(
            call.value.judgment,
            ceiling=ceiling,
            has_high_trust_conflict=bundle.has_high_trust_conflict,
        )

        judgment = Judgment(
            created_by="judgment_synthesizer",
            inquiry_id=inquiry.id,
            selected_hypothesis_ids=[
                item.id for item in hypotheses if item.status is HypothesisStatus.SUPPORTED
            ],
            conclusion=draft.conclusion,
            rationale_summary=draft.rationale_summary,
            strongest_counterarguments=draft.strongest_counterarguments,
            unresolved_unknowns=draft.unresolved_unknowns,
            applicability=draft.applicability,
            confidence_band=draft.confidence_band,
            confidence_basis=draft.confidence_basis,
            revision_conditions=draft.revision_conditions,
            recommended_epistemic_action=draft.recommended_epistemic_action,
            uncertainty_type=draft.uncertainty_type,
        )

        return ModuleOutcome(
            value=SynthesizedJudgment(
                judgment=judgment,
                adjustments=adjustments,
                confidence_ceiling=ceiling,
                invocations=(call.invocation,),
            ),
            invocations=(call.invocation,),
            notes=tuple(adjustments),
        )


def _hypothesis_summary(hypothesis: Hypothesis) -> str:
    """把假设渲染为给合成器看的一行摘要。"""
    return (
        f"{hypothesis.statement}"
        f"（状态：{hypothesis.status.value}，类别：{hypothesis.category.value}）"
    )


def _apply_safety_adjustments(
    draft: JudgmentDraft,
    *,
    ceiling: ConfidenceBand,
    has_high_trust_conflict: bool,
) -> tuple[JudgmentDraft, tuple[str, ...]]:
    """把模型输出改写为满足不变量 3 与置信上限的形式。

    🔴 **改写必须留痕。** 一个被静默改写的判断在事后回放时
    与"模型原本就这么说"完全无法区分。

    Args:
        draft: 模型给出的判断草稿。
        ceiling: 代码计算的置信上限。
        has_high_trust_conflict: 上下文是否存在高可信冲突。

    Returns:
        ``(改写后的草稿, 改写说明)``。
    """
    adjustments: list[str] = []
    updates: dict[str, object] = {}

    clamped = clamp(draft.confidence_band, ceiling)
    if clamped is not draft.confidence_band:
        updates["confidence_band"] = clamped
        adjustments.append(
            f"置信度由 {draft.confidence_band.value} 下调至 {clamped.value}"
            f"（证据结构计算出的上限为 {ceiling.value}）"
        )

    # 🔴 不变量 3：存在未解决未知时不得给出无保留结论
    if draft.recommended_epistemic_action is EpistemicAction.ANSWER and draft.unresolved_unknowns:
        updates["recommended_epistemic_action"] = EpistemicAction.ANSWER_WITH_CAVEAT
        adjustments.append(
            "认知动作由 answer 下调为 answer_with_caveat：存在未解决未知（不变量 3）"
        )

    # 🔴 不变量 3：存在高可信冲突时不得给出无保留结论
    if (
        draft.recommended_epistemic_action is EpistemicAction.ANSWER
        and has_high_trust_conflict
        and "recommended_epistemic_action" not in updates
    ):
        updates["recommended_epistemic_action"] = EpistemicAction.ANSWER_WITH_CAVEAT
        adjustments.append(
            "认知动作由 answer 下调为 answer_with_caveat：存在高可信冲突（不变量 3）"
        )

    if not updates:
        return draft, ()
    return draft.model_copy(update=updates), tuple(adjustments)
