"""工作单元的内存实现。

🔴 **与 SQL 实现共享同一条核心规则：未提交即丢弃。**

每个工作单元持有一份写入暂存区，读操作可见的是"已提交 + 本事务暂存"，
``commit()`` 时一次性合并，``rollback()`` 或异常退出时全部丢弃。
这正是任务书阶段 2 验收条件"事务失败不会留下半成品数据"在内存侧的落地。

🔴 **记忆在阶段 5 加入了工作单元。** 阶段 3 时它是独立 Port（每个方法
自带一个事务），代价是 ``MemoryService.correct`` 的
"取代旧记忆 + 写入新记忆 + 记录事件"跨在三个事务上——
最后一步失败会留下一条没有审计记录的纠正（ADR-0015 §5）。
现在两侧（内存与 PostgreSQL）都把记忆挂在工作单元上，四步同生共死。
"""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Self
from uuid import UUID

from ai_psi.application.ports import EventStore, IdempotencyStore, MemoryRepository, RoundRepository
from ai_psi.domain.cognitive_rounds import CognitiveRound
from ai_psi.domain.events import Event
from ai_psi.domain.memories import Memory
from ai_psi.infrastructure.in_memory.event_store import InMemoryEventStore
from ai_psi.infrastructure.in_memory.memory_store import InMemoryMemoryRepository
from ai_psi.infrastructure.in_memory.repositories import (
    InMemoryIdempotencyStore,
    InMemoryRoundRepository,
)
from ai_psi.infrastructure.in_memory.store import (
    IdempotencyRecord,
    InMemoryStore,
    MemoryIndexEntry,
)
from ai_psi.providers.embeddings import EmbeddingProvider

__all__ = ["InMemoryUnitOfWork", "make_in_memory_unit_of_work_factory"]


def make_in_memory_unit_of_work_factory(
    store: InMemoryStore,
    embeddings: EmbeddingProvider,
) -> Callable[[], InMemoryUnitOfWork]:
    """构造内存工作单元工厂。

    应用服务只依赖 :data:`ai_psi.application.ports.UnitOfWorkFactory`
    协议；本函数产出的工厂满足该协议（返回类型协变）。

    Args:
        store: 共享的内存数据。
        embeddings: 向量 Provider。**刻意不给默认值**——
            一个"不传就用本地实现"的默认值会让装配错误变成静默的行为差异，
            而记忆向量正是那种"错了也不会报错、只是永远查不到"的东西。

    Returns:
        每次调用产出一个新工作单元（即一个新"事务"）的可调用对象。
    """

    def _factory() -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(store, embeddings)

    return _factory


