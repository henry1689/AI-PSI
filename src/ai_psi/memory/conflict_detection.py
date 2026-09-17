"""记忆的重复与冲突检查（任务书 §10.1「重复和冲突检查」）。

本模块把两件事**分开**，因为它们的确信程度差了一个数量级：

========================================  ==========================================
检查                                       判据
========================================  ==========================================
**重复**（:func:`find_exact_duplicate`）    归一化后内容完全相同 → **确定**
**疑似冲突**（:func:`find_potential_conflicts`）  向量相似度超阈值 → **仅是线索**
========================================  ==========================================

🔴 **为什么不合并成一个"相似度超阈值就算重复"的检查。**

因为两者触发的动作不同：重复会被**拒绝写入**，而冲突只是被**记录下来**。
拒绝写入是一个有代价的决定——它会让用户再次陈述同一件事时得不到任何反馈。
用一个启发式的分数去驱动一个有代价的动作，等于把"猜"当成"判"。

反过来，**冲突也绝不由这里判定**。"这两条记忆矛盾"是一个关于语义的断言，
而 V0.1 能算的只有"它们很接近"。把相似度高就写成 ``contradicts_ids``
是在**伪造一个事实**（任务书 §14 的不变量 3 正是要防止这类无根据的确定）。

因此本模块的产出是**线索**：相似的那几条是谁、有多像、为什么像。
它们进入记忆事件的负载作为审计材料，供后续回合显式呈现
（任务书 §5.11「冲突不强行合并」），而 ``contradicts_ids``
只接受**显式**的冲突关系（用户纠正、人工标注）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from ai_psi.domain.enums import MemoryType
from ai_psi.domain.memories import Memory

__all__ = [
    "DEFAULT_CONFLICT_SIMILARITY_THRESHOLD",
    "PotentialConflict",
    "find_exact_duplicate",
    "find_potential_conflicts",
    "normalize_for_comparison",
]

#: 判定"疑似冲突"的向量相似度阈值。
#:
#: ⚠️ **该阈值是针对默认的本地哈希向量标定的。**
#: 余弦相似度的绝对水平随向量空间而变——换成语义模型后，
#: 不相关文本的相似度普遍偏高，同一个 0.75 会从"很接近"变成"随便两条都算"。
#: 换 Provider 时必须重新标定，这一点记录在 ADR-0017 的后续项里。
#:
#: 之所以仍然给一个具体数字而不是留空：留空意味着冲突检查默认关闭，
#: 而"默认关闭的检查"在实践中等同于不存在。
DEFAULT_CONFLICT_SIMILARITY_THRESHOLD: Final[float] = 0.75


def normalize_for_comparison(text: str) -> str:
    """把文本归一化到可做**相等比较**的形式。

    去空白、转小写。刻意**不做**同义词、词干化或标点归一——那些都是
    近似手段，而这里要回答的是"是不是同一句话"，
    必须是一个可以给出确定答案的问题。

    Args:
        text: 原始文本。

    Returns:
        归一化后的文本。
    """
    return "".join(char for char in text if not char.isspace()).lower()


@dataclass(frozen=True, slots=True)
class PotentialConflict:
    """一条疑似的冲突线索。

    Attributes:
        memory_id: 与之可能冲突的既有记忆。
        similarity: 向量相似度，已归一到 ``[0, 1]``。
        memory_type: 既有记忆的类型——**类型不同时冲突的可能性大得多**
            （一条 USER_PREFERENCE 与一条 SEMANTIC 说同一件事，
            往往不是矛盾而是"偏好与事实"的正常共存）。
        reason: 可读的线索说明。
    """

    memory_id: UUID
    similarity: float
    memory_type: MemoryType
    reason: str


def find_exact_duplicate(
    *,
    content: str,
    memory_type: MemoryType,
    user_id: UUID | None,
    existing: Sequence[Memory],
) -> Memory | None:
    """在既有记忆中找出与待写入内容**完全相同**的那一条。

    🔴 判据是确定的，不依赖任何向量：归一化后的内容相等、
    类型相同、作用域相同。三条全部满足才算重复。

    为什么不只看内容：同一个用户完全可能既有"喜欢简洁回答"这个**偏好**，
    又有内容相同的**已确认事实**——它们是两件不同的事，
    合并掉会丢掉类型信息。

    Args:
        content: 待写入内容。
        memory_type: 待写入类型。
        user_id: 待写入的作用域。
        existing: 既有记忆（调用方已按作用域检索得到）。

    Returns:
        重复的那条既有记忆；没有则返回 ``None``。
    """
    target = normalize_for_comparison(content)
    for memory in existing:
        if (
            memory.memory_type is memory_type
            and memory.belongs_to(user_id)
            and normalize_for_comparison(memory.content) == target
        ):
            return memory
    return None


def find_potential_conflicts(
    candidates: Sequence[tuple[Memory, float]],
    *,
    threshold: float = DEFAULT_CONFLICT_SIMILARITY_THRESHOLD,
) -> list[PotentialConflict]:
    """从候选里挑出"很像但不同"的记忆作为冲突线索。

    相似度归一化到 ``[0, 1]``（``(cos + 1) / 2``）后再比阈值——
    余弦的原始范围是 ``[-1, 1]``，直接把 0.75 当作阈值意味着
    "夹角小于 41 度"，而归一化之后的 0.75 是"夹角小于 60 度"，
    两者对同一份数据的判定完全不同。这里必须明确用的是哪一种。

    Args:
        candidates: ``(记忆, 余弦相似度)`` 列表。
        threshold: 触发线索的相似度下限。

    Returns:
        按相似度降序、同分按 id 升序排列的线索列表。
    """
    findings: list[PotentialConflict] = []
    for memory, similarity in candidates:
        unit = (similarity + 1.0) / 2.0
        if unit < threshold:
            continue
        findings.append(
            PotentialConflict(
                memory_id=memory.id,
                similarity=unit,
                memory_type=memory.memory_type,
                reason=(
                    f"与既有记忆 {memory.id} 的向量相似度为 {unit:.3f}"
                    f"（阈值 {threshold:.2f}），主题高度接近；"
                    "是否为真冲突需由语义判断，本检查只提供线索"
                ),
            )
        )
    findings.sort(key=lambda finding: (-finding.similarity, str(finding.memory_id)))
    return findings
