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

__all__ = [
    "UNIQUE_VIOLATION",
    "is_unique_violation",
    "unique_violation_constraint",
]

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


def unique_violation_constraint(exc: BaseException) -> str | None:
    """取唯一冲突**撞的是哪一个约束/索引**的名字。

    🔴 **存在的理由：同一张表上的两次唯一冲突是两件完全不同的事。**

    ``improvement_proposals`` 上有两类唯一性：

    * **主键**（``id`` 是 ``uuid4``）——正常路径不可能撞，
      撞了就是编程错误，应当大声失败；
    * **业务键** ``(error_class, applicability[1])``
      （阶段 7 · R72 的部分唯一索引）——两个并发学习运行撞车是
      **预期内**的结果，调用方要把它翻译成"该模式已被覆盖"。

    只判断"是不是 23505"这两件事就分不开，调用方只能二选一：
    要么把编程错误当成正常并发（静默吞掉真 bug），
    要么把正常并发当成故障（用户看到 500）。

    ⚠️ **唯一索引也会走 ``CONSTRAINT NAME`` 字段**：
    PostgreSQL 在唯一索引冲突的报文里，把**索引名**填进这个字段
    （不只是命名约束才有）。这条由
    ``test_the_pattern_conflict_names_the_index`` 直接钉住——
    哪天驱动或数据库不再这么填，那条用例会先红，
    而不是让这里的判断悄悄退化成"永远返回 ``None``"。

    Args:
        exc: 待判断的异常。

    Returns:
        约束/索引名；取不到时返回 ``None``（**不猜**——
        猜错会把两类冲突混成一类）。
    """
    for candidate in (exc, getattr(exc, "orig", None)):
        diag = getattr(candidate, "diag", None)
        name = getattr(diag, "constraint_name", None)
        if isinstance(name, str) and name:
            return name
    return None
