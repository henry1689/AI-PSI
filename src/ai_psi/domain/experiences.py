"""经验记录与经验评价。

经验是"反馈 → 学习"之间的桥梁。

🔴 **关键字段：``evidence_available_at_time``。**

它区分了两种看起来一样、实则完全不同的情况：

* **当时判断错了** —— 信息足够，推理有问题 → ``REASONING_ERROR``
* **当时信息本就不足** —— 判断在当时是合理的 → 不该记为错误

没有这个字段，系统会把所有"后来被推翻的判断"都记成错误，
从而学到错误的教训（比如变得过度保守、或错误地降低某类问题的置信度）。

## 阶段 6.5 §二：为什么经验需要**身份**

在阶段 6 的评审里，门槛被同一个回合的三次抽取骗过——因为
``Experience.id`` 是每次构建新生成的 ``uuid4``，"同一个回合抽三次"
就得到三条 id 互不相同的经验。修法当时是在模式发现里按
``(cognitive_round_id, judgment_id)`` 去重，但那是**在消费端打补丁**：
抽取端仍然可以无限复制出"看起来独立"的经验。

本模块把身份**下沉到对象本身**：

* :func:`canonical_key_for` —— 决定"这条经验唯一标识了哪一件事"；
* :func:`independence_group_for` —— 决定"这条经验与谁不是独立证据"；
* :class:`ExperienceEvaluationRecord` —— 记录"谁在什么时候、
  凭什么证据确认了它"。

🔴 **身份必须由**事实**决定，不能由**创建行为**决定。**
``uuid4`` 是创建行为，``(回合, 评价对象, 种类, 抽取器版本)`` 是事实。

## 阶段 6.5 §二：为什么经验需要**评价**

同一个 ``error_type`` 字段此前同时表达了两件事：
"系统的元认知怀疑这里出了问题"和"这里确实出了问题"。
两者按同一权重计入提案门槛，于是**系统自己的怀疑可以自我确认成规律**。

:class:`~ai_psi.domain.enums.ExperienceEvaluation` 把它们分开，
:class:`ExperienceEvaluationRecord` 记录外部证据如何逐次抬高它。

⚠️ **单次经验不产生提案**（任务书 §11.3，不变量 10）。
本对象只是记录，是否升级为提案由 :mod:`ai_psi.learning` 按门槛决定。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from ai_psi.domain.common import EntityMetadata, UtcDatetime
from ai_psi.domain.enums import (
    ConfidenceBand,
    ErrorType,
    ExperienceEvaluation,
    ExperienceEvaluator,
    ExperienceKind,
    OrdinalLevel,
    VerificationStatus,
)

__all__ = [
    "EXTRACTOR_VERSION",
    "Experience",
    "ExperienceAssessment",
    "ExperienceEvaluationRecord",
    "assert_evaluator_may_produce",
    "assess_experiences",
    "canonical_key_for",
    "evaluation_target_for_judgment",
    "independence_group_for",
]

#: 经验抽取逻辑的版本。
#:
#: 🔴 **改动抽取逻辑就必须递增它**，因为它是 ``canonical_key`` 的一段。
#:
#: 不递增的后果是无声的：新的抽取规则产出的经验与旧的**同键**，
#: 于是 PostgreSQL 的唯一约束拒绝写入，而拒绝不报错——
#: 表现是"这次运行只读到 N 条经验"，而不是"抽取失败了"。
EXTRACTOR_VERSION: str = "experience-extractor/1"

#: ``canonical_key`` 的分隔符。
#:
#: 🔴 用 ``|`` 而不是 ``:`` —— 各段里 ``:`` 是常见字符（``judgment:<uuid>``），
#: 用它做分隔符会让"两段拼接"与"一段含分隔符"无法区分，
#: 而那种碰撞的表现是**唯一约束静默拒绝第二条经验**。
_KEY_SEPARATOR = "|"


def evaluation_target_for_judgment(judgment_id: UUID) -> str:
    """一次判断在本系统里的评价对象标识。

    🔴 **为什么要包一层而不是直接用 ``judgment_id``：**

    评价对象将来不只有判断。"这次用的检索策略对不对""这条记忆读得对不对"
    都会产生经验，而它们的 id 空间互不相干——直接放裸 ``UUID`` 进去，
    一个判断的 id 和一个记忆的 id 在 ``canonical_key`` 里长得一模一样。
    加上类型前缀之后，"对什么做评价"这件事在键里就是自明的。

    Args:
        judgment_id: 判断 id。

    Returns:
        形如 ``"judgment:<uuid>"`` 的标识。
    """
    return f"judgment:{judgment_id}"


def canonical_key_for(
    *,
    cognitive_round_id: UUID,
    evaluation_target: str,
    experience_kind: ExperienceKind,
    extractor_version: str,
) -> str:
    """一条经验的规范标识（阶段 6.5 §二.10）。

    🔴 **它由四件事实共同决定，缺一不可：**

    * ``cognitive_round_id``——**哪一个回合**里发生的事；
    * ``evaluation_target``——**对什么**做的评价（判断 / 策略 / 记忆）；
    * ``experience_kind``——**哪一类**经验（回合复盘 / 别的）；
    * ``extractor_version``——**哪一版抽取逻辑**。

    前三者缺任何一个，"同一个回合里不同对象的两条经验"会撞键；
    少了第四个，抽取逻辑一变，旧经验会被新经验**静默顶掉**
    （唯一约束拒绝第二条，而拒绝是无声的）。

    ⚠️ **这里刻意不含 ``experience.id``。** id 是 ``uuid4``，
    它标识的是"这个对象"，不是"这件事"。把创建行为写进身份，
    等于允许无限复制"看起来独立"的经验。

    Args:
        cognitive_round_id: 来源回合。
        evaluation_target: 评价对象。
        experience_kind: 经验种类。
        extractor_version: 抽取器版本。

    Returns:
        形如 ``"round_outcome|<round>|judgment:<j>|v1"`` 的稳定字符串。
    """
    return _KEY_SEPARATOR.join(
        (
            experience_kind.value,
            str(cognitive_round_id),
            evaluation_target,
            extractor_version,
        )
    )


def independence_group_for(*, idempotency_key: str | None, cognitive_round_id: UUID) -> str:
    """一条经验所属的**独立性分组**（阶段 6.5 §二.12）。

    🔴 **同一个分组里的经验不是彼此独立的证据。**

    门槛的语义是"这件事在不同场合发生过三次"。判定"不同场合"用的是
    本字段，不是经验条数、也不是 ``Experience.id``。

    两条规则的优先级：

    1. **回合带幂等键时用幂等键。** 同一 ``Idempotency-Key`` 的所有
       技术重试（网络抖动后客户端重发、网关重放）共用它，
       因此它们落在同一分组里——它们是**同一次请求**。
    2. **否则用回合 id。** ``cognitive_round_id`` 在事件重放时保持不变，
       所以"把历史事件重新投影一遍"不会造出第二次发生。

    ⚠️ **诚实说明它的边界：** 客户端**不带幂等键**地重发同一句话，
    系统会当成一个新的回合（因为它无从知道那是重试）。
    要让它也塌缩成一次，只能靠比较消息正文——而那是**语义判断**，
    不是本系统做归因的方式（ADR-0018 §1）。
    这个边界是已知的残余风险，不是遗漏。

    Args:
        idempotency_key: 该回合的幂等键；没有则为 ``None``。
        cognitive_round_id: 来源回合。

    Returns:
        独立性分组标识。
    """
    if idempotency_key:
        return f"idem:{idempotency_key}"
    return f"round:{cognitive_round_id}"


class Experience(EntityMetadata):
    """一次认知回合的复盘记录（任务书 §5.12）。

    🔴 **本对象的身份由事实决定，不由创建行为决定。**
    见 :func:`canonical_key_for`。
    """

    cognitive_round_id: UUID = Field(description="来源认知回合")
    judgment_id: UUID = Field(description="当时的判断")

    situation_signature: str = Field(
        min_length=1,
        description=(
            "情境签名，用于判定「同类错误」（见 :class:`~ai_psi.domain.situations.Situation`）"
        ),
    )
    inquiry_type: str = Field(min_length=1, description="问题类型")

    evidence_available_at_time: list[UUID] = Field(
        default_factory=list,
        description=(
            "**判断发生时**就已掌握的证据。"
            "用于区分'推理错误'与'当时信息不足'——"
            "两者看起来一样，但该学到的东西完全不同"
        ),
    )

    predicted_feedback: list[str] = Field(
        default_factory=list,
        description="当时预期的反馈",
    )
    actual_feedback: list[str] = Field(
        default_factory=list,
        description="实际收到的反馈",
    )
    later_evidence_ids: list[UUID] = Field(
        default_factory=list,
        description="判断之后才出现的证据——用于归因",
    )

    error_type: ErrorType | None = Field(
        default=None,
        description="错误分类。None 表示未发现错误，或尚无法归因",
    )
    attribution_confidence: ConfidenceBand = Field(
        default=ConfidenceBand.VERY_LOW,
        description="归因置信度。低置信度的归因不应驱动任何策略变化",
    )

    strategy_used: list[str] = Field(default_factory=list, description="本次采用的分析策略")
    strategy_effectiveness: OrdinalLevel | None = Field(
        default=None,
        description="策略有效性评估",
    )

    applicable_conditions: list[str] = Field(
        default_factory=list,
        description="该经验成立的条件。**缺少条件限制的经验极易被过度推广**",
    )
    counterexamples: list[str] = Field(
        default_factory=list,
        description="反例——记录反例是阻止经验被过度推广的主要手段",
    )

    verification_status: VerificationStatus = Field(default=VerificationStatus.UNVERIFIED)

    # ------------------------------------------------------------------
    # 规范身份语义（阶段 6.5 §二.9–12）
    # ------------------------------------------------------------------

    experience_kind: ExperienceKind = Field(
        default=ExperienceKind.ROUND_OUTCOME,
        description="经验种类，参与 canonical_key 的构成",
    )
    evaluation_target: str = Field(
        min_length=1,
        description=(
            "本经验评价的是**什么**，形如 ``judgment:<uuid>``。"
            "与 experience_kind 一起，让同一个回合里不同对象的经验不撞键"
        ),
    )
    origin_event_ids: list[UUID] = Field(
        default_factory=list,
        description=(
            "产生本经验的**事件**。"
            "它是从事件回放里重建经验的锚点——少了它，"
            "「这条经验到底读的是哪几个事件」只能靠时间和 payload 猜"
        ),
    )
    independence_group: str = Field(
        min_length=1,
        description=(
            "独立性分组。🔴 同组经验**不是**彼此的独立证据。见 :func:`independence_group_for`"
        ),
    )
    canonical_key: str = Field(
        min_length=1,
        description=(
            "规范标识，由 (回合, 评价对象, 种类, 抽取器版本) 决定。"
            "🔴 PostgreSQL 上有唯一约束，重复抽取同一件事会被拒绝"
        ),
    )
    extractor_version: str = Field(
        min_length=1,
        description=(
            "抽取逻辑的版本。改动抽取逻辑必须**递增它**——"
            "否则新逻辑产出的经验会与旧的撞键并被静默拒绝"
        ),
    )

    # ------------------------------------------------------------------
    # 评价状态（阶段 6.5 §二.3–8）
    # ------------------------------------------------------------------

    evaluation: ExperienceEvaluation = Field(
        default=ExperienceEvaluation.UNASSESSED,
        description=(
            "**抽取时刻**的评价状态。"
            "🔴 此处只可能是 UNASSESSED 或 SUSPECTED——"
            "抽取发生在回合结束时，那时还没有任何外部证据"
        ),
    )
    evaluator_type: ExperienceEvaluator | None = Field(
        default=None,
        description="谁做的这个评价。UNASSESSED 时为 None",
    )
    evaluator_version: str | None = Field(
        default=None,
        description="评价逻辑的版本。与 evaluator_type 同生共死",
    )
    evaluation_evidence_refs: list[UUID] = Field(
        default_factory=list,
        description=(
            "支撑该评价的**证据引用**（反馈事件 id、后到的证据 id 等）。"
            "🔴 没有它，「为什么这条经验是 SUPPORTED」事后无法复核"
        ),
    )

    @property
    def origin_round_id(self) -> UUID:
        """本经验所评估的那件事发生在哪个回合。

        ⚠️ **这是 ``cognitive_round_id`` 的别名，不是第二个字段。**

        阶段 6.5 §二.9 用 "origin" 这个词是想强调"**来源**回合"，
        而 ``cognitive_round_id`` 从阶段 6 起就一直是这个意思。
        再存一栏会导致两栏在某个更新路径上分家——那正是这一节
        要消灭的东西（同一个事实有两个名字）。

        之所以保留 ``cognitive_round_id`` 作为**存储**名：
        事件日志是只追加的，历史上已经写进去的 ``experience.created``
        负载用的是这个名字。改字段名就是一次数据迁移，
        不是一次重命名。
        """
        return self.cognitive_round_id

    @property
    def is_attributable(self) -> bool:
        """本次经验是否足以归因到某个错误类型。

        归因置信度过低时不应生成改进提案——
        否则会产生大量基于噪声的"改进"。
        """
        return (
            self.error_type is not None
            and self.attribution_confidence.rank >= ConfidenceBand.LOW.rank
        )

    @model_validator(mode="after")
    def _check_canonical_identity(self) -> Self:
        """🔴 ``canonical_key`` 必须与它的四个来源逐字一致。

        它不是一个自由字段。允许调用方随手填一个字符串，
        等于允许"两条不同的经验共用一个键"（后者会被唯一约束
        静默拒绝），或者"同一条经验换一个键"（后者会被重复计数）。
        两种都是无声的。
        """
        expected = canonical_key_for(
            cognitive_round_id=self.cognitive_round_id,
            evaluation_target=self.evaluation_target,
            experience_kind=self.experience_kind,
            extractor_version=self.extractor_version,
        )
        if self.canonical_key != expected:
            msg = (
                f"canonical_key 与它的事实来源不一致："
                f"收到 {self.canonical_key!r}，按 "
                f"(回合, 评价对象, 种类, 抽取器版本) 应为 {expected!r}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_extraction_time_evaluation(self) -> Self:
        """🔴 **抽取时刻的评价最多只能是 SUSPECTED**（§二.4）。

        抽取发生在回合收尾，那一刻唯一的评估者是内部元认知。
        允许在这里写 ``SUPPORTED``，等于允许"系统自己确认自己"——
        而那正是本节要消灭的东西。
        """
        assert_evaluator_may_produce(
            evaluator=self.evaluator_type,
            evaluation=self.evaluation,
            where="Experience（抽取时刻）",
        )
        return self


class ExperienceEvaluationRecord(EntityMetadata):
    """对一条已有经验的**追加评价**（阶段 6.5 §二.3–5、§二.8）。

    🔴 **为什么不是直接改 ``Experience`` 上的字段：**

    经验是**不可变的观察**，留在只追加的事件流里（ADR-0018 §?）。
    用户纠正、后续证据都在经验写入**之后**才到。要表达
    "这条经验后来被用户确认了"，只有两条路：改写经验（破坏不可变
    与审计链），或者追加一条评价（本对象）。

    后者还顺带回答了一个前者答不了的问题：**「它被改过几次、
    每次是谁改的」**。一条经验从 ``SUSPECTED`` 抬到 ``CONFIRMED``
    的过程本身是有信息量的——它说明用户确实为这件事费过口舌。

    ⚠️ **本对象一旦写入就不再变化。** 允许改它，等于允许
    "先按用户的纠正抬到 CONFIRMED，再悄悄把它降回去"。
    """

    experience_id: UUID = Field(description="被评价的经验")
    experience_canonical_key: str = Field(
        min_length=1,
        description=(
            "被评价经验的 canonical_key。"
            "🔴 冗余存一份是**有意的**：经验只存在于事件日志里，"
            "而评价事件可能在任何时刻被单独读取；"
            "少了它，一次评价在脱离经验流时无法说明自己评的是谁"
        ),
    )

    evaluation: ExperienceEvaluation = Field(description="评价后的状态")
    evaluator_type: ExperienceEvaluator = Field(
        description="谁做的这个评价。🔴 不可为 None——评价记录的存在本身就是一次判断"
    )
    evaluator_version: str = Field(min_length=1, description="评价逻辑的版本")
    evidence_refs: list[UUID] = Field(
        default_factory=list,
        description="支撑该评价的证据引用（反馈事件 id 等）",
    )
    reasons: list[str] = Field(
        default_factory=list,
        description="逐条可读的理由——**不可解释的评价日后无法被推翻**",
    )
    evaluated_at: UtcDatetime = Field(description="评价发生的时刻")

    @model_validator(mode="after")
    def _check_evaluator_may_produce(self) -> Self:
        """🔴 **内部元认知不得产生 SUPPORTED / CONFIRMED**（§二.4）。"""
        assert_evaluator_may_produce(
            evaluator=self.evaluator_type,
            evaluation=self.evaluation,
            where="ExperienceEvaluationRecord",
        )
        return self

    @model_validator(mode="after")
    def _check_confirmation_needs_evidence(self) -> Self:
        """🔴 ``SUPPORTED`` / ``CONFIRMED`` 必须指出它凭什么。

        没有证据引用的"确认"是一条**不可复核的断言**。
        它会让一条经验永久地高于其他经验，而没有任何人能回答
        "当初是谁、根据什么把它抬上去的"。
        """
        if self.evaluation.rank >= ExperienceEvaluation.SUPPORTED.rank and not self.evidence_refs:
            msg = (
                f"评价为 {self.evaluation.value} 时必须给出 evidence_refs——"
                "没有证据引用的确认不可复核（阶段 6.5 §二.8）"
            )
            raise ValueError(msg)
        return self


@dataclass(frozen=True, slots=True)
class ExperienceAssessment:
    """一条经验与它的**有效**评价（阶段 6.5 §二.3）。

    🔴 **"有效"不等于 ``experience.evaluation``。**

    后者是**抽取时刻**的记录，那一刻唯一的评估者是内部元认知，
    因此它最多是 ``SUSPECTED``。用户纠正、后续证据都在那之后才到，
    以 :class:`ExperienceEvaluationRecord` 的形式追加。

    本对象是两者合并后的结果——**门槛必须按它计数**，
    而不是按抽取时刻那个必然偏低的快照。

    Attributes:
        experience: 经验本身。
        evaluation: 合并后的有效评价。
        evaluator_types: 参与过评价的**全部**评估者（去重、有序）。
            保留全部而不是只留最高分那个：``CONFIRMED`` 来自用户
            与来自独立评测，对下游是不同的信息。
        evidence_refs: 全部支撑证据引用（去重、有序）。
    """

    experience: Experience
    evaluation: ExperienceEvaluation
    evaluator_types: tuple[ExperienceEvaluator, ...] = ()
    evidence_refs: tuple[UUID, ...] = ()


def assess_experiences(
    experiences: Sequence[Experience],
    records: Sequence[ExperienceEvaluationRecord] = (),
) -> tuple[ExperienceAssessment, ...]:
    """把经验与它们的追加评价合并成有效评价（阶段 6.5 §二.3）。

    🔴 **合并规则是"取最高一档"，不是"取最后一条"。**

    事件流是只追加的，而 V0.1 **没有**"推翻一次确认"的评价路径。
    在这种情况下"最后一条说了算"会让一条被用户明确确认过的经验，
    被任何一次随后的 ``SUSPECTED`` 重评**降级**——而降级是无声的，
    它的表现只是"这条模式突然少了一次计数"。

    取最高档的代价是：将来真的需要降级时，必须**显式**引入
    一种表达否定评价的评估者，而不是靠写入顺序。那是好事——
    降低一条经验的评价等级是个需要被看见的动作。

    ⚠️ 记录里指向不存在经验的条目会被**忽略**。经验只活在事件流里，
    而事件流可能因为保留策略被裁剪；一条孤立的评价记录不该让
    整次读取失败，但它也不会凭空造出一条经验。

    Args:
        experiences: 经验集合。
        records: 评价记录集合（顺序无关）。

    Returns:
        与 ``experiences`` 同序的评估结果。
    """
    by_experience: dict[UUID, list[ExperienceEvaluationRecord]] = {}
    for record in records:
        by_experience.setdefault(record.experience_id, []).append(record)

    assessments: list[ExperienceAssessment] = []
    for experience in experiences:
        related = by_experience.get(experience.id, [])
        evaluation = experience.evaluation
        evaluators: list[ExperienceEvaluator] = []
        if experience.evaluator_type is not None:
            evaluators.append(experience.evaluator_type)
        refs: list[UUID] = list(experience.evaluation_evidence_refs)
        for record in related:
            if record.evaluation.rank > evaluation.rank:
                evaluation = record.evaluation
            evaluators.append(record.evaluator_type)
            refs.extend(record.evidence_refs)
        assessments.append(
            ExperienceAssessment(
                experience=experience,
                evaluation=evaluation,
                evaluator_types=tuple(sorted(set(evaluators), key=lambda item: item.value)),
                evidence_refs=tuple(sorted(set(refs), key=str)),
            )
        )
    return tuple(assessments)


def assert_evaluator_may_produce(
    *,
    evaluator: ExperienceEvaluator | None,
    evaluation: ExperienceEvaluation,
    where: str,
) -> None:
    """🔴 **阶段 6.5 §二.4 的唯一执行点。**

    规则只有一条：**内部元认知最多产生 ``SUSPECTED``。**

    ⚠️ 这条规则被两处用到（:class:`Experience` 的抽取时刻、
    :class:`ExperienceEvaluationRecord` 的追加评价），因此写成
    **一个函数**而不是两段各写一遍的判断。两段判断会漂移，
    而漂移的那一天，两条路径对"什么算证据"给出不同答案——
    且没有任何测试会发现，因为它们各自都是自洽的。

    Args:
        evaluator: 做出评价的一方；``None`` 表示尚未评价。
        evaluation: 评价后的状态。
        where: 出错时用来说明"在哪条路径上"。

    Raises:
        ValueError: 内部元认知试图产生 ``SUPPORTED`` / ``CONFIRMED``，
            或评价状态与评估者存在与否不一致。
    """
    if evaluator is None:
        if evaluation is not ExperienceEvaluation.UNASSESSED:
            msg = (
                f"{where}：没有评估者却给出了 {evaluation.value} 的评价。"
                "「谁说的」与「说了什么」必须同时存在"
            )
            raise ValueError(msg)
        return

    if evaluation is ExperienceEvaluation.UNASSESSED:
        msg = (
            f"{where}：评估者 {evaluator.value} 给出的评价是 unassessed。"
            "做过评价就是有状态，请用 SUSPECTED 表达「怀疑但不确认」"
        )
        raise ValueError(msg)

    if (
        evaluator is ExperienceEvaluator.INTERNAL_METACOGNITION
        and evaluation.rank > ExperienceEvaluation.SUSPECTED.rank
    ):
        msg = (
            f"{where}：内部元认知最多产生 suspected，不得产生 {evaluation.value}。"
            "🔴 内部怀疑不是证据——允许它产生 supported/confirmed，"
            "等于允许系统自己的怀疑自我确认成规律（阶段 6.5 §二.4）"
        )
        raise ValueError(msg)
