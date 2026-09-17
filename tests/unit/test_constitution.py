"""认知宪法的单元测试（ADR-0011）。

宪法是"必须实现为代码断言，而不是只写文档"的落点（任务书 §14）。
本文件验证：**不变量确实存在、确实可强制、且形状不会被悄悄改坏。**
"""

from __future__ import annotations

import pytest

from ai_psi.cognition import constitution
from ai_psi.cognition.constitution import (
    AUTO_WRITABLE_MEMORY_TYPES,
    CONSTITUTION_VERSION,
    FORBIDDEN_MEMORY_CONTENT_CLASSES,
    INVARIANTS,
    constitution_fingerprint,
    invariant,
)
from ai_psi.domain.enums import EpistemicAction, MemoryType, ProposalStatus
from ai_psi.domain.exceptions import ConstitutionViolationError

pytestmark = pytest.mark.unit


class TestInvariantCatalogue:
    """任务书 §14 恰好列出 20 条认知不变量。"""

    def test_exactly_twenty_invariants(self) -> None:
        assert len(INVARIANTS) == 20

    def test_ids_are_unique_and_well_formed(self) -> None:
        ids = [inv.invariant_id for inv in INVARIANTS]
        assert len(set(ids)) == len(ids)
        assert ids == [f"I{i:02d}" for i in range(1, 21)]

    def test_every_invariant_has_a_statement(self) -> None:
        for inv in INVARIANTS:
            assert inv.statement.strip()
            assert len(inv.statement) > 5

    def test_every_invariant_names_a_code_mechanism(self) -> None:
        """🔴 "必须实现为代码断言或自动测试，而不是只写文档"。

        因此每条 enforcement 都必须指出具体的代码机制，
        不能是"应当注意"这类无从验证的表述。
        """
        vague = ("应当", "需要", "注意", "尽量")
        for inv in INVARIANTS:
            assert inv.enforcement.strip(), f"{inv.invariant_id} 未说明强制机制"
            assert not any(word in inv.enforcement for word in vague), (
                f"{inv.invariant_id} 的强制机制描述含糊，无法验证"
            )

    def test_stage_is_in_range(self) -> None:
        for inv in INVARIANTS:
            assert 1 <= inv.enforceable_in_stage <= 8

    def test_lookup_by_id(self) -> None:
        assert invariant("I11").statement.startswith("ImprovementProposal")
        assert invariant("I17").statement.startswith("状态机")

    def test_unknown_id_raises(self) -> None:
        with pytest.raises(KeyError):
            invariant("I99")

    def test_stage_one_covers_the_core_boundaries(self) -> None:
        """阶段 1 必须已经能强制这些边界，否则地基是空的。"""
        stage_one = {inv.invariant_id for inv in INVARIANTS if inv.enforceable_in_stage == 1}
        assert {"I01", "I02", "I03", "I05", "I06", "I10", "I11", "I13", "I17", "I19", "I20"} <= (
            stage_one
        )


class TestMemoryWriteWhitelist:
    """🔴 ADR-0004：白名单而非黑名单——未列出的默认拒绝。"""

    def test_whitelist_is_small(self) -> None:
        assert len(AUTO_WRITABLE_MEMORY_TYPES) < len(MemoryType)

    def test_sensitive_types_are_not_auto_writable(self) -> None:
        """用户稳定人格推断等一律不得自动写入。"""
        for memory_type in (MemoryType.SELF_MODEL, MemoryType.STRATEGY, MemoryType.SEMANTIC):
            assert memory_type not in AUTO_WRITABLE_MEMORY_TYPES

    def test_whitelist_is_immutable(self) -> None:
        assert isinstance(AUTO_WRITABLE_MEMORY_TYPES, frozenset)
        with pytest.raises(AttributeError):
            AUTO_WRITABLE_MEMORY_TYPES.add(MemoryType.SELF_MODEL)  # type: ignore[attr-defined]

    def test_forbidden_content_classes_cover_task_book_list(self) -> None:
        """任务书 §10.3 的清单必须被完整覆盖。"""
        joined = "".join(FORBIDDEN_MEMORY_CONTENT_CLASSES)
        for keyword in ("心理诊断", "第三方隐私", "思维链", "通用策略", "人格"):
            assert keyword in joined, f"禁止类别清单缺少：{keyword}"


