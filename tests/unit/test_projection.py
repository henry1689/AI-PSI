"""事件流投影的单元测试（纯函数，零 IO）。

🔴 投影同时是一次**审计**：它不只重建状态，还逐条验证转移是否合法。
因此这里既测"正常重建"，也测"事件流自相矛盾时必须报错"。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from ai_psi.cognition.projection import project_round
from ai_psi.domain.enums import ErrorType, EventType, RoundState
from ai_psi.domain.events import Event
from ai_psi.domain.exceptions import DomainError, IllegalStateTransitionError
from tests.helpers import construct

pytestmark = pytest.mark.unit

NOON = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _started(round_id: object) -> Event:
    return construct(
        Event,
        event_type=EventType.COGNITIVE_ROUND_STARTED,
        occurred_at=NOON,
        actor_type="system",
        actor_id="test",
        cognitive_round_id=round_id,
    )


def _state_changed(round_id: object, from_state: str, to_state: str, **extra: object) -> Event:
    return construct(
        Event,
        event_type=EventType.COGNITIVE_ROUND_STATE_CHANGED,
        occurred_at=NOON,
        actor_type="system",
        actor_id="test",
        cognitive_round_id=round_id,
        payload={"from_state": from_state, "to_state": to_state, "reason": "test", **extra},
    )


class TestHappyPath:
    def test_minimal_stream_projects_to_created(self) -> None:
        round_id = uuid4()
        projection = project_round([_started(round_id)])

        assert projection.cognitive_round_id == round_id
        assert projection.state is RoundState.CREATED
        assert projection.transition_count == 0
        assert projection.event_count == 1

    def test_transition_chain_is_rebuilt(self) -> None:
        round_id = uuid4()
        events = [
            _started(round_id),
            _state_changed(round_id, "created", "triaging"),
            _state_changed(round_id, "triaging", "framing"),
            _state_changed(round_id, "framing", "retrieving"),
        ]
        projection = project_round(events)

        assert projection.state is RoundState.RETRIEVING
        assert [(t.from_state, t.to_state) for t in projection.transitions] == [
            (RoundState.CREATED, RoundState.TRIAGING),
            (RoundState.TRIAGING, RoundState.FRAMING),
            (RoundState.FRAMING, RoundState.RETRIEVING),
        ]

    def test_non_state_events_are_ignored_for_state_but_counted(self) -> None:
        round_id = uuid4()
        events = [
            _started(round_id),
            construct(
                Event,
                event_type=EventType.EVIDENCE_ATTACHED,
                occurred_at=NOON,
                actor_type="system",
                actor_id="test",
                cognitive_round_id=round_id,
            ),
        ]
        projection = project_round(events)
        assert projection.state is RoundState.CREATED
        assert projection.event_count == 2

    def test_reason_is_carried_per_transition(self) -> None:
        round_id = uuid4()
        events = [
            _started(round_id),
            construct(
                Event,
                event_type=EventType.COGNITIVE_ROUND_STATE_CHANGED,
                occurred_at=NOON,
                actor_type="system",
                actor_id="test",
                cognitive_round_id=round_id,
                payload={
                    "from_state": "created",
                    "to_state": "triaging",
                    "reason": "关切识别完成",
                },
            ),
        ]
        projection = project_round(events)
        assert projection.transitions[0].reason == "关切识别完成"


class TestTerminalDiagnostics:
    def test_completed_stop_reason_is_recovered(self) -> None:
        round_id = uuid4()
        events = [
            _started(round_id),
            _state_changed(round_id, "created", "triaging"),  # 只为制造一个非终态历史
            _state_changed(
                round_id,
                "triaging",
                "cancelled",
                stop_reason="user_cancelled",
            ),
        ]
        projection = project_round(events)
        assert projection.state is RoundState.CANCELLED
        assert projection.is_terminal
        assert projection.stop_reason == "user_cancelled"

    def test_failure_diagnostics_are_recovered(self) -> None:
        round_id = uuid4()
        events = [
            _started(round_id),
            _state_changed(
                round_id,
                "created",
                "failed",
                failure_stage="triaging",
                error_category="process_error",
            ),
        ]
        projection = project_round(events)

        assert projection.state is RoundState.FAILED
        assert projection.failure_stage == "triaging"
        assert projection.error_category is ErrorType.PROCESS_ERROR


class TestInvalidStreams:
    def test_empty_stream_is_rejected(self) -> None:
        with pytest.raises(DomainError, match="事件流为空"):
            project_round([])

    def test_multi_round_stream_is_rejected(self) -> None:
        events = [_started(uuid4()), _started(uuid4())]
        with pytest.raises(DomainError, match="跨越了多个回合"):
            project_round(events)

    def test_round_less_event_is_rejected(self) -> None:
        event = construct(
            Event,
            event_type=EventType.USER_MESSAGE_RECEIVED,
            occurred_at=NOON,
            actor_type="user",
            actor_id="u",
        )
        with pytest.raises(DomainError, match="不属于任何回合"):
            project_round([event])


class TestProjectionAsAudit:
    """🔴 回放即审计：非法历史必须报错，而不是被忠实重现。"""

    def test_illegal_transition_in_stream_is_rejected(self) -> None:
        round_id = uuid4()
        events = [
            _started(round_id),
            _state_changed(round_id, "created", "completed"),
        ]
        with pytest.raises(IllegalStateTransitionError):
            project_round(events)

    def test_self_contradictory_stream_is_rejected(self) -> None:
        """事件的 from_state 与重建出的当前状态不一致。"""
        round_id = uuid4()
        events = [
            _started(round_id),
            _state_changed(round_id, "analyzing", "deliberating"),
        ]
        with pytest.raises(DomainError, match="自相矛盾"):
            project_round(events)

    def test_missing_from_state_is_rejected(self) -> None:
        round_id = uuid4()
        event = construct(
            Event,
            event_type=EventType.COGNITIVE_ROUND_STATE_CHANGED,
            occurred_at=NOON,
            actor_type="system",
            actor_id="test",
            cognitive_round_id=round_id,
            payload={"to_state": "triaging"},
        )
        with pytest.raises(DomainError, match="缺少字段"):
            project_round([_started(round_id), event])

    def test_invalid_state_string_is_rejected(self) -> None:
        round_id = uuid4()
        events = [
            _started(round_id),
            _state_changed(round_id, "created", "thinking_very_hard"),
        ]
        with pytest.raises(DomainError, match="不是合法状态"):
            project_round(events)

    def test_duplicate_adjacent_transition_is_rejected(self) -> None:
        """同一转移出现两次会让状态链对不上。"""
        round_id = uuid4()
        events = [
            _started(round_id),
            _state_changed(round_id, "created", "triaging"),
            _state_changed(round_id, "created", "triaging"),
        ]
        with pytest.raises(DomainError, match="自相矛盾"):
            project_round(events)

    def test_terminal_state_cannot_be_left(self) -> None:
        round_id = uuid4()
        events = [
            _started(round_id),
            _state_changed(round_id, "created", "cancelled"),
            _state_changed(round_id, "cancelled", "triaging"),
        ]
        with pytest.raises(IllegalStateTransitionError):
            project_round(events)
