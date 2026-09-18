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

from collections.abc import Callable
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
    "EXPECTED_PROPOSAL_STATUSES",
    "RUNTIME_CHECKED_INVARIANTS",
    "InvariantCheck",
    "assert_structural_invariants",
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


#: ``ProposalStatus`` **应当**恰好包含的成员。
#:
#: 🔴 **白名单，不是黑名单。**
#:
#: 初版用 ``AUTO_PROMOTION_FORBIDDEN_VALUES``（``active``/``applied``/
#: ``promoted``/``live`` 四个词）来判断"有没有表示已生效的成员"——
#: 那是一份**四个词的名单**，不是"状态集合必须恰好是这五个"。
#: 往枚举里加一个 ``ENABLED = "enabled"``（语义就是已生效）能同时
#: 骗过类型层与兜底层，而自检照绿、还在 detail 里声称"7 个提案状态中
#: 无一可表示已生效"。
#:
#: 现在改成钉死成员集合：**任何新增都必须在两处同时改**，
#: 而"给提案加一个能生效的状态"这件事就再也无法悄悄发生。
EXPECTED_PROPOSAL_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "draft",
        "pending_evaluation",
        "evaluated",
        "rejected",
        "approved_for_manual_trial",
    }
)


def check_structural_invariants() -> tuple[InvariantCheck, ...]:
    """对全部**可运行期检查**的结构性保证做一次自检。

    🔴 **任何一项检查自己崩掉，都算这项检查未通过。**

    检查的目的是回答"这个保证还在不在"。如果检查代码本身抛了异常，
    答案是"不知道"——而"不知道"必须以**不健康**的形式报出来，
    而不是穿到调用方去。

    初版让非 ``ValueError`` 的异常直接穿出：守卫改成抛
    ``ConstitutionViolationError``（比 ``ValueError`` 更贴切）之后，
    ``/health/cognitive`` 会返回 **500**，而不是把"地基坏了"报成
    ``degraded``——那恰好是这个端点存在的理由。

    Returns:
        检查结果元组，顺序确定（按不变量编号）。
    """
    return (
        _guarded("I01", _check_i01),
        _guarded("I10", _check_i10),
        _guarded("I11", _check_i11),
    )


