"""概念及其定义边界。

概念分析（任务书 §9.5）在 D2 可选、D3/D4 必须运行。

许多看似分歧的争论实际上是**用语分歧**——
双方对同一个词的理解不同。把定义显式化，往往能消解掉一半的"冲突"，
剩下的才是真实分歧。
"""

from __future__ import annotations

from pydantic import Field

from ai_psi.domain.common import EntityMetadata

__all__ = ["Concept"]


class Concept(EntityMetadata):
    """一个被显式定义的概念（任务书 §5.7）。"""

    term: str = Field(min_length=1, description="术语")

    working_definition: str = Field(
        min_length=1,
        description="本次讨论中采用的工作定义——不追求普遍正确，只求当下够用",
    )
    alternative_definitions: list[str] = Field(
        default_factory=list,
        description="其他常见定义。差异往往正是分歧的来源",
    )

    boundaries: list[str] = Field(
        default_factory=list,
        description="边界：什么算它、什么不算它",
    )
    ambiguity_notes: list[str] = Field(
        default_factory=list,
        description="歧义说明：本词在哪些意义上被不同地使用",
    )

    related_concepts: list[str] = Field(default_factory=list, description="相关概念")
    context_scope: list[str] = Field(
        default_factory=list,
        description="该定义适用的语境范围",
    )
