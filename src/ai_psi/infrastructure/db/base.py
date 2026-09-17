"""SQLAlchemy 声明式基类与命名约定。

⚠️ **ORM 实体不是领域对象**（ADR-0006）。

`domain/`、`infrastructure/db/models.py`、`api/schemas.py` 是**三套独立定义**，
转换必须走显式的映射函数（:mod:`ai_psi.infrastructure.db.mappers`），
**不允许 `model_validate` 一键互转**。这样一次数据库结构变更不会自动
波及领域层与 API 层——那正是层间耦合最常见的来源。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

__all__ = ["NAMING_CONVENTION", "Base", "enum_check_expression"]


#: 约束命名约定。
#:
#: 🔴 **这不是风格偏好，是迁移能否回滚的前提。**
#: 不指定约定时，PostgreSQL 会为 CHECK/UNIQUE 约束生成随机名，
#: 而 Alembic 的 `downgrade` 需要按名字 `DROP CONSTRAINT`——
#: 随机名意味着**迁移无法可靠回滚**。
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """全部 ORM 实体的基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def enum_check_expression(column: str, enum_cls: type[StrEnum]) -> str:
    """由 Python 枚举生成 CHECK 约束表达式。

    好处是**数据库约束与 Python 枚举自动保持同步**——
    新增一个枚举成员而不写迁移，数据库会直接拒绝该值。

    Args:
        column: 列名。
        enum_cls: 字符串枚举类型。

    Returns:
        形如 ``state IN ('created', 'triaging', ...)`` 的 SQL 表达式。
    """
    allowed = ", ".join(f"'{member.value}'" for member in enum_cls)
    return f"{column} IN ({allowed})"
