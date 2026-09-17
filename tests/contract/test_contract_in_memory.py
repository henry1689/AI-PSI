"""内存适配器的契约测试（零依赖，可在 ``make test-unit`` 下运行）。

配套的 PostgreSQL 实现在 ``tests/integration/test_contract_postgres.py``，
跑的是**同一组断言**。
"""

from __future__ import annotations

import pytest

from ai_psi.infrastructure.in_memory.memory_store import InMemoryMemoryRepository
from ai_psi.infrastructure.in_memory.store import InMemoryStore
from ai_psi.infrastructure.in_memory.unit_of_work import make_in_memory_unit_of_work_factory
from tests.contract.base import (
    EventStoreContract,
    IdempotencyContract,
    MemoryRepositoryContract,
    RoundRepositoryContract,
    UnitOfWorkContract,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def uow_factory():
    """每次用例一块全新的内存存储。"""
    return make_in_memory_unit_of_work_factory(InMemoryStore())


@pytest.fixture
async def event_store(uow_factory):
    async with uow_factory() as uow:
        yield uow.events


@pytest.fixture
async def round_repository(uow_factory):
    async with uow_factory() as uow:
        yield uow.rounds


@pytest.fixture
async def idempotency_store(uow_factory):
    async with uow_factory() as uow:
        yield uow.idempotency


@pytest.fixture
def memory_repository() -> InMemoryMemoryRepository:
    return InMemoryMemoryRepository()


class TestInMemoryEventStore(EventStoreContract):
    """事件存储的内存实现。"""


class TestInMemoryRoundRepository(RoundRepositoryContract):
    """回合仓储的内存实现。"""


class TestInMemoryIdempotencyStore(IdempotencyContract):
    """幂等键存储的内存实现。"""


class TestInMemoryMemoryRepository(MemoryRepositoryContract):
    """长期记忆仓储的内存实现。"""


class TestInMemoryUnitOfWork(UnitOfWorkContract):
    """工作单元的内存实现。"""
