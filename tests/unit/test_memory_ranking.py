"""记忆检索的公共部件与综合排序（任务书 §9.3 的记忆侧）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ai_psi.domain.enums import MemoryStatus, MemoryType
from ai_psi.domain.memories import Memory
from ai_psi.memory.ranking import (
    DEFAULT_RANKING_WEIGHTS,
    ScoredMemory,
    conflicting_ids,
    rank_candidates,
)
from ai_psi.memory.retrieval import (
    MIN_RECALL_SIZE,
    cosine_similarity,
    is_zero_vector,
    lexical_overlap,
    recall_size,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _memory(content: str, *, days_ago: int = 0, **overrides: object) -> Memory:
    payload: dict[str, object] = {
        "created_by": "test",
        "memory_type": MemoryType.USER_PREFERENCE,
        "content": content,
        "valid_from": NOW - timedelta(days=days_ago),
        "status": MemoryStatus.ACTIVE,
    }
    payload.update(overrides)
    return Memory(**payload)  # type: ignore[arg-type]


class TestCosineSimilarity:
    def test_identical_vectors(self) -> None:
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors(self) -> None:
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_dimension_mismatch_raises(self) -> None:
        """🔴 维度不同就是不同的向量空间，**比较它们没有意义**。

        截断到较短的那个会让它看起来像一次正常的比较——
        得到的是一个数字，而不是一个错误。
        """
        with pytest.raises(ValueError, match="维度不一致"):
            cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])

    def test_zero_vector_yields_zero_not_nan(self) -> None:
        """零向量没有方向，返回 0（无关）而不是 NaN。

        NaN 会污染整条排序链：比较结果全是 False，
        `sorted` 的顺序退化成"输入顺序"，而输入顺序来自数据库，
        看起来正常、实际不确定。
        """
        result = cosine_similarity([0.0, 0.0], [1.0, 1.0])
        assert result == 0.0
        assert result == result  # 不是 NaN


class TestIsZeroVector:
    def test_all_zero(self) -> None:
        assert is_zero_vector([0.0, 0.0])

    def test_any_non_zero(self) -> None:
        assert not is_zero_vector([0.0, 1e-12])


class TestRecallSize:
    def test_scales_with_limit(self) -> None:
        assert recall_size(20) == 80

    def test_has_a_floor(self) -> None:
        """单条检索时也要召回到足够多的候选，否则排序阶段没有调整余地。"""
        assert recall_size(1) == MIN_RECALL_SIZE

    def test_negative_limit_is_safe(self) -> None:
        assert recall_size(-5) == MIN_RECALL_SIZE


class TestLexicalOverlap:
    def test_identical_is_one(self) -> None:
        assert lexical_overlap("用户偏好简洁回答", "用户偏好简洁回答") == pytest.approx(1.0)

    def test_unrelated_is_low(self) -> None:
        assert lexical_overlap("简洁回答", "今天股市大跌") < 0.2

    def test_partial_overlap_is_between(self) -> None:
        score = lexical_overlap("简洁回答", "用户偏好简洁回答")
        assert 0.0 < score < 1.0


class TestRankCandidates:
    def test_higher_similarity_ranks_first(self) -> None:
        close = _memory("用户偏好简洁回答")
        far = _memory("用户喜欢详细回答")
        ranked = rank_candidates([(far, 0.2), (close, 0.9)], query="简洁回答", now=NOW, limit=10)
        assert [item.memory.content for item in ranked] == [
            "用户偏好简洁回答",
            "用户喜欢详细回答",
        ]

    def test_recent_memory_wins_when_relevance_ties(self) -> None:
        old = _memory("用户偏好简洁回答", days_ago=2000)
        recent = _memory("用户偏好简洁回答", days_ago=1)
        ranked = rank_candidates([(old, 0.5), (recent, 0.5)], query="简洁回答", now=NOW, limit=10)
        assert ranked[0].memory.id == recent.id

    def test_recency_is_capped_at_one_for_future_valid_from(self) -> None:
        """未来的 ``valid_from`` 得到满分，**不是大于 1**。

        时钟偏移或预置生效时间都可能造出未来的时间戳；
        得分溢出 ``[0, 1]`` 会让权重的含义失效。
        """
        future = _memory("用户偏好简洁回答", days_ago=-30)
        ranked = rank_candidates([(future, 0.5)], query="简洁回答", now=NOW, limit=10)
        assert ranked[0].recency == 1.0

    def test_ordering_is_deterministic_on_ties(self) -> None:
        """🔴 同分时按 ``created_at`` 降序、再按 id 升序。

        少了最后这一层，两条完全同分的记忆在不同运行里会给出不同的顺序，
        而"检索结果不可复现"会让任何一次排查都无从下手。
        """
        first = _memory("用户偏好简洁回答", id=uuid4())
        second = _memory("用户偏好简洁回答", id=uuid4())
        candidates = [(first, 0.5), (second, 0.5)]
        order_a = [
            item.memory.id
            for item in rank_candidates(candidates, query="简洁回答", now=NOW, limit=10)
        ]
        order_b = [
            item.memory.id
            for item in rank_candidates(
                list(reversed(candidates)), query="简洁回答", now=NOW, limit=10
            )
        ]
        assert order_a == order_b

    def test_conflicts_are_not_penalised(self) -> None:
        """🔴 存在显式冲突的记忆**不降权**。

        冲突本身是有价值的信息（任务书 §5.11）："系统同时记着两种说法"
        比"只记得其中一种"更接近事实。降权会让它在检索里消失，
        等于把冲突掩盖掉。
        """
        other = uuid4()
        conflicted = _memory("用户喜欢详细回答", contradicts_ids=[other])
        plain = _memory("用户喜欢详细回答")
        ranked = rank_candidates(
            [(conflicted, 0.5), (plain, 0.5)], query="详细回答", now=NOW, limit=10
        )
        assert ranked[0].score == pytest.approx(ranked[1].score)

    def test_limit_is_respected(self) -> None:
        candidates = [(_memory(f"偏好 {index}"), 0.5 - index / 100) for index in range(10)]
        assert len(rank_candidates(candidates, query="偏好", now=NOW, limit=3)) == 3

    def test_zero_limit_returns_nothing(self) -> None:
        assert rank_candidates([(_memory("偏好"), 1.0)], query="偏好", now=NOW, limit=0) == []

    def test_empty_candidates(self) -> None:
        assert rank_candidates([], query="偏好", now=NOW, limit=5) == []

    def test_scores_stay_within_unit_interval(self) -> None:
        """得分必须落在 ``[0, 1]``，否则权重失去意义。"""
        ranked = rank_candidates(
            [(_memory("偏好"), -1.0), (_memory("偏好"), 1.0)],
            query="偏好",
            now=NOW,
            limit=10,
        )
        assert all(0.0 <= item.score <= 1.0 for item in ranked)
        assert DEFAULT_RANKING_WEIGHTS.relevance + DEFAULT_RANKING_WEIGHTS.recency <= 1.0


class TestConflictAwareIds:
    def test_flags_mutually_conflicting_members(self) -> None:
        first = _memory("A")
        second = _memory("B", contradicts_ids=[first.id])
        flagged = conflicting_ids([first, second])
        assert flagged == {first.id, second.id}

    def test_ignores_conflicts_outside_the_result_set(self) -> None:
        """与结果集之外的记忆冲突不算——用户看不到那一条，标注只会让人困惑。"""
        lonely = _memory("A", contradicts_ids=[uuid4()])
        assert conflicting_ids([lonely]) == frozenset()

    def test_empty(self) -> None:
        assert conflicting_ids([]) == frozenset()


class TestScoredMemoryShape:
    def test_exposes_score_breakdown(self) -> None:
        """明细必须逐项可见：结果被质疑时要能回答"为什么它排在前面"。"""
        ranked = rank_candidates([(_memory("偏好"), 0.5)], query="偏好", now=NOW, limit=1)
        item: ScoredMemory = ranked[0]
        assert item.similarity == pytest.approx((0.5 + 1) / 2)
        assert 0.0 <= item.lexical <= 1.0
        assert item.score == pytest.approx(
            DEFAULT_RANKING_WEIGHTS.relevance * (0.5 * item.similarity + 0.5 * item.lexical)
            + DEFAULT_RANKING_WEIGHTS.recency * item.recency
        )
