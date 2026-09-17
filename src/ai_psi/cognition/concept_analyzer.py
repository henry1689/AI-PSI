"""概念分析（任务书 §9.5）。

D2 可选、D3/D4 必跑。

许多看似分歧的争论实际上是**用语分歧**——双方对同一个词的理解不同。
把定义显式化，往往能消解掉一半的"冲突"，剩下的才是真实分歧。

概念清单来自 :attr:`~ai_psi.domain.inquiries.Inquiry.ambiguous_concepts`
（框定阶段标记的）与用户消息中的关键术语。
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from ai_psi.cognition.base import ModuleOutcome, invocation_context
from ai_psi.prompts.schemas import (
    ConceptAnalysisOutput,
    ConceptAnalyzerInput,
)
from ai_psi.providers.gateway import ModelGateway

__all__ = ["ConceptAnalyzer"]


class ConceptAnalyzer:
    """概念澄清器。"""

    def __init__(self, gateway: ModelGateway) -> None:
        """初始化。

        Args:
            gateway: 模型调用网关。
        """
        self._gateway = gateway

    async def analyze(
        self,
        *,
        question: str,
        concepts: Sequence[str] = (),
        context: Sequence[str] = (),
        user_id: UUID | None = None,
        conversation_id: UUID | None = None,
        cognitive_round_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> ModuleOutcome[ConceptAnalysisOutput]:
        """执行概念分析。

        Args:
            question: 认知问题。
            concepts: 需要澄清的术语。
            context: 这些术语出现的语境。
            user_id: 归属用户。
            conversation_id: 所属会话。
            cognitive_round_id: 当前回合 id。
            correlation_id: 关联链标识。

        Returns:
            概念分析结果与调用记录。
        """
        payload = ConceptAnalyzerInput(
            question=question,
            concepts=list(concepts),
            context=list(context),
        )
        call = await self._gateway.structured(
            task_name="concept_analyzer",
            payload=payload,
            response_model=ConceptAnalysisOutput,
            context=invocation_context(
                cognitive_round_id=cognitive_round_id,
                conversation_id=conversation_id,
                user_id=user_id,
                correlation_id=correlation_id,
            ),
        )
        return ModuleOutcome(value=call.value, invocations=(call.invocation,))
