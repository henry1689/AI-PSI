"""跨工作单元共享的内存数据。

:class:`InMemoryStore` 相当于"数据库"：它持有已提交的数据与全局序号。
每个 :class:`~ai_psi.infrastructure.in_memory.unit_of_work.InMemoryUnitOfWork`
是它的一个事务视图，写入先进入暂存区，``commit()`` 时才合并。

**为什么不做成"直接写进共享字典"**：那样一次失败的事务会留下半成品，
与任务书阶段 2 的验收条件（"事务失败不会留下半成品数据"）直接冲突，
而且会让内存实现与 PostgreSQL 实现的行为在**最不该有差异的地方**产生差异。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from uuid import UUID

from ai_psi.domain.cognitive_rounds import CognitiveRound
from ai_psi.domain.events import Event

__all__ = ["IdempotencyRecord", "InMemoryStore"]


@dataclass
class IdempotencyRecord:
    """幂等键的占位记录。"""

    key: str
    request_hash: str
    cognitive_round_id: UUID | None = None


@dataclass
class InMemoryStore:
    """内存中的"数据库"。

    线程安全通过一把粗粒度锁实现。V0.1 的内存实现只服务于测试与本地演示，
    并发压力不在其设计目标内；**但它必须是正确的**，
    因为它承载着契约测试。
    """

    #: ``(sequence, event)`` 列表。
    events: list[tuple[int, Event]] = field(default_factory=list)

    #: 回合表。
    rounds: dict[UUID, CognitiveRound] = field(default_factory=dict)

    #: 幂等键表。
    idempotency: dict[str, IdempotencyRecord] = field(default_factory=dict)

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _sequence: int = field(default=0, repr=False)

    def next_sequence(self) -> int:
        """分配下一个全局序号。

        🔴 **序号只增不减。** 事务回滚时已经分配的序号**不回收**——
        PostgreSQL 的序列同样如此。把回滚实现成"序号回退"会让内存实现
        与真实实现产生行为差异，而那正是契约测试要防的。

        Returns:
            单调递增的序号，从 1 开始。
        """
        with self._lock:
            self._sequence += 1
            return self._sequence

    def current_sequence(self) -> int:
        """返回已分配的最大序号。"""
        with self._lock:
            return self._sequence

    def apply(
        self,
        *,
        events: list[tuple[int, Event]],
        rounds: dict[UUID, CognitiveRound],
        reservations: dict[str, IdempotencyRecord],
    ) -> None:
        """把一次事务的暂存区合并进共享数据。

        **这是内存实现里唯一的写入口**，且只在 ``commit()`` 时被调用。
        把它放在 store 上（而不是让工作单元直接改字段）有两个好处：
        锁的边界清楚，且"未提交的写入不可见"这条规则只有一处需要保证。

        记忆不在其中——:class:`~ai_psi.application.ports.MemoryRepository`
        的每个方法自身就是一次原子操作，不参与工作单元
        （见 :mod:`ai_psi.infrastructure.in_memory.memory_store` 的模块文档）。

        Args:
            events: 待追加的 ``(sequence, event)``。
            rounds: 待写入的回合。
            reservations: 待写入的幂等占位。
        """
        with self._lock:
            self.events.extend(events)
            self.rounds.update(rounds)
            self.idempotency.update(reservations)

    def clear(self) -> None:
        """清空全部数据（测试夹具用）。"""
        with self._lock:
            self.events.clear()
            self.rounds.clear()
            self.idempotency.clear()
            self._sequence = 0
