"""领域对象 ↔ ORM 实体的映射测试（零 IO）。

这些转换是**手写的显式映射**（ADR-0006），因此必须逐个字段验证——
一个漏掉的字段表现为"存进去能读到旧值"，是最难发现的一类缺陷。

本文件不连数据库：ORM 行对象可以脱离会话直接构造。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ai_psi.domain.cognitive_rounds import CognitiveBudget, CognitiveRound
from ai_psi.domain.enums import (
    ActorType,
    CognitiveDepth,
    ErrorType,
    EventType,
    RoundState,
    SensitivityLevel,
    TrustLevel,
)
from ai_psi.domain.events import Event, ModelInvocationInfo
from ai_psi.infrastructure.db.mappers import (
    apply_event,
    round_to_row,
    round_to_values,
    row_to_event,
    row_to_round,
)

pytestmark = pytest.mark.unit

NOON = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


class TestEventMapping:
    def test_roundtrip_preserves_every_field(self) -> None:
        original = Event(
            event_type=EventType.HYPOTHESIS_CREATED,
            occurred_at=NOON - timedelta(hours=1),
            recorded_at=NOON,
            actor_type=ActorType.MODEL,
            actor_id="hypothesis_generator",
            user_id=uuid4(),
            conversation_id=uuid4(),
            cognitive_round_id=uuid4(),
            correlation_id=uuid4(),
            causation_id=uuid4(),
            payload={"hypothesis": "x", "nested": {"a": [1, 2]}},
            evidence_refs=[uuid4()],
            trust_level=TrustLevel.HIGH,
            sensitivity=SensitivityLevel.SENSITIVE,
        )

        assert _roundtrip(original) == original

    def test_enum_values_are_stored_as_strings(self) -> None:
        original = Event(
            event_type=EventType.USER_MESSAGE_RECEIVED,
            occurred_at=NOON,
            actor_type=ActorType.USER,
            actor_id="u",
        )
        row = _to_row(original)
        assert row.event_type == "user.message.received"
        assert row.actor_type == "user"
        assert row.trust_level == "medium"

    def test_model_info_roundtrip(self) -> None:
        info = ModelInvocationInfo(
            provider="mock",
            model="mock-v1",
            task_name="hypothesis_generator",
            prompt_version="1.2.0",
            started_at=NOON,
            completed_at=NOON,
            response_hash="sha256:abc",
            input_token_count=120,
            output_token_count=48,
        )
        original = Event(
            event_type=EventType.HYPOTHESIS_CREATED,
            occurred_at=NOON,
            actor_type=ActorType.MODEL,
            actor_id="m",
            model_info=info,
        )

        restored = _roundtrip(original)
        assert restored.model_info is not None
        assert restored.model_info.prompt_version == "1.2.0"
        assert restored.model_info.response_hash == "sha256:abc"
        assert restored.model_info.output_token_count == 48

    def test_model_info_is_none_when_absent(self) -> None:
        original = Event(
            event_type=EventType.USER_MESSAGE_RECEIVED,
            occurred_at=NOON,
            actor_type=ActorType.USER,
            actor_id="u",
        )
        assert _roundtrip(original).model_info is None

    def test_payload_is_copied_not_shared(self) -> None:
        """payload 必须是副本——共享引用会让后续修改互相污染。"""
        payload = {"a": 1}
        original = Event(
            event_type=EventType.USER_MESSAGE_RECEIVED,
            occurred_at=NOON,
            actor_type=ActorType.USER,
            actor_id="u",
            payload=payload,
        )
        row = _to_row(original)
        assert row.payload == payload
        assert row.payload is not payload


class TestRoundMapping:
    def test_roundtrip_preserves_every_field(self) -> None:
        original = CognitiveRound(
            created_by="orchestrator",
            user_id=uuid4(),
            conversation_id=uuid4(),
            trigger_event_id=uuid4(),
            state=RoundState.ANALYZING,
            depth_level=CognitiveDepth.D3,
            budget=CognitiveBudget.for_depth(CognitiveDepth.D3),
            model_calls_used=4,
            metacognitive_loops=1,
            idempotency_key="key-1",
            correlation_id=uuid4(),
            causation_id=uuid4(),
            started_at=NOON,
        )

        restored = row_to_round(round_to_row(original))

        assert restored.id == original.id
        assert restored.state is original.state
        assert restored.depth_level is original.depth_level
        assert restored.model_calls_used == 4
        assert restored.metacognitive_loops == 1
        assert restored.idempotency_key == "key-1"
        assert restored.budget.max_model_calls == original.budget.max_model_calls

    def test_terminal_diagnostics_roundtrip(self) -> None:
        original = CognitiveRound(
            created_by="orchestrator",
            state=RoundState.FAILED,
            failure_stage="analyzing",
            error_category=ErrorType.CALIBRATION_ERROR,
            started_at=NOON,
            completed_at=NOON,
        )
        restored = row_to_round(round_to_row(original))

        assert restored.state is RoundState.FAILED
        assert restored.failure_stage == "analyzing"
        assert restored.error_category is ErrorType.CALIBRATION_ERROR

    def test_completed_stop_reason_roundtrip(self) -> None:
        original = CognitiveRound(
            created_by="orchestrator",
            state=RoundState.COMPLETED,
            stop_reason="no_marginal_cognitive_gain",
            started_at=NOON,
            completed_at=NOON,
        )
        assert row_to_round(round_to_row(original)).stop_reason == "no_marginal_cognitive_gain"

    def test_null_fields_stay_null(self) -> None:
        original = CognitiveRound(created_by="orchestrator")
        restored = row_to_round(round_to_row(original))

        assert restored.user_id is None
        assert restored.stop_reason is None
        assert restored.failure_stage is None
        assert restored.error_category is None
        assert restored.started_at is None
        assert restored.completed_at is None

    def test_budget_survives_as_structured_data(self) -> None:
        original = CognitiveRound(
            created_by="orchestrator",
            budget=CognitiveBudget(
                max_model_calls=7,
                max_metacognitive_loops=1,
                max_hypotheses=3,
                max_retrieved_memories=11,
                max_context_tokens=9_000,
                max_duration_seconds=42,
                max_cost_units=1.5,
            ),
        )
        restored = row_to_round(round_to_row(original))
        assert restored.budget == original.budget

    def test_round_to_values_excludes_primary_key(self) -> None:
        """UPDATE 语句不能改主键——这是 round_to_values 被抽出来的原因。"""
        values = round_to_values(CognitiveRound(created_by="o"))
        assert "id" not in values
        assert "state" in values

    def test_round_to_values_covers_every_column(self) -> None:
        """🔴 新增领域字段却忘了加进映射，表现为"能存不能改"。

        这条断言让遗漏在单元测试阶段就暴露，而不是等到线上更新失效。
        """
        from ai_psi.infrastructure.db.models import CognitiveRoundRow

        mapped = set(round_to_values(CognitiveRound(created_by="o")))
        # 主键由插入路径单独处理，不在 round_to_values 里
        columns = set(CognitiveRoundRow.__table__.columns.keys()) - {"id"}
        assert mapped == columns


def _to_row(event: Event):
    from ai_psi.infrastructure.db.models import EventRow

    row = EventRow()
    apply_event(row, event)
    return row


def _roundtrip(event: Event) -> Event:
    """领域事件 → ORM 行 → 领域事件。"""
    return row_to_event(_to_row(event))
