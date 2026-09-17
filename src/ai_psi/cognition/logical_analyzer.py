"""逻辑分析（任务书 §9.7）。

检查论证的**连接**是否成立：前提是否支持结论、是否循环论证、
是否偷换概念、是否混淆必要与充分条件、是否由相关推出因果、
是否过度概括、是否忽略反例、是否把价值偏好当作事实。

D1 及以上必跑。D0 直答问题不做逻辑分析——对"水在标准大气压下的沸点"
做谬误排查，正是任务书 §9.8 禁止的那类无意义动作。
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from ai_psi.cognition.base import ModuleOutcome, invocation_context
from ai_psi.domain.hypotheses import Hypothesis
from ai_psi.prompts.schemas import (
    LogicalAnalysisOutput,
    LogicalAnalyzerInput,
)
from ai_psi.providers.gateway import ModelGateway

__all__ = ["LogicalAnalyzer"]


class LogicalAnalyzer:
    """论证连接检查器。"""

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
        claims: Sequence[str] = (),
        premises: Sequence[str] = (),
        hypotheses: Sequence[Hypothesis] = (),
        user_id: UUID | None = None,
        conversation_id: UUID | None = None,
        cognitive_round_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> ModuleOutcome[LogicalAnalysisOutput]:
        """执行逻辑分析。

        Args:
            question: 认知问题。
            claims: 待检查的主张。
            premises: 主张所依赖的前提。
            hypotheses: 本回合的候选假设（作为待检查的主张一并送入）。
            user_id: 归属用户。
            conversation_id: 所属会话。
            cognitive_round_id: 当前回合 id。
            correlation_id: 关联链标识。

        Returns:
            逻辑分析结果与调用记录。
        """
        payload = LogicalAnalyzerInput(
            question=question,
            claims=list(claims) or [item.statement for item in hypotheses],
            premises=list(premises),
            hypotheses=[item.statement for item in hypotheses],
        )
        call = await self._gateway.structured(
            task_name="logical_analyzer",
            payload=payload,
            response_model=LogicalAnalysisOutput,
            context=invocation_context(
                cognitive_round_id=cognitive_round_id,
                conversation_id=conversation_id,
                user_id=user_id,
                correlation_id=correlation_id,
            ),
        )
        return ModuleOutcome(value=call.value, invocations=(call.invocation,))
