"""元认知反思。

元认知回答的问题是：**"该不该继续想下去？"**

任务书 §9.11 要求"规则优先、模型补充"：

* **规则层**（循环次数、新证据、新推理路径、文本相似度、状态漂移、
  预算、停止条件、输出完整性）—— 由 :mod:`ai_psi.reliability` 计算；
* **模型层**（迎合风险、确认偏差、抽象逃逸、虚假平衡、忽视反例、
  不恰当确定）—— 由元认知 Prompt 产出。

🔴 **元认知模型不能调用自身形成无限递归**（任务书 §9.11）。
本对象不持有任何可再次触发元认知的引用，从类型上杜绝了递归。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field, field_validator

from ai_psi.domain.common import EntityMetadata
from ai_psi.domain.enums import MetacognitiveDecision, OrdinalLevel

__all__ = ["Reflection"]


class Reflection(EntityMetadata):
    """一次元认知检查的完整记录（任务书 §5.10）。"""

    cognitive_round_id: UUID = Field(description="所属认知回合")

    # ---- 规则层指标 ----

    new_evidence_present: bool = Field(
        default=False,
        description="本轮是否出现新证据",
    )
    new_reasoning_path_present: bool = Field(
        default=False,
        description="本轮是否出现新推理路径",
    )
    repeated_claim_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "与上一轮判断的重复度，范围 [0, 1]。"
            "保留浮点而非分档：它是相似度**度量**，与置信度语义不同（ADR-0012）"
        ),
    )
    scope_drift_detected: bool = Field(
        default=False,
        description="是否检测到问题范围漂移——原问题被悄悄换成另一个问题",
    )

    # ---- 模型层检查 ----

    confirmation_bias_risk: OrdinalLevel = Field(
        default=OrdinalLevel.VERY_LOW,
        description="确认偏差风险：倾向于寻找支持既有结论的证据",
    )
    user_pleasing_bias_risk: OrdinalLevel = Field(
        default=OrdinalLevel.VERY_LOW,
        description="迎合风险：为让用户满意而放弃分析",
    )
    abstraction_escape_risk: OrdinalLevel = Field(
        default=OrdinalLevel.VERY_LOW,
        description="抽象逃逸风险：用抽象语言掩盖事实不足",
    )

    unsupported_certainty_detected: bool = Field(
        default=False,
        description="是否检测到无依据的确定性表述",
    )
    missing_counterexample_detected: bool = Field(
        default=False,
        description="是否检测到关键反例被忽略",
    )

    # ---- 决策 ----

    stop_condition_reached: bool = Field(
        default=False,
        description="是否已满足 ``Inquiry.stop_conditions`` 中的任一条",
    )
    marginal_value: OrdinalLevel = Field(
        default=OrdinalLevel.MODERATE,
        description="继续思考的边际价值。为 VERY_LOW 时必须停止",
    )

    decision: MetacognitiveDecision = Field(
        default=MetacognitiveDecision.STOP,
        description="内部控制流决策，直接驱动状态机转移（ADR-0010）",
    )
    reasons: list[str] = Field(
        min_length=1,
        description=(
            "**决策理由（必填非空）**。"
            "任务书不变量 19 要求完成回合必须有停止原因；"
            "本字段是该要求在执行层面的落点"
        ),
    )

    @field_validator("reasons")
    @classmethod
    def _no_blank_reasons(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            msg = "决策理由不得为空白条目"
            raise ValueError(msg)
        return cleaned

    @property
    def has_marginal_gain(self) -> bool:
        """继续思考是否还有边际收益。

        任务书 §13.3：没有新证据、没有新推理路径、且重复度高时，
        继续循环不会有任何收获。

        注意：``repeated_claim_score`` 的阈值判定由
        :mod:`ai_psi.reliability.repetition_detector` 负责——
        本属性只暴露原始信号，不内嵌可调阈值（阈值属配置，见 ADR-0008）。
        """
        return self.new_evidence_present or self.new_reasoning_path_present

    def should_force_stop(self, *, repeat_threshold: float) -> bool:
        """防反刍硬规则：是否必须停止（任务书 §13.3）。

        Args:
            repeat_threshold: 重复度阈值，来自配置而非硬编码。

        Returns:
            无新证据、无新推理路径、且重复度超阈值时为 ``True``。
        """
        return (
            not self.new_evidence_present
            and not self.new_reasoning_path_present
            and self.repeated_claim_score >= repeat_threshold
        )
