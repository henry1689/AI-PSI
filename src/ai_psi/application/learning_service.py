"""学习链路的**显式入口**（任务书 §11.1–§11.4，阶段 6.5 §二.13–15）。

🔴 **这里回答的问题：谁、在什么时候，让系统从经验里学到东西？**

`learning/` 的模块都是**纯函数**——没有定时器、没有钩子、没有副作用。
那是刻意的（ADR-0018 §10：自动生成提案等于用噪声喂评审）。
但纯函数库本身不构成能力：在阶段 6 收尾之前，
`PatternDetector` / `PromotionPolicy` / `ProposalGenerator` 在 `src/` 里
**零调用者**，"三次同类错误可生成 Proposal"这条验收条件
只在测试里成立、在系统里不可操作。

本服务把纯组件串成一条可执行的链路，**并且中途必须过门禁**：

```
事件流（真相）
  → ExperienceReader.load()        读经验 + 读评价，合并成有效评价
  → PatternDetector.detect()       按「独立发生次数 × 评价权重」筛
  → ProposalGate.review()          🔴 从仓储**重新**读取、重新计算门槛
  → ProposalGenerator.generate()   生成 DRAFT 草案（用门禁核实过的模式）
  → ProposalService.create()       落库（必须携带门禁结论）
```

🔴 **为什么这里读完还要门禁再读一遍。**

本服务读到的是"要处理哪些模式"（一个**待办清单**），
门禁读到的才是"这些模式现在够不够格"（一个**授权**）。
两次读取之间经验还可能增加——而评审批的是**此刻**的证据够不够。
把待办清单当成授权，正是阶段 6 那个 bug 的形状。

🔴 **提案仍然不会自动生效，本服务也没有任何让它们生效的能力。**
它只生成 `DRAFT`；评估与批准仍然要人来做（不变量 11）。

⚠️ 本服务**读经验、不写经验**。经验由认知运行时在回合收尾时写入，
评价由反馈路径追加——它们都是不可变的观察，留在事件流里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final
from uuid import UUID, uuid4

from ai_psi.application.experience_reader import ExperienceLoad, ExperienceReader
from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.application.proposal_gate import GateEvidence, ProposalGate
from ai_psi.application.proposal_service import ProposalService
from ai_psi.domain.enums import EventType, FeedbackType
from ai_psi.domain.events import Event
from ai_psi.domain.improvement_proposals import ImprovementProposal
from ai_psi.learning.pattern_detector import ErrorPattern, PatternDetector
from ai_psi.learning.proposal_generator import ProposalGenerator

__all__ = ["LearningRun", "LearningService"]


#: 被视为"用户表达了否定"的反馈类型。
#:
#: 与 :data:`~ai_psi.learning.error_classifier` 里那组保持一致——
#: 两处判定同一件事，分开写会让"什么算纠正"在归因与计数上给出不同答案。
NEGATIVE_FEEDBACK_TYPES: Final[frozenset[FeedbackType]] = frozenset(
    {FeedbackType.CORRECTION, FeedbackType.DISAGREEMENT}
)


@dataclass(frozen=True, slots=True)
class CreatedProposal:
    """一条由本次运行落库的提案，连同**批准它的门禁结论**。

    🔴 把结论一起返回，是因为调用方（CLI、运维端点）需要回答
    "为什么这条提案被放行了"。只返回 ``ImprovementProposal``
    会让那个问题只能靠在日志里翻找。
    """

    proposal: ImprovementProposal
    weighted_count: int
    threshold: int
    data_quality: str


@dataclass(frozen=True, slots=True)
class LearningRun:
    """一次学习链路运行的结果。

    🔴 **每个字段都要能被读出来，包括"什么都没发生"的原因。**

    一份只说"生成了 0 条提案"的报告无法回答"为什么没有"——
    而那正是下一次运行时最需要知道的事。
    """

    experiences_considered: int = 0
    unreadable_experiences: int = 0
    """事件负载里读不回来的经验条数。

    它们**不会让整次运行失败**（一条坏记录不该让历史全部作废），
    但必须被计数——否则"只读到 2 条"会被误当成"历史上只有 2 条"。
    """

    unreadable_evaluations: int = 0
    patterns: tuple[ErrorPattern, ...] = ()
    candidates: tuple[ErrorPattern, ...] = ()
    """达到模式门槛、并**被门禁复核过**的观察。"""

    created: tuple[CreatedProposal, ...] = ()
    already_covered: tuple[ErrorPattern, ...] = ()
    suppressed: tuple[tuple[ErrorPattern | None, tuple[str, ...]], ...] = field(default=())
    """未达门槛或未获授权的观察，连同逐条理由。

    ⚠️ 阶段 6 的版本把"未达门槛"与"生成器拒绝"分两处记，
    而**门禁拒绝**这一支根本不存在（那时还没有门禁）。
    """

    def summary(self) -> str:
        """一行式摘要，给 CLI 用。"""
        return (
            f"读取经验 {self.experiences_considered} 条"
            f"（无法解析 {self.unreadable_experiences} 条，"
            f"评价无法解析 {self.unreadable_evaluations} 条）；"
            f"达到模式门槛 {len(self.patterns)} 个；"
            f"通过门禁 {len(self.candidates)} 个；"
            f"生成提案 {len(self.created)} 条；"
            f"已有提案覆盖 {len(self.already_covered)} 个；"
            f"未通过 {len(self.suppressed)} 个"
        )


class LearningService:
    """从经验里发现重复模式并生成提案草案。"""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        reader: ExperienceReader,
        gate: ProposalGate,
        proposal_service: ProposalService,
        *,
        detector: PatternDetector | None = None,
        generator: ProposalGenerator | None = None,
    ) -> None:
        """初始化。

        Args:
            uow_factory: 工作单元工厂（读反馈事件）。
            reader: 经验读取器。🔴 与门禁**同一个实例**。
            gate: 提案门禁。🔴 与读取器配套。
            proposal_service: 提案服务。**必须是同一个实例**——
                另造一个不会出错，但会让"提案从哪来"有两种口径。
            detector: 模式发现器；``None`` 时用门禁的门槛。
            generator: 提案生成器；``None`` 时用默认模板。
        """
        self._uow_factory = uow_factory
        self._reader = reader
        self._gate = gate
        self._proposals = proposal_service
        # 🔴 检测器默认取**门禁的门槛**。两处门槛不一致时，
        # 本服务会先发现一批模式、再被门禁全部拒掉——症状是
        # "链路每次都说发现了 N 个模式，却一条提案也没有"。
        self._detector = (
            detector
            if detector is not None
            else PatternDetector(threshold=gate.threshold, weighting=gate.weighting)
        )
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
        load = await self._reader.load()
        scan = self._detector.detect(load.assessments)
        covered = await self._covered_keys()
        corrections = await self._negative_feedback_by_signature(load)

        candidates: list[ErrorPattern] = []
        created: list[CreatedProposal] = []
        already: list[ErrorPattern] = []
        rejected: list[tuple[ErrorPattern | None, tuple[str, ...]]] = []

        for pattern in scan.patterns:
            verdict = await self._gate.review(
                error_type=pattern.error_type,
                situation_signature=pattern.situation_signature,
                evidence=GateEvidence(
                    fix_direction=fix_direction,
                    # 🔴 这里给的是**观测到的计数**（含 0），不是 ``None``。
                    #
                    # ``None`` 表示"没有接入计数的调用方"——而这里**有**
                    # 调用方在算它（本方法每次运行都会重算一遍全部反馈事件）。
                    # 用 ``None`` 会让条件四永远显示"未观测"，
                    # 那与"数过了，是 0"是两件不同的事（ADR-0018 §3）。
                    #
                    # ⚠️ 它是一个**下界**：反馈若落在没有产出经验的回合上
                    # （失败回合、关掉长期记忆的回合），就归不到任何情境，
                    # 也就不在计数里。这个口径写在
                    # :meth:`_negative_feedback_by_signature` 的文档里。
                    user_corrections=corrections.get(pattern.situation_signature, 0),
                ),
            )
            if not verdict.authorised:
                rejected.append((pattern, verdict.reasons))
                continue

            candidates.append(pattern)
            if (pattern.error_type.value, pattern.situation_signature) in covered:
                already.append(pattern)
                continue

            proposal = self._generator.generate(
                # 🔴 用**门禁重算并授权过的**那一份，不是本服务自己算的。
                # 本服务自己算的那份在这里根本不存在——这正是 §二.14 要的：
                # 生成提案所用的证据，与门禁核实过的证据是同一批。
                pattern=verdict.verified_pattern,
                decision=verdict.verified_decision,
                fix_direction=fix_direction,
            )
            if proposal is None:
                rejected.append((pattern, ("生成器判定证据不足以构造提案",)))
                continue

            stored = await self._proposals.create(
                proposal, verdict=verdict, actor_id=actor_id, correlation_id=correlation
            )
            created.append(
                CreatedProposal(
                    proposal=stored,
                    weighted_count=verdict.recomputed_weighted_count,
                    threshold=verdict.threshold,
                    data_quality=verdict.data_quality,
                )
            )
            covered.add((pattern.error_type.value, pattern.situation_signature))

        for item in scan.suppressed:
            rejected.append(
                (
                    None,
                    (f"[{item.error_type.value} / {item.situation_signature}] ", *item.reasons),
                )
            )

        return LearningRun(
            experiences_considered=len(load.assessments),
            unreadable_experiences=load.unreadable_experiences,
            unreadable_evaluations=load.unreadable_evaluations,
            patterns=scan.patterns,
            candidates=tuple(candidates),
            created=tuple(created),
            already_covered=tuple(already),
            suppressed=tuple(rejected),
        )

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    async def _negative_feedback_by_signature(self, load: ExperienceLoad) -> dict[str, int]:
        """统计每个情境签名下收到过否定反馈的**不同回合数**。

        🔴 **这是 §11.3 条件四的输入，此前没有任何生产者在算它。**

        没有它，``PromotionEvidence.user_corrections`` 永远是"未观测"，
        而"用户纠正显示系统性问题"这条触发条件因此永远无法成立——
        哪怕用户已经纠正了十次。

        口径说明（为什么是"不同回合数"而不是"反馈条数"）：
        门槛的语义是"这件事发生过几次"，而同一个回合里用户连点三次
        "不对"和三个回合各纠正一次，是完全不同的两件事。

        ⚠️ 阶段 6.5 §三 起，用户纠正还会通过 ``experience.evaluated``
        抬高对应经验的评价——那条路径影响的是**条件一**的加权计数，
        与这里统计的条件四是两回事，两者都要有。
        """
        async with self._uow_factory() as uow:
            feedback_events = await uow.events.read_by_event_type(
                event_type=EventType.USER_FEEDBACK_RECEIVED
            )

        signature_by_round = {
            item.experience.cognitive_round_id: item.experience.situation_signature
            for item in load.assessments
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
