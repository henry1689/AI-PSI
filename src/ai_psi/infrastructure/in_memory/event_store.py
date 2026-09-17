"""事件存储的内存实现。

🔴 **只追加。** 与 PostgreSQL 实现一样，本模块不提供任何修改或删除事件的
方法——这不是遗漏，是设计（ADR-0002）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from ai_psi.domain.events import Event
from ai_psi.domain.exceptions import ConflictError

if TYPE_CHECKING:  # 运行期不导入，避免与工作单元互相引用
    from ai_psi.infrastructure.in_memory.unit_of_work import InMemoryUnitOfWork

__all__ = ["InMemoryEventStore"]


class InMemoryEventStore:
    """基于内存的事务性事件存储。

    读操作**能看到本事务尚未提交的写入**（read-your-writes），
    但看不到其他事务的未提交写入——与 SQL 的隔离语义一致。
    """

    def __init__(self, uow: InMemoryUnitOfWork) -> None:
        """初始化。

        Args:
            uow: 所属工作单元。
        """
        self._uow = uow

    async def append(self, event: Event) -> None:
        """追加单个事件。"""
        await self.append_many([event])

    async def append_many(self, events: list[Event]) -> None:
        """批量追加。

        Raises:
            ConflictError: 批次内存在重复 id，或 id 已被写入。
        """
        if not events:
            return

        ids = [event.id for event in events]
        if len(set(ids)) != len(ids):
            duplicates = sorted({str(i) for i in ids if ids.count(i) > 1})
            msg = f"同一批次内存在重复的事件 id：{duplicates}"
            raise ConflictError(msg, context={"duplicate_ids": duplicates})

        existing = {event.id for _, event in self._uow.visible_events()}
        clashing = sorted(str(i) for i in ids if i in existing)
        if clashing:
            msg = f"事件 id 已存在，事件只追加不可覆盖：{clashing}"
            raise ConflictError(msg, context={"duplicate_ids": clashing})

        for event in events:
            self._uow.stage_event(event)

    async def read_stream(
        self,
        *,
        cognitive_round_id: UUID,
        after_sequence: int | None = None,
    ) -> list[Event]:
        """按 ``sequence`` 升序读取某个回合的事件流。"""
        stream = [
            (sequence, event)
            for sequence, event in self._uow.visible_events()
            if event.cognitive_round_id == cognitive_round_id
            and (after_sequence is None or sequence > after_sequence)
        ]
        return [event for _, event in sorted(stream, key=lambda pair: pair[0])]

    async def read_by_correlation(self, *, correlation_id: UUID) -> list[Event]:
        """读取同一因果关联链上的全部事件。"""
        stream = [
            (sequence, event)
            for sequence, event in self._uow.visible_events()
            if event.correlation_id == correlation_id
        ]
        return [event for _, event in sorted(stream, key=lambda pair: pair[0])]

    async def read_since(self, *, after_sequence: int, limit: int = 500) -> list[Event]:
        """按序号增量读取全局事件（跨回合）。"""
        stream = [
            (sequence, event)
            for sequence, event in self._uow.visible_events()
            if sequence > after_sequence
        ]
        ordered = sorted(stream, key=lambda pair: pair[0])[:limit]
        return [event for _, event in ordered]

    async def latest_sequence(self) -> int:
        """返回当前最大序号；空存储返回 0。"""
        return max((sequence for sequence, _ in self._uow.visible_events()), default=0)

    async def latest_sequence_for_round(self, *, cognitive_round_id: UUID) -> int:
        """返回某个回合事件流的最大序号；无事件时返回 0。"""
        stream = [
            sequence
            for sequence, event in self._uow.visible_events()
            if event.cognitive_round_id == cognitive_round_id
        ]
        return max(stream, default=0)

    async def count(self) -> int:
        """事件总数。"""
        return len(self._uow.visible_events())
