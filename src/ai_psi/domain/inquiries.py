"""认知问题（Inquiry）。

Inquiry 的职责是**把关切转化为一个可结束的问题**。

"可结束"是这里的关键词：如果一个问题的范围无边界、没有停止条件，
认知系统就会陷入无限反思。因此 ``scope``、``out_of_scope`` 与
``stop_conditions`` 都是必填且非空的——它们定义了"什么时候算问完了"。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field, field_validator

from ai_psi.domain.common import EntityMetadata
from ai_psi.domain.enums import CognitiveDepth, ExpectedOutputType, InquiryStatus

__all__ = ["Inquiry"]


class Inquiry(EntityMetadata):
    """一个边界清晰、可结束的认知问题（任务书 §5.5）。"""

    concern_id: UUID = Field(description="上游关切")

    question: str = Field(min_length=1, description="核心问题")
    why_it_matters: str = Field(min_length=1, description="为什么值得回答")

    scope: list[str] = Field(min_length=1, description="**在**范围内的内容")
    out_of_scope: list[str] = Field(
        min_length=1,
        description="**明确排除**的范围。与 scope 同等重要——防止问题无限膨胀",
    )

    known_observation_ids: list[UUID] = Field(default_factory=list, description="已知的观察")
    current_belief_ids: list[UUID] = Field(default_factory=list, description="与本题相关的当前信念")

    key_unknowns: list[str] = Field(default_factory=list, description="关键未知")
    ambiguous_concepts: list[str] = Field(default_factory=list, description="需要澄清的概念")
    assumptions_to_check: list[str] = Field(default_factory=list, description="待检验的隐含前提")

    expected_output_type: ExpectedOutputType = Field(default=ExpectedOutputType.DIRECT_ANSWER)
    verification_method: str | None = Field(
        default=None,
        description="如何验证答案；无法验证的问题应显式说明",
    )

    stop_conditions: list[str] = Field(
        min_length=1,
        description="**停止条件（必填）**——满足即结束，防止无限反思",
    )
    reopen_conditions: list[str] = Field(
        default_factory=list,
        description="重新触发条件：什么情况下这个问题值得重开",
    )

    depth_level: CognitiveDepth = Field(
        default=CognitiveDepth.D0,
        description="认知深度。由深度路由决定，模型只能提建议（ADR-0008）",
    )

    status: InquiryStatus = Field(default=InquiryStatus.OPEN)

    @field_validator("scope", "out_of_scope", "stop_conditions")
    @classmethod
    def _no_blank_entries(cls, value: list[str]) -> list[str]:
        """拒绝空白条目——空字符串会让"必填非空"形同虚设。"""
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            msg = "列表元素不得为空字符串或纯空白"
            raise ValueError(msg)
        return cleaned
