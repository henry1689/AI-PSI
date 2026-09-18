"""AI-PSI 领域枚举。

本模块是全部领域对象的词汇表。所有分类字段一律使用枚举白名单，
**不做模糊匹配**——模型返回非法枚举值时应当解析失败，
而不是被"猜"成最接近的成员（把 ``MECHANISTIC`` 猜成 ``MECHANICAL``
会让一个错误静默通过，失败比猜测安全）。

枚举值一律使用小写蛇形字符串，便于 JSON 序列化与日志阅读。
"""

from __future__ import annotations

from enum import StrEnum

# 按字母序排列（ruff RUF022）。文件正文按语义分组，此处以可检索优先。
__all__ = [
    "ActorType",
    "ApprovalLevel",
    "AssumptionNecessity",
    "AssumptionStatus",
    "BeliefStatus",
    "BeliefType",
    "CognitiveDepth",
    "ConcernCategory",
    "ConcernStatus",
    "ConfidenceBand",
    "EpistemicAction",
    "EpistemicClassification",
    "ErrorType",
    "EvaluationVerdict",
    "EventType",
    "EvidenceDirectness",
    "ExpectedOutputType",
    "ExperienceEvaluation",
    "ExperienceEvaluator",
    "ExperienceKind",
    "FeedbackType",
    "HypothesisCategory",
    "HypothesisStatus",
    "InquiryStatus",
    "MemoryStatus",
    "MemoryType",
    "MetacognitiveDecision",
    "OrdinalLevel",
    "ProposalStatus",
    "RetentionPolicy",
    "RoundState",
    "SensitivityLevel",
    "SourceType",
    "Testability",
    "TrustLevel",
    "UncertaintyType",
    "UserModelStatus",
    "VerificationStatus",
]

# ---------------------------------------------------------------------------
# 通用量纲
# ---------------------------------------------------------------------------

_ORDINAL_ORDER: tuple[str, ...] = ("very_low", "low", "moderate", "high", "very_high")


class OrdinalLevel(StrEnum):
    """有序程度量。用于影响、紧迫、风险、边际价值等主观强度。"""

    VERY_LOW = "very_low"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    VERY_HIGH = "very_high"

    @property
    def rank(self) -> int:
        """返回 0–4 的序数位置，供单调性比较与阈值判断使用。"""
        return _ORDINAL_ORDER.index(self.value)

    def at_least(self, other: OrdinalLevel) -> bool:
        """本级别是否不低于 ``other``。"""
        return self.rank >= other.rank


class ConfidenceBand(StrEnum):
    """置信度分档。

    刻意使用分档而非百分比：模型不应伪造精确概率。
    内部规则可计算出档位，但**模型不能直接输出百分比**。
    """

    VERY_LOW = "very_low"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    VERY_HIGH = "very_high"

    @property
    def rank(self) -> int:
        """返回 0–4 的序数位置。

        用于不变量 7（最终回答的结论强度不得高于内部判断的结论强度）:
        回答语气的档位必须 ``<=`` Judgment 的置信档位。
        """
        return _ORDINAL_ORDER.index(self.value)

    def at_least(self, other: ConfidenceBand) -> bool:
        return self.rank >= other.rank


class TrustLevel(StrEnum):
    """信息来源的可信程度。外部检索内容一律不得为 VERIFIED。"""

    UNTRUSTED = "untrusted"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERIFIED = "verified"


class SensitivityLevel(StrEnum):
    """数据敏感度。写入记忆前必须分类（任务书 §17.1）。"""

    PUBLIC = "public"
    INTERNAL = "internal"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"
    HIGHLY_SENSITIVE = "highly_sensitive"

    @property
    def requires_user_confirmation(self) -> bool:
        """SENSITIVE 及以上不可自动批准写入记忆（ADR-0004）。"""
        return self in {SensitivityLevel.SENSITIVE, SensitivityLevel.HIGHLY_SENSITIVE}


