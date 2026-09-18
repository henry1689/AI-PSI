"""上下文构建（任务书 §9.3）。

🔴 **不允许把全部历史塞给模型。**

上下文是有成本的，也是有风险的：无关材料会让模型跑偏，
过期材料会让它说出已经失效的结论，另一用户的材料则是隐私事故。
因此本模块做的事是**选择**，而不是**搬运**：

1. 按主题相关性、来源可靠性、时效性、有效性打分；
2. 在 ``max_retrieved_memories`` 与 ``max_context_tokens`` 双重约束下裁剪；
3. 显式保留三类**通常会被裁掉、但必须出现**的材料：
   冲突信息、已失效信息的状态、用户纠正（§9.3 明确要求）。

第 3 条是本模块存在的主要理由。"只挑最相关的"这个策略天然会把
冲突材料和已失效材料筛掉——而它们恰恰是判断"能不能下确定结论"的关键。

本模块是**纯函数**，不检索、不写库。检索（需要 user_id 作用域校验）
由应用层完成，本模块只做选择与组装。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ai_psi.domain.enums import (
    EvidenceDirectness,
    MemoryStatus,
    OrdinalLevel,
    TrustLevel,
    VerificationStatus,
)
from ai_psi.domain.evidence import Evidence
from ai_psi.domain.memories import Memory
from ai_psi.domain.observations import Observation
from ai_psi.reliability.repetition_detector import jaccard, normalized_ngrams

__all__ = [
    "ContextBuilder",
    "ContextBundle",
    "ContextItem",
    "ContextItemKind",
    "estimate_context_tokens",
]

#: 上下文长度的粗略估算：4 字符 ≈ 1 token。
#:
#: 与 :func:`ai_psi.prompts.registry.estimate_tokens` 同一口径，
#: 这样"上下文预算"与"提示词上限"才不会各说各话。
_CHARS_PER_TOKEN: Final[int] = 4

#: 判定"高可信冲突"的信任门槛：达到该级别才计入冲突信号。
_HIGH_TRUST: Final[frozenset[TrustLevel]] = frozenset({TrustLevel.HIGH, TrustLevel.VERIFIED})


class ContextItemKind(StrEnum):
    """上下文条目的来源类别。"""

    OBSERVATION = "observation"
    EVIDENCE = "evidence"
    MEMORY = "memory"


class ContextItem(BaseModel):
    """一条被选入上下文的材料。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ContextItemKind
    ref_id: UUID
    summary: str = Field(min_length=1)

    trust_level: TrustLevel = TrustLevel.MEDIUM
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    directness: EvidenceDirectness = EvidenceDirectness.INFERRED
    freshness: OrdinalLevel = OrdinalLevel.MODERATE

    independence_group: str | None = None
    is_superseded_or_expired: bool = False
    in_conflict: bool = False
    relevance: float = Field(default=0.0, ge=0.0, le=1.0)

    @property
    def is_high_trust(self) -> bool:
        """是否属于高可信材料（冲突判定只看这一类）。"""
        return self.trust_level in _HIGH_TRUST


