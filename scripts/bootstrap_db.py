#!/usr/bin/env python
"""校验开发数据库连通性，并确保 pgvector 扩展就绪。

用法::

    uv run python scripts/bootstrap_db.py

退出码：
    0  连通且 ``vector`` 扩展可用
    1  连接失败或扩展不可用（错误信息输出到 stderr）

阶段 1 只做连通性与扩展校验；建表与迁移（Alembic）在阶段 2 引入（ADR-0012）。
"""

from __future__ import annotations

import sys

import psycopg

from ai_psi.config import get_settings


def main() -> int:
    settings = get_settings()

    try:
        with psycopg.connect(settings.psycopg_dsn, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version()")
                row = cur.fetchone()
                server_version = row[0] if row else "<unknown>"

                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
                ext_row = cur.fetchone()
                vector_version = ext_row[0] if ext_row else None

            conn.commit()
    except psycopg.Error as exc:
        print(f"[FAIL] 数据库连接或初始化失败：{exc}", file=sys.stderr)
        print(
            "       请确认容器已启动： docker compose up -d",
            file=sys.stderr,
        )
        return 1

    print("[OK] 数据库连通")
    print(f"     server      : {server_version.split(',')[0]}")
    if vector_version is None:
        print("[FAIL] pgvector 扩展不可用", file=sys.stderr)
        return 1
    print(f"     pgvector    : {vector_version}")
    print(f"     environment : {settings.env.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