class VerificationStatus(StrEnum):
    """核验状态。

    🔴 不变量 4：用户赞同**不能**把状态改为 VERIFIED。
    该状态只能由证据变更驱动，反馈路径没有任何入口能设置它。
    """

    UNVERIFIED = "unverified"
    SELF_REPORTED = "self_reported"
    THIRD_PARTY = "third_party"
    VERIFIED = "verified"
    DISPUTED = "disputed"
    REFUTED = "refuted"


class EvidenceDirectness(StrEnum):
    """证据与结论之间的直接程度。"""

    DIRECT = "direct"
    INDIRECT = "indirect"
    INFERRED = "inferred"
    HEARSAY = "hearsay"


class UncertaintyType(StrEnum):
    """不确定性的类型——比"有多不确定"更重要。

    区分这一点可以避免把"信息不足"误判为"推理错误"。
    """

    ALETHIC = "alethic"
    """证据不足：事实本身可判定，但材料不够。"""

    EPISTEMIC = "epistemic"
    """知识缺失：不清楚，且暂时无法弄清楚。"""

    LINGUISTIC = "linguistic"
    """表述歧义：问题本身的措辞有歧义。"""

    ONTOLOGICAL = "ontological"
    """概念边界不清：范畴本身没有清晰定义。"""

    NORMATIVE = "normative"
    """价值不可由事实决定：事实无法推出的价值选择。"""


class ActorType(StrEnum):
    """行为主体类型。"""

    USER = "user"
    SYSTEM = "system"
    MODEL = "model"
    EXTERNAL = "external"


# ---------------------------------------------------------------------------
# 输入与观察
# ---------------------------------------------------------------------------


class SourceType(StrEnum):
    """观察的来源类型。"""

    USER_MESSAGE = "user_message"
    EXTERNAL_DOC = "external_doc"
    SYSTEM_EVENT = "system_event"
    TOOL_RESULT = "tool_result"


class FeedbackType(StrEnum):
    """用户反馈的类型。

    CORRECTION 走版本链路径（生成新记忆版本）；
    其余类型只影响经验记录，**不得**改变任何事实的核验状态。
    """

    ACKNOWLEDGEMENT = "acknowledgement"
    AGREEMENT = "agreement"
    DISAGREEMENT = "disagreement"
    CORRECTION = "correction"
    CLARIFICATION = "clarification"
    RATING = "rating"


# ---------------------------------------------------------------------------
# 关切与问题
# ---------------------------------------------------------------------------


class ConcernCategory(StrEnum):
    """认知关切的类别（任务书 §5.4）。"""

    USER_REQUEST = "user_request"
    KNOWLEDGE_GAP = "knowledge_gap"
    CONFLICT = "conflict"
    PREDICTION_ERROR = "prediction_error"
    CONCEPTUAL_AMBIGUITY = "conceptual_ambiguity"
    LONG_TERM_GOAL = "long_term_goal"
    RELATIONSHIP_BOUNDARY = "relationship_boundary"
    EXPLORATION = "exploration"
    SYSTEM_RELIABILITY = "system_reliability"


class ConcernStatus(StrEnum):
    """关切的生命周期状态。"""

    OPEN = "open"
    ADDRESSED = "addressed"
    DEFERRED = "deferred"
    EXPIRED = "expired"
    DISMISSED = "dismissed"


class ExpectedOutputType(StrEnum):
    """认知任务的期望产物类型。"""

    DIRECT_ANSWER = "direct_answer"
    EXPLANATION = "explanation"
    COMPARISON = "comparison"
    RECOMMENDATION = "recommendation"
    CLARIFICATION_REQUEST = "clarification_request"
    EVIDENCE_REQUEST = "evidence_request"
    STRUCTURED_ANALYSIS = "structured_analysis"
    ACKNOWLEDGEMENT = "acknowledgement"


class InquiryStatus(StrEnum):
    """认知问题的状态。"""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    ANSWERED = "answered"
    DEFERRED = "deferred"
    UNRESOLVED = "unresolved"
    CLOSED = "closed"


