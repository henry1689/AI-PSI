"""可靠性层：预算记账、重复度检测、置信度上限。

这三块是"认知系统不失控"的机械保障：

* 预算是**花钱之前先问够不够**（超预算回合率硬指标 = 0）；
* 重复度是防反刍的客观信号（任务书 §13.3）；
* 置信度上限是"没有依据就不许说得那么肯定"（不变量 3、7）。
"""

from __future__ import annotations

import pytest

from ai_psi.domain.cognitive_rounds import CognitiveBudget
from ai_psi.domain.enums import CognitiveDepth, ConfidenceBand
from ai_psi.domain.exceptions import BudgetExhaustedError
from ai_psi.reliability.budgets import MANDATORY_TAIL_CALLS, BudgetTracker
from ai_psi.reliability.confidence import clamp, derive_ceiling, lower_band
from ai_psi.reliability.repetition_detector import (
    DEFAULT_REPETITION_THRESHOLD,
    jaccard,
    normalized_ngrams,
    repeated_claim_score,
    signature_for,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 预算记账
# ---------------------------------------------------------------------------


class TestBudgetTracker:
    def test_starts_empty(self) -> None:
        tracker = BudgetTracker(CognitiveBudget(max_model_calls=3))
        assert tracker.model_calls_used == 0
        assert tracker.remaining_model_calls == 3

    def test_spend_decrements(self) -> None:
        tracker = BudgetTracker(CognitiveBudget(max_model_calls=2))
        tracker.spend_model_call(task_name="a")
        tracker.spend_model_call(task_name="b")
        assert tracker.remaining_model_calls == 0

    def test_spending_beyond_limit_raises(self) -> None:
        """🔴 超预算必须是**不可能事件**：额度耗尽时直接拒绝花费。"""
        tracker = BudgetTracker(CognitiveBudget(max_model_calls=1))
        tracker.spend_model_call(task_name="a")
        with pytest.raises(BudgetExhaustedError) as excinfo:
            tracker.spend_model_call(task_name="b")
        assert excinfo.value.budget_name == "max_model_calls"
        assert excinfo.value.context["task_name"] == "b"

    def test_tail_reserve_blocks_optional_spending(self) -> None:
        """可选模块花到只剩尾部保留额度时，就必须停下。"""
        tracker = BudgetTracker(CognitiveBudget(max_model_calls=3), tail_reserve=2)
        assert tracker.can_afford(1, keep_tail_reserve=True)
        tracker.spend_model_call(task_name="optional")
        assert not tracker.can_afford(1, keep_tail_reserve=True)
        # 强制步骤仍然可以花——它本来就是被保留的那部分
        assert tracker.can_afford(1, keep_tail_reserve=False)

    def test_tail_reserve_shrinks(self) -> None:
        tracker = BudgetTracker(CognitiveBudget(max_model_calls=3), tail_reserve=3)
        tracker.spend_model_call(task_name="a")
        tracker.set_tail_reserve(1)
        assert tracker.tail_reserve == 1
        assert tracker.can_afford(1, keep_tail_reserve=True)

    def test_rebase_preserves_usage(self) -> None:
        """🔴 下调预算时**必须搬运已消耗计数**。

        不搬运的话，新预算看起来"一分钱都没花"，那是超预算的经典成因。
        """
        tracker = BudgetTracker(CognitiveBudget.for_depth(CognitiveDepth.D4))
        tracker.spend_model_call(task_name="a")
        tracker.spend_model_call(task_name="b")

        rebased = tracker.rebase(CognitiveBudget.for_depth(CognitiveDepth.D2))
        assert rebased.model_calls_used == 2
        assert rebased.budget.max_model_calls == 10

    def test_negative_calls_rejected(self) -> None:
        tracker = BudgetTracker(CognitiveBudget())
        with pytest.raises(ValueError, match="不得为负"):
            tracker.can_afford(-1)

    def test_metacognitive_loop_limit(self) -> None:
        tracker = BudgetTracker(CognitiveBudget(max_metacognitive_loops=1))
        tracker.start_metacognitive_loop()
        with pytest.raises(BudgetExhaustedError) as excinfo:
            tracker.start_metacognitive_loop()
        assert excinfo.value.budget_name == "max_metacognitive_loops"

    def test_snapshot(self) -> None:
        tracker = BudgetTracker(CognitiveBudget(max_model_calls=5, max_metacognitive_loops=2))
        tracker.spend_model_call(task_name="a")
        tracker.start_metacognitive_loop()
        snapshot = tracker.snapshot()
        assert snapshot.remaining_model_calls == 4
        assert snapshot.remaining_metacognitive_loops == 1

    def test_mandatory_tail_is_two(self) -> None:
        """尾部保留语义在文档里写死了，改它必须是有意的。"""
        assert MANDATORY_TAIL_CALLS == 2


# ---------------------------------------------------------------------------
# 重复度检测
# ---------------------------------------------------------------------------


class TestRepetitionDetection:
    def test_identical_text_scores_one(self) -> None:
        text = "这个结论目前只能作为暂定看法"
        assert repeated_claim_score(text, text) == pytest.approx(1.0)

    def test_first_round_is_not_repetition(self) -> None:
        """🔴 没有"上一轮"就谈不上重复。

        把第一轮算成 1.0 会让每一次元认知复核都误判为反刍——
        系统会立刻停止思考，而且停止理由还是错的。
        """
        assert repeated_claim_score(None, "任何文本") == 0.0

    def test_different_text_scores_low(self) -> None:
        score = repeated_claim_score("今天天气不错", "量子力学的基本假设是什么")
        assert score < DEFAULT_REPETITION_THRESHOLD

    def test_almost_identical_text_is_detected(self) -> None:
        """逐字相同（只差标点与空白）会被判为重复——这正是反刍的典型形态。"""
        left = "目前没有足够依据判断对方的真实意图"
        right = "目前没有足够依据判断对方的真实意图。"
        assert repeated_claim_score(left, right) >= DEFAULT_REPETITION_THRESHOLD

    def test_paraphrase_is_not_detected(self) -> None:
        """⚠️ **已知局限**：改写过的同一论点**不会**被字符相似度判为重复。

        实测插入一个词就会把相似度从 1.0 拉到约 0.55，远低于 0.85 的阈值。
        这是因为 V0.1 用的是词面相似度而不是语义相似度（向量在阶段 5 才有）。

        这个局限是**刻意保守**的：漏判的代价是继续多想一轮，
        而循环上限与预算仍然兜得住；误判的代价是系统提前放弃分析，
        而且停止理由还是错的。两者不对称，因此宁可漏判。

        （登记于 risks.md R32）
        """
        left = "目前没有足够依据判断对方的真实意图"
        right = "目前没有足够依据去判断对方真实的意图"
        assert repeated_claim_score(left, right) < DEFAULT_REPETITION_THRESHOLD

    def test_whitespace_is_ignored(self) -> None:
        assert repeated_claim_score("abc def", "abcdef") == pytest.approx(1.0)

    def test_empty_inputs(self) -> None:
        assert jaccard(frozenset(), frozenset()) == 1.0
        assert jaccard(frozenset({"a"}), frozenset()) == 0.0
        assert normalized_ngrams("") == frozenset()
        assert normalized_ngrams("x") == frozenset({"x"})

    def test_signature_for_is_a_set(self) -> None:
        assert signature_for(["a", "a", "b"]) == frozenset({"a", "b"})


# ---------------------------------------------------------------------------
# 置信度
# ---------------------------------------------------------------------------


class TestConfidence:
    def test_no_evidence_caps_at_low(self) -> None:
        ceiling = derive_ceiling(
            evidence_count=0,
            independent_source_count=0,
            has_high_trust_conflict=False,
            unresolved_unknown_count=0,
        )
        assert ceiling is ConfidenceBand.LOW

    def test_single_source_caps_at_moderate(self) -> None:
        ceiling = derive_ceiling(
            evidence_count=3,
            independent_source_count=1,
            has_high_trust_conflict=False,
            unresolved_unknown_count=0,
        )
        assert ceiling is ConfidenceBand.MODERATE

    def test_independent_sources_allow_high(self) -> None:
        ceiling = derive_ceiling(
            evidence_count=3,
            independent_source_count=3,
            has_high_trust_conflict=False,
            unresolved_unknown_count=0,
        )
        assert ceiling is ConfidenceBand.HIGH

    def test_very_high_is_unreachable(self) -> None:
        """🔴 V0.1 中**没有任何路径**能自动达到 VERY_HIGH。

        不变量 4：用户赞同不能把核验状态改为 VERIFIED——
        既然没有东西能被自动确认，也就不该有结论被自动"完全确定"。
        """
        ceiling = derive_ceiling(
            evidence_count=100,
            independent_source_count=100,
            has_high_trust_conflict=False,
            unresolved_unknown_count=0,
        )
        assert ceiling is not ConfidenceBand.VERY_HIGH

    def test_high_trust_conflict_caps_at_low(self) -> None:
        ceiling = derive_ceiling(
            evidence_count=4,
            independent_source_count=4,
            has_high_trust_conflict=True,
            unresolved_unknown_count=0,
        )
        assert ceiling is ConfidenceBand.LOW

    def test_conflict_plus_unknowns_caps_at_very_low(self) -> None:
        ceiling = derive_ceiling(
            evidence_count=2,
            independent_source_count=2,
            has_high_trust_conflict=True,
            unresolved_unknown_count=1,
        )
        assert ceiling is ConfidenceBand.VERY_LOW

    def test_clamp_only_downgrades(self) -> None:
        assert clamp(ConfidenceBand.VERY_HIGH, ConfidenceBand.LOW) is ConfidenceBand.LOW
        assert clamp(ConfidenceBand.LOW, ConfidenceBand.HIGH) is ConfidenceBand.LOW

    def test_lower_band(self) -> None:
        assert lower_band(ConfidenceBand.HIGH, ConfidenceBand.MODERATE) is (ConfidenceBand.MODERATE)
        assert ConfidenceBand.VERY_LOW.rank < ConfidenceBand.VERY_HIGH.rank
