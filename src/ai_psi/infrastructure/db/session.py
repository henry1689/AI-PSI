"""数据库引擎与会话工厂。

采用**异步** SQLAlchemy（psycopg3 异步驱动）。

理由：认知流水线本身是异步的（模型调用、并发检索），
阶段 3 的 FastAPI 也是异步的。现在用同步再在阶段 3 转换，
意味着重写全部仓储签名与事务边界——代价远高于一开始就选异步。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from ai_psi.config import Settings

__all__ = ["create_engine", "create_session_factory"]


def create_engine(
    settings: Settings,
    *,
    echo: bool = False,
    pool_size: int = 5,
    max_overflow: int = 10,
    use_null_pool: bool = False,
) -> AsyncEngine:
    """按配置创建异步引擎。

    Args:
        settings: 运行配置。
        echo: 是否回显 SQL（调试用，**生产必须关闭**——SQL 日志会含参数值）。
        pool_size: 连接池常驻连接数。
        max_overflow: 峰值时允许超出池的连接数。
        use_null_pool: 使用 ``NullPool``——每条语句新建连接、用完即关。
            **集成测试必须开启**：测试为每个用例创建独立事件循环，
            而池中的连接绑定在创建它的循环上；跨循环复用连接会抛
            ``attached to a different loop``。NullPool 从根上避开这个陷阱。

    Returns:
        异步引擎。

    Note:
        ``pool_pre_ping=True`` 会在取连接时先探活。数据库重启或网络闪断后，
        池里的死连接会导致难以诊断的偶发失败——预检把它们变成一次透明重连。
    """
    if use_null_pool:
        return create_async_engine(
            settings.database_url,
            echo=echo,
            poolclass=NullPool,
        )

    return create_async_engine(
        settings.database_url,
        echo=echo,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """创建会话工厂。

    Args:
        engine: 异步引擎。

    Returns:
        会话工厂。

    Note:
        ``expire_on_commit=False`` 是异步场景下的**必需**设置：
        提交后若对象仍标记为过期，任何属性访问都会触发一次隐式 IO，
        而异步环境下这种隐式 IO 会直接抛 ``MissingGreenlet``。
    """
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
    )
