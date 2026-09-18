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
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_psi.application.feedback_service import MemoryEffect
from ai_psi.domain.common import is_blank, unstorable_in
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
    "IDEMPOTENCY_KEY_MAX",
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

#: 请求体的配置：额外字段一律拒绝，**并且去掉字符串首尾空白**。
#:
#: 🔴 `min_length=1` 挡不住 `"   "`。
#:
#: 这是被真实攻击面验证过的一条：`content="   "` 过得了 `min_length=1`，
#: 然后在下游撞出一个 pydantic `ValidationError`（`Memory.content` 也是
#: `min_length=1`，但它在**去空白之后**为空）——那不是领域异常，
#: 最终表现为 **500**。用户输入造成的 500 一律是缺陷。
_STRICT_TRIMMED = ConfigDict(extra="forbid", str_strip_whitespace=True)

#: 事件表里 `actor_id` 列的长度。
#:
#: 请求 schema 接受的长度必须 ≤ 它，否则同一个请求在内存后端成功、
#: 在 PostgreSQL 后端因为 `value too long for type character varying(128)`
#: 炸成 500——**两个后端对同一个 HTTP 契约给出不同结果**。
_ACTOR_ID_MAX = 128

#: `Idempotency-Key` **请求头**的长度上限。
#:
#: 🔴 它是同一类缺陷的第二个实例，而 ADR-0021 §3 当时**漏掉了它**。
#:
#: 那一段把阶段 6 的 `actor_id` 事故写成了教训——"请求 schema 接受的
#: 长度必须 ≤ 数据库列宽"——然后只对**请求体字段**做了对齐。
#: 而幂等键走的是**请求头**：它不经任何 pydantic 模型，
#: 直接落进 `idempotency_keys.key` 与 `cognitive_rounds.idempotency_key`
#: 两个 `varchar(128)` 列。
#:
#: 阶段 6.5 §八 评审 C 实测（129 个字符）：
#: `memory` → **201**（整个回合跑完并落库），
#: `postgres` → **500**（`StringDataRightTruncation`）。
#: 边界确认：128 两边都 201，129 即分叉。
IDEMPOTENCY_KEY_MAX = 128


class _BoundaryRequestModel(BaseModel):
    """全部**请求体**模型的基类：边界上的文本纪律。

    🔴 **为什么是一个基类，而不是给每个字段各挂一个校验器。**

    本阶段先后发现了两个"两个后端两个结果"的实例
    （`actor_id` 超长、自由文本里的 `U+0000`），而它们都不是
    "某个字段忘了加约束"，是**同一类输入的每一个承载字段都漏了**。
    逐字段补校验器的做法在第一个新字段出现时就会重新漏掉——
    而漏掉的表现是"内存里跑得好好的，换 PostgreSQL 就 500"。

    基类把这条规则挂在**模型**上：新加一个 `str` 字段**自动**被覆盖，
    不需要任何人记得去挂校验器。

    ⚠️ 子类可以覆盖 `model_config`（本类只提供一个默认值），
    但**不能**取消这条校验——它是基类的 `model_validator`，
    继承即生效。
    """

    model_config = _STRICT_TRIMMED

    @model_validator(mode="after")
    def _reject_unstorable_text(self) -> Self:
        """把**存不进数据库**的字符挡在边界上（见 :func:`unstorable_in`）。

        拦的是 `U+0000`：PostgreSQL 对 text/varchar/jsonb 参数一律拒收，
        而 Python、JSON 与内存后端都接受。同一个请求因此会在两个后端上
        得到 201 与 500 两个不同结果。

        ⚠️ 逐字段遍历而不是只查"已知的那几个"：字段会被重命名、
        会有新的进来，而**漏掉一个就少挡一处 500**。遍历的代价是
        一次线性扫描，输入有长度上限兜着。
        """
        offenders = [
            f"{name}（{''.join(found)}）"
            for name in type(self).model_fields
            if (found := unstorable_in(getattr(self, name, None)))
        ]
        if offenders:
            msg = (
                f"以下字段含有存不进数据库的字符（U+0000）：{'、'.join(offenders)}。"
                "🔴 它在内存后端能跑通、在 PostgreSQL 上是 500——"
                "两个后端对同一个请求给出不同结果"
            )
            raise ValueError(msg)
        return self


