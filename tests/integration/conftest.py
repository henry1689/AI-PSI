"""集成测试夹具（需要 PostgreSQL）。

🔴 **四个关键设计决定：**

1. **独立的测试数据库。** 集成测试会 ``TRUNCATE`` 数据表；
   指错库就是开发数据的静默丢失。测试库从开发库名派生（追加 ``_test``），
   且 :meth:`Settings.resolved_test_database_url` 会拒绝两者相同的情形。

2. **``NullPool``。** pytest-asyncio 为每个用例创建独立事件循环，
   而连接池中的连接绑定在创建它的循环上。池化会跨循环复用连接并抛
   ``attached to a different loop``。NullPool 每条语句新建连接，从根上避开。

3. **每个用例前清库。** 不做"事务回滚"式隔离——工作单元自己会
   ``commit()``，那会跨出外层事务，反而测不到真实的提交路径。

4. **建库与迁移是同步流程。** alembic 内部用 ``asyncio.Runner`` 自建事件循环，
   在已运行的事件循环里调用会直接报错——因此那些夹具必须是**同步**的。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.config import Settings, get_settings
from ai_psi.infrastructure.asyncio_compat import (
    install_selector_loop_policy,
    make_selector_loop,
)
from ai_psi.infrastructure.db.session import create_session_factory
from ai_psi.infrastructure.db.unit_of_work import make_unit_of_work_factory
from ai_psi.providers.embeddings import LocalHashingEmbedding

# 🔴 除了下面的 hook，还要在导入时设置一次全局策略。
# 原因：hook 只对**异步测试**生效；当**同步测试**请求异步夹具
# （如 `def test_x(self, uow_factory)`）时，pytest-asyncio 会走另一条
# 创建循环的路径，那条路径读的是全局策略。两条路都要覆盖。
install_selector_loop_policy()

#: 清库时按依赖倒序处理。
#:
#: ⚠️ 阶段 5 起 ``memory_embeddings`` 有指向 ``memories`` 的外键，
#: 顺序不再是"仅作防御"：先清被引用的一方会直接违反外键约束。
#: （``TRUNCATE ... CASCADE`` 会自动带上引用它的表，
#: 但显式列出更清楚，也避免依赖 CASCADE 的隐式行为。）
_TABLES = (
    "events",
    "cognitive_rounds",
    "idempotency_keys",
    "memory_embeddings",
    "memories",
    "improvement_proposals",
)


def pytest_asyncio_loop_factories(
    config: pytest.Config,
    item: pytest.Item,
) -> dict[str, Callable[[], asyncio.AbstractEventLoop]] | None:
    """让 pytest-asyncio 用 selector 事件循环创建测试用循环。

    Windows 默认的 ``ProactorEventLoop`` 不被 psycopg 异步驱动支持
    （见 :mod:`ai_psi.infrastructure.asyncio_compat`）。

    ⚠️ 这里用的是 pytest-asyncio 1.4 的 **hook**，而不是已被废弃的
    ``event_loop_policy`` 夹具——后者会触发 ``PytestDeprecationWarning``，
    在本项目 ``filterwarnings = ["error"]`` 的配置下直接变成测试失败。

    Args:
        config: pytest 配置（未使用）。
        item: 当前用例（未使用）。

    Returns:
        循环名到工厂的映射。**不能返回空映射**——那会让 pytest-asyncio 报错。
    """
    del config, item
    return {"selector": make_selector_loop}


def _to_sync_dsn(url: str) -> str:
    """把 SQLAlchemy 异步连接串转成 psycopg 同步 DSN。

    仅用于建库与 alembic——它们都是同步流程。

    Args:
        url: 形如 ``postgresql+psycopg://...`` 的连接串。

    Returns:
        形如 ``postgresql://...`` 的 DSN。
    """
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _ensure_database(test_url: str) -> None:
    """若测试库不存在则创建。

    Args:
        test_url: 测试库连接串。
    """
    base, _, name = test_url.rpartition("/")
    admin_dsn = _to_sync_dsn(f"{base}/postgres")

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if not exists:
            # 库名来自本地配置而非用户输入；用标识符引号包裹以处理特殊字符
            conn.execute(f'CREATE DATABASE "{name}"')

    with psycopg.connect(_to_sync_dsn(test_url)) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.commit()


def _run_migrations(test_url: str) -> None:
    """在测试库上执行 alembic 迁移到 head。

    Args:
        test_url: 测试库连接串。
    """
    config = Config("alembic.ini")
    # env.py 优先读取本属性，从而覆盖它默认从 Settings 取的**开发库**连接串
    config.attributes["override_database_url"] = test_url
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    """指向测试库的配置。"""
    base = get_settings()
    return base.model_copy(update={"database_url": base.resolved_test_database_url()})


@pytest.fixture(scope="session")
def _test_database(test_settings: Settings) -> Iterator[str]:
    """确保测试库存在且结构最新（同步夹具，见模块文档第 4 条）。"""
    url = test_settings.database_url
    _ensure_database(url)
    _run_migrations(url)
    yield url


@pytest.fixture(scope="session")
def engine(_test_database: str, test_settings: Settings) -> Iterator[AsyncEngine]:
    """测试库引擎。

    使用 ``NullPool``：每个用例各自的事件循环，池化连接会跨循环复用并失败。
    因为没有池化连接，退出时无需 ``dispose()``。
    """
    yield create_async_engine(test_settings.database_url, poolclass=NullPool)


@pytest.fixture
async def clean_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    """清空数据表，保证用例之间互不影响。

    在**每个用例开始前**执行——这样失败用例留下的数据仍可用于排查。
    """
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """会话工厂。"""
    return create_session_factory(engine)


@pytest.fixture
def embeddings() -> LocalHashingEmbedding:
    """向量 Provider。

    用**本地确定性实现**：契约测试跑的是存储语义，不是检索质量。
    它对随机性与网络零依赖，因此断言不会因为供应商状态而间歇失败。
    """
    return LocalHashingEmbedding()


@pytest.fixture
async def uow_factory(
    engine: AsyncEngine,
    clean_tables: None,
    embeddings: LocalHashingEmbedding,
) -> UnitOfWorkFactory:
    """工作单元工厂（每个用例前清库）。"""
    return make_unit_of_work_factory(create_session_factory(engine), embeddings)
