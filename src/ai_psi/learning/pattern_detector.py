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

## 计数单位：**独立发生次数 × 评价权重**

门槛问的是"**这件事在不同的场合发生过三次**"，不是
"我们手上有三个经验对象"。这两句话的差别是本模块最要紧的地方。

阶段 6 的初版按 ``Experience.id`` 去重，于是"同一个回合抽三次"
就凑满了门槛。当时的修法是改按 ``(cognitive_round_id, judgment_id)``——
但那是**在消费端打补丁**：抽取端仍然可以造出"看起来独立"的经验。

阶段 6.5 §二.12 把这个问题下沉到经验自身（``independence_group``）：

* 同一 ``Idempotency-Key`` 的所有**技术重试**落在同一分组——
  它们是同一次请求，网络抖一下不该算"又发生了一次"；
* 没有幂等键时按回合 id 分组——**事件重放**因此不会造出第二次发生。

§二.6–7 再加一层：**只有 ``SUPPORTED`` / ``CONFIRMED`` 计权。**
内部元认知的怀疑（``SUSPECTED``）权重为 0——它仍然被记录、
仍然出现在"为什么没达门槛"里，但**不能单独支撑一个提案**。

⚠️ **无法归因的经验（``error_type is None``）不参与分组**，
且其评价状态必然是 ``UNASSESSED``。
"不知道错在哪"与"没有错"是两回事，把它们混在一起会让
"未归因"积累成一个看起来像模式的东西。

⚠️ **未达门槛的分组不会被丢掉。** 它们进 :attr:`PatternScan.suppressed`
并带上逐条理由——一份只说"发现 0 个模式"的报告无法回答"为什么没有"，
而那正是下一次运行时最需要知道的事。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from ai_psi.domain.enums import ErrorType, ExperienceEvaluation, ExperienceEvaluator
from ai_psi.domain.experiences import ExperienceAssessment
from ai_psi.domain.improvement_proposals import PROPOSAL_ESCALATION_THRESHOLD
from ai_psi.learning.evaluation_weighting import DEFAULT_WEIGHTING, EvaluationWeighting

__all__ = [
    "ErrorPattern",
    "PatternDetector",
    "PatternScan",
    "SuppressedPattern",
]

#: 一份经验至少要有多高的归因置信度才参与模式发现。
#:
#: 归因置信度为 ``VERY_LOW`` 的经验是"我们自己也说不准"的记录。
#: 让它们参与计数，等于允许三条含糊的猜测凑成一个提案——
#: 而提案是**会被人认真评估**的东西，用噪声喂它是在浪费评审的时间，
#: 更糟的是会让评审对提案失去信任。
_MIN_ATTRIBUTION_CONFIDENCE: Final[int] = 1  # == ConfidenceBand.LOW.rank


def _distinct_occurrences(
    members: Sequence[ExperienceAssessment],
) -> dict[str, ExperienceAssessment]:
    """按 ``independence_group`` 去重，返回"这件事发生过几次"。

    🔴 **这是门槛的计量单位。**

    分组相同 = **不是彼此的独立证据**。同一个回合被构建多次、
    同一次请求被重试多次、同一段事件流被重放多次——三者都会产生
    多个 ``Experience`` 对象，但它们只对应**一次发生**。

    阶段 6 用 ``(cognitive_round_id, judgment_id)`` 去重，那能挡住
    "同一回合重复抽取"，挡不住"同一次请求的两个回合"。分组键
    把两种情形收进同一条规则。

    ⚠️ 同一回合里的两个不同判断现在也**合并成一次发生**了。
    阶段 6 的文档说它们"确实是不同的认知产物"——这句话没错，
    但它们不是**独立**的：同一个回合共享同一份证据、同一次模型调用、
    同一段推理上下文。用它们凑"三次发生"是阶段 6 那个 bug 的
    弱化版，而不是它的反面。
    """
    unique: dict[str, ExperienceAssessment] = {}
    for item in members:
        unique.setdefault(item.experience.independence_group, item)
    return unique


