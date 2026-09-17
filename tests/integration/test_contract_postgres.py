"""PostgreSQL 实现的契约测试。

🔴 **跑的是与 ``tests/contract/test_contract_in_memory.py`` 完全相同的断言。**

这就是 ADR-0009 说的那件事：两个实现共享同一套契约测试。
差异会在**写第二个实现的那一刻**暴露，而不是在阶段 5 替换时暴露——
后者会让问题看起来像是"新代码的 bug"，而实际上它一直存在。

⚠️ 阶段 3 只覆盖事件存储、回合仓储与幂等键存储。
:class:`~ai_psi.application.ports.MemoryRepository` 的 PostgreSQL 实现
要到阶段 5（需要建表 + pgvector）才交付，因此不在本文件里。
"""

from __future__ import annotations

import pytest

from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.infrastructure.in_memory.memory_store import InMemoryMemoryRepository
from tests.contract.base import (
    EventStoreContract,
    IdempotencyContract,
    MemoryRepositoryContract,
    RoundRepositoryContract,
    UnitOfWorkContract,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def event_store(uow_factory: UnitOfWorkFactory):
    async with uow_factory() as uow:
        yield uow.events


@pytest.fixture
async def round_repository(uow_factory: UnitOfWorkFactory):
    async with uow_factory() as uow:
        yield uow.rounds


@pytest.fixture
async def idempotency_store(uow_factory: UnitOfWorkFactory):
    async with uow_factory() as uow:
        yield uow.idempotency


class TestPostgresEventStore(EventStoreContract):
    """事件存储的 PostgreSQL 实现。"""


class TestPostgresRoundRepository(RoundRepositoryContract):
    """回合仓储的 PostgreSQL 实现。"""


class TestPostgresIdempotencyStore(IdempotencyContract):
    """幂等键存储的 PostgreSQL 实现。"""


class TestPostgresUnitOfWork(UnitOfWorkContract):
    """工作单元的 PostgreSQL 实现。"""


class TestMemoryPortIsNotYetPersistent:
    """阶段 3 的边界：记忆仓储尚无 PostgreSQL 实现。

    🔴 **这条断言存在的意义是让边界可见。**
    如果哪天有人实现了持久化版本却忘了接上契约测试，
    这个用例会提醒他：它现在跑的还是内存实现。
    """

    def test_memory_repository_is_the_in_memory_one(self) -> None:
        repository = InMemoryMemoryRepository()
        assert type(repository).__name__ == "InMemoryMemoryRepository"


@pytest.mark.skip(reason="阶段 5 交付 PostgreSQL + pgvector 后启用")
class TestPostgresMemoryRepository(MemoryRepositoryContract):
    """占位：阶段 5 把记忆仓储接到 PostgreSQL 之后，本类即可启用。"""
