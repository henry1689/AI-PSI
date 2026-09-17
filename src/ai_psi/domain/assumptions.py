"""隐含前提。

前提是推理中**未被说出口但必须成立**的部分。
``necessity`` 字段回答的问题是：**这个前提如果是错的，结论还站得住吗？**

标为 ``CRITICAL`` 的前提应当被优先检验——
如果它不可检验（``UNTESTABLE`` / ``UNFALSIFIABLE``），
那么建立在其上的结论强度必须受到限制。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field

from ai_psi.domain.common import EntityMetadata
from ai_psi.domain.enums import AssumptionNecessity, AssumptionStatus, Testability

__all__ = ["Assumption"]


class Assumption(EntityMetadata):
    """一个推理所依赖的隐含前提（任务书 §5.7）。"""

    statement: str = Field(min_length=1, description="前提的命题内容")
    source: str = Field(
        min_length=1,
        description="前提从何而来：用户陈述、模型补充、常识、系统设定",
    )

    necessity: AssumptionNecessity = Field(
        default=AssumptionNecessity.IMPORTANT,
        description="必要性：它不成立时结论是否还成立",
    )
    testability: Testability = Field(
        default=Testability.TESTABLE_LATER,
        description="可检验性。不可检验的前提必须显式标注，并限制依赖它的结论强度",
    )

    evidence_ids: list[UUID] = Field(
        default_factory=list,
        description="支持该前提的证据",
    )
    status: AssumptionStatus = Field(default=AssumptionStatus.ACTIVE)
