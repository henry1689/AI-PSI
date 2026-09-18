"""不变量 11 的**数据库级**强制，以及应用枚举与迁移的一致性（阶段 6.5 §四.6–7）。

🔴 **这个文件补的是一个"文档说了但没人验过"的洞。**

`README.md`、ADR-0018 与 ADR-0019 三处都写着
"实测 ``INSERT ... status='active'`` 被 PostgreSQL 拒绝
（``ck_improvement_proposals_status_valid``）"——而阶段 6.5 的
能力证据矩阵发现：**全仓库没有任何测试触发过那个约束**。
那是一次一次性手工验证，没有固化成测试。

一条只被手工验证过一次的性质，在下一次改迁移时不会有人注意到它失效。
本文件把它钉住，并且多做一件事：**核对迁移里的允许值与
``ProposalStatus`` 是否一致**。

## 为什么"改枚举"不会自动改数据库

数据库的 CHECK 来自**迁移文件**（`migrations/versions/*.py`），
不是从 Python 枚举生成的。往 `ProposalStatus` 里加一个成员：
类型层立刻接受它，而数据库仍然只允许原来的五个值——
于是新状态在内存后端跑得好好的，在真实数据库上抛
``CheckViolation``。那是"两个后端两个结果"的又一个实例。

因此这里做**双向**核对：

* 枚举里每一个值，数据库都必须接受；
* 数据库接受的每一个值，都必须能在枚举里找到。

只做一个方向会漏掉另一半：漏掉前者会让"新状态在 PG 上写不进去"，
漏掉后者会让"数据库还允许着一个已经删掉的状态"。
"""

from __future__ import annotations

import re
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ai_psi.domain.enums import ApprovalLevel, ErrorType, ProposalStatus

pytestmark = pytest.mark.integration

#: 状态约束的名字。写成一个常量，让断言失败时能指出**是哪一个**约束。
_STATUS_CONSTRAINT = "ck_improvement_proposals_status_valid"

#: 应当被拒绝的状态值。
#:
#: 含大写变体与近义词：CHECK 是**字面量比较**，`'ACTIVE'` 与 `'active'`
#: 都是"不在允许集合里"，但客户端 bug 常常只改大小写。
FORBIDDEN_STATUSES = [
    "active",
    "ACTIVE",
    "enabled",
    "ENABLED",
    "live",
    "deployed",
    "published",
    "applied",
    "promoted",
    "go_live",
]


async def _insert(conn: AsyncConnection, *, status: str, proposal_id: object = None) -> None:
    """直接 INSERT 一行提案，只关心 ``status`` 这一列。

    ⚠️ 走**原生 SQL**而不是仓储：本文件要验证的是**数据库自己的**约束，
    经仓储会被应用层的成员校验先拦下——那样即使 CHECK 被删掉，
    测试照样全绿。
    """
    await conn.execute(
        text(
            """
            INSERT INTO improvement_proposals (
                id, created_at, updated_at, version, created_by, schema_version,
                target_component, observed_problem, error_class,
                supporting_experience_ids, counterexamples, proposed_change,
                expected_benefit, possible_regressions, applicability,
                evaluation_plan, success_metrics, rollback_conditions,
                approval_level, status
            ) VALUES (
                :id, now(), now(), 1, 'test', '1.0.0',
                'prompt:logical_analyzer', '问题', :error_class,
                '{}', '{}', '改动', '收益', '{}', '{}',
                '{}', '{}', '{}', :approval_level, :status
            )
            """
        ),
        {
            "id": proposal_id or uuid4(),
            "error_class": ErrorType.REASONING_ERROR.value,
            "approval_level": ApprovalLevel.USER_AND_REVIEW.value,
            "status": status,
        },
    )


