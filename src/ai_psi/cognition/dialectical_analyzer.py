"""辩证分析（任务书 §9.8）。

D3/D4 使用。目标是找出**真实的张力在哪里**，以及它能否被有条件地消解。

🔴 **禁止机械地"双方都有道理"。**

这条禁令在代码里有一个可测的落点：
:class:`~ai_psi.prompts.schemas.DialecticalAnalysisOutput` 要求
``irreducible_tension`` 与 ``conditional_synthesis`` **至少填一个**。
两者皆空说明这次分析没有产生任何判断力——那正是"各有各的道理"
这种空洞输出的结构特征。

本模块在此之上再加一道：如果两者皆空，**补入一条**说明
"本次分析未能消解该张力"，而不是让一个空壳结果流向判断合成器。
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from ai_psi.cognition.base import ModuleOutcome, invocation_context
from ai_psi.prompts.schemas import (
    DialecticalAnalysisOutput,
    DialecticalAnalyzerInput,
)
from ai_psi.providers.gateway import ModelGateway

__all__ = ["DialecticalAnalyzer"]

_EMPTY_ANALYSIS_NOTE = "本次分析未能消解该张力：现有材料不足以判定哪一方在何种条件下成立"


class DialecticalAnalyzer:
    """辩证分析器。"""

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
        position: str = "",
        supporting_reasons: Sequence[str] = (),
        value_conflicts: Sequence[str] = (),
        user_id: UUID | None = None,
        conversation_id: UUID | None = None,
        cognitive_round_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> ModuleOutcome[DialecticalAnalysisOutput]:
        """执行辩证分析。

        Args:
            question: 认知问题。
            position: 当前主张。
            supporting_reasons: 已知的支持理由。
            value_conflicts: 涉及的价值冲突。
            user_id: 归属用户。
            conversation_id: 所属会话。
            cognitive_round_id: 当前回合 id。
            correlation_id: 关联链标识。

        Returns:
            辩证分析结果与调用记录。
        """
        payload = DialecticalAnalyzerInput(
            question=question,
            position=position,
            supporting_reasons=list(supporting_reasons),
            value_conflicts=list(value_conflicts),
        )
        call = await self._gateway.structured(
            task_name="dialectical_analyzer",
            payload=payload,
            response_model=DialecticalAnalysisOutput,
            context=invocation_context(
                cognitive_round_id=cognitive_round_id,
                conversation_id=conversation_id,
                user_id=user_id,
                correlation_id=correlation_id,
            ),
        )

        notes: list[str] = []
        value = call.value
        if not value.irreducible_tension and not value.conditional_synthesis:
            # 🔴 机械折中的结构特征：既没说张力在哪，也没给出综合。
            # 补一条明确的"未消解"说明，而不是放行一个空洞结果。
            value = value.model_copy(update={"irreducible_tension": _EMPTY_ANALYSIS_NOTE})
            notes.append("辩证分析未给出张力或综合，已标记为未消解（§9.8）")

        return ModuleOutcome(
            value=value,
            invocations=(call.invocation,),
            notes=tuple(notes),
        )
