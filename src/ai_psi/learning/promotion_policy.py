"""提案门槛裁决（任务书 §11.3）。

§11.3 给出五条**任一即可**的触发条件：

1. 同类错误至少出现 3 次；
2. 一个严重错误存在明确修复方向；
3. 离线评测暴露稳定退化；
4. 用户纠正显示现有规则具有系统性问题；
5. 相同认知模块连续低于阈值。

🔴 **本模块对这五条的态度是"能算的算，算不了的要求调用方把输入给出来"，
而不是"算不了的当作不成立"。**

两者的区别在后果上：把无法评估的条件当成"不成立"，
会让系统**永远只从最容易计数的那个条件（第 1 条）产生提案**——
而"离线评测暴露稳定退化"恰恰是最有价值、也最不可能被计数捕捉的那一类。
因此第 3、5 条要求调用方**显式提供**观测结果；没提供就是"未评估"，
它会出现在理由里，而不是消失。

⚠️ 本模块**只决定"能不能生成提案"**，不决定"提案是否生效"。
后者由 ``ProposalStatus`` 的类型约束与人工审批把关（不变量 11）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from ai_psi.domain.enums import ErrorType
from ai_psi.domain.improvement_proposals import PROPOSAL_ESCALATION_THRESHOLD
from ai_psi.learning.pattern_detector import ErrorPattern

__all__ = [
    "PromotionDecision",
    "PromotionEvidence",
    "PromotionPolicy",
    "PromotionTrigger",
]

#: 判定"严重错误"的类别集合。
#:
#: 选取原则是**错误的后果**，不是错误的频率：
#:
#: * ``VALUE_SUBSTITUTION``——把价值偏好当成事实，等于替用户做了他不该被代做的决定；
#: * ``CALIBRATION_ERROR``——说得太满。结论对不对要等证据，但**过度自信本身
#:   就已经在误导用户**，而且它比错误结论更难被用户发现；
#: * ``MEMORY_ERROR``——记忆错误的后果是累积的：一次错误写入会持续影响
#:   此后所有回合，用户很难发现也很难追溯；
#: * ``SCOPE_ERROR``——答的不是问的那件事，而表面上完全看不出来。
#:
#: ⚠️ 刻意**不含** ``UNKNOWN_ERROR``：它不是一个类别，而是"我们不知道类别"。
#: 从它出发的"明确修复方向"必然是无根据的。
SEVERE_ERROR_TYPES: Final[frozenset[ErrorType]] = frozenset(
    {
        ErrorType.VALUE_SUBSTITUTION,
        ErrorType.CALIBRATION_ERROR,
        ErrorType.MEMORY_ERROR,
        ErrorType.SCOPE_ERROR,
    }
)

#: 判定"模块连续低于阈值"所需的连续次数。
MODULE_REGRESSION_STREAK: Final[int] = 3


class PromotionTrigger(StrEnum):
    """§11.3 的五条触发条件。"""

    REPEATED_SAME_ERROR = "repeated_same_error"
    """同类错误至少出现 3 次。"""

    SEVERE_ERROR_WITH_FIX = "severe_error_with_fix"
    """严重错误存在明确修复方向。"""

    OFFLINE_REGRESSION = "offline_regression"
    """离线评测暴露稳定退化。"""

    USER_CORRECTION_PATTERN = "user_correction_pattern"
    """用户纠正显示现有规则具有系统性问题。"""

    MODULE_BELOW_THRESHOLD = "module_below_threshold"
    """相同认知模块连续低于阈值。"""


@dataclass(frozen=True, slots=True)
class PromotionEvidence:
    """裁决所需的全部输入。

    Attributes:
        pattern: 观察到的错误模式；``None`` 表示没有达到次数的模式。
        fix_direction: 严重错误的**明确修复方向**；``None`` 表示没有。
            注意是"明确"——"再看看"不是修复方向。
        offline_regression: 离线评测是否暴露稳定退化。
            ``None`` 表示**未评估**（与 ``False`` 不同）。
        module_streak: 同一模块连续低于阈值的次数。
            ``None`` 表示**未观测**（与 ``0`` 不同）。
        user_corrections: 用户纠正的次数。
            ``None`` 表示**未观测**（与 ``0`` 不同）——它与
            ``module_streak`` 是同一类东西（"观测到的次数"），
            两者对"未观测"的表达必须一致。
        user_correction_shows_systemic_issue: 这些纠正是否指向
            系统性问题（而不是单次口误）。
    """

    pattern: ErrorPattern | None = None
    fix_direction: str | None = None
    offline_regression: bool | None = None
    module_streak: int | None = None
    user_corrections: int | None = None
    user_correction_shows_systemic_issue: bool = False


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    """裁决结果。

    Attributes:
        allowed: 是否允许生成提案。
        triggers: 命中的触发条件。
        reasons: 逐条可读的理由——**包括未命中的条件为什么没命中**。
        counterexample_count: 模式里带反例的经验条数，
            原样透传给提案，不在这里做过滤。
    """

    allowed: bool
    triggers: tuple[PromotionTrigger, ...] = field(default=())
    reasons: tuple[str, ...] = field(default=())
    counterexample_count: int = 0


class PromotionPolicy:
    """按 §11.3 判定一个观察是否可以升级为提案。"""

    def __init__(self, *, threshold: int = PROPOSAL_ESCALATION_THRESHOLD) -> None:
        """初始化。

        Args:
            threshold: 同类错误的次数门槛。

        Raises:
            ValueError: 门槛低于 2（不变量 10）。
        """
        if threshold < 2:
            msg = "提案门槛不得低于 2：单次经验不足以支撑全局策略（不变量 10）"
            raise ValueError(msg)
        self._threshold = threshold

    @property
    def threshold(self) -> int:
        """当前门槛。"""
        return self._threshold

    def decide(self, evidence: PromotionEvidence) -> PromotionDecision:
        """裁断一个观察是否够格成为提案。

        Args:
            evidence: 裁决输入。

        Returns:
            裁决结果。**理由里同时包含未命中的条件**——
            一份只有"为什么可以"的裁决在日后被质疑时无法回答
            "那另外四条呢"。
        """
        triggers: list[PromotionTrigger] = []
        reasons: list[str] = []

        self._check_repetition(evidence, triggers, reasons)
        self._check_severe(evidence, triggers, reasons)
        self._check_regression(evidence, triggers, reasons)
        self._check_user_corrections(evidence, triggers, reasons)
        self._check_module_streak(evidence, triggers, reasons)

        counterexamples = evidence.pattern.counterexample_count if evidence.pattern else 0
        if triggers and counterexamples:
            reasons.append(
                f"⚠️ 支持经验中有 {counterexamples} 条带有反例——"
                "反例不会被过滤掉，它们会原样进入提案的 counterexamples 字段，"
                "因为只看支持证据的改进是不成立的"
            )

        if not triggers:
            reasons.append("五条触发条件一条都未命中，不生成提案（单次经验不推广）")

        return PromotionDecision(
            allowed=bool(triggers),
            triggers=tuple(triggers),
            reasons=tuple(reasons),
            counterexample_count=counterexamples,
        )

    # ------------------------------------------------------------------
    # 五条条件
    # ------------------------------------------------------------------

    def _check_repetition(
        self,
        evidence: PromotionEvidence,
        triggers: list[PromotionTrigger],
        reasons: list[str],
    ) -> None:
        pattern = evidence.pattern
        if pattern is None:
            reasons.append("条件一（同类错误 ≥3 次）：没有达到次数的模式")
            return
        if pattern.count < self._threshold:
            reasons.append(
                f"条件一（同类错误 ≥3 次）：{pattern.error_type.value} 出现 "
                f"{pattern.count} 次，未达门槛 {self._threshold}"
            )
            return
        triggers.append(PromotionTrigger.REPEATED_SAME_ERROR)
        reasons.append(
            f"条件一命中：{pattern.error_type.value} 在情境 "
            f"{pattern.situation_signature} 下出现 {pattern.count} 次"
        )

    def _check_severe(
        self,
        evidence: PromotionEvidence,
        triggers: list[PromotionTrigger],
        reasons: list[str],
    ) -> None:
        pattern = evidence.pattern
        if pattern is None or pattern.error_type not in SEVERE_ERROR_TYPES:
            reasons.append("条件二（严重错误 + 明确修复方向）：没有严重错误类别")
            return
        if not evidence.fix_direction:
            reasons.append(
                f"条件二：{pattern.error_type.value} 属于严重错误，"
                "但**没有给出明确修复方向**——"
                "「再看看」「需要改进」不是修复方向，不足以支撑提案"
            )
            return
        triggers.append(PromotionTrigger.SEVERE_ERROR_WITH_FIX)
        reasons.append(
            f"条件二命中：{pattern.error_type.value} 属于严重错误，"
            f"且已有明确修复方向：{evidence.fix_direction}"
        )

    def _check_regression(
        self,
        evidence: PromotionEvidence,
        triggers: list[PromotionTrigger],
        reasons: list[str],
    ) -> None:
        if evidence.offline_regression is None:
            reasons.append(
                "条件三（离线评测暴露稳定退化）：**未评估**——"
                "把它当作不成立，会让系统永远只从最容易计数的条件产生提案"
            )
            return
        if not evidence.offline_regression:
            reasons.append("条件三：离线评测未暴露稳定退化")
            return
        triggers.append(PromotionTrigger.OFFLINE_REGRESSION)
        reasons.append("条件三命中：离线评测暴露稳定退化")

    def _check_user_corrections(
        self,
        evidence: PromotionEvidence,
        triggers: list[PromotionTrigger],
        reasons: list[str],
    ) -> None:
        if evidence.user_corrections is None:
            reasons.append(
                "条件四（用户纠正显示系统性问题）：**未观测**——"
                "没有接入用户纠正计数的调用方，不要用 0 冒充「观测过且没有」"
            )
            return
        if evidence.user_corrections < self._threshold:
            reasons.append(
                f"条件四（用户纠正显示系统性问题）：用户纠正 "
                f"{evidence.user_corrections} 次，未达门槛 {self._threshold}"
            )
            return
        if not evidence.user_correction_shows_systemic_issue:
            reasons.append(
                f"条件四：用户纠正 {evidence.user_corrections} 次，"
                "但**未被判定为系统性问题**——"
                "同一用户在不同问题上的单次纠正加起来不构成系统性问题"
            )
            return
        triggers.append(PromotionTrigger.USER_CORRECTION_PATTERN)
        reasons.append(f"条件四命中：{evidence.user_corrections} 次用户纠正指向系统性问题")

    def _check_module_streak(
        self,
        evidence: PromotionEvidence,
        triggers: list[PromotionTrigger],
        reasons: list[str],
    ) -> None:
        if evidence.module_streak is None:
            reasons.append("条件五（模块连续低于阈值）：**未观测**——同条件三，未评估不等于不成立")
            return
        if evidence.module_streak < MODULE_REGRESSION_STREAK:
            reasons.append(
                f"条件五：模块连续低于阈值 {evidence.module_streak} 次，"
                f"未达门槛 {MODULE_REGRESSION_STREAK}"
            )
            return
        triggers.append(PromotionTrigger.MODULE_BELOW_THRESHOLD)
        reasons.append(f"条件五命中：模块连续 {evidence.module_streak} 次低于阈值")