# ---------------------------------------------------------------------------
# 概念与假设
# ---------------------------------------------------------------------------


class AssumptionNecessity(StrEnum):
    """前提的必要性——它若是错的，结论是否还成立。"""

    CRITICAL = "critical"
    IMPORTANT = "important"
    INCIDENTAL = "incidental"


class Testability(StrEnum):
    """前提的可检验性。"""

    TESTABLE_NOW = "testable_now"
    TESTABLE_LATER = "testable_later"
    UNTESTABLE = "untestable"
    UNFALSIFIABLE = "unfalsifiable"


class AssumptionStatus(StrEnum):
    """前提的状态。"""

    ACTIVE = "active"
    CONFIRMED = "confirmed"
    CHALLENGED = "challenged"
    REJECTED = "rejected"


class HypothesisCategory(StrEnum):
    """假设的类别（ADR-0012 新增；任务书 §5.8 原为自由字符串）。

    🔴 任务书 §9.6 要求：高风险或高深度问题的候选假设中，
    **至少包含一种非人格化、非心理化解释**。
    该约束通过 :attr:`is_non_agentic` 在代码中可判定。
    """

    FACTUAL = "factual"
    """关于客观事实的假设。"""

    MECHANISTIC = "mechanistic"
    """机制性解释：某事因某个可描述的过程而发生。"""

    CONTEXTUAL = "contextual"
    """情境性解释：由处境、时机、外部条件导致。"""

    INTENTIONAL = "intentional"
    """意图性解释：涉及某方的目的或考虑（可能指向人）。"""

    NON_AGENTIC = "non_agentic"
    """非人格化解释：明确不诉诸任何主体的心理状态。"""

    ALTERNATIVE = "alternative"
    """替代性解释：与主假设竞争的其他可能。"""

    SYSTEMIC = "systemic"
    """结构性/系统性解释：由制度、流程、环境结构导致。"""

    @property
    def is_non_agentic(self) -> bool:
        """该类别是否构成"非人格化、非心理化解释"。

        用于满足任务书 §9.6 与场景 B（不对第三方做心理定论）的要求。
        """
        return self in {
            HypothesisCategory.NON_AGENTIC,
            HypothesisCategory.MECHANISTIC,
            HypothesisCategory.SYSTEMIC,
            HypothesisCategory.CONTEXTUAL,
        }


class HypothesisStatus(StrEnum):
    """假设的状态。

    🔴 不变量 1：**不存在从假设直接跃迁到"已确认事实"的路径。**
    假设只能经证据评估后进入 SUPPORTED / REJECTED / UNRESOLVED，
    而 SUPPORTED 仍不是"事实"——它只表示"当前证据支持"。
    """

    CANDIDATE = "candidate"
    UNDER_EVALUATION = "under_evaluation"
    SUPPORTED = "supported"
    REJECTED = "rejected"
    UNRESOLVED = "unresolved"
    SUPERSEDED = "superseded"


# ---------------------------------------------------------------------------
# 信念与判断
# ---------------------------------------------------------------------------


class BeliefType(StrEnum):
    """信念的类型。"""

    FACTUAL = "factual"
    INTERPRETIVE = "interpretive"
    PREFERENCE = "preference"
    NORMATIVE = "normative"
    PREDICTIVE = "predictive"


class BeliefStatus(StrEnum):
    """信念的状态。

    🔴 不变量 2：Belief 必须有依据（``confidence_basis`` 非空）
    或明确标记为 TENTATIVE。
    """

    TENTATIVE = "tentative"
    ACTIVE = "active"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


