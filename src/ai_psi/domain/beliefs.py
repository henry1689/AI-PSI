"""跨回合的稳定信念。

🔴 **不变量 2**（任务书 §14）：
*Belief 必须具有依据，或明确标记为暂定。*

实现为析取校验：``status`` 为 ``TENTATIVE`` 时允许无依据，
**其余任何状态**都必须给出非空的 ``confidence_basis``。

这条规则防的是"拍脑袋的断言"——一个既没有依据、
又没有被标注为暂定的信念，会在后续推理中被当作可靠前提使用。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field, field_validator, model_validator

from ai_psi.domain.common import EntityMetadata, UtcDatetime, UtcDatetimeOptional
from ai_psi.domain.enums import BeliefStatus, BeliefType, ConfidenceBand, UncertaintyType

__all__ = ["Belief"]


class Belief(EntityMetadata):
    """一条有依据的信念（任务书 §5.9）。"""

    user_id: UUID | None = Field(default=None, description="归属用户；None 表示关于世界的一般信念")

    statement: str = Field(min_length=1, description="信念的命题内容")
    belief_type: BeliefType = Field(default=BeliefType.FACTUAL)
    status: BeliefStatus = Field(default=BeliefStatus.TENTATIVE)

    supporting_evidence_ids: list[UUID] = Field(default_factory=list, description="支持证据")
    opposing_evidence_ids: list[UUID] = Field(default_factory=list, description="反对证据")
    assumption_ids: list[UUID] = Field(default_factory=list, description="依赖的前提")

    applicability: list[str] = Field(
        default_factory=list,
        description="适用范围。缺少适用范围的信念是最危险的一种——它会被过度推广",
    )
    uncertainty_type: UncertaintyType = Field(default=UncertaintyType.ALETHIC)

    confidence_band: ConfidenceBand = Field(
        default=ConfidenceBand.LOW,
        description="置信档位。**不是百分比**——模型不应伪造精确概率",
    )
    confidence_basis: list[str] = Field(
        default_factory=list,
        description="**置信度依据**。非 TENTATIVE 状态时必填非空（不变量 2）",
    )

    valid_from: UtcDatetime
    valid_until: UtcDatetimeOptional = Field(
        default=None,
        description="失效时间；超过此时间的信念不应作为当前有效信念使用",
    )

    revision_conditions: list[str] = Field(
        default_factory=list,
        description="什么情况下应当修正这条信念",
    )
    supersedes_id: UUID | None = Field(
        default=None,
        description="被本信念取代的旧信念 id。**旧版本保留**，不就地覆盖（不变量 5）",
    )

    @field_validator("confidence_basis")
    @classmethod
    def _no_blank_basis(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            msg = "置信度依据不得为空白条目"
            raise ValueError(msg)
        return cleaned

    @model_validator(mode="after")
    def _require_basis_or_tentative(self) -> Belief:
        """🔴 不变量 2：必须有依据，或明确标记为暂定。"""
        if self.status is not BeliefStatus.TENTATIVE and not self.confidence_basis:
            msg = (
                f"状态为 {self.status.value} 的信念必须提供非空的 confidence_basis；"
                "若暂无依据，请将 status 设为 TENTATIVE"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_validity_window(self) -> Belief:
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            msg = "valid_until 必须晚于 valid_from"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _no_self_supersede(self) -> Belief:
        if self.supersedes_id is not None and self.supersedes_id == self.id:
            msg = "信念不能取代自身"
            raise ValueError(msg)
        return self

    def is_currently_valid(self, at: UtcDatetime) -> bool:
        """在给定时刻，这条信念是否仍然有效。

        Args:
            at: 查询时刻（必须带时区）。
        """
        if at < self.valid_from:
            return False
        return self.valid_until is None or at < self.valid_until