@dataclass(frozen=True, slots=True)
class ErrorPattern:
    """一类反复出现的错误，**已达到门槛**。

    Attributes:
        error_type: 错误类别。
        situation_signature: 情境签名。
        experience_ids: 支持本模式的经验 id（升序，可复现）。
        occurrence_count: 独立发生的次数（去重后的分组数）。
        weighted_count: 按评价状态加权后的计数。
            🔴 **门槛比的是它**，不是 ``occurrence_count``——
            三次内部怀疑的分组数是 3，加权数是 0。
        experience_count: 参与计数的经验对象条数。
            它 ≥ ``occurrence_count``；两者的差就是"重复抽取 /
            重试 / 重放"造出的冗余。把它报出来是为了让冗余可见。
        evaluations: 参与计数的经验的有效评价（去重、按次序排列）。
        evaluator_types: 参与计数经验的评估者类型（去重）。
        independence_groups: 去重后的分组键（升序）。
        counterexample_count: 这些经验里**带有反例**的条数。
        applicable_conditions: 从成员经验汇总的适用条件（去重、排序）。
        first_round_id / last_round_id: 最早与最晚的一次，用于判断
            "这个问题还活着吗"——已经很久没再出现的模式优先级更低。
    """

    error_type: ErrorType
    situation_signature: str
    experience_ids: tuple[UUID, ...] = field(default=())
    occurrence_count: int = 0
    weighted_count: int = 0
    experience_count: int = 0
    evaluations: tuple[ExperienceEvaluation, ...] = field(default=())
    evaluator_types: tuple[ExperienceEvaluator, ...] = field(default=())
    independence_groups: tuple[str, ...] = field(default=())
    counterexample_count: int = 0
    applicable_conditions: tuple[str, ...] = field(default=())
    first_round_id: UUID | None = None
    last_round_id: UUID | None = None

    @property
    def count(self) -> int:
        """独立发生的次数。

        ⚠️ 这是"发生过几次"，不是"加权后算几"。门槛用
        :attr:`weighted_count`。
        """
        return self.occurrence_count


