"""错误分类（任务书 §11.2）。

🔴 **这一层只做确定性归因，不做"猜"。**

归因决定了系统之后学什么。归错了比不归因更糟：把"当时信息本就不足"
判成"推理错误"，系统会学到"这类问题要更保守"——
而真正的教训是"这类问题需要先取证"。两种教训方向相反。

因此 :class:`ErrorClassifier` 的每一条判据都必须是**可以被指出来**的：
回合在哪个阶段失败（阶段映射已经是确定的）、元认知检出了什么、
用户是否明确纠正过。凡是需要理解语义才能判定的，一律**不判**——
返回"无法归因"而不是给一个看起来合理的分类。

:class:`ErrorAttribution` 带着 ``reasons``，逐条说明**为什么**得出这个结论。
没有理由的归因在事后被质疑时无法辩护，也就无法被推翻——而不可推翻的
归因会永久地影响策略。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from ai_psi.domain.enums import (
    ConfidenceBand,
    EpistemicAction,
    ErrorType,
    FeedbackType,
    RoundState,
    UncertaintyType,
)

__all__ = ["ErrorAttribution", "ErrorClassifier", "ErrorSignals"]


@dataclass(frozen=True, slots=True)
class ErrorSignals:
    """归因所用的全部输入。

    🔴 **本对象刻意不接收任何自由文本。**

    用户消息、模型输出、回答正文一概不进这里。理由有两条：
    一是归因不该依赖对文本的语义理解（那是模型的工作，而模型的判断
    不该被直接固化成"系统学到了什么"）；二是这些文本可能含用户隐私，
    把它们拉进学习链路等于给隐私开了一条谁也没打算开的路。
    """

    round_state: RoundState
    """回合的终态。"""

    failure_stage: str | None = None
    """失败发生在哪个阶段（不变量 20）。"""

    failure_category: ErrorType | None = None
    """失败时已由阶段映射确定的错误类别。"""

    budget_exhausted: bool = False
    """是否因预算耗尽而终止。"""

    memory_write_rejected: bool = False
    """本次回合的记忆写入是否被策略拒绝。"""

    scope_drift_detected: bool = False
    """元认知是否检测到问题范围漂移。"""

    unsupported_certainty_detected: bool = False
    """元认知是否检测到无依据的确定性表述。"""

    missing_counterexample_detected: bool = False
    """元认知是否检测到关键反例被忽略。"""

    high_confirmation_bias: bool = False
    """确认偏差风险是否达到 HIGH 及以上。"""

    high_user_pleasing_bias: bool = False
    """迎合风险是否达到 HIGH 及以上。"""

    uncertainty_type: UncertaintyType | None = None
    """判断所标注的不确定性类型。"""

    epistemic_action: EpistemicAction | None = None
    """判断建议的认知动作。"""

    feedback_types: tuple[FeedbackType, ...] = ()
    """本次回合收到的用户反馈类型（**只有类型，没有内容**）。"""


@dataclass(frozen=True, slots=True)
class ErrorAttribution:
    """一次归因的结论。

    Attributes:
        error_type: 错误类别；``None`` 表示**无法确定性归因**。
        confidence: 归因置信度。低置信度的归因不驱动任何策略变化。
        reasons: 判定理由，逐条可读——**归因必须能解释自己**。
    """

    error_type: ErrorType | None
    confidence: ConfidenceBand = ConfidenceBand.VERY_LOW
    reasons: tuple[str, ...] = field(default=())

    @property
    def attributable(self) -> bool:
        """本次归因是否足以支撑经验记录。

        与 :attr:`~ai_psi.domain.experiences.Experience.is_attributable`
        是同一条规则（类别非空 + 置信度不低于 LOW），
        放在这里是为了让调用方在构造 ``Experience`` **之前**就能判断，
        而不是构造完再回头检查。
        """
        return self.error_type is not None and self.confidence.rank >= ConfidenceBand.LOW.rank


class ErrorClassifier:
    """确定性的错误分类器。

    判据按**具体性**排序，先命中的胜出：

    1. 回合失败且已由阶段映射给出类别 —— 最具体，直接采用；
    2. 结构性信号（预算耗尽 / 记忆被拒 / 范围漂移 / 确定性无依据 …）——
       它们**指名了类别**；
    3. 用户反馈 —— 它是"确实错了"的强证据，但**不指名类别**；
    4. 都不命中 —— 不归因。

    🔴 **第 2 条排在第 3 条前面**，这是有意的：用户纠正说明"有错"，
    而元认知信号说明"错在哪"。两者同时出现时，
    后者给出的类别比 ``UNKNOWN_ERROR`` 有用得多。
    反过来把用户反馈排在前面，会让所有"用户纠正过"的回合
    都归成同一个笼统的类别，模式发现因此失去分辨力。
    """

    def classify(self, signals: ErrorSignals) -> ErrorAttribution:
        """对一次回合做错误归因。

        Args:
            signals: 归因输入。

        Returns:
            归因结论。无法确定性归因时 ``error_type`` 为 ``None``。
        """
        for rule in (
            self._from_failure,
            self._from_budget,
            self._from_memory_rejection,
            self._from_scope_drift,
            self._from_unsupported_certainty,
            self._from_value_substitution,
            self._from_reasoning_signals,
            self._from_user_feedback,
        ):
            attribution = rule(signals)
            if attribution is not None:
                return attribution

        return ErrorAttribution(
            error_type=None,
            confidence=ConfidenceBand.VERY_LOW,
            reasons=("没有任何确定性判据命中，不做归因",),
        )

    # ------------------------------------------------------------------
    # 判据
    # ------------------------------------------------------------------

    def _from_failure(self, signals: ErrorSignals) -> ErrorAttribution | None:
        """回合失败且已有明确的错误类别。

        🔴 **要求终态确实是 ``FAILED``。**

        只看 ``failure_category is not None`` 的话，一个终态是
        ``COMPLETED`` 却带着失败类别的回合会被归成"失败"，
        理由栏还会写下"回合在「respond」阶段失败（终态 completed）"
        这种自相矛盾的句子——而它会作为一次真实错误进入模式发现。

        ``CognitiveRound`` 只要求 FAILED 时必填这两个字段，
        **没有禁止**其他状态携带它们；因此这道判断必须在这里做。
        """
        if signals.failure_category is None:
            return None
        if signals.round_state is not RoundState.FAILED:
            return None
        stage = signals.failure_stage or "未知阶段"
        return ErrorAttribution(
            error_type=signals.failure_category,
            confidence=ConfidenceBand.MODERATE,
            reasons=(
                f"回合在「{stage}」阶段失败（终态 {signals.round_state.value}）",
                f"错误类别由阶段映射确定：{signals.failure_category.value}",
            ),
        )

    def _from_budget(self, signals: ErrorSignals) -> ErrorAttribution | None:
        """预算耗尽导致回合没能给出应有的结论。"""
        if not signals.budget_exhausted:
            return None
        return ErrorAttribution(
            error_type=ErrorType.PROCESS_ERROR,
            confidence=ConfidenceBand.MODERATE,
            reasons=(
                "回合因预算耗尽而终止",
                "这不是判断错了，而是**流程没跑完**——"
                "归为过程错误而非推理错误，两者的改进方向完全不同",
            ),
        )

    def _from_memory_rejection(self, signals: ErrorSignals) -> ErrorAttribution | None:
        """记忆写入被策略拒绝。"""
        if not signals.memory_write_rejected:
            return None
        return ErrorAttribution(
            error_type=ErrorType.MEMORY_ERROR,
            confidence=ConfidenceBand.MODERATE,
            reasons=(
                "本回合的记忆写入被 WritePolicy 拒绝",
                "系统试图记住不该记的东西——这是记忆层的问题，不是判断层的问题",
            ),
        )

    def _from_scope_drift(self, signals: ErrorSignals) -> ErrorAttribution | None:
        """问题范围被悄悄换掉了。"""
        if not signals.scope_drift_detected:
            return None
        return ErrorAttribution(
            error_type=ErrorType.SCOPE_ERROR,
            confidence=ConfidenceBand.MODERATE,
            reasons=(
                "元认知检测到问题范围漂移",
                "范围漂移会让回答「答得很对但不是问的那件事」——结果看起来完整，错误却完全不可见",
            ),
        )

    def _from_unsupported_certainty(self, signals: ErrorSignals) -> ErrorAttribution | None:
        """在没有依据的情况下给出了确定性表述。"""
        if not signals.unsupported_certainty_detected:
            return None
        return ErrorAttribution(
            error_type=ErrorType.CALIBRATION_ERROR,
            confidence=ConfidenceBand.MODERATE,
            reasons=(
                "元认知检测到无依据的确定性表述",
                "置信度失准是本系统特别关注的类型："
                "结论对不对要等证据，但**说得太满**本身就已经是错误",
            ),
        )

    def _from_value_substitution(self, signals: ErrorSignals) -> ErrorAttribution | None:
        """把价值选择当成事实判断来回答。

        🔴 判据是**结构性的**，不看措辞：判断自己声明了
        ``NORMATIVE``（事实无法决定的价值选择），却给出了
        ``ANSWER``（可以给出结论）——这两件事放在一起就是自相矛盾。
        """
        if signals.uncertainty_type is not UncertaintyType.NORMATIVE:
            return None
        if signals.epistemic_action is not EpistemicAction.ANSWER:
            return None
        return ErrorAttribution(
            error_type=ErrorType.VALUE_SUBSTITUTION,
            confidence=ConfidenceBand.MODERATE,
            reasons=(
                "判断声明不确定性类型为 normative（事实无法决定价值）",
                "但建议的认知动作是 answer（可以给出结论）——两者自相矛盾",
                "这不是「答错了」，而是「用事实的口气回答了价值问题」",
            ),
        )

    def _from_reasoning_signals(self, signals: ErrorSignals) -> ErrorAttribution | None:
        """推理过程层面的信号。"""
        if not (
            signals.missing_counterexample_detected
            or signals.high_confirmation_bias
            or signals.high_user_pleasing_bias
        ):
            return None

        detected: list[str] = []
        if signals.missing_counterexample_detected:
            detected.append("关键反例被忽略")
        if signals.high_confirmation_bias:
            detected.append("确认偏差风险偏高")
        if signals.high_user_pleasing_bias:
            detected.append("迎合风险偏高")

        return ErrorAttribution(
            error_type=ErrorType.REASONING_ERROR,
            confidence=(
                ConfidenceBand.MODERATE
                if signals.missing_counterexample_detected
                else ConfidenceBand.LOW
            ),
            reasons=(
                f"元认知检出：{'；'.join(detected)}",
                "这三项都属于**推理过程**的问题，与结论是否正确无关——结论恰好正确也不能抵消它们",
            ),
        )

    def _from_user_feedback(self, signals: ErrorSignals) -> ErrorAttribution | None:
        """用户明确表示判断有误。

        🔴 **这是"确实错了"的最强证据，但它不告诉我们错在哪。**

        错误类别需要理解用户到底在纠正什么，而那是语义判断——
        本层刻意不做。给一个看起来具体的类别（比如一律判成
        ``FACTUAL_ERROR``）会让模式发现把一堆互不相干的错误
        聚成一类，然后生成一个针对错误问题的提案。

        因此这里归为 ``UNKNOWN_ERROR`` 并**在理由里说清楚为什么**。
        任务书 §11.3 明确把"用户纠正显示现有规则具有系统性问题"
        列为提案的合法触发条件之一，所以这条归因是有用的：
        它标记出"用户反复纠正"这件事本身，而不是假装知道原因。
        """
        corrections = {
            FeedbackType.CORRECTION,
            FeedbackType.DISAGREEMENT,
        }
        matched = [item for item in signals.feedback_types if item in corrections]
        if not matched:
            return None
        return ErrorAttribution(
            error_type=ErrorType.UNKNOWN_ERROR,
            confidence=ConfidenceBand.LOW,
            reasons=(
                f"用户给出了明确的否定反馈：{'、'.join(item.value for item in matched)}",
                "用户反馈证明判断有误，但**错误类别需要语义判断**，"
                "确定性规则无法给出——因此记为 unknown_error 而不是猜一个具体类别",
                "如果这类反馈反复出现，它本身就是 §11.3 认可的提案触发条件",
            ),
        )


#: 阶段名到错误类别的映射。
#:
#: 🔴 **这是这张映射表的唯一一份。**
#:
#: 阶段 3 时它住在 `ai_psi.application.cognitive_runtime` 里，
#: 阶段 6 的归因也需要同一张表，于是搬到了学习层——
#: 运行时改为从这里 import（`_category_for`），副本已删除。
#:
#: 两份各自维护的映射一旦漂移，"同一个失败在两个地方得到不同类别"
#: 就会发生，且没有任何地方会报错：失败回合照常有类别、
#: 经验记录也照常有类别，只是它们对不上。
#: `tests/unit/test_error_classifier.py` 从**运行时的源码**里读出
#: 它实际会赋的阶段名，断言与本表逐项一致。
STAGE_ERROR_CATEGORY: Final[dict[str, ErrorType]] = {
    "triage": ErrorType.CONCEPTUAL_ERROR,
    "frame": ErrorType.SCOPE_ERROR,
    "retrieve": ErrorType.EVIDENCE_ERROR,
    "analyze": ErrorType.REASONING_ERROR,
    "deliberate": ErrorType.REASONING_ERROR,
    "review": ErrorType.CALIBRATION_ERROR,
    "synthesize": ErrorType.REASONING_ERROR,
    "respond": ErrorType.EXPRESSION_ERROR,
}
