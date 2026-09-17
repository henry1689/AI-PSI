"""用户模型。

⚠️ **本模块是最容易越界的领域对象，因此边界写得最死。**

任务书 §2.3 明确禁止：

* **心理诊断**；
* **自动生成用户稳定人格画像**；
* 从少量对话推断价值观；
* 根据少量互动得出稳定人格结论。

🔴 **不变量 13**：用户模型中的推测不得标记为确认事实。
实现方式同不变量 11——**``UserModelStatus`` 里根本不存在 ``CONFIRMED``**。
只要类型里没有，就没有代码能把它设进去。

用户模型条目只描述**可观察的行为模式**与**用户自己陈述的内容**，
不描述"用户是什么样的人"。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field, model_validator

from ai_psi.domain.common import EntityMetadata, UtcDatetime, UtcDatetimeOptional
from ai_psi.domain.enums import ConfidenceBand, UserModelStatus

__all__ = ["UserModel"]


class UserModel(EntityMetadata):
    """关于用户的一条**可修正的**认识（任务书 §5.3 相关约束）。

    🔴 所有条目都必须附带证据，且都可以被用户查看与删除。
    """

    user_id: UUID = Field(description="归属用户")

    attribute: str = Field(
        min_length=1,
        description=(
            "属性名。应当是**行为层面**的（如 'preferred_response_length'），"
            "而不是人格层面的（如 'personality_type'）"
        ),
    )
    value: str = Field(min_length=1, description="该属性的取值")

    evidence_ids: list[UUID] = Field(
        min_length=1,
        description=("**支撑证据（必填非空）**。没有证据的用户模型条目就是刻板印象——不允许存在"),
    )

    confidence_band: ConfidenceBand = Field(
        default=ConfidenceBand.LOW,
        description="置信档位。用户模型的置信度上限通常远低于事实判断",
    )

    status: UserModelStatus = Field(
        default=UserModelStatus.HYPOTHESIZED,
        description=(
            "🔴 状态空间中**不存在 CONFIRMED**（不变量 13）。"
            "最高只能到 USER_STATED——即'用户自己这么说过'，"
            "这仍然不等于'关于用户的事实'"
        ),
    )

    valid_from: UtcDatetime
    valid_until: UtcDatetimeOptional = Field(
        default=None,
        description="失效时间。用户会变化，过期的用户模型必须能被识别",
    )

    @model_validator(mode="after")
    def _check_validity_window(self) -> UserModel:
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            msg = "valid_until 必须晚于 valid_from"
            raise ValueError(msg)
        return self

    @property
    def is_confirmable(self) -> bool:
        """🔴 恒为 ``False``（不变量 13）。

        用户模型条目永远不能被标记为"确认事实"。
        本属性存在是为了让调用方显式检查，而不是靠约定。
        """
        return False
