"""情境快照。

⚠️ **本对象是 ADR-0012 登记的缺口补充。**
任务书 §4 的目录结构列出了 ``domain/situations.py``，
但 §5 的领域模型章节**没有给出 Situation 的定义**，
只在 ``Experience.situation_signature`` 中隐含地引用了一个字符串签名。

没有情境概念，"同类错误至少出现 3 次"（§11.3 的提案门槛）
就无法被判定——因为"同类"无从比较。因此本模块给出**最小**的情境定义：
它不试图完整建模世界，只提供一个**可比较的签名**与少量显式特征。

刻意保持最小：任何超出"让同类可比"范围的功能都不属于 V0.1（ADR-0012）。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field

from ai_psi.domain.common import EntityMetadata
from ai_psi.domain.enums import CognitiveDepth

__all__ = ["Situation"]


class Situation(EntityMetadata):
    """一次认知发生时的情境（ADR-0012 补充定义）。

    用于让不同回合之间可以判断"是否属于同类问题"，
    从而支撑 :class:`~ai_psi.domain.experiences.Experience` 的模式发现
    与改进提案的门槛判定（任务书 §11.3）。
    """

    user_id: UUID | None = Field(default=None, description="归属用户")
    conversation_id: UUID | None = Field(default=None, description="所属会话")

    situation_signature: str = Field(
        min_length=1,
        description=(
            "情境签名。**必须是可比较的稳定标识**——"
            "同样的情境应当产生同样的签名，否则模式发现会失效。"
            "建议由问题类型 + 深度 + 关键特征派生，而非自由文本"
        ),
    )

    inquiry_type: str = Field(
        min_length=1,
        description="问题类型，如 factual / causal / relational / normative",
    )
    depth_level: CognitiveDepth = Field(
        default=CognitiveDepth.D0,
        description="本次采用的认知深度",
    )

    salient_features: list[str] = Field(
        default_factory=list,
        description="情境的关键特征——决定它属于哪一类问题的判别依据",
    )
    applicable_conditions: list[str] = Field(
        default_factory=list,
        description="该情境分类成立的条件",
    )
    counterexamples: list[str] = Field(
        default_factory=list,
        description="该情境分类的例外情况",
    )