class EpistemicAction(StrEnum):
    """对外呈现的认知处境与建议（ADR-0010）。

    回答的问题是：**"就这个问题而言，我们现在处于什么认知状态、
    建议用户怎么办？"** 它是结果性的，会传达给用户。

    与 :class:`MetacognitiveDecision` 的区别见 ADR-0010。
    """

    ANSWER = "answer"
    """可以给出结论。"""

    ANSWER_WITH_CAVEAT = "answer_with_caveat"
    """可以回答，但必须带明确限定。"""

    REQUEST_EVIDENCE = "request_evidence"
    """缺关键证据，建议补充材料。"""

    WAIT = "wait"
    """已发出检索请求，等待结果。"""

    DEFER = "defer"
    """当前能力或资料不足，暂不判断。"""

    OUT_OF_SCOPE = "out_of_scope"
    """事实无法决定的价值选择，超出认知系统职责。

    🔴 这不是失败，而是诚实的认知结论（任务书 §9.10、§9.9：
    "不得假装价值冲突存在唯一科学答案"）。
    """

    @property
    def allows_strong_conclusion(self) -> bool:
        """是否允许输出强结论。

        用于不变量 7 的实现：只有 ANSWER 允许无保留的确定表述。
        """
        return self is EpistemicAction.ANSWER


class EpistemicClassification(StrEnum):
    """上下文条目的认知分类（任务书 §9.4）。

    🔴 输出不得只给置信度，必须说明依据类型。
    """

    OBSERVED = "observed"
    SUPPORTED = "supported"
    TENTATIVE = "tentative"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"
    POSSIBLY_OUTDATED = "possibly_outdated"
    INACCESSIBLE = "inaccessible"
    OUT_OF_CAPABILITY = "out_of_capability"


# ---------------------------------------------------------------------------
# 元认知
# ---------------------------------------------------------------------------


class MetacognitiveDecision(StrEnum):
    """回合的内部控制流决策（ADR-0010）。

    回答的问题是：**"这个认知回合接下来怎么走？"**
    直接驱动状态机转移（见 ``docs/state_machine.md`` §2.2）。
    它是过程性的，用户通常看不到。
    """

    CONTINUE = "continue"
    CHANGE_METHOD = "change_method"
    NARROW_SCOPE = "narrow_scope"
    LOWER_CONFIDENCE = "lower_confidence"
    REQUEST_EVIDENCE = "request_evidence"
    WAIT = "wait"
    STOP = "stop"
    ESCALATE_TO_RESEARCH = "escalate_to_research"


# ---------------------------------------------------------------------------
# 记忆
# ---------------------------------------------------------------------------


class MemoryType(StrEnum):
    """长期记忆的类型（任务书 §5.11）。"""

    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    USER_CONFIRMED_FACT = "user_confirmed_fact"
    USER_PREFERENCE = "user_preference"
    USER_GOAL = "user_goal"
    CONCEPTUAL = "conceptual"
    STRATEGY = "strategy"
    FAILURE_CASE = "failure_case"
    SELF_MODEL = "self_model"


class MemoryStatus(StrEnum):
    """记忆的状态。

    🔴 不变量 6：``SUPERSEDED`` / ``DELETED`` / ``EXPIRED``
    **不得作为默认有效记忆返回**（见 :meth:`is_default_retrievable`）。
    """

    PROPOSED = "proposed"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DISPUTED = "disputed"
    EXPIRED = "expired"
    DELETED = "deleted"
    REJECTED = "rejected"

    @property
    def is_default_retrievable(self) -> bool:
        """是否为默认检索可见状态。

        只有 ACTIVE 与 DISPUTED 可见——DISPUTED 仍可见是因为
        "存在争议"本身是有价值的信息，但它会在回答中标注为有争议。
        """
        return self in {MemoryStatus.ACTIVE, MemoryStatus.DISPUTED}


class RetentionPolicy(StrEnum):
    """记忆的保留策略。"""

    PERMANENT = "permanent"
    LONG_TERM = "long_term"
    SESSION = "session"
    UNTIL_SUPERSEDED = "until_superseded"
    USER_CONTROLLED = "user_controlled"


# ---------------------------------------------------------------------------
# 经验与提案
# ---------------------------------------------------------------------------


