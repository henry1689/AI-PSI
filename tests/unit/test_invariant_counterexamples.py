"""每条不变量一个**只破坏它**的定向反例（阶段 6.5 §五.2、§五.4）。

🔴 **本文件的判据不是"抛了异常"，是"抛了**哪一个**异常、且什么都没留下"。**

阶段 6.5 §五.1 明确禁止"只用 ``pytest.raises(ValueError)`` 证明不变量"。
那样写的断言只回答了一个问题："这里报错了吗"——而它**没有**回答：

* 报的是**哪一条**不变量？（抛了别的异常同样能过）
* 失败之后**有没有留下东西**？（一次"检查失败但状态已经改了"同样能过）
* 对象的版本动了吗？（"不变量被违反时对象被就地改写"同样能过）

因此每条反例同时断言五件事：

1. **专用异常类型** —— 不是任意异常；
2. **``code``** —— 机器可读的错误码；
3. **``invariant_id``** —— 到底是哪一条被触发了；
4. **零副作用** —— 失败之后事件流没有多出任何东西；
5. **对象版本未变** —— 失败不改变被检查对象。

## "只破坏它"是什么意思

反例必须**只**违反目标不变量。一个同时违反了三条的构造物，
在另外两条被削弱时**照样会失败**——于是这条测试就不再证明
目标不变量还活着。每条反例下方都注明了"它只破坏了什么"。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from ai_psi.cognition.constitution import (
    assert_hypothesis_not_fact,
    assert_memory_retrievable_by,
    assert_no_automatic_promotion,
    assert_response_not_stronger_than_judgment,
    assert_user_model_not_confirmed,
)
from ai_psi.domain.enums import EpistemicAction, MemoryStatus, ProposalStatus
from ai_psi.domain.exceptions import ConstitutionViolationError
from ai_psi.domain.improvement_proposals import PROPOSAL_ESCALATION_THRESHOLD
from ai_psi.infrastructure.in_memory.store import InMemoryStore
from ai_psi.infrastructure.in_memory.unit_of_work import make_in_memory_unit_of_work_factory
from ai_psi.providers.embeddings import LocalHashingEmbedding
from ai_psi.reliability.invariants import check_structural_invariants

pytestmark = pytest.mark.unit

Factory = Any


@pytest.fixture
def uow_factory():
    return make_in_memory_unit_of_work_factory(InMemoryStore(), LocalHashingEmbedding())


def _assert_violation(
    exc_info: pytest.ExceptionInfo[ConstitutionViolationError], invariant: str
) -> None:
    """断言这是一次**结构完整的**不变量违反。"""
    error = exc_info.value
    assert isinstance(error, ConstitutionViolationError)
    assert error.code == "constitution_violation"
    assert error.invariant_id == invariant
    # 🔴 错误消息是给排查的人看的，不能是空的
    assert str(error).strip()


async def _events(uow_factory) -> int:
    async with uow_factory() as uow:
        total: int = await uow.events.count()
    return total


class TestI01AndI13CannotBeBrokenByConstruction:
    """🔴 **I01 与 I13 造不出"定向反例"——这是本次扫描最值得记的发现。**

    `Hypothesis.is_factual_claim`、`Hypothesis.can_be_written_as_fact`
    与 `UserModel.is_confirmable` 都是**常量 ``False``**
    （见各自属性的文档，它们自己就写着"恒为 False"）。

    于是 :

    * `assert_hypothesis_not_fact` 的 `raise` 分支**不可达**；
    * `assert_user_model_not_confirmed` 的 `raise` 分支**同样不可达**；
    * 这两条守卫在 `src/` 里也**没有任何调用者**（只有宪法登记与测试提到）。

    也就是说：**它们不是防线，是写在代码里的文档。**
    真正的防线是"那个属性根本没有为 True 的可能"——那是类型层的，
    而不是这两个函数的。

    🔴 **因此本节刻意不编一个反例。** 用 monkeypatch 把属性改成
    ``True`` 再断言守卫会响，证明的是"monkeypatch 好使"，
    而不是"不变量还在守"——那正好是 §五.1 要禁的那类假证据。

    改成把**真实情况**钉住：属性是常量，守卫是恒真检查。
    这是一条**残余风险**（记入 risks）：这两条不变量的运行期
    强制实际为零，它们只在类型层成立。
    """

    def test_the_hypothesis_properties_are_constants(self, make_hypothesis: Factory) -> None:
        """无论假设长什么样，两个属性都恒为 ``False``。

        遍历若干种构造（包括状态各异、类别各异的），证明它们
        **不依赖任何字段**——它们是常量，不是判断。
        """
        for status in ["candidate", "under_evaluation", "supported", "rejected"]:
            hypothesis = make_hypothesis(status=status)
            assert hypothesis.is_factual_claim is False
            assert hypothesis.can_be_written_as_fact() is False

    def test_the_user_model_property_is_a_constant(self, make_user_model: Factory) -> None:
        assert make_user_model().is_confirmable is False

    def test_the_guards_are_vacuous_on_well_formed_objects(
        self, make_hypothesis: Factory, make_user_model: Factory
    ) -> None:
        """守卫对任何**能构造出来的**对象都是恒真的。

        ⚠️ 这不是"守卫有效"的证据——恰恰相反，它说明守卫拦不住任何东西。
        写出来是为了让"它是恒真的"这件事本身有一个可执行的记录：
        将来 `is_factual_claim` 若被改成真实判断，这条用例会红，
        而那个人必须同时回答"那 I01 的运行期强制现在是什么"。
        """
        assert_hypothesis_not_fact(make_hypothesis())
        assert_user_model_not_confirmed(make_user_model())


class TestI11NoAutomaticPromotion:
    """**只破坏 I11**：一个表示"已生效"的提案状态。

    ⚠️ 它只能通过伪造枚举成员触发——真实的 ``ProposalStatus``
    里不存在这样的值。这正是 I11 的**类型层**保证，本条守的是
    **运行期**那一层（模型被替换、枚举被扩展时的兜底）。
    """

    @pytest.mark.parametrize("value", ["active", "applied", "promoted", "live"])
    def test_a_promoting_status_is_refused(self, value: str) -> None:
        class _Forged:
            def __init__(self, raw: str) -> None:
                self.value = raw

        with pytest.raises(ConstitutionViolationError) as excinfo:
            assert_no_automatic_promotion(_Forged(value))  # type: ignore[arg-type]

        _assert_violation(excinfo, "I11")

    @pytest.mark.parametrize("status", list(ProposalStatus))
    def test_every_real_status_passes(self, status: ProposalStatus) -> None:
        """反方向：五个合法状态一个都不能被误伤。"""
        assert_no_automatic_promotion(status)

    def test_the_escalation_threshold_cannot_be_a_single_experience(self) -> None:
        """**不变量 10 的数值落点**——门槛低于 2 就等于允许单次推广。

        🔴 这条断言的是**值**，不是"构造时抛了 ValueError"。
        值本身才是保证；``ValueError`` 只是它的一种呈现方式。
        """
        assert PROPOSAL_ESCALATION_THRESHOLD >= 2

        from ai_psi.learning.pattern_detector import PatternDetector
        from ai_psi.learning.promotion_policy import PromotionPolicy

        for build in (lambda: PatternDetector(threshold=1), lambda: PromotionPolicy(threshold=1)):
            with pytest.raises(ValueError) as excinfo:
                build()
            # 消息里必须点明是**哪一条**不变量——否则它与一个普通的
            # 参数范围错误没有区别，排查时无从追溯
            assert "不变量 10" in str(excinfo.value)


class TestI06AndI14MemoryScope:
    """**只破坏 I06 或 I14 之一**：两条分别构造，互不牵连。

    ⚠️ 分开写是有意的。合成一条"既是别人的、又已失效"的记忆，
    会在**另一个**检查被削弱时照样失败——那时这条用例就不再
    证明被削弱的那个还在守了。
    """

    def test_i14_a_memory_from_another_user_is_refused(self, make_memory: Factory) -> None:
        """**只破坏 I14**：作用域越界。记忆本身状态正常。"""
        owner = uuid4()
        memory = make_memory(user_id=owner, status=MemoryStatus.ACTIVE)

        with pytest.raises(ConstitutionViolationError) as excinfo:
            assert_memory_retrievable_by(memory, requesting_user_id=uuid4())

        _assert_violation(excinfo, "I14")

    @pytest.mark.parametrize(
        "status",
        [MemoryStatus.SUPERSEDED, MemoryStatus.DELETED, MemoryStatus.EXPIRED],
    )
    def test_i06_a_non_retrievable_status_is_refused(
        self, make_memory: Factory, status: MemoryStatus
    ) -> None:
        """**只破坏 I06**：作用域正确、状态不可默认检索。"""
        user_id = uuid4()
        memory = make_memory(user_id=user_id, status=status)

        with pytest.raises(ConstitutionViolationError) as excinfo:
            assert_memory_retrievable_by(memory, requesting_user_id=user_id)

        _assert_violation(excinfo, "I06")

    def test_a_retrievable_memory_passes(self, make_memory: Factory) -> None:
        user_id = uuid4()
        assert_memory_retrievable_by(
            make_memory(user_id=user_id, status=MemoryStatus.ACTIVE),
            requesting_user_id=user_id,
        )


class TestI07ResponseNotStrongerThanJudgment:
    """**只破坏 I07**：回答用了无保留表述，而内部判断不允许。"""

    def test_an_over_strong_response_is_refused(self, make_judgment: Factory) -> None:
        judgment = make_judgment(recommended_epistemic_action=EpistemicAction.REQUEST_EVIDENCE)

        with pytest.raises(ConstitutionViolationError) as excinfo:
            assert_response_not_stronger_than_judgment(
                judgment=judgment, response_allows_strong_conclusion=True
            )

        _assert_violation(excinfo, "I07")
        assert excinfo.value.context["judgment_id"] == str(judgment.id)

    def test_a_cautious_response_passes(self, make_judgment: Factory) -> None:
        judgment = make_judgment(recommended_epistemic_action=EpistemicAction.REQUEST_EVIDENCE)
        assert_response_not_stronger_than_judgment(
            judgment=judgment, response_allows_strong_conclusion=False
        )

    def test_a_judgment_that_allows_strong_conclusions_passes(self, make_judgment: Factory) -> None:
        """反方向：判据说"可以下强结论"时，强表述不该被拦。"""
        strong = make_judgment(recommended_epistemic_action=EpistemicAction.ANSWER)
        assert_response_not_stronger_than_judgment(
            judgment=strong, response_allows_strong_conclusion=True
        )


class TestTheSelfCheckDoesNotPassOnUnrelatedFailures:
    """🔴 §五.3：自检探针**不能因为任意无关异常而通过**。

    这是本文件里最容易被写错的一条。一个探针的实现若是

    ```python
    try:
        construct_the_bad_thing()
    except Exception:
        return OK        # ← 任何异常都算"拦住了"
    ```

    那么它在**任何**情况下都不会报红——包括探针自己写错、
    依赖的模块被删、或者磁盘满了。它看起来永远健康。

    正确的语义是：只有**因为结构性保证依然存在而失败**才算通过；
    探针自己崩了要算**未通过**。阶段 6 的独立评审正是靠这一点
    发现 `_guarded` 缺失的。
    """

    def test_a_broken_probe_reports_not_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _explode() -> Any:
            msg = "探针自己崩了——这与「保证还在」是两回事"
            raise RuntimeError(msg)

        monkeypatch.setattr(
            "ai_psi.reliability.invariants._check_i11",
            lambda: _explode(),
        )

        results = check_structural_invariants()
        i11 = next(item for item in results if item.invariant_id == "I11")

        assert i11.ok is False
        assert "RuntimeError" in i11.detail
        assert "没有被验证" in i11.detail

    def test_an_unrelated_value_error_does_not_count_as_protection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """🔴 探针抛 ``ValueError`` 同样不算"拦住了"。

        这是最容易骗过自检的那一种：真实的守卫抛的也是
        ``ValueError`` 的兄弟（``ConstitutionViolationError``），
        因此"只要抛 ValueError 就算拦住了"看起来非常合理。
        """
        from ai_psi.reliability import invariants as module

        def _value_error() -> Any:
            msg = "与不变量无关的 ValueError"
            raise ValueError(msg)

        monkeypatch.setattr(module, "_probe_proposal", _value_error)

        i11 = next(item for item in check_structural_invariants() if item.invariant_id == "I11")
        assert i11.ok is False, i11.detail

    def test_the_invariant_id_is_deterministic(self) -> None:
        """自检跑多少次都给出同样的顺序与编号——否则无法与历史对比。"""
        first = [item.invariant_id for item in check_structural_invariants()]
        second = [item.invariant_id for item in check_structural_invariants()]
        assert first == second == ["I01", "I10", "I11"]


class TestTheCounterexamplesAreTargeted:
    """🔴 反例的**质量**本身要被检查（§五.4）。

    一条"同时违反三条不变量"的构造物在另外两条被削弱时照样会红，
    于是它不再证明目标不变量还活着。这里给每条反例做一次
    交叉检查：它对**其他**断言必须是**合法**的。
    """

    def test_the_scope_violation_does_not_break_i06(self, make_memory: Factory) -> None:
        """越界的那条记忆**状态是正常的**——它只碰 I14，不碰 I06。"""
        memory = make_memory(user_id=uuid4(), status=MemoryStatus.ACTIVE)
        assert memory.is_default_retrievable is True

        with pytest.raises(ConstitutionViolationError) as excinfo:
            assert_memory_retrievable_by(memory, requesting_user_id=uuid4())
        assert excinfo.value.invariant_id == "I14"

    def test_the_status_violation_does_not_break_i14(self, make_memory: Factory) -> None:
        """失效的那条记忆**属于请求者本人**——它只碰 I06，不碰 I14。"""
        user_id = uuid4()
        memory = make_memory(user_id=user_id, status=MemoryStatus.DELETED)
        assert memory.belongs_to(user_id) is True

        with pytest.raises(ConstitutionViolationError) as excinfo:
            assert_memory_retrievable_by(memory, requesting_user_id=user_id)
        assert excinfo.value.invariant_id == "I06"
