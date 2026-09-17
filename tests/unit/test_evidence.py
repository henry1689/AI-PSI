"""证据的单元测试。

🔴 核心：**同源证据规则**（任务书 §5.6）——
多个转载来源不能自动算作多个独立证据。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_psi.domain.enums import EvidenceDirectness, OrdinalLevel

pytestmark = pytest.mark.unit

NOON = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


class TestSameSourceRule:
    """🔴 同源证据必须能被识别为一组。"""

    def test_independence_group_is_supported(self, make_evidence) -> None:
        ev = make_evidence(independence_group="wire-service-2026-06-01")
        assert ev.independence_group == "wire-service-2026-06-01"

    def test_independent_evidence_defaults_to_none(self, make_evidence) -> None:
        assert make_evidence().independence_group is None

    def test_reprints_share_a_group(self, make_evidence) -> None:
        """十条转载同一篇报道的新闻，在证据计数上只算一条。"""
        group = "wire-service-2026-06-01"
        reprints = [
            make_evidence(source_name=f"站点{i}", independence_group=group) for i in range(10)
        ]
        groups = {ev.independence_group for ev in reprints}
        assert groups == {group}, "同源转载必须共享同一分组，否则会被误计为多条独立证据"

    def test_distinct_sources_have_distinct_groups(self, make_evidence) -> None:
        a = make_evidence(source_name="A", independence_group="group-a")
        b = make_evidence(source_name="B", independence_group="group-b")
        assert a.independence_group != b.independence_group


class TestSupportOpposeRelations:
    def test_supports_and_opposes_are_separate(self, make_evidence) -> None:
        claim = uuid4()
        ev = make_evidence(supports_claim_ids=[claim])
        assert ev.supports_claim_ids == [claim]
        assert ev.opposes_claim_ids == []

    def test_self_contradiction_is_rejected(self, make_evidence) -> None:
        """同一证据不能既支持又反对同一条论断——这一定是编码错误。"""
        claim = uuid4()
        with pytest.raises(ValidationError, match="同时支持并反对"):
            make_evidence(supports_claim_ids=[claim], opposes_claim_ids=[claim])

    def test_supporting_one_and_opposing_another_is_fine(self, make_evidence) -> None:
        a, b = uuid4(), uuid4()
        ev = make_evidence(supports_claim_ids=[a], opposes_claim_ids=[b])
        assert not (set(ev.supports_claim_ids) & set(ev.opposes_claim_ids))


class TestThreeQualityDimensions:
    """可靠性、直接性、时效性是**三个独立维度**。"""

    def test_all_three_can_be_set_independently(self, make_evidence) -> None:
        ev = make_evidence(
            reliability=OrdinalLevel.VERY_HIGH,
            directness=EvidenceDirectness.HEARSAY,
            freshness=OrdinalLevel.LOW,
        )
        assert ev.reliability is OrdinalLevel.VERY_HIGH
        assert ev.directness is EvidenceDirectness.HEARSAY
        assert ev.freshness is OrdinalLevel.LOW

    def test_reliable_but_stale_is_representable(self, make_evidence) -> None:
        """可靠来源的过时数据仍然可能不适用——这个组合必须能表达。"""
        ev = make_evidence(
            reliability=OrdinalLevel.VERY_HIGH,
            freshness=OrdinalLevel.VERY_LOW,
        )
        assert ev.reliability.rank > ev.freshness.rank


class TestTimeConsistency:
    def test_retrieved_before_published_is_rejected(self, make_evidence) -> None:
        with pytest.raises(ValidationError, match="retrieved_at"):
            make_evidence(published_at=NOON, retrieved_at=NOON - timedelta(days=1))

    def test_normal_case(self, make_evidence) -> None:
        ev = make_evidence(published_at=NOON, retrieved_at=NOON + timedelta(hours=2))
        assert ev.retrieved_at > ev.published_at

    def test_both_optional(self, make_evidence) -> None:
        ev = make_evidence()
        assert ev.published_at is None
        assert ev.retrieved_at is None


class TestEvidenceContent:
    def test_summary_is_stored_not_full_text(self, make_evidence) -> None:
        """存摘要而非全文，避免把外部长文灌进系统。"""
        ev = make_evidence(content_summary="该研究认为短回复有多种常见原因")
        assert len(ev.content_summary) < 500

    def test_limitations_default_empty_but_supported(self, make_evidence) -> None:
        assert make_evidence().limitations == []
        assert make_evidence(limitations=["样本量小"]).limitations == ["样本量小"]

    def test_naive_time_is_rejected(self, make_evidence) -> None:
        with pytest.raises(ValidationError, match="时区"):
            make_evidence(published_at=datetime(2026, 6, 1))
