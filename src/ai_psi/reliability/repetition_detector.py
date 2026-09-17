"""重复度检测 —— 防反刍的客观信号（任务书 §13.3）。

任务书要求比较"连续两轮 Judgment 的**语义或结构**重复度"。
阶段 3 写下这段代码时 V0.1 还没有向量能力，因此用的是
**字符 n-gram 的 Jaccard 相似度**：

* 中文没有词边界，按字符二元组切分比按空格切词更稳；
* Jaccard 对长度差异不敏感，短句与长句不会被结构性判为"完全不同"；
* **完全确定**，可测试、可复现，不依赖任何外部服务。

🔴 这是**客观信号**，不是最终决策。是否因此停止由
:mod:`ai_psi.cognition.metacognition` 的规则层裁决。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

__all__ = [
    "DEFAULT_REPETITION_THRESHOLD",
    "jaccard",
    "normalized_ngrams",
    "repeated_claim_score",
    "signature_for",
]

#: 判定"重复"的默认阈值。
#:
#: 0.85 是**保守标定**：它捕捉的是"逐字重复"（反刍最典型的形态），
#: 而**不是**"改写过但意思相同的重复"。字符二元组对插入与替换很敏感——
#: 插一个词就足以把相似度从 1.0 拉到 0.55 左右。
#:
#: 之所以接受这个局限：漏判的代价是"多想了有限的一轮"（循环上限与预算仍然兜得住），
#: 误判的代价是"系统提前放弃分析，而且给出一个错误的停止理由"。两者不对称。
#:
#: ⚠️ **阶段 5 交付了向量检索，但没有因此改这里。** 默认的向量 Provider
#: 本身就是词面的（``LocalHashingEmbedding``），用它做"语义级重复检测"
#: 名不副实；换成真语义向量之前，改这个阈值只是换一种词面算法而已。
#: 登记于 risks.md R32 与 R41。
#:
#: **阈值属于配置，不属于本模块**——调用方从 Settings 传入。
DEFAULT_REPETITION_THRESHOLD: Final[float] = 0.85

_NGRAM_SIZE: Final[int] = 2


def _normalize(text: str) -> str:
    """去掉空白——换行与缩进差异不该影响重复度判定。"""
    return "".join(char for char in text if not char.isspace())


def normalized_ngrams(text: str, *, size: int = _NGRAM_SIZE) -> frozenset[str]:
    """把文本切成字符 n-gram 集合。

    Args:
        text: 原始文本。
        size: n-gram 长度。

    Returns:
        n-gram 集合；文本过短时返回整体作为一个元素。
    """
    normalized = _normalize(text)
    if not normalized:
        return frozenset()
    if len(normalized) <= size:
        return frozenset({normalized})
    return frozenset(normalized[i : i + size] for i in range(len(normalized) - size + 1))


def jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """两个集合的 Jaccard 相似度。

    Args:
        left: 集合 A。
        right: 集合 B。

    Returns:
        ``[0, 1]`` 的相似度。两个空集视为完全相同（返回 ``1.0``）。
    """
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    union = left | right
    if not union:  # pragma: no cover - 两个非空集合的并集不可能为空
        return 1.0
    return len(left & right) / len(union)


def repeated_claim_score(previous: str | None, current: str) -> float:
    """连续两轮判断文本的重复度。

    Args:
        previous: 上一轮的判断文本；``None`` 表示这是第一轮。
        current: 本轮的判断文本。

    Returns:
        ``[0, 1]``。第一轮返回 ``0.0``——**没有"上一轮"就谈不上重复**，
        把它当成 1.0 会让每一轮的第一次元认知都误判为反刍。
    """
    if previous is None:
        return 0.0
    return jaccard(normalized_ngrams(previous), normalized_ngrams(current))


def signature_for(items: Iterable[str]) -> frozenset[str]:
    """把一组标识（假设 id、证据 id、模块输出哈希）转为可比较的签名集合。

    编排器用它判断"本轮是否出现了新证据 / 新推理路径"。
    """
    return frozenset(items)
