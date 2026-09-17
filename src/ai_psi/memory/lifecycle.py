"""记忆的生命周期：过期、失效与保留策略（任务书 §10.4、§5.11）。

记忆的"有效"有两个来源，**两者不可互相替代**：

* ``valid_until``（显式失效时间）——写记忆时就声明"这条到某天为止"，
  例如"用户这周在准备考试"；
* ``status``（状态机）——由取代、纠正、用户删除驱动。

本模块处理**第一条**：把"时间上说不过去了"翻译成状态的变更。
第二条在 :class:`~ai_psi.application.memory_service.MemoryService` 里。

⚠️ **V0.1 没有后台清理进程。** 过期是**读时判定 + 显式维护调用**：
检索路径本来就会过滤掉 ``EXPIRED``，但一条已经过了 ``valid_until``
却还标着 ``ACTIVE`` 的记忆不会自动消失。理由是没有后台调度的情况下
硬做一个"定时过期"只能靠某个请求顺带触发，而那会让
"什么时候过期"变成"什么时候恰好有人请求"——时序不确定，
排查时无从复现。显式的 :func:`due_for_expiry` 让这件事可预测、可测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from ai_psi.domain.enums import MemoryStatus, RetentionPolicy
from ai_psi.domain.memories import Memory

__all__ = [
    "ExpiryPlan",
    "due_for_expiry",
    "is_expired",
    "plan_expiry",
]

#: 状态已经是"不再有效"的那些——过期扫描不需要再碰它们。
_TERMINAL_STATUSES: frozenset[MemoryStatus] = frozenset(
    {
        MemoryStatus.SUPERSEDED,
        MemoryStatus.DELETED,
        MemoryStatus.EXPIRED,
        MemoryStatus.REJECTED,
    }
)


@dataclass(frozen=True, slots=True)
class ExpiryPlan:
    """一次过期判定的结论。

    Attributes:
        memory_id: 目标记忆。
        should_expire: 是否应当置为 ``EXPIRED``。
        reason: 判定的可读理由——必须是**具体事实**
            （"valid_until=... 已过"），而不是"过期了"这种同义反复。
    """

    memory_id: UUID
    should_expire: bool
    reason: str


def is_expired(memory: Memory, *, now: datetime) -> bool:
    """记忆在 ``now`` 时刻是否已经超过显式失效时间。

    🔴 **已经处于终态的记忆不算"过期"。** 一条被用户删除的记忆，
    它的 ``valid_until`` 是否到了不影响任何事；把它再标一次
    ``EXPIRED`` 会覆盖掉"用户主动删除"这个更有信息量的状态。

    Args:
        memory: 待判定的记忆。
        now: 当前时间。

    Returns:
        是否过期。
    """
    if memory.status in _TERMINAL_STATUSES:
        return False
    return memory.valid_until is not None and memory.valid_until <= now


def plan_expiry(memory: Memory, *, now: datetime) -> ExpiryPlan:
    """给出单条记忆的过期判定与理由。

    Args:
        memory: 待判定的记忆。
        now: 当前时间。

    Returns:
        判定结论。
    """
    if memory.status in _TERMINAL_STATUSES:
        return ExpiryPlan(
            memory_id=memory.id,
            should_expire=False,
            reason=f"状态为 {memory.status.value}，已是终态，不再判定过期",
        )
    if memory.valid_until is None:
        return ExpiryPlan(
            memory_id=memory.id,
            should_expire=False,
            reason="未声明 valid_until，按保留策略长期有效",
        )
    if memory.valid_until <= now:
        return ExpiryPlan(
            memory_id=memory.id,
            should_expire=True,
            reason=f"valid_until={memory.valid_until.isoformat()} 已过（当前 {now.isoformat()}）",
        )
    return ExpiryPlan(
        memory_id=memory.id,
        should_expire=False,
        reason=f"valid_until={memory.valid_until.isoformat()} 尚未到达",
    )


def due_for_expiry(memories: list[Memory], *, now: datetime) -> list[ExpiryPlan]:
    """挑出应当被置为 ``EXPIRED`` 的记忆。

    Args:
        memories: 待扫描的记忆。
        now: 当前时间。

    Returns:
        仅包含 ``should_expire`` 为真的判定，按记忆 id 排序以保证可复现。
    """
    plans = [plan_expiry(memory, now=now) for memory in memories]
    return sorted(
        (plan for plan in plans if plan.should_expire),
        key=lambda plan: str(plan.memory_id),
    )


def retention_requirements(memory: Memory) -> str:
    """把保留策略翻译成一句可读的说明。

    存在的意义是让 **API 的导出结果能解释自己**：用户看到一条记忆被标成
    ``PERMANENT`` 与看到 ``SESSION``，对"它为什么还在 / 为什么可能消失"
    应当得到不同的解释。

    🔴 **只描述 V0.1 真正执行了的规则。**

    ``SESSION`` 与 ``UNTIL_SUPERSEDED`` 这两种策略**目前没有区别对待**——
    按会话失效需要"会话"这个 V0.1 里并不存在的一等对象。
    与其写一句"会话级记忆不跨会话保留"来让导出结果显得完整，
    不如直说它暂时等同于长期保留：**导出是给用户看的**，
    在这里说一句做不到的话，用户会据此做出错误的预期。
    """
    match memory.retention_policy:
        case RetentionPolicy.PERMANENT:
            return "长期保留，不因时间自动失效（仍需用户显式删除才会移除）"
        case RetentionPolicy.LONG_TERM:
            return "长期保留，超过 valid_until 后失效"
        case RetentionPolicy.SESSION | RetentionPolicy.UNTIL_SUPERSEDED:
            return (
                f"策略为 {memory.retention_policy.value}；"
                "V0.1 尚未实现按会话或按取代自动失效，当前与长期保留同义"
            )
        case RetentionPolicy.USER_CONTROLLED:
            return "由用户控制：仅在用户显式删除或纠正时变更"
