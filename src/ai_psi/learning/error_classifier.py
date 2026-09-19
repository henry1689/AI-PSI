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
    CORRECTABLE_KINDS,
    ConfidenceBand,
    CorrectedArtifactKind,
    EpistemicAction,
    ErrorType,
    FeedbackType,
    RoundState,
    UncertaintyType,
)

__all__ = [
    "CLASSIFIER_VERSION",
    "CorrectionTarget",
    "ErrorAttribution",
    "ErrorClassifier",
    "ErrorSignals",
]

#: 错误分类逻辑的版本（阶段 6.6，ADR-0023）。
#:
#: 🔴 **改动这里任何一条归因规则都必须递增它**，因为它是每条
#: ``ExperienceAttributionRecord`` 留档的一段。归因决定了系统之后学什么，
#: 而"这条归因是哪一版规则给出的"是事后复核它的唯一入口——
#: 没有版本号，规则一改，历史归因就只能靠猜它当时用的是哪一套。
CLASSIFIER_VERSION: str = "error-classifier/1"


@dataclass(frozen=True, slots=True)
class CorrectionTarget:
    """用户纠正**指到的那个产物**，以及它的结构信号（阶段 6.6）。

    🔴 **它由服务端从回合的事件流里解析出来，不由客户端声明。**
    客户端只给一个 id；"它是哪一类产物"是服务端查出来的。
    让客户端报类别，等于让调用方决定归因规则吃哪一条分支——
    而归因规则决定系统之后学什么。

    🔴 **只能装可纠正的类别**（:data:`~ai_psi.domain.enums.CORRECTABLE_KINDS`）。
    这是本阶段的**唯一执行点**：不可纠正的类别（记忆、问题本身）
    在构造这里就被拒绝，而不是让分类器悄悄少判一条。

    Attributes:
        artifact_kind: 被指产物的类别。
        has_supporting_evidence: 被指的**假设**当时有没有支持证据。
            只有 ``HYPOTHESIS`` 有意义；其余类别忽略它。
        uncertainty_type: 被指的**判断**声明的确定性类型。
            只有 ``JUDGMENT`` 有意义；其余类别忽略它。
    """

    artifact_kind: CorrectedArtifactKind
    has_supporting_evidence: bool = False
    uncertainty_type: UncertaintyType | None = None

    def __post_init__(self) -> None:
        """🔴 拒绝不可纠正的类别——**在归因之前，不是之后**。

        ``MEMORY`` 有它自己的纠正入口（``POST /memories/{id}/correct``），
        ``INQUIRY`` 是**用户自己提的**问题。让它们走到归因层，
        评审查到的会是一条"这个 id 为什么没有类别"的死分支，
        而真正该说的是"这类产物根本不接受纠正"。
        """
        if self.artifact_kind not in CORRECTABLE_KINDS:
            msg = (
                f"{self.artifact_kind.value} 不是可纠正的产物类别。"
                "记忆有它自己的纠正入口；问题是用户自己提的。"
                "这件事必须在构造 CorrectionTarget 之前被挡住"
            )
            raise ValueError(msg)


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

    correction: CorrectionTarget | None = None
    """用户纠正**指到的那个产物**（阶段 6.6）。

    🔴 **它是"用户纠正"与"归因"之间那道闸门的另一半。**

    ``feedback_types`` 只说"用户说了不对"，没说"哪里不对"。少了
    本字段，一条纠正只能被归成"不知道哪一类"，或者更糟——被猜成
    一个看起来具体的类别。有了它，``_from_user_correction`` 才能
    按**被指产物的结构**给出一个有依据的类别。

    ⚠️ 它是 ``None`` 时**不归因**（而不是退到某个笼统类别）：
    "用户指出了一条我们识别不了的产物"与"用户只是笼统地说不对"
    都不足以支撑一个类别。
    """


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
            self._from_user_correction,
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

    def _from_user_correction(self, signals: ErrorSignals) -> ErrorAttribution | None:
        """用户明确纠正，**并且**指出了被纠正的是哪一条产物（阶段 6.6）。

        🔴 **两个条件缺一不可。**

        用户纠正证明"确实错了"，但它**不说明错在哪一类**。要得到类别，
        系统必须知道用户指的是哪一层产物——那是结构信息，
        由服务端从回合事件流里解析（``CorrectionTarget``）。

        只满足第一条时**不归因**（``error_type=None``）。
        这是刻意的：猜测一个"看起来具体"的类别会让模式发现把一堆
        互不相干的错误聚成一类，然后生成一个针对错误问题的提案——
        而归错类比不归因糟得多。

        ## 类别怎么来的：**被指产物 + 它的结构信号**

        映射规则是 V0.1 的**约定**，不是对错误本质的独立测量。
        每一步都能被指出来，因而也能被推翻：

        | 被指产物 | 结构信号 | 类别 |
        |---|---|---|
        | 证据 | — | ``EVIDENCE_ERROR`` |
        | 假设 | 没有支持证据 | ``EVIDENCE_ERROR`` |
        | 假设 | 有支持证据 | ``REASONING_ERROR`` |
        | 判断 | 声明为 ``NORMATIVE`` | ``VALUE_SUBSTITUTION`` |
        | 判断 | 其余 | ``REASONING_ERROR`` |
        | 回答 | — | ``EXPRESSION_ERROR`` |

        ⚠️ **置信度是 ``MODERATE``，但它的语义要说准**：这是
        「用户明确纠正」+「V0.1 结构映射规则」两条**非独立**的依据
        形成的**策略性归因**，不是两个独立来源共同确认了客观错误类别。
        映射规则是本系统的约定，把它说成"互相印证"是把一条策略
        抬高成一次验证（ADR-0023 §置信度）。
        """
        corrections = {
            FeedbackType.CORRECTION,
            FeedbackType.DISAGREEMENT,
        }
        matched = [item for item in signals.feedback_types if item in corrections]
        if not matched:
            # 没有否定反馈 —— 交给下一族判据（本规则是最后一条）。
            return None

        heard = f"用户给出了明确的否定反馈：{'、'.join(item.value for item in matched)}"

        if signals.correction is None:
            # 🔴 **本规则是链条的最后一条**，所以这里返回一个
            # `error_type=None` 的结论不是"跳过"，而是**最终答案**：
            # 有纠正、但指不出被纠正的对象 → 不归因。
            return ErrorAttribution(
                error_type=None,
                confidence=ConfidenceBand.VERY_LOW,
                reasons=(
                    heard,
                    "但这次纠正**没有指出被纠正的是哪一条产物**"
                    "（缺少 related_artifact_id，或它解析不到本回合的任何产物）",
                    "🔴 只说得出「有错」、说不出「错在哪一条」，"
                    "不足以支撑一个错误类别——保持不归因，而不是猜一个",
                ),
            )

        target = signals.correction
        error_type, detail = _category_for_correction(target)
        return ErrorAttribution(
            error_type=error_type,
            confidence=ConfidenceBand.MODERATE,
            reasons=(
                heard,
                f"并且指出了被纠正的产物：一条 **{_KIND_LABEL[target.artifact_kind]}**",
                detail,
                f"🔴 这是**策略性归因**：它来自「用户明确纠正」+「V0.1 结构映射规则」"
                f"（{CLASSIFIER_VERSION}），**不是两个独立来源共同确认**了客观错误类别",
            ),
        )