class ErrorType(StrEnum):
    """错误分类（任务书 §11.2）。

    ``VALUE_SUBSTITUTION``（把价值偏好当作事实）与
    ``CALIBRATION_ERROR``（置信度失准）是本系统特别关注的类型——
    它们最容易被普通聊天系统忽略。
    """

    FACTUAL_ERROR = "factual_error"
    REASONING_ERROR = "reasoning_error"
    CONCEPTUAL_ERROR = "conceptual_error"
    EVIDENCE_ERROR = "evidence_error"
    CALIBRATION_ERROR = "calibration_error"
    SCOPE_ERROR = "scope_error"
    VALUE_SUBSTITUTION = "value_substitution"
    USER_MODEL_ERROR = "user_model_error"
    EXPRESSION_ERROR = "expression_error"
    PROCESS_ERROR = "process_error"
    MEMORY_ERROR = "memory_error"
    UNKNOWN_ERROR = "unknown_error"


class ExperienceEvaluation(StrEnum):
    """一条经验的**错误归因**被确认到什么程度（阶段 6.5 §二.3）。

    🔴 **这个枚举存在的理由：内部元认知不是证据。**

    在它出现之前，``Experience.error_type`` 这个字段同时承担了两件
    完全不同的事：一是"系统的元认知模块怀疑这里出了问题"，
    二是"这里确实出了问题"。两者被写进同一个字段、按同一个权重
    计入提案门槛——于是**系统自己的怀疑可以自我确认成规律**：
    元认知怀疑三次，就有了一个"已被三次独立观察支持"的提案。

    §二.4 因此划了一条硬线：**内部元认知最多产生 ``SUSPECTED``。**
    ``SUPPORTED`` 与 ``CONFIRMED`` 只能来自经验之外的证据
    （用户明确纠正、可靠后续证据、独立评测）。

    四档的语义边界：

    * ``UNASSESSED``——没有任何评估者对它下过判断。系统连怀疑都没有，
      只是把它记下来了。**它不计入门槛。**
    * ``SUSPECTED``——内部元认知怀疑有错。这是一个**假设**，
      不是一条证据。**默认不计入门槛**（§二.7 要求不得与
      ``SUPPORTED``/``CONFIRMED`` 等权——默认权重取 0 是最强的不等权）。
    * ``SUPPORTED``——有独立于内部元认知的证据支持：
      用户明确纠正、或判断之后出现的新证据。
    * ``CONFIRMED``——用户直接指出这是错的，或独立评测确认。
      这是最高一档，它只可能来自**系统之外**。

    ⚠️ ``rank`` 只表达**次序**，不表达**权重**。权重是门槛策略
    （见 :mod:`ai_psi.learning.promotion_policy`），会随策略变化；
    而次序是语义，不该变。
    """

    UNASSESSED = "unassessed"
    SUSPECTED = "suspected"
    SUPPORTED = "supported"
    CONFIRMED = "confirmed"

    @property
    def rank(self) -> int:
        """次序（0 = 最低）。只用于比较，不用于加权。"""
        return _EXPERIENCE_EVALUATION_RANK[self]


_EXPERIENCE_EVALUATION_RANK: dict[ExperienceEvaluation, int] = {
    ExperienceEvaluation.UNASSESSED: 0,
    ExperienceEvaluation.SUSPECTED: 1,
    ExperienceEvaluation.SUPPORTED: 2,
    ExperienceEvaluation.CONFIRMED: 3,
}