class TestInvariantAssertions:
    def test_hypothesis_assertion_passes_for_normal_hypothesis(self, make_hypothesis) -> None:
        constitution.assert_hypothesis_not_fact(make_hypothesis())

    def test_user_model_assertion_passes_normally(self, make_user_model) -> None:
        constitution.assert_user_model_not_confirmed(make_user_model())

    def test_no_automatic_promotion_accepts_all_real_statuses(self) -> None:
        for status in ProposalStatus:
            constitution.assert_no_automatic_promotion(status)

    def test_no_automatic_promotion_rejects_synthetic_active(self) -> None:
        """即使有人伪造出一个"已生效"的状态，也必须被拒绝。"""

        class _FakeStatus:
            value = "active"

        with pytest.raises(ConstitutionViolationError, match="不得自动生效"):
            constitution.assert_no_automatic_promotion(_FakeStatus())  # type: ignore[arg-type]


class TestScopeAssertion:
    def test_correct_owner_passes(self, make_memory, user_id) -> None:
        memory = make_memory(user_id=user_id, status="active")
        constitution.assert_memory_retrievable_by(memory, requesting_user_id=user_id)

    def test_cross_user_access_is_rejected(self, make_memory, user_id, other_user_id) -> None:
        """🔴 不变量 14：跨用户访问是隐私事故。"""
        memory = make_memory(user_id=user_id, status="active")
        with pytest.raises(ConstitutionViolationError, match="作用域"):
            constitution.assert_memory_retrievable_by(memory, requesting_user_id=other_user_id)

    def test_superseded_memory_is_rejected(self, make_memory, user_id) -> None:
        """🔴 不变量 6：被取代的记忆不能作为默认有效结果返回。"""
        memory = make_memory(user_id=user_id, status="superseded")
        with pytest.raises(ConstitutionViolationError, match="不能作为默认有效结果返回"):
            constitution.assert_memory_retrievable_by(memory, requesting_user_id=user_id)

    def test_error_records_the_invariant_id(self, make_memory, user_id) -> None:
        memory = make_memory(user_id=user_id, status="deleted")
        try:
            constitution.assert_memory_retrievable_by(memory, requesting_user_id=user_id)
        except ConstitutionViolationError as exc:
            assert exc.invariant_id == "I06"
        else:  # pragma: no cover
            pytest.fail("应当抛出 ConstitutionViolationError")


class TestResponseStrengthAssertion:
    """🔴 不变量 7：最终回答不能比内部判断更确定。"""

    def test_caveated_judgment_cannot_yield_strong_response(self, make_judgment) -> None:
        judgment = make_judgment(recommended_epistemic_action=EpistemicAction.ANSWER_WITH_CAVEAT)
        with pytest.raises(ConstitutionViolationError, match=r"不变量 7|不得比内部判断更确定"):
            constitution.assert_response_not_stronger_than_judgment(
                judgment=judgment,
                response_allows_strong_conclusion=True,
            )

    def test_caveated_judgment_with_cautious_response_passes(self, make_judgment) -> None:
        judgment = make_judgment(recommended_epistemic_action=EpistemicAction.ANSWER_WITH_CAVEAT)
        constitution.assert_response_not_stronger_than_judgment(
            judgment=judgment,
            response_allows_strong_conclusion=False,
        )

    def test_strong_judgment_permits_strong_response(self, make_judgment) -> None:
        judgment = make_judgment(
            recommended_epistemic_action=EpistemicAction.ANSWER,
            unresolved_unknowns=[],
        )
        constitution.assert_response_not_stronger_than_judgment(
            judgment=judgment,
            response_allows_strong_conclusion=True,
        )


class TestFingerprint:
    def test_fingerprint_is_stable(self) -> None:
        assert constitution_fingerprint() == constitution_fingerprint()

    def test_fingerprint_is_hex_and_short(self) -> None:
        fp = constitution_fingerprint()
        assert len(fp) == 16
        int(fp, 16)  # 不抛异常即为合法十六进制

    def test_version_is_declared(self) -> None:
        assert CONSTITUTION_VERSION == "1.0.0"
