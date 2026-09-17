"""领域对象与 ORM 实体之间的**显式**转换。

🔴 ADR-0006 要求 `domain/`、`db/`、`api/` 三套模型**不得**一键互转。

显式映射的代价是一些样板代码；收益是**一次数据库结构变更不会自动
波及领域层**。一键互转看似省事，实际会把"加一个数据库列"变成
"领域对象悄悄多了一个字段"，而领域对象一旦被存储细节污染，
内存适配器测试与 Provider 替换都会失效。
"""

from __future__ import annotations

from typing import Any

from ai_psi.domain.cognitive_rounds import CognitiveBudget, CognitiveRound
from ai_psi.domain.enums import (
    ActorType,
    CognitiveDepth,
    ErrorType,
    EventType,
    MemoryStatus,
    MemoryType,
    RetentionPolicy,
    RoundState,
    SensitivityLevel,
    TrustLevel,
    VerificationStatus,
)
from ai_psi.domain.events import Event, ModelInvocationInfo
from ai_psi.domain.memories import Memory
from ai_psi.infrastructure.db.models import CognitiveRoundRow, EventRow, MemoryRow

__all__ = [
    "apply_event",
    "apply_memory",
    "apply_round",
    "memory_to_row",
    "memory_to_values",
    "round_to_row",
    "row_to_event",
    "row_to_memory",
    "row_to_round",
]


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------


def apply_event(row: EventRow, event: Event) -> None:
    """把领域事件写入（新建的）ORM 行。

    事件**只追加**，因此只有"新建"这一种用法，不存在更新路径。

    Args:
        row: 新建的 ORM 行。
        event: 源领域对象。
    """
    row.id = event.id
    row.event_type = event.event_type.value
    row.occurred_at = event.occurred_at
    row.recorded_at = event.recorded_at
    row.actor_type = event.actor_type.value
    row.actor_id = event.actor_id
    row.user_id = event.user_id
    row.conversation_id = event.conversation_id
    row.cognitive_round_id = event.cognitive_round_id
    row.correlation_id = event.correlation_id
    row.causation_id = event.causation_id
    row.payload = dict(event.payload)
    row.evidence_refs = list(event.evidence_refs)
    row.trust_level = event.trust_level.value
    row.sensitivity = event.sensitivity.value
    row.model_info = _model_info_to_json(event.model_info)
    row.schema_version = event.schema_version


def row_to_event(row: EventRow) -> Event:
    """把 ORM 行还原为领域事件。

    Args:
        row: 数据库行。

    Returns:
        领域事件对象。
    """
    return Event(
        id=row.id,
        event_type=EventType(row.event_type),
        occurred_at=row.occurred_at,
        recorded_at=row.recorded_at,
        actor_type=ActorType(row.actor_type),
        actor_id=row.actor_id,
        user_id=row.user_id,
        conversation_id=row.conversation_id,
        cognitive_round_id=row.cognitive_round_id,
        correlation_id=row.correlation_id,
        causation_id=row.causation_id,
        payload=dict(row.payload),
        evidence_refs=list(row.evidence_refs),
        trust_level=TrustLevel(row.trust_level),
        sensitivity=SensitivityLevel(row.sensitivity),
        model_info=_model_info_from_json(row.model_info),
        schema_version=row.schema_version,
    )


def _model_info_to_json(info: ModelInvocationInfo | None) -> dict[str, object] | None:
    if info is None:
        return None
    # 只保留结构化字段；response_hash 用于审计比对，不含响应正文
    return info.model_dump(mode="json")


def _model_info_from_json(raw: dict[str, object] | None) -> ModelInvocationInfo | None:
    if raw is None:
        return None
    return ModelInvocationInfo.model_validate(raw)


# ---------------------------------------------------------------------------
# CognitiveRound
# ---------------------------------------------------------------------------


def round_to_values(round_: CognitiveRound) -> dict[str, Any]:
    """把领域回合映射为列名 → 值的字典。

    **本函数是回合字段映射的唯一真相来源**，被三个地方共用：

    * :func:`round_to_row`（插入）；
    * `SqlAlchemyRoundRepository.save` 的乐观锁 UPDATE 语句；
    * 未来可能的批量 upsert。

    单独抽出来的原因：如果 UPDATE 语句另写一份字段列表，
    新增一个领域字段时极易只改插入路径而忘了更新路径——
    结果是"字段能存进去但改不动"，这类 bug 很难在测试中被发现。

    Args:
        round_: 领域对象。

    Returns:
        列名到值的映射，**不含主键**。
    """
    return {
        "created_at": round_.created_at,
        "updated_at": round_.updated_at,
        "version": round_.version,
        "created_by": round_.created_by,
        "schema_version": round_.schema_version,
        "user_id": round_.user_id,
        "conversation_id": round_.conversation_id,
        "trigger_event_id": round_.trigger_event_id,
        "state": round_.state.value,
        "depth_level": round_.depth_level.value,
        "budget": round_.budget.model_dump(mode="json"),
        "model_calls_used": round_.model_calls_used,
        "metacognitive_loops": round_.metacognitive_loops,
        "stop_reason": round_.stop_reason,
        "failure_stage": round_.failure_stage,
        "error_category": (
            round_.error_category.value if round_.error_category is not None else None
        ),
        "idempotency_key": round_.idempotency_key,
        "correlation_id": round_.correlation_id,
        "causation_id": round_.causation_id,
        "started_at": round_.started_at,
        "completed_at": round_.completed_at,
    }


