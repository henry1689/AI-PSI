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
from typing import Any, Final
from uuid import UUID

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from ai_psi.domain.enums import (
    ActorType,
    ApprovalLevel,
    ErrorType,
    MemoryStatus,
    MemoryType,
    ProposalStatus,
    RetentionPolicy,
    RoundState,
    SensitivityLevel,
    TrustLevel,
    VerificationStatus,
)
from ai_psi.infrastructure.db.base import Base, enum_check_expression
from ai_psi.providers.embeddings import DEFAULT_EMBEDDING_DIMENSION

__all__ = [
    "CognitiveRoundRow",
    "EventRow",
    "IdempotencyKeyRow",
    "ImprovementProposalRow",
    "MemoryEmbeddingRow",
    "MemoryRow",
]


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


class MemoryRow(Base):
    """长期记忆的主表（任务书 §5.11）。

    🔴 **记忆错误与其他错误不同：它的后果是累积的。**
    一次错误的回答只影响一次交互，一次错误的记忆写入会持续影响此后所有回合。
    因此本表的约束比别处更严——领域层的每一条不变式，只要能在数据库上表达，
    这里就再写一遍。

    ⚠️ **本表不存放向量。** 向量在 :class:`MemoryEmbeddingRow` 里，
    理由见该类的文档。
    """

    __tablename__ = "memories"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)

    # ---- EntityMetadata ----
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: 乐观锁版本。与回合表同一条规则：条件更新，**绝不静默覆盖**。
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)

    # ---- 作用域（🔴 不变量 14）----
    #: ``None`` 表示系统级记忆。**检索路径必须带它做过滤**，
    #: 且必须用 ``IS NOT DISTINCT FROM`` 而不是 ``=``——后者在
    #: ``user_id`` 为 ``NULL`` 时永远不成立，系统级记忆会一条都查不出来。
    user_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)

    # ---- 内容 ----
    memory_type: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # ---- 溯源 ----
    source_event_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, default=list
    )
    evidence_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, default=list
    )

    # ---- 效力与敏感度 ----
    verification_status: Mapped[str] = mapped_column(String(24), nullable=False)
    applicability: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    sensitivity: Mapped[str] = mapped_column(String(24), nullable=False)
    retention_policy: Mapped[str] = mapped_column(String(24), nullable=False)
    access_scope: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)

    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # ---- 版本链与冲突 ----
    supersedes_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    contradicts_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, default=list
    )

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    embedding_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        CheckConstraint(enum_check_expression("memory_type", MemoryType), name="memory_type_valid"),
        CheckConstraint(enum_check_expression("status", MemoryStatus), name="status_valid"),
        CheckConstraint(
            enum_check_expression("verification_status", VerificationStatus),
            name="verification_status_valid",
        ),
        CheckConstraint(
            enum_check_expression("sensitivity", SensitivityLevel), name="sensitivity_valid"
        ),
        CheckConstraint(
            enum_check_expression("retention_policy", RetentionPolicy),
            name="retention_policy_valid",
        ),
        CheckConstraint("content <> ''", name="content_non_empty"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="valid_until_after_valid_from",
        ),
        # 🔴 自我引用在领域层已被模型校验器拦住，这里再拦一次。
        # 取代链一旦出现自环，遍历它的代码会陷入死循环，
        # 而"记忆取代链"正是纠正追溯路径。
        CheckConstraint("supersedes_id IS NULL OR supersedes_id <> id", name="no_self_supersede"),
        CheckConstraint("NOT (id = ANY(contradicts_ids))", name="no_self_contradiction"),
        # 检索的主力索引：作用域 + 状态。
        Index("ix_memories_user_status", "user_id", "status"),
        Index("ix_memories_user_type", "user_id", "memory_type"),
        # 失效扫描（把到期的记忆标为 EXPIRED）
        Index("ix_memories_valid_until", "valid_until"),
    )


