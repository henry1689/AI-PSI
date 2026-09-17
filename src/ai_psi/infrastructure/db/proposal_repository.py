"""改进提案的 PostgreSQL 实现（任务书 §5.12、§12.4）。

与 :class:`~ai_psi.infrastructure.in_memory.proposal_store.InMemoryProposalRepository`
跑的是**同一组契约断言**（``tests/contract/base.py``）。

🔴 **本类没有任何"让提案生效"的路径。**

不是"没有实现"，而是**没有这个概念**：``ProposalStatus`` 里不存在
``ACTIVE``，因此既没有可写的状态值，也没有可构造的对象（不变量 11）。
数据库那一侧同样如此——``status`` 列的 CHECK 由该枚举生成，
天然排除了它。

⚠️ 本类不自行提交事务，由工作单元统一管理。
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_psi.domain.enums import ErrorType, ProposalStatus
from ai_psi.domain.exceptions import ConflictError, NotFoundError, OptimisticLockError
from ai_psi.domain.improvement_proposals import ImprovementProposal
from ai_psi.infrastructure.db.errors import is_unique_violation
from ai_psi.infrastructure.db.mappers import proposal_to_row, proposal_to_values, row_to_proposal
from ai_psi.infrastructure.db.models import ImprovementProposalRow

__all__ = ["SqlAlchemyProposalRepository"]


class SqlAlchemyProposalRepository:
    """改进提案仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        """初始化。

        Args:
            session: 由工作单元管理的会话。
        """
        self._session = session

    async def add(self, proposal: ImprovementProposal) -> None:
        """写入一条新提案。

        Raises:
            ConflictError: 主键已存在。
        """
        self._session.add(proposal_to_row(proposal))
        try:
            await self._session.flush()
        except Exception as exc:
            if is_unique_violation(exc):
                msg = f"改进提案已存在：{proposal.id}"
                raise ConflictError(msg, context={"proposal_id": str(proposal.id)}) from exc
            raise

    async def get(self, proposal_id: UUID) -> ImprovementProposal | None:
        """按 id 读取提案。"""
        row = await self._session.get(ImprovementProposalRow, proposal_id)
        return None if row is None else row_to_proposal(row)

    async def save(self, proposal: ImprovementProposal, *, expected_version: int) -> None:
        """带乐观锁的更新。

        🔴 **绝不静默覆盖。** 两条并发的状态流转（比如"批准"与"驳回"同时到达）
        如果后写的那条覆盖了先写的，结果是一条提案**同时**被批准和驳回——
        而事件流里两条理由都在。乐观锁在这里不是性能优化，是正确性要求。

        Raises:
            OptimisticLockError: 版本不匹配。
            NotFoundError: 提案不存在。
        """
        stmt = (
            update(ImprovementProposalRow)
            .where(
                ImprovementProposalRow.id == proposal.id,
                ImprovementProposalRow.version == expected_version,
            )
            .values(**proposal_to_values(proposal))
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        if result.rowcount == 0:
            await self._raise_save_failure(proposal.id, expected_version)
        await self._session.flush()

    async def list_all(
        self,
        *,
        status: ProposalStatus | None = None,
        error_class: ErrorType | None = None,
        limit: int | None = None,
    ) -> list[ImprovementProposal]:
        """列出提案。"""
        stmt = select(ImprovementProposalRow)
        if status is not None:
            stmt = stmt.where(ImprovementProposalRow.status == status.value)
        if error_class is not None:
            stmt = stmt.where(ImprovementProposalRow.error_class == error_class.value)
        # 顺序必须确定：created_at 可能相同，id 兜底
        stmt = stmt.order_by(ImprovementProposalRow.created_at.desc(), ImprovementProposalRow.id)
        if limit is not None:
            stmt = stmt.limit(max(0, limit))
        rows = (await self._session.scalars(stmt)).all()
        return [row_to_proposal(row) for row in rows]

    async def _raise_save_failure(self, proposal_id: UUID, expected_version: int) -> None:
        """受影响行数为 0 时，判定真实原因并抛出对应异常。"""
        actual = await self._session.scalar(
            select(ImprovementProposalRow.version).where(ImprovementProposalRow.id == proposal_id)
        )
        if actual is None:
            msg = f"改进提案不存在：{proposal_id}"
            raise NotFoundError(msg, context={"proposal_id": str(proposal_id)})

        msg = (
            f"乐观锁冲突：提案 {proposal_id} 期望版本 {expected_version}，"
            f"实际版本 {actual}。这说明期间有其他写入者提交了状态流转"
        )
        raise OptimisticLockError(
            msg,
            entity_type="ImprovementProposal",
            entity_id=str(proposal_id),
            expected_version=expected_version,
            actual_version=actual,
        )
