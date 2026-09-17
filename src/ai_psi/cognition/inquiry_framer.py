"""问题框定（任务书 §9.2）。

把一个关切转化为**一个可结束的问题**。产出分两部分：

* :class:`~ai_psi.prompts.schemas.InquiryDraft` —— 问题的边界（范围、排除项、停止条件）；
* :class:`~ai_psi.prompts.schemas.DepthSignals` —— 深度路由的模型侧信号。

🔴 **领域对象在深度确定之后才构造。**

:class:`~ai_psi.domain.inquiries.Inquiry` 带着 ``depth_level`` 字段，
而深度要等 :func:`~ai_psi.cognition.depth_router.route_depth` 看过信号之后才能定。
先在草稿里定稿、再造领域对象，比"先造一个深度错的、再改"干净得多——
后者会在事件的 ``inquiry.created`` 里留下一次并不真实的深度变更。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from ai_psi.cognition.base import ModuleOutcome, invocation_context
from ai_psi.domain.concerns import Concern
from ai_psi.domain.enums import CognitiveDepth
from ai_psi.domain.events import ModelInvocationInfo
from ai_psi.domain.inquiries import Inquiry
from ai_psi.prompts.schemas import (
    DepthSignals,
    InquiryDraft,
    InquiryFramerInput,
    InquiryFramerOutput,
)
from ai_psi.providers.gateway import ModelGateway

__all__ = ["FramedInquiry", "InquiryFramer", "to_domain_inquiry"]


@dataclass(frozen=True, slots=True)
class FramedInquiry:
    """问题框定的结果（尚未定深度）。

    Attributes:
        draft: 问题草稿。
        signals: 深度信号。
        invocations: 本次消耗的模型调用记录。
    """

    draft: InquiryDraft
    signals: DepthSignals
    invocations: tuple[ModelInvocationInfo, ...] = ()


class InquiryFramer:
    """把关切框定为可结束的问题。"""

    def __init__(self, gateway: ModelGateway) -> None:
        """初始化。

        Args:
            gateway: 模型调用网关。
        """
        self._gateway = gateway

    async def frame(
        self,
        *,
        concern: Concern,
        user_message: str,
        known_observations: Sequence[str] = (),
        current_beliefs: Sequence[str] = (),
        user_id: UUID | None = None,
        conversation_id: UUID | None = None,
        cognitive_round_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> ModuleOutcome[FramedInquiry]:
        """框定问题。

        Args:
            concern: 上游关切。
            user_message: 用户消息原文。
            known_observations: 已知观察的摘要。
            current_beliefs: 与本题相关的当前信念。
            user_id: 归属用户。
            conversation_id: 所属会话。
            cognitive_round_id: 当前回合 id。
            correlation_id: 关联链标识。

        Returns:
            问题草稿、深度信号与调用记录。
        """
        payload = InquiryFramerInput(
            concern_statement=concern.statement,
            why_it_matters=concern.why_it_matters,
            user_message=user_message,
            known_observations=list(known_observations),
            current_beliefs=list(current_beliefs),
        )
        call = await self._gateway.structured(
            task_name="inquiry_framer",
            payload=payload,
            response_model=InquiryFramerOutput,
            context=invocation_context(
                cognitive_round_id=cognitive_round_id,
                conversation_id=conversation_id,
                user_id=user_id,
                correlation_id=correlation_id,
            ),
        )

        return ModuleOutcome(
            value=FramedInquiry(
                draft=call.value.inquiry,
                signals=call.value.inquiry.depth_signals,
                invocations=(call.invocation,),
            ),
            invocations=(call.invocation,),
        )


def to_domain_inquiry(
    framed: FramedInquiry,
    *,
    concern: Concern,
    depth: CognitiveDepth,
    created_by: str = "inquiry_framer",
) -> Inquiry:
    """把问题草稿映射为领域对象。

    🔴 **显式映射，不做一键互转**（ADR-0006）。
    草稿里带着 ``depth_signals`` 这种提示词层概念，领域对象里不该有它。

    Args:
        framed: 框定结果。
        concern: 上游关切。
        depth: 深度路由的最终结果。
        created_by: 创建者标识。

    Returns:
        领域问题对象。
    """
    draft = framed.draft
    return Inquiry(
        created_by=created_by,
        concern_id=concern.id,
        question=draft.question,
        why_it_matters=draft.why_it_matters,
        scope=draft.scope,
        out_of_scope=draft.out_of_scope,
        key_unknowns=draft.key_unknowns,
        ambiguous_concepts=draft.ambiguous_concepts,
        assumptions_to_check=draft.assumptions_to_check,
        expected_output_type=draft.expected_output_type,
        verification_method=draft.verification_method,
        stop_conditions=draft.stop_conditions,
        reopen_conditions=draft.reopen_conditions,
        depth_level=depth,
    )