class MemoryEmbeddingRow(Base):
    """记忆的向量索引（任务书 §10.5「向量索引同步删除」）。

    🔴 **为什么单独一张表，而不是 ``memories`` 上的一列。**

    因为不变量 15（"删除的记忆不得继续出现在向量检索结果中"）
    必须是**结构性保证**，而不是"查询恰好带了过滤条件"。

    * 若向量是 ``memories`` 的一列：逻辑删除只把 ``status`` 改成
      ``deleted``，行还在，**向量还在索引里**。检索正确与否完全取决于
      每一次查询都记得加 ``WHERE status IN (...)`——漏一处就是隐私事故。
    * 而且 pgvector 的 HNSW 索引无法"只删一个向量"，
      要保留行就只能让它在索引里继续占据位置。

    独立成表之后，"删除传播到向量索引"是一次**真实执行过的 DELETE**，
    可以直接查这张表来断言；外键上的 ``ON DELETE CASCADE`` 还额外保证了
    主表被物理删除时索引不会成为孤儿。

    ⚠️ 本表在逻辑删除时**物理删除行**，而主表保留行。
    这不是不一致：主表保存的是"曾经记过什么"的审计事实，
    索引保存的是"现在能检索到什么"的能力，两者本来就有不同的生命周期。
    """

    __tablename__ = "memory_embeddings"

    #: 一主键 + 外键。一条记忆至多有一个当前向量。
    memory_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("memories.id", ondelete="CASCADE"),
        primary_key=True,
    )

    #: 向量本体。维度是**列的固定属性**——不同维度的向量不可比较，
    #: 因此改维度必须走迁移（见 ADR-0017）。
    embedding: Mapped[list[float]] = mapped_column(
        Vector(DEFAULT_EMBEDDING_DIMENSION), nullable=False
    )

    #: 生成该向量所用的向量空间版本。
    #: 检索时只比对**同版本**的向量——换模型后旧向量自动失效，
    #: 而不是被拿去和一个语义空间已经不同的查询向量比较，
    #: 那会得到看似合理、实则无意义的结果。
    embedding_version: Mapped[str] = mapped_column(String(64), nullable=False)

    #: 生成该向量的 Provider 名称（诊断用，不含密钥）。
    provider: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("embedding_version <> ''", name="embedding_version_non_empty"),
        # 🔴 余弦距离的 HNSW 索引。向量的 L2 归一化由 Provider 保证，
        # 因此余弦与内积在这里是等价的；用余弦是因为它对
        # "某个 Provider 忘了归一化"更宽容。
        Index(
            "ix_memory_embeddings_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_memory_embeddings_version", "embedding_version"),
    )


# ---------------------------------------------------------------------------
# R72：同一业务模式至多一条**活跃**提案（阶段 7）
# ---------------------------------------------------------------------------
#
# 背景：``LearningService._covered_keys()`` 是"先读已存在的提案、再生成"，
# 读与写之间没有锁、表上也没有对应的唯一约束。两个并发的学习运行会各自
# 读到"还没有提案"的旧快照，各走完门禁与生成，然后在写入时才分胜负——
# 结果是两条内容完全相同的 DRAFT（评审 6.6 §F1 实测 3/3 复现）。
#
# 🔴 **业务键是 `(error_class, applicability[0])`，不是整数组。**
# ``_covered_keys()`` 取的是 ``applicability[0]``，而 ``applicability``
# 是 ``text[]``：整数组唯一会把 ``[]`` 与 ``['a','b']`` 判成与应用层
# **不同**的键（前者被应用层视为"不覆盖任何东西"，后者被应用层视为
# 与 ``['a']`` 同键）。索引表达式因此逐字对齐应用层。
#
# ⚠️ **PostgreSQL 的数组下标从 1 起**，所以 SQL 里的 ``applicability[1]``
# 就是 Python 里的 ``applicability[0]``。两个后端的 ``find_active_for_pattern``
# 各按自己语言的约定写，靠契约测试对齐。
ACTIVE_PATTERN_INDEX_NAME: Final[str] = "uq_improvement_proposals_active_pattern"

#: 索引里的第二列：业务签名。见上方"数组下标从 1 起"的说明。
_ACTIVE_PATTERN_INDEX_EXPRESSION: Final[str] = "(applicability[1])"

