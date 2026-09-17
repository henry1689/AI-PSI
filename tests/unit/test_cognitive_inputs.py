"""上下文构建、认知状态分析与假设评估。

这三个模块合在一起回答了"进入分析阶段之前，系统**知道什么、不知道什么**"。

🔴 它们的共同主题是**不让不该被丢掉的东西被丢掉**：
冲突材料、已失效材料、以及"当前材料不足以支持任何意图判断"这一可能性。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from ai_psi.cognition.context_builder import (
    ContextBuilder,
    ContextItemKind,
    estimate_context_tokens,
)
from ai_psi.cognition.epistemic_analyzer import EpistemicAnalysis, EpistemicAnalyzer
from ai_psi.cognition.hypothesis_generator import HypothesisEvaluator
from ai_psi.domain.enums import (
    EpistemicClassification,
    HypothesisCategory,
    HypothesisStatus,
    MemoryStatus,
    MemoryType,
    OrdinalLevel,
    SourceType,
    TrustLevel,
    VerificationStatus,
)
from ai_psi.domain.evidence import Evidence
from ai_psi.domain.memories import Memory
from ai_psi.domain.observations import Observation

pytestmark = pytest.mark.unit

QUESTION = "朋友的简短回复有哪些可能的解释"


def _observation(content: str = "朋友今天只回复了一个「嗯」") -> Observation:
    return Observation(
        created_by="test",
        source_type=SourceType.USER_MESSAGE,
        source_id="msg-1",
        content=content,
        trust_level=TrustLevel.HIGH,
    )


def _memory(
    content: str,
    *,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    user_id: UUID | None = None,
) -> Memory:
    return Memory(
        created_by="test",
        user_id=user_id,
        memory_type=MemoryType.USER_PREFERENCE,
        content=content,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        status=status,
    )


def _evidence(
    summary: str,
    *,
    reliability: OrdinalLevel = OrdinalLevel.HIGH,
    supports: list[UUID] | None = None,
    opposes: list[UUID] | None = None,
    group: str | None = None,
) -> Evidence:
    return Evidence(
        created_by="test",
        source_name="来源",
        content_summary=summary,
        reliability=reliability,
        independence_group=group,
        supports_claim_ids=supports or [],
        opposes_claim_ids=opposes or [],
        verification_status=VerificationStatus.VERIFIED,
    )


class TestContextBuilder:
    def test_selects_relevant_items(self) -> None:
        builder = ContextBuilder(max_items=10, max_tokens=10_000)
        bundle = builder.build(
            question=QUESTION,
            observations=(_observation(),),
            memories=(_memory("朋友的简短回复可能只是因为当时在忙"),),
        )
        assert bundle.item_count >= 1
        assert any(item.kind is ContextItemKind.OBSERVATION for item in bundle.items)

    def test_respects_item_limit(self) -> None:
        builder = ContextBuilder(max_items=2, max_tokens=100_000)
        bundle = builder.build(
            question=QUESTION,
            memories=tuple(_memory(f"朋友的回复解释 {i}") for i in range(10)),
        )
        assert bundle.item_count <= 2
        assert bundle.dropped_count > 0, "被裁掉的条数必须被记录，不能静默截断"

    def test_respects_token_limit(self) -> None:
        builder = ContextBuilder(max_items=100, max_tokens=20)
        bundle = builder.build(
            question=QUESTION,
            memories=tuple(_memory("朋友的简短回复" * 20) for _ in range(5)),
        )
        assert bundle.estimated_tokens <= 20

    def test_superseded_memory_is_kept_and_flagged(self) -> None:
        """🔴 已失效材料**不参与裁剪**：它的价值在于说明当前依据的边界。"""
        builder = ContextBuilder(max_items=0, max_tokens=1)
        bundle = builder.build(
            question=QUESTION,
            memories=(_memory("用户以前喜欢详细回答", status=MemoryStatus.SUPERSEDED),),
        )
        assert bundle.item_count == 1
        assert bundle.superseded
        assert "已失效" in bundle.superseded[0]

    def test_conflicting_evidence_is_flagged(self) -> None:
        claim = uuid4()
        builder = ContextBuilder(max_items=10, max_tokens=10_000)
        bundle = builder.build(
            question=QUESTION,
            evidence=(
                _evidence("来源甲认为成立", supports=[claim]),
                _evidence("来源乙认为不成立", opposes=[claim]),
            ),
        )
        assert bundle.conflicting
        assert len(bundle.conflicting) == 2

    def test_high_trust_conflict_flag(self) -> None:
        claim = uuid4()
        builder = ContextBuilder(max_items=10, max_tokens=10_000)
        bundle = builder.build(
            question=QUESTION,
            evidence=(
                _evidence("甲", reliability=OrdinalLevel.HIGH, supports=[claim]),
                _evidence("乙", reliability=OrdinalLevel.HIGH, opposes=[claim]),
            ),
        )
        assert bundle.has_high_trust_conflict

    def test_low_trust_conflict_does_not_count(self) -> None:
        """低可信材料的冲突不足以压低结论强度。"""
        claim = uuid4()
        builder = ContextBuilder(max_items=10, max_tokens=10_000)
        bundle = builder.build(
            question=QUESTION,
            evidence=(
                _evidence("甲", reliability=OrdinalLevel.LOW, supports=[claim]),
                _evidence("乙", reliability=OrdinalLevel.LOW, opposes=[claim]),
            ),
        )
        assert not bundle.has_high_trust_conflict

    def test_independent_source_count_groups_same_origin(self) -> None:
        """🔴 同源证据只算一组（任务书 §5.6）。"""
        builder = ContextBuilder(max_items=10, max_tokens=10_000)
        bundle = builder.build(
            question=QUESTION,
            evidence=tuple(_evidence(f"转载 {i}", group="同一篇报道") for i in range(5)),
        )
        assert bundle.independent_source_count == 1

    def test_corrections_are_carried_through(self) -> None:
        builder = ContextBuilder(max_items=10, max_tokens=10_000)
        bundle = builder.build(question=QUESTION, corrections=("用户更正了偏好",))
        assert bundle.corrections == ("用户更正了偏好",)

    def test_summarize_for_prompt_ranks_by_relevance(self) -> None:
        builder = ContextBuilder(max_items=10, max_tokens=10_000)
        bundle = builder.build(
            question=QUESTION,
            memories=(
                _memory("朋友的简短回复有哪些可能的解释"),
                _memory("完全无关的话题：烘焙面包的温度"),
            ),
        )
        summaries = bundle.summarize_for_prompt()
        assert "简短回复" in summaries[0]

    def test_estimate_context_tokens(self) -> None:
        assert estimate_context_tokens(()) == 0
        assert estimate_context_tokens(("abcdefgh",)) == 2


class TestEpistemicAnalyzer:
    def _analyze(self, **kwargs: object) -> EpistemicAnalysis:
        builder = ContextBuilder(max_items=10, max_tokens=10_000)
        bundle = builder.build(question=QUESTION, **kwargs)  # type: ignore[arg-type]
        return EpistemicAnalyzer().analyze(
            bundle=bundle,
            key_unknowns=["朋友当时的处境"],
            inaccessible=["对方的私人日程"],
            out_of_capability=["判断对方是否在说谎"],
        )

    def test_observation_is_classified_as_observed(self) -> None:
        analysis = self._analyze(observations=(_observation(),))
        assert analysis.count(EpistemicClassification.OBSERVED) == 1

    def test_verified_high_trust_evidence_is_supported(self) -> None:
        analysis = self._analyze(evidence=(_evidence("高可信来源的说法"),))
        assert analysis.count(EpistemicClassification.SUPPORTED) == 1

    def test_unverified_evidence_is_tentative(self) -> None:
        evidence = Evidence(
            created_by="test",
            source_name="来源",
            content_summary="未核验的说法",
            reliability=OrdinalLevel.HIGH,
            verification_status=VerificationStatus.UNVERIFIED,
        )
        analysis = self._analyze(evidence=(evidence,))
        assert analysis.count(EpistemicClassification.TENTATIVE) == 1

    def test_superseded_memory_is_possibly_outdated(self) -> None:
        analysis = self._analyze(memories=(_memory("旧偏好", status=MemoryStatus.SUPERSEDED),))
        assert analysis.count(EpistemicClassification.POSSIBLY_OUTDATED) == 1

    def test_conflict_wins_over_supported(self) -> None:
        """🔴 判定顺序即优先级：冲突优先于"我看到了它"。

        顺序写反会让系统拿着互相矛盾的材料说"证据充分"。
        """
        claim = uuid4()
        analysis = self._analyze(
            evidence=(
                _evidence("甲", supports=[claim]),
                _evidence("乙", opposes=[claim]),
            )
        )
        assert analysis.count(EpistemicClassification.CONFLICTING) == 2
        assert analysis.count(EpistemicClassification.SUPPORTED) == 0

    def test_unknowns_are_always_registered(self) -> None:
        """🔴 不变量 9 的前提：未知项必须先被记下来，才谈得上"不被覆盖"。"""
        analysis = self._analyze()
        assert analysis.unknown_count == 1
        assert analysis.count(EpistemicClassification.INACCESSIBLE) == 1
        assert analysis.count(EpistemicClassification.OUT_OF_CAPABILITY) == 1

    def test_every_entry_explains_its_basis(self) -> None:
        """§9.4：输出不得只给置信度，必须说明依据类型。"""
        analysis = self._analyze(observations=(_observation(),))
        for entry in analysis.entries:
            assert entry.basis
            assert entry.subject

    def test_dropped_material_is_reported(self) -> None:
        builder = ContextBuilder(max_items=0, max_tokens=1)
        bundle = builder.build(question=QUESTION, memories=(_memory("朋友的回复"),))
        analysis = EpistemicAnalyzer().analyze(bundle=bundle)
        assert analysis.dropped_material_count == 1


class TestHypothesisEvaluator:
    def test_without_evidence_all_are_unresolved(self, make_hypothesis) -> None:
        result = HypothesisEvaluator().evaluate(
            hypotheses=[make_hypothesis()],
            evidence=[],
        )
        assert result.unresolved_count == 1
        assert result.hypotheses[0].status is HypothesisStatus.UNRESOLVED

    def test_supporting_evidence_yields_supported_not_fact(self, make_hypothesis) -> None:
        """🔴 不变量 1：``SUPPORTED`` 的语义是"当前证据支持"，不是"事实成立"。"""
        hypothesis = make_hypothesis()
        evidence = _evidence("支持", supports=[hypothesis.id])
        result = HypothesisEvaluator().evaluate(hypotheses=[hypothesis], evidence=[evidence])
        assert result.hypotheses[0].status is HypothesisStatus.SUPPORTED
        assert not result.hypotheses[0].can_be_written_as_fact()
        assert result.hypotheses[0].is_factual_claim is False

    def test_opposing_evidence_rejects(self, make_hypothesis) -> None:
        hypothesis = make_hypothesis()
        evidence = _evidence("反对", opposes=[hypothesis.id])
        result = HypothesisEvaluator().evaluate(hypotheses=[hypothesis], evidence=[evidence])
        assert result.hypotheses[0].status is HypothesisStatus.REJECTED

    def test_both_sides_leave_it_under_evaluation(self, make_hypothesis) -> None:
        hypothesis = make_hypothesis()
        result = HypothesisEvaluator().evaluate(
            hypotheses=[hypothesis],
            evidence=[
                _evidence("支持", supports=[hypothesis.id]),
                _evidence("反对", opposes=[hypothesis.id]),
            ],
        )
        assert result.hypotheses[0].status is HypothesisStatus.UNDER_EVALUATION

    def test_evidence_ids_are_recorded(self, make_hypothesis) -> None:
        hypothesis = make_hypothesis()
        supporting = _evidence("支持", supports=[hypothesis.id])
        result = HypothesisEvaluator().evaluate(hypotheses=[hypothesis], evidence=[supporting])
        assert result.hypotheses[0].supporting_evidence_ids == [supporting.id]

    def test_no_status_reaches_confirmed_fact(self, make_hypothesis) -> None:
        """把每一种证据组合都试一遍，没有任何一条通往"已确认事实"。"""
        hypothesis = make_hypothesis()
        combinations = [
            [],
            [_evidence("s", supports=[hypothesis.id])],
            [_evidence("o", opposes=[hypothesis.id])],
            [_evidence("s", supports=[hypothesis.id]), _evidence("o", opposes=[hypothesis.id])],
        ]
        for evidence in combinations:
            result = HypothesisEvaluator().evaluate(hypotheses=[hypothesis], evidence=evidence)
            assert result.hypotheses[0].status in {
                HypothesisStatus.UNRESOLVED,
                HypothesisStatus.SUPPORTED,
                HypothesisStatus.REJECTED,
                HypothesisStatus.UNDER_EVALUATION,
            }


class TestNonAgenticRequirement:
    """§9.6：高风险问题的候选假设中，至少一个必须是非人格化解释。"""

    def test_helper_detects_non_agentic_category(self, make_hypothesis) -> None:
        from ai_psi.domain.hypotheses import Hypothesis

        hypotheses = [
            make_hypothesis(category=HypothesisCategory.INTENTIONAL),
            make_hypothesis(category=HypothesisCategory.NON_AGENTIC),
        ]
        assert Hypothesis.has_non_agentic_explanation(hypotheses)

    def test_only_intentional_hypotheses_do_not_qualify(self, make_hypothesis) -> None:
        from ai_psi.domain.hypotheses import Hypothesis

        hypotheses = [make_hypothesis(category=HypothesisCategory.INTENTIONAL)]
        assert not Hypothesis.has_non_agentic_explanation(hypotheses)

    def test_mechanistic_and_contextual_count_as_non_agentic(self) -> None:
        for category in (
            HypothesisCategory.MECHANISTIC,
            HypothesisCategory.SYSTEMIC,
            HypothesisCategory.CONTEXTUAL,
            HypothesisCategory.NON_AGENTIC,
        ):
            assert category.is_non_agentic, category
        assert not HypothesisCategory.INTENTIONAL.is_non_agentic
        assert not HypothesisCategory.FACTUAL.is_non_agentic
