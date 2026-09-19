"""R72 的迁移验收：建索引、诊断重复、可回退（阶段 7 第一项）。

四条必须验的路径（用户第 9 条要求的逐条落实）：

1. **空库升级** —— 索引建出来，且形状与设计逐字一致；
2. **已有库升级（无重复）** —— 照常成功；
3. **已有库升级（有重复）** —— **报错并列出冲突**，索引**不**建立；
4. **downgrade** —— 索引消失；再 upgrade 能回来（可重跑）。

## 为什么这几条必须用**独立临时库**

第 3、4 条要把库带回"旧版本"或"缺索引"的状态。在共享的测试库上做，
`tests/integration/conftest.py` 的 session 级 `_test_database` 夹具
会在**下一次 pytest 启动**时把它 `upgrade head` 回来——
于是"缺索引"这个前提被悄悄撤销，验证变成空跑
（实测过：这正是 R72 反向验证不能用 downgrade 的原因，见脚本注释）。

因此这里从测试库名派生一个 `ai_psi_migration_test`，
每条用例**重建**它，与其它用例零共享。

⚠️ 这些用例是**同步**的：alembic 内部用 `asyncio.Runner` 自建事件循环，
在已运行的事件循环里调用会直接报错（同 conftest 的说明）。
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from ai_psi.config import Settings
from ai_psi.infrastructure.db.models import ACTIVE_PATTERN_INDEX_NAME

pytestmark = [pytest.mark.integration]

#: 新索引的前一个版本。升级到它 = "R72 之前的世界"。
_PREVIOUS_REVISION = "19c6e2485c2d"

#: 建索引的那条迁移 + 它的部分谓词。
_INDEX_PREDICATE = "cardinality(applicability) > 0"


def _to_sync_dsn(url: str) -> str:
    """把 SQLAlchemy 异步连接串转成 psycopg 同步 DSN。"""
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@pytest.fixture(scope="session")
def migration_database_url(test_settings: Settings) -> str:
    """从测试库名派生一个**迁移专用**库的连接串。"""
    base, separator, name = test_settings.database_url.rpartition("/")
    assert separator, test_settings.database_url
    return f"{base}/{name}_migration_test"


@pytest.fixture
def scratch(migration_database_url: str) -> Iterator[str]:
    """每条用例一个**全新**的库：先删后建，用完再删。

    删库比清表彻底：迁移用例要验的恰恰是"表还不存在/结构是旧的"
    这类状态，清表清不掉它。

    ⚠️ **产出的是异步连接串**（``postgresql+psycopg://``）——
    与 `tests/integration/conftest.py::_run_migrations` 传给 alembic 的
    是同一种形状。``migrations/env.py`` 按它建引擎；递一条同步串进去，
    SQLAlchemy 会退回 psycopg2 方言，而本仓库只装了 psycopg3
    （症状是 `ModuleNotFoundError: No module named 'psycopg2'`）。
    本模块自己的 psycopg 连接在用到时各自转换。
    """
    sync = _to_sync_dsn(migration_database_url)
    base, _, name = sync.rpartition("/")
    admin = f"{base}/postgres"

    def _recreate() -> None:
        with psycopg.connect(admin, autocommit=True) as conn:
            # 断开可能残留的连接，否则 DROP DATABASE 会被拒。
            conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
            conn.execute(f'CREATE DATABASE "{name}"')
        with psycopg.connect(sync, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")

    _recreate()
    yield migration_database_url
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def _alembic(url: str) -> Config:
    """指向 ``url`` 的 alembic 配置。

    🔴 **必须显式注入连接串。** `alembic.ini` 刻意没有 `sqlalchemy.url`，
    而 `migrations/env.py` 拿不到 `override_database_url` 时会
    **回退到开发库**——裸跑 `alembic upgrade` 会打到 `ai_psi` 上。
    """
    config = Config("alembic.ini")
    config.attributes["override_database_url"] = url
    return config


def _indexdef(url: str) -> str | None:
    """索引定义；不存在返回 ``None``（查 ``pg_indexes``，不抛异常）。"""
    with psycopg.connect(_to_sync_dsn(url)) as conn:
        row = conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename = 'improvement_proposals' AND indexname = %s",
            (ACTIVE_PATTERN_INDEX_NAME,),
        ).fetchone()
    return None if row is None else str(row[0])


def _insert_proposal(url: str, *, signature: str, status: str = "draft") -> str:
    """直接插一条提案行，绕过整个应用层。返回它的 id。"""
    proposal_id = str(uuid4())
    with psycopg.connect(_to_sync_dsn(url)) as conn:
        conn.execute(
            """
            INSERT INTO improvement_proposals (
                id, created_at, updated_at, version, created_by, schema_version,
                target_component, observed_problem, error_class,
                supporting_experience_ids, counterexamples, proposed_change,
                expected_benefit, possible_regressions, applicability,
                evaluation_plan, success_metrics, rollback_conditions,
                approval_level, status
            ) VALUES (
                %s, now(), now(), 1, 'test', '1',
                'prompt:logical_analyzer', '迁移验收', 'reasoning_error',
                ARRAY[]::uuid[], ARRAY[]::text[], '改动', '收益',
                ARRAY[]::text[], ARRAY[%s], ARRAY[]::text[],
                ARRAY[]::text[], ARRAY[]::text[], 'user_and_review', %s
            )
            """,
            (proposal_id, signature, status),
        )
        conn.commit()
    return proposal_id


class TestUpgradingAFreshDatabase:
    """空库升级：索引必须建出来，且形状与设计一致。"""

    def test_the_index_exists_and_is_partial(self, scratch: str) -> None:
        command.upgrade(_alembic(scratch), "head")

        definition = _indexdef(scratch)
        assert definition is not None, "迁移跑完了却没有索引"
        # 逐项核对：表达式、下标、以及两半谓词。
        # 少任何一项，并发防线都会在某个状态下静默失效。
        assert "(applicability[1])" in definition, definition
        assert "error_class" in definition, definition
        assert _INDEX_PREDICATE in definition, definition
        assert "rejected" in definition, definition
        assert "approved_for_manual_trial" in definition, definition

    def test_the_index_name_matches_the_application_constant(self, scratch: str) -> None:
        """🔴 迁移里的名字与应用常量必须一致。

        迁移刻意**不 import 应用代码**（它是历史快照）。代价是那个名字
        出现了两处，而分家的后果很具体：新库上建的是 A 名字，
        ``SqlAlchemyProposalRepository`` 却按 B 名字分流——于是
        **业务键冲突会被当成主键冲突**，并发下照样写出两条提案，
        而应用层还以为是 uuid4 撞了。这条用例就是钉住它的。
        """
        command.upgrade(_alembic(scratch), "head")
        assert _indexdef(scratch) is not None


class TestUpgradingADatabaseWithData:
    """已有库升级。"""

    def test_existing_rows_without_duplicates_upgrade_cleanly(self, scratch: str) -> None:
        command.upgrade(_alembic(scratch), _PREVIOUS_REVISION)
        _insert_proposal(scratch, signature="d1|with_evidence|h2")
        _insert_proposal(scratch, signature="d2|no_evidence|h1")

        command.upgrade(_alembic(scratch), "head")

        assert _indexdef(scratch) is not None

    def test_terminal_rows_do_not_block_the_upgrade(self, scratch: str) -> None:
        """两条**同键但已终态**的提案不该拦住迁移。"""
        command.upgrade(_alembic(scratch), _PREVIOUS_REVISION)
        _insert_proposal(scratch, signature="d2|no_evidence|h2", status="rejected")
        _insert_proposal(scratch, signature="d2|no_evidence|h2", status="rejected")

        command.upgrade(_alembic(scratch), "head")

        assert _indexdef(scratch) is not None

    def test_duplicate_active_rows_stop_the_upgrade_with_a_readable_report(
        self, scratch: str
    ) -> None:
        """🔴 **有重复就报错，且报文必须点得出是哪几条。**

        迁移**不自动清理**：提案是审计记录，"这一条曾经存在过"本身是信息，
        删掉哪一条是人的决定。但"失败"必须**可诊断**——否则运维看到的
        只有 PostgreSQL 的一句 `could not create unique index`，
        既不知道冲突键、也不知道涉及哪些提案。
        """
        command.upgrade(_alembic(scratch), _PREVIOUS_REVISION)
        first = _insert_proposal(scratch, signature="d2|no_evidence|h2")
        second = _insert_proposal(scratch, signature="d2|no_evidence|h2")

        with pytest.raises(RuntimeError) as caught:
            command.upgrade(_alembic(scratch), "head")

        message = str(caught.value)
        assert "d2|no_evidence|h2" in message, message
        assert first in message, message
        assert second in message, message
        assert "不会自动清理" in message, message
        # 🔴 **索引不得建立**：建了就等于在"已确认有重复"的库上
        # 留下一个假的唯一性承诺。
        assert _indexdef(scratch) is None

    def test_the_upgrade_succeeds_once_the_duplicates_are_resolved(self, scratch: str) -> None:
        """人工处置之后重跑必须能过——否则迁移就是一条死路。"""
        command.upgrade(_alembic(scratch), _PREVIOUS_REVISION)
        _insert_proposal(scratch, signature="d2|no_evidence|h2")
        kept = _insert_proposal(scratch, signature="d2|no_evidence|h2")
        with pytest.raises(RuntimeError):
            command.upgrade(_alembic(scratch), "head")

        # 人工处置：把多余的那条推进终态（**不是删除**——审计要留）
        with psycopg.connect(_to_sync_dsn(scratch)) as conn:
            conn.execute(
                "UPDATE improvement_proposals SET status = 'rejected' "
                "WHERE status = 'draft' AND id <> %s",
                (kept,),
            )
            conn.commit()

        command.upgrade(_alembic(scratch), "head")
        assert _indexdef(scratch) is not None


class TestDowngrading:
    """downgrade 必须干净，且可重跑。"""

    def test_downgrade_drops_the_index_and_upgrade_restores_it(self, scratch: str) -> None:
        command.upgrade(_alembic(scratch), "head")
        assert _indexdef(scratch) is not None

        command.downgrade(_alembic(scratch), _PREVIOUS_REVISION)
        # 🔴 降级 = **重新打开 R72**。这不是一次无副作用的回退，
        # 因此这里断言的正是"保证确实没了"——而不是"文件改回去了"。
        assert _indexdef(scratch) is None

        command.upgrade(_alembic(scratch), "head")
        assert _indexdef(scratch) is not None