class TestForbiddenStatusesAreRejectedByTheDatabase:
    """🔴 不变量 11 的最后一层：**写入绕过了全部应用层代码，数据库仍然拒绝。**"""

    @pytest.mark.parametrize("status", FORBIDDEN_STATUSES)
    async def test_the_database_refuses_it(
        self, engine: AsyncEngine, clean_tables: None, status: str
    ) -> None:
        del clean_tables
        with pytest.raises(IntegrityError) as excinfo:
            async with engine.begin() as conn:
                await _insert(conn, status=status)
        # 🔴 断言**是哪一个**约束拒绝的。只断言"抛了 IntegrityError"
        # 会让"constraint 被删掉、但另一列恰好也非法"这种情形蒙混过关。
        assert _STATUS_CONSTRAINT in str(excinfo.value)

    async def test_the_constraint_still_exists(
        self, engine: AsyncEngine, clean_tables: None
    ) -> None:
        """约束本身在不在——上一条在它被删除时**不会**失败（会变成无异常）。

        它确实会失败（`pytest.raises` 要求抛错），但失败信息说的是
        "没有抛异常"，而排查的人需要知道的是"约束不见了"。这条用例
        把那个诊断直接给出来。
        """
        del clean_tables
        async with engine.begin() as conn:
            found = await conn.scalar(
                text("SELECT count(*) FROM pg_constraint WHERE conname = :name"),
                {"name": _STATUS_CONSTRAINT},
            )
        assert found == 1, f"约束 {_STATUS_CONSTRAINT} 在数据库里不存在"


class TestTheConstraintMatchesTheApplicationEnum:
    """🔴 §四.7：应用枚举与真实 PostgreSQL CHECK 的一致性。

    ⚠️ **数据库约束来自迁移，不会随 Python 枚举自动同步**（§四.8）。
    这两条用例是那句文档的话在代码里的对应物。
    """

    @pytest.mark.parametrize("status", [item.value for item in ProposalStatus])
    async def test_every_enum_member_is_accepted_by_the_database(
        self, engine: AsyncEngine, clean_tables: None, status: str
    ) -> None:
        """枚举里的每一个值，数据库都必须接受。

        反向的失败（枚举加了、迁移没加）在这里表现为
        ``CheckViolation``——而那正是真实运行时会遇到的情形。
        """
        del clean_tables
        async with engine.begin() as conn:
            await _insert(conn, status=status)

    async def test_the_constraint_allows_exactly_the_enum(
        self, engine: AsyncEngine, clean_tables: None
    ) -> None:
        """解析约束定义，逐字比对允许集合与 ``ProposalStatus``。

        上一条证明"枚举 ⊆ 数据库"，这一条证明"数据库 ⊆ 枚举"。
        缺了后者，一个**已经删掉**的状态会继续被数据库允许——
        而它在枚举里找不到，于是任何按枚举过滤的代码都会漏掉它。
        """
        del clean_tables
        async with engine.begin() as conn:
            definition: str = await conn.scalar(
                text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name"),
                {"name": _STATUS_CONSTRAINT},
            )

        allowed = set(re.findall(r"'([^']+)'", definition))
        assert allowed == {item.value for item in ProposalStatus}


class TestTheMigratedColumnsMatchTheirEnums:
    """同一类一致性，扩展到提案表上其余的枚举列。

    ⚠️ 只覆盖**有 Python 枚举与之对应**的列。`approval_level` 与
    `error_class` 满足这个条件；`observed_problem` 一类的自由文本不适用。
    """

    @pytest.mark.parametrize(
        ("constraint", "expected"),
        [
            (
                "ck_improvement_proposals_approval_level_valid",
                {item.value for item in ApprovalLevel},
            ),
            (
                "ck_improvement_proposals_error_class_valid",
                {item.value for item in ErrorType},
            ),
        ],
    )
    async def test_the_allowed_values_match(
        self,
        engine: AsyncEngine,
        clean_tables: None,
        constraint: str,
        expected: set[str],
    ) -> None:
        del clean_tables
        async with engine.begin() as conn:
            definition: str = await conn.scalar(
                text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :name"),
                {"name": constraint},
            )
        assert set(re.findall(r"'([^']+)'", definition)) == expected
