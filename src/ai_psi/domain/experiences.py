"""经验记录。

经验是"反馈 → 学习"之间的桥梁。

🔴 **关键字段：``evidence_available_at_time``。**

它区分了两种看起来一样、实则完全不同的情况：

* **当时判断错了** —— 信息足够，推理有问题 → ``REASONING_ERROR``
* **当时信息本就不足** —— 判断在当时是合理的 → 不该记为错误

没有这个字段，系统会把所有"后来被推翻的判断"都记成错误，
从而学到错误的教训（比如变得过度保守、或错误地降低某类问题的置信度）。

🔴 **单次经验不产生提案**（任务书 §11.3，不变量 10）。
本对象只是记录，是否升级为提案由 :mod:`ai_psi.learning` 按门槛决定。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field

from ai_psi.domain.common import EntityMetadata
from ai_psi.domain.enums import ConfidenceBand, ErrorType, OrdinalLevel, VerificationStatus

__all__ = ["Experience"]


class Experience(EntityMetadata):
    """一次认知回合的复盘记录（任务书 §5.12）。"""

    cognitive_round_id: UUID = Field(description="来源认知回合")
    judgment_id: UUID = Field(description="当时的判断")

    situation_signature: str = Field(
        min_length=1,
        description=(
            "情境签名，用于判定「同类错误」（见 :class:`~ai_psi.domain.situations.Situation`）"
        ),
    )
    inquiry_type: str = Field(min_length=1, description="问题类型")

    evidence_available_at_time: list[UUID] = Field(
        default_factory=list,
        description=(
            "**判断发生时**就已掌握的证据。"
            "用于区分'推理错误'与'当时信息不足'——"
            "两者看起来一样，但该学到的东西完全不同"
        ),
    )

    predicted_feedback: list[str] = Field(
        default_factory=list,
        description="当时预期的反馈",
    )
    actual_feedback: list[str] = Field(
        default_factory=list,
        description="实际收到的反馈",
    )
    later_evidence_ids: list[UUID] = Field(
        default_factory=list,
        description="判断之后才出现的证据——用于归因",
    )

    error_type: ErrorType | None = Field(
        default=None,
        description="错误分类。None 表示未发现错误，或尚无法归因",
    )
    attribution_confidence: ConfidenceBand = Field(
        default=ConfidenceBand.VERY_LOW,
        description="归因置信度。低置信度的归因不应驱动任何策略变化",
    )

    strategy_used: list[str] = Field(default_factory=list, description="本次采用的分析策略")
    strategy_effectiveness: OrdinalLevel | None = Field(
        default=None,
        description="策略有效性评估",
    )

    applicable_conditions: list[str] = Field(
        default_factory=list,
        description="该经验成立的条件。**缺少条件限制的经验极易被过度推广**",
    )
    counterexamples: list[str] = Field(
        default_factory=list,
        description="反例——记录反例是阻止经验被过度推广的主要手段",
    )

    verification_status: VerificationStatus = Field(default=VerificationStatus.UNVERIFIED)

    @property
    def is_attributable(self) -> bool:
        """本次经验是否足以归因到某个错误类型。

        归因置信度过低时不应生成改进提案——
        否则会产生大量基于噪声的"改进"。
        """
        return (
            self.error_type is not None
            and self.attribution_confidence.rank >= ConfidenceBand.LOW.rank
        )