def _guarded(invariant_id: str, check: Callable[[], InvariantCheck]) -> InvariantCheck:
    """跑一项检查；它自己崩了就是它没通过。"""
    try:
        return check()
    except Exception as exc:
        return InvariantCheck(
            invariant_id=invariant_id,
            statement=_statement_of(invariant_id),
            ok=False,
            detail=(
                f"自检本身执行失败（{type(exc).__name__}: {exc}）——"
                "检查跑不起来，就等于这条保证没有被验证"
            ),
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

    # 🔴 探针必须**同时**验证正向与负向，否则任何一条无关的 ValueError 都能骗过它。
    #
    # 初版只捕获 `threshold=1` 抛出的 ValueError，于是把守卫改成
    # `raise ValueError("提案必须至少有一条支撑经验")`（删掉 threshold<2 的判断）
    # 之后自检照样报绿，detail 还在宣称"拒绝低于 2 的取值"——
    # 它给出了一条**自己没验证过**的断言。
    #
    # 现在三问缺一不可：
    #   1) 单条经验 + threshold=1 → 必须抛（守卫在）；
    #   2) 单条经验 + 默认门槛 → 必须为 False（门槛是 3，不是说 1 会被拒就完事）；
    #   3) 三条经验 + 默认门槛 → 必须为 True（正向路径真的通）。
    one = _probe_proposal(1)
    three = _probe_proposal(PROPOSAL_ESCALATION_THRESHOLD)

    try:
        one.meets_escalation_threshold(threshold=1)
    except ValueError:
        pass
    else:
        return InvariantCheck(
            invariant_id="I10",
            statement=_statement_of("I10"),
            ok=False,
            detail="meets_escalation_threshold 接受了 threshold=1，单次经验可被推广",
        )

    if one.meets_escalation_threshold():
        return InvariantCheck(
            invariant_id="I10",
            statement=_statement_of("I10"),
            ok=False,
            detail=(
                f"单条经验在门槛 {PROPOSAL_ESCALATION_THRESHOLD} 下被判为已达到门槛——"
                "拒绝 threshold=1 只是形式，真正的门槛没有生效"
            ),
        )

    if not three.meets_escalation_threshold():
        return InvariantCheck(
            invariant_id="I10",
            statement=_statement_of("I10"),
            ok=False,
            detail=(
                f"{PROPOSAL_ESCALATION_THRESHOLD} 条经验仍判为未达门槛——"
                "门槛被抬到了没人能过的位置，等于永远不产生提案"
            ),
        )

    return InvariantCheck(
        invariant_id="I10",
        statement=_statement_of("I10"),
        ok=True,
        detail=(
            f"门槛为 {PROPOSAL_ESCALATION_THRESHOLD}：拒绝低于 2 的取值，"
            f"单条不达标，{PROPOSAL_ESCALATION_THRESHOLD} 条达标"
        ),
    )


def _probe_proposal(experience_count: int) -> ImprovementProposal:
    """构造一个带指定条数支撑经验的自检探针。"""
    return ImprovementProposal(
        created_by="invariants_check",
        target_component="probe",
        observed_problem="运行期自检",
        error_class=_PROBE_ERROR_TYPE,
        proposed_change="运行期自检",
        expected_benefit="运行期自检",
        supporting_experience_ids=[UUID(int=index + 1) for index in range(experience_count)],
    )


def _check_i11() -> InvariantCheck:
    """I11：ImprovementProposal 不能自动生效。

    三层检查，缺一不可：

    1. **白名单层**——``ProposalStatus`` 的成员集合必须**恰好**等于
       :data:`EXPECTED_PROPOSAL_STATUSES`。这是主检查：它不认识"已生效"
       这个词，它只认识"集合变了"。
    2. **类型层**——试着构造一个"已生效"状态。构造失败本身就是
       "``ProposalStatus`` 里没有这个成员"的直接证据，比读一遍成员列表更硬：
       它验证的是运行期行为，而不是我们**以为**枚举里有什么。
    3. **兜底层**——宪法里的断言函数是否还拦得住。类型层已经保证了
       真实状态里构造不出"已生效"，因此这一层只能用一个**鸭子类型的替身**
       来验证：它的存在意义正是"万一类型层被绕过"。
    """
    actual = frozenset(item.value for item in ProposalStatus)
    added = sorted(actual - EXPECTED_PROPOSAL_STATUSES)
    removed = sorted(EXPECTED_PROPOSAL_STATUSES - actual)
    if added or removed:
        parts: list[str] = []
        if added:
            parts.append(f"多出 {added}（其中任何一个都可能就是通往「已生效」的那一个）")
        if removed:
            parts.append(f"少了 {removed}")
        return InvariantCheck(
            invariant_id="I11",
            statement=_statement_of("I11"),
            ok=False,
            detail=(
                "ProposalStatus 的成员集合已改变：" + "；".join(parts) + "。"
                "增删状态必须是一个有意的决定——"
                "确认之后同步更新 EXPECTED_PROPOSAL_STATUSES 与数据库 CHECK"
            ),
        )

    constructible = sorted(
        value for value in AUTO_PROMOTION_FORBIDDEN_VALUES if _enum_accepts(ProposalStatus, value)
    )
    if constructible:
        return InvariantCheck(
            invariant_id="I11",
            statement=_statement_of("I11"),
            ok=False,
            detail=f"可以构造出表示「已生效」的提案状态：{constructible}",
        )

    probe = _probe_proposal(1)
    if probe.can_become_active:
        return InvariantCheck(
            invariant_id="I11",
            statement=_statement_of("I11"),
            ok=False,
            detail="ImprovementProposal.can_become_active 为 True",
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
            f"{len(list(ProposalStatus))} 个提案状态与预期集合逐字相等"
            f"（{sorted(EXPECTED_PROPOSAL_STATUSES)}）；"
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
