"""认知状态分析（任务书 §9.4）。

任务书要求把上下文分类为八类：``OBSERVED`` / ``SUPPORTED`` / ``TENTATIVE`` /
``CONFLICTING`` / ``UNKNOWN`` / ``POSSIBLY_OUTDATED`` / ``INACCESSIBLE`` /
``OUT_OF_CAPABILITY``，并且"输出不得只给置信度，必须说明依据类型"。

⚠️ **本模块是确定性代码，不是模型调用**（偏差登记：ADR-0015）。

理由：这八类分类完全可以从材料的**元数据**推出来——
信任等级、核验状态、时效性、来源直接性、是否被取代、是否冲突。
把结构化元数据交给模型重新分类，既多花一次调用，
又引入了一个可能出错的环节：模型完全可能把一条
``VERIFIED`` 的证据说成 ``TENTATIVE``，而代码不会。

"依据类型"这一要求反而只有代码能如实满足——因为依据就是元数据本身。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from ai_psi.cognition.context_builder import ContextBundle, ContextItem, ContextItemKind
from ai_psi.domain.enums import (
    EpistemicClassification,
    OrdinalLevel,
    TrustLevel,
    VerificationStatus,
)

__all__ = ["EpistemicAnalysis", "EpistemicAnalyzer", "EpistemicEntry"]

#: 低于该时效性的证据被视为"可能过时"。
_STALE_FRESHNESS: Final[OrdinalLevel] = OrdinalLevel.LOW


class EpistemicEntry(BaseModel):
    """一条材料（或一个未知项）的认知分类。

    ``basis`` 说明**依据类型**，而不是置信度——
    这正是任务书 §9.4 要求的"不得只给置信度"。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    classification: EpistemicClassification
    subject: str = Field(min_length=1)
    basis: str = Field(min_length=1, description="凭什么这样分类（依据类型）")


class EpistemicAnalysis(BaseModel):
    """整个上下文的认知状态画像。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: tuple[EpistemicEntry, ...] = ()
    dropped_material_count: int = Field(default=0, ge=0)

    def count(self, classification: EpistemicClassification) -> int:
        """某一分类的条目数。"""
        return sum(1 for entry in self.entries if entry.classification is classification)

    @property
    def unknown_count(self) -> int:
        """未知项数量。"""
        return self.count(EpistemicClassification.UNKNOWN)

    @property
    def conflicting_count(self) -> int:
        """冲突项数量。"""
        return self.count(EpistemicClassification.CONFLICTING)

    @property
    def has_conflict(self) -> bool:
        """是否存在冲突材料。"""
        return self.conflicting_count > 0

    @property
    def summarised_entries(self) -> tuple[str, ...]:
        """给提示词用的条目摘要（``分类: 内容``）。"""
        return tuple(f"{entry.classification.value}: {entry.subject}" for entry in self.entries)


class EpistemicAnalyzer:
    """上下文材料的认知状态分类器。"""

    def analyze(
        self,
        *,
        bundle: ContextBundle,
        key_unknowns: Sequence[str] = (),
        inaccessible: Sequence[str] = (),
        out_of_capability: Sequence[str] = (),
    ) -> EpistemicAnalysis:
        """分类上下文并补入未知项。

        🔴 **未知项必须显式登记。** 任务书不变量 9 要求
        "D4 哲理分析不能覆盖事实层未知"——如果未知项根本没被记录，
        下游的哲理分析就连"不要覆盖什么"都无从谈起。

        Args:
            bundle: 已选定的上下文。
            key_unknowns: 认知问题中列出的关键未知。
            inaccessible: 当前拿不到的材料（需要权限、需要外部系统等）。
            out_of_capability: 超出系统能力的判断。

        Returns:
            认知状态分析结果。
        """
        entries = [_classify(item) for item in bundle.items]
        entries.extend(
            EpistemicEntry(
                classification=EpistemicClassification.UNKNOWN,
                subject=text,
                basis="认知问题中显式列出的关键未知",
            )
            for text in key_unknowns
            if text.strip()
        )
        entries.extend(
            EpistemicEntry(
                classification=EpistemicClassification.INACCESSIBLE,
                subject=text,
                basis="当前无法获取：不在可访问范围内",
            )
            for text in inaccessible
            if text.strip()
        )
        entries.extend(
            EpistemicEntry(
                classification=EpistemicClassification.OUT_OF_CAPABILITY,
                subject=text,
                basis="超出本系统当前具备的认知能力",
            )
            for text in out_of_capability
            if text.strip()
        )
        return EpistemicAnalysis(
            entries=tuple(entries),
            dropped_material_count=bundle.dropped_count,
        )


def _classify(item: ContextItem) -> EpistemicEntry:
    """把一条上下文材料映射为认知分类。

    判定顺序即优先级：**冲突与失效优先于"我看到了它"**。
    一条被核验过的证据如果已经过时，它的分类必须是
    ``POSSIBLY_OUTDATED`` 而不是 ``SUPPORTED``——
    顺序写反会让系统拿着过期材料下确定结论。
    """
    if item.is_superseded_or_expired:
        return EpistemicEntry(
            classification=EpistemicClassification.POSSIBLY_OUTDATED,
            subject=item.summary,
            basis="该材料已被取代或已超出有效期",
        )
    if item.in_conflict:
        return EpistemicEntry(
            classification=EpistemicClassification.CONFLICTING,
            subject=item.summary,
            basis="同一论断上同时存在支持与反对的材料",
        )
    if item.kind is ContextItemKind.OBSERVATION:
        return EpistemicEntry(
            classification=EpistemicClassification.OBSERVED,
            subject=item.summary,
            basis=f"直接观察（来源直接性：{item.directness.value}）",
        )
    if (
        item.kind is ContextItemKind.EVIDENCE
        and item.verification_status is VerificationStatus.VERIFIED
        and item.trust_level in {TrustLevel.HIGH, TrustLevel.VERIFIED}
    ):
        return EpistemicEntry(
            classification=EpistemicClassification.SUPPORTED,
            subject=item.summary,
            basis=f"已核验的高可信来源（{item.verification_status.value}）",
        )
    if item.freshness.rank <= _STALE_FRESHNESS.rank:
        return EpistemicEntry(
            classification=EpistemicClassification.POSSIBLY_OUTDATED,
            subject=item.summary,
            basis=f"时效性偏低（{item.freshness.value}）",
        )
    return EpistemicEntry(
        classification=EpistemicClassification.TENTATIVE,
        subject=item.summary,
        basis=f"尚未核验（{item.verification_status.value}），只能作为暂定依据",
    )
