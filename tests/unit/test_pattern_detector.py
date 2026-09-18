"""模式发现（任务书 §11.3 第一条）。

🔴 本文件的重点是**"同类"的定义**。

模式发现是提案的上游：它把 N 条经验说成"同一类错误发生了 N 次"。
这个"同一类"如果没有一个可比较的定义，门槛就无从判定——
而门槛是不变量 10（单次经验不推广）唯一的数值落点。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

import pytest

from ai_psi.domain.enums import ConfidenceBand, ErrorType
from ai_psi.domain.experiences import Experience
from ai_psi.learning.pattern_detector import ErrorPattern, PatternDetector

pytestmark = pytest.mark.unit

#: ``tests/conftest.py`` 的 ``make_experience`` 夹具形状。
Factory = Callable[..., Any]


def _experience(make_experience: Factory, **overrides: object) -> Experience:
    """构造一条**可归因**的经验。默认置信度必须够高，否则会被过滤掉。"""
    payload: dict[str, object] = {
        "error_type": ErrorType.REASONING_ERROR,
        "attribution_confidence": ConfidenceBand.MODERATE,
        "situation_signature": "d1|with_evidence|h2",
    }
    payload.update(overrides)
    experience: Experience = make_experience(**payload)
    return experience


def _repeated(make_experience: Factory, count: int, **overrides: object) -> list[Experience]:
    return [_experience(make_experience, **overrides) for _ in range(count)]


@pytest.fixture
def detector() -> PatternDetector:
    return PatternDetector()


class TestThreshold:
    def test_below_threshold_is_not_a_pattern(self, detector, make_experience) -> None:
        """🔴 不变量 10 的第一道落地：两条经验不构成模式。"""
        assert detector.detect(_repeated(make_experience, 2)) == []

    def test_threshold_is_inclusive(self, detector, make_experience) -> None:
        patterns = detector.detect(_repeated(make_experience, 3))
        assert len(patterns) == 1
        assert patterns[0].count == 3

    def test_empty_input_yields_nothing(self, detector) -> None:
        assert detector.detect([]) == []

    def test_threshold_below_two_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="不变量 10"):
            PatternDetector(threshold=1)

    def test_threshold_is_configurable_upward(self, make_experience) -> None:
        strict = PatternDetector(threshold=4)
        assert strict.detect(_repeated(make_experience, 3)) == []
        assert strict.threshold == 4


class TestWhatCountsAsTheSameKind:
    """🔴 同类 = （错误类别, 情境签名）。两者缺一不可。"""

    def test_different_signature_is_a_different_pattern(self, detector, make_experience) -> None:
        """只看类别的后果：两类问题里各错两次会被并成"错了四次"。"""
        experiences = _repeated(make_experience, 2, situation_signature="d1|with_evidence|h2")
        experiences += _repeated(make_experience, 2, situation_signature="d3|no_evidence|h0")
        assert detector.detect(experiences) == []

    def test_different_error_type_is_a_different_pattern(self, detector, make_experience) -> None:
        """只看情境的后果：同一类问题里的不同错误会被并成"这类问题老出错"。"""
        experiences = _repeated(make_experience, 2, error_type=ErrorType.SCOPE_ERROR)
        experiences += _repeated(make_experience, 2, error_type=ErrorType.EVIDENCE_ERROR)
        assert detector.detect(experiences) == []

    def test_both_dimensions_must_match(self, detector, make_experience) -> None:
        experiences = _repeated(make_experience, 2, error_type=ErrorType.SCOPE_ERROR)
        experiences += _repeated(
            make_experience,
            2,
            error_type=ErrorType.SCOPE_ERROR,
            situation_signature="另一个情境",
        )
        experiences += _repeated(make_experience, 1, error_type=ErrorType.EVIDENCE_ERROR)
        assert detector.detect(experiences) == []


class TestUnattributableExperiencesAreExcluded:
    """🔴 "不知道错在哪"与"没有错"是两回事。"""

    def test_missing_error_type_does_not_count(self, detector, make_experience) -> None:
        unattributed = _repeated(make_experience, 3, error_type=None)
        assert detector.detect(unattributed) == []

    def test_unattributed_experiences_do_not_pad_a_real_pattern(
        self, detector, make_experience
    ) -> None:
        """两条已归因 + 三条未归因 ≠ 五条同类错误。"""
        experiences = _repeated(make_experience, 2)
        experiences += _repeated(make_experience, 3, error_type=None)
        assert detector.detect(experiences) == []

    def test_very_low_confidence_is_excluded(self, detector, make_experience) -> None:
        """含糊的猜测凑不成提案——提案是**会被人认真评估**的东西。"""
        shaky = _repeated(make_experience, 3, attribution_confidence=ConfidenceBand.VERY_LOW)
        assert detector.detect(shaky) == []

    def test_low_confidence_still_counts(self, detector, make_experience) -> None:
        """LOW 是**可用的下界**，边界值要落在能用的那一侧。"""
        patterns = detector.detect(
            _repeated(make_experience, 3, attribution_confidence=ConfidenceBand.LOW)
        )
        assert len(patterns) == 1


class TestPatternContent:
    def test_duplicate_ids_do_not_inflate_the_count(self, detector, make_experience) -> None:
        """同一条经验被重复传入不算两次——否则门槛可以被同一份证据凑满。"""
        experience = _experience(make_experience)
        assert detector.detect([experience, experience, experience]) == []

    def test_experience_ids_are_sorted(self, detector, make_experience) -> None:
        patterns = detector.detect(_repeated(make_experience, 3))
        ids = patterns[0].experience_ids
        assert list(ids) == sorted(ids, key=str)

    def test_counterexamples_are_counted(self, detector, make_experience) -> None:
        experiences = _repeated(make_experience, 2, counterexamples=["反例"])
        experiences += _repeated(make_experience, 1)
        pattern = detector.detect(experiences)[0]
        assert pattern.counterexample_count == 2

    def test_conditions_are_merged_deduplicated_and_sorted(self, detector, make_experience) -> None:
        experiences = _repeated(
            make_experience, 1, applicable_conditions=["仅限单来源", "中文语料"]
        )
        experiences += _repeated(make_experience, 2, applicable_conditions=["仅限单来源"])
        pattern = detector.detect(experiences)[0]
        assert pattern.applicable_conditions == ("中文语料", "仅限单来源")

    def test_first_and_last_round_are_tracked(self, detector, make_experience) -> None:
        """用于判断"这个问题还活着吗"——久未复现的模式优先级更低。"""
        patterns = detector.detect(_repeated(make_experience, 3))
        assert patterns[0].first_round_id is not None
        assert patterns[0].last_round_id is not None

    def test_pattern_count_matches_members(self, detector, make_experience) -> None:
        pattern = detector.detect(_repeated(make_experience, 4))[0]
        assert pattern.count == len(pattern.experience_ids) == 4

    def test_pattern_is_frozen(self, detector, make_experience) -> None:
        pattern = detector.detect(_repeated(make_experience, 3))[0]
        assert isinstance(pattern, ErrorPattern)
        with pytest.raises(dataclasses.FrozenInstanceError):
            pattern.error_type = ErrorType.FACTUAL_ERROR  # type: ignore[misc]


class TestDeterministicOrdering:
    """🔴 同一批经验在任何一次运行里都必须得到同样的顺序。

    否则"这次跑出三个提案、下次跑出两个"会变成常态，
    而没有人能解释为什么。
    """

    def test_input_order_does_not_matter(self, detector, make_experience) -> None:
        experiences = _repeated(make_experience, 3)
        forwards = detector.detect(experiences)
        backwards = detector.detect(list(reversed(experiences)))
        assert [item.experience_ids for item in forwards] == [
            item.experience_ids for item in backwards
        ]

    def test_more_frequent_patterns_come_first(self, detector, make_experience) -> None:
        experiences = _repeated(make_experience, 3, situation_signature="少")
        experiences += _repeated(make_experience, 5, situation_signature="多")
        patterns = detector.detect(experiences)
        assert [item.count for item in patterns] == [5, 3]

    def test_ties_are_broken_deterministically(self, detector, make_experience) -> None:
        experiences = _repeated(make_experience, 3, situation_signature="sig-b")
        experiences += _repeated(make_experience, 3, situation_signature="sig-a")
        patterns = detector.detect(experiences)
        assert [item.situation_signature for item in patterns] == ["sig-a", "sig-b"]

    def test_repeated_runs_agree(self, detector, make_experience) -> None:
        experiences = _repeated(make_experience, 3, situation_signature="甲")
        experiences += _repeated(
            make_experience, 3, error_type=ErrorType.SCOPE_ERROR, situation_signature="乙"
        )
        first = [
            (item.error_type, item.situation_signature) for item in detector.detect(experiences)
        ]
        second = [
            (item.error_type, item.situation_signature) for item in detector.detect(experiences)
        ]
        assert first == second
