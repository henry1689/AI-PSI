"""长期记忆的单元测试。

🔴 本文件覆盖三条最关键的认知不变量：

* **I05**：用户纠正必须生成新版本（不就地覆盖）；
* **I06**：被取代的记忆不能作为默认有效记忆返回；
* **I14**：记忆检索必须遵守 user_id 作用域。

记忆错误与其他错误不同：**它的后果是累积的**。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_psi.domain.enums import MemoryStatus, MemoryType, SensitivityLevel

pytestmark = pytest.mark.unit

JAN = datetime(2026, 1, 1, tzinfo=UTC)


class TestInvariant6SupersededNotReturned:
    """🔴 不变量 6：被取代的记忆不能作为默认有效记忆返回。"""

    def test_proposed_memory_is_not_retrievable(self, make_memory) -> None:
        """未获批准的提案不得被检索到。"""
        assert not make_memory(status=MemoryStatus.PROPOSED).is_default_retrievable

    def test_active_memory_is_retrievable(self, make_memory) -> None:
        assert make_memory(status=MemoryStatus.ACTIVE).is_default_retrievable

    def test_superseded_memory_is_not_retrievable(self, make_memory) -> None:
        assert not make_memory(status=MemoryStatus.SUPERSEDED).is_default_retrievable

    def test_deleted_memory_is_not_retrievable(self, make_memory) -> None:
        assert not make_memory(status=MemoryStatus.DELETED).is_default_retrievable

    def test_expired_memory_is_not_retrievable(self, make_memory) -> None:
        assert not make_memory(status=MemoryStatus.EXPIRED).is_default_retrievable

    def test_rejected_memory_is_not_retrievable(self, make_memory) -> None:
        assert not make_memory(status=MemoryStatus.REJECTED).is_default_retrievable

    def test_disputed_remains_retrievable_but_flagged(self, make_memory) -> None:
        """「存在争议」本身是有价值的信息，故保留可见。"""
        assert make_memory(status=MemoryStatus.DISPUTED).is_default_retrievable


class TestInvariant5CorrectionCreatesNewVersion:
    """🔴 不变量 5：用户纠正必须生成新版本，不就地覆盖。"""

    def test_supersede_produces_a_new_object(self, make_memory) -> None:
        old = make_memory(status=MemoryStatus.ACTIVE)
        replacement = old.superseded_by(replacement_id=uuid4())

        assert replacement is not old
        assert old.status is MemoryStatus.ACTIVE, "原对象不得被就地修改"

    def test_supersede_sets_status_and_bumps_version(self, make_memory) -> None:
        old = make_memory(status=MemoryStatus.ACTIVE)
        replacement = old.superseded_by(replacement_id=uuid4())

        assert replacement.status is MemoryStatus.SUPERSEDED
        assert replacement.version == old.version + 1
        assert not replacement.is_default_retrievable

    def test_supersede_records_the_replacement_link(self, make_memory) -> None:
        """纠错痕迹必须保留——用户应当能看到"为什么被改过"。"""
        new_id = uuid4()
        replacement = make_memory().superseded_by(replacement_id=new_id)
        assert new_id in replacement.contradicts_ids

    def test_new_memory_points_back_via_supersedes_id(self, make_memory) -> None:
        old = make_memory()
        new = make_memory(supersedes_id=old.id)
        assert new.supersedes_id == old.id

    def test_self_supersede_is_rejected(self, make_memory) -> None:
        memory = make_memory()
        with pytest.raises(ValidationError, match="取代自身"):
            make_memory(id=memory.id, supersedes_id=memory.id)


class TestInvariant14UserScope:
    """🔴 不变量 14：记忆检索必须遵守 user_id 作用域。"""

    def test_belongs_to_matches_owner(self, make_memory, user_id) -> None:
        assert make_memory(user_id=user_id).belongs_to(user_id)

    def test_belongs_to_rejects_other_user(self, make_memory, user_id, other_user_id) -> None:
        """🔴 跨用户访问是隐私事故，不是过滤条件没加。"""
        assert not make_memory(user_id=user_id).belongs_to(other_user_id)

    def test_system_level_memory_is_scoped_to_none(self, make_memory, user_id) -> None:
        system_memory = make_memory(user_id=None)
        assert system_memory.belongs_to(None)
        assert not system_memory.belongs_to(user_id)

    def test_user_memory_is_not_visible_to_system_scope(self, make_memory, user_id) -> None:
        """用户记忆不得因为"查的是系统作用域"而被返回。"""
        assert not make_memory(user_id=user_id).belongs_to(None)


class TestMemoryConflicts:
    """冲突不强行合并——冲突本身是有价值的信息（场景 E）。"""

    def test_contradicts_ids_can_be_recorded(self, make_memory) -> None:
        other = uuid4()
        memory = make_memory(contradicts_ids=[other])
        assert memory.contradicts_ids == [other]

    def test_self_contradiction_is_rejected(self, make_memory) -> None:
        memory = make_memory()
        with pytest.raises(ValidationError, match="与自己冲突"):
            make_memory(id=memory.id, contradicts_ids=[memory.id])


class TestMemoryClassification:
    def test_sensitivity_is_classified_at_construction(self, make_memory) -> None:
        """写入前必须分类（任务书 §17.1）。"""
        memory = make_memory(sensitivity=SensitivityLevel.SENSITIVE)
        assert memory.sensitivity.requires_user_confirmation

    def test_all_memory_types_accepted(self, make_memory) -> None:
        for memory_type in MemoryType:
            assert make_memory(memory_type=memory_type).memory_type is memory_type

    def test_invalid_memory_type_is_rejected(self, make_memory) -> None:
        with pytest.raises(ValidationError):
            make_memory(memory_type="personality_profile")

    def test_sources_are_traceable(self, make_memory) -> None:
        """记忆必须可追溯到它为什么存在。"""
        event_ids = [uuid4(), uuid4()]
        memory = make_memory(source_event_ids=event_ids)
        assert memory.source_event_ids == event_ids

    def test_validity_window_enforced(self, make_memory) -> None:
        with pytest.raises(ValidationError, match="valid_until"):
            make_memory(valid_from=JAN, valid_until=datetime(2025, 1, 1, tzinfo=UTC))

    def test_embedding_version_is_tracked(self, make_memory) -> None:
        """向量模型版本影响检索兼容性。"""
        assert make_memory(embedding_version="v1").embedding_version == "v1"
