"""认知深度路由（任务书 §7，ADR-0008）。

🔴 **深度由代码决定，不由模型决定。**

任务书 §7.3 的措辞是"优先使用确定性规则，再允许模型建议"，
并且明确禁止"为了展示能力而自动提升到 D4"。
既然最终决定权在代码，那么最干净的做法就是：

* 模型侧只提供**深度信号**（:class:`~ai_psi.prompts.schemas.DepthSignals`），
  它们是关于问题性质的事实性判断（是否涉及价值冲突、是否存在多个解释）；
* 代码侧 :func:`route_depth` 依据信号、用户显式请求与**可用预算**决定深度。

被约束方不参与约束自己的规则的制定——这是不变量 7 的同一思路。

**模块矩阵与预算是绑定的**：算出一个预算撑不起的深度没有意义，
因此路由的最后一步是**预算降级**（ADR-0008：降级并显式说明，
而不是悄悄超支，也不是直接拒绝）。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from ai_psi.domain.cognitive_rounds import CognitiveBudget
from ai_psi.domain.enums import CognitiveDepth, OrdinalLevel
from ai_psi.prompts.schemas import DepthSignals

__all__ = [
    "NOMINAL_MODEL_CALLS_BY_DEPTH",
    "DepthRoutingInput",
    "DepthRoutingResult",
    "route_depth",
]


class DepthRoutingInput(BaseModel):
    """深度路由的输入（任务书 §7.2 的结构）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    inquiry: str = Field(min_length=1)

    estimated_impact: OrdinalLevel = OrdinalLevel.MODERATE
    ambiguity: OrdinalLevel = OrdinalLevel.MODERATE
    evidence_conflict: OrdinalLevel = OrdinalLevel.LOW
    value_conflict: OrdinalLevel = OrdinalLevel.LOW
    long_term_relevance: OrdinalLevel = OrdinalLevel.LOW

    #: 模型侧的结构性信号（§7.2 之外的补充；ADR-0015 登记）。
    signals: DepthSignals = Field(default_factory=DepthSignals)

    user_requested_depth: CognitiveDepth | None = None
    available_budget: CognitiveBudget = Field(default_factory=CognitiveBudget)

    @classmethod
    def from_signals(
        cls,
        *,
        inquiry: str,
        signals: DepthSignals,
        user_requested_depth: CognitiveDepth | None,
        available_budget: CognitiveBudget,
    ) -> DepthRoutingInput:
        """从 :class:`DepthSignals` 构造路由输入。

        Args:
            inquiry: 认知问题的核心表述。
            signals: 模型给出的深度信号。
            user_requested_depth: 用户显式请求的深度。
            available_budget: 可用预算。

        Returns:
            路由输入。
        """
        return cls(
            inquiry=inquiry,
            estimated_impact=signals.estimated_impact,
            ambiguity=signals.ambiguity,
            evidence_conflict=signals.evidence_conflict,
            value_conflict=signals.value_conflict,
            long_term_relevance=signals.long_term_relevance,
            signals=signals,
            user_requested_depth=user_requested_depth,
            available_budget=available_budget,
        )


@dataclass(frozen=True, slots=True)
class DepthRoutingResult:
    """深度路由的结果。

    Attributes:
        depth: 最终深度。
        requested_depth: 用户显式请求的深度（未请求时为 ``None``）。
        reason: 定级的依据，写入事件负载以便事后核对。
        degraded_from: 因预算不足而由该深度降级而来；未降级时为 ``None``。
    """

    depth: CognitiveDepth
    requested_depth: CognitiveDepth | None
    reason: str
    degraded_from: CognitiveDepth | None = None

    @property
    def is_degraded(self) -> bool:
        """是否发生了预算降级。"""
        return self.degraded_from is not None

    @property
    def honoured_user_request(self) -> bool:
        """用户显式请求的深度是否被完整采纳。"""
        return self.requested_depth is not None and self.depth is self.requested_depth


#: 各深度的**标称**模型调用数（单次分析、不含额外元认知循环）。
#:
#: 与 :data:`~ai_psi.cognition.orchestrator.MODULE_MATRIX` 一一对应，
#: 由 ``tests/unit/test_depth_router.py`` 断言两者不漂移。
NOMINAL_MODEL_CALLS_BY_DEPTH: Final[MappingProxyType[CognitiveDepth, int]] = MappingProxyType(
    {
        CognitiveDepth.D0: 4,
        CognitiveDepth.D1: 6,
        CognitiveDepth.D2: 8,
        CognitiveDepth.D3: 10,
        CognitiveDepth.D4: 11,
    }
)

