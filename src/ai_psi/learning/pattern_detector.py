"""模式发现（任务书 §11.3「同类错误至少出现 3 次」）。

从一堆经验记录里找出**反复出现**的同类错误。

🔴 **"同类"必须有一个可比较的定义，否则门槛无从判定。**

本模块用 ``(错误类别, 情境签名)`` 作为同类判据：

* **错误类别**——错在哪；
* **情境签名**——在什么样的情形下错的（由
  :func:`ai_psi.application.cognitive_runtime._situation_signature`
  从深度、证据量、假设量派生，**不含问题内容**）。

两者缺一不可。只看类别，"关系推测类问题"与"事实核验类问题"里
各自出现两次推理错误会被并成"推理错误出现四次"——于是生成一个
针对错误的提案。只看情境，同一类问题里"推理错误"与"校准错误"
会被并成"这类问题老出错"——同样无法指导改进。

⚠️ **无法归因的经验（``error_type is None``）不参与分组。**
"不知道错在哪"与"没有错"是两回事，把它们混在一起会让
"未归因"积累成一个看起来像模式的东西。

🔴 **同一个回合的同一个判断，无论被构建几次，都只算一次。**

去重键是 ``(cognitive_round_id, judgment_id)``，**不是 ``Experience.id``**。

这不是学究式的洁癖：``Experience.id`` 是每次 ``ExperienceBuilder.build()``
新生成的 ``uuid4``，因此"同一个回合构建三次"会得到三条 id 互不相同的
经验——按 id 去重的话，**一次错误就能凑满三次的门槛**，而不变量 10
（单次经验不得推广）在这个位置上直接失效。

门槛的语义是"**这个错误在不同的回合里发生过三次**"，不是
"我们手上有三个经验对象"。去重键必须与这句话对齐。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from ai_psi.domain.enums import ErrorType
from ai_psi.domain.experiences import Experience
from ai_psi.domain.improvement_proposals import PROPOSAL_ESCALATION_THRESHOLD

__all__ = ["ErrorPattern", "PatternDetector"]

#: 一份经验至少要有多高的归因置信度才参与模式发现。
#:
#: 归因置信度为 ``VERY_LOW`` 的经验是"我们自己也说不准"的记录。
#: 让它们参与计数，等于允许三条含糊的猜测凑成一个提案——
#: 而提案是**会被人认真评估**的东西，用噪声喂它是在浪费评审的时间，
#: 更糟的是会让评审对提案失去信任。
_MIN_ATTRIBUTION_CONFIDENCE: Final[int] = 1  # == ConfidenceBand.LOW.rank


def _distinct_occurrences(members: Sequence[Experience]) -> dict[tuple[UUID, UUID], Experience]:
    """把经验按 ``(回合, 判断)`` 去重，返回"这件事发生过几次"。

    🔴 **这是门槛的计量单位。** 同一个回合的同一个判断被构建多次，
    在这里坍缩成一次——``Experience.id`` 是每次构建新生成的 uuid4，
    按它去重等于允许用一次错误凑满三次的门槛。

    同一回合内若出现多个判断（当前 V0.1 每回合一个），
    它们会被算作不同的发生——它们确实是不同的认知产物。
    """
    unique: dict[tuple[UUID, UUID], Experience] = {}
    for item in members:
        unique.setdefault((item.cognitive_round_id, item.judgment_id), item)
    return unique


@dataclass(frozen=True, slots=True)
class ErrorPattern:
    """一类反复出现的错误。

    Attributes:
        error_type: 错误类别。
        situation_signature: 情境签名。
        experience_ids: 支持本模式的经验 id（升序，可复现）。
        counterexample_count: 这些经验里**带有反例**的条数。
        applicable_conditions: 从成员经验汇总的适用条件（去重、排序）。
        first_round_id / last_round_id: 最早与最晚的一次，用于判断
            "这个问题还活着吗"——已经很久没再出现的模式优先级更低。
    """

    error_type: ErrorType
    situation_signature: str
    experience_ids: tuple[UUID, ...] = field(default=())
    counterexample_count: int = 0
    applicable_conditions: tuple[str, ...] = field(default=())
    first_round_id: UUID | None = None
    last_round_id: UUID | None = None

    @property
    def count(self) -> int:
        """重复次数。"""
        return len(self.experience_ids)


class PatternDetector:
    """从经验集合里发现重复模式。"""

    def __init__(self, *, threshold: int = PROPOSAL_ESCALATION_THRESHOLD) -> None:
        """初始化。

        Args:
            threshold: 同类错误达到多少次才算一个模式。

        Raises:
            ValueError: 门槛低于 2——单次经验不足以支撑任何模式
                （不变量 10）。
        """
        if threshold < 2:
            msg = "模式门槛不得低于 2：单次经验不构成模式（任务书 §11.3、不变量 10）"
            raise ValueError(msg)
        self._threshold = threshold

    @property
    def threshold(self) -> int:
        """当前门槛。"""
        return self._threshold

    def detect(self, experiences: Sequence[Experience]) -> list[ErrorPattern]:
        """发现重复模式。

        Args:
            experiences: 候选经验（顺序无关——结果按内容排序）。

        Returns:
            达到门槛的模式，按 ``(次数降序, 错误类别, 情境签名)`` 排列。
            排序是确定的：同一批经验在任何一次运行里都得到同样的顺序，
            否则"这次跑出三个提案、下次跑出两个"会变成常态。
        """
        grouped: dict[tuple[ErrorType, str], list[Experience]] = defaultdict(list)
        for experience in experiences:
            if experience.error_type is None:
                continue
            if experience.attribution_confidence.rank < _MIN_ATTRIBUTION_CONFIDENCE:
                continue
            grouped[(experience.error_type, experience.situation_signature)].append(experience)

        patterns = [
            self._build_pattern(error_type, signature, members)
            for (error_type, signature), members in grouped.items()
            if len(_distinct_occurrences(members)) >= self._threshold
        ]
        patterns.sort(
            key=lambda item: (-item.count, item.error_type.value, item.situation_signature)
        )
        return patterns

    def _build_pattern(
        self,
        error_type: ErrorType,
        signature: str,
        members: list[Experience],
    ) -> ErrorPattern:
        """把一组同类经验汇成一个模式。"""
        unique = _distinct_occurrences(members)
        # 🔴 按**发生时间**排序，`first/last` 才真的表示"最早/最晚发生的一次"。
        # 阶段 6 的初版按经验 id 排序，而 id 是 uuid4——所谓"最早/最晚"
        # 实际是"id 字典序最小/最大"，与时间无关。
        # `id` 是并列时的确定排序键（同一微秒内创建的两条经验）。
        ordered = sorted(unique.values(), key=lambda item: (item.created_at, str(item.id)))
        rounds = [item.cognitive_round_id for item in ordered]
        conditions = sorted(
            {condition for item in ordered for condition in item.applicable_conditions}
        )
        return ErrorPattern(
            error_type=error_type,
            situation_signature=signature,
            # 经验 id 本身仍按字典序排列：列在提案里的证据顺序必须可复现，
            # 而"哪条最早"由上面那两个字段回答。
            experience_ids=tuple(sorted((item.id for item in ordered), key=str)),
            counterexample_count=sum(1 for item in ordered if item.counterexamples),
            applicable_conditions=tuple(conditions),
            first_round_id=rounds[0] if rounds else None,
            last_round_id=rounds[-1] if rounds else None,
        )
