"""不变量的运行期自检（任务书 §14）。

🔴 **本文件的重点不是"自检现在是绿的"，而是"自检会红"。**

一组恒为绿的检查与没有检查是同一件事。因此这里反复做的动作是：
**把某条结构性保证按坏，然后要求自检把它指出来。**
做不到这一点的自检只是一份写得好看的清单。
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

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

    def test_the_named_invariant_does_not_move_with_the_tail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 变异测试发现：**同时坏两条**时，报出来的是哪一条没有被钉住。

        ``failures[0]`` 取的是第一条失败的。改成 ``failures[-1]`` 之后，
        只坏一条的用例照样通过（此时首尾同一个），而实际报出的编号
        变成了**列表末尾**那一条——于是"坏得越多，报出来的越靠后"，
        审计记录的头条随条数漂移。

        ``msg`` 里按同样的顺序列出了全部失败项，结构化字段必须与它
        指向同一处，否则告警路由拿到的编号与消息正文对不上。
        """
        monkeypatch.setattr(selfcheck, "PROPOSAL_ESCALATION_THRESHOLD", 1)  # I10
        monkeypatch.setattr(selfcheck, "ProposalStatus", _ShrunkProposalStatus)  # I11

        with pytest.raises(ConstitutionViolationError) as excinfo:
            assert_structural_invariants()
        assert excinfo.value.context["invariant_id"] == "I10", excinfo.value.context
        # 两条都要出现在正文里——只报一条等于把另一条吞了
        message = str(excinfo.value)
        assert "I10" in message and "I11" in message, message

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

    def test_the_probe_ids_are_genuine_uuids(self) -> None:
        """🔴 变异测试发现：探针的 id 是不是**合法 UUID**，之前没有任何断言。

        把 ``index + 1`` 改成 ``index / 1`` 之后，``uuid.UUID`` 会**照收**
        浮点数：``UUID(int=0.0)`` 构造成功，``.int`` 是 ``float``，
        而 ``.hex`` 在使用时抛 ``TypeError``。这个模块里的探针对象
        不参与持久化，所以它当场不炸——但一个"产出 UUID"的函数
        产出的是**读不出十六进制**的东西，就是坏的。
        """
        for item in selfcheck._probe_proposal(3).supporting_experience_ids:
            assert isinstance(item.int, int), item
            assert UUID(int=item.int) == item

    def test_the_probe_ids_never_collide_with_the_fixed_probe_id(self) -> None:
        """🔴 ``index + 1`` 的 ``+1`` 不是装饰：它把全零 UUID 挡在外面。

        去掉它（``+0`` / ``*1`` / ``//1`` / ``**1``）、或者换成
        ``<< 1`` / ``^ 1``，都会让 id 里出现 ``UUID(int=0)``——
        而那是本模块自己的固定探针标识 ``_PROBE_UUID``。
        同一个自检里两个不同的东西共用一个标识，报告就没法读了。
        """
        ids = selfcheck._probe_proposal(3).supporting_experience_ids
        assert selfcheck._PROBE_UUID not in ids


class TestTheGuardBoundaryIsTwo:
    """🔴 变异测试发现：``PROPOSAL_ESCALATION_THRESHOLD < 2`` 的**边界值
    本身**没有被断言。

    已有用例覆盖了 1（报"守卫被改坏"）和 3（默认值），唯独漏了 **2**。
    而 2 恰恰是这条守卫声称的最小合法值——它是边界。

    把 ``< 2`` 改成 ``<= 2``：门槛为 2 时走进"门槛被改坏"那一支，
    仍然报 ``ok=False``，而"1 会失败"这条断言对它照样成立。
    """

    @staticmethod
    def _change_the_threshold(monkeypatch: pytest.MonkeyPatch, threshold: int) -> None:
        """模拟**一次合法的门槛改动**——两处一起改。

        🔴 必须两处都改，这不是测试的方便，而是真实改动的样子：
        常量 ``PROPOSAL_ESCALATION_THRESHOLD`` 既被自检读来算探针条数，
        又被用作 ``meets_escalation_threshold`` 的默认值。只改前者是
        **改了一半**，自检会（正确地）报出两者分叉。
        """
        monkeypatch.setattr(selfcheck, "PROPOSAL_ESCALATION_THRESHOLD", threshold)
        monkeypatch.setattr(selfcheck, "_default_escalation_threshold", lambda: threshold)

    def test_a_threshold_of_two_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._change_the_threshold(monkeypatch, 2)
        result = selfcheck._check_i10()
        assert result.ok is True, result.detail

    def test_the_boundary_moves_with_the_constant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """🔴 自检不能只对**写死的那一个取值**成立。

        改这个常量是合法操作（只要不低于 2）。改到 2 之后自检必须**跟着**
        变成"门槛为 2、两条达标"。

        实测过一次它做不到：`meets_escalation_threshold` 的默认参数是
        **def 时**绑定到旧值的，探针条数却按运行期常量算——两者分叉，
        自检对着一个**没被改坏**的守卫报红，进程起不来。
        假警报比没有警报更坏：它会逼着人去关掉自检。
        """
        # 🔴 1000 不是凑数：`-5..256` 之外的整数**不被 CPython 缓存**，
        #    因此只有在这个量级上，「两处指向同一个数」才真的要求
        #    **相等**而不是「恰好是同一个对象」。少了它，把一致性判据
        #    写成 `is not` 的实现照样能过——那在小整数上只是碰巧对。
        for threshold in (2, 3, 4, 1000):
            self._change_the_threshold(monkeypatch, threshold)
            result = selfcheck._check_i10()
            assert result.ok is True, (threshold, result.detail)
            assert str(threshold) in result.detail

    def test_changing_only_the_constant_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """反方向：**只改一处**必须报出来，而不是当成一次合法改动。

        这正是"两处必须一起改"这句话的可执行版本：默认值还绑在旧门槛上，
        于是不吃默认值的调用方与吃默认值的调用方会按两个数判断。
        """
        monkeypatch.setattr(selfcheck, "PROPOSAL_ESCALATION_THRESHOLD", 2)
        result = selfcheck._check_i10()
        assert result.ok is False
        assert "默认门槛" in result.detail


