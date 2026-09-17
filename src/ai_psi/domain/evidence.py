"""证据及其支持/反对关系。

🔴 **同源证据规则**（任务书 §5.6）：
多个转载来源**不能**自动算作多个独立证据。

这是防止"看起来证据很多"的主要机制。十条转自同一篇报道的新闻
在证据计数上应当**只算一条**。它们通过共享的 ``independence_group``
被识别为一组，在置信度计算中按组计权。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field, model_validator

from ai_psi.domain.common import EntityMetadata, UtcDatetimeOptional
from ai_psi.domain.enums import EvidenceDirectness, OrdinalLevel, VerificationStatus

__all__ = ["Evidence"]


class Evidence(EntityMetadata):
    """一条可追溯的支撑材料（任务书 §5.6）。"""

    observation_id: UUID | None = Field(
        default=None,
        description="来源观察；外部资料可能没有对应的 Observation",
    )

    source_uri: str | None = Field(default=None, description="来源地址")
    source_name: str = Field(min_length=1, description="来源名称")

    content_summary: str = Field(
        min_length=1,
        description="内容摘要——存摘要而非全文，避免把外部长文灌进系统",
    )

    published_at: UtcDatetimeOptional = Field(default=None, description="原始发布时间")
    retrieved_at: UtcDatetimeOptional = Field(default=None, description="获取时间")

    independence_group: str | None = Field(
        default=None,
        description=(
            "**同源证据标记**。共享同一组的证据在计数与置信度计算中只按一组计。None 表示该证据独立"
        ),
    )

    reliability: OrdinalLevel = Field(
        default=OrdinalLevel.MODERATE,
        description="来源可靠性",
    )
    directness: EvidenceDirectness = Field(
        default=EvidenceDirectness.DIRECT,
        description="证据与结论的直接程度",
    )
    freshness: OrdinalLevel = Field(
        default=OrdinalLevel.MODERATE,
        description="时效性。与可靠性、直接性并列为**三个独立维度**——"
        "一个可靠来源的过时数据仍然可能不适用",
    )

    supports_claim_ids: list[UUID] = Field(default_factory=list, description="本证据支持的论断")
    opposes_claim_ids: list[UUID] = Field(default_factory=list, description="本证据反对的论断")

    limitations: list[str] = Field(
        default_factory=list,
        description="本证据的局限——没有局限的证据通常意味着分析不到位",
    )
    verification_status: VerificationStatus = Field(default=VerificationStatus.UNVERIFIED)

    @model_validator(mode="after")
    def _no_self_contradiction(self) -> Evidence:
        """同一证据不得既支持又反对同一条论断。

        出现这种数据的唯一原因通常是编码错误，让它在构造时就失败。
        """
        overlap = set(self.supports_claim_ids) & set(self.opposes_claim_ids)
        if overlap:
            msg = (
                "同一证据不能同时支持并反对同一论断，冲突的论断 id: "
                f"{sorted(str(i) for i in overlap)}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_retrieval_order(self) -> Evidence:
        """获取时间不得早于发布时间。"""
        if (
            self.published_at is not None
            and self.retrieved_at is not None
            and self.retrieved_at < self.published_at
        ):
            msg = "retrieved_at 不得早于 published_at"
            raise ValueError(msg)
        return self
