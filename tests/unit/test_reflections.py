"""元认知反思的单元测试。

🔴 覆盖防反刍硬规则（任务书 §13.3，不变量 8）与元认知递归禁令（§9.11）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_psi.domain.enums import MetacognitiveDecision, OrdinalLevel

pytestmark = pytest.mark.unit

REPEAT_THRESHOLD = 0.85


class TestInvariant8AntiRumination:
    """🔴 不变量 8：没有新证据或新路径时，不得允许无限继续。"""

    def test_no_new_evidence_no_new_path_high_repeat_forces_stop(self, make_reflection) -> None:
        r = make_reflection(
            new_evidence_present=False,
            new_reasoning_path_present=False,
            repeated_claim_score=0.95,
        )
        assert r.should_force_stop(repeat_threshold=REPEAT_THRESHOLD)

    def test_new_evidence_prevents_forced_stop(self, make_reflection) -> None:
        """有新证据就有边际收益，不该被强制停止。"""
        r = make_reflection(
            new_evidence_present=True,
            new_reasoning_path_present=False,
            repeated_claim_score=0.95,
        )
        assert not r.should_force_stop(repeat_threshold=REPEAT_THRESHOLD)

    def test_new_reasoning_path_prevents_forced_stop(self, make_reflection) -> None:
        r = make_reflection(
            new_evidence_present=False,
            new_reasoning_path_present=True,
            repeated_claim_score=0.95,
        )
        assert not r.should_force_stop(repeat_threshold=REPEAT_THRESHOLD)

    def test_low_repeat_does_not_force_stop(self, make_reflection) -> None:
        """重复度不高说明还在推进，即使暂时没有新证据。"""
        r = make_reflection(
            new_evidence_present=False,
            new_reasoning_path_present=False,
            repeated_claim_score=0.4,
        )
        assert not r.should_force_stop(repeat_threshold=REPEAT_THRESHOLD)

    def test_threshold_is_injectable_not_hardcoded(self, make_reflection) -> None:
        """阈值属配置（ADR-0008），不得内嵌在领域对象里。"""
        r = make_reflection(new_evidence_present=False, repeated_claim_score=0.5)
        assert not r.should_force_stop(repeat_threshold=0.85)
        assert r.should_force_stop(repeat_threshold=0.3)

    def test_boundary_is_inclusive(self, make_reflection) -> None:
        r = make_reflection(repeated_claim_score=REPEAT_THRESHOLD)
        assert r.should_force_stop(repeat_threshold=REPEAT_THRESHOLD)


class TestMarginalGain:
    def test_has_gain_when_evidence_or_path_present(self, make_reflection) -> None:
        assert make_reflection(new_evidence_present=True).has_marginal_gain
        assert make_reflection(new_reasoning_path_present=True).has_marginal_gain

    def test_no_gain_when_neither(self, make_reflection) -> None:
        assert not make_reflection().has_marginal_gain


class TestRepeatedClaimScoreRange:
    def test_score_is_bounded_to_unit_interval(self, make_reflection) -> None:
        with pytest.raises(ValidationError):
            make_reflection(repeated_claim_score=1.5)
        with pytest.raises(ValidationError):
            make_reflection(repeated_claim_score=-0.1)

    def test_score_is_a_float_not_a_band(self, make_reflection) -> None:
        """它是相似度**度量**，与置信度分档语义不同（ADR-0012）。"""
        r = make_reflection(repeated_claim_score=0.42)
        assert isinstance(r.repeated_claim_score, float)


class TestModelLayerChecks:
    def test_bias_risks_are_ordinal(self, make_reflection) -> None:
        r = make_reflection(
            confirmation_bias_risk=OrdinalLevel.HIGH,
            user_pleasing_bias_risk=OrdinalLevel.VERY_HIGH,
            abstraction_escape_risk=OrdinalLevel.MODERATE,
        )
        assert r.user_pleasing_bias_risk.rank == 4
        assert r.confirmation_bias_risk.rank == 3

    def test_defaults_are_conservative(self, make_reflection) -> None:
        r = make_reflection()
        assert r.confirmation_bias_risk is OrdinalLevel.VERY_LOW
        assert r.user_pleasing_bias_risk is OrdinalLevel.VERY_LOW

    def test_unsupported_certainty_is_flaggeable(self, make_reflection) -> None:
        assert make_reflection(unsupported_certainty_detected=True).unsupported_certainty_detected

    def test_missing_counterexample_is_flaggeable(self, make_reflection) -> None:
        assert make_reflection(missing_counterexample_detected=True).missing_counterexample_detected


class TestDecisionReasons:
    def test_reasons_are_required(self, make_reflection) -> None:
        """停止必须有理由——不变量 19 在执行层面的落点。"""
        with pytest.raises(ValidationError):
            make_reflection(reasons=[])

    def test_blank_reasons_rejected(self, make_reflection) -> None:
        with pytest.raises(ValidationError):
            make_reflection(reasons=["   "])

    def test_default_decision_is_stop(self, make_reflection) -> None:
        """默认停止——不继续是最安全的默认值。"""
        assert make_reflection().decision is MetacognitiveDecision.STOP


class TestNoRecursion:
    """🔴 任务书 §9.11：元认知模型不能调用自身形成无限递归。"""

    def test_reflection_holds_no_executable_reference(self) -> None:
        """Reflection 上不存在任何可再次触发元认知的字段。"""
        from ai_psi.domain.reflections import Reflection

        fields = set(Reflection.model_fields)
        assert "callable" not in fields
        assert "next_reflection" not in fields
        assert "nested_reflection" not in fields
