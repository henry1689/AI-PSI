"""认知模块的公共类型。

认知模块（分析器、生成器、合成器）共享一个极小的共同形状：
**拿着网关调用一次模型，把结果与调用记录一起交出去。**

刻意不做基类继承。各模块的输入输出差异很大，
强行抽出 `BaseAnalyzer` 只会得到一个"什么都不是"的抽象——
调用方真正需要的只是 :class:`ModuleOutcome` 这一个数据结构。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ai_psi.domain.events import ModelInvocationInfo
from ai_psi.providers.base import InvocationContext

__all__ = ["ModuleOutcome", "invocation_context"]


def invocation_context(
    *,
    cognitive_round_id: UUID | None = None,
    conversation_id: UUID | None = None,
    user_id: UUID | None = None,
    correlation_id: UUID | None = None,
) -> InvocationContext:
    """构造模型调用上下文。

    各认知模块统一用它，避免每个模块各写一遍、
    也就不会出现"某个模块忘了带 round_id"这种只在排查时才发现的缺口。

    Returns:
        调用上下文。
    """
    return InvocationContext(
        cognitive_round_id=cognitive_round_id,
        conversation_id=conversation_id,
        user_id=user_id,
        correlation_id=correlation_id,
    )


@dataclass(frozen=True, slots=True)
class ModuleOutcome[T]:
    """一次认知模块执行的结果。

    Attributes:
        value: 模块产出的对象。
        invocations: 本次执行消耗的模型调用记录，**按发生顺序**。
            调用方负责把它们挂到对应事件上——不变量 18 要求
            "所有模型调用必须记录模型和 Prompt 版本"，
            而调用记录只有在事件里才可审计。
        notes: 供诊断的自由文本（例如"因预算不足已跳过"）。
            不进入任何领域对象。
    """

    value: T
    invocations: tuple[ModelInvocationInfo, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def invocation(self) -> ModelInvocationInfo | None:
        """最后一次调用的记录；没有模型调用时返回 ``None``。"""
        return self.invocations[-1] if self.invocations else None

    @property
    def model_call_count(self) -> int:
        """本次执行消耗的模型调用次数。"""
        return len(self.invocations)