@dataclass(frozen=True, slots=True)
class SuppressedPattern:
    """有成员、但**未达门槛**的分组。

    🔴 存在的理由是让"为什么没有提案"可回答。

    只报告合格模式的实现会让"三条 SUSPECTED 被权重挡在门外"
    与"历史上根本没有这些经验"在外部看来完全一样——
    而这两件事该导致完全不同的下一步动作。
    """

    error_type: ErrorType
    situation_signature: str
    occurrence_count: int
    weighted_count: int
    experience_count: int
    evaluations: tuple[ExperienceEvaluation, ...] = field(default=())
    reasons: tuple[str, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class PatternScan:
    """一次模式发现的完整结果。

    Attributes:
        patterns: 达到门槛的模式，按 ``(加权计数降序, 错误类别, 情境签名)``。
        suppressed: 未达门槛的分组，按同样规则排列。
        unattributable_count: 因无法归因而被跳过的经验条数。
        low_confidence_count: 因归因置信度过低而被跳过的经验条数。
    """

    patterns: tuple[ErrorPattern, ...] = ()
    suppressed: tuple[SuppressedPattern, ...] = ()
    unattributable_count: int = 0
    low_confidence_count: int = 0


class PatternDetector:
    """从经验集合里发现重复模式。"""

    def __init__(
        self,
        *,
        threshold: int = PROPOSAL_ESCALATION_THRESHOLD,
        weighting: EvaluationWeighting = DEFAULT_WEIGHTING,
    ) -> None:
        """初始化。

        Args:
            threshold: 同类错误达到多少**加权**次数才算一个模式。
            weighting: 评价状态的计数权重。

        Raises:
            ValueError: 门槛低于 2——单次经验不足以支撑任何模式
                （不变量 10）。
        """
        if threshold < 2:
            msg = "模式门槛不得低于 2：单次经验不构成模式（任务书 §11.3、不变量 10）"
            raise ValueError(msg)
        self._threshold = threshold
        self._weighting = weighting

    @property
    def threshold(self) -> int:
        """当前门槛。"""
        return self._threshold

    @property
    def weighting(self) -> EvaluationWeighting:
        """当前使用的评价权重表。"""
        return self._weighting

    def detect(self, assessments: Sequence[ExperienceAssessment]) -> PatternScan:
        """发现重复模式。

        Args:
            assessments: 候选经验及其有效评价（顺序无关——结果按内容排序）。

        Returns:
            扫描结果。**未达门槛的分组也在里面**（:attr:`PatternScan.suppressed`），
            否则"为什么没有提案"无法回答。

        Note:
            排序是确定的：同一批经验在任何一次运行里都得到同样的顺序，
            否则"这次跑出三个提案、下次跑出两个"会变成常态。
        """
        grouped: dict[tuple[ErrorType, str], list[ExperienceAssessment]] = defaultdict(list)
        unattributable = 0
        low_confidence = 0
        for item in assessments:
            experience = item.experience
            if experience.error_type is None:
                unattributable += 1
                continue
            if experience.attribution_confidence.rank < _MIN_ATTRIBUTION_CONFIDENCE:
                low_confidence += 1
                continue
            grouped[(experience.error_type, experience.situation_signature)].append(item)

        patterns: list[ErrorPattern] = []
        suppressed: list[SuppressedPattern] = []
        for (error_type, signature), members in grouped.items():
            unique = _distinct_occurrences(members)
            weighted = sum(self._weighting.weight_of(item.evaluation) for item in unique.values())
            if weighted >= self._threshold:
                patterns.append(self._build_pattern(error_type, signature, unique, weighted))
            else:
                suppressed.append(self._build_suppressed(error_type, signature, unique, weighted))

        patterns.sort(
            key=lambda item: (-item.weighted_count, item.error_type.value, item.situation_signature)
        )
        suppressed.sort(
            key=lambda item: (-item.weighted_count, item.error_type.value, item.situation_signature)
        )
        return PatternScan(
            patterns=tuple(patterns),
            suppressed=tuple(suppressed),
            unattributable_count=unattributable,
            low_confidence_count=low_confidence,
        )

    def _build_pattern(
        self,
        error_type: ErrorType,
        signature: str,
        unique: dict[str, ExperienceAssessment],
        weighted: int,
    ) -> ErrorPattern:
        """把一组已达门槛的同类经验汇成一个模式。"""
        ordered = _ordered(unique)
        experiences = [item.experience for item in ordered]
        # 🔴 按**发生时间**排序，`first/last` 才真的表示"最早/最晚发生的一次"。
        # 阶段 6 的初版按经验 id 排序，而 id 是 uuid4——所谓"最早/最晚"
        # 实际是"id 字典序最小/最大"，与时间无关。
        rounds = [item.cognitive_round_id for item in experiences]
        conditions = sorted(
            {condition for item in experiences for condition in item.applicable_conditions}
        )
        return ErrorPattern(
            error_type=error_type,
            situation_signature=signature,
            # 经验 id 本身仍按字典序排列：列在提案里的证据顺序必须可复现，
            # 而"哪条最早"由上面那两个字段回答。
            experience_ids=tuple(sorted((item.id for item in experiences), key=str)),
            occurrence_count=len(ordered),
            weighted_count=weighted,
            experience_count=len(experiences),
            evaluations=tuple(sorted({item.evaluation for item in ordered}, key=lambda e: e.rank)),
            evaluator_types=tuple(
                sorted(
                    {t for item in ordered for t in item.evaluator_types},
                    key=lambda e: e.value,
                )
            ),
            independence_groups=tuple(sorted(unique, key=str)),
            counterexample_count=sum(1 for item in experiences if item.counterexamples),
            applicable_conditions=tuple(conditions),
            first_round_id=rounds[0] if rounds else None,
            last_round_id=rounds[-1] if rounds else None,
        )

    def _build_suppressed(
        self,
        error_type: ErrorType,
        signature: str,
        unique: dict[str, ExperienceAssessment],
        weighted: int,
    ) -> SuppressedPattern:
        """把一组未达门槛的同类经验记下来，并说明为什么没达。"""
        ordered = _ordered(unique)
        evaluations = sorted({item.evaluation for item in ordered}, key=lambda e: e.rank)
        reasons: list[str] = [
            f"{error_type.value} 在情境 {signature} 下发生 {len(ordered)} 次，"
            f"加权计数 {weighted}，未达门槛 {self._threshold}"
        ]
        if all(not self._weighting.counts_toward_threshold(item.evaluation) for item in ordered):
            # 🔴 这是 §二.6–7 要求的"可见性"：纯 SUSPECTED 被挡下时
            # 必须说清是**权重**挡的，而不是"经验不够多"。
            reasons.append(
                "全部经验的评价状态为 "
                f"{'、'.join(item.value for item in evaluations)}，"
                f"默认权重为 {self._weighting.weight_of(evaluations[-1])}——"
                "内部元认知的怀疑不是证据，它不足以支撑提案（阶段 6.5 §二.6–7）。"
                "需要用户纠正、后续证据或独立评测把它抬到 supported/confirmed"
            )
        return SuppressedPattern(
            error_type=error_type,
            situation_signature=signature,
            occurrence_count=len(ordered),
            weighted_count=weighted,
            experience_count=len(unique),
            evaluations=tuple(evaluations),
            reasons=tuple(reasons),
        )


def _ordered(unique: dict[str, ExperienceAssessment]) -> list[ExperienceAssessment]:
    """把去重后的分组按**发生时间**排序。

    ``id`` 是并列时的确定排序键（同一微秒内创建的两条经验）。
    """
    return sorted(
        unique.values(),
        key=lambda item: (item.experience.created_at, str(item.experience.id)),
    )
