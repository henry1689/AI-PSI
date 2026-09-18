"""从回合与事件流读出评测用的度量（阶段 6.5 §四.1）。

🔴 **本模块把 `OfflineEvaluator` 从死代码变成可达能力。**

阶段 6.5 的能力证据矩阵发现：`OfflineEvaluator` 在 `src/` 里
**零调用者**，而它的输入类型 :class:`~ai_psi.learning.offline_evaluator.RoundMetrics`
在全仓库**没有任何生产者**——只有测试在手工构造它。

这意味着"离线评测暴露稳定退化"（任务书 §11.3 条件三）这条触发条件
在系统里永远无法成立，也意味着阶段 6.5 §七.15–16 要验收的两条语义
（lower-is-better 指标退化要判出来 / 未评估不得自动判定为未退化）
**没有任何路径能触达**。

本模块补上那个缺口：把真实回合与它们的事件流翻译成 ``RoundMetrics``。

🔴 **只读。** 不写任何东西，也不产生任何事件。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final
from uuid import UUID

from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.cognition.projection import project_artifacts
from ai_psi.domain.cognitive_rounds import CognitiveRound
from ai_psi.domain.enums import EventType, RoundState
from ai_psi.domain.events import Event
from ai_psi.learning.offline_evaluator import RoundMetrics

__all__ = ["METACOGNITIVE_STOP_REASONS", "RoundMetricsReader"]


#: 判定"停止是**元认知**决定的"所依据的停止原因。
#:
#: 🔴 这是一份**白名单**。没列进来的原因一律不算——
#: 指标 ``metacognitive_stop_rate`` 说的是"这个回合的终止
#: 是认知层自己判断出来的，还是被外部条件切断的"，
#: 两者对系统成熟度的含义完全不同。
#:
#: * ``DIRECT_ANSWER``——D0 直答，**根本没进元认知**；
#: * ``BUDGET_CONSTRAINT``——被预算切断，是**外部约束**；
#: * ``NO_CONCERN_DETECTED``——连关切都没有，谈不上"决定停止"；
#: * ``AWAITING_EVIDENCE`` / ``ESCALATED_TO_RESEARCH``——不是终态。
#:
#: 白名单的代价是：新增一个停止原因时，它默认**不计入**
#: ``metacognitive_stop_rate``。这是有意的——一个没想到过的新原因
#: 应该先被讨论它属于哪一类，而不是先默默提高这个指标。
METACOGNITIVE_STOP_REASONS: Final[frozenset[str]] = frozenset(
    {
        "INQUIRY_STOP_CONDITION_SATISFIED",
        "NO_MARGINAL_COGNITIVE_GAIN",
        "METACOGNITIVE_LOOP_LIMIT",
        "SCOPE_DRIFT_DETECTED",
        "LOWERED_CONFIDENCE",
        "MODEL_DECISION_STOP",
        "RULE_LAYER_STOP",
    }
)


class RoundMetricsReader:
    """把真实回合翻译成评测度量。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        """初始化。

        Args:
            uow_factory: 工作单元工厂。
        """
        self._uow_factory = uow_factory

    async def load(self, round_ids: Sequence[UUID] | None = None) -> tuple[RoundMetrics, ...]:
        """读出一批回合的度量。

        Args:
            round_ids: 要读的回合；``None`` 表示**全部**。

        Returns:
            度量元组，按回合 id 排序（确定的顺序——否则同一个输入
            两次运行会得到不同的对照结果）。
        """
        async with self._uow_factory() as uow:
            rounds = await uow.rounds.list_all()
            wanted = set(round_ids) if round_ids is not None else None
            selected = [item for item in rounds if wanted is None or item.id in wanted]
            selected.sort(key=lambda item: str(item.id))
            events_by_round = {
                item.id: await uow.events.read_stream(cognitive_round_id=item.id)
                for item in selected
            }

        return tuple(_metrics_of(round_, events_by_round[round_.id]) for round_ in selected)


def _metrics_of(round_: CognitiveRound, events: Sequence[Event]) -> RoundMetrics:
    """把一个回合与它的事件流折算成度量。

    ⚠️ 所有数字都来自**已经落库的东西**（回合行 + 事件负载），
    没有一个来自"当前的运行状态"。评测要回答的是"历史上发生了什么"，
    而不是"现在内存里是什么"。
    """
    view = project_artifacts(list(events))
    invocations = view.model_invocations

    successful = sum(1 for item in invocations if item.completed_at is not None)
    failed = len(invocations) - successful
    latency = sum(item.latency_ms or 0 for item in invocations)
    input_tokens = sum(item.input_token_count or 0 for item in invocations)
    output_tokens = sum(item.output_token_count or 0 for item in invocations)

    return RoundMetrics(
        cognitive_round_id=round_.id,
        state=round_.state,
        depth=round_.depth_level,
        model_calls=round_.model_calls_used,
        metacognitive_loops=round_.metacognitive_loops,
        successful_model_calls=successful,
        failed_model_calls=failed,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_latency_ms=latency,
        metacognitive_stop=(
            round_.state is RoundState.COMPLETED
            and round_.stop_reason in METACOGNITIVE_STOP_REASONS
        ),
        unsupported_certainty_detected=_unsupported_certainty(events),
    )


def _unsupported_certainty(events: Sequence[Event]) -> bool:
    """本回合的反思是否报过"无依据的确定性"。

    ⚠️ 它读的是**元认知自己报的**那一栏（``metacognition.completed``
    的 ``reflection``），不是独立判定。指标说明里已经写明这一点——
    把它当成客观事实会让这个指标从一个**信号**变成一个**自我评价**。

    取**最后一次**元认知复核的结论：一个回合可能复核多轮，
    而反思是"复核之后对当下的判断"，最后一轮才代表最终的自我评价。
    """
    for event in reversed(events):
        if event.event_type is not EventType.METACOGNITION_COMPLETED:
            continue
        payload = event.payload.get("reflection")
        if isinstance(payload, dict):
            return bool(payload.get("unsupported_certainty_detected", False))
    return False