#: 被指产物类别在理由里的说法。
_KIND_LABEL: Final[dict[CorrectedArtifactKind, str]] = {
    CorrectedArtifactKind.EVIDENCE: "证据",
    CorrectedArtifactKind.HYPOTHESIS: "论断（假设）",
    CorrectedArtifactKind.JUDGMENT: "判断",
    CorrectedArtifactKind.RESPONSE: "回答",
}


def _category_for_correction(target: CorrectionTarget) -> tuple[ErrorType, str]:
    """按被指产物推出错误类别与一句可读的判据。

    🔴 **每个可纠正的类别都必须在这里有一支。**
    少了任何一支，下面那个 ``raise`` 会**炸掉**而不是静默地不归因——
    fail-closed：往 ``CORRECTABLE_KINDS`` 里加一类却忘了加规则，
    症状应该是"归因时立刻报错"，而不是"这类纠正悄悄不算数"。

    Returns:
        ``(错误类别, 判据)``。
    """
    kind = target.artifact_kind
    if kind is CorrectedArtifactKind.EVIDENCE:
        return ErrorType.EVIDENCE_ERROR, "被纠正的对象是**证据本身**——这是证据层的问题"
    if kind is CorrectedArtifactKind.HYPOTHESIS:
        if not target.has_supporting_evidence:
            return (
                ErrorType.EVIDENCE_ERROR,
                "这条论断在那时**没有任何证据支撑**——问题出在证据层，不是推理层",
            )
        return (
            ErrorType.REASONING_ERROR,
            "这条论断有证据支撑，而用户仍指出它错了——证据在那儿，是推理用错了",
        )
    if kind is CorrectedArtifactKind.JUDGMENT:
        if target.uncertainty_type is UncertaintyType.NORMATIVE:
            return (
                ErrorType.VALUE_SUBSTITUTION,
                "被纠正的判断自己声明了 value 层面无法由事实决定（normative），"
                "却给出了可下结论的结论——这不是「答错了」，是「用事实的口气回答了价值问题」",
            )
        return ErrorType.REASONING_ERROR, "被纠正的是**判断本身**——推理层的产物"
    if kind is CorrectedArtifactKind.RESPONSE:
        return ErrorType.EXPRESSION_ERROR, "被纠正的是**给用户的回答**——表达层的产物"

    msg = (
        f"没有为 {kind.value} 定义归因规则。它本该在构造 CorrectionTarget 时"
        "就被 CORRECTABLE_KINDS 挡住——往那张表里加类别时，这里必须同时加一支"
    )
    raise ValueError(msg)


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
