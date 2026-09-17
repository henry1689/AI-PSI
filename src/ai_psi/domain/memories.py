"""长期记忆。

🔴 **本模块承载三条最关键的认知不变量**（任务书 §14）：

* **不变量 6**：被取代的记忆不能作为默认有效记忆返回
  → :attr:`Memory.is_default_retrievable`
* **不变量 14**：记忆检索必须遵守 ``user_id`` 作用域
  → ``user_id`` 字段；检索接口在阶段 5 强制要求该参数
* **不变量 5**：用户纠正必须生成新版本
  → ``supersedes_id`` 版本链；**不就地覆盖**

记忆错误与其他错误不同：**它的后果是累积的**。
一次错误的回答只影响一次交互，一次错误的记忆写入会持续影响此后所有回合。
因此这里的约束比别处更严。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field, model_validator

from ai_psi.domain.common import EntityMetadata, UtcDatetime, UtcDatetimeOptional
from ai_psi.domain.enums import (
    MemoryStatus,
    MemoryType,
    RetentionPolicy,
    SensitivityLevel,
    VerificationStatus,
)

__all__ = ["Memory"]


class Memory(EntityMetadata):
    """一条长期记忆（任务书 §5.11）。

    🔴 **模型不能直接创建可用的 Memory。**
    写入路径必须经过 ``MemoryProposal`` → ``WritePolicy`` → 应用服务
    （ADR-0004）。本对象的存在不代表它已被批准写入。
    """

    user_id: UUID | None = Field(
        default=None,
        description="**作用域键**。检索必须强制携带（不变量 14）。None 表示系统级记忆",
    )

    memory_type: MemoryType
    content: str = Field(min_length=1, description="记忆内容")

    source_event_ids: list[UUID] = Field(
        default_factory=list,
        description="来源事件——记忆必须可追溯到它为什么存在",
    )
    evidence_ids: list[UUID] = Field(default_factory=list, description="支撑证据")

    verification_status: VerificationStatus = Field(
        default=VerificationStatus.UNVERIFIED,
        description=(
            "核验状态。🔴 不变量 4：用户赞同**不能**把它改为 VERIFIED，该状态只能由证据变更驱动"
        ),
    )

    applicability: list[str] = Field(
        default_factory=list,
        description="适用范围",
    )

    sensitivity: SensitivityLevel = Field(
        default=SensitivityLevel.PERSONAL,
        description="**写入前必须分类**（任务书 §17.1）。SENSITIVE 及以上不可自动批准",
    )
    retention_policy: RetentionPolicy = Field(default=RetentionPolicy.USER_CONTROLLED)
    access_scope: list[str] = Field(default_factory=list, description="访问范围")

    valid_from: UtcDatetime
    valid_until: UtcDatetimeOptional = Field(
        default=None,
        description="失效时间；失效后不作为默认有效记忆返回",
    )

    supersedes_id: UUID | None = Field(
        default=None,
        description="被本记忆取代的旧记忆 id。🔴 **旧版本保留**，不就地覆盖（不变量 5）",
    )
    contradicts_ids: list[UUID] = Field(
        default_factory=list,
        description=(
            "与本记忆冲突的其他记忆。**冲突不强行合并**——"
            "冲突本身是有价值的信息，应在后续回合中显式呈现（场景 E）"
        ),
    )

    status: MemoryStatus = Field(default=MemoryStatus.PROPOSED)
    embedding_version: str | None = Field(
        default=None,
        description="生成该记忆向量所用的嵌入模型版本，用于检索兼容性判断",
    )

    @model_validator(mode="after")
    def _no_self_reference(self) -> Memory:
        """取代关系与冲突关系都不得指向自身。"""
        if self.supersedes_id is not None and self.supersedes_id == self.id:
            msg = "记忆不能取代自身"
            raise ValueError(msg)
        if self.id in self.contradicts_ids:
            msg = "记忆不能与自己冲突"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_validity_window(self) -> Memory:
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            msg = "valid_until 必须晚于 valid_from"
            raise ValueError(msg)
        return self

    @property
    def is_default_retrievable(self) -> bool:
        """🔴 **不变量 6**：本记忆是否可作为默认有效结果被检索返回。

        ``SUPERSEDED`` / ``EXPIRED`` / ``DELETED`` / ``REJECTED`` / ``PROPOSED``
        一律**不可**默认返回。只有 ``ACTIVE`` 与 ``DISPUTED`` 可见——
        后者可见是因为"存在争议"本身是有价值的信息，但会在回答中被标注。
        """
        return self.status.is_default_retrievable

    def belongs_to(self, user_id: UUID | None) -> bool:
        """本记忆是否属于给定作用域。

        🔴 **不变量 14**：检索路径必须调用本方法（或等价检查），
        且传入的 ``user_id`` 不得来自不可信输入。
        """
        return self.user_id == user_id

    def superseded_by(self, *, replacement_id: UUID) -> Memory:
        """返回一个被取代后的副本（``status=SUPERSEDED``）。

        🔴 不变量 5：**不就地覆盖**。调用方应持久化本副本，
        并保留原记录的审计轨迹——纠错痕迹本身是重要信息。

        Args:
            replacement_id: 取代它的新记忆 id。
        """
        return self.bumped(
            status=MemoryStatus.SUPERSEDED,
            contradicts_ids=[*self.contradicts_ids, replacement_id],
        )
