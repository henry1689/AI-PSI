"""仓储实现：认知回合与幂等键。

与 :mod:`ai_psi.infrastructure.event_store` 一样，本模块的仓储
**不自行提交事务**——事务边界由工作单元统一管理。
"""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_psi.application.ports import IdempotencyOutcome, IdempotencyReservation
from ai_psi.domain.cognitive_rounds import CognitiveRound
from ai_psi.domain.exceptions import ConflictError, NotFoundError, OptimisticLockError
from ai_psi.infrastructure.db.errors import is_unique_violation
from ai_psi.infrastructure.db.mappers import round_to_row, round_to_values, row_to_round
from ai_psi.infrastructure.db.models import CognitiveRoundRow, IdempotencyKeyRow

__all__ = ["SqlAlchemyIdempotencyStore", "SqlAlchemyRoundRepository"]


class SqlAlchemyRoundRepository:
    """认知回合仓储（当前状态投影）。"""

    def __init__(self, session: AsyncSession) -> None:
        """初始化。

        Args:
            session: 由工作单元管理的会话。
        """
        self._session = session

    async def add(self, round_: CognitiveRound) -> None:
        """插入新回合。

        Args:
            round_: 新回合。

        Raises:
            ConflictError: 主键冲突。
        """
        self._session.add(round_to_row(round_))
        try:
            await self._session.flush()
        except Exception as exc:
            if is_unique_violation(exc):
                msg = f"认知回合已存在：{round_.id}"
                raise ConflictError(msg, context={"cognitive_round_id": str(round_.id)}) from exc
            raise

    async def get(self, round_id: UUID) -> CognitiveRound | None:
        """按 id 读取回合。

        Args:
            round_id: 回合 id。

        Returns:
            领域对象；不存在时返回 ``None``。
        """
        row = await self._session.get(CognitiveRoundRow, round_id)
        return None if row is None else row_to_round(row)

    async def save(self, round_: CognitiveRound, *, expected_version: int) -> None:
        """带乐观锁的更新。

        🔴 更新条件同时匹配 ``id`` 与 ``version``。受影响行数为 0 时，
        再区分是"记录不存在"还是"版本冲突"——两者的处理方式完全不同：
        前者是调用方的错误，后者是正常的并发路径，应当重试。

        Args:
            round_: 新的领域状态。
            expected_version: 调用方读到的版本号。

        Raises:
            OptimisticLockError: 版本不匹配。
            NotFoundError: 回合不存在。
        """
        stmt = (
            update(CognitiveRoundRow)
            .where(
                CognitiveRoundRow.id == round_.id,
                CognitiveRoundRow.version == expected_version,
            )
            .values(**round_to_values(round_))
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))

        if result.rowcount == 0:
            await self._raise_save_failure(round_.id, expected_version)

        await self._session.flush()

    async def _raise_save_failure(self, round_id: UUID, expected_version: int) -> None:
        """受影响行数为 0 时，判定真实原因并抛出对应异常。"""
        actual_version = await self._session.scalar(
            select(CognitiveRoundRow.version).where(CognitiveRoundRow.id == round_id)
        )
        if actual_version is None:
            msg = f"认知回合不存在：{round_id}"
            raise NotFoundError(msg, context={"cognitive_round_id": str(round_id)})

        msg = (
            f"乐观锁冲突：回合 {round_id} 期望版本 {expected_version}，"
            f"实际版本 {actual_version}。这说明期间有其他写入者提交了变更"
        )
        raise OptimisticLockError(
            msg,
            entity_type="CognitiveRound",
            entity_id=str(round_id),
            expected_version=expected_version,
            actual_version=actual_version,
        )

    async def find_by_idempotency_key(self, key: str) -> CognitiveRound | None:
        """按幂等键查找既有回合。

        Args:
            key: 幂等键。

        Returns:
            领域对象；不存在时返回 ``None``。
        """
        row = await self._session.scalar(
            select(CognitiveRoundRow).where(CognitiveRoundRow.idempotency_key == key)
        )
        return None if row is None else row_to_round(row)

    async def list_all(self, *, limit: int | None = None) -> list[CognitiveRound]:
        """列出回合（``created_at`` 升序，同刻按 id 升序）。

        🔴 **排序写在 SQL 里，不在 Python 里。** 在 Python 里排序意味着
        先把整张表读进内存再切 ``limit``——那样 ``limit`` 一点都没省下
        读取量，而它存在的理由恰恰是不要全读。
        """
        statement = select(CognitiveRoundRow).order_by(
            CognitiveRoundRow.created_at.asc(), CognitiveRoundRow.id.asc()
        )
        if limit is not None:
            statement = statement.limit(limit)
        rows = (await self._session.scalars(statement)).all()
        return [row_to_round(row) for row in rows]


