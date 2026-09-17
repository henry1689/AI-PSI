"""回答规划（任务书 §9.12 的 Planner）。

⚠️ **本模块是确定性代码，不是模型调用**（偏差登记：ADR-0015）。

Planner 要决定的六件事——直接回答什么、哪些不确定性要告诉用户、
是否需要展示替代解释、哪些内部候选不应表达、是否需要澄清、回答多长——
**每一件都能从 :class:`~ai_psi.domain.judgments.Judgment` 直接读出来**。

让模型来决定"要不要表现得确定"，等于让被约束方自己执行不变量 7。
放在代码里，不变量 7 就从"事后抽查"变成了"结构性成立"。

唯一需要判断力的是"哪些候选不该说"——那也有明确的规则：
**在没有证据支持时，不向用户呈现关于第三方心理动机的推断**
（任务书 §9.1、场景 B）。被隐藏的内容会记进 ``withheld_candidates``，
所以"隐瞒"本身也是可审计的。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from ai_psi.domain.enums import (
    CognitiveDepth,
    ConfidenceBand,
    EpistemicAction,
    HypothesisCategory,
    HypothesisStatus,
)
from ai_psi.domain.hypotheses import Hypothesis
from ai_psi.domain.inquiries import Inquiry
from ai_psi.domain.judgments import Judgment
from ai_psi.domain.reflections import Reflection
from ai_psi.prompts.schemas import LengthHint, ResponsePlan, Tone

__all__ = ["MAX_ALTERNATIVES_SHOWN", "ResponsePlanner"]

#: 最多向用户展示几个替代解释。超过这个数会让回答变成清单。
MAX_ALTERNATIVES_SHOWN: Final[int] = 3

#: 深度到回答长度的映射。
_LENGTH_BY_DEPTH: Final[dict[CognitiveDepth, LengthHint]] = {
    CognitiveDepth.D0: "short",
    CognitiveDepth.D1: "short",
    CognitiveDepth.D2: "medium",
    CognitiveDepth.D3: "medium",
    CognitiveDepth.D4: "long",
}


class ResponsePlanner:
    """把判断翻译成回答方案。"""

    def plan(
        self,
        *,
        judgment: Judgment,
        inquiry: Inquiry,
        hypotheses: Sequence[Hypothesis] = (),
        depth: CognitiveDepth = CognitiveDepth.D0,
        response_style: str = "structured",
        reflection: Reflection | None = None,
        conflicts: Sequence[str] = (),
    ) -> ResponsePlan:
        """生成回答方案。

        Args:
            judgment: 内部判断（回答的唯一事实来源）。
            inquiry: 所属认知问题。
            hypotheses: 候选假设（用于决定展示哪些替代解释）。
            depth: 认知深度。
            response_style: 用户请求的回答风格。
            reflection: 本轮的元认知反思；``None`` 表示未运行元认知。
            conflicts: 上下文中的冲突材料摘要。

        Returns:
            回答方案。
        """
        alternatives, withheld = _split_candidates(hypotheses, depth=depth)
        needs_clarification = judgment.recommended_epistemic_action in {
            EpistemicAction.REQUEST_EVIDENCE,
            EpistemicAction.DEFER,
        }

        return ResponsePlan(
            direct_answer_points=[judgment.conclusion],
            uncertainties_to_surface=_surfaced_uncertainties(judgment, conflicts),
            alternative_explanations_to_show=alternatives,
            withheld_candidates=withheld,
            needs_clarification=needs_clarification,
            clarification_question=_clarification_question(inquiry, needs_clarification),
            length_hint=_LENGTH_BY_DEPTH[depth],
            depth_hint=depth,
            tone=_tone_for(judgment),
            allows_strong_conclusion=judgment.allows_strong_conclusion,
            response_style=response_style,
        )


def _surfaced_uncertainties(
    judgment: Judgment,
    conflicts: Sequence[str],
) -> list[str]:
    """汇总必须向用户说明的不确定性。

    🔴 **"存在高可信冲突"必须说出来，即使判断自己没把它记成"未知"。**
    模型完全可能一边看到两条互相矛盾的可靠来源、一边给出一个看起来
    没有未知的结论——那正是任务书 §9.4 的 ``CONFLICTING`` 分类
    要防住的情形。冲突来自上下文，因此由上下文补进方案。
    没有这一步，"不强行合并冲突"（场景 E）就只是一句口号。

    Args:
        judgment: 内部判断。
        conflicts: 上下文中的冲突材料摘要。

    Returns:
        去重后的不确定性条目。
    """
    surfaced = list(judgment.unresolved_unknowns)
    surfaced.extend(f"材料之间存在冲突：{item}" for item in conflicts if item not in surfaced)
    return list(dict.fromkeys(surfaced))


def _split_candidates(
    hypotheses: Sequence[Hypothesis],
    *,
    depth: CognitiveDepth,
) -> tuple[list[str], list[str]]:
    """把候选假设分成"可以展示"与"应当隐藏"两组。

    🔴 **第三方的心理推断在没有证据支持时不向用户呈现。**
    这不是审查，而是任务书 §9.1 与场景 B 的明确要求：
    系统只描述可观察的行为，不对第三方的人格与动机下结论。

    被隐藏的候选会写进 ``withheld_candidates``——
    "系统决定不说什么"和"系统说了什么"同样需要可审计。

    Args:
        hypotheses: 候选假设。
        depth: 认知深度。

    Returns:
        ``(可以展示的替代解释, 被隐藏的候选)``。
    """
    showable: list[str] = []
    withheld: list[str] = []

    for hypothesis in hypotheses:
        unsupported = hypothesis.status in {HypothesisStatus.REJECTED, HypothesisStatus.UNRESOLVED}
        if hypothesis.category is HypothesisCategory.INTENTIONAL and unsupported:
            withheld.append(hypothesis.statement)
            continue
        if depth.level < CognitiveDepth.D2.level:
            # D0/D1 不做多假设展示——那正是"不产生多个无意义假设"（场景 A）
            continue
        if hypothesis.is_non_agentic or hypothesis.status in {
            HypothesisStatus.SUPPORTED,
            HypothesisStatus.UNDER_EVALUATION,
        }:
            showable.append(hypothesis.statement)

    return showable[:MAX_ALTERNATIVES_SHOWN], withheld


def _clarification_question(inquiry: Inquiry, needed: bool) -> str | None:
    """生成澄清问题。

    V0.1 只在**框定阶段已经标记出歧义概念**时生成澄清问题——
    因为没有歧义概念时的"你想问什么"往往只是把问题推回给用户，
    而不是真的缺少信息。

    Args:
        inquiry: 认知问题。
        needed: 是否需要澄清。

    Returns:
        澄清问题；不需要或无从问起时返回 ``None``。
    """
    if not needed or not inquiry.ambiguous_concepts:
        return None
    return (
        f"「{inquiry.ambiguous_concepts[0]}」在这里具体指什么？换个说法可能会得到完全不同的回答。"
    )


def _tone_for(judgment: Judgment) -> Tone:
    """由判断推导回答语气。

    🔴 **语气是判断的函数，不是模型的自由选择**（不变量 7）。

    Args:
        judgment: 内部判断。

    Returns:
        语气档位。
    """
    if judgment.allows_strong_conclusion:
        return "assertive"
    if judgment.confidence_band is ConfidenceBand.VERY_LOW:
        return "tentative"
    if judgment.confidence_band.rank >= ConfidenceBand.MODERATE.rank:
        return "plain"
    return "cautious"