#: 部分谓词。两半各有理由：
#:
#: * ``cardinality(applicability) > 0``——空数组下标越界得到 NULL，
#:   而 PostgreSQL 的唯一索引**允许多个 NULL**。不排除它们就会留下一个
#:   "看起来唯一、实际对这类行毫无约束"的约束。排除之后，
#:   索引里根本不存在 NULL，而不是靠 NULL 互不相等侥幸不冲突。
#:   方向与应用层一致：``_covered_keys()`` 同样跳过空 applicability。
#: * ``status NOT IN (终态)``——数据库裁决的是"至多一条**活跃**提案"。
#:   "终态也不再提议"（R55）仍由应用层承担，它是策略不是不变量。
_ACTIVE_PATTERN_INDEX_PREDICATE: Final[str] = (
    "cardinality(applicability) > 0 AND status NOT IN ('rejected', 'approved_for_manual_trial')"
)


class ImprovementProposalRow(Base):
    """改进提案（任务书 §5.12、§11）。

    🔴 **为什么提案有表，而经验没有。**

    经验是**不可变的观察**（"这个回合发生了这件事"），写入之后从不更新，
    因此它的家在事件流里——加一张表只会引入"投影与事件流不一致"的可能。
    提案不同：它的状态会变（``DRAFT`` → … → 终态），需要乐观锁、
    按 id 取、按状态列表查询。这与 ``cognitive_rounds`` 的理由完全一样
    （ADR-0002：表是"为查询与并发控制而物化的投影"）。

    🔴 **``status`` 的 CHECK 是对不变量 11 的数据库级强制。**

    ``enum_check_expression`` 由 ``ProposalStatus`` 生成，而这个枚举里
    **不存在 ACTIVE**。因此"把提案标记为已生效"这件事在数据库层
    就不可能发生——即使应用层被绕过。
    """

    __tablename__ = "improvement_proposals"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)

    # ---- EntityMetadata ----
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False)

    # ---- 提案内容 ----
    target_component: Mapped[str] = mapped_column(String(128), nullable=False)
    observed_problem: Mapped[str] = mapped_column(Text, nullable=False)
    error_class: Mapped[str] = mapped_column(String(32), nullable=False)

    supporting_experience_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PGUUID(as_uuid=True)), nullable=False, default=list
    )
    counterexamples: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)

    proposed_change: Mapped[str] = mapped_column(Text, nullable=False)
    expected_benefit: Mapped[str] = mapped_column(Text, nullable=False)

    possible_regressions: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    applicability: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)

    evaluation_plan: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    success_metrics: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    rollback_conditions: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )

    approval_level: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)

    __table_args__ = (
        CheckConstraint(enum_check_expression("error_class", ErrorType), name="error_class_valid"),
        CheckConstraint(
            enum_check_expression("approval_level", ApprovalLevel), name="approval_level_valid"
        ),
        # 🔴 不变量 11 的数据库级强制：ProposalStatus 里没有 ACTIVE，
        # 因此这个白名单**天然排除了"已生效"**。
        CheckConstraint(enum_check_expression("status", ProposalStatus), name="status_valid"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("target_component <> ''", name="target_component_non_empty"),
        # 提案必须能解释自己为什么存在——没有预期收益的提案无法被评估
        CheckConstraint("expected_benefit <> ''", name="expected_benefit_non_empty"),
        Index("ix_improvement_proposals_status_created", "status", "created_at"),
        Index("ix_improvement_proposals_error_class", "error_class"),
        Index("ix_improvement_proposals_component", "target_component"),
        # 🔴 **R72 的并发防线**（阶段 7）。见模块级常量上的说明。
        Index(
            ACTIVE_PATTERN_INDEX_NAME,
            "error_class",
            # ⚠️ 必须包 `text()`。裸字符串会被 SQLAlchemy 当成**列名**去解析，
            # 而这是一个表达式——解析失败时抛
            # `ConstraintColumnNotFoundError: no column named '(applicability[1])'`。
            text(_ACTIVE_PATTERN_INDEX_EXPRESSION),
            unique=True,
            postgresql_where=text(_ACTIVE_PATTERN_INDEX_PREDICATE),
        ),
    )
