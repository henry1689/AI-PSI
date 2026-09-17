"""假设的单元测试。

🔴 覆盖任务书 §5.8 的四条硬约束与 §9.6 的非人格化解释要求。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from ai_psi.domain.enums import HypothesisCategory, HypothesisStatus
from ai_psi.domain.hypotheses import Hypothesis

pytestmark = pytest.mark.unit


class TestInvariant1NoFactPath:
    """🔴 不变量 1：Hypothesis 不能直接变成已确认事实。"""

    def test_default_status_is_candidate(self, make_hypothesis) -> None:
        assert make_hypothesis().status is HypothesisStatus.CANDIDATE

    def test_cannot_be_written_as_fact(self, make_hypothesis) -> None:
        assert make_hypothesis().can_be_written_as_fact() is False

    def test_is_never_a_factual_claim(self, make_hypothesis) -> None:
        assert make_hypothesis().is_factual_claim is False

    def test_no_status_yields_a_fact(self, make_hypothesis) -> None:
        """**任意**状态下的假设都不是事实——这是类型级保证。"""
        for status in HypothesisStatus:
            h = make_hypothesis(status=status)
            assert h.can_be_written_as_fact() is False
            assert h.is_factual_claim is False

    def test_even_supported_is_not_a_fact(self, make_hypothesis) -> None:
        """``SUPPORTED`` 的语义是"当前证据支持"，不是"事实成立"。"""
        h = make_hypothesis(status=HypothesisStatus.SUPPORTED)
        assert h.can_be_written_as_fact() is False


class TestFalsifiability:
    """不可被反驳的命题不是假设，是信念宣告。"""

    def test_falsification_conditions_are_required(self, make_hypothesis) -> None:
        with pytest.raises(ValidationError):
            make_hypothesis(falsification_conditions=[])

    def test_blank_conditions_are_rejected(self, make_hypothesis) -> None:
        with pytest.raises(ValidationError):
            make_hypothesis(falsification_conditions=["   "])

    def test_valid_condition_accepted(self, make_hypothesis) -> None:
        h = make_hypothesis(falsification_conditions=["若观察到 X，则本假设不成立"])
        assert len(h.falsification_conditions) == 1


class TestNonAgenticExplanations:
    """🔴 任务书 §9.6：至少包含一种非人格化、非心理化解释（场景 B）。"""

    def test_non_agentic_category_is_recognized(self, make_hypothesis) -> None:
        h = make_hypothesis(category=HypothesisCategory.NON_AGENTIC)
        assert h.is_non_agentic

    def test_mechanistic_and_systemic_also_count(self, make_hypothesis) -> None:
        assert make_hypothesis(category=HypothesisCategory.MECHANISTIC).is_non_agentic
        assert make_hypothesis(category=HypothesisCategory.SYSTEMIC).is_non_agentic

    def test_intentional_does_not_count(self, make_hypothesis) -> None:
        """「他讨厌我」不是非人格化解释。"""
        h = make_hypothesis(
            category=HypothesisCategory.INTENTIONAL,
            statement="朋友讨厌用户",
        )
        assert not h.is_non_agentic

    def test_group_has_non_agentic_explanation(self, make_hypothesis) -> None:
        """场景 B：候选假设中必须至少有一个非心理化解释。"""
        hypotheses = [
            make_hypothesis(
                category=HypothesisCategory.INTENTIONAL,
                statement="朋友讨厌用户",
            ),
            make_hypothesis(
                category=HypothesisCategory.NON_AGENTIC,
                statement="朋友当时正在忙，无暇长回复",
            ),
        ]
        assert Hypothesis.has_non_agentic_explanation(hypotheses)

    def test_group_of_only_psychological_explanations_fails(self, make_hypothesis) -> None:
        hypotheses = [
            make_hypothesis(category=HypothesisCategory.INTENTIONAL, statement="他讨厌我"),
            make_hypothesis(category=HypothesisCategory.INTENTIONAL, statement="他在生气"),
        ]
        assert not Hypothesis.has_non_agentic_explanation(hypotheses)

    def test_empty_group_fails(self) -> None:
        assert not Hypothesis.has_non_agentic_explanation([])


class TestHypothesisFields:
    def test_opposing_evidence_can_be_recorded(self, make_hypothesis) -> None:
        """假设必须能记录反对证据——只记支持证据就是确认偏差。"""
        h = make_hypothesis(opposing_evidence_ids=[uuid4()])
        assert len(h.opposing_evidence_ids) == 1

    def test_predicted_observations_support_testing(self, make_hypothesis) -> None:
        h = make_hypothesis(predicted_observations=["朋友后续回复变长"])
        assert h.predicted_observations

    def test_invalid_category_is_rejected(self, make_hypothesis) -> None:
        with pytest.raises(ValidationError):
            make_hypothesis(category="psychological_diagnosis")

    def test_applicability_is_expressible(self, make_hypothesis) -> None:
        h = make_hypothesis(applicability=["仅在异步通信场景"])
        assert h.applicability == ["仅在异步通信场景"]