class ExperienceEvaluator(StrEnum):
    """**谁**判定了一条经验的评价状态（阶段 6.5 §二.8）。

    🔴 **知道了"是什么状态"还不够，必须知道"谁说的"。**

    ``CONFIRMED`` 来自"用户明确说这是错的"与来自"内部模块自己这么认为"，
    是完全不同的两件事——哪怕它们最终都是错误类型的事实描述。
    没有这一栏，一次策略放宽（比如允许内部元认知产生 ``SUPPORTED``）
    会让**历史上所有**的印象分记录在一夜之间改变含义，
    而且没有任何痕迹说明它们当时是谁判的。

    ⚠️ 这四者之间**没有**"哪个更权威"的偏序：``USER_CORRECTION``
    说"用户认为错了"，``LATER_EVIDENCE`` 说"后面出现了反证"，
    它们是**不同类型的依据**，不是同一个尺子上的刻度。
    把它们排成序会让"用户说的"和"证据显示的"在需要区分时无法区分
    （不变量 4：用户赞同不能把事实改为已验证）。
    """

    INTERNAL_METACOGNITION = "internal_metacognition"
    """内部元认知模块（反思、置信度检查、反刍信号）。**最多产生 SUSPECTED。**"""

    LATER_EVIDENCE = "later_evidence"
    """判断**之后**才出现的证据。"""

    USER_CORRECTION = "user_correction"
    """用户明确表示了否定（纠正 / 不同意）。"""

    INDEPENDENT_EVALUATION = "independent_evaluation"
    """独立于本回合的评测（阶段 7 的 Golden Dataset 对照等）。"""


class ExperienceKind(StrEnum):
    """经验的种类，参与 ``canonical_key`` 的构成（阶段 6.5 §二.10）。

    ⚠️ V0.1 只有一种。保留它是因为 ``canonical_key`` 必须能区分
    "同一个回合里、针对同一对象、但属于不同种类"的经验——
    少了这一维，将来新增一种经验就会与既有的撞键，
    而撞键的表现是"新经验被唯一约束静默拒绝"，不是报错。
    """

    ROUND_OUTCOME = "round_outcome"
    """一次认知回合的结果复盘。"""


class ApprovalLevel(StrEnum):
    """提案所需的审批级别。"""

    NONE = "none"
    SYSTEM = "system"
    USER = "user"
    USER_AND_REVIEW = "user_and_review"


class ProposalStatus(StrEnum):
    """改进提案的状态（任务书 §5.12）。

    🔴 **不存在 ACTIVE，也不得有通往"生效"的路径。**
    状态机在此终结——``APPROVED_FOR_MANUAL_TRIAL`` 的语义是
    "批准进行**人工试验**"，不是"上线"。

    这是不变量 11（ImprovementProposal 不能自动生效）的**类型级保证**：
    只要类型里根本不存在 ACTIVE，就不可能有代码把它设进去。
    """

    DRAFT = "draft"
    PENDING_EVALUATION = "pending_evaluation"
    EVALUATED = "evaluated"
    REJECTED = "rejected"
    APPROVED_FOR_MANUAL_TRIAL = "approved_for_manual_trial"

    @property
    def is_terminal(self) -> bool:
        """REJECTED 与 APPROVED_FOR_MANUAL_TRIAL 是终态。

        V0.1 没有任何从终态继续演进的路径。
        """
        return self in {
            ProposalStatus.REJECTED,
            ProposalStatus.APPROVED_FOR_MANUAL_TRIAL,
        }


class EvaluationVerdict(StrEnum):
    """一次离线评估的结论。

    🔴 **``INCONCLUSIVE`` 是一个必须存在的选项。**

    没有它，评估者只能在"有效"与"无效"之间二选一——
    而"样本太少，看不出差别"是最常见、也最诚实的结果。
    逼它变成其中一个，等于把噪声变成结论。
    """

    IMPROVED = "improved"
    """目标指标改善，且对照指标未退化。"""

    NO_CHANGE = "no_change"
    """没有可观测的变化。"""

    REGRESSED = "regressed"
    """出现退化（任务书 §11.3 第三条触发条件所指的那一种）。"""

    INCONCLUSIVE = "inconclusive"
    """数据不足以得出结论。**不是"没差"**，是"看不出"。"""


# ---------------------------------------------------------------------------
# 用户模型
# ---------------------------------------------------------------------------


