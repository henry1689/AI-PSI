"""数据库引擎与会话工厂。

采用**异步** SQLAlchemy（psycopg3 异步驱动）。

理由：认知流水线本身是异步的（模型调用、并发检索），
阶段 3 的 FastAPI 也是异步的。现在用同步再在阶段 3 转换，
意味着重写全部仓储签名与事务边界——代价远高于一开始就选异步。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

__all__ = ["create_session_factory"]

# ⚠️ 这里曾经有一个 ``create_engine(settings, *, pool_size=5, ...)``。
# 它**全仓零引用**：真正建引擎的是组合根
# （``container.py``：``create_async_engine(url, pool_pre_ping=True)``）
# 与集成夹具（``tests/integration/conftest.py``：``poolclass=NullPool``）。
#
# 🔴 **它不只是"几行没人用的代码"，而是一个会骗人的配置面**：
# 它把 ``pool_size`` / ``max_overflow`` 摆在签名里，读代码的人会以为
# 连接池是按这两个数配置的——而实际生效的是 ``create_async_engine``
# 的默认值。改池大小的人会改到这里，然后发现没有任何变化。
#
# 阶段 6.5 §八 评审 A 找出它之后，按 §四 的三选一删掉了。
# 关于 NullPool 的那段知识没有丢——它本来就在
# ``tests/integration/conftest.py`` 里，而且写在**真正用它的地方**。


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