#: 会话类自由文本的上限：用户消息、反馈正文、记忆内容。
#:
#: 🔴 **没有上限的自由文本不是"宽松"，是一条存储耗尽通道。**
#: 这些字段会进**只追加**的事件表，而且反馈正文还会进记忆。
#: 没有上限意味着一个请求就能往一个删不掉的地方写任意多字节。
#: 定的值远大于任何真实输入（8000 字符约等于一篇长文），
#: 因此它约束的是滥用，不是用法。
_FREE_TEXT_MAX = 8_000

#: 短说明类文本的上限：驳回理由、批准说明、单条对照口径。
_NOTE_MAX = 2_000

#: 列表字段的条目数上限。
#:
#: 与长度上限同理：`evidence` 会原样进提案并持久化，
#: 一个一万项的列表就是一个一万项的提案。
_LIST_MAX = 50


def _reject_if_blank(value: str) -> str:
    """把"看起来有内容、实际什么都没说"的输入挡在边界上。

    🔴 **``min_length=1`` 挡不住它。**

    ``str_strip_whitespace`` 用的是 ``str.strip()``，而后者不认识
    零宽空格一类的格式字符（``"\\u200b".isspace()`` 是 ``False``）。
    于是一个只由零宽字符组成的正文，长度不为 0、strip 之后也不为空，
    会一路穿到事件流里——它看起来是空的，占着位置，而且删不掉。

    判据统一在 :func:`ai_psi.domain.common.is_blank` 里，
    服务层与边界用的是**同一个**函数：两处各写一份的话，
    "什么算空白"会在 HTTP 层与领域层给出不同答案，
    而两处各自都是自洽的。

    Args:
        value: 待校验的字符串（已由配置去掉首尾空白）。

    Returns:
        原值。

    Raises:
        ValueError: 该文本去掉空白与不可见格式字符后为空。
    """
    if is_blank(value):
        msg = "该字段不能是空白（含零宽字符等不可见格式字符）"
        raise ValueError(msg)
    return value


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


class SubmitMessageRequest(_BoundaryRequestModel):
    """提交用户消息并启动认知回合。"""

    model_config = _STRICT_TRIMMED

    user_id: UUID | None = Field(default=None, description="归属用户")
    content: str = Field(
        min_length=1,
        max_length=_FREE_TEXT_MAX,
        description=(
            "用户消息原文。⚠️ 首尾空白会被去掉，因此「   」是一个 422 "
            "而不是一个空消息启动的完整认知回合"
        ),
    )
    requested_depth: CognitiveDepth | None = Field(
        default=None,
        description="用户显式请求的深度。🔴 仍受预算与规则约束（ADR-0008）",
    )
    response_style: str = Field(default="structured", min_length=1, max_length=_NOTE_MAX)
    allow_long_term_memory: bool = True

    @field_validator("content")
    @classmethod
    def _content_must_say_something(cls, value: str) -> str:
        """🔴 空白消息不该启动一个认知回合——那会白花一次预算。"""
        return _reject_if_blank(value)


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
    differs_from_projection: bool = Field(
        default=False,
        description=(
            "🔴 **数据一致性告警**：事件流重建出的状态与当前状态表不一致。"
            "为 True 通常意味着有写入绕过了应用服务。正常情况下恒为 False；"
            "阶段 6.5 之前这个信号在代码里算出来了却没有任何出口"
        ),
    )
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


class CorrectMemoryRequest(_BoundaryRequestModel):
    """用户纠正一条记忆。"""

    model_config = _STRICT_TRIMMED

    user_id: UUID = Field(description="发起纠正的用户；必须与记忆的作用域一致")
    new_content: str = Field(min_length=1, max_length=_FREE_TEXT_MAX, description="新的内容")

    @field_validator("new_content")
    @classmethod
    def _content_must_say_something(cls, value: str) -> str:
        """🔴 把一条记忆纠正成"看不见的东西"，等于用纠正把它删了。"""
        return _reject_if_blank(value)


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


