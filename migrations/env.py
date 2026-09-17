"""Alembic 迁移环境（异步）。

连接串从 :mod:`ai_psi.config` 读取（环境变量注入），**不写进 alembic.ini**——
配置文件会进版本库，而连接串含密码。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from ai_psi.config import get_settings
from ai_psi.infrastructure.asyncio_compat import make_selector_loop
from ai_psi.infrastructure.db import models  # noqa: F401

# 🔴 必须 import 全部 ORM 模块，否则 Base.metadata 是空的，
# autogenerate 会认为"所有表都该被删除"。
from ai_psi.infrastructure.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 连接串在运行时注入。% 需要转义，否则 configparser 会尝试插值。
#
# `override_database_url` 由调用方（集成测试夹具）通过 Config.attributes 传入，
# 用于把迁移指向测试库而不是开发库。
_override_url = config.attributes.get("override_database_url")
_url = _override_url if isinstance(_override_url, str) else get_settings().database_url
config.set_main_option("sqlalchemy.url", _url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连数据库。

    用于在无法访问数据库的环境中审阅迁移内容。
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在给定连接上执行迁移。"""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """在线模式：建立异步连接并执行迁移。"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """在线模式入口。

    使用 ``asyncio.Runner(loop_factory=...)`` 而不是 ``asyncio.run()``：
    Windows 默认的 ProactorEventLoop 不被 psycopg 异步驱动支持
    （见 :mod:`ai_psi.infrastructure.asyncio_compat`）。
    """
    with asyncio.Runner(loop_factory=make_selector_loop) as runner:
        runner.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
