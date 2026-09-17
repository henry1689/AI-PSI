"""事件流投影——从事件重建认知回合的状态。

🔴 **纯函数，零 IO。** 输入是一段按 ``sequence`` 升序排列的事件，
输出是重建出的回放结果。它不碰数据库、不知道仓储存在。

**投影同时是一次校验。** 回放时不只重建状态，还逐条验证每次转移是否合法：

* 事件的 ``from_state`` 必须等于当前状态（否则事件流本身自相矛盾）；
* 转移必须在 :data:`~ai_psi.cognition.state_machine.TRANSITIONS` 中。

这样"回放"就不只是重现历史，而是**对历史的一致性审计**——
如果某次写入绕过了状态机，回放会直接报错，而不是忠实重现一个非法状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from ai_psi.cognition.state_machine import assert_transition
from ai_psi.domain.enums import ErrorType, EventType, RoundState
from ai_psi.domain.events import Event, ModelInvocationInfo
from ai_psi.domain.exceptions import DomainError

__all__ = [
    "ArtifactRecord",
    "ArtifactView",
    "RoundProjection",
    "Transition",
    "project_artifacts",
    "project_round",
]


@dataclass(frozen=True, slots=True)
class Transition:
    """一次状态转移。"""

    from_state: RoundState
    to_state: RoundState
    reason: str


@dataclass(frozen=True, slots=True)
class RoundProjection:
    """从事件流重建出的回合视图。

    Attributes:
        cognitive_round_id: 回合 id。
        state: 重建出的当前状态。
        transitions: 按顺序的转移列表。
        event_count: 参与投影的事件总数。
        stop_reason: 停止原因（不变量 19 的回放侧校验依据）。
        failure_stage: 失败阶段（不变量 20）。
        error_category: 错误类别（不变量 20）。
    """

    cognitive_round_id: UUID
    state: RoundState
    transitions: tuple[Transition, ...]
    event_count: int
    stop_reason: str | None
    failure_stage: str | None
    error_category: ErrorType | None

    @property
    def is_terminal(self) -> bool:
        """重建出的状态是否为终态。"""
        return self.state.is_terminal

    @property
    def transition_count(self) -> int:
        """状态转移次数。"""
        return len(self.transitions)


def project_round(events: list[Event]) -> RoundProjection:
    """把事件流投影为回合视图，并校验转移的合法性。

    Args:
        events: 按 ``sequence`` 升序排列的事件。顺序由事件存储保证
            （见 :meth:`~ai_psi.application.ports.EventStore.read_stream`）——
            时间戳精度不足以在同一微秒内定序，因此**不能**用 ``recorded_at`` 排序。

    Returns:
        重建出的回合视图。

    Raises:
        DomainError: 事件流为空、事件不属于同一回合，或事件流内部自相矛盾。
        IllegalStateTransitionError: 事件流中包含非法的状态转移——
            这说明有写入绕过了状态机。
    """
    if not events:
        msg = "事件流为空，无法投影。至少需要一条 cognitive_round.started 事件"
        raise DomainError(msg)

    round_ids = {event.cognitive_round_id for event in events}
    if len(round_ids) > 1:
        msg = f"事件流跨越了多个回合，无法投影：{sorted(str(i) for i in round_ids)}"
        raise DomainError(msg)
    if None in round_ids:
        msg = "事件流中存在不属于任何回合的事件"
        raise DomainError(msg)

    round_id = next(iter(round_ids))
    assert round_id is not None  # 已在上方排除 None

    state = RoundState.CREATED
    transitions: list[Transition] = []
    stop_reason: str | None = None
    failure_stage: str | None = None
    error_category: ErrorType | None = None

    for event in events:
        if event.event_type is not EventType.COGNITIVE_ROUND_STATE_CHANGED:
            continue

        payload: dict[str, Any] = dict(event.payload)
        from_state = _parse_state(payload, "from_state", event)
        to_state = _parse_state(payload, "to_state", event)
        reason = str(payload.get("reason", ""))

        if from_state is not state:
            msg = (
                f"事件流自相矛盾：事件 {event.id} 声明从 {from_state.value} 转移，"
                f"但重建出的当前状态是 {state.value}"
            )
            raise DomainError(msg, context={"event_id": str(event.id)})

        # 🔴 回放即审计：非法转移在这里被拒绝，而不是被忠实重现
        assert_transition(from_state, to_state)
        transitions.append(Transition(from_state=from_state, to_state=to_state, reason=reason))
        state = to_state

        stop_reason = payload.get("stop_reason", stop_reason)
        failure_stage = payload.get("failure_stage", failure_stage)
        raw_category = payload.get("error_category")
        if raw_category is not None:
            error_category = ErrorType(raw_category)

    return RoundProjection(
        cognitive_round_id=round_id,
        state=state,
        transitions=tuple(transitions),
        event_count=len(events),
        stop_reason=stop_reason,
        failure_stage=failure_stage,
        error_category=error_category,
    )


def _parse_state(payload: dict[str, Any], key: str, event: Event) -> RoundState:
    """从 payload 解析状态字段。

    Args:
        payload: 事件负载。
        key: 字段名。
        event: 事件本身，用于错误上下文。

    Returns:
        解析出的状态。

    Raises:
        DomainError: 字段缺失或不是合法状态。
    """
    raw = payload.get(key)
    if raw is None:
        msg = f"状态转移事件缺少字段 {key!r}：{event.id}"
        raise DomainError(msg, context={"event_id": str(event.id), "missing_field": key})
    try:
        return RoundState(raw)
    except ValueError as exc:
        msg = f"状态转移事件中的 {key!r} 不是合法状态：{raw!r}"
        raise DomainError(
            msg, context={"event_id": str(event.id), "invalid_value": str(raw)}
        ) from exc


# ---------------------------------------------------------------------------
# 认知产物的投影
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """事件流中的一条认知产物记录。

    ``payload`` 保持**原始字典**，不在投影阶段还原成领域对象：
    投影是只读审计视图，重新构造领域对象既昂贵又在语义上多余
    （ADR-0006 反对无谓的三层互转）。
    """

    event_type: EventType
    occurred_at: datetime
    actor_id: str
    payload: dict[str, Any]
    model_info: ModelInvocationInfo | None


@dataclass(frozen=True, slots=True)
class ArtifactView:
    """一个回合的全部认知产物（按事件顺序）。"""

    cognitive_round_id: UUID
    records: tuple[ArtifactRecord, ...]

    def of_type(self, *types: EventType) -> tuple[ArtifactRecord, ...]:
        """按事件类型筛选。

        Args:
            *types: 目标事件类型。

        Returns:
            命中的记录，保持事件顺序。
        """
        wanted = set(types)
        return tuple(record for record in self.records if record.event_type in wanted)

    @property
    def response_text(self) -> str | None:
        """最后一次渲染出的回答文本。"""
        rendered = self.of_type(EventType.RESPONSE_GENERATED)
        if not rendered:
            return None
        value = rendered[-1].payload.get("text")
        return None if value is None else str(value)

    @property
    def latest_judgment(self) -> dict[str, Any] | None:
        """最后一次判断的原始负载（可能因认知循环而有多条）。"""
        judgments = self.of_type(EventType.JUDGMENT_CREATED)
        if not judgments:
            return None
        payload = judgments[-1].payload.get("judgment")
        return payload if isinstance(payload, dict) else None

    @property
    def all_judgments(self) -> tuple[dict[str, Any], ...]:
        """全部判断负载——认知循环会产出多条，保留它们是审计信息。"""
        return tuple(
            payload
            for record in self.of_type(EventType.JUDGMENT_CREATED)
            if isinstance(payload := record.payload.get("judgment"), dict)
        )

    @property
    def latest_reflection(self) -> dict[str, Any] | None:
        """最后一次元认知反思的原始负载。"""
        reflections = self.of_type(EventType.METACOGNITION_COMPLETED)
        if not reflections:
            return None
        payload = reflections[-1].payload.get("reflection")
        return payload if isinstance(payload, dict) else None

    @property
    def model_invocations(self) -> tuple[ModelInvocationInfo, ...]:
        """本回合全部模型调用记录（不变量 18 的审计出口）。

        🔴 只包含**元信息**：模型名、Prompt 版本、耗时、响应哈希与重试次数。
        不含任何响应内容。
        """
        return tuple(record.model_info for record in self.records if record.model_info is not None)


def project_artifacts(events: list[Event]) -> ArtifactView:
    """把事件流投影为认知产物视图。

    Args:
        events: 按 ``sequence`` 升序排列的事件。

    Returns:
        产物视图。

    Raises:
        DomainError: 事件流为空或不属于同一回合。
    """
    if not events:
        msg = "事件流为空，无法投影认知产物"
        raise DomainError(msg)

    round_ids = {event.cognitive_round_id for event in events}
    if len(round_ids) > 1 or None in round_ids:
        msg = f"事件流跨越了多个回合或存在无回合事件，无法投影：{sorted(map(str, round_ids))}"
        raise DomainError(msg)

    return ArtifactView(
        cognitive_round_id=next(iter(round_ids)),  # type: ignore[arg-type]
        records=tuple(
            ArtifactRecord(
                event_type=event.event_type,
                occurred_at=event.occurred_at,
                actor_id=event.actor_id,
                payload=dict(event.payload),
                model_info=event.model_info,
            )
            for event in events
        ),
    )
