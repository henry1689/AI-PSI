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

from ai_psi.domain.enums import VerificationStatus
from ai_psi.domain.experiences import Experience
from ai_psi.learning.error_classifier import ErrorAttribution, ErrorClassifier, ErrorSignals

__all__ = ["ExperienceBuilder", "RoundRecord"]


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


# ⚠️ **``Experience.strategy_effectiveness`` 在 V0.1 恒为 ``None``。**
#
# 回答"这次采用的分析策略好不好"需要一个判据，而 V0.1 没有：
# 只有"回合出错了没有"（这是错误归因，不是策略评估）与
# "绕了几圈"（这是反刍信号）。用它们凑一个三档评级会把两个
# 不同的问题混成一个数字，而那个数字之后会被当成"策略有效性"来用。
#
# 真正能回答它的是阶段 7 的评测系统（有 Golden Dataset 与基线对照）。
# 在那之前留空，比填一个看起来像答案的东西好。
