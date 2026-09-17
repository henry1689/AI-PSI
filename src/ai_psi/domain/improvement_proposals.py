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

from uuid import UUID

from pydantic import Field, model_validator

from ai_psi.domain.common import EntityMetadata
from ai_psi.domain.enums import ApprovalLevel, ErrorType, ProposalStatus

__all__ = ["PROPOSAL_ESCALATION_THRESHOLD", "ImprovementProposal"]

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
