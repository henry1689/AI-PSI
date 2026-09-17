"""用户模型的单元测试。

🔴 这是最容易越界的领域对象：任务书 §2.3 明确禁止心理诊断、
自动生成稳定人格画像、从少量对话推断价值观。

**不变量 13**：用户模型中的推测不得标记为确认事实。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_psi.domain.enums import ConfidenceBand, UserModelStatus

pytestmark = pytest.mark.unit

JAN = datetime(2026, 1, 1, tzinfo=UTC)


class TestInvariant13NoConfirmed:
    """🔴 不变量 13：不得标记为确认事实。"""

    def test_can_never_be_confirmed(self, make_user_model) -> None:
        for status in UserModelStatus:
            assert make_user_model(status=status).is_confirmable is False

    def test_confirmed_status_is_not_assignable(self, make_user_model) -> None:
        with pytest.raises(ValidationError):
            make_user_model(status="confirmed")

    def test_highest_status_is_user_stated(self) -> None:
        """最高只能到"用户自己这么说过"。"""
        assert UserModelStatus.USER_STATED in UserModelStatus
        forbidden = {"confirmed", "verified", "established", "fact"}
        assert not (forbidden & {s.value for s in UserModelStatus})

    def test_default_is_hypothesized(self, make_user_model) -> None:
        """默认是"假设"，不是"观察到的模式"——保守是安全的方向。"""
        assert make_user_model().status is UserModelStatus.HYPOTHESIZED


class TestEvidenceRequirement:
    """没有证据的用户模型条目就是刻板印象。"""

    def test_evidence_is_required(self, make_user_model) -> None:
        with pytest.raises(ValidationError):
            make_user_model(evidence_ids=[])

    def test_evidence_is_carried(self, make_user_model) -> None:
        evidence = [uuid4()]
        assert make_user_model(evidence_ids=evidence).evidence_ids == evidence


class TestBehavioralNotPersonality:
    """属性名应当是行为层面的，不是人格层面的。"""

    def test_behavioral_attribute_accepted(self, make_user_model) -> None:
        model = make_user_model(attribute="preferred_response_length", value="concise")
        assert model.attribute == "preferred_response_length"

    def test_attribute_is_required_non_empty(self, make_user_model) -> None:
        with pytest.raises(ValidationError):
            make_user_model(attribute="")

    def test_default_confidence_is_low(self, make_user_model) -> None:
        """用户模型的置信度上限通常远低于事实判断。"""
        assert make_user_model().confidence_band is ConfidenceBand.LOW


class TestStaleness:
    def test_validity_window_enforced(self, make_user_model) -> None:
        with pytest.raises(ValidationError, match="valid_until"):
            make_user_model(valid_from=JAN, valid_until=datetime(2025, 6, 1, tzinfo=UTC))

    def test_stale_status_exists(self) -> None:
        """用户会变化，过期的用户模型必须能被识别。"""
        assert UserModelStatus.STALE in UserModelStatus

    def test_retracted_status_exists(self) -> None:
        assert UserModelStatus.RETRACTED in UserModelStatus

    def test_open_ended_is_allowed(self, make_user_model) -> None:
        assert make_user_model(valid_until=None).valid_until is None
