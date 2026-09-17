"""认知关切。

关切是"值不值得启动一次认知"的判定结果。

🔴 **必须被过滤掉的关切**（任务书 §9.1）：

* 无依据的主动问题；
* 纯粹为了表现聪明的探索；
* 不相关旧记忆引发的联想；
* 未授权的隐私推测。

实现上，``source_event_ids`` 是**必填非空**的——
没有来源的关切本身就不该被创建。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field, field_validator

from ai_psi.domain.common import EntityMetadata, UtcDatetimeOptional
from ai_psi.domain.enums import ConcernCategory, ConcernStatus, OrdinalLevel

__all__ = ["Concern"]


class Concern(EntityMetadata):
    """一个值得投入认知资源的关切（任务书 §5.4）。"""

    source_event_ids: list[UUID] = Field(
        min_length=1,
        description="触发本关切的事件。**必填非空**——无依据的关切不应被创建",
    )

    category: ConcernCategory
    statement: str = Field(min_length=1, description="关切是什么")
    why_it_matters: str = Field(min_length=1, description="为什么它值得投入认知资源")

    related_goal_ids: list[UUID] = Field(
        default_factory=list,
        description=(
            "相关长期目标的 id。V0.1 指向 Memory(memory_type=USER_GOAL)，"
            "无独立 Goal 聚合（ADR-0012）"
        ),
    )

    impact: OrdinalLevel = Field(default=OrdinalLevel.MODERATE, description="影响程度")
    urgency: OrdinalLevel = Field(default=OrdinalLevel.MODERATE, description="紧迫程度")
    uncertainty: OrdinalLevel = Field(default=OrdinalLevel.MODERATE, description="当前不确定程度")

    expected_information_value: OrdinalLevel = Field(
        default=OrdinalLevel.MODERATE,
        description="解决它能带来多少新信息——决定值不值得花预算",
    )
    cognitive_cost: OrdinalLevel = Field(
        default=OrdinalLevel.MODERATE,
        description="解决它需要多少认知预算",
    )

    status: ConcernStatus = Field(default=ConcernStatus.OPEN)
    expiry_at: UtcDatetimeOptional = Field(
        default=None,
        description="关切失效时间；过期的关切不应再启动新回合",
    )

    @field_validator("related_goal_ids", "source_event_ids")
    @classmethod
    def _no_duplicate_refs(cls, value: list[UUID]) -> list[UUID]:
        """去重但保持顺序——重复引用会让"证据计数"虚高。"""
        seen: set[UUID] = set()
        result: list[UUID] = []
        for item in value:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result
