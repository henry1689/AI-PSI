"""事件与模型调用记录。

🔴 **本模块的 ``Event`` 是唯一不继承 ``EntityMetadata`` 的领域对象**（ADR-0006）。

理由：事件只追加，**永不修改、永不删除**（ADR-0002）。
``version`` 与 ``updated_at`` 对不可变的事件毫无意义，
强行继承反而会诱导出"更新事件"这种违背设计的行为。

事件改用**双时间戳**：

* ``occurred_at`` —— 事件在现实世界中**发生**的时间；
* ``recorded_at`` —— 事件被系统**记录**的时间。

回放时必须区分二者，否则乱序到达的事件会被错误地按记录时间排序。
"""

from __future__ import annotations

from typing import Any, Self
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_psi.domain.common import SCHEMA_VERSION_V1, UtcDatetime, utc_now
from ai_psi.domain.enums import ActorType, EventType, SensitivityLevel, TrustLevel

__all__ = ["Event", "ModelInvocationInfo"]


class ModelInvocationInfo(BaseModel):
    """单次模型调用的审计记录（任务书 §8.2）。

    🔴 **不保存供应商返回的完整隐藏推理内容。**
    只保留 ``response_hash``——原始响应的哈希，用于事后审计比对，
    不含内容本身（ADR-0003，`docs/cognitive_constitution.md` 红线一）。

    🔴 不变量 18：所有模型调用必须记录 ``model`` 与 ``prompt_version``。
    两者都是必填字段，缺失即构造失败。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    invocation_id: UUID = Field(
        default_factory=uuid4,
        description="本次调用标识。**重试必须使用新的 invocation_id**（任务书 §6.3）",
    )
    provider: str = Field(
        min_length=1, description="Provider 名称，如 anthropic / openai_compatible / mock"
    )
    model: str = Field(min_length=1, description="模型标识（不变量 18）")
    task_name: str = Field(min_length=1, description="Prompt 任务名，与 PromptRegistry 中的键一致")
    prompt_version: str = Field(min_length=1, description="Prompt 语义版本（不变量 18）")

    started_at: UtcDatetime
    completed_at: UtcDatetime | None = None
    latency_ms: int | None = Field(default=None, ge=0)

    input_token_count: int | None = Field(default=None, ge=0)
    output_token_count: int | None = Field(default=None, ge=0)

    retry_count: int = Field(default=0, ge=0)
    result_status: str = Field(
        default="success", min_length=1, description="success / invalid_output / timeout / error"
    )
    response_hash: str | None = Field(
        default=None, description="原始响应哈希，用于审计比对（不保存内容）"
    )

    @model_validator(mode="after")
    def _check_time_order(self) -> Self:
        if self.completed_at is not None and self.completed_at < self.started_at:
            msg = "completed_at 不得早于 started_at"
            raise ValueError(msg)
        return self


class Event(BaseModel):
    """不可变事件（任务书 §5.2，ADR-0002）。

    事件是系统的**真相来源**；当前状态是它在聚合上的投影。

    🔴 ``payload`` 必须**脱敏**：不得写入高敏感用户正文、密钥或
    Authorization 头（`docs/security.md` §3）。
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: UUID = Field(default_factory=uuid4)
    event_type: EventType

    occurred_at: UtcDatetime = Field(description="事件发生时间（UTC）")
    recorded_at: UtcDatetime = Field(default_factory=utc_now, description="事件被记录时间（UTC）")

    actor_type: ActorType
    actor_id: str = Field(min_length=1, description="触发者的标识：组件名、模型名或用户 id")

    # 作用域与关联
    user_id: UUID | None = Field(default=None, description="用户作用域键（不变量 14）")
    conversation_id: UUID | None = None
    cognitive_round_id: UUID | None = None
    correlation_id: UUID = Field(
        default_factory=uuid4,
        description="同一次请求的关联链标识，用于把分散的事件串成一条因果链",
    )
    causation_id: UUID | None = Field(
        default=None,
        description="直接触发本事件的上游事件 id",
    )

    payload: dict[str, Any] = Field(default_factory=dict, description="事件负载，**必须脱敏**")
    evidence_refs: list[UUID] = Field(default_factory=list)

    trust_level: TrustLevel = TrustLevel.MEDIUM
    sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL

    model_info: ModelInvocationInfo | None = Field(
        default=None,
        description="若本事件由模型调用产生，记录该次调用的审计信息",
    )

    schema_version: str = Field(default=SCHEMA_VERSION_V1, min_length=1)

    @model_validator(mode="after")
    def _check_recorded_after_occurred(self) -> Self:
        """记录时间不得早于发生时间。

        允许相等（本地同步产生的事件），但不允许"还没发生就被记录"。
        """
        if self.recorded_at < self.occurred_at:
            msg = (
                f"recorded_at ({self.recorded_at.isoformat()}) 不得早于 "
                f"occurred_at ({self.occurred_at.isoformat()})"
            )
            raise ValueError(msg)
        return self
