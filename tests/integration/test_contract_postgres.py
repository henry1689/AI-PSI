"""PostgreSQL 实现的契约测试。

🔴 **跑的是与 ``tests/contract/test_contract_in_memory.py`` 完全相同的断言。**

这就是 ADR-0009 说的那件事：两个实现共享同一套契约测试。
差异会在**写第二个实现的那一刻**暴露，而不是在阶段 5 替换时暴露——
后者会让问题看起来像是"新代码的 bug"，而实际上它一直存在。

阶段 3 覆盖事件存储、回合仓储与幂等键存储；阶段 5 补齐
:class:`~ai_psi.application.ports.MemoryRepository`——
阶段 3 留下的 ``TestPostgresMemoryRepository`` 占位类**直接启用**，
没有另写一套断言。
"""

from __future__ import annotations

import pytest

from ai_psi.application.ports import UnitOfWorkFactory
from tests.contract.base import (
    EventStoreContract,
    IdempotencyContract,
    MemoryRepositoryContract,
    ProposalRepositoryContract,
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


@pytest.fixture
async def memory_repository(uow_factory: UnitOfWorkFactory):
    async with uow_factory() as uow:
        yield uow.memories


@pytest.fixture
async def proposal_repository(uow_factory: UnitOfWorkFactory):
    async with uow_factory() as uow:
        yield uow.proposals


class TestPostgresEventStore(EventStoreContract):
    """事件存储的 PostgreSQL 实现。"""


class TestPostgresRoundRepository(RoundRepositoryContract):
    """回合仓储的 PostgreSQL 实现。"""


class TestPostgresIdempotencyStore(IdempotencyContract):
    """幂等键存储的 PostgreSQL 实现。"""


class TestPostgresUnitOfWork(UnitOfWorkContract):
    """工作单元的 PostgreSQL 实现。"""


class TestPostgresMemoryRepository(MemoryRepositoryContract):
    """长期记忆仓储的 PostgreSQL + pgvector 实现。

    阶段 3 留下的占位类在这里启用——断言**一行未改**。
    """


class TestPostgresProposalRepository(ProposalRepositoryContract):
    """改进提案仓储的 PostgreSQL 实现。

    🔴 与内存实现跑的是**同一组断言**。规格里的
    "同一时间戳的提案按 id 排序"这类条款，正是只能在两个实现的
    对比中才暴露出来的差异。
    """
