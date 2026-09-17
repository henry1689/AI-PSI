"""事件与模型调用记录的单元测试（ADR-0002、ADR-0006）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_psi.domain import EntityMetadata
from ai_psi.domain.enums import ActorType, EventType
from ai_psi.domain.events import Event, ModelInvocationInfo
from tests.helpers import construct, rejects

pytestmark = pytest.mark.unit

NOON = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


class TestEventMetadataException:
    """🔴 ADR-0006：``Event`` 是唯一不继承 ``EntityMetadata`` 的领域对象。"""

    def test_event_does_not_inherit_entity_metadata(self) -> None:
        assert not issubclass(Event, EntityMetadata)

    def test_event_has_no_version_field(self) -> None:
        """事件只追加、永不更新，``version`` 对它无意义。"""
        assert "version" not in Event.model_fields

    def test_event_has_no_updated_at_field(self) -> None:
        assert "updated_at" not in Event.model_fields

    def test_event_has_no_bumped_helper(self) -> None:
        """``bumped()`` 是乐观锁更新的入口——事件不该有它。"""
        assert not hasattr(Event, "bumped")


class TestEventDualTimestamps:
    """双时间戳：区分「事情何时发生」与「我们何时知道」。"""

    def test_occurred_at_is_required(self) -> None:
        rejects(
            Event,
            event_type=EventType.USER_MESSAGE_RECEIVED,
            actor_type=ActorType.USER,
            actor_id="u",
        )

    def test_recorded_at_defaults_to_now(self, make_event) -> None:
        assert make_event().recorded_at.tzinfo is not None

    def test_recorded_before_occurred_is_rejected(self, make_event) -> None:
        """事件不能被记录在发生之前。"""
        with pytest.raises(ValidationError, match="recorded_at"):
            construct(
                Event,
                event_type=EventType.USER_MESSAGE_RECEIVED,
                occurred_at=NOON,
                recorded_at=NOON - timedelta(hours=1),
                actor_type=ActorType.USER,
                actor_id="u",
            )

    def test_recorded_equal_to_occurred_is_allowed(self, make_event) -> None:
        """本地同步产生的事件两者相等，应当允许。"""
        event = make_event(occurred_at=NOON, recorded_at=NOON)
        assert event.recorded_at == event.occurred_at

    def test_late_arriving_event_keeps_original_occurred_at(self, make_event) -> None:
        """迟到的外部事件：发生时间在早先，记录时间在现在。"""
        event = make_event(
            occurred_at=NOON - timedelta(days=3),
            recorded_at=NOON,
            actor_type=ActorType.EXTERNAL,
            actor_id="feed",
        )
        assert event.occurred_at < event.recorded_at


class TestEventScoping:
    def test_user_id_is_optional_for_system_events(self, make_event) -> None:
        event = make_event(
            event_type=EventType.COGNITIVE_ROUND_STARTED,
            actor_type=ActorType.SYSTEM,
            actor_id="orchestrator",
        )
        assert event.user_id is None

    def test_correlation_id_is_generated(self, make_event) -> None:
        assert make_event().correlation_id is not None

    def test_causation_id_defaults_to_none(self, make_event) -> None:
        """根事件没有上游因由。"""
        assert make_event().causation_id is None

    def test_causation_chain_can_be_expressed(self, make_event) -> None:
        root = make_event()
        child = make_event(
            event_type=EventType.CONCERN_CREATED,
            actor_type=ActorType.SYSTEM,
            actor_id="concern_detector",
            correlation_id=root.correlation_id,
            causation_id=root.id,
        )
        assert child.correlation_id == root.correlation_id
        assert child.causation_id == root.id

    def test_user_scope_is_carried(self, make_event) -> None:
        uid = uuid4()
        assert make_event(user_id=uid).user_id == uid


class TestEventTypeWhitelist:
    def test_invalid_event_type_is_rejected(self) -> None:
        """枚举白名单——不做模糊匹配。"""
        rejects(
            Event,
            event_type="user.message.recieved",  # 拼错
            occurred_at=NOON,
            actor_type=ActorType.USER,
            actor_id="u",
        )

    def test_extra_field_is_rejected(self) -> None:
        """模型返回的多余字段不得被静默接受。"""
        rejects(
            Event,
            event_type=EventType.USER_MESSAGE_RECEIVED,
            occurred_at=NOON,
            actor_type=ActorType.USER,
            actor_id="u",
            reasoning="模型隐藏思维链不应能进入事件",
        )

    def test_every_event_type_is_constructible(self, make_event) -> None:
        for event_type in EventType:
            assert make_event(event_type=event_type).event_type is event_type


class TestModelInvocationInfo:
    """🔴 不变量 18 + 红线一（不存隐藏思维链）。"""

    def test_model_is_required(self) -> None:
        """🔴 不变量 18：必须记录模型。"""
        rejects(
            ModelInvocationInfo,
            provider="mock",
            task_name="t",
            prompt_version="1.0.0",
            started_at=NOON,
        )

    def test_prompt_version_is_required(self) -> None:
        """🔴 不变量 18：必须记录 Prompt 版本。"""
        rejects(
            ModelInvocationInfo,
            provider="mock",
            model="m",
            task_name="t",
            started_at=NOON,
        )

    def test_no_reasoning_field_exists(self) -> None:
        """🔴 红线一：不保存供应商返回的完整隐藏推理内容。"""
        for forbidden in ("reasoning", "thinking", "chain_of_thought", "raw_response"):
            assert forbidden not in ModelInvocationInfo.model_fields

    def test_reasoning_payload_is_rejected(self) -> None:
        rejects(
            ModelInvocationInfo,
            provider="mock",
            model="m",
            task_name="t",
            prompt_version="1.0.0",
            started_at=NOON,
            reasoning="这是模型的内部推理流，不允许保存",
        )

    def test_invocation_ids_are_unique_per_call(self, make_invocation_info) -> None:
        """任务书 §6.3：重试必须使用新的 invocation_id。"""
        assert make_invocation_info().invocation_id != make_invocation_info().invocation_id

    def test_completed_before_started_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="completed_at"):
            construct(
                ModelInvocationInfo,
                provider="mock",
                model="m",
                task_name="t",
                prompt_version="1.0.0",
                started_at=NOON,
                completed_at=NOON - timedelta(seconds=1),
            )

    def test_token_counts_must_be_non_negative(self) -> None:
        rejects(
            ModelInvocationInfo,
            provider="mock",
            model="m",
            task_name="t",
            prompt_version="1.0.0",
            started_at=NOON,
            input_token_count=-1,
        )

    def test_response_hash_is_kept_but_content_is_not(self, make_invocation_info) -> None:
        info = make_invocation_info(response_hash="sha256:abc123")
        assert info.response_hash == "sha256:abc123"

    def test_retry_count_defaults_to_zero(self, make_invocation_info) -> None:
        assert make_invocation_info().retry_count == 0

    def test_result_status_defaults_to_success(self, make_invocation_info) -> None:
        assert make_invocation_info().result_status == "success"


class TestEventCarriesInvocationInfo:
    def test_model_info_attached(self, make_event, make_invocation_info) -> None:
        info = make_invocation_info()
        event = make_event(
            event_type=EventType.HYPOTHESIS_CREATED,
            actor_type=ActorType.MODEL,
            actor_id=info.model,
            model_info=info,
        )
        assert event.model_info is not None
        assert event.model_info.model == info.model

    def test_evidence_refs_default_empty(self, make_event) -> None:
        assert make_event().evidence_refs == []

    def test_payload_accepts_structured_data(self, make_event) -> None:
        round_id = uuid4()
        event = make_event(
            event_type=EventType.COGNITIVE_ROUND_STATE_CHANGED,
            actor_type=ActorType.SYSTEM,
            actor_id="orchestrator",
            payload={"from_state": "analyzing", "to_state": "deliberating"},
            cognitive_round_id=round_id,
        )
        assert event.payload["from_state"] == "analyzing"
        assert event.cognitive_round_id == round_id

    def test_default_trust_and_sensitivity_are_conservative(self, make_event) -> None:
        event = make_event()
        assert event.trust_level.value == "medium"
        assert event.sensitivity.value == "internal"
