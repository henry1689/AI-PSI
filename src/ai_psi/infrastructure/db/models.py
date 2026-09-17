"""ORM 实体（阶段 2：事件、认知回合、幂等键）。

⚠️ **本模块不是领域模型**。领域对象在 `ai_psi.domain`，两者通过
:mod:`ai_psi.infrastructure.db.mappers` 显式转换（ADR-0006）。

🔴 **本模块刻意把关键认知不变量下沉到数据库约束。**

应用层已经用模型校验器拦住了非法数据（不变量 19/20），这里再加一道
数据库级 CHECK。理由是这两条不变量守的是**可诊断性**——
一个"完成了但不知道为什么停"或"失败了但查不出原因"的回合，
在事后排查时等同于**没有记录**。应用层有 bug 时，数据库是最后一道防线。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from ai_psi.domain.enums import ActorType, ErrorType, RoundState, SensitivityLevel, TrustLevel
from ai_psi.infrastructure.db.base import Base, enum_check_expression

__all__ = ["CognitiveRoundRow", "EventRow", "IdempotencyKeyRow"]


class EventRow(Base):
    """事件表（只追加）。

    🔴 **本表没有 UPDATE / DELETE 路径。** 仓储（`repositories.py`）只暴露
    ``append`` 与读取方法。事件是系统的真相来源，当前状态只是它的投影
    （ADR-0002）。

    双时间戳：``occurred_at`` 是事件**发生**时间，``recorded_at`` 是**被记录**时间。
    回放时必须区分二者，否则乱序到达的事件会被错误地按记录时间排序。
    """

    __tablename__ = "events"

    #: 全局单调递增序。🔴 **回放的排序依据**——
    #: `recorded_at` 精度不足以区分同一微秒内的多次写入，用它排序会得到不确定结果。
    sequence: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), nullable=False, unique=True
    )

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)

    event_type: Mapped[str] = mapped_column(String(64), nullable=False)

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # 作用域与因果关联
    user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    conversation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    cognitive_round_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    correlation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    causation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    #: 事件负载。**写入前必须脱敏**（任务书 §17.1）——
    #: 高敏感用户正文、密钥、Authorization 头一律不得进入此列。
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    evidence_refs: Mapped[list[UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, default=list
    )

    trust_level: Mapped[str] = mapped_column(String(16), nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(24), nullable=False)

    #: 模型调用的审计信息（仅哈希，不含思维链）。
    model_info: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        CheckConstraint(enum_check_expression("actor_type", ActorType), name="actor_type_valid"),
        CheckConstraint(enum_check_expression("trust_level", TrustLevel), name="trust_level_valid"),
        CheckConstraint(
            enum_check_expression("sensitivity", SensitivityLevel), name="sensitivity_valid"
        ),
        CheckConstraint(
            "recorded_at >= occurred_at",
            name="recorded_not_before_occurred",
        ),
        # 回放按回合流读取，按 sequence 排序
        Index("ix_events_round_sequence", "cognitive_round_id", "sequence"),
        # 因果链追溯
        Index("ix_events_correlation_id", "correlation_id"),
        # 用户作用域审计（不变量 14 的排查入口）
        Index("ix_events_user_sequence", "user_id", "sequence"),
        Index("ix_events_type_recorded", "event_type", "recorded_at"),
    )


class CognitiveRoundRow(Base):
    """认知回合的**当前状态投影**（ADR-0002）。

    真正的真相来源是 `events` 表；本表是为查询与并发控制而物化的投影。
    两者在**同一事务**中写入。
    """

    __tablename__ = "cognitive_rounds"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)

    # ---- EntityMetadata ----
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: 乐观锁版本。🔴 更新条件必须带 `WHERE version = :expected`，
    #: 冲突时抛 OptimisticLockError，**绝不静默覆盖**（ADR-0002）。
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)

    # ---- 作用域 ----
    user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    conversation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    trigger_event_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    # ---- 认知过程 ----
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    depth_level: Mapped[str] = mapped_column(String(8), nullable=False)
    budget: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    model_calls_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metacognitive_loops: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ---- 终态诊断（不变量 19 / 20）----
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # ---- 幂等与因果 ----
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    correlation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    causation_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(enum_check_expression("state", RoundState), name="state_valid"),
        CheckConstraint(
            enum_check_expression("error_category", ErrorType),
            name="error_category_valid",
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("model_calls_used >= 0", name="model_calls_non_negative"),
        CheckConstraint("metacognitive_loops >= 0", name="metacognitive_loops_non_negative"),
        CheckConstraint(
            "model_calls_used <= (budget ->> 'max_model_calls')::int",
            name="model_calls_within_budget",
        ),
        # 🔴 不变量 19：完成回合必须有停止原因
        CheckConstraint(
            "state <> 'completed' OR (stop_reason IS NOT NULL AND stop_reason <> '')",
            name="completed_requires_stop_reason",
        ),
        # 🔴 不变量 20：失败回合必须可诊断
        CheckConstraint(
            "state <> 'failed' OR (failure_stage IS NOT NULL AND error_category IS NOT NULL)",
            name="failed_requires_diagnostics",
        ),
        CheckConstraint(
            "completed_at IS NULL OR started_at IS NULL OR completed_at >= started_at",
            name="completed_not_before_started",
        ),
        CheckConstraint(
            "updated_at >= created_at",
            name="updated_not_before_created",
        ),
        Index("ix_cognitive_rounds_user_state", "user_id", "state"),
        Index("ix_cognitive_rounds_conversation", "conversation_id"),
    )


class IdempotencyKeyRow(Base):
    """幂等键（任务书 §13.4）。

    🔴 API 重试**不得创建重复回合**。做法是：以请求方的
    ``Idempotency-Key`` 为唯一键先占位，占位成功后才有权创建回合。

    同一 key 携带**不同**的请求体哈希时视为冲突（客户端复用 key 是 bug），
    返回 ``ConflictError``，而不是默默返回旧结果——后者会掩盖调用方的问题。
    """

    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    #: 请求体哈希。用于区分「真的重试」与「误用同一个 key」。
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 占位成功后由业务写入；为 None 表示占位中（上一次请求尚未提交）。
    cognitive_round_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("key <> ''", name="key_non_empty"),
        Index("ix_idempotency_keys_created_at", "created_at"),
    )
