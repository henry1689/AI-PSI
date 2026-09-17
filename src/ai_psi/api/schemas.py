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

from ai_psi.domain.enums import (
    CognitiveDepth,
    ConfidenceBand,
    EpistemicAction,
    RoundState,
)

__all__ = [
    "ConversationCreatedResponse",
    "DimensionStatus",
    "HealthResponse",
    "JudgmentView",
    "ModelInvocationView",
    "ReflectionView",
    "ReplayResponse",
    "RoundResponseBody",
    "RoundStatusResponse",
    "RoundSummaryResponse",
    "SubmitMessageRequest",
    "SubmitMessageResponse",
]

_STRICT = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# 会话与消息（§12.1）
# ---------------------------------------------------------------------------


class ConversationCreatedResponse(BaseModel):
    """创建会话的响应。

    ⚠️ V0.1 **不持久化会话实体**——它是一个客户端分组键，
    服务端只负责生成一个稳定的 UUID。会话记录要随长期记忆一起在阶段 5 落地。
    """

    model_config = _STRICT

    conversation_id: UUID
    note: str = "V0.1 会话为客户端分组键，服务端尚未持久化会话实体（阶段 5 落地）"


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
