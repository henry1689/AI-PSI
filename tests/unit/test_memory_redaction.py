"""审计脱敏与用户数据导出（任务书 §10.5、§17.1）。

这个文件守的是一条容易被绕过的规则：

> **审计信息不得保留被删除内容正文。**

它之所以容易被绕过，是因为"留一份副本方便追溯"听起来完全合理——
直到有人意识到：用户以为删掉的东西，一直躺在**只追加**的事件表里。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ai_psi.domain.enums import MemoryStatus, MemoryType, RetentionPolicy, SensitivityLevel
from ai_psi.domain.exceptions import ConstitutionViolationError
from ai_psi.domain.memories import Memory
from ai_psi.memory.redaction import (
    FORBIDDEN_AUDIT_KEYS,
    USER_DATA_EXPORT_VERSION,
    assert_audit_payload_safe,
    audit_payload_for_memory,
    export_bundle,
    export_record,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 1, tzinfo=UTC)
SECRET = "用户有一段他不想让任何人知道的话"


def _memory(content: str = SECRET, **overrides: object) -> Memory:
    payload: dict[str, object] = {
        "created_by": "test",
        "user_id": uuid4(),
        "memory_type": MemoryType.USER_PREFERENCE,
        "content": content,
        "valid_from": NOW - timedelta(days=10),
        "status": MemoryStatus.ACTIVE,
    }
    payload.update(overrides)
    return Memory(**payload)  # type: ignore[arg-type]


class TestAssertAuditPayloadSafe:
    def test_accepts_a_clean_payload(self) -> None:
        assert_audit_payload_safe({"memory_id": "x", "content_length": 3}, operation="t")

    @pytest.mark.parametrize("key", sorted(FORBIDDEN_AUDIT_KEYS))
    def test_rejects_forbidden_keys(self, key: str) -> None:
        with pytest.raises(ConstitutionViolationError) as excinfo:
            assert_audit_payload_safe({key: "正文"}, operation="memory_delete")
        assert excinfo.value.invariant_id == "S10.5"
        assert excinfo.value.context["offending_keys"] == [key]

    def test_key_matching_is_case_insensitive(self) -> None:
        """大小写不同的 ``Content`` 同样是正文。

        事件负载由人手工组装，而手写键名的大小写是随意的；
        只匹配小写等于给"绕过去"留了一扇小门。
        """
        with pytest.raises(ConstitutionViolationError):
            assert_audit_payload_safe({"Content": "正文"}, operation="t")

    def test_error_message_names_the_rule(self) -> None:
        with pytest.raises(ConstitutionViolationError, match="不得保留内容正文"):
            assert_audit_payload_safe({"content": "x"}, operation="t")


class TestAuditPayloadForMemory:
    def test_contains_identifiers_but_no_content(self) -> None:
        memory = _memory()
        payload = audit_payload_for_memory(memory, operation="memory_delete")
        assert payload["memory_id"] == str(memory.id)
        assert payload["memory_type"] == memory.memory_type.value
        assert payload["content_length"] == len(SECRET)
        assert SECRET not in str(payload)

    def test_contains_no_hash_either(self) -> None:
        """🔴 不含内容的哈希。

        哈希是内容的**可验证承诺**：对取值空间很小的短句
        （"用户有抑郁症"），持有哈希的人可以枚举候选逐个比对来还原原文。
        删除之后还留下这样一条线索，与"内容真的不再存在"相差不远，但不是。
        """
        payload = audit_payload_for_memory(_memory(), operation="memory_delete")
        assert not any("hash" in key or "digest" in key or "fingerprint" in key for key in payload)

    def test_records_sensitivity_for_auditing(self) -> None:
        memory = _memory(sensitivity=SensitivityLevel.HIGHLY_SENSITIVE)
        payload = audit_payload_for_memory(memory, operation="memory_delete")
        assert payload["sensitivity"] == "highly_sensitive"


class TestExportRecord:
    def test_contains_the_content(self) -> None:
        """与审计负载相反：**导出必须有正文**。

        "按 user_id 导出"如果导出的是一堆 id 和长度，那就不是导出。
        """
        record = export_record(_memory(), now=NOW)
        assert record["content"] == SECRET

    def test_explains_the_retention_policy(self) -> None:
        record = export_record(_memory(retention_policy=RetentionPolicy.SESSION), now=NOW)
        assert "尚未实现" in record["retention_note"]

    def test_marks_expired_memories_at_export_time(self) -> None:
        record = export_record(_memory(valid_until=NOW - timedelta(days=1)), now=NOW)
        assert record["expired_at_export"] is True

    def test_marks_superseded_versions(self) -> None:
        """导出要能看出"这条已经被取代了"——版本链是纠错痕迹的全部。"""
        record = export_record(
            _memory(status=MemoryStatus.SUPERSEDED, supersedes_id=uuid4()), now=NOW
        )
        assert record["superseded"] is True
        assert record["supersedes_id"] is not None


class TestExportBundle:
    def test_includes_inactive_memories(self) -> None:
        """🔴 导出包含被取代与已删除的记忆。

        "为什么发生过修正"靠版本链回答；只导出有效记忆会让纠错痕迹
        凭空消失——而那恰恰是用户最需要看到的部分。
        """
        user = uuid4()
        active = _memory(user_id=user)
        deleted = _memory(user_id=user, status=MemoryStatus.DELETED)
        bundle = export_bundle(user_id=user, memories=[active, deleted], now=NOW)
        assert bundle["memory_count"] == 2
        assert bundle["active_count"] == 1

    def test_carries_a_format_version(self) -> None:
        """导出文件会独立于代码演进，"这份文件是哪个版本导出的"必须可判断。"""
        bundle = export_bundle(user_id=None, memories=[], now=NOW)
        assert bundle["export_version"] == USER_DATA_EXPORT_VERSION

    def test_serialisable(self) -> None:
        """导出包必须能直接 JSON 序列化——它是交给用户的东西。"""
        import json

        bundle = export_bundle(user_id=uuid4(), memories=[_memory()], now=NOW)
        assert SECRET in json.dumps(bundle, ensure_ascii=False)

    def test_empty_export(self) -> None:
        bundle = export_bundle(user_id=uuid4(), memories=[], now=NOW)
        assert bundle["memories"] == []
        assert bundle["memory_count"] == 0
