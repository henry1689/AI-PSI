"""候选假设。

🔴 **任务书 §5.8 的四条硬约束，全部在代码中强制：**

1. 默认状态为 ``CANDIDATE``；
2. **禁止直接写入事实记忆**——见 :attr:`Hypothesis.is_factual_claim`；
3. 必须允许 ``REJECTED`` / ``UNRESOLVED``（枚举中已存在）；
4. 高风险或高深度问题**至少保留一个非人格化、非心理化解释**（§9.6）——
   见 :meth:`Hypothesis.has_non_agentic_explanation`。

第 4 条是场景 B 的核心：面对"朋友是不是讨厌我"这类问题，
系统必须能提出"他可能只是忙""消息可能没送达"等非心理化解释，
而不是顺着用户的猜测往下推。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field, field_validator

from ai_psi.domain.common import EntityMetadata
from ai_psi.domain.enums import HypothesisCategory, HypothesisStatus, UncertaintyType

__all__ = ["Hypothesis"]


class Hypothesis(EntityMetadata):
    """一个候选解释（任务书 §5.8）。

    🔴 不变量 1：**假设不能直接变成已确认事实。**
    状态枚举中不存在这样的跃迁路径。``SUPPORTED`` 的语义是
    "当前证据支持"，**不是**"事实成立"。
    """

    inquiry_id: UUID = Field(description="所属认知问题")

    statement: str = Field(min_length=1, description="假设的命题内容")

    category: HypothesisCategory = Field(
        default=HypothesisCategory.FACTUAL,
        description="假设类别。用于判定是否构成'非人格化解释'（§9.6）",
    )

    supporting_evidence_ids: list[UUID] = Field(
        default_factory=list, description="支持本假设的证据"
    )
    opposing_evidence_ids: list[UUID] = Field(default_factory=list, description="反对本假设的证据")
    assumption_ids: list[UUID] = Field(default_factory=list, description="本假设依赖的前提")

    predicted_observations: list[str] = Field(
        default_factory=list,
        description="若本假设成立，应当能观察到什么",
    )

    falsification_conditions: list[str] = Field(
        min_length=1,
        description=(
            "**可反驳条件（必填）**。不可被反驳的命题不是假设，是信念宣告。缺失即构造失败"
        ),
    )

    applicability: list[str] = Field(
        default_factory=list,
        description="适用范围：该解释在什么条件下成立",
    )

    uncertainty_type: UncertaintyType = Field(
        default=UncertaintyType.ALETHIC,
        description="不确定性的类型——比'有多不确定'更能指导下一步动作",
    )

    status: HypothesisStatus = Field(
        default=HypothesisStatus.CANDIDATE,
        description="默认候选。🔴 不存在通往'已确认事实'的状态",
    )

    @field_validator("falsification_conditions")
    @classmethod
    def _no_blank_conditions(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            msg = "可反驳条件不得为空白"
            raise ValueError(msg)
        return cleaned

    @property
    def is_non_agentic(self) -> bool:
        """本假设是否构成非人格化、非心理化解释（任务书 §9.6）。"""
        return self.category.is_non_agentic

    @property
    def is_factual_claim(self) -> bool:
        """本假设是否处于"已被当作事实"的状态。

        🔴 恒为 ``False``——假设永远不能成为事实（不变量 1）。
        该方法存在是为了让调用方（如记忆写入策略）能**显式检查**
        并拒绝"把假设写进事实记忆"的尝试，而不是靠约定。
        """
        return False

    def can_be_written_as_fact(self) -> bool:
        """是否允许作为事实写入长期记忆。

        🔴 恒为 ``False``（不变量 1）。任何调用点拿到 ``False``
        都应当拒绝写入，而不是绕过。
        """
        return False

    @classmethod
    def has_non_agentic_explanation(cls, hypotheses: list[Hypothesis]) -> bool:
        """给定一组候选假设中，是否**至少有一个**非人格化解释。

        用于满足任务书 §9.6：高风险或高深度问题必须保留
        至少一个合理替代解释，且不得全部诉诸心理动机。
        """
        return any(h.is_non_agentic for h in hypotheses)
