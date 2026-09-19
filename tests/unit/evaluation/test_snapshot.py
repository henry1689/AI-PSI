"""D 组：参考库快照的稳定性与检测能力（阶段 7 · S2）。

这一组全部是纯单元测试：**不连数据库**。被验证的是快照的两条性质——

1. **稳定**：同样的数据一定得到同样的哈希（否则"前后一致"无从谈起）；
2. **敏感**：INSERT / UPDATE / DELETE 三种写入都能被发现
   （只比行数不足以证明内容没被覆盖）。

⚠️ "快照真的读到了库里的行"这件事必须由集成测试证明，
不能靠这里的桩数据——那会变成"用假的输入证明真的行为"。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from ai_psi.evaluation.snapshot import (
    ReferenceSnapshot,
    TableSnapshot,
    _stable_value,
)

pytestmark = pytest.mark.unit


def _table(
    name: str = "events",
    *,
    row_count: int = 3,
    primary_key_digest: str = "pk-a",
    content_digest: str = "content-a",
) -> TableSnapshot:
    """一张表的快照，字段可按需覆盖。"""
    return TableSnapshot(
        table=name,
        row_count=row_count,
        primary_key_digest=primary_key_digest,
        content_digest=content_digest,
    )


def _snapshot(
    table: TableSnapshot | None = None,
    *,
    event_order_digest: str = "order-a",
    learning_digest: str = "learning-a",
    proposal_event_digest: str = "proposal-a",
) -> ReferenceSnapshot:
    """一份快照，三项摘要可按需覆盖。"""
    return ReferenceSnapshot(
        tables=(table if table is not None else _table(),),
        event_order_digest=event_order_digest,
        learning_digest=learning_digest,
        proposal_event_digest=proposal_event_digest,
    )


class TestStableValue:
    """值的稳定序列化。

    🔴 这一组管的是"同样的数据两次运行得到同一个字符串"。
    任何一处不稳定，都会让"参考库前后一致"变成一句空话——
    因为不一致可能只是序列化的抖动，而真正的不一致会被淹没在里面。
    """

    def test_bool_is_never_confused_with_int(self) -> None:
        """Python 里 ``bool`` 是 ``int`` 的子类；不先判断就会都变成 "1"/"0"。"""
        assert _stable_value(True) == "true"
        assert _stable_value(False) == "false"
        assert _stable_value(True) != _stable_value(1)
        assert _stable_value(False) != _stable_value(0)

    def test_none_has_its_own_placeholder(self) -> None:
        """空值不能与字符串 "None" 撞成同一个哈希。"""
        assert _stable_value(None) != _stable_value("None")

    def test_json_is_key_sorted(self) -> None:
        """JSONB 不承诺键序；不排序就会得到"数据没变、哈希变了"。"""
        assert _stable_value({"b": 1, "a": 2}) == _stable_value({"a": 2, "b": 1})

    def test_nested_json_is_also_sorted(self) -> None:
        nested = {"outer": {"z": [{"b": 1, "a": 2}]}}
        reordered = {"outer": {"z": [{"a": 2, "b": 1}]}}
        assert _stable_value(nested) == _stable_value(reordered)

    def test_uuid_is_lowercase_hyphenated(self) -> None:
        raw = UUID("12345678-1234-5678-1234-567812345678")
        assert _stable_value(raw) == "12345678-1234-5678-1234-567812345678"

    def test_datetime_is_isoformat(self) -> None:
        moment = datetime(2026, 9, 20, 12, 30, 45, tzinfo=UTC)
        assert _stable_value(moment) == moment.isoformat()

    def test_float_keeps_full_precision(self) -> None:
        """向量列是 ``float``；用 ``str`` 在某些实现上会截断精度。"""
        assert _stable_value(0.1) == repr(0.1)

    def test_bytes_are_hex(self) -> None:
        assert _stable_value(b"\x00\xff") == "00ff"

    def test_strings_are_kept_as_is(self) -> None:
        assert _stable_value("原样") == "原样"

    def test_uuid_nested_in_a_list_is_serializable(self) -> None:
        """🔴 ``events.evidence_refs`` 是 ``ARRAY(PGUUID)``，读出来就是 ``list[UUID]``。

        少了 ``json.dumps`` 的 ``default`` 兜底，快照会在读**第一张表**时
        直接抛 ``TypeError``——而这条路径平时不走，只有真读库时才炸。
        """
        raw = UUID("12345678-1234-5678-1234-567812345678")
        assert _stable_value([raw]) == _stable_value([str(raw)])

    def test_datetime_nested_in_a_dict_is_serializable(self) -> None:
        """嵌套表示必须与顶层表示**用同一套写法**，否则哈希会无谓地抖动。"""
        moment = datetime(2026, 9, 20, tzinfo=UTC)
        assert _stable_value({"at": moment}) == _stable_value({"at": moment.isoformat()})

    def test_two_calls_agree(self) -> None:
        """同一次进程内、同一个值，两次调用必须完全相同。"""
        value = {"nested": [1, 2, {"b": 3, "a": 4}], "t": datetime(2026, 1, 1, tzinfo=UTC)}
        assert _stable_value(value) == _stable_value(value)


class TestTableSnapshotDiff:
    """单表快照的三种写入检测。"""

    def test_identical_tables_produce_no_difference(self) -> None:
        assert _snapshot().diff(_snapshot()) == ()

    def test_insert_is_detected(self) -> None:
        """INSERT：行数与主键集合都会变。"""
        after = _snapshot(_table(row_count=4, primary_key_digest="pk-b"))
        differences = _snapshot().diff(after)
        assert any("行数" in item for item in differences)
        assert any("主键集合" in item for item in differences)

    def test_delete_is_detected(self) -> None:
        """DELETE：同样是行数与主键集合。"""
        after = _snapshot(_table(row_count=2, primary_key_digest="pk-c"))
        differences = _snapshot().diff(after)
        assert any("行数" in item for item in differences)
        assert any("主键集合" in item for item in differences)

    def test_delete_plus_insert_with_a_stable_row_count_is_still_detected(self) -> None:
        """🔴 「删一条、加一条」行数完全不变——只比行数会漏掉它。"""
        after = _snapshot(_table(row_count=3, primary_key_digest="pk-d"))
        differences = _snapshot().diff(after)
        assert not any("行数" in item for item in differences)
        assert any("主键集合" in item for item in differences)

    def test_update_is_detected_by_the_content_digest(self) -> None:
        """🔴 UPDATE 行数与主键集合都不变——只有内容哈希能发现它。

        这就是"只比较行数不足以证明原记录未被覆盖"的具体样子。
        """
        after = _snapshot(_table(content_digest="content-b"))
        differences = _snapshot().diff(after)
        assert not any("行数" in item for item in differences)
        assert not any("主键集合" in item for item in differences)
        assert any("UPDATE" in item for item in differences)

    def test_content_change_alone_is_enough(self) -> None:
        """内容哈希变了就是变了——不因为"其它指标没动"而放过。"""
        assert _snapshot().diff(_snapshot(_table(content_digest="x"))) != ()

    def test_a_table_missing_from_one_side_is_reported(self) -> None:
        """一张表只在其中一次快照里出现，本身就是异常。"""
        empty = ReferenceSnapshot(
            tables=(),
            event_order_digest="o",
            learning_digest="l",
            proposal_event_digest="p",
        )
        assert any("只在其中一次" in item for item in _snapshot().diff(empty))

    def test_lookup_of_a_missing_table_raises(self) -> None:
        """🔴 "没快照到"与"快照到 0 行"是两件事，不能返回同一个值。"""
        with pytest.raises(KeyError):
            _snapshot().table("not_snapshotted")


class TestReferenceSnapshotDigests:
    """三项跨表摘要。"""

    def test_event_order_change_is_detected(self) -> None:
        """顺序哈希独立于内容哈希：同样的行换一个顺序也是变化。"""
        assert _snapshot().diff(_snapshot(event_order_digest="order-b")) != ()

    def test_learning_state_change_is_detected(self) -> None:
        """经验 / 评价 / 归因都活在事件流里——它们变了必须报出来。"""
        differences = _snapshot().diff(_snapshot(learning_digest="learning-b"))
        assert any("学习状态" in item for item in differences)

    def test_proposal_event_change_is_detected(self) -> None:
        differences = _snapshot().diff(_snapshot(proposal_event_digest="proposal-b"))
        assert any("改进提案" in item for item in differences)

    def test_total_rows_sums_every_table(self) -> None:
        snapshot = ReferenceSnapshot(
            tables=(_table("events", row_count=2), _table("memories", row_count=3)),
            event_order_digest="o",
            learning_digest="l",
            proposal_event_digest="p",
        )
        assert snapshot.total_rows == 5


class TestSummaryShape:
    """写进报告的那一份。"""

    def test_summary_has_no_row_content(self) -> None:
        """🔴 报告会被提交、被粘贴；它不该带上参考库的数据本身。"""
        summary = _snapshot().summary()
        assert set(summary) == {"tables", "total_rows", "event_order_digest", "learning_digest"}
        assert set(summary["tables"][0]) == {
            "table",
            "row_count",
            "primary_key_digest",
            "content_digest",
        }
