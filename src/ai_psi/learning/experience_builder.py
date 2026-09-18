"""经验构建（任务书 §11.1「收集经验」）。

把一次认知回合的**结构信号**转成 :class:`~ai_psi.domain.experiences.Experience`。

🔴 **本模块不接收任何自由文本。**

用户消息、模型输出、回答正文、反馈原文一概不进这里。理由是它决定了
**系统之后学什么**：如果经验里带着"用户当时说了什么"，那么"能不能
从这条经验里学到东西"就会依赖对那段文本的理解——而那是语义判断，
不是确定性规则能给的。确定性判断与语义判断混在一条链路里，
最后没人说得清某条经验到底是从事实推出来的还是从一句话里猜出来的。

反馈因此以**类型**参与（"用户纠正了"），而不是以**内容**参与。
原文留在事件流里，那是审计的地方，也是唯一需要它的地方。

⚠️ **单次经验不产生提案**（任务书 §11.3，不变量 10）。
本模块只负责记录；是否升级为提案由
:class:`~ai_psi.learning.promotion_policy.PromotionPolicy` 按门槛决定。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from ai_psi.domain.enums import (
    ExperienceEvaluation,
    ExperienceEvaluator,
    ExperienceKind,
    VerificationStatus,
)
from ai_psi.domain.experiences import (
    EXTRACTOR_VERSION,
    Experience,
    canonical_key_for,
    evaluation_target_for_judgment,
    independence_group_for,
)
from ai_psi.learning.error_classifier import ErrorAttribution, ErrorClassifier, ErrorSignals

__all__ = ["EVALUATOR_VERSION", "ExperienceBuilder", "RoundRecord"]

#: 抽取时那一手评价（内部元认知）的逻辑版本。
#:
#: 与 :data:`~ai_psi.domain.experiences.EXTRACTOR_VERSION` 分开：
#: 前者说的是"这条经验**怎么被抽出来**的"，后者说的是
#: "它**被谁判成什么状态**"。两件事独立演进——
#: 改了归因判据（评价逻辑）不必让所有经验换 canonical_key。
EVALUATOR_VERSION: str = "internal-metacognition/1"


@dataclass(frozen=True, slots=True)
class RoundRecord:
    """构建经验所需的、来自**认知回合**的全部输入。

    与 :class:`~ai_psi.learning.error_classifier.ErrorSignals` 分开：
    前者描述"这个回合是什么"（客观事实），后者描述"这个回合出了什么问题"
    （判断信号）。合成一个对象会让"有没有出错"变得像回合的固有属性，
    而它其实来自一组可以单独讨论、单独替换的判据。
    """

    cognitive_round_id: UUID
    judgment_id: UUID
    situation_signature: str
    inquiry_type: str

    evidence_ids: tuple[UUID, ...] = ()
    """**判断发生时**就已掌握的证据。

    🔴 这是经验记录里最关键的一个字段：它区分"当时判断错了"与
    "当时信息本就不足"。少了它，系统会把所有后来被推翻的判断都记成错误，
    从而学到错误的教训——比如变得过度保守。
    """

    strategy_used: tuple[str, ...] = ()
    """本回合采用的分析策略（模块名），用于日后回答"哪种策略有效"。"""

    later_evidence_ids: tuple[UUID, ...] = ()
    """判断**之后**才出现的证据——归因的关键输入。"""

    origin_event_ids: tuple[UUID, ...] = ()
    """产生本经验的**事件**（阶段 6.5 §二.9）。

    它是"这条经验读的是哪几个事件"的锚点。没有它，从事件回放里
    重建经验只能靠时间和 payload 形状猜，而"猜"在审计场景里
    等于没有证据。
    """

    idempotency_key: str | None = None
    """该回合的客户端幂等键（阶段 6.5 §二.12）。

    🔴 它的唯一用途是算 ``independence_group``：同一幂等键的
    所有技术重试落在**同一分组**里，因此在门槛上只算一次。
    没有它，一次网络抖动后的重发就会被当成"这件事又发生了一次"。
    """

    applicable_conditions: tuple[str, ...] = ()
    counterexamples: tuple[str, ...] = ()
    """该经验成立的条件与反例。

    缺少条件限制的经验极易被过度推广；反例则是阻止它被过度推广的
    主要手段。两者都由调用方提供——本模块不替调用方决定
    "这条经验在什么范围内成立"。

    ⚠️ **这两栏放的是短的结构化标签，不是自由文本。**
    它们会原样进入 ``Experience``、被模式发现汇总、并最终列进提案的
    ``applicability`` 持久化。放一段用户原话进去，它就跟着进了
    只追加的事件表。本模块没有机制拦住这件事——它靠的是调用方
    不往这里填原文（见 ADR-0018 §1 的"残余风险"）。
    """


class ExperienceBuilder:
    """从回合记录构建经验。"""

    def __init__(self, classifier: ErrorClassifier | None = None) -> None:
        """初始化。

        Args:
            classifier: 错误分类器；``None`` 时使用默认实现。
        """
        self._classifier = classifier if classifier is not None else ErrorClassifier()

    @property
    def classifier(self) -> ErrorClassifier:
        """当前使用的分类器。"""
        return self._classifier

    def build(
        self,
        *,
        record: RoundRecord,
        signals: ErrorSignals,
        created_by: str = "experience_builder",
    ) -> tuple[Experience, ErrorAttribution]:
        """构建一条经验记录。

        Args:
            record: 回合的客观事实。
            signals: 归因判据。
            created_by: 产生该记录的组件。

        Returns:
            ``(经验, 归因结论)``。归因结论一并返回，是因为调用方
            （尤其是审计与调试）需要知道**为什么**是这么归的——
            只把 ``error_type`` 存进经验，理由就丢了。

        Note:
            ``attribution_confidence`` 直接来自归因结论。
            无法归因时 ``error_type`` 为 ``None`` 且置信度为
            ``VERY_LOW``——**这不是"没有错误"，而是"不知道"**，
            两者在后续的模式发现里必须区分对待
            （见 :class:`~ai_psi.learning.pattern_detector.PatternDetector`）。
        """
        attribution = self._classifier.classify(signals)
        target = evaluation_target_for_judgment(record.judgment_id)
        evaluation, evaluator = _internal_metacognition_verdict(attribution)
        experience = Experience(
            created_by=created_by,
            cognitive_round_id=record.cognitive_round_id,
            judgment_id=record.judgment_id,
            situation_signature=record.situation_signature,
            inquiry_type=record.inquiry_type,
            evidence_available_at_time=list(record.evidence_ids),
            later_evidence_ids=list(record.later_evidence_ids),
            error_type=attribution.error_type,
            attribution_confidence=attribution.confidence,
            strategy_used=list(record.strategy_used),
            applicable_conditions=list(record.applicable_conditions),
            counterexamples=list(record.counterexamples),
            # 🔴 **反馈以类型进入经验。** 初版漏了这一行，于是
            # ``actual_feedback`` 永远是空列表、``summarise_feedback``
            # 在生产里没有任何调用者——"反馈以类型参与学习"就只剩
            # 归因那一条路，经验本身记不住"这一条是从哪种反馈来的"。
            actual_feedback=list(self.summarise_feedback(signals)),
            verification_status=VerificationStatus.UNVERIFIED,
            # --- 规范身份（事实派生，不由调用方随手填）---
            experience_kind=ExperienceKind.ROUND_OUTCOME,
            evaluation_target=target,
            origin_event_ids=list(record.origin_event_ids),
            extractor_version=EXTRACTOR_VERSION,
            independence_group=independence_group_for(
                idempotency_key=record.idempotency_key,
                cognitive_round_id=record.cognitive_round_id,
            ),
            canonical_key=canonical_key_for(
                cognitive_round_id=record.cognitive_round_id,
                evaluation_target=target,
                experience_kind=ExperienceKind.ROUND_OUTCOME,
                extractor_version=EXTRACTOR_VERSION,
            ),
            # --- 抽取时刻的评价（§二.4：内部元认知最多 SUSPECTED）---
            evaluation=evaluation,
            evaluator_type=evaluator,
            evaluator_version=EVALUATOR_VERSION if evaluator is not None else None,
        )
        return experience, attribution

    def summarise_feedback(self, signals: ErrorSignals) -> tuple[str, ...]:
        """把反馈**类型**摘要成经验可用的字符串。

        🔴 **摘要里只有类型，没有原文。**

        把用户原文放进去有两个后果：一是学习链路里从此带着用户隐私，
        二是同一件事会因为措辞不同而被当成两件事——
        「你理解错了」和「我没这个意思」说的是同一个意思。

        ⚠️ 模式发现当前**不按这个字段分组**（它按
        ``(错误类别, 情境签名)``）。``actual_feedback`` 的价值在于
        记录"这条经验是从哪种反馈来的"，供人工复盘与后续版本使用。

        Args:
            signals: 归因输入。

        Returns:
            形如 ``("feedback:correction",)`` 的摘要项。
        """
        return tuple(sorted(f"feedback:{item.value}" for item in set(signals.feedback_types)))


def _internal_metacognition_verdict(
    attribution: ErrorAttribution,
) -> tuple[ExperienceEvaluation, ExperienceEvaluator | None]:
    """内部元认知对本回合的自我评价（阶段 6.5 §二.4）。

    🔴 **它最多只能说「怀疑」。**

    抽取发生在回合收尾，那一刻除了系统自己没有任何评估者。
    归因判据指向了某个错误类别，这是一个**假设**，不是一个观察——
    用户还没说话，后续证据还没出现。

    因此本函数的返回值只有两种可能：

    * 归因成功 → ``(SUSPECTED, INTERNAL_METACOGNITION)``；
    * 归因失败 → ``(UNASSESSED, None)``——**"不知道错在哪"不是一种评价**，
      它连怀疑都算不上。

    注意第二种情况**不是** ``SUSPECTED`` 配 ``error_type=None``：
    那样会让"我们没看出问题"与"我们怀疑有问题但说不出是哪类"
    在计数时长得一样。

    Args:
        attribution: 归因结论。

    Returns:
        ``(评价状态, 评估者)``。
    """
    if attribution.error_type is None:
        return ExperienceEvaluation.UNASSESSED, None
    return ExperienceEvaluation.SUSPECTED, ExperienceEvaluator.INTERNAL_METACOGNITION


# ⚠️ **``Experience.strategy_effectiveness`` 在 V0.1 恒为 ``None``。**
#
# 回答"这次采用的分析策略好不好"需要一个判据，而 V0.1 没有：
# 只有"回合出错了没有"（这是错误归因，不是策略评估）与
# "绕了几圈"（这是反刍信号）。用它们凑一个三档评级会把两个
# 不同的问题混成一个数字，而那个数字之后会被当成"策略有效性"来用。
#
# 真正能回答它的是阶段 7 的评测系统（有 Golden Dataset 与基线对照）。
# 在那之前留空，比填一个看起来像答案的东西好。
