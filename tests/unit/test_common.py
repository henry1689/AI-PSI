"""``EntityMetadata`` 与时间语义的单元测试（ADR-0006）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from ai_psi.domain.common import SCHEMA_VERSION_V1, EntityMetadata, utc_now
from ai_psi.domain.enums import SourceType
from ai_psi.domain.observations import Observation
from tests.helpers import rejects

pytestmark = pytest.mark.unit


class _Sample(EntityMetadata):
    """用于测试基类行为的最小具体实现。"""

    label: str = "sample"


class TestTimeSemantics:
    """时间必须统一 UTC 且 tz-aware。"""

    def test_utc_now_is_aware_and_utc(self) -> None:
        now = utc_now()
        assert now.tzinfo is not None
        assert now.utcoffset() == timedelta(0)

    def test_naive_datetime_is_rejected(self) -> None:
        """🔴 naive datetime 会让跨时区比较静默出错，必须直接拒绝。"""
        with pytest.raises(ValidationError, match="时区"):
            # 故意构造 naive datetime
            _Sample(created_by="t", created_at=datetime(2026, 1, 1, 12, 0, 0))

    def test_non_utc_aware_datetime_is_converted(self) -> None:
        """带非 UTC 时区的时间是合法的，但会被转换到 UTC。"""
        tokyo = timezone(timedelta(hours=9))
        sample = _Sample(created_by="t", created_at=datetime(2026, 1, 1, 21, 0, 0, tzinfo=tokyo))
        assert sample.created_at == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

    def test_updated_at_may_not_precede_created_at(self) -> None:
        created = datetime(2026, 1, 2, tzinfo=UTC)
        earlier = datetime(2026, 1, 1, tzinfo=UTC)
        with pytest.raises(ValidationError, match="updated_at"):
            _Sample(created_by="t", created_at=created, updated_at=earlier)


class TestSchemaStrictness:
    """未知字段一律拒绝——模型输出是不可信输入。"""

    def test_extra_field_is_rejected(self) -> None:
        rejects(_Sample, created_by="t", unexpected_field="should not be silently accepted")

    def test_created_by_is_required_and_non_empty(self) -> None:
        with pytest.raises(ValidationError):
            _Sample(created_by="")
        with pytest.raises(ValidationError):
            _Sample.model_validate({})

    def test_defaults(self) -> None:
        sample = _Sample(created_by="t")
        assert sample.version == 1
        assert sample.schema_version == SCHEMA_VERSION_V1
        assert sample.created_at.tzinfo is not None

    def test_ids_are_unique(self) -> None:
        ids = {_Sample(created_by="t").id for _ in range(50)}
        assert len(ids) == 50

    def test_version_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            _Sample(created_by="t", version=0)


class TestOptimisticLocking:
    """乐观锁更新路径必须产生新版本，禁止就地覆盖（ADR-0002）。"""

    def test_bumped_increments_version_and_refreshes_timestamp(self) -> None:
        original = _Sample(created_by="t", created_at=datetime(2020, 1, 1, tzinfo=UTC))
        updated = original.bumped(label="changed")

        assert updated.version == original.version + 1
        assert updated.updated_at > original.updated_at
        assert updated.label == "changed"

    def test_bumped_returns_new_instance(self) -> None:
        """原实例不得被修改——就地覆盖会销毁审计证据。"""
        original = _Sample(created_by="t")
        updated = original.bumped(label="changed")

        assert original is not updated
        assert original.label == "sample"
        assert original.version == 1

    def test_bumped_preserves_identity(self) -> None:
        original = _Sample(created_by="t")
        assert original.bumped().id == original.id

    def test_bumped_always_overrides_version(self) -> None:
        """``bumped`` 自己决定新版本号，调用方传的 ``version`` 会被覆盖。

        这是有意的：乐观锁的版本推进不能由调用方随意指定，
        否则可以伪造版本号绕过冲突检测。
        """
        sample = _Sample(created_by="t")
        assert sample.bumped(version=99).version == sample.version + 1

    def test_bumped_revalidates(self) -> None:
        """``bumped`` 走完整校验，非法更新同样会被拒绝。"""
        naive = datetime(2026, 1, 1, 12, 0, 0)
        with pytest.raises(ValidationError, match="时区"):
            _Sample(created_by="t").bumped(created_at=naive)


class TestAssignmentValidation:
    """``validate_assignment=True`` 让非法赋值当场失败。"""

    def test_invalid_assignment_is_rejected_immediately(self) -> None:
        sample = _Sample(created_by="t")
        with pytest.raises(ValidationError):
            sample.version = 0

    def test_valid_assignment_is_accepted(self) -> None:
        sample = _Sample(created_by="t")
        sample.label = "changed"
        assert sample.label == "changed"
        assert sample.version == 1, "直接赋值不应自动递增版本——递增只应经 bumped()"


class TestEntityInheritance:
    """ADR-0006：所有领域对象继承 EntityMetadata。"""

    def test_observation_carries_metadata(self) -> None:
        obs = Observation(
            created_by="t",
            content="x",
            source_id="s",
            source_type=SourceType.USER_MESSAGE,
        )
        assert obs.id is not None
        assert obs.version == 1
        assert obs.schema_version == SCHEMA_VERSION_V1
        assert obs.created_at.tzinfo is not None