class TestTheCheckProbesTheInputsItClaims:
    """🔴 变异测试发现：``_check_i10`` 的**探针参数**没有任何测试。

    ``one = _probe_proposal(1)`` 与 ``three = _probe_proposal(门槛)``
    里的两个数字，正是这条检查**声称在做的事**：它说"单条不达标、
    三条达标"，那就必须真的拿 1 条和 3 条去试。

    改成 ``0`` 或 ``2`` 之后三条断言全部照样成立（0 和 2 都低于门槛 3），
    于是细节信息变成了假的：报告里写着"单条不达标"，实际试的是别的条数。
    """

    def test_i10_probes_with_one_and_with_the_threshold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[int] = []
        real = selfcheck._probe_proposal

        def _spy(count: int) -> object:
            seen.append(count)
            return real(count)

        monkeypatch.setattr(selfcheck, "_probe_proposal", _spy)
        selfcheck._check_i10()
        assert seen == [1, selfcheck.PROPOSAL_ESCALATION_THRESHOLD]

    def test_i10_asks_the_guard_about_the_boundary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """被问的那个门槛值同样不能变。

        ``meets_escalation_threshold(threshold=1)`` 问的是"1 会被拒吗"。
        改成 0 之后答案一样是"会"，但它证明的东西弱了：一个只在
        ``threshold < 1`` 时才拒绝的守卫能让 0 通过检查。
        """
        asked: list[int] = []

        class _Recording:
            def meets_escalation_threshold(self, *, threshold: int = 3) -> bool:
                asked.append(threshold)
                if threshold < 2:
                    msg = "提案门槛不得低于 2"
                    raise ValueError(msg)
                return False

        monkeypatch.setattr(selfcheck, "ImprovementProposal", lambda **_: _Recording())
        selfcheck._check_i10()
        # 三问：先是边界那一问（1 会不会被拒），后两问是「探针达标了吗」
        # ——它们必须用**当前**门槛去问，而不是某个写死的值。
        #
        # ⚠️ 断言写成有序的全等而不是 set：`asked[1]` 曾经来自一次
        # **吃默认值**的调用（作者只改了其中一处），于是这条用例在
        # 常量 ≠ 3 时反而变红——而常量改成 2 本身是合法的。
        threshold = selfcheck.PROPOSAL_ESCALATION_THRESHOLD
        assert asked == [1, threshold, threshold], asked


