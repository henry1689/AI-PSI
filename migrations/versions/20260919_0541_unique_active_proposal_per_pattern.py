"""unique active proposal per pattern

Revision ID: b7f1c9d4e2a3
Revises: 19c6e2485c2d
Create Date: 2026-09-19 05:41:11.432580+00:00

🔴 **R72 的并发防线**（阶段 7 第一项）。

背景：``LearningService._covered_keys()`` 是"先读已存在的提案、再生成"，
读与写之间没有锁、表上也没有对应的唯一约束。两个并发的学习运行各自
读到"还没有提案"的旧快照，各走完门禁与生成，然后在**写入**时才分胜负
——结果是两条内容完全相同的 DRAFT（阶段 6.6 的独立评审实测 3/3 复现）。
重复的提案**删不掉**（只有 evaluate→reject 一条路），会长期占着评审队列。

## 这个索引在表达什么

> **同一个业务模式 ``(error_class, applicability[0])``，
> 至多存在一条"活跃"提案。**

三个设计点，每一个都不是随手写的：

1. **索引表达式是 ``(applicability[1])`` 而不是整个数组。**
   应用层 ``_covered_keys()`` 取的是 ``applicability[0]``。整数组唯一会把
   ``['a','b']`` 与 ``['a']`` 判成两个键，而应用层认为它们是同一个
   ——索引比应用规则更松。**PostgreSQL 的数组下标从 1 起**，
   所以 ``applicability[1]`` 恰好就是 Python 里的 ``applicability[0]``。

2. **部分谓词排除空 applicability。**
   空数组下标越界得到 NULL，而 PostgreSQL 的唯一索引**允许多个 NULL**。
   不排除它们，就会留下一个"看起来唯一、实际对这类行毫无约束"的约束。
   排除之后索引里根本不存在 NULL，而不是靠"NULL 互不相等"侥幸不冲突。
   方向与应用层一致：``_covered_keys()`` 同样跳过空 applicability
   （那类提案不覆盖任何模式）。

3. **部分谓词排除终态。**
   数据库裁决的是"至多一条**活跃**提案"。"终态也不再提议"（R55）是
   **策略**不是不变量，仍由应用层承担——把它写进索引会让阶段 7 之后
   任何"驳回 N 天后可重开"的设计都必须先换一次索引。

## ⚠️ 建立索引前先诊断重复数据

本次迁移**不会自动清理**任何东西。若库里已经存在同键的多条活跃提案
（正是 R72 那个竞态留下的产物），迁移**报错并列出冲突**，由人决定
保留哪一条。

不自动删的理由：提案是**给人评审的审计对象**，
"这一条曾经存在过"本身是信息。自动删掉一条，等于让一次真实的
系统缺陷从记录里消失。治理这条数据的决定权在用户，不在迁移脚本。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7f1c9d4e2a3"
down_revision: str | None = "19c6e2485c2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 索引名。与 ``ai_psi.infrastructure.db.models.ACTIVE_PATTERN_INDEX_NAME``
#: 及 ``SqlAlchemyProposalRepository`` 里分流用的那个名字**必须一致**。
#:
#: ⚠️ 这里刻意**不 import 应用代码**：迁移是历史快照，它描述的是
#: "这一刻之后数据库长什么样"。若它跟着应用常量走，某天那个常量改了名，
#: 这条早就跑过的迁移会在**新库**上建出另一个名字的索引，
#: 而老库上是旧名字——两个库的唯一性防线靠不同名字维持。
#: 一致性由 ``tests/integration/test_migration_pattern_uniqueness.py`` 钉住。
_INDEX_NAME = "uq_improvement_proposals_active_pattern"

#: 部分谓词，见模块文档第 2、3 点。
_INDEX_WHERE = (
    "cardinality(applicability) > 0 AND status NOT IN ('rejected', 'approved_for_manual_trial')"
)

#: 诊断查询：列出每一个"同键多条活跃提案"的业务键及其成员。
_DIAGNOSE_DUPLICATES = sa.text(
    """
    SELECT error_class,
           applicability[1] AS signature,
           array_agg(id ORDER BY created_at, id) AS proposal_ids,
           count(*) AS n
      FROM improvement_proposals
     WHERE cardinality(applicability) > 0
       AND status NOT IN ('rejected', 'approved_for_manual_trial')
     GROUP BY error_class, applicability[1]
    HAVING count(*) > 1
     ORDER BY error_class, signature
    """
)


def _assert_no_duplicate_active_patterns(connection: sa.Connection) -> None:
    """发现重复就报错并停下——**不自动清理**（理由见模块文档）。

    Args:
        connection: 迁移使用的连接。

    Raises:
        RuntimeError: 存在同键的多条活跃提案。报文列出每个冲突键与
            它的成员提案 id，便于人工处置。
    """
    rows = connection.execute(_DIAGNOSE_DUPLICATES).all()
    if not rows:
        return

    lines = [
        "存在同键的多条活跃提案，唯一索引未能建立（阶段 7 · R72）。",
        f"共 {len(rows)} 组冲突：",
    ]
    for error_class, signature, proposal_ids, count in rows:
        lines.append(f"  - ({error_class}, {signature}) × {count}")
        lines.extend(f"      - {item}" for item in proposal_ids)
    lines.extend(
        (
            "",
            "迁移**不会自动清理**：提案是审计记录，删掉哪一条是人的决定。",
            "请先人工处置（通常保留最早的一条，把其余推进终态而不是删除），",
            "再重新执行 `alembic upgrade head`。",
        )
    )
    raise RuntimeError("\n".join(lines))


def upgrade() -> None:
    """先诊断重复，再建索引。"""
    _assert_no_duplicate_active_patterns(op.get_bind())
    # 🔴 **这里用原生 SQL，不用 `op.create_index`。**
    # 后者会先把传入的"列"解析成表上的真实列名，而
    # `(applicability[1])` 是一个**表达式**——解析必然失败
    # （实测：`ConstraintColumnNotFoundError: no column named
    # '(applicability[1])' is present`）。`sa.text()` 也救不了，
    # 解析发生在它之前。
    op.execute(
        f"CREATE UNIQUE INDEX {_INDEX_NAME} "
        f"ON improvement_proposals (error_class, (applicability[1])) "
        f"WHERE {_INDEX_WHERE}"
    )


def downgrade() -> None:
    """删掉索引。

    🔴 **降级会重新打开 R72。** 删掉它之后，并发的两个学习运行又能
    各自写出内容相同的 DRAFT，而这是**静默**的——没有任何测试会在
    降级后的库里自动变红，除非有人主动去跑
    ``tests/integration/test_proposal_pattern_concurrency.py``。

    因此：降级只应当在"确认要放弃这条保证"时执行，
    而不是当成一次无副作用的回退。
    """
    op.execute(f"DROP INDEX {_INDEX_NAME}")
