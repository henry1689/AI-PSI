"""认知回合的暂定判断。

``Judgment`` 是一次认知回合的**结论**，也是回答渲染的唯一依据。

🔴 **``rationale_summary`` 是结构化理由摘要，不是隐藏思维链。**
它应当是简短的、可向用户展示的理由条目
（如"物理常识支持""该结论在标准条件下成立"），
而不是模型的内部推理流（`docs/cognitive_constitution.md` 红线一）。

🔴 **不变量 3 的实现位置**（任务书 §14）：
*存在高可信冲突时，不得输出无保留的确定结论。*
本模块把该规则实现为：``EpistemicAction.ANSWER``（无保留结论）
要求 ``unresolved_unknowns`` 为空——因为存在未解决未知时，
任何"无保留"的表述都超出了证据允许的范围。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field, field_validator, model_validator

from ai_psi.domain.common import EntityMetadata
from ai_psi.domain.enums import ConfidenceBand, EpistemicAction, UncertaintyType

__all__ = ["Judgment"]


class Judgment(EntityMetadata):
    """一次认知回合的暂定结论（任务书 §5.9）。"""

    inquiry_id: UUID = Field(description="所属认知问题")

    selected_hypothesis_ids: list[UUID] = Field(
        default_factory=list,
        description="被采纳的假设。空表示没有足够依据采纳任何一个",
    )

    conclusion: str = Field(min_length=1, description="暂定结论")
    rationale_summary: list[str] = Field(
        min_length=1,
        description="**结构化理由摘要**（非隐藏思维链）。必须说明结论建立在什么之上",
    )
    strongest_counterarguments: list[str] = Field(
        default_factory=list,
        description=(
            "最强反证。**允许为空**——对'水在标准大气压下的沸点'这类问题，"
            "强行制造反方观点正是任务书 §9.8 禁止的'机械地双方都有道理'。"
            "但若存在真实反证而此处为空，属于分析缺陷"
        ),
    )
    unresolved_unknowns: list[str] = Field(
        default_factory=list,
        description="未解决的未知。非空时不得给出无保留结论（不变量 3）",
    )

    applicability: list[str] = Field(
        default_factory=list,
        description="适用范围",
    )

    confidence_band: ConfidenceBand = Field(
        default=ConfidenceBand.MODERATE,
        description="判断强度档位",
    )
    confidence_basis: list[str] = Field(
        min_length=1,
        description="**置信度依据（必填非空）**。没有依据的置信度只是语气",
    )

    revision_conditions: list[str] = Field(
        default_factory=list,
        description="什么情况下这个判断应当被修正",
    )

    recommended_epistemic_action: EpistemicAction = Field(
        default=EpistemicAction.ANSWER_WITH_CAVEAT,
        description="下一认知动作 / 对用户的认知建议（ADR-0010）",
    )

    uncertainty_type: UncertaintyType = Field(
        default=UncertaintyType.ALETHIC,
        description="不确定性的类型。用于区分「信息不足」与「推理存疑」",
    )

    @field_validator("rationale_summary", "confidence_basis")
    @classmethod
    def _no_blank_entries(cls, value: list[str]) -> list[str]:
        """拒绝空白条目——空字符串会让"必填非空"形同虚设。"""
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            msg = "列表元素不得为空字符串或纯空白"
            raise ValueError(msg)
        return cleaned

    @model_validator(mode="after")
    def _enforce_invariant_3(self) -> Judgment:
        """🔴 不变量 3：存在未解决未知时，不得输出无保留的确定结论。"""
        if self.recommended_epistemic_action.allows_strong_conclusion and self.unresolved_unknowns:
            msg = (
                "存在未解决未知时不得给出无保留结论（不变量 3）。"
                "请改用 ANSWER_WITH_CAVEAT / REQUEST_EVIDENCE / DEFER / WAIT / OUT_OF_SCOPE，"
                f"或将 unresolved_unknowns 清空。当前未解决未知: {self.unresolved_unknowns}"
            )
            raise ValueError(msg)
        return self

    @property
    def allows_strong_conclusion(self) -> bool:
        """本判断是否允许在面向用户的回答中使用无保留的确定表述。

        不变量 7 的实现基础：最终回答的结论强度
        **不得高于**内部 Judgment 的结论强度。
        """
        return self.recommended_epistemic_action.allows_strong_conclusion
