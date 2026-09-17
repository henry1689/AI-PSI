"""认知不变量的**运行期自检**（任务书 §14）。

``cognition/constitution.py`` 记录的是"每条不变量**在何处**被强制"，
本模块回答的是另一个问题：**"那些强制手段现在还在不在？"**

这两件事不一样。宪法里写着"``ProposalStatus`` 没有 ACTIVE 成员"，
而当某天有人往枚举里加了一个成员，宪法文档不会自己变——
它依然写着那句话，依然通过所有断言元组数量的测试。
只有一条**运行期检查**能发现结构性的保证被削弱了。

🔴 **本模块检查的是"结构性保证"，不是"某次运行的行为"。**

它能回答"改进提案真的不可能自动生效吗"，不能回答
"这一次的提案评价是否公正"。后者是测试的领域。
把它做成启动自检是有意的：**一个被改坏了的地基应当让进程起不来**，
而不是等到某次请求才以奇怪的方式表现出来。

⚠️ 未覆盖的不变量**不会假装通过**。检查清单里没有的条目，
在报告里显示为"未做运行期检查（由测试覆盖）"，而不是一个绿色的勾。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from ai_psi.cognition.constitution import (
    AUTO_PROMOTION_FORBIDDEN_VALUES,
    INVARIANTS,
    assert_no_automatic_promotion,
)
from ai_psi.domain.enums import ErrorType, HypothesisStatus, ProposalStatus
from ai_psi.domain.exceptions import ConstitutionViolationError
from ai_psi.domain.hypotheses import Hypothesis
from ai_psi.domain.improvement_proposals import (
    PROPOSAL_ESCALATION_THRESHOLD,
    ImprovementProposal,
)

__all__ = [
    "RUNTIME_CHECKED_INVARIANTS",
    "InvariantCheck",
    "check_structural_invariants",
    "failing_checks",
]


@dataclass(frozen=True, slots=True)
class InvariantCheck:
    """一条不变量的运行期检查结果。

    Attributes:
        invariant_id: 形如 ``"I11"`` 的标识。
        statement: 不变量陈述。
        ok: 检查是否通过。
        detail: 检查了什么、结论是什么。
    """

    invariant_id: str
    statement: str
    ok: bool
    detail: str


def _statement_of(invariant_id: str) -> str:
    """从宪法里取回不变量的陈述文本。"""
    for item in INVARIANTS:
        if item.invariant_id == invariant_id:
            return item.statement
    return "（宪法中未登记）"  # pragma: no cover - 只有改动宪法时才会发生


def check_structural_invariants() -> tuple[InvariantCheck, ...]:
    """对全部**可运行期检查**的结构性保证做一次自检。

    Returns:
        检查结果元组，顺序确定（按不变量编号）。
    """
    return (
        _check_i01(),
        _check_i10(),
        _check_i11(),
    )


def failing_checks() -> tuple[InvariantCheck, ...]:
    """返回未通过的自检项。"""
    return tuple(item for item in check_structural_invariants() if not item.ok)


def assert_structural_invariants() -> None:
    """任一结构性保证被削弱时抛错。

    适合放在进程启动路径上：**地基被改坏时进程不该起来**。

    Raises:
        ConstitutionViolationError: 存在未通过的自检项。
    """
    failures = failing_checks()
    if not failures:
        return
    detail = "；".join(f"{item.invariant_id}（{item.detail}）" for item in failures)
    msg = f"认知不变量的结构性保证已被削弱：{detail}"
    raise ConstitutionViolationError(msg, invariant_id=failures[0].invariant_id)


# ---------------------------------------------------------------------------
# 各项检查
# ---------------------------------------------------------------------------


def _check_i01() -> InvariantCheck:
    """I01：Hypothesis 不能直接变成已确认事实。"""
    forbidden = ("confirmed", "verified", "established", "canonical")
    # 🔴 试着**构造**一个"已确认"状态：构造失败本身就是"类型里没有它"的证据
    constructible = sorted(value for value in forbidden if _enum_accepts_hypothesis(value))
    if constructible:
        return InvariantCheck(
            invariant_id="I01",
            statement=_statement_of("I01"),
            ok=False,
            detail=f"可以构造出表示「已确认」的假设状态：{constructible}",
        )

    # 逐个状态验证 can_be_written_as_fact 恒为 False
    smuggling = [status.value for status in HypothesisStatus if _hypothesis_can_become_fact(status)]
    if smuggling:
        return InvariantCheck(
            invariant_id="I01",
            statement=_statement_of("I01"),
            ok=False,
            detail=f"以下状态允许假设被写成事实：{smuggling}",
        )

    return InvariantCheck(
        invariant_id="I01",
        statement=_statement_of("I01"),
        ok=True,
        detail=(
            f"{len(list(HypothesisStatus))} 个假设状态中，无一可被写成已确认事实"
            f"（构造 {forbidden[0]} 会失败）"
        ),
    )


def _enum_accepts_hypothesis(value: str) -> bool:
    """该假设状态名是否真的存在。"""
    try:
        HypothesisStatus(value)
    except ValueError:
        return False
    return True


def _hypothesis_can_become_fact(status: HypothesisStatus) -> bool:
    """构造一个该状态的假设，检查它能否被写成事实。"""
    hypothesis = Hypothesis(
        created_by="invariants_check",
        inquiry_id=_PROBE_UUID,
        statement="运行期自检用的探针假设",
        falsification_conditions=["探针条件"],
        status=status,
    )
    return bool(hypothesis.can_be_written_as_fact())


def _check_i10() -> InvariantCheck:
    """I10：用户个体经验不能自动升级为全局策略。"""
    if PROPOSAL_ESCALATION_THRESHOLD < 2:
        return InvariantCheck(
            invariant_id="I10",
            statement=_statement_of("I10"),
            ok=False,
            detail=(
                f"提案门槛被改为 {PROPOSAL_ESCALATION_THRESHOLD}——"
                "低于 2 等于允许单次经验推广为全局策略"
            ),
        )

    # 门槛本身也必须拒绝小于 2 的取值，否则它只是"当前恰好是 3"
    probe = ImprovementProposal(
        created_by="invariants_check",
        target_component="probe",
        observed_problem="运行期自检",
        error_class=_PROBE_ERROR_TYPE,
        proposed_change="运行期自检",
        expected_benefit="运行期自检",
    )
    try:
        probe.meets_escalation_threshold(threshold=1)
    except ValueError:
        return InvariantCheck(
            invariant_id="I10",
            statement=_statement_of("I10"),
            ok=True,
            detail=f"门槛为 {PROPOSAL_ESCALATION_THRESHOLD}，且拒绝低于 2 的取值",
        )
    return InvariantCheck(
        invariant_id="I10",
        statement=_statement_of("I10"),
        ok=False,
        detail="meets_escalation_threshold 接受了 threshold=1，单次经验可被推广",
    )


def _check_i11() -> InvariantCheck:
    """I11：ImprovementProposal 不能自动生效。

    两层检查，缺一不可：

    1. **类型层**——试着构造一个"已生效"状态。构造失败本身就是
       "``ProposalStatus`` 里没有这个成员"的直接证据，比读一遍成员列表更硬：
       它验证的是运行期行为，而不是我们**以为**枚举里有什么。
    2. **兜底层**——宪法里的断言函数是否还拦得住。类型层已经保证了
       真实状态里构造不出"已生效"，因此这一层只能用一个**鸭子类型的替身**
       来验证：它的存在意义正是"万一类型层被绕过"。
    """
    forbidden = AUTO_PROMOTION_FORBIDDEN_VALUES
    present = sorted(item.value for item in ProposalStatus if item.value in forbidden)
    if present:
        return InvariantCheck(
            invariant_id="I11",
            statement=_statement_of("I11"),
            ok=False,
            detail=f"ProposalStatus 出现了表示「已生效」的成员：{present}",
        )

    probe = ImprovementProposal(
        created_by="invariants_check",
        target_component="probe",
        observed_problem="运行期自检",
        error_class=_PROBE_ERROR_TYPE,
        proposed_change="运行期自检",
        expected_benefit="运行期自检",
    )
    if probe.can_become_active:
        return InvariantCheck(
            invariant_id="I11",
            statement=_statement_of("I11"),
            ok=False,
            detail="ImprovementProposal.can_become_active 为 True",
        )

    constructible = sorted(value for value in forbidden if _enum_accepts(ProposalStatus, value))
    if constructible:
        return InvariantCheck(
            invariant_id="I11",
            statement=_statement_of("I11"),
            ok=False,
            detail=f"可以构造出表示「已生效」的提案状态：{constructible}",
        )

    if not _backstop_rejects_active():
        return InvariantCheck(
            invariant_id="I11",
            statement=_statement_of("I11"),
            ok=False,
            detail="assert_no_automatic_promotion 不再拦截「已生效」状态——兜底层失效",
        )

    return InvariantCheck(
        invariant_id="I11",
        statement=_statement_of("I11"),
        ok=True,
        detail=(
            f"{len(list(ProposalStatus))} 个提案状态中无一可表示「已生效」；"
            f"构造 active 会失败；can_become_active 恒为 False；"
            f"宪法兜底断言仍在（拦下替身）"
        ),
    )


def _enum_accepts(enum_cls: type[ProposalStatus], value: str) -> bool:
    """该枚举能否由 ``value`` 构造出来（即是否真的存在这个成员）。"""
    try:
        enum_cls(value)
    except ValueError:
        return False
    return True


class _FakeActiveStatus:
    """鸭子类型的"已生效状态替身"。

    ``assert_no_automatic_promotion`` 在运行期只读 ``.value``，
    因此这个替身能被喂进去。它的用途是验证**兜底层本身还活着**——
    真实状态里永远构造不出这个值，所以没有别的办法测到那条分支。
    """

    value = "active"


def _backstop_rejects_active() -> bool:
    """宪法兜底断言是否仍能拦下"已生效"状态。"""
    try:
        assert_no_automatic_promotion(_FakeActiveStatus())  # type: ignore[arg-type]
    except ConstitutionViolationError:
        return True
    return False


#: 会被运行期自检覆盖的不变量编号。
#:
#: 🔴 清单之外的不变量**不是自动通过**，只是它们的强制手段无法在运行期
#: 低成本地验证（例如"用户赞同不能把事实改为已验证"要跑一条完整的
#: 反馈路径）。它们由测试覆盖，这一点在自检报告里会明说。
RUNTIME_CHECKED_INVARIANTS: Final[frozenset[str]] = frozenset({"I01", "I10", "I11"})

#: 自检探针用的固定 id 与类别（只存在于内存中，不参与任何持久化）。
_PROBE_UUID: Final[UUID] = UUID(int=0)
_PROBE_ERROR_TYPE: Final[ErrorType] = ErrorType.UNKNOWN_ERROR
