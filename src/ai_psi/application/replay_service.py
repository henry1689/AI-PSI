"""历史回合回放（任务书 §12.5、§18）。

🔴 **回放是只读的。**

它不修改任何事件、不创建新回合、不写数据库。
"回放"在这里不是"重跑一遍认知过程"——那会产生新事件、污染历史；
而是**从事件流重建当时的认知轨迹**，用于审计与一致性校验。

投影本身还会校验转移合法性（:mod:`ai_psi.cognition.projection`），
因此回放同时是一次**对历史的一致性审计**：如果某次写入绕过了状态机，
回放会直接报错，而不是忠实重现一个非法状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.cognition.projection import RoundProjection, project_round
from ai_psi.domain.exceptions import NotFoundError

__all__ = ["ReplayResult", "ReplayService"]


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """一次回放的结果。

    Attributes:
        projection: 从事件流重建出的轨迹。
        differs_from_projection: 投影结果与当前状态表是否不一致。

        🔴 该字段是**数据一致性告警**。为 ``True`` 说明事件流与状态投影
        不一致——通常意味着有写入绕过了应用服务。正常情况下恒为 ``False``。
    """

    projection: RoundProjection
    differs_from_projection: bool


class ReplayService:
    """历史回合回放。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        """初始化。

        Args:
            uow_factory: 工作单元工厂。
        """
        self._uow_factory = uow_factory

    async def replay_round(self, round_id: UUID) -> ReplayResult:
        """回放一个回合的完整事件流。

        Args:
            round_id: 回合 id。

        Returns:
            回放结果。

        Raises:
            NotFoundError: 该回合没有任何事件。
            DomainError: 事件流自相矛盾。
            IllegalStateTransitionError: 事件流中包含非法转移。
        """
        async with self._uow_factory() as uow:
            events = await uow.events.read_stream(cognitive_round_id=round_id)
            if not events:
                msg = f"回合没有事件可供回放：{round_id}"
                raise NotFoundError(msg, context={"cognitive_round_id": str(round_id)})

            projection = project_round(events)

            current = await uow.rounds.get(round_id)
            differs = current is not None and current.state is not projection.state

        return ReplayResult(projection=projection, differs_from_projection=differs)

    async def replay_round_incremental(
        self,
        round_id: UUID,
        *,
        after_sequence: int,
    ) -> list[UUID]:
        """增量回放：只取游标之后的事件 id。

        完整重建需要全量事件；但如果调用方已经持有一个游标，
        用它只拉取增量部分可以避免重复传输。

        Args:
            round_id: 回合 id。
            after_sequence: 游标（只返回序号大于它的事件）。

        Returns:
            增量事件的 id 列表，按序。
        """
        async with self._uow_factory() as uow:
            events = await uow.events.read_stream(
                cognitive_round_id=round_id,
                after_sequence=after_sequence,
            )
        return [event.id for event in events]

    async def current_cursor(self, round_id: UUID) -> int:
        """返回该回合当前的游标值（事件流最大序号）。

        Args:
            round_id: 回合 id。

        Returns:
            最大序号；无事件时为 0。
        """
        async with self._uow_factory() as uow:
            return await uow.events.latest_sequence_for_round(cognitive_round_id=round_id)
