"""观察对象。

🔴 **Observation 只描述"看见或收到什么"，禁止写心理诊断。**

这条边界是本系统区别于普通聊天机器人的关键之一：

* 「用户回复很短」        → ``Observation``（可观察的事实）
* 「用户情绪低落」        → ``Hypothesis``（推断，不是观察）
* 「朋友只回了一个'嗯'」  → ``Observation``
* 「朋友讨厌用户」        → ``Hypothesis``

把推断写进 Observation，等于让未经验证的猜测获得"事实"的身份，
后续所有推理都会以它为基础。场景 B 专门验证这条边界。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field

from ai_psi.domain.common import EntityMetadata, UtcDatetimeOptional
from ai_psi.domain.enums import (
    EvidenceDirectness,
    SensitivityLevel,
    SourceType,
    TrustLevel,
    VerificationStatus,
)

__all__ = ["Observation"]


class Observation(EntityMetadata):
    """一次可观察到的输入或现象（任务书 §5.3）。"""

    user_id: UUID | None = Field(default=None, description="归属用户；None 表示系统级观察")

    source_type: SourceType
    source_id: str = Field(min_length=1, description="来源标识：消息 id、文档 id 等")

    content: str = Field(min_length=1, description="观察到的内容本身，不加解释")

    observed_at: UtcDatetimeOptional = Field(
        default=None,
        description="现象发生的时间；None 表示与事件记录时间相同",
    )

    directness: EvidenceDirectness = Field(
        default=EvidenceDirectness.DIRECT,
        description="观察的直接程度：亲眼所见 vs 转述",
    )
    trust_level: TrustLevel = Field(default=TrustLevel.MEDIUM)
    verification_status: VerificationStatus = Field(default=VerificationStatus.UNVERIFIED)

    possible_expiry: UtcDatetimeOptional = Field(
        default=None,
        description="该观察可能失效的时间点（如'今天'的天气）",
    )

    limitations: list[str] = Field(
        default_factory=list,
        description="该观察的局限：样本太小、时机特殊、转述失真等",
    )
    sensitivity: SensitivityLevel = Field(default=SensitivityLevel.PERSONAL)
