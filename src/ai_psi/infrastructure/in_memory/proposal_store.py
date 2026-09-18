"""改进提案仓储的内存实现。

与 PostgreSQL 实现跑**同一组契约断言**。

🔴 与 SQL 实现一样，本类**没有任何"让提案生效"的路径**——
不是没实现，而是 ``ProposalStatus`` 里不存在那个状态（不变量 11）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from ai_psi.domain.enums import ErrorType, ProposalStatus
from ai_psi.domain.exceptions import ConflictError, NotFoundError, OptimisticLockError
from ai_psi.domain.improvement_proposals import (
    ImprovementProposal,
    assert_status_is_a_member,
)

if TYPE_CHECKING:  # 运行期不导入，避免与工作单元互相引用
    from ai_psi.infrastructure.in_memory.unit_of_work import InMemoryUnitOfWork

__all__ = ["InMemoryProposalRepository"]


class InMemoryProposalRepository:
    """改进提案仓储的内存实现。"""

    def __init__(self, uow: InMemoryUnitOfWork) -> None:
        """初始化。

        Args:
            uow: 所属工作单元。
        """
        self._uow = uow

    async def add(self, proposal: ImprovementProposal) -> None:
        """写入一条新提案。

        Raises:
            ConflictError: 主键已存在。
            ConstitutionViolationError: 状态不是 ``ProposalStatus`` 的成员。
        """
        assert_status_is_a_member(proposal)
        if self._uow.visible_proposal(proposal.id) is not None:
            msg = f"改进提案已存在：{proposal.id}"
            raise ConflictError(msg, context={"proposal_id": str(proposal.id)})
        self._uow.stage_proposal(proposal)

    async def get(self, proposal_id: UUID) -> ImprovementProposal | None:
        """按 id 读取提案。"""
        return self._uow.visible_proposal(proposal_id)

    async def save(self, proposal: ImprovementProposal, *, expected_version: int) -> None:
        """带乐观锁的更新。

        Raises:
            NotFoundError: 提案不存在。
            OptimisticLockError: 版本不匹配。
            ConstitutionViolationError: 状态不是 ``ProposalStatus`` 的成员。
        """
        assert_status_is_a_member(proposal)
        current = self._uow.visible_proposal(proposal.id)
        if current is None:
            msg = f"改进提案不存在：{proposal.id}"
            raise NotFoundError(msg, context={"proposal_id": str(proposal.id)})
        if current.version != expected_version:
            msg = (
                f"乐观锁冲突：提案 {proposal.id} 期望版本 {expected_version}，"
                f"实际版本 {current.version}"
            )
            raise OptimisticLockError(
                msg,
                entity_type="ImprovementProposal",
                entity_id=str(proposal.id),
                expected_version=expected_version,
                actual_version=current.version,
            )
        self._uow.stage_proposal(proposal)

    async def list_all(
        self,
        *,
        status: ProposalStatus | None = None,
        error_class: ErrorType | None = None,
        limit: int | None = None,
    ) -> list[ImprovementProposal]:
        """列出提案（顺序与 SQL 实现一致）。"""
        proposals = [
            item
            for item in self._uow.visible_proposals()
            if (status is None or item.status is status)
            and (error_class is None or item.error_class is error_class)
        ]
        proposals.sort(key=lambda item: (-item.created_at.timestamp(), str(item.id)))
        if limit is not None:
            proposals = proposals[: max(0, limit)]
        return proposals
