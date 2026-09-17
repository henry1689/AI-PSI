"""数据库基础设施。

包含 SQLAlchemy 声明式基类、ORM 实体、会话工厂、仓储与工作单元。

⚠️ 本包中的 ORM 实体**不是**领域对象（ADR-0006）。两者通过
:mod:`ai_psi.infrastructure.db.mappers` 显式转换。
"""

from __future__ import annotations

__all__: list[str] = []
