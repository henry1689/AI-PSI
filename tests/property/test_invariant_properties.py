"""认知不变量的属性测试（任务书 §15.2）。

任务书 §15.2 要求用 Hypothesis 验证的关键命题：

* 任意 Proposal 不会自动进入 ACTIVE；
* Hypothesis 永远不会绕过验证直接成为确认事实；
* 回合循环次数永远不超过预算；
* 任意模型非法结构不会直接写入数据库（阶段 3 补）。

本文件覆盖前三项在**纯领域层**可验证的部分。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from ai_psi.cognition.constitution import (
    assert_no_automatic_promotion,
    assert_user_model_not_confirmed,
)
from ai_psi.domain.cognitive_rounds import CognitiveBudget, CognitiveRound
from ai_psi.domain.enums import (
    BeliefStatus,
    ConfidenceBand,
    EpistemicAction,
    ErrorType,
    HypothesisCategory,
    HypothesisStatus,
    MemoryStatus,
    MemoryType,
    OrdinalLevel,
    ProposalStatus,
    UserModelStatus,
)
from ai_psi.domain.hypotheses import Hypothesis
from ai_psi.domain.improvement_proposals import ImprovementProposal
from ai_psi.domain.judgments import Judgment
from ai_psi.domain.memories import Memory
from ai_psi.domain.user_models import UserModel
from tests.helpers import construct

pytestmark = [pytest.mark.property, pytest.mark.unit]

#: 固定时间点——领域对象要求 tz-aware UTC 时间
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

proposal_statuses = st.sampled_from(list(ProposalStatus))
hypothesis_statuses = st.sampled_from(list(HypothesisStatus))
hypothesis_categories = st.sampled_from(list(HypothesisCategory))
memory_statuses = st.sampled_from(list(MemoryStatus))
user_model_statuses = st.sampled_from(list(UserModelStatus))
belief_statuses = st.sampled_from(list(BeliefStatus))
epistemic_actions = st.sampled_from(list(EpistemicAction))
confidence_bands = st.sampled_from(list(ConfidenceBand))
ordinal_levels = st.sampled_from(list(OrdinalLevel))


class TestProposalNeverActivates:
    """🔴 不变量 11：任意状态下提案都不得自动生效。"""

    @given(status=proposal_statuses)
    @settings(max_examples=100)
    def test_can_become_active_is_always_false(self, status) -> None:
        proposal = ImprovementProposal(
            created_by="prop_test",
            target_component="prompt:x",
            observed_problem="p",
            error_class=ErrorType.REASONING_ERROR,
            proposed_change="c",
            expected_benefit="b",
            status=status,
        )
        assert proposal.can_become_active is False

    @given(status=proposal_statuses)
    @settings(max_examples=100)
    def test_no_status_is_rejected_by_the_constitution_guard(self, status) -> None:
        assert_no_automatic_promotion(status)

    @given(status=st.text(min_size=1, max_size=20))
    @settings(max_examples=200)
    def test_no_arbitrary_status_string_can_bypass_the_enum(self, status: str) -> None:
        """任何伪造的状态字符串都无法通过枚举校验。"""
        try:
            proposal = construct(
                ImprovementProposal,
                created_by="prop_test",
                target_component="t",
                observed_problem="p",
                error_class=ErrorType.REASONING_ERROR,
                proposed_change="c",
                expected_benefit="b",
                status=status,
            )
        except ValidationError:
            return
        # 若字符串恰好是合法枚举值，则它必定不是"已生效"状态
        assert proposal.status in set(ProposalStatus)
        assert proposal.can_become_active is False


class TestHypothesisNeverBecomesFact:
    """🔴 不变量 1：假设永远不会绕过验证直接成为确认事实。"""

    @given(status=hypothesis_statuses, category=hypothesis_categories)
    @settings(max_examples=300)
    def test_never_writable_as_fact(self, status, category) -> None:
        h = Hypothesis(
            created_by="hyp_test",
            inquiry_id=uuid4(),
            statement="s",
            category=category,
            falsification_conditions=["c"],
            status=status,
        )
        assert h.can_be_written_as_fact() is False
        assert h.is_factual_claim is False

    @given(status=hypothesis_statuses)
    @settings(max_examples=100)
    def test_missing_falsification_is_always_rejected(self, status) -> None:
        """不可反驳的命题在任何状态下都不能被构造出来。"""
        with pytest.raises(ValidationError):
            Hypothesis(
                created_by="hyp_test",
                inquiry_id=uuid4(),
                statement="s",
                falsification_conditions=[],
                status=status,
            )

    @given(categories=st.lists(hypothesis_categories, min_size=1, max_size=6))
    @settings(max_examples=200)
    def test_has_non_agentic_matches_the_categories(self, categories) -> None:
        hypotheses = [
            Hypothesis(
                created_by="hyp_test",
                inquiry_id=uuid4(),
                statement=f"s{i}",
                category=category,
                falsification_conditions=["c"],
            )
            for i, category in enumerate(categories)
        ]
        expected = any(c.is_non_agentic for c in categories)
        assert Hypothesis.has_non_agentic_explanation(hypotheses) is expected


class TestUserModelNeverConfirmed:
    """🔴 不变量 13。"""

    @given(status=user_model_statuses)
    @settings(max_examples=50)
    def test_is_confirmable_always_false(self, status) -> None:
        model = UserModel(
            created_by="um_test",
            user_id=uuid4(),
            attribute="a",
            value="v",
            evidence_ids=[uuid4()],
            status=status,
            valid_from=EPOCH,
        )
        assert model.is_confirmable is False
        assert_user_model_not_confirmed(model)


class TestMemoryScopeAndStatus:
    """🔴 不变量 6 + 14：检索必须同时满足作用域与有效状态。"""

    @given(status=memory_statuses)
    @settings(max_examples=100)
    def test_is_default_retrievable_only_for_active_and_disputed(self, status) -> None:
        memory = Memory(
            created_by="mem_test",
            memory_type=MemoryType.EPISODIC,
            content="c",
            valid_from=EPOCH,
            status=status,
        )
        expected = status in {MemoryStatus.ACTIVE, MemoryStatus.DISPUTED}
        assert memory.is_default_retrievable is expected

    @given(status=memory_statuses)
    @settings(max_examples=100)
    def test_superseded_deleted_expired_are_never_retrievable(self, status) -> None:
        memory = Memory(
            created_by="mem_test",
            memory_type=MemoryType.EPISODIC,
            content="c",
            valid_from=EPOCH,
            status=status,
        )
        if status in {MemoryStatus.SUPERSEDED, MemoryStatus.DELETED, MemoryStatus.EXPIRED}:
            assert not memory.is_default_retrievable

    @given(user_a=st.uuids(), user_b=st.uuids())
    @settings(max_examples=200)
    def test_cross_user_access_is_always_denied(self, user_a, user_b) -> None:
        """🔴 任意用户 A 的私有记忆不会被视为属于用户 B。"""
        if user_a == user_b:
            return
        memory = Memory(
            created_by="mem_test",
            memory_type=MemoryType.EPISODIC,
            content="private",
            user_id=user_a,
            valid_from=EPOCH,
            status=MemoryStatus.ACTIVE,
        )
        assert memory.belongs_to(user_a)
        assert not memory.belongs_to(user_b)


class TestBudgetIsNeverExceeded:
    """🔴 超预算认知回合率必须为 0。"""

    @given(
        used=st.integers(min_value=0, max_value=100),
        limit=st.integers(min_value=1, max_value=50),
    )
    @settings(max_examples=400)
    def test_round_rejects_usage_over_budget(self, used: int, limit: int) -> None:
        budget = CognitiveBudget(max_model_calls=limit)
        if used > limit:
            with pytest.raises(ValidationError):
                CognitiveRound(
                    created_by="round_test",
                    budget=budget,
                    model_calls_used=used,
                )
        else:
            round_ = CognitiveRound(
                created_by="round_test",
                budget=budget,
                model_calls_used=used,
            )
            assert round_.model_calls_used <= round_.budget.max_model_calls
            assert round_.remaining_model_calls == limit - used

    @given(used=st.integers(min_value=0, max_value=200))
    @settings(max_examples=200)
    def test_construction_never_yields_over_budget(self, used: int) -> None:
        """任何构造成功的回合都不可能是超预算的。"""
        try:
            round_ = CognitiveRound(created_by="round_test", model_calls_used=used)
        except ValidationError:
            return
        assert round_.model_calls_used <= round_.budget.max_model_calls


class TestJudgmentStrengthConsistency:
    """🔴 不变量 3 + 7 的联合属性。"""

    @given(
        action=epistemic_actions,
        unknowns=st.lists(st.text(min_size=1, max_size=10), max_size=3),
    )
    @settings(max_examples=300)
    def test_strong_conclusion_implies_no_unknowns(self, action, unknowns) -> None:
        try:
            judgment = Judgment(
                created_by="judg_test",
                inquiry_id=uuid4(),
                conclusion="c",
                rationale_summary=["r"],
                confidence_basis=["b"],
                recommended_epistemic_action=action,
                unresolved_unknowns=unknowns,
            )
        except ValidationError:
            # 被拒绝的唯一合法原因是：要求强结论却存在未解决未知
            assert action.allows_strong_conclusion and unknowns
            return
        if judgment.allows_strong_conclusion:
            assert not judgment.unresolved_unknowns

    @given(band=confidence_bands, action=epistemic_actions)
    @settings(max_examples=200)
    def test_confidence_band_never_grants_strong_conclusion_by_itself(self, band, action) -> None:
        """高置信度本身**不**赋予强结论的许可——许可只来自 EpistemicAction。"""
        judgment = Judgment(
            created_by="judg_test",
            inquiry_id=uuid4(),
            conclusion="c",
            rationale_summary=["r"],
            confidence_basis=["b"],
            confidence_band=band,
            recommended_epistemic_action=action,
        )
        assert judgment.allows_strong_conclusion is action.allows_strong_conclusion


class TestOrdinalMonotonicity:
    """有序量纲的单调性——置信度分档不得乱序。"""

    @given(a=ordinal_levels, b=ordinal_levels)
    @settings(max_examples=200)
    def test_at_least_is_consistent_with_rank(self, a, b) -> None:
        assert a.at_least(b) is (a.rank >= b.rank)

    @given(a=confidence_bands, b=confidence_bands)
    @settings(max_examples=200)
    def test_confidence_at_least_is_antisymmetric(self, a, b) -> None:
        if a.at_least(b) and b.at_least(a):
            assert a is b

    @given(a=ordinal_levels, b=ordinal_levels, c=ordinal_levels)
    @settings(max_examples=200)
    def test_rank_is_transitive(self, a, b, c) -> None:
        if a.at_least(b) and b.at_least(c):
            assert a.at_least(c)
