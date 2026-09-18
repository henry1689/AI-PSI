"""提案生成（任务书 §11.3、§5.12）。

把达到门槛的模式转成一条 ``DRAFT`` 状态的
:class:`~ai_psi.domain.improvement_proposals.ImprovementProposal`。

🔴 **生成提案不等于提出改动方案。**

本模块的模板是**确定性的**，不调用模型。理由有两条：

1. 任务书 §11.1 明令禁止"自动改 Prompt 并上线"。让模型来写改动方案，
   写出来的东西会带着模型的措辞与判断，而它接下来的去向是
   **被人认真评估**——那等于用模型的输出冒充系统观察到的结论。
2. 提案的价值在于**它引用的证据**（``supporting_experience_ids``），
   不在于它的措辞是否漂亮。模板能把这些证据摆清楚就够了。

因此 ``proposed_change`` 描述的是**往哪个方向看**，而不是"改成什么"。
具体改成什么，由评审的人在看到证据之后决定——这正是
``APPROVED_FOR_MANUAL_TRIAL``（批准进行**人工试验**）的语义。
"""

from __future__ import annotations

from typing import Final

from ai_psi.domain.enums import ApprovalLevel, ErrorType, ProposalStatus
from ai_psi.domain.improvement_proposals import (
    PROPOSAL_ESCALATION_THRESHOLD,
    ImprovementProposal,
)
from ai_psi.learning.pattern_detector import ErrorPattern
from ai_psi.learning.promotion_policy import PromotionDecision, PromotionTrigger

__all__ = ["ProposalGenerator", "triggered_by"]

#: 错误类别 → 最可能出问题的组件。
#:
#: ⚠️ **这是"往哪看"的线索，不是诊断结论。**
#: 它由错误类别推出，而错误类别本身来自若干确定性判据——
#: 两者都不足以断定"问题就在这个组件里"。提案的措辞因此用
#: "疑似"而不是"就是"，评审时也应当把它当作起点而非结论。
_TARGET_COMPONENT: Final[dict[ErrorType, str]] = {
    ErrorType.CONCEPTUAL_ERROR: "prompt:inquiry_framer",
    ErrorType.SCOPE_ERROR: "prompt:inquiry_framer",
    ErrorType.EVIDENCE_ERROR: "module:context_builder",
    ErrorType.FACTUAL_ERROR: "module:context_builder",
    ErrorType.REASONING_ERROR: "prompt:logical_analyzer",
    ErrorType.CALIBRATION_ERROR: "module:metacognition",
    ErrorType.VALUE_SUBSTITUTION: "prompt:judgment_synthesizer",
    ErrorType.USER_MODEL_ERROR: "module:user_model",
    ErrorType.EXPRESSION_ERROR: "prompt:response_renderer",
    ErrorType.PROCESS_ERROR: "module:orchestrator",
    ErrorType.MEMORY_ERROR: "policy:memory_write",
    ErrorType.UNKNOWN_ERROR: "unattributed",
}

#: 错误类别 → 观察到的问题的描述模板。
_OBSERVED_PROBLEM: Final[dict[ErrorType, str]] = {
    ErrorType.CONCEPTUAL_ERROR: "多次把问题的性质判断错了，导致后续分析建立在错误的框定上",
    ErrorType.SCOPE_ERROR: "多次发生问题范围漂移：回答的不是被问的那件事",
    ErrorType.EVIDENCE_ERROR: "多次在证据不足或证据未被正确纳入的情况下给出结论",
    ErrorType.FACTUAL_ERROR: "多次出现事实层面的错误",
    ErrorType.REASONING_ERROR: "多次在推理过程中忽略关键反例或受确认偏差影响",
    ErrorType.CALIBRATION_ERROR: "多次在没有依据的情况下给出确定性表述",
    ErrorType.VALUE_SUBSTITUTION: "多次把价值选择当作事实判断来回答",
    ErrorType.USER_MODEL_ERROR: "多次对用户的理解出现偏差",
    ErrorType.EXPRESSION_ERROR: "多次在表达层面偏离内部判断",
    ErrorType.PROCESS_ERROR: "多次因流程未跑完而没能给出应有的结论",
    ErrorType.MEMORY_ERROR: "多次试图写入不该写入的记忆",
    ErrorType.UNKNOWN_ERROR: (
        "用户反复给出否定反馈，但**未能归因到具体错误类别**——"
        "确定性判据给不出类别，需要人工调查这些纠正是否指向某条共同的规则缺陷"
    ),
}


