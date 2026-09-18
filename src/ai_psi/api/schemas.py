"""API Schema（任务书 §12）。

🔴 **与领域对象分离**（ADR-0006）。
:class:`~ai_psi.domain.judgments.Judgment` 是内部判断，
``JudgmentView`` 是"允许客户端看到的判断"——两者之间的差异
（例如不暴露``selected_hypothesis_ids``）必须是一个**显式决定**，
而不是"顺手把领域对象序列化出去"的副作用。

🔴 **摘要接口不返回完整隐藏思维链**（任务书 §12.1 的注）。
返回的是**结构化理由**：结论文本、理由条目、反证、未知、适用条件。
提示词与模型原始响应**永远不出现在这里**——模型调用记录只暴露
元信息（模型名、Prompt 版本、耗时、响应哈希）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ai_psi.application.feedback_service import MemoryEffect
from ai_psi.domain.enums import (
    ApprovalLevel,
    CognitiveDepth,
    ConfidenceBand,
    EpistemicAction,
    ErrorType,
    EvaluationVerdict,
    FeedbackType,
    MemoryStatus,
    MemoryType,
    ProposalStatus,
    RetentionPolicy,
    RoundState,
    SensitivityLevel,
    VerificationStatus,
)

__all__ = [
    "ApproveProposalRequest",
    "ConversationCreatedResponse",
    "CorrectMemoryRequest",
    "CorrectMemoryResponse",
    "DeleteMemoryResponse",
    "DimensionStatus",
    "EvaluateProposalRequest",
    "FeedbackRequest",
    "FeedbackResponse",
    "HealthResponse",
    "JudgmentView",
    "MemoryListResponse",
    "MemoryView",
    "ModelInvocationView",
    "ProposalListResponse",
    "ProposalTransitionResponse",
    "ProposalView",
    "ReflectionView",
    "RejectProposalRequest",
    "ReplayResponse",
    "RoundResponseBody",
    "RoundStatusResponse",
    "RoundSummaryResponse",
    "SubmitMessageRequest",
    "SubmitMessageResponse",
    "UserDataDeletionResponse",
    "UserDataExportResponse",
]

_STRICT = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# 会话与消息（§12.1）
# ---------------------------------------------------------------------------


class ConversationCreatedResponse(BaseModel):
    """创建会话的响应。

    ⚠️ V0.1 **不持久化会话实体**——它是一个客户端分组键，
    服务端只负责生成一个稳定的 UUID。

    🔴 阶段 3 这里曾写着"会话记录要随长期记忆一起在阶段 5 落地"。
    阶段 5 落地的是**长期记忆**，不是会话；而这个承诺留在了响应里，
    客户端会据此以为"会话历史在服务端存着"。**一个不会发生的承诺
    比没有承诺更糟**——它让调用方把数据放在了一个不存在的地方。
    """

    model_config = _STRICT

    conversation_id: UUID
    note: str = (
        "V0.1 会话为客户端分组键，服务端**不持久化**会话实体；"
        "会话历史由客户端保存，服务端只保证回合与记忆可追溯"
    )


class SubmitMessageRequest(BaseModel):
    """提交用户消息并启动认知回合。"""

    model_config = _STRICT

    user_id: UUID | None = Field(default=None, description="归属用户")
    content: str = Field(min_length=1, description="用户消息原文")
    requested_depth: CognitiveDepth | None = Field(
        default=None,
        description="用户显式请求的深度。🔴 仍受预算与规则约束（ADR-0008）",
    )
    response_style: str = Field(default="structured", min_length=1)
    allow_long_term_memory: bool = True


class SubmitMessageResponse(BaseModel):
    """提交消息的响应。

    ⚠️ **任务书 §12.1 的示例把 ``status`` 写成固定值 ``"CREATED"``。**
    V0.1 没有后台任务队列（那是阶段 4 之后的事），回合在请求内**同步执行完毕**，
    因此这里返回**真实终态**而不是一个必然已经过期的 ``"CREATED"``。
    （偏差登记：ADR-0015）
    """

    model_config = _STRICT

    message_id: UUID = Field(description="对应 ``user.message.received`` 事件 id")
    cognitive_round_id: UUID
    status: RoundState = Field(description="回合的**真实**终态")
    depth: CognitiveDepth
    stop_reason: str | None = None
    response: str | None = Field(
        default=None,
        description="最终回答。``None`` 表示本回合没有产生回答（如无值得处理的关切）",
    )


# ---------------------------------------------------------------------------
# 回合状态与摘要（§12.1）
# ---------------------------------------------------------------------------


class RoundStatusResponse(BaseModel):
    """回合的运行状态。"""

    model_config = _STRICT

    cognitive_round_id: UUID
    state: RoundState
    depth_level: CognitiveDepth
    stop_reason: str | None = None
    failure_stage: str | None = Field(default=None, description="不变量 20：失败回合必填")
    error_category: str | None = Field(default=None, description="不变量 20：失败回合必填")
    model_calls_used: int = Field(ge=0)
    max_model_calls: int = Field(ge=1)
    metacognitive_loops: int = Field(ge=0)
    max_metacognitive_loops: int = Field(ge=0)
    version: int = Field(ge=1)


class JudgmentView(BaseModel):
    """允许客户端看到的判断。"""

    model_config = _STRICT

    conclusion: str
    rationale_summary: list[str]
    strongest_counterarguments: list[str] = Field(default_factory=list)
    unresolved_unknowns: list[str] = Field(default_factory=list)
    applicability: list[str] = Field(default_factory=list)
    confidence_band: ConfidenceBand
    confidence_basis: list[str]
    revision_conditions: list[str] = Field(default_factory=list)
    recommended_epistemic_action: EpistemicAction
    uncertainty_type: str


class ReflectionView(BaseModel):
    """允许客户端看到的元认知反思（**结构化信号，不是思维流**）。"""

    model_config = _STRICT

    new_evidence_present: bool
    new_reasoning_path_present: bool
    repeated_claim_score: float = Field(ge=0.0, le=1.0)
    scope_drift_detected: bool
    confirmation_bias_risk: str
    user_pleasing_bias_risk: str
    abstraction_escape_risk: str
    unsupported_certainty_detected: bool
    missing_counterexample_detected: bool
    stop_condition_reached: bool
    marginal_value: str
    decision: str
    reasons: list[str]


class ModelInvocationView(BaseModel):
    """一次模型调用的**元信息**。

    🔴 不含响应内容，只有哈希（任务书 §8.2，认知宪法红线一）。
    """

    model_config = _STRICT

    invocation_id: UUID
    provider: str
    model: str
    task_name: str
    prompt_version: str
    latency_ms: int | None = None
    retry_count: int = Field(ge=0)
    result_status: str
    response_hash: str | None = None


class RoundSummaryResponse(BaseModel):
    """结构化认知摘要。

    🔴 **这是"可审计的理由"，不是"模型的推理过程"。**
    它回答"这个结论建立在什么之上、为什么停下来"，
    而不是"模型内部想了什么"。
    """

    model_config = _STRICT

    cognitive_round_id: UUID
    state: RoundState
    depth_level: CognitiveDepth
    stop_reason: str | None = None

    concerns: list[dict[str, Any]] = Field(default_factory=list)
    inquiry: dict[str, Any] | None = None
    hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    judgment: JudgmentView | None = None
    reflection: ReflectionView | None = None
    analyses: dict[str, Any] = Field(default_factory=dict)
    response_text: str | None = None

    adjustments: list[str] = Field(
        default_factory=list,
        description="系统对模型输出所做的改写，逐条留痕（如置信度下调、认知动作降级）",
    )
    skipped_steps: list[str] = Field(
        default_factory=list,
        description=(
            "**被跳过的分析步骤及原因**（预算不足 / 模型调用失败）。"
            "🔴 降级本身可以接受，但必须可见——"
            "否则事后无法分辨「少做了一个分析」与「分析跑了但没产出」"
        ),
    )
    model_invocations: list[ModelInvocationView] = Field(default_factory=list)


class RoundResponseBody(BaseModel):
    """最终回答。"""

    model_config = _STRICT

    cognitive_round_id: UUID
    state: RoundState
    text: str | None
    judgment: JudgmentView | None = None


# ---------------------------------------------------------------------------
# 回放与健康（§12.5）
# ---------------------------------------------------------------------------


class ReplayResponse(BaseModel):
    """历史回放结果。"""

    model_config = _STRICT

    cognitive_round_id: UUID
    state: RoundState
    transition_count: int = Field(ge=0)
    event_count: int = Field(ge=0)
    transitions: list[dict[str, Any]] = Field(default_factory=list)
    stop_reason: str | None = None
    failure_stage: str | None = None
    error_category: str | None = None
    projected_at: datetime


class DimensionStatus(BaseModel):
    """单项健康检查。"""

    model_config = _STRICT

    name: str
    ok: bool
    detail: str = ""


class HealthResponse(BaseModel):
    """健康检查响应。"""

    model_config = _STRICT

    status: str
    checks: list[DimensionStatus] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 判断与记忆（§12.3）
# ---------------------------------------------------------------------------


class MemoryView(BaseModel):
    """允许客户端看到的一条长期记忆。

    🔴 **这里返回的是正文。** 记忆是**用户自己的数据**，
    ``GET /users/{id}/memories`` 与导出接口存在的意义就是把它们交还给用户。
    需要区别对待的是**审计**：事件负载里永远不含正文
    （见 :mod:`ai_psi.memory.redaction` 与任务书 §10.5）。
    """

    model_config = _STRICT

    id: UUID
    user_id: UUID | None = None
    memory_type: MemoryType
    content: str
    status: MemoryStatus
    verification_status: VerificationStatus
    sensitivity: SensitivityLevel
    retention_policy: RetentionPolicy

    applicability: list[str] = Field(default_factory=list)
    valid_from: datetime
    valid_until: datetime | None = None

    supersedes_id: UUID | None = None
    contradicts_ids: list[UUID] = Field(default_factory=list)
    #: 本条与**同一批结果中**的其他记忆是否存在显式冲突关系。
    #: 冲突要被呈现，而不是被排序掩盖（任务书 §5.11）。
    conflicts_within_results: bool = False

    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=1)

    @property
    def default_retrievable(self) -> bool:
        """是否属于默认可检索状态（不变量 6）。"""
        return self.status.is_default_retrievable


class MemoryListResponse(BaseModel):
    """某用户的记忆列表。"""

    model_config = _STRICT

    user_id: UUID
    count: int = Field(ge=0)
    include_inactive: bool
    memories: list[MemoryView] = Field(default_factory=list)


class CorrectMemoryRequest(BaseModel):
    """用户纠正一条记忆。"""

    model_config = _STRICT

    user_id: UUID = Field(description="发起纠正的用户；必须与记忆的作用域一致")
    new_content: str = Field(min_length=1, description="新的内容")


class CorrectMemoryResponse(BaseModel):
    """纠正结果。

    🔴 **不变量 5**：旧记忆被取代，**不就地覆盖**。
    两个 id 都返回，客户端因此可以完整地展示版本链。
    """

    model_config = _STRICT

    corrected_memory_id: UUID
    replacement_memory_id: UUID
    superseded_status: MemoryStatus
    replacement_status: MemoryStatus
    audit_event_ids: list[UUID] = Field(default_factory=list)


class DeleteMemoryResponse(BaseModel):
    """删除结果。"""

    model_config = _STRICT

    memory_id: UUID
    status: MemoryStatus
    audit_event_id: UUID


class UserDataExportResponse(BaseModel):
    """用户数据导出（任务书 §10.5）。

    ``export`` 是完整的导出包，含**被取代与已删除的记忆**——
    "为什么发生过修正"靠版本链回答，只导出有效记忆会让纠错痕迹消失。
    """

    model_config = _STRICT

    user_id: UUID
    audit_event_id: UUID
    export: dict[str, Any]


class UserDataDeletionResponse(BaseModel):
    """用户数据删除结果。

    ⚠️ **只覆盖记忆。** 事件流、认知回合与判断是系统运行史，不在删除范围内；
    "事件只追加"是根本约束（ADR-0002）。这一点必须在文档里说清楚，
    不能靠调用方自己猜。
    """

    model_config = _STRICT

    user_id: UUID
    deleted_count: int = Field(ge=0)
    memory_ids: list[UUID] = Field(default_factory=list)
    audit_event_ids: list[UUID] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 反馈（任务书 §12.2）
# ---------------------------------------------------------------------------


class FeedbackRequest(BaseModel):
    """对某个认知回合的反馈。"""

    model_config = _STRICT

    feedback_type: FeedbackType = Field(description="反馈类型")
    content: str = Field(min_length=1, description="反馈正文")
    related_claim: str | None = Field(
        default=None,
        description="用户指出的、被纠正的具体说法",
    )
    allow_memory_update: bool = Field(
        default=False,
        description=(
            "是否允许本次反馈更新长期记忆。"
            "🔴 这是「允许提给写入流程」，不是「允许写入」——"
            "写入仍由记忆写入策略裁决（ADR-0004）"
        ),
    )


class FeedbackResponse(BaseModel):
    """反馈处理结果。

    🔴 ``memory_effect`` 必须回传，**包括"什么都没做"的理由**。

    用户打开了 ``allow_memory_update`` 却什么都没发生，
    与"记下来了"在响应里长得一样的话，调用方只能靠猜。
    """

    model_config = _STRICT

    round_id: UUID
    feedback_type: FeedbackType
    audit_event_id: UUID
    memory_effect: MemoryEffect = Field(
        description=(
            "对长期记忆的实际影响："
            "none（未请求）/ not_eligible（不满足条件）/ written / "
            "duplicate（已记过）/ rejected_by_policy"
        )
    )
    memory_id: UUID | None = Field(default=None, description="写入的记忆 id（若有）")
    memory_written: bool = Field(description="是否真的产生了新记忆")
    reasons: list[str] = Field(default_factory=list, description="包括「为什么没有写记忆」")


# ---------------------------------------------------------------------------
# 改进提案（任务书 §12.4）
# ---------------------------------------------------------------------------


class ProposalView(BaseModel):
    """一条改进提案的对外视图。

    🔴 **``status`` 永远不会是"已生效"。**

    这不需要靠约定：``ProposalStatus`` 里根本不存在 ``ACTIVE``
    （不变量 11）。``APPROVED_FOR_MANUAL_TRIAL`` 的语义是
    "批准进行**人工试验**"，不是上线。
    """

    model_config = _STRICT

    id: UUID
    status: ProposalStatus
    error_class: ErrorType
    target_component: str
    observed_problem: str

    supporting_experience_count: int = Field(
        ge=0,
        description="支撑经验条数。**数量决定它是否够格成为提案**（不变量 10）",
    )
    counterexamples: list[str] = Field(default_factory=list)

    proposed_change: str
    expected_benefit: str
    possible_regressions: list[str] = Field(default_factory=list)
    applicability: list[str] = Field(default_factory=list)
    evaluation_plan: list[str] = Field(default_factory=list)
    success_metrics: list[str] = Field(default_factory=list)
    rollback_conditions: list[str] = Field(default_factory=list)

    approval_level: ApprovalLevel
    can_become_active: bool = Field(
        description="🔴 恒为 false（不变量 11）。这个字段存在是为了让客户端**能检查它**"
    )
    is_terminal: bool

    created_at: datetime
    updated_at: datetime
    version: int = Field(ge=1)


class ProposalListResponse(BaseModel):
    """提案列表。"""

    model_config = _STRICT

    count: int = Field(ge=0)
    proposals: list[ProposalView] = Field(default_factory=list)


class EvaluateProposalRequest(BaseModel):
    """记录一次离线评估。"""

    model_config = _STRICT

    verdict: EvaluationVerdict = Field(
        description="评估结论。⚠️ inconclusive 是「看不出」而不是「没差」，它是必须存在的选项"
    )
    evidence: list[str] = Field(
        min_length=1,
        description=(
            "对照口径、样本量、参照版本。**不得为空**——"
            "「结论：改善」而没说跟什么比、比了多少个样本，"
            "是一条无法被复核、因而也无法被推翻的记录"
        ),
    )
    notes: str | None = Field(default=None, description="评审说明")
    actor_id: str = Field(default="reviewer", min_length=1)


class ProposalTransitionResponse(BaseModel):
    """一次状态流转的结果。

    🔴 两个事件 id 都不是可选的——状态变了却没有审批记录，
    正是这条链路最不能出的一类缺陷。
    """

    model_config = _STRICT

    proposal_id: UUID
    status: ProposalStatus
    version: int = Field(ge=1)
    audit_event_id: UUID
    can_become_active: bool = Field(description="🔴 恒为 false（不变量 11）")


class ApproveProposalRequest(BaseModel):
    """批准进行人工试验。"""

    model_config = _STRICT

    approved_by: str = Field(min_length=1, description="批准人标识")
    note: str | None = Field(default=None, description="批准说明")


class RejectProposalRequest(BaseModel):
    """驳回提案。"""

    model_config = _STRICT

    rejected_by: str = Field(min_length=1, description="驳回人标识")
    reason: str = Field(
        min_length=1,
        description="驳回理由。**必填**：「不想做」与「做不了」对后来者是完全不同的信息",
    )
