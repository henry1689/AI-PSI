"""Prompt 任务的输入/输出 Schema。

🔴 **这些 Schema 不是领域对象。** 它们描述的是"模型应当返回什么形状"，
与 :mod:`ai_psi.domain` 中的实体之间**只能显式映射**，不做一键互转
（ADR-0006 的三层模型规则）。

三条强制规则（`docs/prompt_contracts.md` §4）：

1. **全部 ``extra="forbid"``** —— 模型多返回的字段不被静默接受；
2. **分类字段一律用领域枚举** —— 非法值 → 解析失败，**不做模糊匹配**
   （把 ``MECHANISTIC`` 猜成 ``MECHANICAL`` 会让一个错误静默通过）；
3. **输出 Schema 中不存在 ``reasoning`` / ``thinking`` / ``chain_of_thought``
   之类的字段** —— 要的是结构化理由（``rationale_summary``），不是推理流（红线一）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ai_psi.domain.enums import (
    CognitiveDepth,
    ConcernCategory,
    ConfidenceBand,
    EpistemicAction,
    ExpectedOutputType,
    HypothesisCategory,
    MetacognitiveDecision,
    OrdinalLevel,
    UncertaintyType,
)

__all__ = [
    "CausalAnalysisOutput",
    "CausalAnalyzerInput",
    "ConceptAnalysisOutput",
    "ConceptAnalyzerInput",
    "ConceptDraft",
    "ConcernDetectorInput",
    "ConcernDetectorOutput",
    "ConcernDraft",
    "DepthSignals",
    "DialecticalAnalysisOutput",
    "DialecticalAnalyzerInput",
    "HypothesisDraft",
    "HypothesisGeneratorInput",
    "HypothesisGeneratorOutput",
    "InquiryDraft",
    "InquiryFramerInput",
    "InquiryFramerOutput",
    "JudgmentDraft",
    "JudgmentSynthesizerInput",
    "JudgmentSynthesizerOutput",
    "LogicalAnalysisOutput",
    "LogicalAnalyzerInput",
    "MetacognitionInput",
    "MetacognitionOutput",
    "PhilosophicalAnalysisOutput",
    "PhilosophicalAnalyzerInput",
    "ResponsePlan",
    "ResponseRendererInput",
]

_STRICT = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# 关切检测（任务书 §9.1）
# ---------------------------------------------------------------------------


class ConcernDetectorInput(BaseModel):
    """关切检测的输入。

    对应任务书 §9.1 的五项输入：当前事件、最近会话摘要、未完成问题、
    已确认用户目标、系统运行状态。
    """

    model_config = _STRICT

    user_message: str = Field(min_length=1)
    conversation_summary: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    confirmed_user_goals: list[str] = Field(default_factory=list)
    system_status: list[str] = Field(default_factory=list)


class ConcernDraft(BaseModel):
    """一个候选关切。

    ``should_start_round`` 为 ``False`` 的关切会被丢弃——
    任务书 §9.1 要求过滤"无依据的主动问题"与"纯粹为了表现聪明的探索"。
    """

    model_config = _STRICT

    statement: str = Field(min_length=1)
    why_it_matters: str = Field(min_length=1)
    category: ConcernCategory
    impact: OrdinalLevel = OrdinalLevel.MODERATE
    urgency: OrdinalLevel = OrdinalLevel.MODERATE
    uncertainty: OrdinalLevel = OrdinalLevel.MODERATE
    expected_information_value: OrdinalLevel = OrdinalLevel.MODERATE
    cognitive_cost: OrdinalLevel = OrdinalLevel.MODERATE
    should_start_round: bool = True


class ConcernDetectorOutput(BaseModel):
    """关切检测的输出。允许为空列表——"这件事不值得启动认知"是合法结论。"""

    model_config = _STRICT

    concerns: list[ConcernDraft] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 问题框定与深度信号（任务书 §9.2、§7.2）
# ---------------------------------------------------------------------------


class DepthSignals(BaseModel):
    """深度路由的**模型侧**信号（ADR-0008）。

    🔴 **模型在这里只能"提出建议"。** 最终深度由
    :func:`ai_psi.cognition.depth_router.route_depth` 依据确定性规则、
    用户显式请求与可用预算决定。模型把一切都标成 ``very_high``
    不会让系统自动升到 D4。
    """

    model_config = _STRICT

    simple_fact_with_sufficient_evidence: bool = False
    needs_explanation_or_comparison: bool = False
    multiple_plausible_interpretations: bool = False
    user_explicitly_philosophical: bool = False
    framework_conflict: bool = False

    estimated_impact: OrdinalLevel = OrdinalLevel.MODERATE
    ambiguity: OrdinalLevel = OrdinalLevel.MODERATE
    evidence_conflict: OrdinalLevel = OrdinalLevel.LOW
    value_conflict: OrdinalLevel = OrdinalLevel.LOW
    long_term_relevance: OrdinalLevel = OrdinalLevel.LOW


class InquiryFramerInput(BaseModel):
    """问题框定的输入。"""

    model_config = _STRICT

    concern_statement: str = Field(min_length=1)
    why_it_matters: str = Field(min_length=1)
    user_message: str = Field(min_length=1)
    known_observations: list[str] = Field(default_factory=list)
    current_beliefs: list[str] = Field(default_factory=list)


class InquiryDraft(BaseModel):
    """一个可结束的认知问题。

    ``out_of_scope`` 与 ``stop_conditions`` 是"可结束"的定义——
    缺少它们的问题会无限膨胀（任务书 §9.2）。
    """

    model_config = _STRICT

    question: str = Field(min_length=1)
    why_it_matters: str = Field(min_length=1)
    scope: list[str] = Field(min_length=1)
    out_of_scope: list[str] = Field(min_length=1)
    known_observations: list[str] = Field(default_factory=list)
    key_unknowns: list[str] = Field(default_factory=list)
    ambiguous_concepts: list[str] = Field(default_factory=list)
    assumptions_to_check: list[str] = Field(default_factory=list)
    expected_output_type: ExpectedOutputType = ExpectedOutputType.DIRECT_ANSWER
    verification_method: str | None = None
    stop_conditions: list[str] = Field(min_length=1)
    reopen_conditions: list[str] = Field(default_factory=list)
    depth_signals: DepthSignals = Field(default_factory=DepthSignals)


class InquiryFramerOutput(BaseModel):
    """问题框定的输出。"""

    model_config = _STRICT

    inquiry: InquiryDraft


# ---------------------------------------------------------------------------
# 假设生成（任务书 §9.6）
# ---------------------------------------------------------------------------


class HypothesisGeneratorInput(BaseModel):
    """假设生成的输入。"""

    model_config = _STRICT

    question: str = Field(min_length=1)
    scope: list[str] = Field(default_factory=list)
    key_unknowns: list[str] = Field(default_factory=list)
    evidence_summaries: list[str] = Field(default_factory=list)
    max_hypotheses: int = Field(default=4, ge=1)
    requires_non_agentic: bool = Field(
        default=True,
        description="是否至少需要一个非人格化、非心理化解释（§9.6，场景 B）",
    )


class HypothesisDraft(BaseModel):
    """一个候选假设。"""

    model_config = _STRICT

    statement: str = Field(min_length=1)
    category: HypothesisCategory = HypothesisCategory.FACTUAL
    predicted_observations: list[str] = Field(default_factory=list)
    falsification_conditions: list[str] = Field(
        min_length=1,
        description="**必填非空**——不可被反驳的命题不是假设，是信念宣告",
    )
    applicability: list[str] = Field(default_factory=list)
    uncertainty_type: UncertaintyType = UncertaintyType.ALETHIC


class HypothesisGeneratorOutput(BaseModel):
    """假设生成的输出。"""

    model_config = _STRICT

    hypotheses: list[HypothesisDraft] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 逻辑分析（任务书 §9.7）
# ---------------------------------------------------------------------------


class LogicalAnalyzerInput(BaseModel):
    """逻辑分析的输入。"""

    model_config = _STRICT

    question: str = Field(min_length=1)
    claims: list[str] = Field(default_factory=list)
    premises: list[str] = Field(default_factory=list)
    hypotheses: list[str] = Field(default_factory=list)


class LogicalAnalysisOutput(BaseModel):
    """逻辑分析的输出（字段取自任务书 §9.7 的结构定义）。"""

    model_config = _STRICT

    claims: list[str] = Field(default_factory=list)
    premises: list[str] = Field(default_factory=list)
    inference_types: list[str] = Field(default_factory=list)
    valid_links: list[str] = Field(default_factory=list)
    weak_links: list[str] = Field(default_factory=list)
    fallacy_risks: list[str] = Field(default_factory=list)
    counterexamples: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 因果分析
# ---------------------------------------------------------------------------


class CausalAnalyzerInput(BaseModel):
    """因果分析的输入。"""

    model_config = _STRICT

    question: str = Field(min_length=1)
    causal_claims: list[str] = Field(default_factory=list)


class CausalAnalysisOutput(BaseModel):
    """因果分析的输出。

    ``correlation_only`` 与 ``confounders`` 是核心：
    把相关当因果是本系统最需要拦住的推理错误之一（§9.7）。
    """

    model_config = _STRICT

    causal_claims: list[str] = Field(default_factory=list)
    proposed_mechanisms: list[str] = Field(default_factory=list)
    confounders: list[str] = Field(default_factory=list)
    alternative_causes: list[str] = Field(default_factory=list)
    evidence_for_causation: list[str] = Field(default_factory=list)
    correlation_only: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 概念分析（任务书 §9.5）
# ---------------------------------------------------------------------------


class ConceptAnalyzerInput(BaseModel):
    """概念分析的输入。"""

    model_config = _STRICT

    question: str = Field(min_length=1)
    concepts: list[str] = Field(default_factory=list)
    context: list[str] = Field(default_factory=list)


class ConceptDraft(BaseModel):
    """一个被显式定义的概念。"""

    model_config = _STRICT

    term: str = Field(min_length=1)
    working_definition: str = Field(min_length=1)
    alternative_definitions: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    ambiguity_notes: list[str] = Field(default_factory=list)
    context_scope: list[str] = Field(default_factory=list)


class ConceptAnalysisOutput(BaseModel):
    """概念分析的输出。"""

    model_config = _STRICT

    concepts: list[ConceptDraft] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    equivocation_risks: list[str] = Field(default_factory=list)
    false_dichotomy_risks: list[str] = Field(default_factory=list)
    descriptive_normative_confusions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 辩证分析（任务书 §9.8）
# ---------------------------------------------------------------------------


class DialecticalAnalyzerInput(BaseModel):
    """辩证分析的输入。"""

    model_config = _STRICT

    question: str = Field(min_length=1)
    position: str = Field(default="", description="当前主张；为空表示尚未形成主张")
    supporting_reasons: list[str] = Field(default_factory=list)
    value_conflicts: list[str] = Field(default_factory=list)


class DialecticalAnalysisOutput(BaseModel):
    """辩证分析的输出。

    🔴 **禁止机械地"双方都有道理"**（§9.8）。
    因此 ``irreducible_tension`` 与 ``conditional_synthesis`` 是
    **互斥的**：要么指出真实的、不可消除的张力，要么给出有条件的综合。
    两者皆空说明这次分析没有产生判断力。
    """

    model_config = _STRICT

    current_position: str = ""
    strongest_support: list[str] = Field(default_factory=list)
    strongest_opposition: list[str] = Field(default_factory=list)
    shared_premises: list[str] = Field(default_factory=list)
    scope_of_each_side: list[str] = Field(default_factory=list)
    irreducible_tension: str | None = None
    conditional_synthesis: str | None = None
    value_judgement_required: bool = Field(
        default=False,
        description="该问题是否必须由价值选择决定，事实无法给出唯一答案",
    )
    is_false_balance: bool = Field(
        default=False,
        description=(
            "分析本身是否退化成了机械折中。由分析者**自检**并如实标注——"
            "这不是评分，而是给下游元认知的输入"
        ),
    )


# ---------------------------------------------------------------------------
# 哲理分析（任务书 §9.9）
# ---------------------------------------------------------------------------


class PhilosophicalAnalyzerInput(BaseModel):
    """哲理分析的输入。"""

    model_config = _STRICT

    question: str = Field(min_length=1)
    value_conflicts: list[str] = Field(default_factory=list)
    factual_unknowns: list[str] = Field(
        default_factory=list,
        description=(
            "**事实层的未知**。🔴 不变量 9：哲理分析不得覆盖它们——"
            "用抽象语言绕开事实不足正是 §9.9 明令禁止的行为"
        ),
    )


class PhilosophicalAnalysisOutput(BaseModel):
    """哲理分析的输出（字段取自任务书 §9.9 的结构定义）。

    注意 ``alternative_frameworks`` 是字符串列表而非对象列表：
    任务书提到的 ``PhilosophicalFramework`` 在多处被引用却从未定义，
    已登记为 ADR-0012 的 G 系列缺口。
    """

    model_config = _STRICT

    central_question: str = Field(min_length=1)
    ontological_questions: list[str] = Field(default_factory=list)
    epistemological_questions: list[str] = Field(default_factory=list)
    value_questions: list[str] = Field(default_factory=list)
    agency_and_responsibility: list[str] = Field(default_factory=list)
    temporal_perspectives: list[str] = Field(default_factory=list)
    hidden_worldviews: list[str] = Field(default_factory=list)
    alternative_frameworks: list[str] = Field(default_factory=list)
    unresolved_tensions: list[str] = Field(default_factory=list)
    practical_implications: list[str] = Field(default_factory=list)
    epistemic_limits: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 判断合成（任务书 §9.10）
# ---------------------------------------------------------------------------


class JudgmentSynthesizerInput(BaseModel):
    """判断合成的输入。"""

    model_config = _STRICT

    question: str = Field(min_length=1)
    scope: list[str] = Field(default_factory=list)
    evidence_summaries: list[str] = Field(default_factory=list)
    hypothesis_summaries: list[str] = Field(default_factory=list)
    key_unknowns: list[str] = Field(default_factory=list)
    logical_analysis: LogicalAnalysisOutput | None = None
    causal_analysis: CausalAnalysisOutput | None = None
    dialectical_analysis: DialecticalAnalysisOutput | None = None
    philosophical_analysis: PhilosophicalAnalysisOutput | None = None
    max_confidence_band: ConfidenceBand = Field(
        default=ConfidenceBand.MODERATE,
        description=(
            "🔴 **由代码计算的置信上限**，不是建议。"
            "合成器不得给出高于此档位的置信度；"
            "违反会被 :mod:`ai_psi.cognition.confidence` 下调并记录"
        ),
    )


class JudgmentDraft(BaseModel):
    """一个暂定判断。"""

    model_config = _STRICT

    conclusion: str = Field(min_length=1)
    rationale_summary: list[str] = Field(min_length=1)
    strongest_counterarguments: list[str] = Field(default_factory=list)
    unresolved_unknowns: list[str] = Field(default_factory=list)
    applicability: list[str] = Field(default_factory=list)
    confidence_band: ConfidenceBand = ConfidenceBand.LOW
    confidence_basis: list[str] = Field(min_length=1)
    revision_conditions: list[str] = Field(default_factory=list)
    recommended_epistemic_action: EpistemicAction = EpistemicAction.ANSWER_WITH_CAVEAT
    uncertainty_type: UncertaintyType = UncertaintyType.ALETHIC


class JudgmentSynthesizerOutput(BaseModel):
    """判断合成的输出。"""

    model_config = _STRICT

    judgment: JudgmentDraft


# ---------------------------------------------------------------------------
# 元认知（任务书 §9.11）
# ---------------------------------------------------------------------------


class MetacognitionInput(BaseModel):
    """元认知的输入。

    🔴 **只包含规则层已经算好的指标与当前判断的摘要**，
    不包含完整的分析过程——元认知要检查的是"该不该继续"，
    不是重做一遍分析（那正是反刍的定义）。
    """

    model_config = _STRICT

    question: str = Field(min_length=1)
    judgment_conclusion: str = Field(default="")
    judgment_confidence: ConfidenceBand = ConfidenceBand.LOW
    unresolved_unknowns: list[str] = Field(default_factory=list)
    loop_index: int = Field(default=0, ge=0)
    loops_remaining: int = Field(default=0, ge=0)
    model_calls_remaining: int = Field(default=0, ge=0)
    new_evidence_present: bool = True
    new_reasoning_path_present: bool = True
    repeated_claim_score: float = Field(default=0.0, ge=0.0, le=1.0)
    stop_conditions_satisfied: bool = False
    user_message_summary: str = Field(default="")


class MetacognitionOutput(BaseModel):
    """元认知的模型层输出。

    🔴 **模型只能"提议"。** 最终决策由
    :func:`ai_psi.cognition.metacognition.decide` 在规则层裁决后给出——
    没有新证据、没有新路径、重复度超阈值时，规则直接强制 STOP，
    模型说什么都不算（§13.3，不变量 8）。
    """

    model_config = _STRICT

    confirmation_bias_risk: OrdinalLevel = OrdinalLevel.VERY_LOW
    user_pleasing_bias_risk: OrdinalLevel = OrdinalLevel.VERY_LOW
    abstraction_escape_risk: OrdinalLevel = OrdinalLevel.VERY_LOW
    unsupported_certainty_detected: bool = False
    missing_counterexample_detected: bool = False
    marginal_value: OrdinalLevel = OrdinalLevel.LOW
    proposed_decision: MetacognitiveDecision = MetacognitiveDecision.STOP
    reasons: list[str] = Field(min_length=1)


# ---------------------------------------------------------------------------
# 回答规划与渲染（任务书 §9.12）
# ---------------------------------------------------------------------------

#: 回答长度档位。
LengthHint = Literal["short", "medium", "long"]

#: 回答语气档位。
#:
#: 🔴 与 :attr:`~ai_psi.domain.enums.EpistemicAction.allows_strong_conclusion`
#: 联动：不允许强结论时**不得**使用 ``assertive``。
Tone = Literal["plain", "cautious", "tentative", "assertive"]


class ResponsePlan(BaseModel):
    """回答规划的结果（任务书 §9.12 的 Planner 输出）。

    ⚠️ **本对象由确定性代码生成，不是模型产物。**
    任务书 §4 的目录把它列在 Prompt 任务里，但它的每一项决策
    （该说什么、哪些不确定性要告诉用户、能否用确定语气、回答多长）
    都能从 :class:`~ai_psi.domain.judgments.Judgment` 与
    :class:`~ai_psi.domain.reflections.Reflection` 直接推出。
    让模型来决定"要不要表现得确定"，等于把不变量 7 交给被约束方自己执行。
    （偏差登记：ADR-0015）
    """

    model_config = _STRICT

    direct_answer_points: list[str] = Field(default_factory=list)
    uncertainties_to_surface: list[str] = Field(default_factory=list)
    alternative_explanations_to_show: list[str] = Field(default_factory=list)
    withheld_candidates: list[str] = Field(
        default_factory=list,
        description="**不应向用户表达**的内部候选。列出即表示主动隐藏，需可审计",
    )
    needs_clarification: bool = False
    clarification_question: str | None = None
    length_hint: LengthHint = "medium"
    depth_hint: CognitiveDepth = CognitiveDepth.D0
    tone: Tone = "plain"
    allows_strong_conclusion: bool = False
    response_style: str = "structured"


class ResponseRendererInput(BaseModel):
    """回答渲染的输入。"""

    model_config = _STRICT

    plan: ResponsePlan
    conclusion: str = Field(min_length=1)
    rationale_summary: list[str] = Field(default_factory=list)
    applicability: list[str] = Field(default_factory=list)
    confidence_band: ConfidenceBand = ConfidenceBand.LOW
    epistemic_action: EpistemicAction = EpistemicAction.ANSWER_WITH_CAVEAT
    response_style: str = "structured"