class SqlAlchemyIdempotencyStore:
    """幂等键存储（任务书 §13.4）。

    🔴 **并发安全靠数据库，不靠应用层检查。**
    先查后插存在竞态窗口：两个并发请求可能都查到"不存在"然后都插入。
    这里用 ``INSERT ... ON CONFLICT DO NOTHING RETURNING``，
    由唯一约束仲裁——**只有一个请求能拿到返回行**。
    """

    def __init__(self, session: AsyncSession) -> None:
        """初始化。

        Args:
            session: 由工作单元管理的会话。
        """
        self._session = session

    async def reserve(self, *, key: str, request_hash: str) -> IdempotencyReservation:
        """原子地占位一个幂等键。

        Args:
            key: 客户端提供的幂等键。
            request_hash: 请求体哈希。

        Returns:
            占位结论。``CONFLICT`` 覆盖两种情况：同一 key 携带不同请求体
            （客户端 bug），以及上一次占位尚未提交（调用方应退避重试）。
        """
        stmt = (
            pg_insert(IdempotencyKeyRow)
            .values(key=key, request_hash=request_hash)
            .on_conflict_do_nothing(index_elements=["key"])
            .returning(IdempotencyKeyRow.key)
        )
        inserted = await self._session.scalar(stmt)
        if inserted is not None:
            return IdempotencyReservation(outcome=IdempotencyOutcome.RESERVED)

        existing = await self._session.get(IdempotencyKeyRow, key)
        if existing is None:
            # 极端竞态：对方在同一事务内刚回滚。当作冲突，让调用方重试。
            return IdempotencyReservation(outcome=IdempotencyOutcome.CONFLICT)

        if existing.request_hash != request_hash:
            return IdempotencyReservation(outcome=IdempotencyOutcome.CONFLICT)

        if existing.cognitive_round_id is None:
            # 占位成功但业务尚未提交——上一次请求仍在进行中
            return IdempotencyReservation(outcome=IdempotencyOutcome.CONFLICT)

        return IdempotencyReservation(
            outcome=IdempotencyOutcome.REPLAY,
            cognitive_round_id=existing.cognitive_round_id,
        )

    async def bind(self, *, key: str, cognitive_round_id: UUID) -> None:
        """把占位与已创建的回合绑定。

        🔴 **受影响行数为 0 时必须报错，而不是静默成功。**

        绑定一个从未占位的 key 说明调用流程出了问题。静默跳过会让这个
        幂等键永远处于"已占位但未绑定"的状态——此后每一次重试都会得到
        ``CONFLICT`` 而不是 ``REPLAY``，用户看到的是"重试永远失败"，
        而根因（一次错误的调用顺序）却没有任何地方记录。

        Raises:
            ConflictError: 该 key 从未被占位。
        """
        stmt = (
            update(IdempotencyKeyRow)
            .where(IdempotencyKeyRow.key == key)
            .values(cognitive_round_id=cognitive_round_id)
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        if result.rowcount == 0:
            msg = f"幂等键 {key!r} 尚未占位，无法绑定回合"
            raise ConflictError(msg, context={"idempotency_key": key})
        await self._session.flush()
