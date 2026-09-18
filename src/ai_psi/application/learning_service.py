"""学习链路的**显式入口**（任务书 §11.1–§11.4）。

🔴 **这里回答的问题：谁、在什么时候，让系统从经验里学到东西？**

`learning/` 的六个模块都是**纯函数**——没有定时器、没有钩子、没有副作用。
那是刻意的（ADR-0018 §10：自动生成提案等于用噪声喂评审）。
但纯函数库本身不构成能力：在阶段 6 收尾之前，
`PatternDetector` / `PromotionPolicy` / `ProposalGenerator` 在 `src/` 里
**零调用者**，"三次同类错误可生成 Proposal"这条验收条件
只在测试里成立、在系统里不可操作。

本服务把四个纯组件串成一条可执行的链路：

```
事件流里的 Experience
  → PatternDetector.detect        （同类错误出现了几次）
  → PromotionPolicy.decide        （够不够格成为提案）
  → ProposalGenerator.generate    （生成 DRAFT 草案）
  → ProposalService.create        （落库 + 留审计事件）
```

🔴 **提案仍然不会自动生效，本服务也没有任何让它们生效的能力。**
它只生成 `DRAFT`；评估与批准仍然要人来做（不变量 11）。

⚠️ 本服务**读经验、不写经验**。经验由认知运行时在回合收尾时写入
（成功路径）与失败路径写入，它们是不可变的观察，留事件流里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final
from uuid import UUID, uuid4

from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.application.proposal_service import ProposalService
from ai_psi.domain.enums import EventType, FeedbackType
from ai_psi.domain.events import Event
from ai_psi.domain.experiences import Experience
from ai_psi.domain.improvement_proposals import ImprovementProposal
from ai_psi.learning.pattern_detector import ErrorPattern, PatternDetector
from ai_psi.learning.promotion_policy import (
    SEVERE_ERROR_TYPES,
    PromotionDecision,
    PromotionEvidence,
    PromotionPolicy,
)
from ai_psi.learning.proposal_generator import ProposalGenerator

__all__ = ["LearningRun", "LearningService", "ProposalCandidate"]


#: 被视为"用户表达了否定"的反馈类型。
#:
#: 与 :data:`~ai_psi.learning.error_classifier` 里那组保持一致——
#: 两处判定同一件事，分开写会让"什么算纠正"在归因与计数上给出不同答案。
NEGATIVE_FEEDBACK_TYPES: Final[frozenset[FeedbackType]] = frozenset(
    {FeedbackType.CORRECTION, FeedbackType.DISAGREEMENT}
)


@dataclass(frozen=True, slots=True)
class ProposalCandidate:
    """一个达到门槛、可以生成提案的观察。"""

    pattern: ErrorPattern
    decision: PromotionDecision


@dataclass(frozen=True, slots=True)
class LearningRun:
    """一次学习链路运行的结果。

    🔴 **每个字段都要能被读出来，包括"什么都没发生"的原因。**

    一份只说"生成了 0 条提案"的报告无法回答"为什么没有"——
    而那正是下一次运行时最需要知道的事。
    """

    experiences_considered: int = 0
    patterns: tuple[ErrorPattern, ...] = ()
    candidates: tuple[ProposalCandidate, ...] = ()
    created: tuple[ImprovementProposal, ...] = ()
    already_covered: tuple[ErrorPattern, ...] = ()
    not_eligible: tuple[tuple[ErrorPattern, tuple[str, ...]], ...] = field(default=())
    unreadable_experiences: int = 0
    """事件负载里读不回来的经验条数。

    它们**不会让整次运行失败**（一条坏记录不该让历史全部作废），
    但必须被计数——否则"只读到 2 条"会被误当成"历史上只有 2 条"。
    """

    def summary(self) -> str:
        """一行式摘要，给 CLI 用。"""
        return (
            f"读取经验 {self.experiences_considered} 条"
            f"（无法解析 {self.unreadable_experiences} 条）；"
            f"发现模式 {len(self.patterns)} 个；"
            f"达到门槛 {len(self.candidates)} 个；"
            f"生成提案 {len(self.created)} 条；"
            f"已有提案覆盖 {len(self.already_covered)} 个；"
            f"未达门槛 {len(self.not_eligible)} 个"
        )


class LearningService:
    """从经验里发现重复模式并生成提案草案。"""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        proposal_service: ProposalService,
        *,
        detector: PatternDetector | None = None,
        policy: PromotionPolicy | None = None,
        generator: ProposalGenerator | None = None,
    ) -> None:
        """初始化。

        Args:
            uow_factory: 工作单元工厂（读经验、读反馈、读既有提案）。
            proposal_service: 提案服务。**必须是同一个实例**——
                另造一个不会出错，但会让"提案从哪来"有两种口径。
            detector: 模式发现器；``None`` 时用默认门槛。
            policy: 门槛裁决器；``None`` 时用默认门槛。
            generator: 提案生成器；``None`` 时用默认模板。
        """
        self._uow_factory = uow_factory
        self._proposals = proposal_service
        self._detector = detector if detector is not None else PatternDetector()
        self._policy = policy if policy is not None else PromotionPolicy()
        self._generator = generator if generator is not None else ProposalGenerator()

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    async def review(
        self,
        *,
        fix_direction: str | None = None,
        actor_id: str = "learning_service",
        correlation_id: UUID | None = None,
    ) -> LearningRun:
        """跑一次完整的学习链路。

        🔴 **同一个模式只会有一条未终结的提案。**

        重复运行（没有任何新经验时）不会造出重复提案——
        提案是**给人评审**的东西，"同一件事递了两遍"是在浪费评审的时间，
        更糟的是会让评审开始怀疑这份清单。

        Args:
            fix_direction: 严重错误的明确修复方向（若有）。
            actor_id: 产生者标识，写进事件。
            correlation_id: 关联链标识；整次运行共用一条。

        Returns:
            本次运行的结果。
        """
        correlation = correlation_id or uuid4()
        experiences, unreadable = await self._load_experiences()
        feedback_counts = await self._negative_feedback_by_signature()

        patterns = self._detector.detect(experiences)
        covered = await self._covered_keys()

        candidates: list[ProposalCandidate] = []
        created: list[ImprovementProposal] = []
        already: list[ErrorPattern] = []
        not_eligible: list[tuple[ErrorPattern, tuple[str, ...]]] = []

        for pattern in patterns:
            decision = self._policy.decide(
                PromotionEvidence(
                    pattern=pattern,
                    fix_direction=fix_direction
                    if pattern.error_type in SEVERE_ERROR_TYPES
                    else None,
                    user_corrections=feedback_counts.get(pattern.situation_signature),
                )
            )
            if not decision.allowed:
                not_eligible.append((pattern, decision.reasons))
                continue

            candidates.append(ProposalCandidate(pattern=pattern, decision=decision))
            if _key_of(pattern) in covered:
                already.append(pattern)
                continue

            proposal = self._generator.generate(
                pattern=pattern, decision=decision, fix_direction=fix_direction
            )
            if proposal is None:
                # 生成器可以拒绝（例如没有支撑证据）——它不是错误，
                # 但也不能悄悄消失，所以记进"未达门槛"的理由里。
                not_eligible.append((pattern, ("生成器判定证据不足以构造提案",)))
                continue
            created.append(
                await self._proposals.create(
                    proposal, actor_id=actor_id, correlation_id=correlation
                )
            )
            covered.add(_key_of(pattern))

        return LearningRun(
            experiences_considered=len(experiences),
            patterns=tuple(patterns),
            candidates=tuple(candidates),
            created=tuple(created),
            already_covered=tuple(already),
            not_eligible=tuple(not_eligible),
            unreadable_experiences=unreadable,
        )

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    async def _load_experiences(self) -> tuple[list[Experience], int]:
        """从事件流里读回全部经验。

        Returns:
            ``(可解析的经验, 无法解析的条数)``。

        ⚠️ **一条坏记录不该让整次运行失败。**

        事件负载是历史数据，而领域对象会演进；用一次异常把整段历史作废，
        代价是"这个系统再也不能从过去学习"。因此这里逐条 try，
        把读不回来的**计数**报出去——报告里看得见，而不是悄悄少几条。
        """
        async with self._uow_factory() as uow:
            events = await uow.events.read_by_event_type(event_type=EventType.EXPERIENCE_CREATED)

        restored: list[Experience] = []
        unreadable = 0
        for event in events:
            experience = _experience_from_event(event)
            if experience is None:
                unreadable += 1
            else:
                restored.append(experience)
        return restored, unreadable

    async def _negative_feedback_by_signature(self) -> dict[str, int]:
        """统计每个情境签名下收到过否定反馈的**不同回合数**。

        🔴 **这是 §11.3 条件四的输入，此前没有任何生产者在算它。**

        没有它，``PromotionEvidence.user_corrections`` 永远是"未观测"，
        而"用户纠正显示系统性问题"这条触发条件因此永远无法成立——
        哪怕用户已经纠正了十次。

        口径说明（为什么是"不同回合数"而不是"反馈条数"）：
        门槛的语义是"这件事发生过几次"，而同一个回合里用户连点三次
        "不对"和三个回合各纠正一次，是完全不同的两件事。
        """
        async with self._uow_factory() as uow:
            feedback_events = await uow.events.read_by_event_type(
                event_type=EventType.USER_FEEDBACK_RECEIVED
            )
        experiences, _ = await self._load_experiences()

        signature_by_round = {
            item.cognitive_round_id: item.situation_signature for item in experiences
        }
        rounds_by_signature: dict[str, set[UUID]] = {}
        for event in feedback_events:
            if not _is_negative_feedback(event):
                continue
            round_id = event.cognitive_round_id
            if round_id is None:
                continue
            signature = signature_by_round.get(round_id)
            if signature is None:
                # 那个回合没有留下经验（失败回合、或禁用了长期记忆），
                # 因此无法把它归到任何情境下。
                continue
            rounds_by_signature.setdefault(signature, set()).add(round_id)

        return {signature: len(rounds) for signature, rounds in rounds_by_signature.items()}

    async def _covered_keys(self) -> set[tuple[str, str]]:
        """已被**未终结**提案覆盖的 ``(错误类别, 情境签名)``。"""
        proposals = await self._proposals.list_all()
        return {
            (proposal.error_class.value, proposal.applicability[0])
            for proposal in proposals
            if not proposal.status.is_terminal and proposal.applicability
        }


def _experience_from_event(event: Event) -> Experience | None:
    """把 ``experience.created`` 的负载还原成领域对象；失败返回 ``None``。"""
    payload = event.payload.get("experience")
    if not isinstance(payload, dict):  # pragma: no cover - 负载恒为字典
        return None
    cleaned = {
        key: value for key, value in payload.items() if key not in _AUDIT_ONLY_EXPERIENCE_KEYS
    }
    try:
        return Experience.model_validate(cleaned)
    except Exception:
        # ⚠️ 这里**刻意吞掉一切异常**：单条坏记录不该让整段历史作废。
        # 代价是"读不回来"必须被计数上报（见 LearningRun.unreadable_experiences），
        # 否则"只读到 2 条"会被误当成"历史上只有 2 条"。
        return None


#: 事件负载里比 ``Experience`` 多出来的审计字段。
_AUDIT_ONLY_EXPERIENCE_KEYS: Final[frozenset[str]] = frozenset(
    {"stop_reason", "attribution_reasons"}
)


def _is_negative_feedback(event: Event) -> bool:
    """该反馈事件是否表达了否定。"""
    raw = event.payload.get("feedback_type")
    if not isinstance(raw, str):  # pragma: no cover - 负载恒有该字段
        return False
    try:
        feedback_type = FeedbackType(raw)
    except ValueError:  # pragma: no cover - 枚举白名单由写入侧保证
        return False
    return feedback_type in NEGATIVE_FEEDBACK_TYPES


def _key_of(pattern: ErrorPattern) -> tuple[str, str]:
    """模式在提案侧的对应键：``(错误类别, 情境签名)``。

    生成器把 ``error_class=pattern.error_type``、
    ``applicability=[pattern.situation_signature]`` 写进提案，
    因此这两个字段合起来就是"这条提案对应哪个模式"。
    """
    return (pattern.error_type.value, pattern.situation_signature)
