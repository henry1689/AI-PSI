"""事件存储的 PostgreSQL 实现。

🔴 **只追加**。本模块不提供任何修改或删除事件的方法——这不是遗漏，
是设计（ADR-0002）。事件是系统的真相来源；当前状态是它的投影。
修改历史事件等同于销毁审计证据。

排序依据是 ``sequence``（数据库生成的单调递增列），**不是时间戳**。
同一微秒内写入的多个事件用 ``recorded_at`` 排序会得到不确定的顺序，
而回放结果的不确定性会让"可复现"这个承诺失效。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_psi.domain.enums import EventType
from ai_psi.domain.events import Event
from ai_psi.domain.exceptions import ConflictError
from ai_psi.infrastructure.db.errors import is_unique_violation
from ai_psi.infrastructure.db.mappers import apply_event, row_to_event
from ai_psi.infrastructure.db.models import EventRow

__all__ = ["SqlAlchemyEventStore"]


class SqlAlchemyEventStore:
    """基于 SQLAlchemy 的事件存储。

    本对象**不自行提交事务**——它只是当前会话上的一个视图，
    事务边界由 :class:`~ai_psi.infrastructure.db.unit_of_work.SqlAlchemyUnitOfWork`
    统一管理。这样"事件 + 投影"才能落在同一个事务里。
    """

    def __init__(self, session: AsyncSession) -> None:
        """初始化。

        Args:
            session: 由工作单元管理的会话。
        """
        self._session = session

    async def append(self, event: Event) -> None:
        """追加单个事件。

        Args:
            event: 待追加的事件。
        """
        await self.append_many([event])

    async def append_many(self, events: list[Event]) -> None:
        """批量追加，要么全部成功要么全部失败。

        Args:
            events: 待追加的事件。

        Raises:
            ConflictError: 批次内存在重复 id，或与库中已存在的事件 id 冲突。
        """
        if not events:
            return

        ids = [event.id for event in events]
        if len(set(ids)) != len(ids):
            duplicates = sorted({str(i) for i in ids if ids.count(i) > 1})
            msg = f"同一批次内存在重复的事件 id：{duplicates}"
            raise ConflictError(msg, context={"duplicate_ids": duplicates})

        rows: list[EventRow] = []
        for event in events:
            row = EventRow()
            apply_event(row, event)
            rows.append(row)

        self._session.add_all(rows)
        # flush 让数据库约束立刻生效——问题在这里暴露，
        # 而不是等到整个事务提交时才以难以定位的形式出现。
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # 🔴 **Port 不允许泄漏基础设施异常。**
            # 契约说"重复 id 抛 ConflictError"，调用方就按 ConflictError 写
            # ``except``；把 SQLAlchemy 的 IntegrityError 透出去，
            # 那些 except 会静默失效，而这正是契约测试要防的漂移。
            if is_unique_violation(exc):
                conflicts = sorted(str(i) for i in ids)
                msg = f"事件 id 已存在，事件只追加不可覆盖：{conflicts}"
                raise ConflictError(msg, context={"duplicate_ids": conflicts}) from exc
            raise

    async def read_stream(
        self,
        *,
        cognitive_round_id: UUID,
        after_sequence: int | None = None,
    ) -> list[Event]:
        """按 ``sequence`` 升序读取某个回合的事件流。

        Args:
            cognitive_round_id: 回合 id。
            after_sequence: 只返回序号大于该值的事件（增量回放）。

        Returns:
            按 ``sequence`` 升序排列的领域事件。
        """
        stmt = select(EventRow).where(EventRow.cognitive_round_id == cognitive_round_id)
        if after_sequence is not None:
            stmt = stmt.where(EventRow.sequence > after_sequence)
        stmt = stmt.order_by(EventRow.sequence)

        result = await self._session.execute(stmt)
        return [row_to_event(row) for row in result.scalars().all()]

    async def read_by_correlation(self, *, correlation_id: UUID) -> list[Event]:
        """读取同一因果关联链上的全部事件。

        Args:
            correlation_id: 关联链标识。

        Returns:
            按 ``sequence`` 升序排列的领域事件。
        """
        stmt = (
            select(EventRow)
            .where(EventRow.correlation_id == correlation_id)
            .order_by(EventRow.sequence)
        )
        result = await self._session.execute(stmt)
        return [row_to_event(row) for row in result.scalars().all()]

    async def read_since(self, *, after_sequence: int, limit: int = 500) -> list[Event]:
        """按序号增量读取全局事件（跨回合）。

        用于后台消费者与离线评估。

        Args:
            after_sequence: 只返回序号大于该值的事件。
            limit: 单次上限，防止一次拉取过多。

        Returns:
            按 ``sequence`` 升序排列的领域事件。
        """
        stmt = (
            select(EventRow)
            .where(EventRow.sequence > after_sequence)
            .order_by(EventRow.sequence)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [row_to_event(row) for row in result.scalars().all()]

    async def read_by_event_type(
        self,
        *,
        event_type: EventType,
        limit: int | None = None,
    ) -> list[Event]:
        """按事件类型读取，按 ``sequence`` 升序。

        走 ``ix_events_type_recorded`` 索引（阶段 2 就建好的），
        因此它是一次索引扫描，不是全表扫描。

        Args:
            event_type: 目标事件类型。
            limit: 返回条数上限；``None`` 表示不限制。

        Returns:
            命中事件，按 ``sequence`` 升序。
        """
        stmt = (
            select(EventRow)
            .where(EventRow.event_type == event_type.value)
            .order_by(EventRow.sequence)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return [row_to_event(row) for row in result.scalars().all()]

    async def latest_sequence(self) -> int:
        """返回当前最大序号；空表返回 0。"""
        value = await self._session.scalar(select(func.max(EventRow.sequence)))
        return value or 0

    async def latest_sequence_for_round(self, *, cognitive_round_id: UUID) -> int:
        """返回某个回合事件流的最大序号；无事件时返回 0。

        Args:
            cognitive_round_id: 回合 id。

        Returns:
            该回合事件流的最大 ``sequence``。
        """
        value = await self._session.scalar(
            select(func.max(EventRow.sequence)).where(
                EventRow.cognitive_round_id == cognitive_round_id
            )
        )
        return value or 0

    async def count(self) -> int:
        """事件总数。"""
        return await self._session.scalar(select(func.count()).select_from(EventRow)) or 0