class FeedbackRequest(_BoundaryRequestModel):
    """对某个认知回合的反馈。"""

    model_config = _STRICT_TRIMMED

    feedback_type: FeedbackType = Field(description="反馈类型")
    content: str = Field(
        min_length=1,
        max_length=_FREE_TEXT_MAX,
        description=("反馈正文。⚠️ 首尾空白会被去掉，因此「   」是一个 422 而不是一个 500"),
    )
    related_claim: str | None = Field(
        default=None,
        max_length=_NOTE_MAX,
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

    @field_validator("content")
    @classmethod
    def _content_must_say_something(cls, value: str) -> str:
        """🔴 只由不可见字符组成的反馈，是一条**看起来存在的**记录。"""
        return _reject_if_blank(value)


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


class EvaluateProposalRequest(_BoundaryRequestModel):
    """记录一次离线评估。"""

    model_config = _STRICT_TRIMMED

    verdict: EvaluationVerdict = Field(
        description="评估结论。⚠️ inconclusive 是「看不出」而不是「没差」，它是必须存在的选项"
    )
    evidence: list[str] = Field(
        min_length=1,
        max_length=_LIST_MAX,
        description=(
            "对照口径、样本量、参照版本。**不得为空、也不得全是空白**——"
            "「结论：改善」而没说跟什么比、比了多少个样本，"
            "是一条无法被复核、因而也无法被推翻的记录"
        ),
    )
    notes: str | None = Field(default=None, max_length=_NOTE_MAX, description="评审说明")
    actor_id: str = Field(default="reviewer", min_length=1, max_length=_ACTOR_ID_MAX)

    @field_validator("evidence")
    @classmethod
    def _evidence_items_must_say_something(cls, value: list[str]) -> list[str]:
        """🔴 逐条校验，而不只看列表长度。

        `min_length=1` 只保证"有一项"，保证不了"那一项说了什么"——
        `evidence=["   "]` 会带着一句空白被永久写进事件负载，
        而它正是这个字段要防的那类"无法被复核的记录"。
        """
        blank = [index for index, item in enumerate(value) if is_blank(item)]
        if blank:
            msg = f"evidence 的第 {blank} 项是空白——没有对照口径的结论无法被复核"
            raise ValueError(msg)
        over = [index for index, item in enumerate(value) if len(item) > _NOTE_MAX]
        if over:
            msg = f"evidence 的第 {over} 项超过 {_NOTE_MAX} 字符"
            raise ValueError(msg)
        return value


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


class ApproveProposalRequest(_BoundaryRequestModel):
    """批准进行人工试验。"""

    model_config = _STRICT_TRIMMED

    approved_by: str = Field(min_length=1, max_length=_ACTOR_ID_MAX, description="批准人标识")
    note: str | None = Field(default=None, max_length=_NOTE_MAX, description="批准说明")


class RejectProposalRequest(_BoundaryRequestModel):
    """驳回提案。"""

    model_config = _STRICT_TRIMMED

    rejected_by: str = Field(min_length=1, max_length=_ACTOR_ID_MAX, description="驳回人标识")
    reason: str = Field(
        min_length=1,
        max_length=_NOTE_MAX,
        description=(
            "驳回理由。**必填**：「不想做」与「做不了」对后来者是完全不同的信息。"
            "⚠️ 首尾空白与不可见格式字符会被去掉，因此「   」是一个 422"
        ),
    )

    @field_validator("reason")
    @classmethod
    def _reason_must_say_something(cls, value: str) -> str:
        """🔴 一条"看不见理由"的驳回，等于把"不想做"记成了"做不了"。"""
        return _reject_if_blank(value)


# ---------------------------------------------------------------------------
# 学习链路（任务书 §11，阶段 6.5 §四/§七）
# ---------------------------------------------------------------------------


class LearningRunRequest(_BoundaryRequestModel):
    """跑一次学习链路的输入。

    🔴 **全部字段可选，且没有任何一个是"批准"。**

    这条路径能做的只有"跑一次链路、生成 DRAFT 草案"。
    批准与驳回在 `improvement-proposals/*` 那几条路由上，
    而且必须先评估（不变量 11）。
    """

    model_config = _STRICT_TRIMMED

    fix_direction: str | None = Field(
        default=None,
        max_length=_NOTE_MAX,
        description="严重错误的明确修复方向（若有）。⚠️ 只影响理由与条件二，不影响次数门槛",
    )
    baseline_round_ids: list[UUID] | None = Field(
        default=None,
        max_length=_LIST_MAX,
        description=(
            "离线评测的基线回合。``None`` 表示只算全部回合的基线快照——"
            "那仍然是一次真实评测，只是**没有对照**（§11.3 条件三记为「未评估」）"
        ),
    )
    candidate_round_ids: list[UUID] | None = Field(
        default=None,
        max_length=_LIST_MAX,
        description="对照用的候选回合；``None`` 表示没有候选数据，**不等于**「没有退化」",
    )
    actor_id: str = Field(default="learning_service", min_length=1, max_length=_ACTOR_ID_MAX)


class SuppressedObservation(BaseModel):
    """一个**没有被放行**的观察，连同理由。

    🔴 它必须出现在响应里。一份只说"生成了 0 条提案"的报告
    无法回答"为什么没有"——而那正是下一次运行时最需要知道的事。
    """

    model_config = _STRICT

    error_class: str | None = None
    situation_signature: str | None = None
    reasons: list[str] = Field(default_factory=list)

    @classmethod
    def from_entry(cls, pattern: object, reasons: tuple[str, ...]) -> SuppressedObservation:
        """从 ``LearningRun.suppressed`` 的一项构造。

        🔴 **情境签名必须带上。** 只说"某个模式没过门禁"是不够的——
        运维要知道的是**哪一个**模式，否则这份清单无法指导任何动作。
        `LearningRun.suppressed` 的第一项在"未达模式门槛"那一支里是
        ``None``（那时连模式都没形成），因此这里两栏都可为 ``None``，
        而理由里会写清是哪一种。
        """
        from ai_psi.learning.pattern_detector import ErrorPattern

        typed = pattern if isinstance(pattern, ErrorPattern) else None
        return cls(
            error_class=None if typed is None else typed.error_type.value,
            situation_signature=None if typed is None else typed.situation_signature,
            reasons=list(reasons),
        )


class LearningRunResponse(BaseModel):
    """一次学习链路运行的结果。

    🔴 ``comparison_available`` 必须回传，**包括它为 false 的时候**。
    单侧数据不能作为"改动没有退化"的证据——两者的区别要能从
    响应里读出来，而不是靠调用方去猜。
    """

    model_config = _STRICT

    experiences_considered: int = Field(ge=0)
    unreadable_experiences: int = Field(ge=0)
    unreadable_evaluations: int = Field(ge=0)
    patterns_found: int = Field(ge=0)
    gate_approved: int = Field(ge=0)
    created_proposal_ids: list[UUID] = Field(default_factory=list)
    already_covered: int = Field(ge=0)
    suppressed: list[SuppressedObservation] = Field(default_factory=list)
    comparison_available: bool = Field(
        description=(
            "离线评测是否真的做了对照。🔴 **false 不等于「没有退化」**——"
            "它表示没有候选数据，条件三因此记为「未评估」而不是「不成立」"
        )
    )
    evaluation_reasons: list[str] = Field(default_factory=list)
    summary: str

    @classmethod
    def from_run(cls, run: object) -> LearningRunResponse:
        """从 ``LearningRun`` 构造响应。"""
        from ai_psi.application.learning_service import LearningRun

        assert isinstance(run, LearningRun)
        comparison = run.offline_evaluation
        return cls(
            experiences_considered=run.experiences_considered,
            unreadable_experiences=run.unreadable_experiences,
            unreadable_evaluations=run.unreadable_evaluations,
            patterns_found=len(run.patterns),
            gate_approved=len(run.candidates),
            created_proposal_ids=[item.proposal.id for item in run.created],
            already_covered=len(run.already_covered),
            suppressed=[
                SuppressedObservation.from_entry(pattern, reasons)
                for pattern, reasons in run.suppressed
            ],
            comparison_available=(False if comparison is None else comparison.comparison_available),
            evaluation_reasons=[] if comparison is None else list(comparison.reasons),
            summary=run.summary(),
        )
