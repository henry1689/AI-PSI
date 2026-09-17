"""信念与判断的单元测试。

🔴 本文件覆盖两条最容易被忽略的不变量：

* **I02**：Belief 必须具有依据，或明确标记为暂定；
* **I03**：存在未解决未知时，不得输出无保留的确定结论。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from ai_psi.domain.enums import BeliefStatus, ConfidenceBand, EpistemicAction

pytestmark = pytest.mark.unit

JAN = datetime(2026, 1, 1, tzinfo=UTC)
JUN = datetime(2026, 6, 1, tzinfo=UTC)


class TestInvariant2BeliefNeedsBasis:
    """🔴 不变量 2：必须有依据，或明确标记为暂定。"""

    def test_active_belief_requires_confidence_basis(self, make_belief) -> None:
        with pytest.raises(ValidationError, match="confidence_basis"):
            make_belief(status=BeliefStatus.ACTIVE, confidence_basis=[])

    def test_tentative_belief_may_lack_basis(self, make_belief) -> None:
        """暂定信念允许暂无依据——这正是 TENTATIVE 的含义。"""
        belief = make_belief(status=BeliefStatus.TENTATIVE, confidence_basis=[])
        assert belief.confidence_basis == []

    def test_disputed_belief_also_requires_basis(self, make_belief) -> None:
        with pytest.raises(ValidationError, match="confidence_basis"):
            make_belief(status=BeliefStatus.DISPUTED, confidence_basis=[])

    def test_error_message_suggests_tentative(self, make_belief) -> None:
        """错误信息应给出可行动的建议，而不是只说"校验失败"。"""
        with pytest.raises(ValidationError, match="TENTATIVE"):
            make_belief(status=BeliefStatus.ACTIVE, confidence_basis=[])

    def test_blank_basis_entries_rejected(self, make_belief) -> None:
        with pytest.raises(ValidationError):
            make_belief(status=BeliefStatus.ACTIVE, confidence_basis=["  "])


class TestBeliefValidityWindow:
    def test_valid_until_must_follow_valid_from(self, make_belief) -> None:
        with pytest.raises(ValidationError, match="valid_until"):
            make_belief(valid_from=JUN, valid_until=JAN)

    def test_open_ended_belief_is_valid(self, make_belief) -> None:
        assert make_belief(valid_until=None).valid_until is None

    def test_is_currently_valid_within_window(self, make_belief) -> None:
        belief = make_belief(valid_from=JAN, valid_until=datetime(2026, 3, 1, tzinfo=UTC))
        assert belief.is_currently_valid(JUN - timedelta(days=120))
        assert not belief.is_currently_valid(JUN)

    def test_not_yet_valid(self, make_belief) -> None:
        belief = make_belief(valid_from=JUN)
        assert not belief.is_currently_valid(JAN)


class TestBeliefVersionChain:
    """🔴 不变量 5 的同类要求：纠正产生新版本，不就地覆盖。"""

    def test_supersedes_id_is_supported(self, make_belief) -> None:
        old_id = make_belief().id
        new_belief = make_belief(supersedes_id=old_id)
        assert new_belief.supersedes_id == old_id

    def test_self_supersede_is_rejected(self, make_belief) -> None:
        belief = make_belief()
        with pytest.raises(ValidationError, match="取代自身"):
            make_belief(id=belief.id, supersedes_id=belief.id)


class TestInvariant3JudgmentStrength:
    """🔴 不变量 3：存在高可信冲突时，不得输出无保留的确定结论。"""

    def test_unconditional_answer_with_unknowns_is_rejected(self, make_judgment) -> None:
        with pytest.raises(ValidationError, match="不变量 3"):
            make_judgment(
                recommended_epistemic_action=EpistemicAction.ANSWER,
                unresolved_unknowns=["朋友的真实动机未知"],
            )

    def test_unconditional_answer_without_unknowns_is_allowed(self, make_judgment) -> None:
        j = make_judgment(
            recommended_epistemic_action=EpistemicAction.ANSWER,
            unresolved_unknowns=[],
        )
        assert j.allows_strong_conclusion

    def test_caveated_answer_with_unknowns_is_allowed(self, make_judgment) -> None:
        """带保留的回答与未解决未知是相容的——这正是它存在的意义。"""
        j = make_judgment(
            recommended_epistemic_action=EpistemicAction.ANSWER_WITH_CAVEAT,
            unresolved_unknowns=["朋友的真实动机未知"],
        )
        assert not j.allows_strong_conclusion

    def test_defer_and_out_of_scope_allow_unknowns(self, make_judgment) -> None:
        for action in (EpistemicAction.DEFER, EpistemicAction.OUT_OF_SCOPE, EpistemicAction.WAIT):
            j = make_judgment(recommended_epistemic_action=action, unresolved_unknowns=["x"])
            assert not j.allows_strong_conclusion

    def test_out_of_scope_is_a_legitimate_conclusion(self, make_judgment) -> None:
        """任务书 §9.10：'事实无法决定价值选择'必须能作为结论输出。"""
        j = make_judgment(
            recommended_epistemic_action=EpistemicAction.OUT_OF_SCOPE,
            conclusion="这个问题的答案不由事实决定，取决于你重视什么",
            unresolved_unknowns=[],
        )
        assert j.recommended_epistemic_action is EpistemicAction.OUT_OF_SCOPE


class TestJudgmentRequiredContent:
    def test_rationale_summary_is_required(self, make_judgment) -> None:
        """结论必须说明建立在什么之上。"""
        with pytest.raises(ValidationError):
            make_judgment(rationale_summary=[])

    def test_blank_rationale_entries_rejected(self, make_judgment) -> None:
        with pytest.raises(ValidationError):
            make_judgment(rationale_summary=["  "])

    def test_confidence_basis_is_required(self, make_judgment) -> None:
        """没有依据的置信度只是语气。"""
        with pytest.raises(ValidationError):
            make_judgment(confidence_basis=[])

    def test_counterarguments_may_be_empty_for_trivial_facts(self, make_judgment) -> None:
        """🔴 允许为空——对"水在标准大气压下的沸点"强行制造反方观点
        正是任务书 §9.8 禁止的"机械地双方都有道理"。"""
        j = make_judgment(strongest_counterarguments=[])
        assert j.strongest_counterarguments == []

    def test_counterarguments_can_be_recorded(self, make_judgment) -> None:
        j = make_judgment(strongest_counterarguments=["另一种解释是他在忙"])
        assert j.strongest_counterarguments == ["另一种解释是他在忙"]


class TestJudgmentConfidence:
    def test_confidence_is_banded_not_percentage(self, make_judgment) -> None:
        """置信度用分档——模型不应伪造精确概率。"""
        j = make_judgment(confidence_band=ConfidenceBand.HIGH)
        assert j.confidence_band.rank == ConfidenceBand.HIGH.rank

    def test_percentage_string_is_rejected(self, make_judgment) -> None:
        with pytest.raises(ValidationError):
            make_judgment(confidence_band="87%")

    def test_default_action_is_cautious(self, make_judgment) -> None:
        """默认不得是无保留结论——保守是安全的方向。"""
        assert make_judgment().recommended_epistemic_action is EpistemicAction.ANSWER_WITH_CAVEAT
