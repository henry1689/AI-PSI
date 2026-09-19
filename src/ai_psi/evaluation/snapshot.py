"""参考库快照：证明"评测没有改动既有状态"（阶段 7 · S2）。

## 它要回答的问题

评测跑完之后，参考库**逐字节**还是原来那样吗？行数一样不够——一条被
``UPDATE`` 覆盖掉的记录，行数完全看不出来。因此快照同时采集三样东西：

* **行数** —— 发现 INSERT / DELETE；
* **主键集合** —— 发现"删一条、加一条"，行数不变的那种；
* **稳定内容哈希** —— 发现 UPDATE。

三者合起来才能覆盖三种写入。

## 排序与稳定序列化

数据库不承诺 ``SELECT`` 的返回顺序（§十八 D：不依赖数据库无序返回）。
因此每张表都带一个**显式排序键**，行按它升序读出后再拼哈希。

值一律转成稳定文本：``uuid`` 转小写连字符形式、``datetime`` 转 ISO-8601、
``JSONB`` 转 ``sort_keys=True`` 的紧凑 JSON、``float`` 用 ``repr``
（避免 ``str`` 在某些平台上退化精度）。

## 时间戳纳不纳入

**纳入。** ``created_at`` / ``updated_at`` 是业务字段：一条被改过的回合，
其 ``updated_at`` 必然变化，而那正是要发现的。把它们排除在外，
等于主动放弃一个"这行被写过了"的信号。

## 这不是安全设施

哈希用 SHA-256，但它**不是**抗碰撞的安全用途，只是"内容有没有变"的证据。
刻意不做加盐，也不做 HMAC——加盐会让两次快照无法直接比较。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = [
    "ReferenceSnapshot",
    "TableSnapshot",
    "take_reference_snapshot",
]

#: 快照覆盖的表及其**显式排序键**。
#:
#: 🔴 表名与排序键都是本模块里的常量，不来自任何外部输入——
#: 它们会被直接插进 SQL，因此不能是变量。
#:
#: * ``events`` 用 ``sequence``：全局单调递增序，是回放与顺序哈希的
#:   权威依据（``recorded_at`` 的精度不足以区分同一微秒内的多次写入）；
#: * 其余表用主键。
_TABLE_SPECS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("events", ("sequence",)),
    ("cognitive_rounds", ("id",)),
    ("idempotency_keys", ("key",)),
    ("memories", ("id",)),
    ("memory_embeddings", ("memory_id",)),
    ("improvement_proposals", ("id",)),
)

#: 学习状态在**事件流**里的前缀。
#:
#: 🔴 经验、评价、归因都没有独立表（见 ``infrastructure/db/models.py``）——
#: 它们以事件的形式存在于 ``events``。因此"学习状态有没有变"这个问题
#: 必须按事件类型去问，不能去找一张不存在的表。
_LEARNING_EVENT_PREFIX: Final[str] = "experience."

#: 改进提案在事件流里的前缀（提案本身另有 ``improvement_proposals`` 表）。
_PROPOSAL_EVENT_PREFIX: Final[str] = "improvement_proposal."

#: ``None`` 的占位符。用一个不可能出现在业务数据里的符号，
#: 免得把"空值"与"字符串 None"混成同一个哈希。
_NULL: Final[str] = "∅"

#: 行内字段分隔符与行间分隔符。
_FIELD_SEP: Final[str] = "|"
_ROW_SEP: Final[str] = "\n"


def _stable_value(value: Any) -> str:
    """把一个数据库值转成**稳定**文本。

    🔴 稳定性是这一层唯一的硬要求：同样的数据在同一台机器上跑两次，
    必须得到同一个字符串。因此不使用 ``str()`` 兜底（它对
    ``Decimal`` / 自定义类型的行为会随实现变化），而是逐类显式处理。

    Args:
        value: 数据库读出的值。

    Returns:
        稳定文本。
    """
    if value is None:
        return _NULL
    if isinstance(value, bool):
        # ⚠️ 必须在 int 之前判断——Python 里 bool 是 int 的子类，
        # 落到下面会变成 "1"/"0"，与整数无法区分。
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # repr 保证往返精度；str 在某些实现上会截断。
        return repr(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, (dict, list, tuple)):
        # JSONB / ARRAY：键序不承诺稳定，因此显式排序且不留空格。
        #
        # 🔴 ``default`` 不是可选的：``evidence_refs`` 是 ``ARRAY(PGUUID)``，
        # 读出来是 ``list[UUID]``；JSONB 里也可能嵌着别的东西。
        # 少了它，快照一读 ``events`` 表就会抛
        # ``TypeError: Object of type UUID is not JSON serializable``。
        return json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=_json_fallback,
        )
    # 兜底（如 Decimal）：repr 比 str 更接近"这台机器上实际是什么"。
    return repr(value)


def _json_fallback(value: object) -> str:
    """``json.dumps`` 遇到不认识的类型时调用。

    🔴 **与 :func:`_stable_value` 的顶层分支用同一套表示。**
    同一个值，无论出现在顶层还是嵌在 JSONB / 数组里，都必须得到同一个
    字符串——否则"数据没变、哈希变了"会以最难查的形式出现。
    """
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return repr(value)


def _digest(lines: list[str]) -> str:
    """对一组已经**排好序**的文本行取 SHA-256。"""
    return hashlib.sha256(_ROW_SEP.join(lines).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TableSnapshot:
    """一张表在某个时刻的样子。"""

    table: str
    row_count: int
    #: 主键集合的哈希——发现"删一条、加一条"这种行数不变的情形。
    primary_key_digest: str
    #: 全字段内容哈希（按排序键升序拼接）——发现 UPDATE。
    content_digest: str

    def summary(self) -> dict[str, Any]:
        """可写进报告的形态。"""
        return {
            "table": self.table,
            "row_count": self.row_count,
            "primary_key_digest": self.primary_key_digest,
            "content_digest": self.content_digest,
        }


@dataclass(frozen=True, slots=True)
class ReferenceSnapshot:
    """参考库在某个时刻的完整可比较状态。"""

    tables: tuple[TableSnapshot, ...]
    #: 原始事件流的**顺序**哈希：按 ``sequence`` 升序，串起 ``(sequence, 事件 id)``。
    event_order_digest: str
    #: 学习状态（经验、评价、归因）的内容哈希。
    learning_digest: str
    #: 改进提案事件的内容哈希。
    proposal_event_digest: str

    def table(self, name: str) -> TableSnapshot:
        """按表名取一份表快照。

        Raises:
            KeyError: 该表不在快照里。**刻意不返回空值**——"没快照到"
                与"快照到 0 行"是完全不同的两件事。
        """
        for item in self.tables:
            if item.table == name:
                return item
        msg = f"快照里没有表 {name!r}"
        raise KeyError(msg)

    @property
    def total_rows(self) -> int:
        """全部表的行数之和。"""
        return sum(item.row_count for item in self.tables)

    def diff(self, other: ReferenceSnapshot) -> tuple[str, ...]:
        """逐项比较，返回差异描述；**完全一致时返回空元组**。

        Args:
            other: 评测之后重新采集的快照。

        Returns:
            人类可读的差异列表。空表示未检测到任何变化。
        """
        differences: list[str] = []
        mine = {item.table: item for item in self.tables}
        theirs = {item.table: item for item in other.tables}

        for name in sorted(set(mine) | set(theirs)):
            before = mine.get(name)
            after = theirs.get(name)
            if before is None or after is None:
                differences.append(f"{name}: 只在其中一次快照里出现")
                continue
            if before.row_count != after.row_count:
                differences.append(
                    f"{name}: 行数 {before.row_count} -> {after.row_count}（有 INSERT 或 DELETE）"
                )
            if before.primary_key_digest != after.primary_key_digest:
                differences.append(f"{name}: 主键集合变化（有行被删除或新增）")
            if before.content_digest != after.content_digest:
                differences.append(f"{name}: 内容哈希变化（有行被 UPDATE）")

        if self.event_order_digest != other.event_order_digest:
            differences.append("events: 原始事件流的顺序哈希变化")
        if self.learning_digest != other.learning_digest:
            differences.append("events: 学习状态（经验 / 评价 / 归因）变化")
        if self.proposal_event_digest != other.proposal_event_digest:
            differences.append("events: 改进提案事件变化")
        return tuple(differences)

    def summary(self) -> dict[str, Any]:
        """可写进报告的最小证据。

        🔴 只有布尔结论与计数，**没有内容**——报告不该带上参考库的数据。
        """
        return {
            "tables": [item.summary() for item in self.tables],
            "total_rows": self.total_rows,
            "event_order_digest": self.event_order_digest,
            "learning_digest": self.learning_digest,
        }


async def _snapshot_table(
    engine: AsyncEngine, table: str, order_by: tuple[str, ...]
) -> TableSnapshot:
    """读一张表，产出它的行数、主键哈希与内容哈希。

    Args:
        engine: 目标库引擎。
        table: 表名（本模块常量）。
        order_by: 排序键（本模块常量）。

    Returns:
        该表的快照。
    """
    order_clause = ", ".join(order_by)
    # 🔴 表名与排序列都来自本模块的常量元组，不含外部输入。
    statement = text(f"SELECT * FROM {table} ORDER BY {order_clause}")

    async with engine.connect() as connection:
        result = await connection.execute(statement)
        columns = list(result.keys())
        rows = result.fetchall()

    key_positions = [columns.index(name) for name in order_by]

    content_lines: list[str] = []
    key_lines: list[str] = []
    for row in rows:
        # 列顺序由驱动决定，因此按**列名**排序后再拼——否则同一次查询
        # 在不同驱动/版本下会得到不同的哈希。
        pairs = sorted(zip(columns, row, strict=True), key=lambda item: item[0])
        content_lines.append(
            _FIELD_SEP.join(f"{name}={_stable_value(value)}" for name, value in pairs)
        )
        key_lines.append(_FIELD_SEP.join(str(row[position]) for position in key_positions))

    return TableSnapshot(
        table=table,
        row_count=len(rows),
        primary_key_digest=_digest(key_lines),
        content_digest=_digest(content_lines),
    )


async def _event_digests(engine: AsyncEngine) -> tuple[str, str, str]:
    """从 ``events`` 里取三份**顺序 / 学习状态 / 提案**摘要。

    Returns:
        ``(顺序哈希, 学习状态哈希, 提案事件哈希)``。
    """
    async with engine.connect() as connection:
        result = await connection.execute(
            text("SELECT sequence, id, event_type, payload FROM events ORDER BY sequence")
        )
        rows = result.fetchall()

    order_lines: list[str] = []
    learning_lines: list[str] = []
    proposal_lines: list[str] = []

    for sequence, event_id, event_type, payload in rows:
        order_lines.append(f"{sequence}{_FIELD_SEP}{event_id}")
        rendered = f"{event_id}{_FIELD_SEP}{_stable_value(payload)}"
        if event_type.startswith(_LEARNING_EVENT_PREFIX):
            learning_lines.append(rendered)
        elif event_type.startswith(_PROPOSAL_EVENT_PREFIX):
            proposal_lines.append(rendered)

    return _digest(order_lines), _digest(learning_lines), _digest(proposal_lines)


async def take_reference_snapshot(engine: AsyncEngine) -> ReferenceSnapshot:
    """读一遍参考库，产出一份可比较的快照。

    🔴 **全程只读。** 用的是 ``engine.connect()`` 而不是 ``begin()``：
    快照本身绝不能成为"参考库被写过"的原因。

    Args:
        engine: 参考库引擎。

    Returns:
        快照。
    """
    tables = tuple(
        [await _snapshot_table(engine, table, order_by) for table, order_by in _TABLE_SPECS]
    )
    order_digest, learning_digest, proposal_digest = await _event_digests(engine)
    return ReferenceSnapshot(
        tables=tables,
        event_order_digest=order_digest,
        learning_digest=learning_digest,
        proposal_event_digest=proposal_digest,
    )