class UserModelStatus(StrEnum):
    """用户模型条目的状态。

    🔴 不变量 13：**不存在 CONFIRMED**——用户模型中的推测
    不得标记为确认事实。

    🔴 任务书 §2.3：禁止根据少量对话生成稳定人格诊断。
    """

    HYPOTHESIZED = "hypothesized"
    OBSERVED_PATTERN = "observed_pattern"
    USER_STATED = "user_stated"
    STALE = "stale"
    RETRACTED = "retracted"


# ---------------------------------------------------------------------------
# 认知回合
# ---------------------------------------------------------------------------


class CognitiveDepth(StrEnum):
    """认知深度等级（任务书 §7.1）。

    预算与模块启用矩阵见 ADR-0008。
    """

    D0 = "d0"
    """直接回答：理解、检索、表达。"""

    D1 = "d1"
    """基础分析：已知未知、简单逻辑、判断。"""

    D2 = "d2"
    """多假设分析：假设、反证、替代解释。"""

    D3 = "d3"
    """系统与价值分析：概念、视角、长期影响、价值冲突。"""

    D4 = "d4"
    """哲理与元框架分析：世界观、认识边界、主体性、意义。"""

    @property
    def level(self) -> int:
        """返回 0–4 的数值等级。"""
        return int(self.value[1])

    def at_least(self, other: CognitiveDepth) -> bool:
        return self.level >= other.level


class RoundState(StrEnum):
    """认知回合状态（任务书 §6.1）。

    转移表见 ``docs/state_machine.md`` §2，实现于
    :mod:`ai_psi.cognition.state_machine`。
    """

    CREATED = "created"
    TRIAGING = "triaging"
    FRAMING = "framing"
    RETRIEVING = "retrieving"
    ANALYZING = "analyzing"
    DELIBERATING = "deliberating"
    METACOGNITIVE_REVIEW = "metacognitive_review"
    SYNTHESIZING = "synthesizing"
    RESPONDING = "responding"
    COMPLETED = "completed"
    WAITING_FOR_EVIDENCE = "waiting_for_evidence"
    SUSPENDED = "suspended"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """终态：不可再转移。重新激活需要创建新回合并以 causation_id 关联。"""
        return self in {
            RoundState.COMPLETED,
            RoundState.SUSPENDED,
            RoundState.FAILED,
            RoundState.CANCELLED,
        }


# ---------------------------------------------------------------------------
# 事件
# ---------------------------------------------------------------------------