class InMemoryUnitOfWork:
    """内存工作单元。

    用法与 SQL 版本完全一致::

        async with uow_factory() as uow:
            await uow.rounds.add(round_)
            await uow.events.append(event)
            await uow.commit()          # 不调用它就不会生效
    """

    def __init__(self, store: InMemoryStore, embeddings: EmbeddingProvider) -> None:
        """初始化。

        Args:
            store: 共享的内存数据。
            embeddings: 向量 Provider。
        """
        self._store = store
        self._committed = False

        self._staged_events: list[tuple[int, Event]] = []
        self._staged_rounds: dict[UUID, CognitiveRound] = {}
        self._staged_reservations: dict[str, IdempotencyRecord] = {}
        self._staged_memories: dict[UUID, Memory] = {}
        #: 值为 ``None`` 表示"删除该索引项"。
        self._staged_index: dict[UUID, MemoryIndexEntry | None] = {}

        self.events: EventStore = InMemoryEventStore(self)
        self.rounds: RoundRepository = InMemoryRoundRepository(self)
        self.idempotency: IdempotencyStore = InMemoryIdempotencyStore(self)
        self.memories: MemoryRepository = InMemoryMemoryRepository(self, embeddings)

    # ------------------------------------------------------------------
    # 事务边界
    # ------------------------------------------------------------------

    async def __aenter__(self) -> Self:
        """进入事务作用域。"""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """退出作用域。

        🔴 **未提交即丢弃。** 这条规则与 SQL 版本的"未提交即回滚"是同一条。
        """
        if not self._committed:
            self.discard()

    async def commit(self) -> None:
        """提交：把暂存区一次性合并进共享数据。"""
        self._store.apply(
            events=self._staged_events,
            rounds=self._staged_rounds,
            reservations=self._staged_reservations,
            memories=self._staged_memories,
            index=self._staged_index,
        )
        self._committed = True
        self.discard()

    async def rollback(self) -> None:
        """显式回滚。"""
        self.discard()
        self._committed = False

    def discard(self) -> None:
        """丢弃暂存区。

        序号**不回退**——PostgreSQL 的序列在回滚后同样会留下空洞，
        让内存实现"补偿"这个空洞反而会制造行为差异。
        """
        self._staged_events = []
        self._staged_rounds = {}
        self._staged_reservations = {}
        self._staged_memories = {}
        self._staged_index = {}

    # ------------------------------------------------------------------
    # 暂存与可见性（供本包的仓储使用）
    # ------------------------------------------------------------------

    def stage_event(self, event: Event) -> None:
        """暂存一个事件并分配序号。"""
        self._staged_events.append((self._store.next_sequence(), event))

    def stage_round(self, round_: CognitiveRound) -> None:
        """暂存一个回合（新增或更新）。"""
        self._staged_rounds[round_.id] = round_

    def stage_reservation(self, record: IdempotencyRecord) -> None:
        """暂存一条幂等占位。"""
        self._staged_reservations[record.key] = record

    def stage_memory(self, memory: Memory) -> None:
        """暂存一条记忆（新增或更新）。"""
        self._staged_memories[memory.id] = memory

    def stage_index(self, memory_id: UUID, entry: MemoryIndexEntry | None) -> None:
        """暂存一次索引变更；``entry`` 为 ``None`` 表示删除。"""
        self._staged_index[memory_id] = entry

    def visible_events(self) -> list[tuple[int, Event]]:
        """返回"已提交 + 本事务暂存"的全部事件。"""
        return [*self._store.events, *self._staged_events]

    def visible_rounds(self) -> list[CognitiveRound]:
        """返回"已提交 + 本事务暂存"的全部回合。"""
        merged = dict(self._store.rounds)
        merged.update(self._staged_rounds)
        return list(merged.values())

    def visible_round(self, round_id: UUID) -> CognitiveRound | None:
        """按 id 返回可见回合。"""
        if round_id in self._staged_rounds:
            return self._staged_rounds[round_id]
        return self._store.rounds.get(round_id)

    def visible_reservation(self, key: str) -> IdempotencyRecord | None:
        """按 key 返回可见的幂等占位。"""
        if key in self._staged_reservations:
            return self._staged_reservations[key]
        return self._store.idempotency.get(key)

    def visible_memory(self, memory_id: UUID) -> Memory | None:
        """按 id 返回可见记忆。"""
        if memory_id in self._staged_memories:
            return self._staged_memories[memory_id]
        return self._store.memories.get(memory_id)

    def visible_memories(self) -> list[Memory]:
        """返回"已提交 + 本事务暂存"的全部记忆。"""
        merged = dict(self._store.memories)
        merged.update(self._staged_memories)
        return list(merged.values())

    def visible_index(self) -> dict[UUID, MemoryIndexEntry]:
        """返回"已提交 + 本事务暂存"的向量索引。

        🔴 删除**在本方法里就生效**，而不是等提交。检索读的就是它，
        因此"删了但本事务内还检索得到"是不可能发生的——
        把删除推迟到 commit 会让同一事务里的
        "删除 → 再检索"路径读到已经删掉的记忆。
        """
        merged = dict(self._store.index)
        for memory_id, entry in self._staged_index.items():
            if entry is None:
                merged.pop(memory_id, None)
            else:
                merged[memory_id] = entry
        return merged
