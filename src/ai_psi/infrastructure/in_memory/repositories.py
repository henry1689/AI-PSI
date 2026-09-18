"""回合仓储与幂等键存储的内存实现。

语义与 PostgreSQL 实现**逐条对齐**：

* ``save`` 用 ``expected_version`` 做乐观锁，冲突抛
  :class:`~ai_psi.domain.exceptions.OptimisticLockError`（**不静默覆盖**）；
* ``reserve`` 的占位是原子的——同一 key 只有一个调用方拿到 ``RESERVED``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from ai_psi.application.ports import IdempotencyOutcome, IdempotencyReservation
from ai_psi.domain.cognitive_rounds import CognitiveRound
from ai_psi.domain.exceptions import ConflictError, NotFoundError, OptimisticLockError
from ai_psi.infrastructure.in_memory.store import IdempotencyRecord

if TYPE_CHECKING:  # 运行期不导入，避免循环引用
    from ai_psi.infrastructure.in_memory.unit_of_work import InMemoryUnitOfWork

__all__ = ["InMemoryIdempotencyStore", "InMemoryRoundRepository"]


class InMemoryRoundRepository:
    """认知回合仓储的内存实现。"""

    def __init__(self, uow: InMemoryUnitOfWork) -> None:
        """初始化。

        Args:
            uow: 所属工作单元。
        """
        self._uow = uow

    async def add(self, round_: CognitiveRound) -> None:
        """插入新回合。

        Raises:
            ConflictError: 主键已存在。
        """
        if self._uow.visible_round(round_.id) is not None:
            msg = f"认知回合已存在：{round_.id}"
            raise ConflictError(msg, context={"cognitive_round_id": str(round_.id)})
        self._uow.stage_round(round_)

    async def get(self, round_id: UUID) -> CognitiveRound | None:
        """按 id 读取回合。"""
        return self._uow.visible_round(round_id)

    async def save(self, round_: CognitiveRound, *, expected_version: int) -> None:
        """带乐观锁的更新。

        🔴 版本不匹配时**绝不静默覆盖**——那是并发场景下最难排查的一类
        数据损坏（ADR-0002）。

        Raises:
            NotFoundError: 回合不存在。
            OptimisticLockError: 版本不匹配。
        """
        current = self._uow.visible_round(round_.id)
        if current is None:
            msg = f"认知回合不存在：{round_.id}"
            raise NotFoundError(msg, context={"cognitive_round_id": str(round_.id)})

        if current.version != expected_version:
            msg = (
                f"乐观锁冲突：回合 {round_.id} 期望版本 {expected_version}，"
                f"实际版本 {current.version}。这说明期间有其他写入者提交了变更"
            )
            raise OptimisticLockError(
                msg,
                entity_type="CognitiveRound",
                entity_id=str(round_.id),
                expected_version=expected_version,
                actual_version=current.version,
            )
        # 🔴 期望版本一并交给暂存区，**提交时还要再复核一次**。
        # 这一次检查读的是"可见版本"，而并发冲突恰恰发生在
        # 暂存之后、提交之前：两个事务都读到 v1、都过得了这一关，
        # 后提交的那个静默覆盖前一个（见 `InMemoryStore.apply`）。
        self._uow.stage_round(round_, expected_version=expected_version)

    async def find_by_idempotency_key(self, key: str) -> CognitiveRound | None:
        """按幂等键查找既有回合。"""
        for round_ in self._uow.visible_rounds():
            if round_.idempotency_key == key:
                return round_
        return None

    async def list_all(self, *, limit: int | None = None) -> list[CognitiveRound]:
        """列出回合（``created_at`` 升序，同刻按 id 升序）。"""
        ordered = sorted(
            self._uow.visible_rounds(),
            key=lambda item: (item.created_at, str(item.id)),
        )
        return ordered if limit is None else ordered[:limit]


class InMemoryIdempotencyStore:
    """幂等键存储的内存实现。

    🔴 占位判定是**纯函数式**的：先看已提交与暂存两处，
    再一次性决定 RESERVED / REPLAY / CONFLICT。
    没有"先查后插"的竞态窗口——因为内存实现里判定与写入之间没有 await。
    """

    def __init__(self, uow: InMemoryUnitOfWork) -> None:
        """初始化。

        Args:
            uow: 所属工作单元。
        """
        self._uow = uow

    async def reserve(self, *, key: str, request_hash: str) -> IdempotencyReservation:
        """原子地占位一个幂等键。

        Args:
            key: 客户端提供的幂等键。
            request_hash: 请求体哈希。

        Returns:
            占位结论。
        """
        existing = self._uow.visible_reservation(key)
        if existing is None:
            self._uow.stage_reservation(
                IdempotencyRecord(key=key, request_hash=request_hash, cognitive_round_id=None)
            )
            return IdempotencyReservation(outcome=IdempotencyOutcome.RESERVED)

        if existing.request_hash != request_hash:
            return IdempotencyReservation(outcome=IdempotencyOutcome.CONFLICT)
        if existing.cognitive_round_id is None:
            return IdempotencyReservation(outcome=IdempotencyOutcome.CONFLICT)
        return IdempotencyReservation(
            outcome=IdempotencyOutcome.REPLAY,
            cognitive_round_id=existing.cognitive_round_id,
        )

    async def bind(self, *, key: str, cognitive_round_id: UUID) -> None:
        """把占位与已创建的回合绑定。

        Raises:
            ConflictError: 该 key 从未被占位。
        """
        existing = self._uow.visible_reservation(key)
        if existing is None:
            msg = f"幂等键 {key!r} 尚未占位，无法绑定回合"
            raise ConflictError(msg, context={"idempotency_key": key})
        self._uow.stage_reservation(
            IdempotencyRecord(
                key=key,
                request_hash=existing.request_hash,
                cognitive_round_id=cognitive_round_id,
            )
        )
