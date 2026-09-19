"""专用评测 / 参考数据库的创建、迁移与清理（阶段 7 · S2）。

## 这里是"两个库"真正落地的位置

:mod:`ai_psi.evaluation.isolation` 负责**判断**两个 URL 安全不安全；
本模块负责**动手**：建库、迁移到 head、读取版本、最后删掉。

## 为什么用 Alembic 而不是 ``metadata.create_all``

``create_all`` 会把"当前模型长什么样"直接建成表，绕过迁移历史。
它与真实部署的库会有细微差异（列顺序、默认值、索引谓词），
而评测要证明的恰恰是"在**真实**的库上能跑"。因此两个库都用正式迁移升级。

## 为什么同步流程要丢进线程

Alembic 内部用 ``asyncio.Runner`` 自建事件循环（见 ``migrations/env.py``），
在已经运行的事件循环里直接调用会报 "cannot be called from a running event
loop"。``asyncio.to_thread`` 把整个调用挪到没有循环的线程里，
放它自己建一个——这是集成测试夹具一直在用的同一套办法
（``tests/integration/conftest.py`` 的模块文档第 4 条）。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Final
from urllib.parse import urlunsplit

import psycopg
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ai_psi.evaluation.isolation import (
    FORBIDDEN_DATABASE_NAMES,
    DatabaseIdentity,
    IsolationError,
)

__all__ = [
    "REQUIRED_TABLES",
    "alembic_head",
    "create_engine_for",
    "current_revision",
    "drop_database",
    "ensure_database",
    "require_at_head",
    "upgrade_to_head",
]

#: 仓库根（``<root>/src/ai_psi/evaluation/postgres.py`` 往上四层）。
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]

#: 迁移到 head 之后**必须**存在的表。
#:
#: 🔴 这一条是用来把"没有用 stamp 糊弄"变成**可验证**的：
#: 一个空库被 ``alembic stamp head`` 过之后，``alembic_version`` 会说
#: "我已是 head"，但一张表都没有。只比较版本号看不出这件事，
#: 而比较版本号**加**表是否存在能看出来。
REQUIRED_TABLES: Final[tuple[str, ...]] = (
    "cognitive_rounds",
    "events",
    "idempotency_keys",
    "improvement_proposals",
    "memories",
    "memory_embeddings",
)

#: 可用于建库 / 删库的库名字符集。
#:
#: 🔴 库名会被拼进 ``CREATE DATABASE "..."`` 与 ``DROP DATABASE "..."``。
#: 隔离守卫保证的是**后缀与禁用名**，不保证字符集；这里补上后者，
#: 于是"引号、分号能不能进库名"不再是靠标识符引号兜着。
_MANAGEABLE_NAME: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_]+$")


def _alembic_config(target_url: str) -> Config:
    """构造指向**指定库**的 Alembic 配置。

    ``script_location`` 与 ``prepend_sys_path`` 都写成绝对路径：
    这样无论从哪个工作目录调用，读到的都是本仓库的迁移，
    而不是"恰好 cwd 下的某个 migrations 目录"。

    Args:
        target_url: 迁移目标连接串。

    Returns:
        配置好了 ``override_database_url`` 的 Alembic 配置。
    """
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    config.set_main_option("prepend_sys_path", str(_REPO_ROOT))
    # env.py 优先读这个属性，从而不去碰 Settings 里的开发库连接串。
    config.attributes["override_database_url"] = target_url
    return config


def _sync_dsn(identity: DatabaseIdentity, *, database: str | None = None) -> str:
    """把身份转成 psycopg 同步 DSN（去掉 SQLAlchemy 的方言后缀）。

    同样是**结构化重建**：换 scheme、换库名，其余原样。
    """
    name = identity.database if database is None else database
    return urlunsplit(("postgresql", identity.netloc, f"/{name}", identity.query, ""))


def _require_manageable_name(identity: DatabaseIdentity, *, development: DatabaseIdentity) -> None:
    """建库 / 删库前的二次守卫。

    🔴 "删除前必须再次执行名称守卫"不是重复劳动：从守卫通过到真正执行
    删除之间，可能隔着建库、迁移、跑完 10 个案例等许多步骤，
    而这条库名会一路传下来。多验一次的成本是几微秒，
    验错的成本是删掉一个不是评测库的库。

    Args:
        identity: 待操作的库。
        development: 开发库身份（不得被删）。

    Raises:
        IsolationError: 库名不安全。
    """
    name = identity.database
    if not _MANAGEABLE_NAME.match(name):
        raise IsolationError(f"库名含有不允许的字符，拒绝执行建库 / 删库操作：{name!r}")
    if name in FORBIDDEN_DATABASE_NAMES:
        raise IsolationError(f"拒绝把系统库 {name!r} 作为评测 / 参考库来操作")
    if name == development.database:
        raise IsolationError(f"拒绝操作开发库 {name!r}")


def create_engine_for(identity: DatabaseIdentity) -> AsyncEngine:
    """为某个库建一个**独立的**引擎。

    🔴 每个库各自一个引擎——参考库与评测库不共享 engine、不共享连接池、
    不共享 session。共享池会让"这条查询打到哪个库"取决于池里恰好
    还回哪条连接，而隔离证据正是建立在"某个写入一定落在评测库"之上的。

    ``NullPool`` 与集成测试一致：每条语句新建连接，用完即关。

    Args:
        identity: 目标库。

    Returns:
        该库的异步引擎。
    """
    return create_async_engine(identity.normalized_url, poolclass=NullPool)


def alembic_head() -> str:
    """当前唯一的 Alembic head。

    Returns:
        head 的 revision 标识。

    Raises:
        IsolationError: head 不唯一（分叉的迁移历史无法确定该升到哪）。
    """
    config = Config(str(_REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    heads = tuple(ScriptDirectory.from_config(config).get_heads())
    if len(heads) != 1:
        raise IsolationError(
            f"Alembic 有 {len(heads)} 个 head，无法确定评测目标版本：{sorted(heads)}"
        )
    return str(heads[0])


async def current_revision(engine: AsyncEngine) -> str | None:
    """读库上记录的迁移版本。

    Args:
        engine: 目标库引擎。

    Returns:
        ``alembic_version`` 里的版本号；**从未迁移过**（表不存在）时为 ``None``。
    """
    async with engine.connect() as connection:
        try:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
        except ProgrammingError:
            # 表不存在 == 这个库没有迁移过。返回 None 让上层按"不是 head"处理，
            # 而不是在这里把它当成"已是最新"。
            return None
        row = result.fetchone()
    return None if row is None else str(row[0])


async def missing_tables(engine: AsyncEngine) -> tuple[str, ...]:
    """返回 :data:`REQUIRED_TABLES` 里**不存在**的那些表。

    Args:
        engine: 目标库引擎。

    Returns:
        缺失的表名（升序）；全部存在时为空。
    """
    absent: list[str] = []
    async with engine.connect() as connection:
        for table in REQUIRED_TABLES:
            result = await connection.execute(
                text("SELECT to_regclass(:name)"), {"name": f"public.{table}"}
            )
            if result.scalar() is None:
                absent.append(table)
    return tuple(absent)


async def require_at_head(engine: AsyncEngine, *, role: str) -> str:
    """确认目标库确实迁移到了 head 且**表真的建出来了**。

    Args:
        engine: 目标库引擎。
        role: 角色名，用于报错。

    Returns:
        当前（也是 head）的 revision。

    Raises:
        IsolationError: 版本不是 head，或版本号对但表缺失
            （后者说明版本是用 ``stamp`` 伪造的，或迁移只跑了一半）。
    """
    head = alembic_head()
    current = await current_revision(engine)
    if current != head:
        raise IsolationError(
            f"{role}的迁移版本不是 head：当前 {current!r}，期望 {head!r}。"
            "请先执行 alembic upgrade head——评测不接受未迁移的库"
        )
    absent = await missing_tables(engine)
    if absent:
        raise IsolationError(
            f"{role}的版本号是 head（{head}），但缺少表 {list(absent)}。"
            "版本号与实际表结构不一致，通常意味着用 alembic stamp 伪造了迁移状态"
        )
    return head


def _ensure_database_sync(identity: DatabaseIdentity) -> bool:
    """建库（不存在时）并确保 vector 扩展就绪。同步实现。"""
    created = False
    with psycopg.connect(_sync_dsn(identity, database="postgres"), autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (identity.database,)
        ).fetchone()
        if not exists:
            # 库名已通过 _require_manageable_name：字符集受限、非系统库、非开发库。
            connection.execute(f'CREATE DATABASE "{identity.database}"')
            created = True

    with psycopg.connect(_sync_dsn(identity), autocommit=True) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
    return created


def _drop_database_sync(identity: DatabaseIdentity) -> None:
    """删库。同步实现。``WITH (FORCE)`` 断开残留连接（PostgreSQL 13+）。"""
    with psycopg.connect(_sync_dsn(identity, database="postgres"), autocommit=True) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{identity.database}" WITH (FORCE)')


def _upgrade_sync(identity: DatabaseIdentity) -> None:
    """跑 Alembic 迁移到 head。同步实现。"""
    command.upgrade(_alembic_config(identity.normalized_url), "head")


async def ensure_database(identity: DatabaseIdentity, *, development: DatabaseIdentity) -> bool:
    """若目标库不存在则创建，并确保 vector 扩展就绪。

    Args:
        identity: 目标库。
        development: 开发库身份（用于二次名称守卫）。

    Returns:
        ``True`` 表示本次**新建**了库。

    Raises:
        IsolationError: 库名不安全。
    """
    _require_manageable_name(identity, development=development)
    return await asyncio.to_thread(_ensure_database_sync, identity)


async def upgrade_to_head(identity: DatabaseIdentity) -> None:
    """把目标库迁移到 Alembic head。

    Args:
        identity: 目标库。
    """
    await asyncio.to_thread(_upgrade_sync, identity)


async def drop_database(identity: DatabaseIdentity, *, development: DatabaseIdentity) -> None:
    """删除专用临时库。

    🔴 **删除前重新跑一遍名称守卫**（见 :func:`_require_manageable_name`）。

    Args:
        identity: 要删除的库。
        development: 开发库身份（用于二次名称守卫）。

    Raises:
        IsolationError: 库名不安全。
    """
    _require_manageable_name(identity, development=development)
    await asyncio.to_thread(_drop_database_sync, identity)
