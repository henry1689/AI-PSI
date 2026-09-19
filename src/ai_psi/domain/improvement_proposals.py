"""改进提案。

🔴 **本模块是"受控迭代"边界的核心，承载不变量 10 与 11。**

* **不变量 11**：``ImprovementProposal`` 不能自动生效。
  实现方式不是"加一道审批"，而是**在类型里根本不存在 ACTIVE**——
  ``ProposalStatus`` 没有这个成员，也不存在通往"生效"的路径。
  只要类型里没有，就不可能有代码把它设进去。

* **不变量 10**：用户个体经验不能自动升级为全局策略。
  实现为 :data:`PROPOSAL_ESCALATION_THRESHOLD` 与
  :meth:`ImprovementProposal.meets_escalation_threshold`。

**单次普通错误只创建 Experience，不生成 Proposal**（任务书 §11.3）。
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from pydantic import Field, model_validator

from ai_psi.domain.common import EntityMetadata
from ai_psi.domain.enums import ApprovalLevel, ErrorType, ProposalStatus
from ai_psi.domain.exceptions import ConstitutionViolationError

__all__ = [
    "PROPOSAL_ESCALATION_THRESHOLD",
    "TERMINAL_PROPOSAL_STATUSES",
    "ImprovementProposal",
    "active_pattern_key",
    "assert_status_is_a_member",
]

#: 已到达终态的提案状态。
#:
#: 🔴 **由 :meth:`ProposalStatus.is_terminal` 派生，不是另写一份名单。**
#: 两份名单迟早会分家，而分家的表现是"某个状态到底算不算终态"
#: 在两个后端上答案不同——那正是 R72 这类缺陷的温床。
#:
#: 用途：阶段 7 · R72 的**活跃提案唯一性**要排除终态
#: （``uq_improvement_proposals_active_pattern`` 的部分谓词、
#: 两个后端的 ``find_active_for_pattern``）。
TERMINAL_PROPOSAL_STATUSES: Final[frozenset[ProposalStatus]] = frozenset(
    item for item in ProposalStatus if item.is_terminal
)


def active_pattern_key(proposal: ImprovementProposal) -> tuple[str, str] | None:
    """提案的**业务模式键**：``(error_class, applicability[0])``。

    🔴 **这是"同一个模式"在全仓库的唯一定义**（阶段 7 · R72）。

    三个地方必须用同一个键，否则唯一性会在缝合处漏掉：

    * ``LearningService._covered_keys()``（快速路径的读）；
    * PostgreSQL 的 ``uq_improvement_proposals_active_pattern``
      索引表达式 ``(applicability[1])``（**下标从 1 起**，见
      :data:`~ai_psi.infrastructure.db.models.ACTIVE_PATTERN_INDEX_NAME`）；
    * 内存 Store 的提交时复核。

    ⚠️ 取的是 ``applicability[0]`` 而**不是整个数组**：整数组会把
    ``['a','b']`` 与 ``['a']`` 判成两个键，而应用层认为它们是同一个。

    Args:
        proposal: 待取键的提案。

    Returns:
        业务键；``applicability`` 为空时返回 ``None``
        ——这类提案**不覆盖任何模式**，因此不参与唯一性
        （索引的部分谓词同样排除它们）。
    """
    if not proposal.applicability:
        return None
    return (proposal.error_class.value, proposal.applicability[0])


def assert_status_is_a_member(proposal: ImprovementProposal) -> None:
    """🔴 拒绝任何**不是** :class:`ProposalStatus` 成员的状态值。

    本模块的文档说"只要类型里没有 ACTIVE，就不可能有代码把它设进去"。
    这句话在一个地方不成立：``model_construct``。

    ```python
    ImprovementProposal.model_construct(status="active")   # 成功
    ```

    ``model_construct`` 会跳过全部校验，构造出一个 ``status`` 是**裸字符串**
    ``"active"`` 的提案。类型注解拦不住它，``model_validate`` 也不会被调用。

    因此"类型里没有"必须再补一道**运行期**的检查，位置就在对象变成
    持久化数据的那一刻——仓储层。内存实现此前会把它原样存下来并读回，
    SQL 实现则会以一个 `AttributeError: 'str' object has no attribute 'value'`
    崩溃——**两种都不是"有意拦截"**。

    Raises:
        ConstitutionViolationError: 状态不是枚举成员。
    """
    # ⚠️ 先赋给 `object` 再判断：直接写 `isinstance(proposal.status, ...)`
    # 会被静态检查判为"恒真"，因而把下面的分支标成不可达——
    # 而这条检查针对的恰恰是**静态检查看不见的那个构造方式**。
    status: object = proposal.status
    if isinstance(status, ProposalStatus):
        return
    msg = (
        f"提案 {proposal.id} 的 status 不是 ProposalStatus 的成员："
        f"{status!r}（类型 {type(status).__name__}）。"
        "这通常意味着对象是用 model_construct 绕开校验构造的——"
        "不变量 11 要求提案状态只能是枚举里那五个值之一"
    )
    raise ConstitutionViolationError(
        msg,
        invariant_id="I11",
        context={"proposal_id": str(proposal.id), "status_repr": repr(status)},
    )


#: 同类错误达到该次数才允许产生提案（任务书 §11.3）。
#: 这是不变量 10 的**数值落点**——改成 1 就等于允许单次经验推广为策略。
PROPOSAL_ESCALATION_THRESHOLD = 3


class ImprovementProposal(EntityMetadata):
    """一条待人工评审的改进提案（任务书 §5.12）。

    🔴 提案的终点是 ``APPROVED_FOR_MANUAL_TRIAL``——
    语义是"批准进行**人工试验**"，**不是"上线"**。
    """

    target_component: str = Field(
        min_length=1,
        description="提案针对的组件，如 prompt:hypothesis_generator / policy:memory_write",
    )

    observed_problem: str = Field(min_length=1, description="观察到的问题")
    error_class: ErrorType = Field(description="错误分类")

    supporting_experience_ids: list[UUID] = Field(
        default_factory=list,
        description="支撑本提案的经验记录。**数量决定它是否够格成为提案**",
    )
    counterexamples: list[str] = Field(
        default_factory=list,
        description="反例——改进不能只看支持证据",
    )

    proposed_change: str = Field(min_length=1, description="建议的改动")
    expected_benefit: str = Field(min_length=1, description="预期收益")

    possible_regressions: list[str] = Field(
        default_factory=list,
        description=(
            "可能的退化。对应评测中的「其他场景退化程度」——"
            "只改善目标指标却让无关场景变差的改动是净负面改动"
        ),
    )
    applicability: list[str] = Field(default_factory=list, description="适用范围")

    evaluation_plan: list[str] = Field(
        default_factory=list,
        description="离线评估计划：Baseline 与 Candidate 如何对比",
    )
    success_metrics: list[str] = Field(
        default_factory=list,
        description="成功指标。**必须包含目标指标以外的场景**",
    )
    rollback_conditions: list[str] = Field(
        default_factory=list,
        description="回滚条件",
    )

    approval_level: ApprovalLevel = Field(
        default=ApprovalLevel.USER_AND_REVIEW,
        description="所需审批级别。V0.1 默认需要用户 + 评审",
    )
    status: ProposalStatus = Field(
        default=ProposalStatus.DRAFT,
        description="🔴 状态空间中没有 ACTIVE，也不存在通往生效的路径（不变量 11）",
    )

    @model_validator(mode="after")
    def _no_self_support(self) -> ImprovementProposal:
        """支撑经验不得重复计数——去重，避免用同一条经验凑够门槛。"""
        if len(set(self.supporting_experience_ids)) != len(self.supporting_experience_ids):
            deduped = list(dict.fromkeys(self.supporting_experience_ids))
            object.__setattr__(self, "supporting_experience_ids", deduped)
        return self

    def meets_escalation_threshold(
        self,
        *,
        threshold: int = PROPOSAL_ESCALATION_THRESHOLD,
    ) -> bool:
        """🔴 **不变量 10**：支撑经验是否已达到升级为提案的门槛。

        任务书 §11.3 还列出其他可触发提案的条件
        （严重错误有明确修复方向、离线评测暴露稳定退化、
        用户纠正显示系统性问题、模块连续低于阈值）。
        这些条件的判定需要额外上下文，由 :mod:`ai_psi.learning` 在
        调用本方法之外单独评估；本方法只负责"同类错误次数"这一条。

        Args:
            threshold: 门槛次数，默认 3。**不得传入 1**——
                那等于允许单次经验推广为全局策略。
        """
        if threshold < 2:
            msg = "提案门槛不得低于 2：单次经验不足以支撑全局策略（不变量 10）"
            raise ValueError(msg)
        return len(set(self.supporting_experience_ids)) >= threshold

    @property
    def is_terminal(self) -> bool:
        """提案是否已到达终态。"""
        return self.status.is_terminal

    @property
    def can_become_active(self) -> bool:
        """🔴 恒为 ``False``（不变量 11）。

        本属性存在是为了让调用方能**显式检查**，而不是靠约定；
        任何依赖它为 ``True`` 的代码路径都是缺陷。
        """
        return False