class ProposalGenerator:
    """由模式生成提案草案。"""

    def generate(
        self,
        *,
        pattern: ErrorPattern | None,
        decision: PromotionDecision,
        fix_direction: str | None = None,
        created_by: str = "proposal_generator",
    ) -> ImprovementProposal | None:
        """生成一条提案草案。

        🔴 **本层会重新核对裁决与证据是否自洽，不盲信 ``decision``。**

        初版只检查 ``decision.allowed`` 就直接产出提案，于是
        一个手工构造的 ``PromotionDecision(allowed=True)`` 加上
        一条经验的模式就能生成提案——"第二道保险"只是一句转发。
        现在多两问：**裁决列出来的触发条件，证据撑得起吗？**

        Args:
            pattern: 达到门槛的模式。``None`` 表示这次裁决不是由模式
                触发的（例如"离线评测暴露稳定退化"）——**那种情况下
                没有支撑证据，也就构造不出提案**，返回 ``None``。
            decision: 门槛裁决。未获准许时返回 ``None``。
            fix_direction: 严重错误的修复方向（若有）。
            created_by: 产生该提案的组件。

        Returns:
            提案（``status=DRAFT``）；无法构造时返回 ``None``。

        Raises:
            ValueError: 裁决与证据不自洽（见下）。
        """
        if not decision.allowed or not decision.triggers:
            return None

        if pattern is None:
            # "离线评测退化"这类触发条件不来自某个错误模式，
            # 因此没有支撑经验——而提案的核心就是它引用的证据。
            # 没有证据的提案既不能被评估，也不该占用评审的时间。
            return None

        if (
            PromotionTrigger.REPEATED_SAME_ERROR in decision.triggers
            and pattern.weighted_count < PROPOSAL_ESCALATION_THRESHOLD
        ):
            # 🔴 这是被**伪造**或**篡改**的裁决，不是运行时状态。
            # 静默返回 None 会让它看起来像"没什么可生成的"，
            # 而真相是有人绕过了门槛——那必须响。
            msg = (
                f"裁决声称命中「同类错误重复出现」，但模式只支持 "
                f"加权计数 {pattern.weighted_count}（门槛 {PROPOSAL_ESCALATION_THRESHOLD}，"
                f"发生 {pattern.count} 次、评价 "
                f"{'、'.join(item.value for item in pattern.evaluations)}）。"
                "这不是运行时状态，而是被构造出来的裁决——"
                "不变量 10 不允许从它产出提案"
            )
            raise ValueError(msg)

        error_type = pattern.error_type
        component = _TARGET_COMPONENT[error_type]

        return ImprovementProposal(
            created_by=created_by,
            target_component=component,
            observed_problem=_OBSERVED_PROBLEM[error_type],
            error_class=error_type,
            supporting_experience_ids=list(pattern.experience_ids),
            # 反例原样带进来。🔴 不在这里过滤掉它们——
            # 只带支持证据的提案，评审看到的是一份被裁剪过的事实。
            counterexamples=(
                [f"该情境下另有 {pattern.counterexample_count} 条支撑经验带有反例"]
                if pattern.counterexample_count
                else []
            ),
            proposed_change=self._proposed_change(
                error_type=error_type, component=component, fix_direction=fix_direction
            ),
            expected_benefit=(
                f"降低「{pattern.situation_signature}」情境下 {error_type.value} 的复发率"
            ),
            possible_regressions=[
                "针对该情境的改动可能让**其他情境**下的表现变差——"
                "只改善目标指标却让无关场景退化的改动是净负面改动",
                "过度收紧该情境的判断可能让系统变得不必要的保守",
            ],
            applicability=[pattern.situation_signature],
            evaluation_plan=[
                "在历史回放上对比 Baseline（当前策略）与 Candidate（改动后策略）",
                "目标指标：该情境下同类错误的复发率",
                "对照指标：其他情境的退化程度（任务书 §11.4 明确要求）",
                "阶段 7 交付完整评测后，本计划应替换为具体的 Golden Cases",
            ],
            success_metrics=[
                f"目标：{pattern.situation_signature} 情境下 {error_type.value} 出现次数下降",
                # 🔴 §5.12 要求成功指标里**必须包含目标以外的场景**
                "对照：整体回合完成率不得下降",
                "对照：其他情境的同类错误不得上升",
                "对照：平均模型调用数不得显著上升（避免用更多算力换指标）",
            ],
            rollback_conditions=[
                "任一对照指标出现退化即回滚",
                "目标情境的错误率未见下降但调用成本上升即回滚",
            ],
            approval_level=ApprovalLevel.USER_AND_REVIEW,
            status=ProposalStatus.DRAFT,
        )

    def _proposed_change(
        self,
        *,
        error_type: ErrorType,
        component: str,
        fix_direction: str | None,
    ) -> str:
        """构造"建议的改动"的描述。

        🔴 措辞刻意停在**方向**上，不给具体改法。
        具体改法需要看到本提案引用的那几条经验才能定，
        而那正是人工评审这一步存在的意义。
        """
        if error_type is ErrorType.UNKNOWN_ERROR:
            return (
                f"【待人工调查】目标组件无法从错误类别推出（{component}）。"
                "建议先核查支撑经验对应的回合：用户的纠正是否指向同一类问题、"
                "是否集中在某个模块或某个深度档位。"
                "在查明之前，任何改动方案都是无根据的"
            )
        if fix_direction:
            return f"针对 {component}：{fix_direction}"
        return (
            f"【方向性建议】检查 {component} 在该情境下的行为，"
            "判断是否存在可以收紧或补充的规则；具体改法需结合支撑经验确定"
        )


def triggered_by(decision: PromotionDecision) -> tuple[str, ...]:
    """返回裁决命中的触发条件名。

    供写入 ``improvement_proposal.created`` 事件的负载——
    "这条提案为什么会出现"必须能只查事件就回答出来，
    而不是回头去重跑一遍裁决（那时依赖的数据可能已经变了）。
    """
    return tuple(trigger.value for trigger in decision.triggers)
