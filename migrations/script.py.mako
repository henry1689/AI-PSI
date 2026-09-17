"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# 🔴 Necessary. Alembic autogenerate renders PostgreSQL-specific types as
# `postgresql.JSONB(...)` / `postgresql.ARRAY(...)` but does NOT emit the
# import for them. Without this line the migration raises NameError at
# `alembic upgrade head` time — i.e. only when someone actually runs it.
from sqlalchemy.dialects import postgresql

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
