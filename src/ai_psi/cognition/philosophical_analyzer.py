"""哲理分析（任务书 §9.9）。

仅 D4 默认运行。目标是把问题背后的**框架**显式化，
而不是用抽象词句把问题说得更玄。

🔴 **不变量 9：D4 哲理分析不能覆盖事实层未知。**

实现方式有两层：

1. **输入侧**：事实层未知（``factual_unknowns``）作为**显式字段**送入提示词，
   模型被明确告知它们不可被"分析掉"；
2. **输出侧**：本模块逐条核对——如果某条事实未知在
   ``epistemic_limits`` 与 ``unresolved_tensions`` 里都没有留下痕迹，
   就把它补进 ``epistemic_limits``，并在事件负载里记录补入事实。

第 2 条是必需的：提示词是请求，不是保证。
"用抽象语言回避事实不足"恰恰是这类分析最容易出现的失败模式，
而它又极难从输出本身看出来——因为输出看起来很有深度。
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from ai_psi.cognition.base import ModuleOutcome, invocation_context
from ai_psi.prompts.schemas import (
    PhilosophicalAnalysisOutput,
    PhilosophicalAnalyzerInput,
)
from ai_psi.providers.gateway import ModelGateway

__all__ = ["PhilosophicalAnalyzer"]


class PhilosophicalAnalyzer:
    """框架层分析器。"""

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
        value_conflicts: Sequence[str] = (),
        factual_unknowns: Sequence[str] = (),
        user_id: UUID | None = None,
        conversation_id: UUID | None = None,
        cognitive_round_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> ModuleOutcome[PhilosophicalAnalysisOutput]:
        """执行哲理分析。

        Args:
            question: 认知问题。
            value_conflicts: 涉及的价值冲突。
            factual_unknowns: 事实层的未知。**不可被分析覆盖**（不变量 9）。
            user_id: 归属用户。
            conversation_id: 所属会话。
            cognitive_round_id: 当前回合 id。
            correlation_id: 关联链标识。

        Returns:
            哲理分析结果与调用记录。
        """
        payload = PhilosophicalAnalyzerInput(
            question=question,
            value_conflicts=list(value_conflicts),
            factual_unknowns=list(factual_unknowns),
        )
        call = await self._gateway.structured(
            task_name="philosophical_analyzer",
            payload=payload,
            response_model=PhilosophicalAnalysisOutput,
            context=invocation_context(
                cognitive_round_id=cognitive_round_id,
                conversation_id=conversation_id,
                user_id=user_id,
                correlation_id=correlation_id,
            ),
        )

        value, restored = _restore_factual_unknowns(call.value, factual_unknowns)
        notes = (
            (f"哲理分析未保留 {len(restored)} 条事实层未知，已补入 epistemic_limits（不变量 9）",)
            if restored
            else ()
        )
        return ModuleOutcome(value=value, invocations=(call.invocation,), notes=notes)


def _restore_factual_unknowns(
    analysis: PhilosophicalAnalysisOutput,
    factual_unknowns: Sequence[str],
) -> tuple[PhilosophicalAnalysisOutput, list[str]]:
    """把被分析"消化掉"的事实层未知补回输出。

    Args:
        analysis: 模型给出的哲理分析。
        factual_unknowns: 送入提示词的事实层未知。

    Returns:
        ``(修正后的分析, 被补回的未知列表)``。
    """
    haystack = " ".join([*analysis.epistemic_limits, *analysis.unresolved_tensions])
    missing = [item for item in factual_unknowns if item and item not in haystack]
    if not missing:
        return analysis, []
    return (
        analysis.model_copy(update={"epistemic_limits": [*analysis.epistemic_limits, *missing]}),
        missing,
    )
