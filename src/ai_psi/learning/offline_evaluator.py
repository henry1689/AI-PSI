"""离线评估（任务书 §11.4）。

🔴 **本模块只算"能从事件流里确定性算出来"的指标，其余一律显式标注为未交付。**

§11.4 列了八项指标，§16.1 列了十五项。它们当中：

* 有一部分**就是事件流里的计数**——完成率、调用数、token 成本、延迟、
  结构化输出解析率、元认知信号的命中率。这些可以确定性地算，
  而且算出来的东西有唯一解释；
* 另一部分需要一个**判定口径**——"事实与假设混淆"要判定什么才叫混淆、
  "冲突保留"要判定什么才叫保留、"用户纠正后的复发"要判定两次纠正
  是不是同一件事。这些口径属于阶段 7 的 Golden Dataset
  （任务书 §16.2），在没有它之前算出来的数字只是**看起来像指标**。

因此 :data:`UNAVAILABLE_METRICS` 是一份**清单**，不是一段注释。
它出现在每一次评估结果里，让读报告的人一眼看到"哪些没算"，
而不是从"报告里没有这一项"去推断——那正是最容易被忽略的一类遗漏。

⚠️ 任务书 §16.1 的"关键硬指标"（``cross_user_memory_leak_rate = 0`` 等）
不在本模块的范围内：它们是**测试要断言的性质**，不是每回合的度量。
断言它们的地方是测试套件，不是评估报告。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final
from uuid import UUID

from ai_psi.domain.enums import CognitiveDepth, RoundState

__all__ = [
    "UNAVAILABLE_METRICS",
    "EvaluationComparison",
    "MetricSnapshot",
    "OfflineEvaluator",
    "RoundMetrics",
    "UnavailableMetric",
]


@dataclass(frozen=True, slots=True)
class RoundMetrics:
    """单个回合的度量输入。

    全部来自事件流与回合投影，**不含任何用户正文**。
    """

    cognitive_round_id: UUID
    state: RoundState
    depth: CognitiveDepth
    model_calls: int = 0
    metacognitive_loops: int = 0
    successful_model_calls: int = 0
    failed_model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_latency_ms: int = 0
    metacognitive_stop: bool = False
    unsupported_certainty_detected: bool = False


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    """一项指标的取值。

    Attributes:
        name: 指标名（与任务书 §16.1 一致）。
        value: 取值。
        unit: 单位，便于报告排版。
        note: 口径说明。**每一项都必须能回答"这个数是怎么算出来的"**——
            没有口径的指标在跨版本比较时会被当成同一个东西，而它可能不是。
    """

    name: str
    value: float
    unit: str = ""
    note: str = ""


@dataclass(frozen=True, slots=True)
class UnavailableMetric:
    """一项**本阶段不交付**的指标。

    Attributes:
        name: 指标名。
        reason: 为什么算不了。必须具体到"缺什么"，
            而不是笼统的"暂不支持"——后者无法判断什么时候能补上。
        owner_stage: 由哪个阶段交付。
    """

    name: str
    reason: str
    owner_stage: str


#: 退化判定里"**越高越好**"的指标。
#:
#: ⚠️ 这三个名字必须与 :meth:`OfflineEvaluator.snapshot` 实际产出的名字逐字一致。
#: 写错一个字母的后果是**静默漏判**：那个指标涨跌都不会被算作退化，
#: 而没有任何地方会报错。
HIGHER_IS_BETTER_METRICS: Final[frozenset[str]] = frozenset(
    {
        "cognitive_round_success_rate",
        "structured_output_parse_rate",
        "metacognitive_stop_rate",
    }
)

#: 退化判定里"**越低越好**"的指标。
#:
#: 置信度失准率与反刍率都是方向明确的负向指标——它们上升就是退化，
#: 没有任何解释空间。阶段 6 的初版漏掉了这两个，结果是**真实退化
#: 被报成"未暴露稳定退化"**。
LOWER_IS_BETTER_METRICS: Final[frozenset[str]] = frozenset(
    {
        "unsupported_certainty_rate",
        "rumination_rate",
    }
)


#: 本阶段**不交付**的指标及其原因。
UNAVAILABLE_METRICS: Final[tuple[UnavailableMetric, ...]] = (
    UnavailableMetric(
        name="fact_hypothesis_confusion_rate",
        reason=(
            "需要判定口径：什么算「把假设当成了事实」"
            "（判断里引用了未被证据支持的假设？回答的语气超过了内部判断？）。"
            "口径未定之前算出的数字无法跨版本比较"
        ),
        owner_stage="阶段 7（Golden Dataset）",
    ),
    UnavailableMetric(
        name="conflict_preservation_rate",
        reason=(
            "需要判定口径：什么算「保留了冲突」。"
            "冲突被呈现、被合并、被忽略三者的边界需要标注样本才能定"
        ),
        owner_stage="阶段 7（Golden Dataset）",
    ),
    UnavailableMetric(
        name="user_correction_recurrence_rate",
        reason=(
            "需要判定两次纠正「是不是同一件事」——这依赖语义判断。"
            "可以算的替代量（纠正次数 / 回合数）语义不同，不能拿来充当它"
        ),
        owner_stage="阶段 7（Golden Dataset）",
    ),
    UnavailableMetric(
        name="memory_write_rejection_rate",
        reason=(
            "记忆事件目前**不挂在回合上**（``cognitive_round_id`` 为 None），"
            "因此无法按回合口径统计。按全局口径算出来的数字与 §16.1 的定义不同"
        ),
        owner_stage="阶段 7（事件关联补齐后）",
    ),
    UnavailableMetric(
        name="stale_memory_retrieval_rate",
        reason="需要判定「一条记忆在什么时刻算过期却仍被检索到」，依赖 valid_until 语义的评测口径",
        owner_stage="阶段 7",
    ),
    UnavailableMetric(
        name="proposal_false_promotion_rate",
        reason="需要跨版本跟踪提案的最终去向，属于长期运行指标，不是单次回放能算的",
        owner_stage="阶段 7 之后（需要生产数据）",
    ),
    UnavailableMetric(
        name="cross_scenario_regression",
        reason=(
            "「其他场景的退化程度」需要**先定义场景**才能算细分。"
            "当前的情境签名（深度|证据量|假设数）是结构特征，"
            "不是语义场景——用它分组算出来的退化率与 §11.4 想要的东西不同名同实"
        ),
        owner_stage="阶段 7（Golden Dataset 定场景边界后）",
    ),
    UnavailableMetric(
        name="average_latency_per_depth",
        reason=(
            "§16.1 要求按 D0–D4 分组统计延迟与调用数，"
            "当前只算了全回合均值。**分组均值能掩盖最贵的深度档**——"
            "D4 的成本问题（risks R38）正是被全均值淹没的"
        ),
        owner_stage="阶段 7",
    ),
    UnavailableMetric(
        name="average_model_calls_per_depth",
        reason="同 average_latency_per_depth：按深度的调用数分布需要分组口径，当前只有全回合均值",
        owner_stage="阶段 7",
    ),
    UnavailableMetric(
        name="cross_user_memory_leak_rate",
        reason=(
            "它是**测试断言的性质**（必须恒为 0），不是每回合的度量。由测试套件断言，不进入评估报告"
        ),
        owner_stage="不适用（由测试保证）",
    ),
)


@dataclass(frozen=True, slots=True)
class EvaluationComparison:
    """Baseline 与 Candidate 的对照结果。

    Attributes:
        baseline: Baseline 的指标快照。
        candidate: Candidate 的指标快照；未提供时为空。
        deltas: 逐项差值（candidate - baseline）；未提供 candidate 时为空。
        comparison_available: 是否真的做了对照。
            🔴 ``False`` 时**结果里带的是 Baseline 单侧数据**，
            它不能被当作"改动没有退化"的证据——没有对照就没有结论。
        reasons: 可读说明。
        unavailable: 本阶段不交付的指标清单。
    """

    baseline: tuple[MetricSnapshot, ...] = field(default=())
    candidate: tuple[MetricSnapshot, ...] = field(default=())
    deltas: tuple[tuple[str, float], ...] = field(default=())
    comparison_available: bool = False
    reasons: tuple[str, ...] = field(default=())
    unavailable: tuple[UnavailableMetric, ...] = UNAVAILABLE_METRICS
    round_count: int = 0
    candidate_round_count: int = 0

    def delta_for(self, name: str) -> float | None:
        """返回某项指标的差值；未做对照时返回 ``None``。"""
        for key, value in self.deltas:
            if key == name:
                return value
        return None


class OfflineEvaluator:
    """在历史回合上计算指标并做对照。"""

    #: 判定"反刍"的元认知循环次数下限。
    #:
    #: 一次循环是**正常**的（元认知复核是流水线的固定一环）；
    #: 两次及以上意味着"复核之后又回去重新分析"，那才是绕圈。
    #: 把它写成常量而不是配置：阈值一旦可调，跨版本比较就失去意义。
    RUMINATION_LOOP_THRESHOLD: Final[int] = 2

    def snapshot(self, rounds: Sequence[RoundMetrics]) -> tuple[MetricSnapshot, ...]:
        """计算一轮历史回合的指标快照。

        Args:
            rounds: 各回合的度量。

        Returns:
            指标快照。**回合为空时返回空元组**——
            除零不是一个可以糊弄过去的边界：给出 0.0 会让人以为
            "这个指标算出来是零"，而真相是"没有数据"。
        """
        if not rounds:
            return ()

        total = len(rounds)
        completed = sum(1 for item in rounds if item.state is RoundState.COMPLETED)
        invocations = sum(item.successful_model_calls + item.failed_model_calls for item in rounds)
        successful = sum(item.successful_model_calls for item in rounds)
        tokens = sum(item.input_tokens + item.output_tokens for item in rounds)
        latency = sum(item.total_latency_ms for item in rounds)

        return (
            MetricSnapshot(
                name="cognitive_round_success_rate",
                value=completed / total,
                note=f"completed / 全部回合（{completed}/{total}）",
            ),
            MetricSnapshot(
                name="structured_output_parse_rate",
                value=(successful / invocations) if invocations else 0.0,
                note=(
                    f"成功的模型调用 / 全部调用（{successful}/{invocations}）"
                    if invocations
                    else "本轮没有模型调用"
                ),
            ),
            MetricSnapshot(
                name="unsupported_certainty_rate",
                value=sum(1 for item in rounds if item.unsupported_certainty_detected) / total,
                note=(
                    "元认知标记「无依据的确定性」的回合占比。⚠️ 这是**元认知自己报的**，不是独立判定"
                ),
            ),
            MetricSnapshot(
                name="rumination_rate",
                value=(
                    sum(
                        1
                        for item in rounds
                        if item.metacognitive_loops >= self.RUMINATION_LOOP_THRESHOLD
                    )
                    / total
                ),
                note=(
                    f"元认知循环 ≥{self.RUMINATION_LOOP_THRESHOLD} 次的回合占比"
                    "（1 次是流水线的正常一环，不算绕圈）"
                ),
            ),
            MetricSnapshot(
                name="metacognitive_stop_rate",
                value=sum(1 for item in rounds if item.metacognitive_stop) / total,
                note="由元认知（而非预算或失败）决定停止的回合占比",
            ),
            MetricSnapshot(
                name="average_model_calls_per_round",
                value=sum(item.model_calls for item in rounds) / total,
                unit="calls",
                note="模型调用次数 / 回合数",
            ),
            MetricSnapshot(
                name="average_token_cost_per_round",
                value=tokens / total,
                unit="tokens",
                note="（输入 + 输出）token / 回合数",
            ),
            MetricSnapshot(
                name="average_latency_ms",
                value=latency / total,
                unit="ms",
                note="模型调用总耗时 / 回合数（不含非模型环节）",
            ),
        )

    def compare(
        self,
        *,
        baseline_rounds: Sequence[RoundMetrics],
        candidate_rounds: Sequence[RoundMetrics] | None = None,
    ) -> EvaluationComparison:
        """在历史数据上对照 Baseline 与 Candidate（任务书 §11.4）。

        Args:
            baseline_rounds: 历史回合（当前策略下的真实运行记录）。
            candidate_rounds: 候选策略下的运行记录。
                ``None`` 表示**没有候选数据**——V0.1 不自动产生它，
                因为那需要阶段 7 的 Eval Runner。

        Returns:
            对照结果。``candidate_rounds`` 未提供时
            ``comparison_available`` 为 ``False``，只带 Baseline 单侧数据。
        """
        baseline = self.snapshot(baseline_rounds)
        if not baseline:
            return EvaluationComparison(
                comparison_available=False,
                reasons=("没有历史回合可供评估——Baseline 为空，无从对照",),
            )

        # 🔴 **候选数据为空是一个合法输入，不是调用错误。**
        #
        # 候选策略跑到一半崩了、一条回合都没产出，是完全可能发生的事；
        # 让 `zip(..., strict=True)` 抛一个裸 ValueError，等于把一个
        # "这次评估没有候选数据"的事实报成一次崩溃——调用方拿到的是
        # 一句无从解释的 `zip() argument 2 is shorter`。
        if candidate_rounds is not None and not candidate_rounds:
            return EvaluationComparison(
                baseline=baseline,
                comparison_available=False,
                reasons=(
                    "候选策略**没有产出任何回合**，因此没有可对照的数据",
                    "⚠️ 这不等于「没有退化」——它是一次失败的候选运行，而不是一次成功的对照",
                ),
                round_count=len(baseline_rounds),
            )

        if candidate_rounds is None:
            return EvaluationComparison(
                baseline=baseline,
                comparison_available=False,
                reasons=(
                    "只算出了 Baseline，**没有候选策略的运行数据**，因此没有对照",
                    "候选数据来自候选策略的一次离线运行——"
                    "V0.1 不自动产生它（那需要阶段 7 的 Eval Runner），"
                    "必须由调用方提供",
                    "⚠️ 单侧数据**不能**作为「改动没有退化」的证据",
                ),
                round_count=len(baseline_rounds),
            )

        candidate = self.snapshot(candidate_rounds)
        deltas = tuple(
            (base.name, cand.value - base.value)
            for base, cand in zip(baseline, candidate, strict=True)
        )
        return EvaluationComparison(
            baseline=baseline,
            candidate=candidate,
            deltas=deltas,
            comparison_available=True,
            reasons=(
                f"Baseline {len(baseline_rounds)} 个回合 vs "
                f"Candidate {len(candidate_rounds)} 个回合",
            ),
            round_count=len(baseline_rounds),
            candidate_round_count=len(candidate_rounds),
        )

    def regressed(self, comparison: EvaluationComparison) -> bool:
        """对照结果是否显示**稳定退化**（§11.3 的第三条触发条件）。

        🔴 **只在真的做了对照时才回答。**

        单侧数据无法回答"有没有退化"——返回 ``False`` 会让调用方
        把"没评估"当成"没退化"，而那正是 :class:`PromotionEvidence`
        用 ``None`` 而不是 ``False`` 表示未评估的原因。

        🔴 **只判方向明确的指标，两个方向都算。**

        阶段 6 的初版只把三个"越高越好"的指标算作退化，
        于是 ``unsupported_certainty_rate`` 与 ``rumination_rate``
        从 0 涨到 1.0 —— 一次**货真价实的退化** —— 会被报成"未暴露退化"。
        那正好是这条规则要防的反方向：把真实退化说成没问题。

        方向有争议的指标（调用成本、token、延迟）**仍然不在这里判**：
        它们上升可能是"用了更多算力换更好的结论"，不是单方向的退化。

        Args:
            comparison: 对照结果。

        Returns:
            是否存在退化。

        Raises:
            ValueError: 没有做对照（``comparison_available`` 为 ``False``）。
        """
        if not comparison.comparison_available:
            msg = (
                "没有对照数据，无法判断是否退化。"
                "把「未评估」当成「未退化」会让系统长期忽略真正有价值的改进信号"
            )
            raise ValueError(msg)
        return any(
            delta > 0 if name in LOWER_IS_BETTER_METRICS else delta < 0
            for name, delta in comparison.deltas
            if name in HIGHER_IS_BETTER_METRICS or name in LOWER_IS_BETTER_METRICS
        )
