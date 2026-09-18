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
from ai_psi.domain.enums import ProposalStatus
from ai_psi.domain.exceptions import ConstitutionViolationError
from ai_psi.reliability import invariants as selfcheck
from ai_psi.reliability.invariants import (
    EXPECTED_PROPOSAL_STATUSES,
    RUNTIME_CHECKED_INVARIANTS,
    assert_structural_invariants,
    check_structural_invariants,
    failing_checks,
)

pytestmark = pytest.mark.unit


class _ExtendedProposalStatus(StrEnum):
    """真实成员 + 一个**名字无辜但语义就是"已生效"**的新成员。

    它就是评审用来骗过初版自检的那个反例（``ENABLED``）。
    写成字面量枚举而不是动态构造，是为了让 mypy 能看懂它。
    """

    DRAFT = "draft"
    PENDING_EVALUATION = "pending_evaluation"
    EVALUATED = "evaluated"
    REJECTED = "rejected"
    APPROVED_FOR_MANUAL_TRIAL = "approved_for_manual_trial"
    ENABLED = "enabled"


class _ShrunkProposalStatus(StrEnum):
    """少了"批准进行人工试验"那条——同样是结构性变化。"""

    DRAFT = "draft"
    PENDING_EVALUATION = "pending_evaluation"
    EVALUATED = "evaluated"
    REJECTED = "rejected"


