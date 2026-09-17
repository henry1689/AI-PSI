"""回答规划与渲染的一致性（任务书 §9.12，不变量 7）。

🔴 **不变量 7：最终回答不能比内部判断更确定。**

它由两道防线共同保证：

1. **结构性** —— 语气、长度、能否使用确定表述，全部由 Planner 从
   Judgment 推出，Renderer 只是"照做"；
2. **词面** —— 渲染完成后再扫一遍文本，发现无保留表述而判断不允许时直接判违规。

第 2 道防线**刻意从简**，本文件的用例同时钉死它的行为与它的局限。
"""

from __future__ import annotations

import pytest

from ai_psi.cognition.response_planner import MAX_ALTERNATIVES_SHOWN, ResponsePlanner
from ai_psi.cognition.response_renderer import (
    STRONG_ASSERTION_MARKERS,
    assert_render_consistency,
    detect_strong_assertion_markers,
)
from ai_psi.domain.enums import (
    CognitiveDepth,
    ConfidenceBand,
    EpistemicAction,
    HypothesisCategory,
    HypothesisStatus,
)
from ai_psi.domain.exceptions import ConstitutionViolationError
from ai_psi.prompts.schemas import ResponsePlan

pytestmark = pytest.mark.unit


def _plan(make_judgment, make_inquiry, **judgment_overrides) -> ResponsePlan:
    judgment = make_judgment(**judgment_overrides)
    inquiry = make_inquiry()
    return ResponsePlanner().plan(judgment=judgment, inquiry=inquiry)


class TestPlannerTone:
    def test_unreserved_conclusion_gives_assertive_tone(self, make_judgment, make_inquiry) -> None:
        plan = _plan(
            make_judgment,
            make_inquiry,
            unresolved_unknowns=[],
            recommended_epistemic_action=EpistemicAction.ANSWER,
            confidence_band=ConfidenceBand.HIGH,
        )
        assert plan.tone == "assertive"
        assert plan.allows_strong_conclusion is True

    def test_low_confidence_gives_cautious_tone(self, make_judgment, make_inquiry) -> None:
        plan = _plan(make_judgment, make_inquiry, confidence_band=ConfidenceBand.LOW)
        assert plan.tone == "cautious"
        assert plan.allows_strong_conclusion is False

    def test_very_low_confidence_gives_tentative_tone(self, make_judgment, make_inquiry) -> None:
        plan = _plan(make_judgment, make_inquiry, confidence_band=ConfidenceBand.VERY_LOW)
        assert plan.tone == "tentative"

    def test_unknowns_are_always_surfaced(self, make_judgment, make_inquiry) -> None:
        plan = _plan(
            make_judgment,
            make_inquiry,
            unresolved_unknowns=["朋友当时的处境"],
        )
        assert "朋友当时的处境" in plan.uncertainties_to_surface

    def test_context_conflicts_are_surfaced_even_without_unknowns(
        self, make_judgment, make_inquiry
    ) -> None:
        """🔴 "存在高可信冲突"必须说出来，**即使判断自己没把它记成未知**。

        模型完全可能一边看到两条互相矛盾的可靠来源、一边给出一个
        看起来没有未知的结论。没有这一步，"不强行合并冲突"就只是口号。
        """
        judgment = make_judgment(unresolved_unknowns=[])
        plan = ResponsePlanner().plan(
            judgment=judgment,
            inquiry=make_inquiry(),
            conflicts=["来源甲与来源乙给出了相反结论"],
        )
        assert any("冲突" in item for item in plan.uncertainties_to_surface)


class TestPlannerLength:
    @pytest.mark.parametrize(
        ("depth", "expected"),
        [
            (CognitiveDepth.D0, "short"),
            (CognitiveDepth.D1, "short"),
            (CognitiveDepth.D2, "medium"),
            (CognitiveDepth.D3, "medium"),
            (CognitiveDepth.D4, "long"),
        ],
    )
    def test_length_follows_depth(
        self, make_judgment, make_inquiry, depth: CognitiveDepth, expected: str
    ) -> None:
        plan = ResponsePlanner().plan(judgment=make_judgment(), inquiry=make_inquiry(), depth=depth)
        assert plan.length_hint == expected
        assert plan.depth_hint is depth


class TestCandidateVisibility:
    def test_intentional_unsupported_candidates_are_withheld(
        self, make_judgment, make_inquiry, make_hypothesis
    ) -> None:
        """🔴 无证据的第三方心理推断不向用户呈现（§9.1、场景 B）。"""
        judgment = make_judgment()
        plan = ResponsePlanner().plan(
            judgment=judgment,
            inquiry=make_inquiry(),
            depth=CognitiveDepth.D2,
            hypotheses=[
                make_hypothesis(
                    statement="朋友其实很讨厌用户",
                    category=HypothesisCategory.INTENTIONAL,
                    status=HypothesisStatus.UNRESOLVED,
                ),
                make_hypothesis(
                    statement="对方当时正在忙",
                    category=HypothesisCategory.NON_AGENTIC,
                    status=HypothesisStatus.UNRESOLVED,
                ),
            ],
        )
        assert "朋友其实很讨厌用户" in plan.withheld_candidates
        assert "对方当时正在忙" in plan.alternative_explanations_to_show

    def test_alternative_count_is_capped(
        self, make_judgment, make_inquiry, make_hypothesis
    ) -> None:
        judgment = make_judgment()
        plan = ResponsePlanner().plan(
            judgment=judgment,
            inquiry=make_inquiry(),
            depth=CognitiveDepth.D2,
            hypotheses=[
                make_hypothesis(
                    statement=f"解释 {index}",
                    category=HypothesisCategory.NON_AGENTIC,
                )
                for index in range(10)
            ],
        )
        assert len(plan.alternative_explanations_to_show) == MAX_ALTERNATIVES_SHOWN

    def test_d0_does_not_show_alternatives(
        self, make_judgment, make_inquiry, make_hypothesis
    ) -> None:
        """D0 不展示多假设——那正是"不产生多个无意义假设"（场景 A）。"""
        judgment = make_judgment()
        plan = ResponsePlanner().plan(
            judgment=judgment,
            inquiry=make_inquiry(),
            depth=CognitiveDepth.D0,
            hypotheses=[
                make_hypothesis(
                    statement="某个替代解释",
                    category=HypothesisCategory.NON_AGENTIC,
                )
            ],
        )
        assert plan.alternative_explanations_to_show == []


