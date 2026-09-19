"""改进提案仓储的内存实现。

与 PostgreSQL 实现跑**同一组契约断言**。

🔴 与 SQL 实现一样，本类**没有任何"让提案生效"的路径**——
不是没实现，而是 ``ProposalStatus`` 里不存在那个状态（不变量 11）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from ai_psi.domain.enums import ErrorType, ProposalStatus
from ai_psi.domain.exceptions import (
    ConflictError,
    ConstitutionViolationError,
    NotFoundError,
    OptimisticLockError,
    ProposalPatternConflictError,
)
from ai_psi.domain.improvement_proposals import (
    ImprovementProposal,
    active_pattern_key,
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

        🔴 **与 PostgreSQL 实现同语义**：业务键（同一个活跃模式）撞车抛
        :class:`ProposalPatternConflictError`，主键撞车抛 :class:`ConflictError`。
        两者在 SQL 侧由唯一索引与约束名分流，在这里由两次显式检查分流。

        ⚠️ 这里查的是**可见**提案（已提交 ∪ 本事务暂存），因此同一事务内
        连写两条同键提案会在这里就撞上。提交时还有一道复核
        （``InMemoryStore.apply``），它看的是**最终状态**——
        两者不是重复：这里挡"新增"，那里挡"暂存之后、提交之前被别的
        写入者改过"以及"旧提案同事务转终态"这类需要看合并结果的情形。

        Raises:
            ProposalPatternConflictError: 同一业务模式下已有一条**活跃**提案。
            ConflictError: 主键已存在。
            ConstitutionViolationError: 状态不是 ``ProposalStatus`` 的成员。
        """
        assert_status_is_a_member(proposal)
        if self._uow.visible_proposal(proposal.id) is not None:
            msg = f"改进提案已存在：{proposal.id}"
            raise ConflictError(msg, context={"proposal_id": str(proposal.id)})
        key = active_pattern_key(proposal)
        if (
            key is not None
            and self._uow.active_pattern_owner(key, excluding=proposal.id) is not None
        ):
            error_class, signature = key
            msg = f"该模式已有一条活跃提案：{error_class} / {signature}"
            raise ProposalPatternConflictError(
                msg,
                context={"error_class": error_class, "situation_signature": signature},
            )
        self._uow.stage_proposal(proposal)

    async def get(self, proposal_id: UUID) -> ImprovementProposal | None:
        """按 id 读取提案。"""
        return self._uow.visible_proposal(proposal_id)

    async def find_active_for_pattern(
        self, *, error_class: ErrorType, situation_signature: str
    ) -> ImprovementProposal | None:
        """取该业务模式下**唯一**那条活跃提案；没有则 ``None``。

        🔴 **收齐全部候选再判断数量，不"找到第一条就返回"。**

        正常情况至多一条；若是两条，只可能是提交时复核没生效
        （或数据被绕过写进来），那时挑一条返回会让调用方以为一切正常，
        而唯一性其实已经失效。宁可大声失败。

        ⚠️ 与 SQL 实现的差别只在写法：那边是 ``applicability[1]``
        （PostgreSQL 下标从 1 起），这里是 ``applicability[0]``。
        业务键的定义在 :func:`~ai_psi.domain.improvement_proposals.active_pattern_key`
        ——两边都从那里取，不各写一遍。

        Raises:
            ConstitutionViolationError: 同一业务键下存在**多于一条**活跃提案。
        """
        key = (error_class.value, situation_signature)
        matches = [
            item
            for item in self._uow.visible_proposals()
            if active_pattern_key(item) == key and not item.status.is_terminal
        ]
        if len(matches) > 1:
            msg = (
                f"业务模式 {error_class.value} / {situation_signature} 下存在 "
                f"{len(matches)} 条活跃提案——唯一性失效（阶段 7 · R72）"
            )
            raise ConstitutionViolationError(msg)
        return matches[0] if matches else None

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
        # 🔴 与回合仓储同理：期望版本交给暂存区，提交时在锁内复核。
        self._uow.stage_proposal(proposal, expected_version=expected_version)

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
