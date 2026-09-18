"""不变量的运行期自检（任务书 §14）。

🔴 **本文件的重点不是"自检现在是绿的"，而是"自检会红"。**

一组恒为绿的检查与没有检查是同一件事。因此这里反复做的动作是：
**把某条结构性保证按坏，然后要求自检把它指出来。**
做不到这一点的自检只是一份写得好看的清单。
"""

from __future__ import annotations

from enum import StrEnum

import pytest

from ai_psi.cognition.constitution import INVARIANTS
from ai_psi.domain.exceptions import ConstitutionViolationError
from ai_psi.reliability import invariants as selfcheck
from ai_psi.reliability.invariants import (
    RUNTIME_CHECKED_INVARIANTS,
    assert_structural_invariants,
    check_structural_invariants,
    failing_checks,
)

pytestmark = pytest.mark.unit


class TestCheckInventory:
    def test_three_checks_run(self) -> None:
        assert [item.invariant_id for item in check_structural_invariants()] == [
            "I01",
            "I10",
            "I11",
        ]

    def test_all_pass_on_an_intact_constitution(self) -> None:
        assert failing_checks() == ()

    def test_assertion_is_silent_when_healthy(self) -> None:
        assert_structural_invariants()  # 不抛错

    def test_declared_ids_match_the_checks_that_run(self) -> None:
        """🔴 声明的覆盖范围必须与实际跑的检查一致。

        声明多于实际会让报告里出现一个**永远不会失败**的绿勾。
        """
        executed = {item.invariant_id for item in check_structural_invariants()}
        assert executed == RUNTIME_CHECKED_INVARIANTS

    def test_declared_ids_exist_in_the_constitution(self) -> None:
        known = {item.invariant_id for item in INVARIANTS}
        assert known >= RUNTIME_CHECKED_INVARIANTS

    def test_every_check_quotes_its_statement(self) -> None:
        """检查结果要能自己回答"我在守哪条"，而不是让人去翻宪法。"""
        for item in check_structural_invariants():
            assert item.statement != "（宪法中未登记）"
            assert item.statement.strip()

    def test_every_check_carries_a_detail(self) -> None:
        """只说"正常"的检查在出问题时提供不了任何信息。"""
        for item in check_structural_invariants():
            assert item.detail.strip()

    def test_checks_are_frozen(self) -> None:
        import dataclasses

        with pytest.raises(dataclasses.FrozenInstanceError):
            check_structural_invariants()[0].ok = False  # type: ignore[misc]


class TestTheCheckCanActuallyFail:
    """🔴 本类存在的理由：证明上面那些绿勾是**有意义**的。"""

    def test_i10_notices_a_threshold_of_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """门槛被调到 1 —— 单次经验即可推广为全局策略。"""
        monkeypatch.setattr(selfcheck, "PROPOSAL_ESCALATION_THRESHOLD", 1)
        result = selfcheck._check_i10()
        assert result.ok is False
        assert "1" in result.detail

    def test_i10_notices_when_the_guard_stops_guarding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """门槛值还是 3，但 ``meets_escalation_threshold`` 不再拒绝 1。

        这就是自检与"读一遍常量"的区别：常量对不代表守它的代码还在。
        """

        class _Permissive:
            def meets_escalation_threshold(self, *, threshold: int = 3) -> bool:
                return True

        monkeypatch.setattr(
            selfcheck,
            "ImprovementProposal",
            lambda **_: _Permissive(),  # type: ignore[arg-type]
        )
        result = selfcheck._check_i10()
        assert result.ok is False
        assert "threshold=1" in result.detail

    def test_i11_notices_an_active_like_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """枚举里多出一个表示"已生效"的成员——不变量 11 的类型级保证就此消失。"""
        monkeypatch.setattr(
            selfcheck,
            "AUTO_PROMOTION_FORBIDDEN_VALUES",
            frozenset({"active", "draft"}),  # "draft" 是真实成员
        )
        result = selfcheck._check_i11()
        assert result.ok is False
        assert "draft" in result.detail

    def test_i11_notices_a_proposal_that_can_become_active(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Promotable:
            can_become_active = True

        monkeypatch.setattr(
            selfcheck,
            "ImprovementProposal",
            lambda **_: _Promotable(),  # type: ignore[arg-type]
        )
        result = selfcheck._check_i11()
        assert result.ok is False
        assert "can_become_active" in result.detail

    def test_i11_notices_a_dead_backstop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """类型层仍完好，但宪法的兜底断言被改成空操作。

        没有鸭子类型替身就测不到这条分支——真实状态里永远构造不出 "active"。
        """
        monkeypatch.setattr(selfcheck, "assert_no_automatic_promotion", lambda _status: None)
        result = selfcheck._check_i11()
        assert result.ok is False
        assert "兜底层失效" in result.detail

    def test_i01_notices_a_confirmable_hypothesis_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``HypothesisStatus`` 里出现了表示"已确认"的成员。"""

        class _FakeHypothesisStatus(StrEnum):
            OPEN = "open"
            CONFIRMED = "confirmed"

        monkeypatch.setattr(selfcheck, "HypothesisStatus", _FakeHypothesisStatus)
        result = selfcheck._check_i01()
        assert result.ok is False
        assert "confirmed" in result.detail

    def test_assertion_raises_and_names_the_broken_invariant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 地基被改坏时进程**不该起来**——而不是等某次请求才奇怪地失败。"""
        monkeypatch.setattr(selfcheck, "PROPOSAL_ESCALATION_THRESHOLD", 1)
        with pytest.raises(ConstitutionViolationError) as excinfo:
            assert_structural_invariants()
        assert excinfo.value.context["invariant_id"] == "I10"

    def test_failing_checks_lists_only_the_broken_ones(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(selfcheck, "PROPOSAL_ESCALATION_THRESHOLD", 1)
        failures = failing_checks()
        assert [item.invariant_id for item in failures] == ["I10"]


class TestNoProbeLeaks:
    def test_probe_ids_are_fixed_and_inert(self) -> None:
        """自检探针只活在内存里，不参与任何持久化。"""
        assert selfcheck._PROBE_UUID.int == 0

    def test_checks_take_no_arguments(self) -> None:
        """自检能在没有完整运行时的场合跑——否则它没法放在启动路径上。"""
        import inspect

        assert not inspect.signature(check_structural_invariants).parameters
        assert not inspect.signature(assert_structural_invariants).parameters
