"""因果分析。

任务书 §9.7 把"是否由相关推出因果"列为逻辑检查的一项；
本模块把这一项单独展开——因为它是**后果最严重**的一类推理错误：
一个被当成因果的相关关系会驱动错误的行动，而且很难被后续观察纠正。

D2 及以上运行。
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from ai_psi.cognition.base import ModuleOutcome, invocation_context
from ai_psi.prompts.schemas import CausalAnalysisOutput, CausalAnalyzerInput
from ai_psi.providers.gateway import ModelGateway

__all__ = ["CausalAnalyzer"]


class CausalAnalyzer:
    """因果主张检查器。"""

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
        causal_claims: Sequence[str] = (),
        user_id: UUID | None = None,
        conversation_id: UUID | None = None,
        cognitive_round_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> ModuleOutcome[CausalAnalysisOutput]:
        """执行因果分析。

        Args:
            question: 认知问题。
            causal_claims: 待检查的因果主张。
            user_id: 归属用户。
            conversation_id: 所属会话。
            cognitive_round_id: 当前回合 id。
            correlation_id: 关联链标识。

        Returns:
            因果分析结果与调用记录。
        """
        payload = CausalAnalyzerInput(question=question, causal_claims=list(causal_claims))
        call = await self._gateway.structured(
            task_name="causal_analyzer",
            payload=payload,
            response_model=CausalAnalysisOutput,
            context=invocation_context(
                cognitive_round_id=cognitive_round_id,
                conversation_id=conversation_id,
                user_id=user_id,
                correlation_id=correlation_id,
            ),
        )
        return ModuleOutcome(value=call.value, invocations=(call.invocation,))
