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

from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_psi.domain.enums import ErrorType, ProposalStatus
from ai_psi.domain.exceptions import (
    ConflictError,
    ConstitutionViolationError,
    NotFoundError,
    OptimisticLockError,
    ProposalPatternConflictError,
)
from ai_psi.domain.improvement_proposals import (
    TERMINAL_PROPOSAL_STATUSES,
    ImprovementProposal,
    active_pattern_key,
    assert_status_is_a_member,
)
from ai_psi.infrastructure.db.errors import is_unique_violation, unique_violation_constraint
from ai_psi.infrastructure.db.mappers import proposal_to_row, proposal_to_values, row_to_proposal
from ai_psi.infrastructure.db.models import ACTIVE_PATTERN_INDEX_NAME, ImprovementProposalRow

__all__ = ["SqlAlchemyProposalRepository"]

#: ``find_active_for_pattern`` 的读取上限。
#:
#: 🔴 **2 而不是 1**：只取 1 行就永远发现不了"唯一索引不在"这件事。
#: 取 2 行是为了**证明只有 1 行**。
_ACTIVE_LOOKUP_LIMIT: Final[int] = 2


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

        🔴 **唯一冲突在 `flush()` 这一行抛出，不会推迟到 `commit()`。**
        `session.add()` 只做登记；真正执行 INSERT、并因此触发唯一索引
        检查的就是下面那次显式 `flush()`。调用方因此可以精确地把
        `except` 放在调用本方法的那一层，而**不必**假设冲突会在提交时出现。

        ⚠️ **flush 失败会毒化本事务**：PostgreSQL 把当前事务置为 aborted，
        此后任何语句都会以 ``InFailedSqlTransaction`` 失败。
        本方法**不**试图补救（不开 savepoint），因为调用方在这个事务里
        已经无事可做——它必须让工作单元整体回滚，再开**新的事务**去读回
        胜出的那条。见 :meth:`~ai_psi.application.learning_service.LearningService.review`。

        Raises:
            ProposalPatternConflictError: 同一业务模式下已有一条**活跃**提案。
            ConflictError: 主键已存在。
            ConstitutionViolationError: 状态不是 ``ProposalStatus`` 的成员。
        """
        assert_status_is_a_member(proposal)
        self._session.add(proposal_to_row(proposal))
        try:
            await self._session.flush()
        except Exception as exc:
            if not is_unique_violation(exc):
                raise
            # 🔴 **两类唯一冲突必须分开。** 主键是 uuid4，正常路径不可能撞；
            # 业务键撞车是并发下的**预期结果**，调用方要把它翻译成
            # "该模式已被覆盖"而不是报错。
            #
            # `key` 为 `None`（空 applicability）时不可能撞上那个索引
            # ——部分谓词把它排除在外了——所以这里顺带是一道自洽性检查：
            # 键为空却撞上模式索引，说明索引定义与代码对不上，
            # 那时按普通冲突处理（上层会原样抛出）比编一个原因好。
            key = active_pattern_key(proposal)
            constraint = unique_violation_constraint(exc)
            if key is not None and constraint == ACTIVE_PATTERN_INDEX_NAME:
                error_class, signature = key
                msg = f"该模式已有一条活跃提案：{error_class} / {signature}"
                raise ProposalPatternConflictError(
                    msg,
                    context={"error_class": error_class, "situation_signature": signature},
                ) from exc
            msg = f"改进提案已存在：{proposal.id}"
            raise ConflictError(msg, context={"proposal_id": str(proposal.id)}) from exc

    async def get(self, proposal_id: UUID) -> ImprovementProposal | None:
        """按 id 读取提案。"""
        row = await self._session.get(ImprovementProposalRow, proposal_id)
        return None if row is None else row_to_proposal(row)

    async def find_active_for_pattern(
        self, *, error_class: ErrorType, situation_signature: str
    ) -> ImprovementProposal | None:
        """取该业务模式下**唯一**那条活跃提案；没有则 ``None``。

        🔴 **读取上限是 2 行，且查满 2 行就报错——不是取第一条返回。**

        正常情况下唯一索引保证至多一条。查出两条只可能是
        **索引不在了**（被人手工删掉、或迁移没跑）。这时"挑一条返回"
        会让调用方以为一切正常，而唯一性其实已经失效——
        一个只在并发下才暴露的缺陷，会变成永远不被发现的那种。
        宁可在这里大声失败。

        ⚠️ **PostgreSQL 数组下标从 1 起**：``applicability[1]`` 对应
        Python 里的 ``applicability[0]``，也就是 ``_covered_keys()``
        用的那个键。

        Raises:
            ConstitutionViolationError: 同一业务键下存在**多于一条**活跃提案
                ——唯一索引缺失或未生效。
        """
        stmt = (
            select(ImprovementProposalRow)
            .where(
                ImprovementProposalRow.error_class == error_class.value,
                ImprovementProposalRow.applicability[1] == situation_signature,
                ImprovementProposalRow.status.not_in(
                    [item.value for item in TERMINAL_PROPOSAL_STATUSES]
                ),
            )
            .order_by(ImprovementProposalRow.created_at, ImprovementProposalRow.id)
            .limit(_ACTIVE_LOOKUP_LIMIT)
        )
        rows = list((await self._session.execute(stmt)).scalars())
        if len(rows) > 1:
            msg = (
                f"业务模式 {error_class.value} / {situation_signature} 下存在 "
                f"{len(rows)} 条活跃提案——唯一索引 "
                f"{ACTIVE_PATTERN_INDEX_NAME} 缺失或未生效（阶段 7 · R72）"
            )
            raise ConstitutionViolationError(msg)
        return None if not rows else row_to_proposal(rows[0])

    async def save(self, proposal: ImprovementProposal, *, expected_version: int) -> None:
        """带乐观锁的更新。

        🔴 **绝不静默覆盖。** 两条并发的状态流转（比如"批准"与"驳回"同时到达）
        如果后写的那条覆盖了先写的，结果是一条提案**同时**被批准和驳回——
        而事件流里两条理由都在。乐观锁在这里不是性能优化，是正确性要求。

        Raises:
            OptimisticLockError: 版本不匹配。
            NotFoundError: 提案不存在。
            ConstitutionViolationError: 状态不是 ``ProposalStatus`` 的成员。
        """
        assert_status_is_a_member(proposal)
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