def round_to_row(round_: CognitiveRound) -> CognitiveRoundRow:
    """把领域回合转换为新的 ORM 行。

    Args:
        round_: 领域对象。

    Returns:
        可直接 `session.add()` 的 ORM 行。
    """
    values = round_to_values(round_)
    values["id"] = round_.id
    return CognitiveRoundRow(**values)


def apply_round(row: CognitiveRoundRow, round_: CognitiveRound) -> None:
    """把领域回合的全部字段写入已存在的 ORM 行。

    Args:
        row: 目标 ORM 行。
        round_: 源领域对象。
    """
    row.id = round_.id
    for column, value in round_to_values(round_).items():
        setattr(row, column, value)


def row_to_round(row: CognitiveRoundRow) -> CognitiveRound:
    """把 ORM 行还原为领域回合。

    Args:
        row: 数据库行。

    Returns:
        领域对象。
    """
    return CognitiveRound(
        id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
        created_by=row.created_by,
        schema_version=row.schema_version,
        user_id=row.user_id,
        conversation_id=row.conversation_id,
        trigger_event_id=row.trigger_event_id,
        state=RoundState(row.state),
        depth_level=CognitiveDepth(row.depth_level),
        budget=CognitiveBudget.model_validate(row.budget),
        model_calls_used=row.model_calls_used,
        metacognitive_loops=row.metacognitive_loops,
        stop_reason=row.stop_reason,
        failure_stage=row.failure_stage,
        error_category=ErrorType(row.error_category) if row.error_category else None,
        idempotency_key=row.idempotency_key,
        correlation_id=row.correlation_id,
        causation_id=row.causation_id,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def memory_to_values(memory: Memory) -> dict[str, Any]:
    """把领域记忆映射为列名 → 值的字典。

    与 :func:`round_to_values` 同一条理由：**本函数是字段映射的唯一真相来源**，
    插入路径与乐观锁更新路径共用它。另写一份字段列表，
    新增字段时就可能只改插入、忘了更新——得到"能存进去但改不动"。

    Args:
        memory: 领域对象。

    Returns:
        列名到值的映射，**不含主键**。
    """
    return {
        "created_at": memory.created_at,
        "updated_at": memory.updated_at,
        "version": memory.version,
        "created_by": memory.created_by,
        "schema_version": memory.schema_version,
        "user_id": memory.user_id,
        "memory_type": memory.memory_type.value,
        "content": memory.content,
        "source_event_ids": list(memory.source_event_ids),
        "evidence_ids": list(memory.evidence_ids),
        "verification_status": memory.verification_status.value,
        "applicability": list(memory.applicability),
        "sensitivity": memory.sensitivity.value,
        "retention_policy": memory.retention_policy.value,
        "access_scope": list(memory.access_scope),
        "valid_from": memory.valid_from,
        "valid_until": memory.valid_until,
        "supersedes_id": memory.supersedes_id,
        "contradicts_ids": list(memory.contradicts_ids),
        "status": memory.status.value,
        "embedding_version": memory.embedding_version,
    }


def memory_to_row(memory: Memory) -> MemoryRow:
    """把领域记忆转换为新的 ORM 行。

    Args:
        memory: 领域对象。

    Returns:
        可直接 `session.add()` 的 ORM 行。
    """
    values = memory_to_values(memory)
    values["id"] = memory.id
    return MemoryRow(**values)


def apply_memory(row: MemoryRow, memory: Memory) -> None:
    """把领域记忆的全部字段写入已存在的 ORM 行。

    Args:
        row: 目标 ORM 行。
        memory: 源领域对象。
    """
    row.id = memory.id
    for column, value in memory_to_values(memory).items():
        setattr(row, column, value)


def row_to_memory(row: MemoryRow) -> Memory:
    """把 ORM 行还原为领域记忆。

    Args:
        row: 数据库行。

    Returns:
        领域对象。
    """
    return Memory(
        id=row.id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        version=row.version,
        created_by=row.created_by,
        schema_version=row.schema_version,
        user_id=row.user_id,
        memory_type=MemoryType(row.memory_type),
        content=row.content,
        source_event_ids=list(row.source_event_ids),
        evidence_ids=list(row.evidence_ids),
        verification_status=VerificationStatus(row.verification_status),
        applicability=list(row.applicability),
        sensitivity=SensitivityLevel(row.sensitivity),
        retention_policy=RetentionPolicy(row.retention_policy),
        access_scope=list(row.access_scope),
        valid_from=row.valid_from,
        valid_until=row.valid_until,
        supersedes_id=row.supersedes_id,
        contradicts_ids=list(row.contradicts_ids),
        status=MemoryStatus(row.status),
        embedding_version=row.embedding_version,
    )
