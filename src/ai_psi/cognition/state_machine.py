"""认知回合状态机（纯转移逻辑）。

权威定义见 ``docs/state_machine.md``。**代码与文档必须一致**，
由 ``tests/unit/test_state_machine.py`` 逐条覆盖。

本模块是**纯函数**：不碰数据库、不做 IO、不持有状态
（架构规则 2）。仓储接入、事件写入与乐观锁在阶段 2 由
``application/`` 层负责。

🔴 不变量 17：状态机不得跳过禁止跳过的状态。
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from ai_psi.domain.enums import MetacognitiveDecision, RoundState
from ai_psi.domain.exceptions import IllegalStateTransitionError

__all__ = [
    "STATE_TIMEOUTS_SECONDS",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "allowed_transitions",
    "assert_transition",
    "can_transition",
    "targets_for_decision",
    "timeout_for",
]

#: 终态集合。终态不可再转移——重新激活需要创建一个**新回合**，
#: 并以 ``causation_id`` 指向原回合，以保持审计链完整。
TERMINAL_STATES: Final[frozenset[RoundState]] = frozenset(
    state for state in RoundState if state.is_terminal
)

#: **唯一权威**的状态转移表（``docs/state_machine.md`` §2）。
#:
#: 每个状态映射到它**允许**转移到的状态集合。
#: 不在表中的转移一律非法。
TRANSITIONS: Final[MappingProxyType[RoundState, frozenset[RoundState]]] = MappingProxyType(
    {
        RoundState.CREATED: frozenset(
            {RoundState.TRIAGING, RoundState.FAILED, RoundState.CANCELLED}
        ),
        RoundState.TRIAGING: frozenset(
            {
                RoundState.FRAMING,
                # 关切识别为空时的快捷路径：没有值得启动认知的关切，
                # 直接进入合成（通常是 D0 直答）。**只有 TRIAGING 能走这条边。**
                RoundState.SYNTHESIZING,
                RoundState.FAILED,
                RoundState.CANCELLED,
            }
        ),
        RoundState.FRAMING: frozenset(
            {RoundState.RETRIEVING, RoundState.FAILED, RoundState.CANCELLED}
        ),
        RoundState.RETRIEVING: frozenset(
            {
                RoundState.ANALYZING,
                RoundState.WAITING_FOR_EVIDENCE,
                RoundState.FAILED,
                RoundState.CANCELLED,
            }
        ),
        RoundState.ANALYZING: frozenset(
            {
                RoundState.DELIBERATING,
                # 分析中发现上下文不足，回到检索（由 CHANGE_METHOD 触发）
                RoundState.RETRIEVING,
                RoundState.FAILED,
                RoundState.CANCELLED,
            }
        ),
        RoundState.DELIBERATING: frozenset(
            {
                RoundState.METACOGNITIVE_REVIEW,
                RoundState.ANALYZING,
                RoundState.FAILED,
                RoundState.CANCELLED,
            }
        ),
        RoundState.METACOGNITIVE_REVIEW: frozenset(
            {
                RoundState.ANALYZING,
                RoundState.DELIBERATING,
                RoundState.RETRIEVING,
                RoundState.SYNTHESIZING,
                RoundState.WAITING_FOR_EVIDENCE,
                RoundState.SUSPENDED,
                RoundState.FAILED,
                RoundState.CANCELLED,
            }
        ),
        RoundState.SYNTHESIZING: frozenset(
            {RoundState.RESPONDING, RoundState.FAILED, RoundState.CANCELLED}
        ),
        RoundState.RESPONDING: frozenset(
            {RoundState.COMPLETED, RoundState.FAILED, RoundState.CANCELLED}
        ),
        RoundState.WAITING_FOR_EVIDENCE: frozenset(
            # 等待只能被"证据到达"唤醒，或被取消。
            # 不允许直接跳到分析或合成——那等于跳过了重新检索。
            {RoundState.RETRIEVING, RoundState.CANCELLED}
        ),
        # 终态：无任何出口
        RoundState.COMPLETED: frozenset(),
        RoundState.SUSPENDED: frozenset(),
        RoundState.FAILED: frozenset(),
        RoundState.CANCELLED: frozenset(),
    }
)

#: 各状态的超时秒数（ADR-0008）。``None`` 表示无超时。
STATE_TIMEOUTS_SECONDS: Final[MappingProxyType[RoundState, int | None]] = MappingProxyType(
    {
        RoundState.CREATED: 5,
        RoundState.TRIAGING: 15,
        RoundState.FRAMING: 30,
        RoundState.RETRIEVING: 45,
        RoundState.ANALYZING: 60,
        RoundState.DELIBERATING: 60,
        # 🔴 元认知超时**不判失败**——降级为强制 STOP 进入 SYNTHESIZING。
        # 丢弃整个回合的代价远大于"用现有材料合成答案"。
        RoundState.METACOGNITIVE_REVIEW: 30,
        RoundState.SYNTHESIZING: 45,
        RoundState.RESPONDING: 30,
        # 等待由 reopen_conditions 或用户驱动，不设超时
        RoundState.WAITING_FOR_EVIDENCE: None,
        RoundState.COMPLETED: None,
        RoundState.SUSPENDED: None,
        RoundState.FAILED: None,
        RoundState.CANCELLED: None,
    }
)

#: 元认知决策到状态机目标的映射（``docs/state_machine.md`` §2.2，ADR-0010）。
_METACOGNITIVE_TARGETS: Final[MappingProxyType[MetacognitiveDecision, frozenset[RoundState]]] = (
    MappingProxyType(
        {
            MetacognitiveDecision.CONTINUE: frozenset(
                {RoundState.ANALYZING, RoundState.DELIBERATING}
            ),
            MetacognitiveDecision.CHANGE_METHOD: frozenset(
                {RoundState.RETRIEVING, RoundState.ANALYZING}
            ),
            MetacognitiveDecision.NARROW_SCOPE: frozenset({RoundState.DELIBERATING}),
            MetacognitiveDecision.LOWER_CONFIDENCE: frozenset({RoundState.SYNTHESIZING}),
            MetacognitiveDecision.REQUEST_EVIDENCE: frozenset({RoundState.RETRIEVING}),
            MetacognitiveDecision.WAIT: frozenset({RoundState.WAITING_FOR_EVIDENCE}),
            MetacognitiveDecision.STOP: frozenset({RoundState.SYNTHESIZING}),
            MetacognitiveDecision.ESCALATE_TO_RESEARCH: frozenset({RoundState.SUSPENDED}),
        }
    )
)


def allowed_transitions(from_state: RoundState) -> frozenset[RoundState]:
    """返回从 ``from_state`` 出发**允许**转移到的状态集合。

    Args:
        from_state: 当前状态。

    Returns:
        允许的目标状态集合。终态返回空集。
    """
    return TRANSITIONS[from_state]


def can_transition(from_state: RoundState, to_state: RoundState) -> bool:
    """判断一次状态转移是否合法。

    Args:
        from_state: 当前状态。
        to_state: 目标状态。

    Returns:
        合法返回 ``True``，否则 ``False``。
    """
    return to_state in TRANSITIONS[from_state]


def assert_transition(from_state: RoundState, to_state: RoundState) -> None:
    """校验状态转移，非法时抛出 :class:`IllegalStateTransitionError`。

    这是**唯一**应当被业务代码调用的转移校验入口——
    不要在调用处重复实现判断逻辑。

    Args:
        from_state: 当前状态。
        to_state: 目标状态。

    Raises:
        IllegalStateTransitionError: 该转移不在权威转移表中。
    """
    if can_transition(from_state, to_state):
        return

    if from_state.is_terminal:
        msg = (
            f"回合已处于终态 {from_state.value}，不可再转移。"
            "重新激活需要创建一个新回合，并以 causation_id 指向原回合"
        )
    else:
        allowed = sorted(state.value for state in TRANSITIONS[from_state])
        msg = f"非法状态转移：{from_state.value} → {to_state.value}。允许的目标状态：{allowed}"
    raise IllegalStateTransitionError(msg, from_state=from_state.value, to_state=to_state.value)


def targets_for_decision(decision: MetacognitiveDecision) -> frozenset[RoundState]:
    """返回元认知决策允许转移到的目标状态集合（ADR-0010）。

    元认知决策**不能**直接指定一个任意状态——
    它只能落在本表给定的候选集合内，最终状态由编排器结合当前状态选定。

    Args:
        decision: 元认知决策。

    Returns:
        允许的目标状态集合。
    """
    return _METACOGNITIVE_TARGETS[decision]


def is_valid_decision_target(decision: MetacognitiveDecision, to_state: RoundState) -> bool:
    """元认知决策到目标状态的组合是否合法。

    同时校验两层：目标必须是该决策允许的，且必须在当前状态
    （``METACOGNITIVE_REVIEW``）的合法转移表内。

    Args:
        decision: 元认知决策。
        to_state: 期望的目标状态。

    Returns:
        合法返回 ``True``。
    """
    return to_state in _METACOGNITIVE_TARGETS[decision] and can_transition(
        RoundState.METACOGNITIVE_REVIEW, to_state
    )


def timeout_for(state: RoundState) -> int | None:
    """返回状态的超时秒数（ADR-0008）。

    Args:
        state: 查询的状态。

    Returns:
        超时秒数；``None`` 表示该状态无超时。
    """
    return STATE_TIMEOUTS_SECONDS[state]
