"""数据库错误分类。

🔴 **Port 不允许泄漏基础设施异常。**

:class:`~ai_psi.application.ports.EventStore` 的契约说"重复 id 抛
:class:`~ai_psi.domain.exceptions.ConflictError`"，
而不是抛一个 SQLAlchemy 的 ``IntegrityError``。
调用方按领域异常写 ``except``，一旦底层换成别的数据库、
或者驱动换了异常类，那些 ``except`` 就会静默失效。

这个模块把"这个异常是不是唯一约束冲突"的判断收在一处——
它依赖 psycopg 的 SQLSTATE，属于典型的基础设施细节，
不该散落在仓储实现里。
"""

from __future__ import annotations

from typing import Final

__all__ = ["UNIQUE_VIOLATION", "is_unique_violation"]

#: PostgreSQL 的唯一约束冲突 SQLSTATE。
UNIQUE_VIOLATION: Final[str] = "23505"


def is_unique_violation(exc: BaseException) -> bool:
    """判断异常是否为唯一约束冲突。

    SQLAlchemy 会把驱动异常包在 ``.orig`` 上，而不同层级的异常
    属性名并不统一，因此这里两个位置都查一遍。

    Args:
        exc: 待判断的异常。

    Returns:
        是唯一约束冲突返回 ``True``。
    """
    sqlstate = getattr(exc, "sqlstate", None) or getattr(
        getattr(exc, "orig", None), "sqlstate", None
    )
    return sqlstate == UNIQUE_VIOLATION