def _always_rejects() -> object:
    """一个"永远抛 ValueError，但抛的原因与门槛无关"的替身。"""

    class _Guard:
        def meets_escalation_threshold(self, *, threshold: int = 3) -> bool:
            del threshold
            msg = "提案必须至少有一条支撑经验"
            raise ValueError(msg)

    return _Guard()


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
            lambda **_: _Permissive(),
        )
        result = selfcheck._check_i10()
        assert result.ok is False
        assert "threshold=1" in result.detail

    def test_i11_notices_an_active_like_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """枚举里多出一个**名字就在禁止名单上**的成员。"""
        monkeypatch.setattr(
            selfcheck,
            "AUTO_PROMOTION_FORBIDDEN_VALUES",
            frozenset({"active", "draft"}),  # "draft" 是真实成员
        )
        result = selfcheck._check_i11()
        assert result.ok is False
        assert "draft" in result.detail

    def test_i11_notices_any_new_status_even_with_an_innocent_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 **主检查是白名单：不认识名字，只认识"集合变了"。**

        上一条用例 monkeypatch 的是**禁止名单本身**，所以它只证明了
        "检查会读那份名单"，完全没有证明"检查能发现新成员"。
        实测：给 ``ProposalStatus`` 加一个 ``ENABLED = "enabled"``
        （语义就是已生效）能同时骗过类型层与兜底层——两层共用同一份
        四个词的名单，而自检照绿，detail 里还在宣称
        "7 个提案状态中无一可表示已生效"。
        """
        # 替身必须真的只多出那一个成员，否则这条用例证明的是别的东西
        assert {item.value for item in _ExtendedProposalStatus} == {
            item.value for item in ProposalStatus
        } | {"enabled"}
        monkeypatch.setattr(selfcheck, "ProposalStatus", _ExtendedProposalStatus)

        result = selfcheck._check_i11()
        assert result.ok is False
        assert "enabled" in result.detail

    def test_i11_notices_a_removed_status_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """少一个状态同样是结构性变化——它可能正是"批准"那一条。"""
        assert {item.value for item in _ShrunkProposalStatus} == {
            item.value for item in ProposalStatus
        } - {"approved_for_manual_trial"}
        monkeypatch.setattr(selfcheck, "ProposalStatus", _ShrunkProposalStatus)

        result = selfcheck._check_i11()
        assert result.ok is False
        assert "approved_for_manual_trial" in result.detail

    def test_the_expected_set_matches_the_real_enum(self) -> None:
        """预期集合必须与真实枚举一致，否则自检会永远报红。"""
        assert {item.value for item in ProposalStatus} == EXPECTED_PROPOSAL_STATUSES

    def test_i10_probe_checks_more_than_the_rejection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 只捕获"抛 ValueError"的探针，任何一条无关的 ValueError 都能骗过它。

        实测：把 ``meets_escalation_threshold`` 改成
        ``raise ValueError("提案必须至少有一条支撑经验")``（删掉 threshold<2
        的守卫，只留一个无关的抛错）之后，初版自检照样报绿，
        detail 还在宣称"拒绝低于 2 的取值"——它给出了一条**自己没验证过的**断言。

        现在探针还会问正向路径，那个替身在第二次调用时抛出的异常
        会被 :func:`_guarded` 接住并报为"检查跑不起来"。
        """
        monkeypatch.setattr(
            selfcheck,
            "ImprovementProposal",
            lambda **_: _always_rejects(),  # 永远抛，但原因与门槛无关
        )
        checks = {item.invariant_id: item for item in check_structural_invariants()}
        assert checks["I10"].ok is False

    def test_i10_notices_a_threshold_that_nobody_can_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """正向路径也要验：门槛被抬到没人能过，等于永远不产生提案。"""

        class _NeverMeets:
            def meets_escalation_threshold(self, *, threshold: int = 3) -> bool:
                if threshold < 2:
                    msg = "提案门槛不得低于 2"
                    raise ValueError(msg)
                return False

        monkeypatch.setattr(selfcheck, "ImprovementProposal", lambda **_: _NeverMeets())
        result = selfcheck._check_i10()
        assert result.ok is False
        assert "未达门槛" in result.detail

    def test_a_crashing_check_is_reported_as_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 检查自己崩了 = 这条保证**没有被验证**，必须以不健康的形式报出来。

        初版让非 ``ValueError`` 的异常直接穿出去：守卫改成抛
        ``ConstitutionViolationError``（比 ``ValueError`` 更贴切）之后，
        ``/health/cognitive`` 会返回 **500**，而不是把"地基坏了"
        报成 ``degraded``——那恰好是这个端点存在的理由。
        """

        def _explode() -> object:
            msg = "模拟自检自身崩溃"
            raise ConstitutionViolationError(msg, invariant_id="I11")

        monkeypatch.setattr(selfcheck, "_check_i11", _explode)
        checks = selfcheck.check_structural_invariants()
        i11 = next(item for item in checks if item.invariant_id == "I11")
        assert i11.ok is False
        assert "模拟自检自身崩溃" in i11.detail

    def test_a_crashing_check_does_not_take_down_the_health_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ai_psi.reliability.health import invariant_dimension

        def _explode() -> object:
            msg = "模拟自检自身崩溃"
            raise RuntimeError(msg)

        monkeypatch.setattr(selfcheck, "_check_i10", _explode)
        dimension = invariant_dimension()  # 不抛，报 degraded
        assert dimension.ok is False
        assert "I10" in dimension.detail

    def test_i01_notices_a_status_that_can_be_written_as_fact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``can_be_written_as_fact`` 一旦不再恒为 False，自检必须发现。

        这条分支此前从未被任何用例触发过（覆盖率报告里是一个缺口）——
        而它正是"假设不能被写成事实"这条保证的**最后一层**。
        """

        class _LeakyHypothesis:
            def __init__(self, **_: object) -> None:
                pass

            def can_be_written_as_fact(self) -> bool:
                return True

        monkeypatch.setattr(selfcheck, "Hypothesis", _LeakyHypothesis)
        result = selfcheck._check_i01()
        assert result.ok is False
        assert "写成事实" in result.detail

    def test_i11_notices_a_proposal_that_can_become_active(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Promotable:
            can_become_active = True

        monkeypatch.setattr(
            selfcheck,
            "ImprovementProposal",
            lambda **_: _Promotable(),
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


class TestEachCheckQuotesItsOwnStatement:
    """🔴 变异测试发现的**真实缺口**：陈述可以"张冠李戴"。

    原有用例只断言了 ``statement != "（宪法中未登记）"``——
    它挡得住"取不到"，挡不住"取错了"。

    `_statement_of` 的匹配条件被改成 ``!=`` / ``>=`` / ``>`` 之后，
    它返回的是**另一条**不变量的陈述（或占位符），
    而上述断言对"另一条的陈述"照样为真。

    后果是自检报告里 I10 的检查结果会挂着 I01 的陈述——
    一份**看起来完整、实际指错了**的审计记录。
    """

    def test_the_three_statements_are_pairwise_distinct(self) -> None:
        statements = [item.statement for item in check_structural_invariants()]
        assert len(set(statements)) == len(statements) == 3

    def test_each_statement_belongs_to_its_own_invariant(self) -> None:
        """逐条对照宪法里登记的那一句。"""
        expected = {item.invariant_id: item.statement for item in INVARIANTS}
        for check in check_structural_invariants():
            assert check.statement == expected[check.invariant_id], check.invariant_id


class TestTheI10GuardItselfIsAsserted:
    """🔴 变异测试发现：**门槛守卫的边界**没有被断言。

    `_check_i10` 开头是：

    ```
    if PROPOSAL_ESCALATION_THRESHOLD < 2:
        return ... ok=False ...（detail 写明"低于 2 等于允许单次经验推广"）
    ```

    把 `< 2` 改成 `< 1` 之后，门槛为 1 时的结果仍然是"不通过"——
    只是**走的是另一条分支**（探针那条），detail 里不再有那句话。
    原有用例断言了 ``ok is False`` 与 ``"1" in detail``，两者都仍然成立。

    区别在**理由**：一条说"门槛本身被改坏了"，另一条说"某条经验
    在门槛 1 下被判为达标"。它们指向完全不同的修复动作。
    """

    def test_threshold_one_is_reported_as_a_broken_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(selfcheck, "PROPOSAL_ESCALATION_THRESHOLD", 1)
        result = selfcheck._check_i10()
        assert result.ok is False
        assert "低于 2" in result.detail, result.detail

    def test_the_intact_guard_says_so_in_its_detail(self) -> None:
        """正向：门槛正常时，detail 要写明"拒绝低于 2 的取值"。"""
        result = selfcheck._check_i10()
        assert result.ok is True
        assert "低于 2" in result.detail, result.detail


class TestTheProbeUsesExactlyTheCountItClaims:
    """🔴 变异测试发现：探针**构造参数**没有任何测试。

    `_probe_proposal(n)` 造出 n 条支撑经验，`_check_i10` 用
    `_probe_proposal(1)` 与 `_probe_proposal(PROPOSAL_ESCALATION_THRESHOLD)`
    分别验证"单条不达标"与"三条达标"。

    把 `1` 改成 `2` 之后，"单条不达标"这半句变成"两条不达标"——
    仍然为真（2 < 3），因此原有断言照样通过。**探针不再证明它声称的事。**
    """

    def test_the_probe_builds_the_requested_number(self) -> None:
        for count in (1, 2, 3):
            probe = selfcheck._probe_proposal(count)
            assert len(probe.supporting_experience_ids) == count

    def test_the_probe_ids_are_distinct(self) -> None:
        """重复的 id 会让"三条经验"实际只有一条——门槛形同虚设。"""
        probe = selfcheck._probe_proposal(3)
        assert len(set(probe.supporting_experience_ids)) == 3