class EventType(StrEnum):
    """事件类型全集，共 35 种。

    事件只追加，永不修改、永不删除（ADR-0002）。

    ⚠️ **任务书 §5.2 的清单与状态机、模块清单并不一致，已补四条：**

    * ``cognitive_round.cancelled``（ADR-0012）：任务书清单里没有，
      但 §6.1 的状态机存在 ``CANCELLED`` 状态。缺了它，
      取消一个回合将不留下任何审计轨迹。
    * ``cognition.analysis.completed``（ADR-0015）：任务书 §9 描述了
      逻辑/因果/辩证/哲理四个分析模块，§5.2 的清单里却没有任何一条
      事件与之对应。缺了它，这些模块的模型调用就**没有地方记录**
      ``model`` 与 ``prompt_version``——直接违反不变量 18。
    * ``improvement_proposal.approved_for_manual_trial`` 与
      ``improvement_proposal.rejected``（ADR-0018）：§12.4 要求
      "批准进行人工试验"与"驳回"两个接口，§5.2 却只给了
      ``created`` 与 ``evaluated``。缺了它们，**人类做出的那个批准
      决定本身不会留下任何痕迹**——而整条受控迭代链路存在的理由，
      正是这个决定要可追溯（不变量 11）。

    ``tests/unit/test_enums.py`` 断言每个终态都有对应的事件类型。
    """

    # 用户输入
    USER_MESSAGE_RECEIVED = "user.message.received"
    USER_FEEDBACK_RECEIVED = "user.feedback.received"
    USER_CORRECTION_RECEIVED = "user.correction.received"

    # 认知产物
    OBSERVATION_CREATED = "observation.created"
    CONCERN_CREATED = "concern.created"
    INQUIRY_CREATED = "inquiry.created"
    EVIDENCE_ATTACHED = "evidence.attached"
    CONCEPT_IDENTIFIED = "concept.identified"
    ASSUMPTION_IDENTIFIED = "assumption.identified"
    HYPOTHESIS_CREATED = "hypothesis.created"
    HYPOTHESIS_EVALUATED = "hypothesis.evaluated"
    #: 任务书 §5.2 清单中缺失，为覆盖 §9 的分析模块而补齐（ADR-0015）。
    COGNITION_ANALYSIS_COMPLETED = "cognition.analysis.completed"

    # 信念
    BELIEF_CREATED = "belief.created"
    BELIEF_REVISED = "belief.revised"
    BELIEF_SUPERSEDED = "belief.superseded"

    # 判断与元认知
    JUDGMENT_CREATED = "judgment.created"
    METACOGNITION_COMPLETED = "metacognition.completed"

    # 回答
    RESPONSE_GENERATED = "response.generated"
    RESPONSE_DELIVERED = "response.delivered"

    # 记忆
    MEMORY_PROPOSED = "memory.proposed"
    MEMORY_APPROVED = "memory.approved"
    MEMORY_REJECTED = "memory.rejected"
    MEMORY_EXPIRED = "memory.expired"
    MEMORY_CORRECTED = "memory.corrected"
    #: 阶段 5 新增。导出同样是**数据访问**，必须有痕迹——
    #: 任务书 §17.1 要求"所有导出和删除操作留存不含正文的审计事件"。
    #: 与删除不同：删除是让内容消失，导出是把内容交出去，
    #: 两者都改变不了"曾经发生过"这个事实。
    MEMORY_EXPORTED = "memory.exported"

    # 经验与提案
    EXPERIENCE_CREATED = "experience.created"
    #: 阶段 6.5 §二 新增（ADR-0020）。**经验本身不可变，评价会变。**
    #:
    #: 一条经验在写入时只可能拿到"内部元认知"这一种评估者，
    #: 而用户纠正、后续证据都在那之后才到。要表达"这条经验后来
    #: 被用户确认了"，只有两条路：改写经验（违反不可变），
    #: 或者追加一条评价事件（本事件）。
    #:
    #: 🔴 缺了它，"三次被支持的同类错误"这条验收条件就无法成立——
    #: 生产里能产出的经验永远停在 ``SUSPECTED``。
    EXPERIENCE_EVALUATED = "experience.evaluated"
    IMPROVEMENT_PROPOSAL_CREATED = "improvement_proposal.created"
    IMPROVEMENT_PROPOSAL_EVALUATED = "improvement_proposal.evaluated"
    #: 任务书 §5.2 清单中缺失（ADR-0018）。
    #: 🔴 这是**人类**做出的决定。名字里的 ``for_manual_trial``
    #: 不是修饰：批准的是"做一次人工试验"，不是"上线"（不变量 11）。
    IMPROVEMENT_PROPOSAL_APPROVED = "improvement_proposal.approved_for_manual_trial"
    IMPROVEMENT_PROPOSAL_REJECTED = "improvement_proposal.rejected"

    # 回合生命周期
    COGNITIVE_ROUND_STARTED = "cognitive_round.started"
    COGNITIVE_ROUND_STATE_CHANGED = "cognitive_round.state_changed"
    COGNITIVE_ROUND_COMPLETED = "cognitive_round.completed"
    COGNITIVE_ROUND_SUSPENDED = "cognitive_round.suspended"
    COGNITIVE_ROUND_FAILED = "cognitive_round.failed"
    #: 任务书 §5.2 清单中缺失，为覆盖 CANCELLED 状态而补齐（ADR-0012）。
    COGNITIVE_ROUND_CANCELLED = "cognitive_round.cancelled"