class TestTheDefaultThresholdIsPinnedToTheConstant:
    """🔴 评审发现：把探针改成**显式传参**，代价是「默认值被改坏」不再可见。

    ``meets_escalation_threshold`` 的默认参数在 **def 时**绑定。
    显式传参之后，探针拿常量去问，答案全对——哪怕那个默认值已经被改成
    别的数。默认值比常量**大**时最危险：不吃默认值的调用方与吃默认值的
    调用方会按两个不同的门槛判断，而自检全绿。

    所以显式传参必须配一条单独的、盯住默认值的检查。
    """

    @pytest.mark.parametrize("default", [5, 1])
    def test_a_default_that_disagrees_with_the_constant_is_reported(
        self, monkeypatch: pytest.MonkeyPatch, default: int
    ) -> None:
        """🔴 **两个方向都要**。

        默认值比常量**大**：不吃默认值的调用方按 3 判、吃默认值的按 5 判，
        永远少产生提案；比常量**小**：反过来，门槛形同虚设。
        只断言"大"的那一边，把判据写成 `>` 的实现照样能过。
        """
        monkeypatch.setattr(selfcheck, "_default_escalation_threshold", lambda: default)
        result = selfcheck._check_i10()
        assert result.ok is False
        assert str(default) in result.detail and "默认门槛" in result.detail

    def test_the_shipped_default_agrees_with_the_constant(self) -> None:
        """正向：真实代码里两者本来就是同一个数。"""
        assert selfcheck._default_escalation_threshold() == (
            selfcheck.PROPOSAL_ESCALATION_THRESHOLD
        )

    def test_a_value_equal_but_distinct_object_is_not_a_mismatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 判据必须是**相等**，不是「同一个对象」。

        变异测试发现：把 ``!=`` 改成 ``is not`` 之后自检照样全绿——
        因为 ``-5..256`` 的整数被 CPython 缓存，而更大的字面量在同一个
        编译单元里也常常是同一个常量对象。**那是碰巧，不是契约。**

        真实世界里门槛完全可能来自配置解析（``int("1000")`` 每次都是
        新对象），那时 `is not` 会把一个**正确**的配置报成不一致，
        而自检的失败意味着进程起不来——又是一次假警报。
        """
        monkeypatch.setattr(selfcheck, "PROPOSAL_ESCALATION_THRESHOLD", 1000)
        monkeypatch.setattr(selfcheck, "_default_escalation_threshold", lambda: int("1000"))
        assert selfcheck._check_i10().ok is True


class TestTheI10GuardIsCheckedBeyondItsEntrance:
    """🔴 变异测试发现：I10 的"单条经验被判达标"那一支从未被执行过。

    已有用例里的 ``_Permissive`` 替身**永远返回 True**，因此
    ``threshold=1`` 那一问根本不抛，检查在**第一支**就返回了，
    写着 ``ok=False`` 的那一行永远走不到——把它改成 ``ok=True``
    没有任何用例会红。

    这一支的意义是：守卫**只做形式**。它挡住了字面上的 1，
    但真正决定达标的门槛没生效。
    """

    def test_i10_notices_a_guard_that_only_rejects_the_literal_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FormalOnlyGuard:
            def meets_escalation_threshold(self, *, threshold: int = 3) -> bool:
                if threshold < 2:
                    msg = "提案门槛不得低于 2"
                    raise ValueError(msg)
                return True  # 任何门槛都达标——"三条"与"一条"没有区别

        monkeypatch.setattr(selfcheck, "ImprovementProposal", lambda **_: _FormalOnlyGuard())
        result = selfcheck._check_i10()
        assert result.ok is False
        assert "真正的门槛没有生效" in result.detail


class TestStatementLookupFallsBack:
    """🔴 变异测试发现：``_statement_of`` 的**未命中**这一支没有测试。

    ``item.invariant_id == invariant_id`` 里的 ``==`` 改成 ``>=`` 之后，
    按登记顺序排在目标**前面**的任何一条都会被当成本条返回。
    查询三个真实编号时恰好都先命中自己，所以三条自查全是绿的——
    而报告里会挂上**另一条不变量**的陈述。
    """

    def test_an_unknown_id_gets_the_placeholder(self) -> None:
        assert selfcheck._statement_of("I00") == "（宪法中未登记）"

    def test_a_lower_id_does_not_borrow_an_earlier_statement(self) -> None:
        """``I00`` 在字典序上低于全部登记项——``>=`` 会把它判成 ``I01``。"""
        borrowed = {item.statement for item in INVARIANTS}
        assert selfcheck._statement_of("I00") not in borrowed


class TestTheReportSaysWhichDirectionTheEnumMoved:
    """🔴 变异测试发现：报告只说「变了」，**没有断言是往哪个方向变**。

    ``actual - EXPECTED`` 与 ``EXPECTED - actual`` 是两个**方向**。
    把其中任意一个改成对称差 ``^`` 之后，被删掉的那个成员会跑进
    「多出」那一支里，而 ``ok is False`` 与「成员名出现在 detail 里」
    两条断言照样成立——报告于是在**指错方向**。

    这不是措辞问题：读到「多出了一个状态」和读到「少了一个状态」，
    修复动作完全不同——一个是去查谁加的，一个是去查谁删的。
    """

    def test_an_added_status_is_reported_as_added(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(selfcheck, "ProposalStatus", _ExtendedProposalStatus)
        detail = selfcheck._check_i11().detail
        assert "多出" in detail
        assert "少了" not in detail

    def test_a_removed_status_is_reported_as_removed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(selfcheck, "ProposalStatus", _ShrunkProposalStatus)
        detail = selfcheck._check_i11().detail
        assert "少了" in detail
        assert "多出" not in detail