#: 一次额外元认知循环的模型调用代价：判断合成 + 元认知。
#:
#: ANALYZING 阶段的各分析器**不重跑**——那是"换方法"，由元认知决策
#: ``CHANGE_METHOD`` 触发，不是默认路径。
EXTRA_LOOP_MODEL_CALLS: Final[int] = 2


def route_depth(routing_input: DepthRoutingInput) -> DepthRoutingResult:
    """按确定性规则决定认知深度。

    判定顺序（**先命中先返回**）：深层信号优先于浅层，
    因为涉及价值冲突的问题即使看起来像简单比较，也不该被压到 D1。

    Args:
        routing_input: 路由输入。

    Returns:
        路由结果。
    """
    requested = routing_input.user_requested_depth

    if requested is not None:
        candidate, reason = requested, "用户显式指定深度"
    else:
        candidate, reason = _rule_route(routing_input)

    depth, degraded_from = _apply_budget(candidate, routing_input.available_budget)
    if degraded_from is not None:
        reason = f"{reason}；因可用预算不足由 {degraded_from.value} 降级"

    return DepthRoutingResult(
        depth=depth,
        requested_depth=requested,
        reason=reason,
        degraded_from=degraded_from,
    )


def _rule_route(routing_input: DepthRoutingInput) -> tuple[CognitiveDepth, str]:
    """按规则表定级。

    🔴 **不允许"看起来更深显得更强"的路径。** 升到 D4 必须由
    "用户明确提出哲学问题"或"存在根本框架冲突"触发——
    这两个信号都来自对**问题性质**的判断，而不是对答案长度的期待。

    Args:
        routing_input: 路由输入。

    Returns:
        ``(深度, 依据)``。
    """
    signals = routing_input.signals
    high = OrdinalLevel.HIGH

    if signals.user_explicitly_philosophical or signals.framework_conflict:
        return CognitiveDepth.D4, "用户明确提出哲学问题，或存在根本框架冲突"

    if routing_input.value_conflict.rank >= high.rank:
        return CognitiveDepth.D3, "涉及价值选择，且价值冲突显著"

    if routing_input.long_term_relevance.rank >= high.rank:
        return CognitiveDepth.D3, "涉及长期关系、社会结构或人生选择"

    if signals.multiple_plausible_interpretations:
        return CognitiveDepth.D2, "存在多个合理解释"

    if routing_input.evidence_conflict.rank >= OrdinalLevel.MODERATE.rank:
        return CognitiveDepth.D2, "高可信材料之间存在冲突"

    if (
        signals.simple_fact_with_sufficient_evidence
        and routing_input.ambiguity.rank < OrdinalLevel.MODERATE.rank
        and routing_input.evidence_conflict.rank < OrdinalLevel.MODERATE.rank
    ):
        return CognitiveDepth.D0, "简单事实问题且证据充分"

    if signals.needs_explanation_or_comparison:
        return CognitiveDepth.D1, "需要解释或比较"

    return CognitiveDepth.D1, "无强信号，按基础分析处理"


def _apply_budget(
    candidate: CognitiveDepth,
    available_budget: CognitiveBudget,
) -> tuple[CognitiveDepth, CognitiveDepth | None]:
    """把候选深度降到预算撑得起的最高档。

    Args:
        candidate: 候选深度。
        available_budget: 可用预算。

    Returns:
        ``(最终深度, 被降级的原深度或 None)``。
    """
    limit = available_budget.max_model_calls
    if NOMINAL_MODEL_CALLS_BY_DEPTH[candidate] <= limit:
        return candidate, None

    for level in (CognitiveDepth.D3, CognitiveDepth.D2, CognitiveDepth.D1, CognitiveDepth.D0):
        if level.level < candidate.level and NOMINAL_MODEL_CALLS_BY_DEPTH[level] <= limit:
            return level, candidate
    # 预算低于 D0 的标称需求：仍然执行 D0，由预算记账器在运行期拦截。
    return CognitiveDepth.D0, candidate
