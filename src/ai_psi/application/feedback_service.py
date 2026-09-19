"""反馈与纠正的应用服务（任务书 §12.2、§10.4）。

用户对某个回合给出反馈：纠正、澄清、异议、赞同、评分。

🔴 **反馈不得绕过写入策略。**

``allow_memory_update`` 不是"允许直接写记忆"，而是"允许把这次反馈
**提给**记忆写入流程"。真正的写入仍然走
``MemoryWriteProposal → WritePolicy → MemoryService``（ADR-0004）。
把这条路径短路掉——比如在这里直接构造 ``Memory`` 落库——
会让反馈变成一条绕过全部记忆红线的旁路，而它看起来只是一次用户操作。

🔴 **只有 CORRECTION 与 CLARIFICATION 可以带出记忆。**

这两类是**唯一**提供了新信息的反馈："我说的其实是 X"。
``AGREEMENT`` / ``ACKNOWLEDGEMENT`` / ``RATING`` 不含关于世界或用户的
新事实；``DISAGREEMENT`` 只说"你错了"，没说对的应该是什么。
用它们去写记忆，等于把噪声记成事实——而记忆一旦写错，
它会在之后**每一轮**里持续影响判断。

不满足条件时结果里会**显式说明为什么没写**，而不是静默跳过：
用户打开了开关却什么都没发生，必须能从返回值里看出原因。

🔴 **反馈会把对应回合的经验抬到有证据的档位。**

阶段 6.5 §二.5：只有用户明确纠正、可靠后续证据或独立评测
才能产生 ``SUPPORTED`` / ``CONFIRMED``。内部元认知最多 ``SUSPECTED``，
而**默认权重下 ``SUSPECTED`` 计 0 次**——因此没有这条路径，
"三次同类错误"这条验收条件在系统里永远凑不满。

本服务把反馈事件与经验评价事件写在**同一个事务**里：
它们表达的是同一件事（"用户此刻指出了这个回合的问题"），
分成两个事务就会出现"纠正留下了、它应该抬高的经验没有抬高"。

⚠️ **事务边界：反馈事件、经验评价、记忆更新现在在同一个事务里。**

阶段 6 曾把它们分成两个事务，理由是"丢用户的话比丢一次记忆更新更严重"。
阶段 6.5 §三.5–6 要求三者原子，因此改成单事务
（ADR-0021：该决定被修正，ADR-0018 §4 原文保留）。

不改的是那条顾虑本身，它由**两条**机制承接：

1. **写入策略拒绝是一条正常返回，不是异常。** 反馈内容命中红线时，
   ``WritePolicy`` 拒绝写入并**照常提交**——用户的反馈与它带出的
   经验评价都留下了，只有记忆没写。这是最常见的情形。
2. **真正的数据库故障会让整体回滚，用户的反馈确实会丢。**
   这是"同一事务"的固有代价，不是实现缺陷：原子性与
   "某个子步骤失败时仍保留其余部分"在定义上互斥。
   区别在于，现在这个代价是**写在 ADR 里的、被选择过的**，
   而不是两事务方案里那个没有被讨论过的副作用。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final
from uuid import UUID, uuid4

from ai_psi.application.experience_reader import (
    attribution_from_event,
    evaluation_from_event,
    experience_from_event,
)
from ai_psi.application.memory_service import MemoryService
from ai_psi.application.ports import (
    LEARNING_TRIGGER_FAILED,
    LearningTrigger,
    LearningTriggerOutcome,
    LearningTriggerStatus,
    UnitOfWork,
    UnitOfWorkFactory,
)
from ai_psi.domain.common import utc_now
from ai_psi.domain.enums import (
    CORRECTABLE_KINDS,
    ActorType,
    CorrectedArtifactKind,
    ErrorType,
    EventType,
    ExperienceEvaluation,
    ExperienceEvaluator,
    FeedbackType,
    MemoryType,
    RoundState,
    SensitivityLevel,
    UncertaintyType,
)
from ai_psi.domain.events import Event
from ai_psi.domain.exceptions import NotFoundError
from ai_psi.domain.experiences import (
    Experience,
    ExperienceAttributionRecord,
    ExperienceEvaluationRecord,
    assess_experiences,
)
from ai_psi.domain.memories import Memory
from ai_psi.infrastructure.logging import get_logger
from ai_psi.learning.error_classifier import (
    CLASSIFIER_VERSION,
    CorrectionTarget,
    ErrorClassifier,
    ErrorSignals,
)
from ai_psi.memory.write_policy import MemoryWriteProposal

__all__ = [
    "EVALUATION_BY_FEEDBACK_TYPE",
    "FEEDBACK_MEMORY_TYPE",
    "MEMORY_UPDATING_FEEDBACK_TYPES",
    "FeedbackOutcome",
    "FeedbackService",
    "LearningTriggerOutcome",
    "LearningTriggerStatus",
    "MemoryEffect",
]

#: 本模块的日志器。
#:
#: ⚠️ 走 :func:`~ai_psi.infrastructure.logging.get_logger` 而不是
#: 自己调 ``structlog.get_logger``：脱敏处理器挂在那个统一入口上，
#: 自行获取可能绕过它，而绕过脱敏泄的是密钥或用户正文。
#:
#: 📌 **分层说明**：架构规则 2 禁止 `cognition/`、`memory/`、`learning/`
#: import `infrastructure/`，**未涵盖 `application/`**；
#: `api/app.py` 也已在模块级做同样的导入。这里只取一个日志器，
#: 不碰任何存储实现。
_logger = get_logger(__name__)

#: 反馈类型 → 它能把经验抬到的最高评价档位（阶段 6.5 §二.5）。
#:
#: 🔴 **白名单。** 没列进来的反馈类型**不产生任何评价**——
#: 新增一个反馈类型时，默认行为必须是"它不改变经验的可信度"，
#: 而不是"它悄悄地把经验抬高了一档"。
#:
#: 档位的选择依据是**这句话说了什么**：
#:
#: * ``CORRECTION``——"你这里错了，应该是 X"。它指出了具体问题，
#:   是用户能给出的最强确认 → ``CONFIRMED``；
#: * ``DISAGREEMENT``——"我不同意"。它表达了否定但没说对的是什么，
#:   因此强度低一档 → ``SUPPORTED``；
#: * ``CLARIFICATION`` / ``AGREEMENT`` / ``ACKNOWLEDGEMENT`` / ``RATING``
#:   ——补充信息、赞同、"收到"、打分。它们说的是"好不好"或
#:   "还有别的情况"，**不是"这里错了"**，因此不在此表中。
EVALUATION_BY_FEEDBACK_TYPE: Final[dict[FeedbackType, ExperienceEvaluation]] = {
    FeedbackType.CORRECTION: ExperienceEvaluation.CONFIRMED,
    FeedbackType.DISAGREEMENT: ExperienceEvaluation.SUPPORTED,
}

#: 用户纠正这条评价路径的版本。
EVALUATOR_VERSION: Final[str] = "user-correction/1"


#: 能够带出一条记忆的反馈类型。
#:
#: 🔴 白名单，不是黑名单。新增反馈类型时默认**不允许**带出记忆——
#: 一个没想到过的新类型应该先被讨论，而不是先获得写记忆的能力。
MEMORY_UPDATING_FEEDBACK_TYPES: Final[frozenset[FeedbackType]] = frozenset(
    {
        FeedbackType.CORRECTION,
        FeedbackType.CLARIFICATION,
    }
)

#: 反馈带出的记忆使用哪一种类型。
#:
#: ``USER_PREFERENCE`` 是**当前唯一**既可自动写入、又适合承载
#: "用户陈述自己立场与意图"的类型：
#:
#: * ``USER_CONFIRMED_FACT`` 需要人工评审之外还要走白名单，
#:   而它并不在白名单里——用它会得到一个永远不会落库的路径；
#: * ``SEMANTIC``（事实断言）必须人工评审，反馈不该开后门绕过它；
#: * ``EPISODIC`` / ``FAILURE_CASE`` 描述的是回合事实，不是用户陈述。
FEEDBACK_MEMORY_TYPE: Final[MemoryType] = MemoryType.USER_PREFERENCE

#: 回合没有归属用户时的拒绝理由。
#:
#: 无归属用户 → 写下去的会是一条 ``user_id=None`` 的**全局**记忆，
#: 它会出现在所有人的检索结果里——那比不写糟得多（不变量 14）。
_NO_SCOPE_REASON: Final[str] = "该回合没有归属用户，无法确定记忆的作用域（不变量 14）"


class MemoryEffect(StrEnum):
    """一次反馈对长期记忆产生的结果。"""

    NONE = "none"
    """未请求更新记忆。"""

    NOT_ELIGIBLE = "not_eligible"
    """请求了，但这次反馈不满足更新记忆的条件。"""

    WRITTEN = "written"
    """已写入一条新记忆。"""

    DUPLICATE = "duplicate"
    """内容与既有记忆完全相同，未重复写入。"""

    REJECTED_BY_POLICY = "rejected_by_policy"
    """写入策略未放行（含需要额外确认的情形）。"""


@dataclass(frozen=True, slots=True)
class FeedbackOutcome:
    """一次反馈处理的结果。

    Attributes:
        round_id: 被反馈的回合。
        feedback_type: 反馈类型。
        event: 本次反馈留下的审计事件。
        memory_effect: 对长期记忆的实际影响。
        memory: 写入的记忆；没有写入时为 ``None``。
        evaluations: 本次反馈对经验评价的**实际改动**（阶段 6.5 §二.5）。
            空元组表示这次反馈没有改变任何经验的评价——可能是反馈类型
            本就不携带"这里错了"的信息，也可能是对应经验的档位已经更高。
            两者的区别在 ``reasons`` 里。
        attributions: 本次反馈对经验归因的**实际改动**（阶段 6.6）。
            它回答"这条纠正在系统里变成了哪一类错"——空元组表示
            这次纠正没有形成归因（没给指针、指针解析不到、
            或该经验已经归过同一类因）。
        learning: 提交之后那次学习运行的结果。
            🔴 ``None`` 与 ``status=NOT_TRIGGERED`` 都表示"没触发"，
            但前者是"根本没走到那一步"，后者是"走过去了、条件不满足"。
        reasons: 逐条可读的说明——**包括"为什么没写记忆"、
            "为什么没有归因"**。
    """

    round_id: UUID
    feedback_type: FeedbackType
    event: Event
    memory_effect: MemoryEffect = MemoryEffect.NONE
    memory: Memory | None = None
    evaluations: tuple[ExperienceEvaluationRecord, ...] = ()
    attributions: tuple[ExperienceAttributionRecord, ...] = ()
    learning: LearningTriggerOutcome | None = None
    reasons: tuple[str, ...] = ()

    @property
    def memory_written(self) -> bool:
        """是否真的产生了新记忆。"""
        return self.memory is not None

    @property
    def evaluation_changed(self) -> bool:
        """本次反馈是否提高了至少一条经验的评价。"""
        return bool(self.evaluations)


@dataclass(frozen=True, slots=True)
class _TriggerStep:
    """提交之后那一步的结果 + 要追加到 ``FeedbackOutcome.reasons`` 的话。

    ⚠️ 单独一个类型而不是直接返回元组：元组在这个位置会被读成
    ``(结果, 理由)`` 还是 ``(理由, 结果)`` 全靠记，而它们两个
    都是"看起来合理"的——正是那种改一次就悄悄反过来的签名。
    """

    outcome: LearningTriggerOutcome
    reasons: tuple[str, ...] = ()


def _existing_attributions(
    events: Sequence[Event],
) -> set[tuple[UUID, ErrorType, UUID]]:
    """本回合**已经记过**的归因三元组 ``(经验, 类别, 被指产物)``。

    🔴 用它而不是"数条数"：重复纠正要挡的是**同一件事说两遍**，
    而不是"这个回合的归因条数超了"。两件事说两遍是正常的，
    同一件事说两遍不是。
    """
    seen: set[tuple[UUID, ErrorType, UUID]] = set()
    for event in events:
        if event.event_type is not EventType.EXPERIENCE_ATTRIBUTED:
            continue
        payload = event.payload.get("attribution")
        if not isinstance(payload, dict):  # pragma: no cover - 负载恒为字典
            continue
        try:
            seen.add(
                (
                    UUID(str(payload["experience_id"])),
                    ErrorType(payload["error_type"]),
                    UUID(str(payload["related_artifact_id"])),
                )
            )
        except (KeyError, ValueError):  # pragma: no cover - 坏负载不该让反馈失败
            continue
    return seen


def _resolve_correction_target(
    events: Sequence[Event], artifact_id: UUID
) -> tuple[CorrectionTarget | None, str]:
    """在**本回合**的事件流里把 id 反查成"哪一类产物"。

    🔴 **绑定是解析方式本身带来的，不是一句注释。**

    候选集合只有"这条回合的事件流"，因此：
    别的回合的产物、别的用户/租户的记忆，**根本不在这张表里**——
    不需要额外的过滤条件，也就不会因为忘写一个 `WHERE user_id` 而漏。

    ⚠️ 返回的第二种情况（"找到了但不接受纠正"）与第一种（"找不到"）
    是**两件事**，理由必须分开说：前者是调用方指对了但这类东西
    不该走这条路，后者是调用方指错了。

    Returns:
        ``(纠正目标, 失败理由)``。成功时理由为空串。
    """
    for event in events:
        for key, kind in _PAYLOAD_KINDS.items():
            if event.event_type is not kind[0]:
                continue
            body = event.payload.get(key)
            if not isinstance(body, dict):
                continue
            if str(body.get("id")) != str(artifact_id):
                continue
            artifact_kind = kind[1]
            if artifact_kind not in CORRECTABLE_KINDS:
                return None, (
                    f"被指到的是一条**{_REJECTED_KIND_LABEL[artifact_kind]}**，"
                    "这类产物不接受纠正——因此不归因（但你的反馈已经记下了）"
                )
            return (
                CorrectionTarget(
                    artifact_kind=artifact_kind,
                    has_supporting_evidence=bool(body.get("supporting_evidence_ids")),
                    uncertainty_type=_uncertainty_of(body),
                ),
                "",
            )

    return None, (
        f"related_artifact_id={artifact_id} **在本回合的产物里解析不到**——"
        "它可能是别的回合的产物、别的用户的数据，或者只是一个随机 UUID。"
        "🔴 解析不到就不归因：为了让它「有归因」而接受一个来历不明的 id，"
        "等于把归因规则交给调用方决定"
    )


def _uncertainty_of(judgment_payload: dict[str, Any]) -> UncertaintyType | None:
    """从判断负载里取 ``uncertainty_type``；取不到或不是成员时返回 ``None``。

    ⚠️ **不做字符串到枚举的"尽力转换"**：负载里的值不是成员时，
    说明它的形状与 ``Judgment`` 对不上，那时**当作没有这个信号**
    比猜一个成员安全——猜错会让一条价值判断被归成推理错误。
    """
    raw = judgment_payload.get("uncertainty_type")
    if isinstance(raw, UncertaintyType):
        return raw
    if not isinstance(raw, str):  # pragma: no cover - 恒为字符串或缺失
        return None
    try:
        return UncertaintyType(raw)
    except ValueError:  # pragma: no cover - 负载形状异常
        return None


#: 事件负载里的键 → ``(事件类型, 产物类别)``。
#:
#: 🔴 **只认这张表里的键。** 认不出来的 id 一律"解析不到"——
#: 白名单，因为"允许纠正什么"必须是一次有意的决定。
_PAYLOAD_KINDS: Final[dict[str, tuple[EventType, CorrectedArtifactKind]]] = {
    "hypothesis": (EventType.HYPOTHESIS_CREATED, CorrectedArtifactKind.HYPOTHESIS),
    "judgment": (EventType.JUDGMENT_CREATED, CorrectedArtifactKind.JUDGMENT),
    "inquiry": (EventType.INQUIRY_CREATED, CorrectedArtifactKind.INQUIRY),
}

#: 不可纠正类别在理由里的说法（给人看的那句话）。
_REJECTED_KIND_LABEL: Final[dict[CorrectedArtifactKind, str]] = {
    CorrectedArtifactKind.MEMORY: "记忆",
    CorrectedArtifactKind.INQUIRY: "问题（inquiry）",
}


class FeedbackService:
    """反馈的落地入口。"""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        memory_service: MemoryService,
        learning_trigger: LearningTrigger,
        classifier: ErrorClassifier | None = None,
    ) -> None:
        """初始化。

        Args:
            uow_factory: 工作单元工厂（用于读回合与写反馈事件）。
            memory_service: 记忆服务。**必须是同一个实例**——
                另造一个会让反馈路径与其余路径各持一份写入策略，
                策略一旦被局部替换就会分家。
            learning_trigger: 提交之后触发一次学习运行的入口
                （阶段 6.6，见 :class:`~ai_psi.application.ports.LearningTrigger`）。
                🔴 **刻意没有默认值**：给一个 ``None`` 默认值意味着
                "忘了接"的症状是**静默地不再触发学习**——而那个症状
                与"还没有攒够三次"在外部看来一模一样。
            classifier: 错误分类器；``None`` 时用默认实现。

        Note:
            🔴 **本服务在提交之后会调用一次学习链路。**
            反馈事件、经验评价、经验归因、记忆更新仍然在**一个**事务里；
            学习运行在**那之后**、用自己的事务读已提交的数据。
            顺序不能反：在提交之前调它，它会读不到刚才那条归因。
        """
        self._uow_factory = uow_factory
        self._memory_service = memory_service
        self._learning_trigger = learning_trigger
        self._classifier = classifier if classifier is not None else ErrorClassifier()

    @property
    def memory_service(self) -> MemoryService:
        """当前使用的记忆服务。"""
        return self._memory_service

    async def record(
        self,
        *,
        round_id: UUID,
        feedback_type: FeedbackType,
        content: str,
        related_claim: str | None = None,
        related_artifact_id: UUID | None = None,
        allow_memory_update: bool = False,
        actor_id: str = "user",
    ) -> FeedbackOutcome:
        """记录一次反馈，并在允许时尝试更新长期记忆。

        Args:
            round_id: 被反馈的认知回合。
            feedback_type: 反馈类型。
            content: 反馈正文。
            related_claim: 用户指出的、被纠正的具体说法（自由文本，给人看）。
            related_artifact_id: 用户指出的、被纠正的那个产物的 id
                （阶段 6.6）。🔴 **它是机器可解析的指针**，服务端拿本回合的
                事件流把它反查成"哪一类产物"——客户端说不了谎。
                解析不到就不归因，但**反馈照常成功**。
            allow_memory_update: 是否允许本次反馈更新长期记忆。
                🔴 这是"允许提给写入流程"，不是"允许写入"。
            actor_id: 发起者标识。

        Returns:
            处理结果。

        Raises:
            NotFoundError: 回合不存在。
        """
        correlation = uuid4()

        # 🔴 **整段在一个事务里**（阶段 6.5 §三.5–6）：
        # 反馈事件、经验评价、记忆更新要么都留下，要么都不留。
        async with self._uow_factory() as uow:
            round_ = await uow.rounds.get(round_id)
            if round_ is None:
                msg = f"认知回合不存在：{round_id}"
                raise NotFoundError(msg, context={"cognitive_round_id": str(round_id)})
            user_id = round_.user_id

            # 先算清"这次反馈够不够格写记忆"，再让事件如实记下这个判定。
            ineligible = (
                self._memory_update_blocker(feedback_type=feedback_type, user_id=user_id)
                if allow_memory_update
                else None
            )

            event = self._feedback_event(
                round_id=round_id,
                user_id=user_id,
                correlation_id=correlation,
                actor_id=actor_id,
                feedback_type=feedback_type,
                content=content,
                related_claim=related_claim,
                allow_memory_update=allow_memory_update,
                eligible=allow_memory_update and ineligible is None,
                ineligible_reason=ineligible,
            )
            await uow.events.append(event)

            # 🔴 反馈对**经验评价**的影响，与反馈事件同事务（§二.5）。
            evaluations, evaluation_reasons = await self._evaluate_experiences(
                uow=uow,
                round_id=round_id,
                user_id=user_id,
                feedback_type=feedback_type,
                correlation_id=correlation,
                actor_id=actor_id,
                feedback_event_id=event.id,
            )

            # 🔴 反馈对**经验归因**的影响（阶段 6.6），同样与它同事务。
            # 评价说"该不该计权"，归因说"是哪一类错"——两件事，两个事件，
            # 但必须和这次反馈同生共死。
            attributions, attribution_reasons = await self._attribute_experiences(
                uow=uow,
                round_id=round_id,
                round_state=round_.state,
                feedback_type=feedback_type,
                related_artifact_id=related_artifact_id,
                correlation_id=correlation,
                actor_id=actor_id,
                feedback_event_id=event.id,
            )

            effect, memory, memory_reasons = await self._memory_step(
                uow=uow,
                round_id=round_id,
                user_id=user_id,
                correlation_id=correlation,
                actor_id=actor_id,
                feedback_type=feedback_type,
                content=content,
                feedback_event_id=event.id,
                allow_memory_update=allow_memory_update,
                ineligible=ineligible,
            )

            await uow.commit()

        # 🔴 **提交之后**才触发学习：学习链路开自己的事务读事件流，
        # 在提交之前调它，它看不到刚才那条归因——而症状是
        # "第三次纠正到了、库里也有归因，但提案不出现"（阶段 6.5 的
        # C6.7 就是这个形态）。见 `ports.LearningTrigger`。
        #
        # ⚠️ **这一行是承重的，实证过**：把它挪到 `commit()` 之前、
        # 或直接删掉，黑盒场景 A 立刻变红（提案列表为空）。
        # 单元测试**发现不了**这种改法——内存夹具里的事务边界更松，
        # 只有在真实 PostgreSQL 的开事务语义下才暴露。
        learning = await self._run_learning_after_commit(
            round_id=round_id,
            reasons=attribution_reasons,
        )

        return FeedbackOutcome(
            round_id=round_id,
            feedback_type=feedback_type,
            event=event,
            memory_effect=effect,
            memory=memory,
            evaluations=evaluations,
            attributions=attributions,
            learning=learning.outcome,
            reasons=(*evaluation_reasons, *attribution_reasons, *memory_reasons, *learning.reasons),
        )

    async def _memory_step(
        self,
        *,
        uow: UnitOfWork,
        round_id: UUID,
        user_id: UUID | None,
        correlation_id: UUID,
        actor_id: str,
        feedback_type: FeedbackType,
        content: str,
        feedback_event_id: UUID,
        allow_memory_update: bool,
        ineligible: str | None,
    ) -> tuple[MemoryEffect, Memory | None, tuple[str, ...]]:
        """在**已开启的事务**里走记忆写入流程。

        🔴 **返回原因而不是抛异常。** 记忆被策略拒绝是这条链路的
        正常分支之一（用户说了句命中红线的话），它不该让整个事务
        回滚——那样连反馈本身都留不下。

        Returns:
            ``(影响, 写入的记忆或 None, 理由)``。
        """
        if not allow_memory_update:
            # 🔴 "没请求"与"请求了但不满足"是两回事，不合并成一种结果。
            # 合并之后，调用方无法区分"我忘了开开关"与"我开了但被规则挡住了"。
            return (
                MemoryEffect.NONE,
                None,
                ("本次反馈未请求更新记忆（allow_memory_update=false）",),
            )

        if ineligible is not None:
            return (MemoryEffect.NOT_ELIGIBLE, None, (ineligible,))

        # 到此 ``user_id`` 必定非空：匿名回合会被上面那条判定挡住。
        if user_id is None:  # pragma: no cover - 上一条判定已覆盖，防御性保留
            return (MemoryEffect.NOT_ELIGIBLE, None, (_NO_SCOPE_REASON,))

        outcome = await self._memory_service.propose(
            proposal=MemoryWriteProposal(
                user_id=user_id,
                memory_type=FEEDBACK_MEMORY_TYPE,
                content=content,
                sensitivity=SensitivityLevel.PERSONAL,
                # 指向那条反馈事件——"这条记忆为什么会出现"因此可追
                source_event_ids=(feedback_event_id,),
                user_confirmed=True,
            ),
            actor_id=actor_id,
            correlation_id=correlation_id,
            # 🔴 把事务交出去：记忆写入长在反馈的事务上（§三.5–6）
            uow=uow,
        )

        if outcome.written:
            return (MemoryEffect.WRITTEN, outcome.memory, ("反馈已写入长期记忆",))
        if outcome.duplicate_of is not None:
            return (MemoryEffect.DUPLICATE, None, ("内容与既有记忆完全相同，未重复写入",))
        return (
            MemoryEffect.REJECTED_BY_POLICY,
            None,
            (f"写入策略未放行（{outcome.decision.value}）", *outcome.reasons),
        )

    # ------------------------------------------------------------------
    # 记忆更新
    # ------------------------------------------------------------------

    def _memory_update_blocker(
        self,
        *,
        feedback_type: FeedbackType,
        user_id: UUID | None,
    ) -> str | None:
        """判断本次反馈能否带出记忆（调用方已确认确实请求了更新）。

        Returns:
            ``None`` 表示可以；否则是**不能的原因**。
            返回原因而不是布尔，是因为"为什么没写"必须能被读出来。
        """
        if feedback_type not in MEMORY_UPDATING_FEEDBACK_TYPES:
            allowed = "、".join(sorted(item.value for item in MEMORY_UPDATING_FEEDBACK_TYPES))
            return (
                f"反馈类型 {feedback_type.value} 不携带新信息，不作为记忆来源"
                f"（允许的类型：{allowed}）——"
                "赞同与评分说的是「你做得对不对」，不是「事实是什么」"
            )
        if user_id is None:
            return _NO_SCOPE_REASON
        return None

    # ------------------------------------------------------------------
    # 经验评价（阶段 6.5 §二.5）
    # ------------------------------------------------------------------

    async def _evaluate_experiences(
        self,
        *,
        uow: UnitOfWork,
        round_id: UUID,
        user_id: UUID | None,
        feedback_type: FeedbackType,
        correlation_id: UUID,
        actor_id: str,
        feedback_event_id: UUID,
    ) -> tuple[tuple[ExperienceEvaluationRecord, ...], tuple[str, ...]]:
        """把本回合的经验抬到这次反馈所支持的档位。

        🔴 **这是"内部元认知最多 SUSPECTED"这条规则的出口。**

        没有它，生产里能产出的经验永远停在 ``SUSPECTED``，
        而 ``SUSPECTED`` 在默认权重下计 0 次——"三次同类错误
        生成提案"因此在系统里**永远不可能发生**。

        档位映射（§二.5）：

        * ``CORRECTION``——用户**指出了哪里不对**，这是明确的确认，
          抬到 ``CONFIRMED``；
        * ``DISAGREEMENT``——用户只说"不同意"，没说对的是什么，
          抬到 ``SUPPORTED``；
        * 其余类型（赞同 / 确认收到 / 评分 / 澄清）**不产生评价**。
          澄清与评分说的是"补充信息"和"好不好"，不是"这里错了"。

        ⚠️ **只在真的会抬高时才写。** 用户对同一个回合点两次纠正
        不该产生两条评价记录——那不是"更确认"，只是重复。

        Args:
            uow: 已开启的工作单元（与反馈事件同一个事务）。
            round_id: 被反馈的回合。
            user_id: 归属用户。
            feedback_type: 反馈类型。
            correlation_id: 关联链标识。
            actor_id: 发起者标识。
            feedback_event_id: 反馈事件 id，作为评价的证据引用。

        Returns:
            ``(写入的评价记录, 理由)``。
        """
        target = EVALUATION_BY_FEEDBACK_TYPE.get(feedback_type)
        if target is None:
            return (), ()

        events = await uow.events.read_stream(cognitive_round_id=round_id)
        created = [item for item in events if item.event_type is EventType.EXPERIENCE_CREATED]
        if not created:
            return (
                (),
                (
                    "本回合没有产出经验（失败回合、或该回合关闭了长期记忆），"
                    "本次反馈没有被记到任何经验上",
                ),
            )

        experiences = [
            item for item in (experience_from_event(event) for event in created) if item is not None
        ]
        current = await self._current_evaluations(uow, experiences)

        written: list[ExperienceEvaluationRecord] = []
        skipped = 0
        for experience in experiences:
            if current.get(experience.id, experience.evaluation).rank >= target.rank:
                skipped += 1
                continue
            record = ExperienceEvaluationRecord(
                created_by=actor_id,
                experience_id=experience.id,
                experience_canonical_key=experience.canonical_key,
                evaluation=target,
                evaluator_type=ExperienceEvaluator.USER_CORRECTION,
                evaluator_version=EVALUATOR_VERSION,
                evidence_refs=[feedback_event_id],
                reasons=[
                    f"用户在回合 {round_id} 上给出了 {feedback_type.value} 反馈",
                    f"评价由 {self._describe_prior(current.get(experience.id))} "
                    f"抬到 {target.value}",
                ],
                evaluated_at=utc_now(),
            )
            await uow.events.append(
                Event(
                    event_type=EventType.EXPERIENCE_EVALUATED,
                    occurred_at=utc_now(),
                    actor_type=ActorType.USER,
                    actor_id=actor_id,
                    user_id=user_id,
                    conversation_id=None,
                    cognitive_round_id=round_id,
                    correlation_id=correlation_id,
                    payload={"evaluation": record.model_dump(mode="json")},
                    sensitivity=SensitivityLevel.INTERNAL,
                )
            )
            written.append(record)

        reasons: list[str] = []
        if written:
            reasons.append(
                f"{len(written)} 条经验的评价被抬到 {target.value}"
                f"（依据：{feedback_type.value} 反馈）"
            )
        if skipped:
            reasons.append(f"{skipped} 条经验的评价已不低于 {target.value}，未重复记")
        return tuple(written), tuple(reasons)

    async def _run_learning_after_commit(
        self,
        *,
        round_id: UUID,
        reasons: tuple[str, ...],
    ) -> _TriggerStep:
        """🔴 **提交之后**决定"要不要跑学习"，并把它跑掉（阶段 6.6）。

        ## 为什么必须在这里、必须在这个时刻

        学习链路（``LearningService.review``）通过 ``ExperienceReader``
        开**自己的**事务读事件流。在反馈的事务里调它，那条新连接
        **看不到尚未提交的归因**——症状是"第三次纠正到了、库里也有归因，
        但提案不出现"，而库里一切正常。这正是阶段 6.5 记下的
        C6.7 那种失败形态，所以本方法只在 ``uow.commit()` 之后被调用。

        ## 触发条件

        **本次反馈之后，该回合存在有效归因**（不论是不是本次新写的）。

        用"之后存在"而不是"本次新写了"，是为了对**重试**幂等：
        重复的纠正不会写第二条归因（去重在那里），但仍然会触发一次运行。
        于是"学习链路偶发失败 + 客户端重试"不会把这条链路**永久卡死**
        ——那比多跑一次糟得多。

        ⚠️ **"重试不得重复创建 Proposal" 不由这里保证**，由提案层保证：
        ``LearningService._covered_keys`` 让已存在的
        ``(error_class, signature)`` 不再生成。触发器**不自己记
        "跑过了"**——那会是第二份真相来源，而两份真相迟早会分家。

        Args:
            round_id: 刚被反馈的回合。
            reasons: 归因那一步给的理由，追加到结果里。

        Returns:
            ``(触发结果, 补充理由)``。
        """
        if not await self._round_has_effective_attribution(round_id):
            return _TriggerStep(
                outcome=LearningTriggerOutcome(status=LearningTriggerStatus.NOT_TRIGGERED),
                reasons=reasons,
            )

        try:
            outcome = await self._learning_trigger()
        except Exception as error:
            # 🔴 **捕获一切，这是刻意的。**
            # 反馈与归因**已经提交**；把异常放出去会把一个已经成功的
            # 反馈报成 500，客户端于是重试，而重试会走到同一条死路。
            # 代价是这里吞掉了异常类型——补偿是：完整异常进服务端日志，
            # 对外给一个**稳定错误码 + trace_id**，两者能对上。
            trace_id = uuid4().hex
            _logger.error(
                "learning_trigger_failed",
                trace_id=trace_id,
                round_id=str(round_id),
                error_type=type(error).__name__,
                error=str(error),
                exc_info=True,
            )
            return _TriggerStep(
                outcome=LearningTriggerOutcome(
                    status=LearningTriggerStatus.FAILED,
                    error_code=LEARNING_TRIGGER_FAILED,
                    trace_id=trace_id,
                ),
                reasons=(
                    *reasons,
                    f"学习链路在提交之后运行失败（{LEARNING_TRIGGER_FAILED}，"
                    f"trace_id={trace_id}）。🔴 反馈与归因**已经提交**，不受影响；"
                    "完整异常只写在服务端日志里——API 不回异常原文",
                ),
            )

        return _TriggerStep(
            outcome=outcome,
            reasons=(
                *reasons,
                (
                    f"提交之后触发了一次学习链路；本次新建提案 "
                    f"{len(outcome.created_proposal_ids)} 条"
                ),
            ),
        )

    async def _round_has_effective_attribution(self, round_id: UUID) -> bool:
        """该回合现在有没有**可用**的归因（阶段 6.6）。

        ⚠️ 用 ``assess_experiences`` 而不是"数一下有没有
        ``experience.attributed`` 事件"：一条归因可能与经验自带的类别
        冲突，那时它是**不可用**的。数事件会把它算成"有归因"，
        于是触发一次注定什么也发现不了的学习运行。
        """
        async with self._uow_factory() as uow:
            events = await uow.events.read_stream(cognitive_round_id=round_id)
        created = [item for item in events if item.event_type is EventType.EXPERIENCE_CREATED]
        experiences = [
            item for item in (experience_from_event(event) for event in created) if item is not None
        ]
        attributions = [
            item for item in (attribution_from_event(event) for event in events) if item is not None
        ]
        assessments = assess_experiences(experiences, (), attributions)
        return any(item.effective_error_type is not None for item in assessments)

    async def _attribute_experiences(
        self,
        *,
        uow: UnitOfWork,
        round_id: UUID,
        round_state: RoundState,
        feedback_type: FeedbackType,
        related_artifact_id: UUID | None,
        correlation_id: UUID,
        actor_id: str,
        feedback_event_id: UUID,
    ) -> tuple[tuple[ExperienceAttributionRecord, ...], tuple[str, ...]]:
        """把这次纠正变成对某条经验的**错误归因**（阶段 6.6，ADR-0023）。

        🔴 **两个条件缺一不可**：反馈类型是否定性的（``CORRECTION`` /
        ``DISAGREEMENT``），**并且**它指得出被纠正的是哪一条产物。
        任一条不满足 → 不归因（``error_type`` 保持 ``None``）。

        这比"用户说了不对就归一个类"诚实得多：只说得出"有错"、
        说不出"错在哪一条"，不足以支撑一个错误类别，而**归错类
        比不归因糟得多**——它会让模式发现把互不相干的错误聚成一类。

        ⚠️ **不归因不是失败**：反馈事件、评价、记忆都照常落库，
        理由逐条回给调用方（"为什么这次纠正没有形成归因"）。

        Returns:
            ``(写入的归因记录, 理由)``。
        """
        corrections = {FeedbackType.CORRECTION, FeedbackType.DISAGREEMENT}
        if feedback_type not in corrections:
            # 赞同 / 澄清 / 评分说的是别的事，它们不构成"这里错了"。
            return (), ()

        events = await uow.events.read_stream(cognitive_round_id=round_id)
        created = [item for item in events if item.event_type is EventType.EXPERIENCE_CREATED]
        if not created:
            return (
                (),
                ("本回合没有产出经验，这次纠正没有被归到任何经验上",),
            )

        if related_artifact_id is None:
            return (
                (),
                (
                    "这次纠正**没有指出被纠正的是哪一条产物**"
                    "（未提供 related_artifact_id）——"
                    "只说得出「有错」、说不出「错在哪一条」，因此不归因",
                ),
            )

        target, why = _resolve_correction_target(events, related_artifact_id)
        if target is None:
            return (), (why,)

        # 🔴 分类器走**整条链条**，不是只跑 `_from_user_correction`。
        # 前面那些结构性判据（失败 / 预算耗尽 / 元认知信号）优先于用户纠正——
        # 用户说"错了"，而元认知说"错在哪一层"，后者更有分辨力。
        # 走到用户纠正那一条，恰恰是"抽取时刻没有任何结构信号"的情形，
        # 也就是本阶段要补上的那个缺口。
        attribution = self._classifier.classify(
            ErrorSignals(
                round_state=round_state,
                feedback_types=(feedback_type,),
                correction=target,
            )
        )
        if attribution.error_type is None:
            return (), attribution.reasons

        experiences = [
            item for item in (experience_from_event(event) for event in created) if item is not None
        ]
        already = _existing_attributions(events)

        written: list[ExperienceAttributionRecord] = []
        for experience in experiences:
            # 🔴 **重复纠正不写第二条**：同一个 feedback 事件重放、
            # 或用户在同一个回合上点了两次同样的纠正，都落在这一条上。
            # 少了它，重复反馈会让归因条数涨上去——而门槛数的是
            # `independence_group`，涨条数本身不抬门槛，却会让审计视图骗人。
            if (experience.id, attribution.error_type, related_artifact_id) in already:
                continue
            record = ExperienceAttributionRecord(
                created_by=actor_id,
                experience_id=experience.id,
                experience_canonical_key=experience.canonical_key,
                cognitive_round_id=round_id,
                judgment_id=experience.judgment_id,
                related_artifact_id=related_artifact_id,
                artifact_kind=target.artifact_kind,
                error_type=attribution.error_type,
                confidence=attribution.confidence,
                reasons=list(attribution.reasons),
                classifier_version=CLASSIFIER_VERSION,
                evidence_refs=[feedback_event_id],
            )
            await uow.events.append(
                Event(
                    event_type=EventType.EXPERIENCE_ATTRIBUTED,
                    occurred_at=utc_now(),
                    actor_type=ActorType.USER,
                    actor_id=actor_id,
                    correlation_id=correlation_id,
                    cognitive_round_id=round_id,
                    payload={"attribution": record.model_dump(mode="json")},
                )
            )
            written.append(record)

        if not written:
            return (
                (),
                (
                    "这次纠正指向的产物与类别**已经记过一次**，不重复记——"
                    "重复的纠正不是「更确认」，只是同一件事说了两遍",
                ),
            )
        return tuple(written), ()

    async def _current_evaluations(
        self,
        uow: UnitOfWork,
        experiences: list[Experience],
    ) -> dict[UUID, ExperienceEvaluation]:
        """本回合这些经验**当前**的有效评价。"""
        if not experiences:
            return {}
        known = {item.id for item in experiences}
        records = [
            record
            for event in await uow.events.read_by_event_type(
                event_type=EventType.EXPERIENCE_EVALUATED
            )
            if (record := evaluation_from_event(event)) is not None
            and record.experience_id in known
        ]
        return {
            item.experience.id: item.evaluation for item in assess_experiences(experiences, records)
        }

    @staticmethod
    def _describe_prior(evaluation: ExperienceEvaluation | None) -> str:
        """把"抬升前是什么"写进理由。"""
        return evaluation.value if evaluation is not None else "unassessed"

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------

    def _feedback_event(
        self,
        *,
        round_id: UUID,
        user_id: UUID | None,
        correlation_id: UUID,
        actor_id: str,
        feedback_type: FeedbackType,
        content: str,
        related_claim: str | None,
        allow_memory_update: bool,
        eligible: bool,
        ineligible_reason: str | None,
    ) -> Event:
        """构造 ``user.feedback.received`` 事件（**不落库**）。

        🔴 **负载里有 ``content``，而且不能没有。**

        这与记忆审计负载的脱敏规则（§10.5）**不冲突**：那条规则管的是
        "不得保留**被删除内容**的正文"，而反馈正文是**对话记录**——
        事件流里本来就存着 ``user.message.received`` 的正文。
        把反馈正文摘掉，这条事件就只剩「用户点过一次纠正」，
        事后既说不清他纠正了什么，也说不清那条记忆是从哪句话来的。

        ⚠️ 因此它也继承了同一条边界：``DELETE /users/{id}/data``
        只覆盖记忆，**不覆盖事件流**。这一点在接口文档里对用户明说了。

        🔴 ``memory_effect`` **不在这里预测**。事件只记录这次请求是
        什么、以及是否够格提给写入流程；实际结果是记忆层自己的事件
        （``memory.approved`` / ``memory.rejected``）说的。
        写一个猜的结果进不可变更的事件里，就是在制造一条会过期的记录。
        """
        payload: dict[str, object] = {
            "feedback_type": feedback_type.value,
            "content": content,
            "content_length": len(content),
            "related_claim": related_claim,
            "allow_memory_update": allow_memory_update,
            "memory_update_eligible": eligible,
            "memory_update_ineligible_reason": ineligible_reason,
        }
        return Event(
            event_type=EventType.USER_FEEDBACK_RECEIVED,
            occurred_at=utc_now(),
            actor_type=ActorType.USER,
            actor_id=actor_id,
            user_id=user_id,
            conversation_id=None,
            # 🔴 挂在被反馈的回合上：§12.2 的反馈是**回合级**的，
            # 因此"这个回合收到了什么反馈"沿事件流就能读出来
            cognitive_round_id=round_id,
            correlation_id=correlation_id,
            payload=payload,
            sensitivity=SensitivityLevel.PERSONAL,
        )