class TestClarification:
    def test_request_evidence_sets_clarification(self, make_judgment, make_inquiry) -> None:
        plan = _plan(
            make_judgment,
            make_inquiry,
            recommended_epistemic_action=EpistemicAction.REQUEST_EVIDENCE,
            unresolved_unknowns=["缺少材料"],
        )
        assert plan.needs_clarification is True

    def test_clarification_question_uses_ambiguous_concept(
        self, make_judgment, make_inquiry
    ) -> None:
        judgment = make_judgment(recommended_epistemic_action=EpistemicAction.REQUEST_EVIDENCE)
        plan = ResponsePlanner().plan(
            judgment=judgment,
            inquiry=make_inquiry(ambiguous_concepts=["适应"]),
        )
        assert plan.clarification_question is not None
        assert "适应" in plan.clarification_question

    def test_no_clarification_question_without_ambiguous_concept(
        self, make_judgment, make_inquiry
    ) -> None:
        """没有歧义概念时的"你想问什么"只是把问题推回给用户，不是澄清。"""
        judgment = make_judgment(recommended_epistemic_action=EpistemicAction.REQUEST_EVIDENCE)
        plan = ResponsePlanner().plan(judgment=judgment, inquiry=make_inquiry())
        assert plan.needs_clarification is True
        assert plan.clarification_question is None


class TestStrongMarkerDetection:
    def test_detects_unreserved_claims(self) -> None:
        assert detect_strong_assertion_markers("这毫无疑问是错的")
        assert detect_strong_assertion_markers("他必然是故意的")
        assert detect_strong_assertion_markers("This is definitely wrong")

    def test_ignores_negated_forms(self) -> None:
        """🔴 否定形式是**弱化**表述，不该被判成"过度确定"。

        误判的代价是整个回答作废——比漏掉一个真阳性严重得多。
        """
        assert detect_strong_assertion_markers("这不一定是真的") == ()
        assert detect_strong_assertion_markers("这一点并非毫无疑问") == ()
        assert detect_strong_assertion_markers("not definitely true") == ()

    def test_ignores_hedging_phrases_that_share_roots(self) -> None:
        """``一定程度上``、``未必`` 都是弱化表述，词根匹配会把它们误判。"""
        assert detect_strong_assertion_markers("在一定程度上可以这样说") == ()
        assert detect_strong_assertion_markers("情况未必如此") == ()

    def test_plain_text_has_no_markers(self) -> None:
        assert detect_strong_assertion_markers("目前没有足够依据判断对方意图。") == ()

    def test_all_markers_are_multi_character(self) -> None:
        """单字词根太容易误伤——标记集合必须只用多字短语。"""
        for marker in STRONG_ASSERTION_MARKERS:
            assert len(marker) >= 3, marker


class TestRenderConsistency:
    def test_strong_text_with_cautious_judgment_is_a_violation(self, make_judgment) -> None:
        judgment = make_judgment(recommended_epistemic_action=EpistemicAction.ANSWER_WITH_CAVEAT)
        with pytest.raises(ConstitutionViolationError) as excinfo:
            assert_render_consistency(judgment=judgment, text="这毫无疑问是对的")
        assert excinfo.value.invariant_id == "I07"

    def test_strong_text_with_strong_judgment_is_fine(self, make_judgment) -> None:
        judgment = make_judgment(
            unresolved_unknowns=[],
            recommended_epistemic_action=EpistemicAction.ANSWER,
        )
        assert_render_consistency(judgment=judgment, text="这毫无疑问是对的")

    def test_cautious_text_always_passes(self, make_judgment) -> None:
        """无论内部判断是什么，"带限定"的表述永远合规——它只会比判断更弱。"""
        for action in EpistemicAction:
            judgment = make_judgment(
                unresolved_unknowns=[] if action is EpistemicAction.ANSWER else ["未知"],
                recommended_epistemic_action=action,
            )
            # 不抛异常即为通过
            assert_render_consistency(judgment=judgment, text="目前只能给出暂定看法。")

    def test_emptiness_invariant_holds_for_every_action(self, make_judgment) -> None:
        """不变量 7 在**所有**认知动作下都成立——逐一验证，不靠抽样。"""
        for action in EpistemicAction:
            judgment = make_judgment(
                unresolved_unknowns=[] if action is EpistemicAction.ANSWER else ["未知"],
                recommended_epistemic_action=action,
            )
            if judgment.allows_strong_conclusion:
                assert_render_consistency(judgment=judgment, text="这绝对是正确的")
            else:
                with pytest.raises(ConstitutionViolationError):
                    assert_render_consistency(judgment=judgment, text="这绝对是正确的")
