"""工作单元——事务边界的唯一入口。

🔴 **核心约束（任务书阶段 2 验收条件）：事务失败不得留下半成品数据。**

实现方式：``__aexit__`` 在**未显式 commit** 时一律回滚。
异常路径、提前 return、忘记提交——统统走回滚。
只有调用方主动 ``await uow.commit()`` 才会落库。
"""

from __future__ import annotations

from collections.abc import Callable
from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ai_psi.application.ports import EventStore, IdempotencyStore, MemoryRepository, RoundRepository
from ai_psi.infrastructure.db.memory_repository import SqlAlchemyMemoryRepository
from ai_psi.infrastructure.db.repositories import (
    SqlAlchemyIdempotencyStore,
    SqlAlchemyRoundRepository,
)
from ai_psi.infrastructure.event_store import SqlAlchemyEventStore
from ai_psi.providers.embeddings import EmbeddingProvider

__all__ = ["SqlAlchemyUnitOfWork", "make_unit_of_work_factory"]


def make_unit_of_work_factory(
    session_factory: async_sessionmaker[AsyncSession],
    embeddings: EmbeddingProvider,
) -> Callable[[], SqlAlchemyUnitOfWork]:
    """构造工作单元工厂。

    应用服务只依赖 :data:`ai_psi.application.ports.UnitOfWorkFactory`
    协议；本函数产出的工厂满足该协议（返回类型协变）。

    Args:
        session_factory: SQLAlchemy 会话工厂。
        embeddings: 向量 Provider。**刻意不给默认值**——记忆的向量
            必须与写入时声明在 ``Memory.embedding_version`` 上的版本一致，
            一个隐式默认值会让两者悄悄分家（见 ADR-0017）。

    Returns:
        每次调用产出一个新工作单元（即一个新事务）的可调用对象。
    """

    def _factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory, embeddings)

    return _factory


class SqlAlchemyUnitOfWork:
    """基于 SQLAlchemy 异步会话的工作单元。

    用法::

        async with uow_factory() as uow:
            await uow.rounds.add(round_)
            await uow.events.append(event)
            await uow.commit()          # 不调用它就不会落库

    Note:
        会话在 ``__init__`` 中创建，但**不会立刻建立数据库连接**——
        SQLAlchemy 的会话是惰性的，首个语句执行时才从池中取连接。
        因此提前构造工作单元是廉价且安全的。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embeddings: EmbeddingProvider,
    ) -> None:
        """初始化。

        Args:
            session_factory: 会话工厂。
            embeddings: 向量 Provider（记忆仓储在写入与检索时都要用）。
        """
        self._session_factory = session_factory
        self._session: AsyncSession = session_factory()
        self._committed = False

        # 四个仓储共享同一个会话，因此共享同一个事务。
        # 🔴 记忆也在这里——"取代旧记忆 + 写入新记忆 + 记录事件"
        # 必须是一次原子操作，否则最后一步失败会留下**没有审计记录的纠正**。
        self.events: EventStore = SqlAlchemyEventStore(self._session)
        self.rounds: RoundRepository = SqlAlchemyRoundRepository(self._session)
        self.idempotency: IdempotencyStore = SqlAlchemyIdempotencyStore(self._session)
        self.memories: MemoryRepository = SqlAlchemyMemoryRepository(self._session, embeddings)

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

        🔴 **未提交即回滚。** 这条规则是"事务失败不留半成品"的全部实现——
        无论退出原因是异常、提前 return，还是调用方忘了提交。
        """
        try:
            if not self._committed:
                await self._session.rollback()
        finally:
            await self._session.close()

    async def commit(self) -> None:
        """提交事务。只有调用本方法才会真正落库。"""
        await self._session.commit()
        self._committed = True

    async def rollback(self) -> None:
        """显式回滚。"""
        await self._session.rollback()
        self._committed = False

    @property
    def session(self) -> AsyncSession:
        """底层会话。

        仅供基础设施内部（如迁移脚本、测试夹具）使用。
        **应用服务不应直接使用它**——绕过仓储就绕过了领域规则。
        """
        return self._session