class ContextBundle(BaseModel):
    """交给下游分析器的上下文（任务书 §9.3 的五项必含内容）。

    Attributes:
        items: 入选的全部材料。
        supporting: 支持当前方向的信息摘要。
        conflicting: 相关冲突——**必须出现**，否则会掩盖"证据不一致"这一事实。
        superseded: 已失效信息的状态说明。
        corrections: 用户纠正记录。
        uncertainties: 不确定项。
        dropped_count: 因预算被裁剪掉的条目数。
            暴露它是为了"没有静默截断"——被裁掉多少是审计信息。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ContextItem, ...] = ()
    supporting: tuple[str, ...] = ()
    conflicting: tuple[str, ...] = ()
    superseded: tuple[str, ...] = ()
    corrections: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    dropped_count: int = Field(default=0, ge=0)
    estimated_tokens: int = Field(default=0, ge=0)

    @property
    def item_count(self) -> int:
        """入选条目数。"""
        return len(self.items)

    @property
    def has_high_trust_conflict(self) -> bool:
        """是否存在**高可信材料之间**的冲突。

        这是不变量 3 与置信度上限的输入：低可信材料的冲突
        （例如两条都不可靠的转述互相矛盾）不足以压低结论强度。
        """
        return any(item.in_conflict and item.is_high_trust for item in self.items)

    @property
    def evidence_count(self) -> int:
        """上下文中的证据条数。"""
        return sum(1 for item in self.items if item.kind is ContextItemKind.EVIDENCE)

    @property
    def independent_source_count(self) -> int:
        """独立来源组数。

        🔴 同源证据只算一组：十条转载自同一篇报道的新闻
        在证据计数上就是**一条**（任务书 §5.6）。
        """
        groups: set[str] = set()
        singles = 0
        for item in self.items:
            if item.independence_group is None:
                singles += 1
            else:
                groups.add(item.independence_group)
        return len(groups) + singles

    def summarize_for_prompt(self, *, limit: int = 12) -> tuple[str, ...]:
        """生成给提示词用的材料摘要（按相关度降序）。

        Args:
            limit: 最多返回多少条。

        Returns:
            摘要文本元组。
        """
        ranked = sorted(self.items, key=lambda item: item.relevance, reverse=True)
        return tuple(f"[{item.kind.value}] {item.summary}" for item in ranked[:limit])


def estimate_context_tokens(texts: tuple[str, ...]) -> int:
    """估算一组文本的 token 数。"""
    return sum(max(1, len(text) // _CHARS_PER_TOKEN) for text in texts)


class ContextBuilder:
    """上下文选择器。"""

    def __init__(
        self,
        *,
        max_items: int,
        max_tokens: int,
        min_relevance: float = 0.0,
    ) -> None:
        """初始化。

        Args:
            max_items: 最多选入多少条（对应 ``max_retrieved_memories``）。
            max_tokens: 估算长度上限（对应 ``max_context_tokens``）。
            min_relevance: 相关度下限；低于此值的材料不入选。
        """
        self._max_items = max_items
        self._max_tokens = max_tokens
        self._min_relevance = min_relevance

    def build(
        self,
        *,
        question: str,
        observations: tuple[Observation, ...] = (),
        memories: tuple[Memory, ...] = (),
        evidence: tuple[Evidence, ...] = (),
        corrections: tuple[str, ...] = (),
    ) -> ContextBundle:
        """选择并组装上下文。

        Args:
            question: 当前认知问题，用于计算相关度。
            observations: 本回合可用的观察。
            memories: **已按 user_id 作用域过滤**的候选记忆。
                本方法不做权限判断——那是应用层的职责，
                把权限判断放在这里会让"忘记加过滤条件"变成静默泄漏。
            evidence: 可用证据。
            corrections: 用户纠正记录。

        Returns:
            组装好的上下文。
        """
        query_grams = normalized_ngrams(question)
        candidates = [
            *(_observation_item(obs, query_grams) for obs in observations),
            *(_memory_item(mem, query_grams) for mem in memories),
            *(_evidence_item(ev, query_grams) for ev in evidence),
        ]

        # 冲突标记：同一论断上既有支持又有反对的证据
        conflicting_ids = _conflicting_claim_ids(evidence)
        marked = [
            item.model_copy(update={"in_conflict": True})
            if item.ref_id in conflicting_ids
            else item
            for item in candidates
        ]

        eligible = [item for item in marked if item.relevance >= self._min_relevance]
        # 相关度降序；同分时让冲突条目排在前面——它们更不该被裁掉
        ranked = sorted(eligible, key=lambda item: (item.relevance, item.in_conflict), reverse=True)

        selected, dropped = self._trim(ranked)

        # 🔴 已失效材料与冲突材料**不参与裁剪**（§9.3 明确要求它们出现）：
        # 它们的价值不在于"相关"，而在于"说明了当前证据的边界"。
        forced = [item for item in marked if item.is_superseded_or_expired or item.in_conflict]
        selected_ids = {item.ref_id for item in selected}
        for item in forced:
            if item.ref_id not in selected_ids:
                selected.append(item)
                selected_ids.add(item.ref_id)

        summaries = tuple(item.summary for item in selected)
        return ContextBundle(
            items=tuple(selected),
            supporting=tuple(item.summary for item in selected if not item.in_conflict),
            conflicting=tuple(item.summary for item in selected if item.in_conflict),
            superseded=tuple(
                f"{item.summary}（状态：已失效/被取代，不作为当前有效依据）"
                for item in selected
                if item.is_superseded_or_expired
            ),
            corrections=corrections,
            uncertainties=tuple(text for text in corrections if "不确定" in text),
            dropped_count=dropped,
            estimated_tokens=estimate_context_tokens(summaries),
        )

    def _trim(self, ranked: list[ContextItem]) -> tuple[list[ContextItem], int]:
        """按条数与长度双重约束裁剪。

        Returns:
            ``(入选条目, 被裁剪条数)``。
        """
        selected: list[ContextItem] = []
        tokens = 0
        for item in ranked:
            if len(selected) >= self._max_items:
                break
            item_tokens = estimate_context_tokens((item.summary,))
            if tokens + item_tokens > self._max_tokens:
                break
            selected.append(item)
            tokens += item_tokens
        return selected, len(ranked) - len(selected)


def _observation_item(observation: Observation, query_grams: frozenset[str]) -> ContextItem:
    """把观察转换为上下文条目。"""
    return ContextItem(
        kind=ContextItemKind.OBSERVATION,
        ref_id=observation.id,
        summary=observation.content,
        trust_level=observation.trust_level,
        verification_status=observation.verification_status,
        directness=observation.directness,
        freshness=OrdinalLevel.HIGH,
        relevance=jaccard(query_grams, normalized_ngrams(observation.content)),
    )


def _memory_item(memory: Memory, query_grams: frozenset[str]) -> ContextItem:
    """把记忆转换为上下文条目。

    🔴 已失效/被取代的记忆**仍然可能入选**，但会被标记——
    "这个结论曾经成立、现在已失效"是有价值的信息，
    直接丢弃反而会让系统重复犯同一个错。
    """
    return ContextItem(
        kind=ContextItemKind.MEMORY,
        ref_id=memory.id,
        summary=memory.content,
        trust_level=TrustLevel.MEDIUM,
        verification_status=memory.verification_status,
        directness=EvidenceDirectness.INFERRED,
        freshness=(
            OrdinalLevel.LOW if memory.status is not MemoryStatus.ACTIVE else OrdinalLevel.HIGH
        ),
        is_superseded_or_expired=not memory.status.is_default_retrievable,
        relevance=jaccard(query_grams, normalized_ngrams(memory.content)),
    )


def _evidence_item(evidence: Evidence, query_grams: frozenset[str]) -> ContextItem:
    """把证据转换为上下文条目。

    🔴 ``Evidence.reliability`` 必须映射到 ``ContextItem.trust_level``——
    否则"高可信材料之间的冲突"永远判不出来（所有证据都会是中可信），
    而不变量 3 与置信度上限都依赖这个判断。
    """
    return ContextItem(
        kind=ContextItemKind.EVIDENCE,
        ref_id=evidence.id,
        summary=f"{evidence.source_name}：{evidence.content_summary}",
        trust_level=_trust_from_reliability(evidence.reliability),
        verification_status=evidence.verification_status,
        directness=evidence.directness,
        freshness=evidence.freshness,
        independence_group=evidence.independence_group,
        relevance=jaccard(query_grams, normalized_ngrams(evidence.content_summary)),
    )


def _trust_from_reliability(reliability: OrdinalLevel) -> TrustLevel:
    """把证据的来源可靠性映射为信任等级。

    ``VERIFIED`` 刻意**不可达**：核验状态由
    :attr:`~ai_psi.domain.evidence.Evidence.verification_status` 单独承载，
    来源可靠不等于内容已被核验。
    """
    if reliability.rank >= OrdinalLevel.HIGH.rank:
        return TrustLevel.HIGH
    if reliability.rank >= OrdinalLevel.MODERATE.rank:
        return TrustLevel.MEDIUM
    return TrustLevel.LOW


def _conflicting_claim_ids(evidence: tuple[Evidence, ...]) -> frozenset[UUID]:
    """返回同时被支持与反对的论断 id 所对应的证据 id。

    判定"冲突"的依据是证据自己在 ``supports_claim_ids`` /
    ``opposes_claim_ids`` 上的声明，而不是文本相似度——
    后者会把"同一个论断的两种表述"误判成冲突。
    """
    supported: set[UUID] = set()
    opposed: set[UUID] = set()
    for item in evidence:
        supported.update(item.supports_claim_ids)
        opposed.update(item.opposes_claim_ids)
    contested = supported & opposed
    if not contested:
        return frozenset()
    return frozenset(
        item.id
        for item in evidence
        if contested & (set(item.supports_claim_ids) | set(item.opposes_claim_ids))
    )


# ⚠️ 这里曾经有一个 ``ContextSelectionStats``（"上下文选择的统计，
# 用于观测指标"）。它**全仓零引用**——没有生产者、没有消费者、
# 也不在任何 ``__all__`` 里，连测试都没碰过它。
#
# 阶段 6.5 §八 评审 A 把它找出来之后，按 §四 自己定的三选一
# （接入 / 删除 / 登记待办）选了**删除**：一个没人算、没人读的
# "观测指标"不是待办，是一段会让人以为"已经有这个指标了"的代码。
# 真正需要它的时候，它会跟着**第一个消费者**一起出现。
