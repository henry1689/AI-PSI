"""观察、关切与认知问题的单元测试。

重点关注**边界**而非字段：Observation 不得承载心理推断；
Concern 必须有依据；Inquiry 必须是"可结束的"。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_psi.domain.enums import CognitiveDepth, ConcernStatus

pytestmark = pytest.mark.unit


class TestObservation:
    """🔴 Observation 只描述"看见或收到什么"。"""

    def test_observation_has_no_inference_field(self, make_observation) -> None:
        """对象上不存在任何"推断"字段——推断必须走 Hypothesis。"""
        fields = set(type(make_observation()).model_fields)
        assert "inference" not in fields
        assert "interpretation" not in fields
        assert "diagnosis" not in fields

    def test_observed_short_reply_is_a_valid_observation(self, make_observation) -> None:
        """场景 B：「回复很短」是可观察的事实。"""
        obs = make_observation(content="朋友今天只回复了一个「嗯」")
        assert obs.content == "朋友今天只回复了一个「嗯」"

    def test_observation_can_declare_its_limits(self, make_observation) -> None:
        """观察应当能声明自身局限——这是它区别于"事实"的关键。"""
        obs = make_observation(
            content="朋友只回复了一个「嗯」",
            limitations=["样本仅一条消息", "无法判断当时的语境"],
        )
        assert len(obs.limitations) == 2

    def test_possible_expiry_supports_time_bound_observations(self, make_observation) -> None:
        obs = make_observation(possible_expiry=datetime(2026, 6, 2, tzinfo=UTC))
        assert obs.possible_expiry is not None

    def test_naive_expiry_is_rejected(self, make_observation) -> None:
        with pytest.raises(ValidationError, match="时区"):
            make_observation(possible_expiry=datetime(2026, 6, 2))

    def test_content_must_be_non_empty(self, make_observation) -> None:
        with pytest.raises(ValidationError):
            make_observation(content="")

    def test_invalid_source_type_is_rejected(self, make_observation) -> None:
        with pytest.raises(ValidationError):
            make_observation(source_type="telepathy")


class TestConcern:
    """关切必须有依据——无来源的关切不应被创建（任务书 §9.1）。"""

    def test_source_events_are_required(self, make_concern) -> None:
        with pytest.raises(ValidationError):
            make_concern(source_event_ids=[])

    def test_duplicate_sources_are_deduplicated(self, make_concern) -> None:
        """重复引用会让"证据计数"虚高。"""
        shared = uuid4()
        concern = make_concern(source_event_ids=[shared, shared, shared])
        assert concern.source_event_ids == [shared]

    def test_why_it_matters_is_required(self, make_concern) -> None:
        """说不清"为什么重要"的关切不值得花认知预算。"""
        with pytest.raises(ValidationError):
            make_concern(why_it_matters="")

    def test_defaults_to_open(self, make_concern) -> None:
        assert make_concern().status is ConcernStatus.OPEN

    def test_all_nine_categories_are_accepted(self, make_concern) -> None:
        from ai_psi.domain.enums import ConcernCategory

        for category in ConcernCategory:
            assert make_concern(category=category).category is category

    def test_information_value_and_cost_are_tracked(self, make_concern) -> None:
        """关切必须能表达"值不值得想"——这是预算分配的依据。"""
        from ai_psi.domain.enums import OrdinalLevel

        concern = make_concern(
            expected_information_value=OrdinalLevel.VERY_HIGH,
            cognitive_cost=OrdinalLevel.LOW,
        )
        assert concern.expected_information_value.rank > concern.cognitive_cost.rank


class TestInquiry:
    """Inquiry 必须把关切转成**可结束的**问题。"""

    def test_out_of_scope_is_required(self, make_inquiry) -> None:
        """排除范围与范围同等重要——没有它，问题会无限膨胀。"""
        with pytest.raises(ValidationError):
            make_inquiry(out_of_scope=[])

    def test_scope_is_required(self, make_inquiry) -> None:
        with pytest.raises(ValidationError):
            make_inquiry(scope=[])

    def test_stop_conditions_are_required(self, make_inquiry) -> None:
        """🔴 没有停止条件的问题会导致无限反思。"""
        with pytest.raises(ValidationError):
            make_inquiry(stop_conditions=[])

    def test_blank_entries_are_rejected(self, make_inquiry) -> None:
        """空字符串会让"必填非空"形同虚设。"""
        with pytest.raises(ValidationError):
            make_inquiry(scope=["  ", "有效范围"])

    def test_reopen_conditions_are_optional(self, make_inquiry) -> None:
        """有些问题被回答后就此结束，无需重开条件。"""
        assert make_inquiry().reopen_conditions == []

    def test_depth_defaults_to_d0(self, make_inquiry) -> None:
        assert make_inquiry().depth_level is CognitiveDepth.D0

    def test_depth_can_be_set_by_router(self, make_inquiry) -> None:
        assert make_inquiry(depth_level=CognitiveDepth.D3).depth_level is CognitiveDepth.D3
