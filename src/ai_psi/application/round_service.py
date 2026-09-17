"""认知回合并应用服务。

🔴 **本服务是回合状态变更的唯一入口**（架构规则 3）。

每一次转移都在**一个事务**内完成三件事：

1. 校验转移合法性（状态机）；
2. 写入 ``cognitive_round.state_changed`` 事件；
3. 更新当前状态投影（带乐观锁）。

三者同进同退。分开做的后果是"事件写了但状态没变"或反之，
而这类不一致在事后排查时几乎无法复原原因（ADR-0002）。

**本服务只依赖 Port**，不 import 任何 `infrastructure/`。
阶段 3 换成内存实现时，这里的代码一行都不用改（ADR-0009）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from ai_psi.application.ports import IdempotencyOutcome, UnitOfWorkFactory
from ai_psi.cognition.state_machine import assert_transition
from ai_psi.domain.cognitive_rounds import CognitiveBudget, CognitiveRound
from ai_psi.domain.common import utc_now
from ai_psi.domain.enums import (
    ActorType,
    CognitiveDepth,
    ErrorType,
    EventType,
    RoundState,
)
from ai_psi.domain.events import Event
from ai_psi.domain.exceptions import ConflictError, NotFoundError

__all__ = ["CognitiveRoundService", "RoundTransitionResult", "StartRoundResult"]


@dataclass(frozen=True, slots=True)
class StartRoundResult:
    """创建回合的结果。

    Attributes:
        round: 回合对象。幂等重放时是**已存在**的那个。
        created: ``True`` 表示本次真的创建了新回合；
            ``False`` 表示命中幂等键，返回的是既有回合。
        event: 本次追加的 ``cognitive_round.started`` 事件；重放时为 ``None``。
    """

    round: CognitiveRound
    created: bool
    event: Event | None


@dataclass(frozen=True, slots=True)
class RoundTransitionResult:
    """一次状态转移的结果。

    Attributes:
        round: 转移后的回合（版本号已递增）。
        event: 本次追加的 ``state_changed`` 事件。
    """

    round: CognitiveRound
    event: Event


class CognitiveRoundService:
    """认知回合的创建与状态转移。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        """初始化。

        Args:
            uow_factory: 工作单元工厂。**依赖工厂而非实例**——
                每次操作需要独立的事务边界。
        """
        self._uow_factory = uow_factory

    # ------------------------------------------------------------------
    # 创建
    # ------------------------------------------------------------------

    async def start_round(
        self,
        *,
        created_by: str = "orchestrator",
        user_id: UUID | None = None,
        conversation_id: UUID | None = None,
        trigger_event_id: UUID | None = None,
        depth_level: CognitiveDepth = CognitiveDepth.D0,
        budget: CognitiveBudget | None = None,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> StartRoundResult:
        """创建一个新回合，并写入起始事件与起始状态。

        🔴 **幂等**：提供 ``idempotency_key`` 时，重复提交同一请求
        不会创建第二个回合（任务书 §13.4）。同一 key 搭配不同请求体
        会抛 :class:`ConflictError`——那是客户端复用 key 的 bug，
        静默返回旧结果会掩盖它。

        Args:
            created_by: 发起者标识。
            user_id: 归属用户。
            conversation_id: 所属会话。
            trigger_event_id: 触发本回合的事件。
            depth_level: 深度路由结果。
            budget: 认知预算；``None`` 时按深度取默认值（ADR-0008）。
            correlation_id: 关联链标识。
            causation_id: 上游因由。
            idempotency_key: 客户端幂等键。
            request_hash: 请求体哈希，仅在提供幂等键时有意义。

        Returns:
            创建结果。

        Raises:
            ConflictError: 幂等键冲突（请求体不同，或上一次请求仍在进行中）。
        """
        async with self._uow_factory() as uow:
            if idempotency_key is not None:
                reservation = await uow.idempotency.reserve(
                    key=idempotency_key,
                    request_hash=request_hash or "",
                )
                if reservation.outcome is IdempotencyOutcome.CONFLICT:
                    msg = (
                        f"幂等键冲突：{idempotency_key!r} 已被使用，"
                        "但请求体不同或上一次请求尚未完成"
                    )
                    raise ConflictError(msg, context={"idempotency_key": idempotency_key})

                if reservation.outcome is IdempotencyOutcome.REPLAY:
                    existing_id = reservation.cognitive_round_id
                    if existing_id is None:  # pragma: no cover - 协议保证非空
                        msg = "幂等重放结果缺少回合 id"
                        raise ConflictError(msg, context={"idempotency_key": idempotency_key})
                    existing = await uow.rounds.get(existing_id)
                    if existing is None:  # pragma: no cover - 数据一致性异常
                        msg = f"幂等键指向的回合不存在：{existing_id}"
                        raise NotFoundError(msg, context={"cognitive_round_id": str(existing_id)})
                    return StartRoundResult(round=existing, created=False, event=None)

            round_ = CognitiveRound(
                created_by=created_by,
                user_id=user_id,
                conversation_id=conversation_id,
                trigger_event_id=trigger_event_id,
                state=RoundState.CREATED,
                depth_level=depth_level,
                budget=budget if budget is not None else CognitiveBudget.for_depth(depth_level),
                correlation_id=correlation_id or uuid4(),
                causation_id=causation_id,
                idempotency_key=idempotency_key,
            )

            started_event = _build_event(
                event_type=EventType.COGNITIVE_ROUND_STARTED,
                round_=round_,
                actor_id=created_by,
                causation_id=trigger_event_id,
                payload={
                    "state": round_.state.value,
                    "depth_level": round_.depth_level.value,
                    "budget": round_.budget.model_dump(mode="json"),
                },
            )

            await uow.rounds.add(round_)
            await uow.events.append(started_event)

            if idempotency_key is not None:
                await uow.idempotency.bind(key=idempotency_key, cognitive_round_id=round_.id)

            await uow.commit()

        return StartRoundResult(round=round_, created=True, event=started_event)

    # ------------------------------------------------------------------
    # 状态转移
    # ------------------------------------------------------------------

    async def transition(
        self,
        round_id: UUID,
        to_state: RoundState,
        *,
        reason: str,
        actor_id: str = "orchestrator",
        stop_reason: str | None = None,
        failure_stage: str | None = None,
        error_category: ErrorType | None = None,
        model_calls_used: int | None = None,
        metacognitive_loops: int | None = None,
        budget: CognitiveBudget | None = None,
        depth_level: CognitiveDepth | None = None,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> RoundTransitionResult:
        """把回合推进到新状态。

        Args:
            round_id: 回合 id。
            to_state: 目标状态。
            reason: 转移原因，写入事件负载。
            actor_id: 执行者标识。
            stop_reason: 停止原因。``COMPLETED`` 时**必填**（不变量 19）。
            failure_stage: 失败阶段。``FAILED`` 时**必填**（不变量 20）。
            error_category: 错误类别。``FAILED`` 时**必填**（不变量 20）。
            model_calls_used: 覆盖已消耗的模型调用数。
            metacognitive_loops: 覆盖已执行的元认知循环轮数。
            budget: 覆盖回合预算。深度路由在 ``FRAMING`` 阶段才最终定级，
                此时把预算从初始上限**下调**到该深度的真实预算。
                若已消耗的调用数超过新预算，模型校验器会拒绝这次转移——
                宁可保留较宽的预算，也不能出现"预算比已花费还小"的回合。
            depth_level: 覆盖认知深度（深度路由的最终结果）。
            diagnostics: 附加诊断信息，合并进事件负载。
                用于记录"这一步为什么没做"这类**降级事实**——
                降级本身可以接受，但必须留下痕迹，否则事后无从分辨
                "系统少做了一个分析"与"分析跑了但没产出"。

        Returns:
            转移结果。

        Raises:
            NotFoundError: 回合不存在。
            IllegalStateTransitionError: 该转移不在权威转移表中（不变量 17）。
            OptimisticLockError: 期间有其他写入者提交了变更。
        """
        async with self._uow_factory() as uow:
            current = await uow.rounds.get(round_id)
            if current is None:
                msg = f"认知回合不存在：{round_id}"
                raise NotFoundError(msg, context={"cognitive_round_id": str(round_id)})

            # 🔴 不变量 17：非法转移在这里被拒绝，且**发生在任何写入之前**
            assert_transition(current.state, to_state)

            changes: dict[str, Any] = {
                "state": to_state,
                # 离开 CREATED 即视为开始处理
                "started_at": current.started_at
                or (utc_now() if current.state is RoundState.CREATED else None),
            }
            if stop_reason is not None:
                changes["stop_reason"] = stop_reason
            if failure_stage is not None:
                changes["failure_stage"] = failure_stage
            if error_category is not None:
                changes["error_category"] = error_category
            if model_calls_used is not None:
                changes["model_calls_used"] = model_calls_used
            if metacognitive_loops is not None:
                changes["metacognitive_loops"] = metacognitive_loops
            if budget is not None:
                changes["budget"] = budget
            if depth_level is not None:
                changes["depth_level"] = depth_level
            if to_state.is_terminal:
                changes["completed_at"] = utc_now()

            # bumped() 递增版本号并刷新 updated_at；
            # 领域模型的校验器会在这一步拦住"完成却没有停止原因"（不变量 19）
            updated = current.bumped(**changes)

            event = _build_event(
                event_type=EventType.COGNITIVE_ROUND_STATE_CHANGED,
                round_=updated,
                actor_id=actor_id,
                payload={
                    "from_state": current.state.value,
                    "to_state": to_state.value,
                    "reason": reason,
                    "stop_reason": updated.stop_reason,
                    "failure_stage": updated.failure_stage,
                    "error_category": (
                        updated.error_category.value if updated.error_category is not None else None
                    ),
                    "budget_snapshot": {
                        "model_calls_used": updated.model_calls_used,
                        "metacognitive_loops": updated.metacognitive_loops,
                        "max_model_calls": updated.budget.max_model_calls,
                        "max_metacognitive_loops": updated.budget.max_metacognitive_loops,
                        "depth_level": updated.depth_level.value,
                    },
                    **dict(diagnostics or {}),
                },
            )

            await uow.events.append(event)

            # 🔴 终态额外写一条**具名**事件。
            #
            # 只有 state_changed 的话，"找出所有失败的回合"就必须解析
            # 每条事件的负载才知道它到没到终态；而任务书 §5.2 明确列出了
            # cognitive_round.failed / completed / suspended / cancelled
            # 四种事件，它们在阶段 2 里从未被发出过——是**死的词汇表**。
            #
            # 两条事件在同一个事务里写入，因此不会出现"状态变了但没有终态事件"。
            terminal_type = _TERMINAL_EVENT_TYPES.get(to_state)
            if terminal_type is not None:
                await uow.events.append(
                    _build_event(
                        event_type=terminal_type,
                        round_=updated,
                        actor_id=actor_id,
                        causation_id=event.id,
                        payload={
                            "from_state": current.state.value,
                            "to_state": to_state.value,
                            "reason": reason,
                            "stop_reason": updated.stop_reason,
                            "failure_stage": updated.failure_stage,
                            "error_category": (
                                updated.error_category.value
                                if updated.error_category is not None
                                else None
                            ),
                            # 🔴 诊断信息也要进**终态**事件：读摘要的人
                            # 只关心终态那一条，不会去翻中间转移。
                            **dict(diagnostics or {}),
                        },
                    )
                )

            await uow.rounds.save(updated, expected_version=current.version)
            await uow.commit()

        return RoundTransitionResult(round=updated, event=event)

    # ------------------------------------------------------------------
    # 三个终态的语义化入口
    # ------------------------------------------------------------------

    async def complete_round(
        self,
        round_id: UUID,
        *,
        stop_reason: str,
        actor_id: str = "orchestrator",
    ) -> RoundTransitionResult:
        """标记回合完成。

        Args:
            round_id: 回合 id。
            stop_reason: **停止原因（必填）**——不变量 19 要求
                每个完成的回合都能回答"为什么停下来"。
            actor_id: 执行者标识。

        Returns:
            转移结果。
        """
        return await self.transition(
            round_id,
            RoundState.COMPLETED,
            reason=stop_reason,
            actor_id=actor_id,
            stop_reason=stop_reason,
        )

    async def fail_round(
        self,
        round_id: UUID,
        *,
        stage: str,
        error_category: ErrorType,
        reason: str,
        actor_id: str = "orchestrator",
    ) -> RoundTransitionResult:
        """标记回合失败。

        任务书 §6.3 要求"回合失败不得破坏已写入事件"——
        本方法只追加失败事件，不触碰任何历史事件。

        Args:
            round_id: 回合 id。
            stage: 失败阶段（必填）。
            error_category: 错误类别（必填）。
            reason: 人类可读的失败说明。
            actor_id: 执行者标识。

        Returns:
            转移结果。
        """
        return await self.transition(
            round_id,
            RoundState.FAILED,
            reason=reason,
            actor_id=actor_id,
            failure_stage=stage,
            error_category=error_category,
        )

    async def cancel_round(
        self,
        round_id: UUID,
        *,
        reason: str,
        actor_id: str = "orchestrator",
    ) -> RoundTransitionResult:
        """取消回合。

        Args:
            round_id: 回合 id。
            reason: 取消原因。
            actor_id: 执行者标识。

        Returns:
            转移结果。
        """
        return await self.transition(
            round_id, RoundState.CANCELLED, reason=reason, actor_id=actor_id
        )


#: 终态到**具名**终态事件的映射。
#:
#: 任务书 §5.2 列出了这四种事件；阶段 2 把全部转移统一写成
#: ``cognitive_round.state_changed``，于是这四种类型从未被发出。
#: 阶段 3 的失败路径测试暴露了这个缺口（ADR-0015）。
_TERMINAL_EVENT_TYPES: dict[RoundState, EventType] = {
    RoundState.COMPLETED: EventType.COGNITIVE_ROUND_COMPLETED,
    RoundState.FAILED: EventType.COGNITIVE_ROUND_FAILED,
    RoundState.SUSPENDED: EventType.COGNITIVE_ROUND_SUSPENDED,
    RoundState.CANCELLED: EventType.COGNITIVE_ROUND_CANCELLED,
}


def _build_event(
    *,
    event_type: EventType,
    round_: CognitiveRound,
    actor_id: str,
    payload: dict[str, Any],
    causation_id: UUID | None = None,
) -> Event:
    """构造一个属于该回合的事件。

    Args:
        event_type: 事件类型。
        round_: 所属回合。
        actor_id: 触发者标识。
        payload: 事件负载，调用方负责确保已脱敏。
        causation_id: 上游因由；``None`` 时沿用回合的触发事件。

    Returns:
        领域事件对象。
    """
    return Event(
        event_type=event_type,
        occurred_at=utc_now(),
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        user_id=round_.user_id,
        conversation_id=round_.conversation_id,
        cognitive_round_id=round_.id,
        correlation_id=round_.correlation_id or uuid4(),
        causation_id=causation_id if causation_id is not None else round_.trigger_event_id,
        payload=payload,
    )
