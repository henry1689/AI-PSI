"""元认知与防反刍（任务书 §9.11、§13.3）。

任务书要求"规则优先、模型补充"。本模块把这个顺序实现得很死：

```
规则层算出客观指标 → 模型层做偏差自检 → 规则层裁决最终决策
```

🔴 **模型只能"提议"。** 没有新证据、没有新推理路径、重复度超阈值时，
规则直接强制 ``STOP``，并把停止原因记为
``NO_MARGINAL_COGNITIVE_GAIN``——模型提议什么都不算（不变量 8）。

这不是不信任模型，而是：**"该不该继续"恰恰是最容易被模型答错的问题**。
一个倾向于继续思考的模型总能找到"还有值得探索的地方"的理由，
而反刍的特征正是"每一轮看起来都还有事可做"。

🔴 **元认知不能调用自身。** :class:`Reflection` 不持有任何可再次触发
元认知的引用，本模块也只做一次检查，不做递归。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from ai_psi.cognition.base import ModuleOutcome, invocation_context
from ai_psi.cognition.state_machine import targets_for_decision
from ai_psi.domain.enums import (
    ConfidenceBand,
    MetacognitiveDecision,
    OrdinalLevel,
    RoundState,
)
from ai_psi.domain.events import ModelInvocationInfo
from ai_psi.domain.exceptions import InvariantViolationError
from ai_psi.domain.inquiries import Inquiry
from ai_psi.domain.judgments import Judgment
from ai_psi.domain.reflections import Reflection
from ai_psi.prompts.schemas import MetacognitionInput, MetacognitionOutput
from ai_psi.providers.gateway import ModelGateway
from ai_psi.reliability.budgets import BudgetTracker
from ai_psi.reliability.repetition_detector import repeated_claim_score

__all__ = [
    "POST_LOOP_TAIL_CALLS",
    "Metacognition",
    "MetacognitionRequest",
    "MetacognitiveReview",
    "RuleDecision",
    "StopReason",
    "decide",
    "resolve_next_state",
]

#: 一次额外认知循环的模型调用代价：重新合成判断 + 再次元认知复核。
#:
#: ANALYZING 阶段的分析器**不重跑**——那是 ``CHANGE_METHOD`` 的路径，
#: 不是默认路径。
EXTRA_LOOP_MODEL_CALLS: Final[int] = 2

#: 额外一轮循环之后仍然必须留出的调用数：**回答渲染**。
#:
#: 为什么是 1 而不是 2：判断已经由这轮循环自己产出了，
#: 尾部只剩渲染。把它写成 2 会让每一轮可负担的循环都少一次，
#: 从而把"能继续"误判成"必须停止"。
POST_LOOP_TAIL_CALLS: Final[int] = 1


class StopReason(StrEnum):
    """回合停止原因。

    这些字符串会写进 :attr:`CognitiveRound.stop_reason`，
    并成为"所有完成回合都能回答为什么停下来"（不变量 19）的实际答案。
    """

    DIRECT_ANSWER = "DIRECT_ANSWER"
    """D0 直答：深度决定不进入元认知，检索到答案即停。"""

    INQUIRY_STOP_CONDITION_SATISFIED = "INQUIRY_STOP_CONDITION_SATISFIED"
    """认知问题自己声明的停止条件已满足。"""

    NO_MARGINAL_COGNITIVE_GAIN = "NO_MARGINAL_COGNITIVE_GAIN"
    """🔴 反刍停止：无新证据、无新推理路径、重复度超阈值（§13.3 指定的字面量）。"""

    METACOGNITIVE_LOOP_LIMIT = "METACOGNITIVE_LOOP_LIMIT"
    """元认知循环轮数已达上限。"""

    BUDGET_CONSTRAINT = "BUDGET_CONSTRAINT"
    """预算不足以再跑一轮——这是**正常路径**，不是失败。"""

    SCOPE_DRIFT_DETECTED = "SCOPE_DRIFT_DETECTED"
    """检测到问题范围漂移：原问题被悄悄换成了另一个问题。"""

    LOWERED_CONFIDENCE = "LOWERED_CONFIDENCE"
    """元认知要求降低置信度后直接进入合成。"""

    MODEL_DECISION_STOP = "MODEL_DECISION_STOP"
    """模型层提议停止，且规则层没有更强的理由。"""

    AWAITING_EVIDENCE = "AWAITING_EVIDENCE"
    """进入等待状态，不是终态。"""

    ESCALATED_TO_RESEARCH = "ESCALATED_TO_RESEARCH"
    """超出当前能力，挂起等待更高能力的处理。"""

    RULE_LAYER_STOP = "RULE_LAYER_STOP"
    """规则层独立完成复核并决定停止（未调用模型层）。"""

    NO_CONCERN_DETECTED = "NO_CONCERN_DETECTED"
    """🔴 **这不是失败**：关切检测的输出为空，说明这条消息不值得启动认知。
    任务书 §9.1 明确允许零个关切（§9.1 的输出是"零到多个 Concern"）。"""


@dataclass(frozen=True, slots=True)
class MetacognitionRequest:
    """一次元认知复核的输入。

    Attributes:
        cognitive_round_id: 所属回合。
        inquiry: 当前认知问题。
        judgment: 当前判断。
        loop_index: 本次是该回合的第几次复核（从 0 开始）。
        stop_condition_reached: 认知问题的停止条件是否已满足。
        new_evidence_present: 相比上一轮是否出现新证据。
        new_reasoning_path_present: 相比上一轮是否出现新推理路径。
        scope_drift_detected: 是否检测到范围漂移。
        previous_judgment_conclusion: 上一轮判断的结论文本，用于重复度计算。
        user_message_summary: 用户消息摘要。
    """

    cognitive_round_id: UUID
    inquiry: Inquiry
    judgment: Judgment
    loop_index: int
    stop_condition_reached: bool
    new_evidence_present: bool
    new_reasoning_path_present: bool
    scope_drift_detected: bool = False
    previous_judgment_conclusion: str | None = None
    user_message_summary: str = ""


@dataclass(frozen=True, slots=True)
class RuleDecision:
    """规则层的裁决。

    Attributes:
        decision: 最终决策。
        stop_reason: 停止原因；未停止时为 ``None``。
        forced: 规则是否覆盖了模型的提议。
        reasons: 裁决理由。
    """

    decision: MetacognitiveDecision
    stop_reason: str | None
    forced: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MetacognitiveReview:
    """一次元认知复核的完整结果。

    Attributes:
        reflection: 领域反思对象（含规则指标与模型层检查）。
        decision: 最终决策。
        stop_reason: 停止原因；未停止时为 ``None``。
        forced: 规则是否覆盖了模型提议。
        next_state: 该决策对应的下一个回合状态。
        invocations: 模型调用记录。
    """

    reflection: Reflection
    decision: MetacognitiveDecision
    stop_reason: str | None
    forced: bool
    next_state: RoundState
    invocations: tuple[ModelInvocationInfo, ...] = ()

    @property
    def is_terminal_decision(self) -> bool:
        """该决策是否通向终态（停止或挂起）。"""
        return self.next_state.is_terminal


def decide(
    *,
    repeated_claim_score_value: float,
    new_evidence_present: bool,
    new_reasoning_path_present: bool,
    scope_drift_detected: bool,
    stop_condition_reached: bool,
    loops_remaining: int,
    can_afford_extra_loop: bool,
    proposed: MetacognitiveDecision,
    repeat_threshold: float,
) -> RuleDecision:
    """规则层裁决（**纯函数**，可独立测试）。

    判定顺序即优先级。``NO_MARGINAL_COGNITIVE_GAIN`` 排在最前，
    因为反刍是最难被"再想一轮"发现的问题——
    等循环次数用尽才发现，已经浪费了预算，而且已经产出了冗余分析。

    Args:
        repeated_claim_score_value: 与上一轮判断的重复度。
        new_evidence_present: 是否有新证据。
        new_reasoning_path_present: 是否有新推理路径。
        scope_drift_detected: 是否检测到范围漂移。
        stop_condition_reached: 认知问题声明的停止条件是否满足。
        loops_remaining: 剩余元认知循环轮数。
        can_afford_extra_loop: 预算是否撑得起再跑一轮。
        proposed: 模型提议的决策。
        repeat_threshold: 重复度阈值（来自配置）。

    Returns:
        规则裁决。
    """
    continuing = {
        MetacognitiveDecision.CONTINUE,
        MetacognitiveDecision.CHANGE_METHOD,
        MetacognitiveDecision.NARROW_SCOPE,
    }

    # 🔴 §13.3 的反刍硬规则：没有新证据、没有新路径、且重复度超阈值
    if (
        not new_evidence_present
        and not new_reasoning_path_present
        and repeated_claim_score_value >= repeat_threshold
    ):
        return RuleDecision(
            decision=MetacognitiveDecision.STOP,
            stop_reason=StopReason.NO_MARGINAL_COGNITIVE_GAIN.value,
            forced=True,
            reasons=(
                (
                    f"与上一轮判断的重复度为 {repeated_claim_score_value:.2f}，"
                    f"超过阈值 {repeat_threshold:.2f}；"
                    "且没有新证据、没有新的推理路径——继续不会带来新东西"
                ),
            ),
        )

    if scope_drift_detected:
        return RuleDecision(
            decision=MetacognitiveDecision.STOP,
            stop_reason=StopReason.SCOPE_DRIFT_DETECTED.value,
            forced=True,
            reasons=("检测到问题范围漂移：当前分析已经偏离原始问题",),
        )

    if stop_condition_reached and proposed in continuing:
        return RuleDecision(
            decision=MetacognitiveDecision.STOP,
            stop_reason=StopReason.INQUIRY_STOP_CONDITION_SATISFIED.value,
            forced=True,
            reasons=("认知问题声明的停止条件已满足，无需继续分析",),
        )

    if proposed in continuing and loops_remaining <= 0:
        return RuleDecision(
            decision=MetacognitiveDecision.STOP,
            stop_reason=StopReason.METACOGNITIVE_LOOP_LIMIT.value,
            forced=True,
            reasons=("元认知循环轮数已达上限，强制进入合成（ADR-0008）",),
        )

    if proposed in continuing and not can_afford_extra_loop:
        return RuleDecision(
            decision=MetacognitiveDecision.STOP,
            stop_reason=StopReason.BUDGET_CONSTRAINT.value,
            forced=True,
            reasons=("剩余预算不足以支撑再一轮分析，强制进入合成",),
        )

    stop_reason = _stop_reason_for(proposed)
    return RuleDecision(
        decision=proposed,
        stop_reason=stop_reason,
        forced=False,
        reasons=("模型层提议被采纳，规则层未发现更强的理由",),
    )


def _stop_reason_for(decision: MetacognitiveDecision) -> str | None:
    """把一个非 CONTINUE 的决策映射到停止原因。"""
    mapping = {
        MetacognitiveDecision.STOP: StopReason.MODEL_DECISION_STOP.value,
        MetacognitiveDecision.LOWER_CONFIDENCE: StopReason.LOWERED_CONFIDENCE.value,
        MetacognitiveDecision.WAIT: StopReason.AWAITING_EVIDENCE.value,
        MetacognitiveDecision.ESCALATE_TO_RESEARCH: StopReason.ESCALATED_TO_RESEARCH.value,
        # REQUEST_EVIDENCE 回到 RETRIEVING，回合尚未结束——没有停止原因
        MetacognitiveDecision.REQUEST_EVIDENCE: None,
    }
    return mapping.get(decision)


def resolve_next_state(decision: MetacognitiveDecision) -> RoundState:
    """把元认知决策映射到下一个回合状态。

    ``CONTINUE`` 指向 ``DELIBERATING`` 而不是 ``ANALYZING``：
    默认路径是"用现有材料重新合成判断"，而不是"把所有分析重跑一遍"——
    后者会瞬间吃掉整个预算。换方法重跑是 ``CHANGE_METHOD`` 的语义。

    Args:
        decision: 元认知决策。

    Returns:
        下一个状态。

    Raises:
        InvariantViolationError: 该决策的目标状态不在权威映射表中。
    """
    targets = targets_for_decision(decision)
    if not targets:  # pragma: no cover - 映射表不会为空
        msg = f"元认知决策 {decision.value!r} 没有合法的目标状态"
        raise InvariantViolationError(msg, invariant_id="I17")

    if decision is MetacognitiveDecision.CONTINUE and RoundState.DELIBERATING in targets:
        return RoundState.DELIBERATING
    if RoundState.SYNTHESIZING in targets:
        return RoundState.SYNTHESIZING
    # 其余决策的目标集合都是单元素，取确定值
    return sorted(targets, key=lambda state: state.value)[0]


class Metacognition:
    """规则优先、模型补充的元认知检查器。"""

    def __init__(self, gateway: ModelGateway, *, repeat_threshold: float) -> None:
        """初始化。

        Args:
            gateway: 模型调用网关。
            repeat_threshold: 重复度阈值。**属于配置，不硬编码在这里**（ADR-0008）。
        """
        self._gateway = gateway
        self._repeat_threshold = repeat_threshold

    @property
    def repeat_threshold(self) -> float:
        """当前的重复度阈值。"""
        return self._repeat_threshold

    async def review(
        self,
        request: MetacognitionRequest,
        *,
        budget: BudgetTracker,
        use_model: bool = True,
        rule_only_stop_reason: str | None = None,
        user_id: UUID | None = None,
        conversation_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> ModuleOutcome[MetacognitiveReview]:
        """执行一次元认知复核。

        Args:
            request: 复核输入。
            budget: 预算记账器。
            use_model: 是否调用模型层。``False`` 时**只跑规则层**——
                D0 直答不需要判断"该不该继续想"，
                预算不足时也不该再花一次调用去问它。
                两种情况都需要留下"为什么停下来"的决策记录（不变量 19），
                且都不消耗任何模型调用。
            rule_only_stop_reason: ``use_model=False`` 时的停止原因。
                **必须由调用方给出**——"为什么停"是上下文相关的：
                D0 是深度使然，预算不足是资源使然，两者不该混用同一个原因。
            user_id: 归属用户。
            conversation_id: 所属会话。
            correlation_id: 关联链标识。

        Returns:
            复核结果与调用记录。``use_model=False`` 时调用记录为空。
        """
        score = repeated_claim_score(
            request.previous_judgment_conclusion,
            request.judgment.conclusion,
        )
        loops_remaining = budget.remaining_metacognitive_loops
        can_afford_extra_loop = budget.can_afford(
            EXTRA_LOOP_MODEL_CALLS + POST_LOOP_TAIL_CALLS,
            keep_tail_reserve=False,
        )

        payload = MetacognitionInput(
            question=request.inquiry.question,
            judgment_conclusion=request.judgment.conclusion,
            judgment_confidence=request.judgment.confidence_band,
            unresolved_unknowns=list(request.judgment.unresolved_unknowns),
            loop_index=request.loop_index,
            loops_remaining=max(0, loops_remaining),
            model_calls_remaining=max(0, budget.remaining_model_calls),
            new_evidence_present=request.new_evidence_present,
            new_reasoning_path_present=request.new_reasoning_path_present,
            repeated_claim_score=score,
            stop_conditions_satisfied=request.stop_condition_reached,
            user_message_summary=request.user_message_summary,
        )
        invocations: tuple[ModelInvocationInfo, ...] = ()
        if use_model:
            call = await self._gateway.structured(
                task_name="metacognition",
                payload=payload,
                response_model=MetacognitionOutput,
                context=invocation_context(
                    cognitive_round_id=request.cognitive_round_id,
                    conversation_id=conversation_id,
                    user_id=user_id,
                    correlation_id=correlation_id,
                ),
            )
            model_output = call.value
            invocations = (call.invocation,)
        else:
            model_output = _rule_layer_only_output()

        rule = decide(
            repeated_claim_score_value=score,
            new_evidence_present=request.new_evidence_present,
            new_reasoning_path_present=request.new_reasoning_path_present,
            scope_drift_detected=request.scope_drift_detected,
            stop_condition_reached=request.stop_condition_reached,
            loops_remaining=loops_remaining,
            can_afford_extra_loop=can_afford_extra_loop,
            proposed=model_output.proposed_decision,
            repeat_threshold=self._repeat_threshold,
        )

        next_state = resolve_next_state(rule.decision)
        # 规则层独跑时，"模型决定停止"这个说法不成立——
        # 停止是深度决定的（D0 不做模型层复核），原因必须如实写明。
        stop_reason = (
            rule.stop_reason
            if use_model
            else (rule_only_stop_reason or StopReason.RULE_LAYER_STOP.value)
        )
        # 🔴 强制停止时，边际价值必须如实记为「没有价值」——
        # 否则回放一份"决定停止但边际价值中等"的反思记录，
        # 会让人以为是系统随机停下来的。
        marginal_value = (
            OrdinalLevel.VERY_LOW
            if rule.forced and rule.decision is MetacognitiveDecision.STOP
            else model_output.marginal_value
        )

        reflection = Reflection(
            created_by="metacognition",
            cognitive_round_id=request.cognitive_round_id,
            new_evidence_present=request.new_evidence_present,
            new_reasoning_path_present=request.new_reasoning_path_present,
            repeated_claim_score=score,
            scope_drift_detected=request.scope_drift_detected,
            confirmation_bias_risk=model_output.confirmation_bias_risk,
            user_pleasing_bias_risk=model_output.user_pleasing_bias_risk,
            abstraction_escape_risk=model_output.abstraction_escape_risk,
            unsupported_certainty_detected=model_output.unsupported_certainty_detected,
            missing_counterexample_detected=model_output.missing_counterexample_detected,
            stop_condition_reached=request.stop_condition_reached,
            marginal_value=marginal_value,
            decision=rule.decision,
            reasons=[*rule.reasons, *model_output.reasons],
        )

        review = MetacognitiveReview(
            reflection=reflection,
            decision=rule.decision,
            stop_reason=stop_reason,
            forced=rule.forced,
            next_state=next_state,
            invocations=invocations,
        )
        notes: tuple[str, ...] = ()
        if not use_model:
            notes = ("未启用模型层复核（该深度不做元认知模型调用）",)
        elif rule.forced:
            notes = (f"规则层覆盖模型提议：{rule.stop_reason}",)
        return ModuleOutcome(value=review, invocations=invocations, notes=notes)


def _rule_layer_only_output() -> MetacognitionOutput:
    """构造"模型层未运行"时的占位输出。

    🔴 **它不是伪造的模型输出，而是显式的"没跑过模型"。**
    六项偏差自检全部落在最低档、``proposed_decision`` 为 ``STOP``——
    真正的裁决由规则层做出，模型层这一格如实记为未评估。
    """
    return MetacognitionOutput(
        marginal_value=OrdinalLevel.VERY_LOW,
        proposed_decision=MetacognitiveDecision.STOP,
        reasons=["规则层独立判定：该深度不启用模型层元认知复核"],
    )


def stop_condition_reached(judgment: Judgment) -> bool:
    """判断认知问题的停止条件是否已满足。

    V0.1 的判定是结构性的：**没有未解决的未知，且置信度不低于中等**。
    认知问题自带的 ``stop_conditions`` 是自由文本，代码无法可靠地求值；
    因此这里只使用可从 :class:`~ai_psi.domain.judgments.Judgment`
    直接读出的结构信号，把自由文本留给元认知的模型层去理解。

    Args:
        judgment: 当前判断。

    Returns:
        已满足返回 ``True``。
    """
    return (
        not judgment.unresolved_unknowns
        and judgment.confidence_band.rank >= ConfidenceBand.MODERATE.rank
    )
