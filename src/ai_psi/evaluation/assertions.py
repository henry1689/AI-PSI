"""Golden Case 的断言注册表（**唯一真相来源**）。

阶段 7 · S1a。这里定义"一条案例可以期望什么"，以及每条期望**怎么被判定**。

🔴 **本模块是断言的唯一真相来源。没有第二份。**

之所以强调这一点：如果另写一份"断言说明文档"，两份就会漂移，
而漂移的那一份会被当成事实（ADR-0021 §2 的教训）。
需要人读的说明请看每条 :class:`AssertionSpec` 的 ``observed_from`` 与
``detail`` 字段——它们就是文档，且**无法与实现不一致**。

## 三条硬约束

1. **每条断言的 ``observed`` 必须来自真实结构化字段。**
   ``observed_from`` 逐条写明它来自哪里（``RoundOutcome`` 的哪个属性，
   或事件流投影的哪个字段）。写不出来的断言不许进注册表——
   "看起来像指标却算不出来"正是 S1a 要防的东西。

2. **不可观测 ≠ 通过。**
   例如"回合没有产出判断"时，``confidence_band_at_most`` 的观测值
   是**不可观测**，判定结果为**不通过**（``observed=None``）。
   自动通过会让"字段根本读不到"伪装成"行为正确"。

3. **``forbidden`` 的语义是 ``not 满足``，不是"另一条规则"。**
   required：``observed 满足 expected`` → 通过；
   forbidden：``observed **不**满足 expected`` → 通过。
   同一套 ``satisfies`` 判定，取反而已。两份规则会漂移，取反不会。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ai_psi.domain.enums import CognitiveDepth, ConfidenceBand, EpistemicAction, RoundState

__all__ = [
    "ANALYSIS_KINDS",
    "ASSERTIONS",
    "AssertionResult",
    "AssertionSpec",
    "CaseObservation",
    "ExpectedKind",
    "JudgeOutcome",
    "assertion_names",
    "evaluate",
    "spec_for",
]


class ExpectedKind(StrEnum):
    """``expected`` 允许的取值类型。

    用**枚举**而不是 ``type`` 对象：它要出现在错误消息里（"期望是整数，
    实际是字符串"），也要参与加载期校验，字符串形式比 ``<class 'int'>`` 可读。
    """

    BOOLEAN = "boolean"
    INTEGER = "integer"
    STRING = "string"


#: ``cognition.analysis.completed`` 事件里 ``analysis_kind`` 的全部取值。
#:
#: 来源：``application/cognitive_runtime.py`` 的 ``_record_analysis`` 调用点
#: （logical / causal / concept / dialectical / philosophical 五处）。
#: 由 ``tests/unit/evaluation/test_assertions.py`` 用一次真实 D4 回合核对：
#: 观测到的 kind 必须全部落在这个集合里。
ANALYSIS_KINDS: Final[tuple[str, ...]] = (
    "causal",
    "concept",
    "dialectical",
    "logical",
    "philosophical",
)

_DEPTHS: Final[tuple[str, ...]] = tuple(depth.value for depth in CognitiveDepth)
_BANDS: Final[tuple[str, ...]] = tuple(band.value for band in ConfidenceBand)
_ACTIONS: Final[tuple[str, ...]] = tuple(action.value for action in EpistemicAction)
_STATES: Final[tuple[str, ...]] = tuple(state.value for state in RoundState)


class CaseObservation(BaseModel):
    """一个案例执行后**实际观测到**的全部结构化字段。

    🔴 这里只放**有真实来源**的字段，每个字段旁边写清来源。
    没有来源的字段不进这个模型——那会让"观测不到"和"观测到空值"混同。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: 来源：``RoundOutcome.state``。
    state: str
    #: 来源：``RoundOutcome.depth``（路由结果，可能被显式 requested_depth 覆盖）。
    depth: str
    #: 来源：``RoundOutcome.stop_reason`` 是否为 None。
    stop_reason_present: bool
    #: 来源：``RoundOutcome.response_text`` 是否为 None。
    response_present: bool
    #: 来源：``RoundOutcome.response_text``（原文）。
    #:
    #: ⚠️ 只用于字符串存在性检查，**不进 canonical JSON**——它是措辞，
    #: 不是语义；把它写进确定性产物会让"改一句模板话术"变成"评测结果变了"。
    response_text: str | None = None
    #: 来源：``RoundOutcome.judgment`` 是否为 None。
    judgment_present: bool
    #: 来源：``RoundOutcome.model_calls_used``。
    model_calls_used: int
    #: 来源：``RoundOutcome.metacognitive_loops``。
    metacognitive_loops: int
    #: 来源：``judgment.confidence_band``；无判断时为 None（**不可观测**）。
    confidence_band: str | None = None
    #: 来源：``judgment.recommended_epistemic_action``；无判断时为 None。
    epistemic_action: str | None = None
    #: 来源：``len(judgment.unresolved_unknowns)``；无判断时为 None。
    unresolved_unknown_count: int | None = None
    #: 来源：``len(judgment.strongest_counterarguments)``；无判断时为 None。
    counterargument_count: int | None = None
    #: 来源：事件流里 ``hypothesis.created`` 事件的条数。
    hypothesis_count: int = 0
    #: 来源：事件流里 ``cognition.analysis.completed`` 的 ``analysis_kind`` 集合（已排序）。
    analysis_kinds: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class JudgeOutcome:
    """一次判定的中间结果。"""

    #: 真实观测到的值。``None`` 表示**不可观测**（不是"观测到空"）。
    observed: str | int | bool | list[str] | None
    #: ``None`` 表示不可观测——调用方必须把它判为**不通过**。
    satisfied: bool | None
    detail: str


@dataclass(frozen=True, slots=True)
class AssertionSpec:
    """一条断言的完整定义。"""

    name: str
    #: 允许使用它的 ``case_type``。S1a 只有 ``cognitive_behavior``。
    applies_to: frozenset[str]
    expected_kind: ExpectedKind
    #: ``expected`` 的取值闭集；``None`` 表示该断言接受任意同类型值（如阈值数字）。
    allowed_values: tuple[str, ...] | None
    #: 🔴 观测值来自哪个真实字段。**必须能回答这个问题**。
    observed_from: str
    #: 判定逻辑的人话说明。
    detail: str
    allows_required: bool = True
    allows_forbidden: bool = True
    #: 🔴 观测值是不是**单值**的。
    #:
    #: 单值断言（如 ``final_state``、``depth_level``）在一个案例里出现两次
    #: 就是自相矛盾——期望它同时是 d0 与 d1 是不可能的。
    #: 多值断言（``analysis_module_ran`` 这类"集合里有没有"）则可以出现多次：
    #: "逻辑**和**因果都要跑"是完全正常的期望。
    single_valued: bool = True
    #: 不可观测时怎么办。S1a 全体统一为"判为不通过"，逐条写明。
    unobservable_behavior: str = "观测值取不到时判为**不通过**（绝不自动通过）"


class _Judge(Protocol):
    """判定函数的形状。

    用 Protocol 而不是 ``Callable[..., JudgeOutcome]``：后者在 strict 下
    只能写成 ``object``，于是每次调用都要 ``# type: ignore``——
    那样等于在注册表这个最关键的位置关掉类型检查。
    """

    def __call__(self, observed: CaseObservation, expected: object) -> JudgeOutcome: ...


def _judge_final_state(observed: CaseObservation, expected: object) -> JudgeOutcome:
    value = observed.state
    return JudgeOutcome(
        observed=value,
        satisfied=value == expected,
        detail=f"回合终态 {value!r}，期望 {expected!r}",
    )


def _judge_depth_level(observed: CaseObservation, expected: object) -> JudgeOutcome:
    value = observed.depth
    return JudgeOutcome(
        observed=value,
        satisfied=value == expected,
        detail=f"实际路由到 {value}，期望恰好是 {expected}",
    )


def _judge_depth_at_most(observed: CaseObservation, expected: object) -> JudgeOutcome:
    actual = CognitiveDepth(observed.depth)
    ceiling = CognitiveDepth(str(expected))
    return JudgeOutcome(
        observed=observed.depth,
        satisfied=actual.level <= ceiling.level,
        detail=(
            f"实际 {actual.value}（第 {actual.level} 档），"
            f"上界 {ceiling.value}（第 {ceiling.level} 档）"
        ),
    )


def _judge_depth_at_least(observed: CaseObservation, expected: object) -> JudgeOutcome:
    actual = CognitiveDepth(observed.depth)
    floor = CognitiveDepth(str(expected))
    return JudgeOutcome(
        observed=observed.depth,
        satisfied=actual.level >= floor.level,
        detail=(
            f"实际 {actual.value}（第 {actual.level} 档），"
            f"下界 {floor.value}（第 {floor.level} 档）"
        ),
    )


def _judge_model_calls_at_most(observed: CaseObservation, expected: object) -> JudgeOutcome:
    value = observed.model_calls_used
    limit = int(str(expected))
    return JudgeOutcome(
        observed=value,
        satisfied=value <= limit,
        detail=f"实际模型调用 {value} 次，上界 {limit}",
    )


def _judge_model_calls_at_least(observed: CaseObservation, expected: object) -> JudgeOutcome:
    value = observed.model_calls_used
    floor = int(str(expected))
    return JudgeOutcome(
        observed=value,
        satisfied=value >= floor,
        detail=f"实际模型调用 {value} 次，下界 {floor}",
    )


def _judge_loops_at_most(observed: CaseObservation, expected: object) -> JudgeOutcome:
    value = observed.metacognitive_loops
    limit = int(str(expected))
    return JudgeOutcome(
        observed=value,
        satisfied=value <= limit,
        detail=f"实际元认知循环 {value} 轮，上界 {limit}",
    )


def _judge_loops_at_least(observed: CaseObservation, expected: object) -> JudgeOutcome:
    value = observed.metacognitive_loops
    floor = int(str(expected))
    return JudgeOutcome(
        observed=value,
        satisfied=value >= floor,
        detail=f"实际元认知循环 {value} 轮，下界 {floor}",
    )


def _judge_stop_reason_present(observed: CaseObservation, expected: object) -> JudgeOutcome:
    value = observed.stop_reason_present
    return JudgeOutcome(
        observed=value,
        satisfied=value is expected,
        detail=f"停止原因为空={not value}，期望为空={not bool(expected)}",
    )


def _judge_response_present(observed: CaseObservation, expected: object) -> JudgeOutcome:
    value = observed.response_present
    return JudgeOutcome(
        observed=value,
        satisfied=value is expected,
        detail=f"回答存在={value}，期望={bool(expected)}",
    )


def _judge_judgment_present(observed: CaseObservation, expected: object) -> JudgeOutcome:
    value = observed.judgment_present
    return JudgeOutcome(
        observed=value,
        satisfied=value is expected,
        detail=f"判断存在={value}，期望={bool(expected)}",
    )


def _judge_hypothesis_at_most(observed: CaseObservation, expected: object) -> JudgeOutcome:
    value = observed.hypothesis_count
    limit = int(str(expected))
    return JudgeOutcome(
        observed=value,
        satisfied=value <= limit,
        detail=f"实际产生 {value} 条假设，上界 {limit}",
    )


def _judge_hypothesis_at_least(observed: CaseObservation, expected: object) -> JudgeOutcome:
    value = observed.hypothesis_count
    floor = int(str(expected))
    return JudgeOutcome(
        observed=value,
        satisfied=value >= floor,
        detail=f"实际产生 {value} 条假设，下界 {floor}",
    )


def _judge_analysis_module_ran(observed: CaseObservation, expected: object) -> JudgeOutcome:
    kinds = list(observed.analysis_kinds)
    wanted = str(expected)
    return JudgeOutcome(
        observed=kinds,
        satisfied=wanted in kinds,
        detail=f"本回合执行的分析模块 {kinds}，期望包含 {wanted!r}",
    )


def _judge_response_contains(observed: CaseObservation, expected: object) -> JudgeOutcome:
    """字符串**存在性**检查。

    🔴 它**不是**"事实正确性判定"。它只回答"这段文字在不在回答里"。
    因此：

    * 只允许检查**必要标记**（例如"必须说明不确定性"这个结构性标记）；
    * 在 ``live`` 模式下措辞不稳定，**不适用**（S1a 只有 deterministic 模式）；
    * 不可观测（没有回答）时判为不通过。
    """
    text = observed.response_text
    marker = str(expected)
    if text is None:
        return JudgeOutcome(
            observed=None,
            satisfied=None,
            detail="回合没有产出回答文本，无法做字符串存在性检查（不可观测）",
        )
    return JudgeOutcome(
        observed=marker in text,
        satisfied=marker in text,
        detail=(
            f"回答{'包含' if marker in text else '不包含'}标记 {marker!r}"
            "（字符串存在性检查，不是语义判定）"
        ),
    )


def _optional_count_judge(value: int | None, expected: object, label: str) -> JudgeOutcome:
    if value is None:
        return JudgeOutcome(
            observed=None,
            satisfied=None,
            detail=f"回合没有产出判断，{label} 不可观测",
        )
    return JudgeOutcome(
        observed=value > 0,
        satisfied=(value > 0) is expected,
        detail=f"{label} 实际 {value} 条，存在={value > 0}",
    )


def _judge_unknowns_present(observed: CaseObservation, expected: object) -> JudgeOutcome:
    return _optional_count_judge(observed.unresolved_unknown_count, expected, "未解决未知")


def _judge_counterargument_present(observed: CaseObservation, expected: object) -> JudgeOutcome:
    return _optional_count_judge(observed.counterargument_count, expected, "最强反证")


def _judge_confidence_band_at_most(observed: CaseObservation, expected: object) -> JudgeOutcome:
    raw = observed.confidence_band
    if raw is None:
        return JudgeOutcome(
            observed=None,
            satisfied=None,
            detail="回合没有产出判断，置信档位不可观测",
        )
    actual = ConfidenceBand(raw)
    ceiling = ConfidenceBand(str(expected))
    return JudgeOutcome(
        observed=actual.value,
        satisfied=actual.rank <= ceiling.rank,
        detail=(
            f"实际置信档 {actual.value}（第 {actual.rank} 档），"
            f"上界 {ceiling.value}（第 {ceiling.rank} 档）"
        ),
    )


def _judge_epistemic_action(observed: CaseObservation, expected: object) -> JudgeOutcome:
    value = observed.epistemic_action
    if value is None:
        return JudgeOutcome(
            observed=None,
            satisfied=None,
            detail="回合没有产出判断，认知动作不可观测",
        )
    return JudgeOutcome(
        observed=value,
        satisfied=value == expected,
        detail=f"实际认知动作 {value!r}，期望 {expected!r}",
    )


_COGNITIVE_BEHAVIOR: Final[frozenset[str]] = frozenset({"cognitive_behavior"})


def _spec(
    name: str,
    expected_kind: ExpectedKind,
    observed_from: str,
    detail: str,
    *,
    allowed_values: tuple[str, ...] | None = None,
    allows_forbidden: bool = True,
    single_valued: bool = True,
) -> AssertionSpec:
    return AssertionSpec(
        name=name,
        applies_to=_COGNITIVE_BEHAVIOR,
        expected_kind=expected_kind,
        allowed_values=allowed_values,
        observed_from=observed_from,
        detail=detail,
        allows_forbidden=allows_forbidden,
        single_valued=single_valued,
    )


#: 🔴 **注册表本体。** 名字 → 定义。没有第二个地方定义断言。
ASSERTIONS: Final[dict[str, AssertionSpec]] = {
    "final_state": _spec(
        "final_state",
        ExpectedKind.STRING,
        "RoundOutcome.state",
        "回合的最终状态必须恰好等于期望状态。",
        allowed_values=_STATES,
    ),
    "depth_level": _spec(
        "depth_level",
        ExpectedKind.STRING,
        "RoundOutcome.depth",
        "实际路由到的深度必须恰好等于期望深度（用于钉住路由规则）。",
        allowed_values=_DEPTHS,
    ),
    "depth_at_most": _spec(
        "depth_at_most",
        ExpectedKind.STRING,
        "RoundOutcome.depth",
        "实际深度不得**超过**期望档位（用于「不得过度分析」）。",
        allowed_values=_DEPTHS,
        allows_forbidden=False,
    ),
    "depth_at_least": _spec(
        "depth_at_least",
        ExpectedKind.STRING,
        "RoundOutcome.depth",
        "实际深度不得**低于**期望档位（用于「这类问题必须展开到某深度」）。",
        allowed_values=_DEPTHS,
        allows_forbidden=False,
    ),
    "model_calls_at_most": _spec(
        "model_calls_at_most",
        ExpectedKind.INTEGER,
        "RoundOutcome.model_calls_used",
        "本回合的模型调用次数不得超过期望值（用于钉住预算表与「不超预算」）。",
        allows_forbidden=False,
    ),
    "model_calls_at_least": _spec(
        "model_calls_at_least",
        ExpectedKind.INTEGER,
        "RoundOutcome.model_calls_used",
        "本回合的模型调用次数不得少于期望值（用于钉住「显式深度确实被展开」）。",
        allows_forbidden=False,
    ),
    "metacognitive_loops_at_least": _spec(
        "metacognitive_loops_at_least",
        ExpectedKind.INTEGER,
        "RoundOutcome.metacognitive_loops",
        "元认知循环轮数不得少于期望值。",
        allows_forbidden=False,
    ),
    "metacognitive_loops_at_most": _spec(
        "metacognitive_loops_at_most",
        ExpectedKind.INTEGER,
        "RoundOutcome.metacognitive_loops",
        "元认知循环轮数不得超过期望值"
        "（D0 的循环上限是 0，见 domain/cognitive_rounds.py 的 _BUDGET_BY_DEPTH）。",
        allows_forbidden=False,
    ),
    "stop_reason_present": _spec(
        "stop_reason_present",
        ExpectedKind.BOOLEAN,
        "RoundOutcome.stop_reason 是否为 None",
        "回合必须（或必须不）记录停止原因（宪法要求「完成回合都有停止原因」）。",
    ),
    "response_present": _spec(
        "response_present",
        ExpectedKind.BOOLEAN,
        "RoundOutcome.response_text 是否为 None",
        "回合必须（或必须不）产出回答文本。",
    ),
    "judgment_present": _spec(
        "judgment_present",
        ExpectedKind.BOOLEAN,
        "RoundOutcome.judgment 是否为 None",
        "回合必须（或必须不）产出判断对象。",
    ),
    "hypothesis_count_at_least": _spec(
        "hypothesis_count_at_least",
        ExpectedKind.INTEGER,
        "事件流里 hypothesis.created 事件的条数",
        "本回合产生的假设条数不得少于期望值。",
        allows_forbidden=False,
    ),
    "hypothesis_count_at_most": _spec(
        "hypothesis_count_at_most",
        ExpectedKind.INTEGER,
        "事件流里 hypothesis.created 事件的条数",
        "本回合产生的假设条数不得超过期望值"
        "（D0 的模块矩阵里没有假设步骤，见 cognition/orchestrator.py）。",
        allows_forbidden=False,
    ),
    "analysis_module_ran": _spec(
        "analysis_module_ran",
        ExpectedKind.STRING,
        "事件流里 cognition.analysis.completed 的 analysis_kind",
        "本回合必须执行了指定的分析模块。要表达「必须不执行」请用 forbidden。",
        allowed_values=ANALYSIS_KINDS,
        # 多值：一个回合可以（也常常应当）同时要求多个模块执行。
        single_valued=False,
    ),
    "unresolved_unknowns_present": _spec(
        "unresolved_unknowns_present",
        ExpectedKind.BOOLEAN,
        "len(judgment.unresolved_unknowns) > 0",
        "判断里必须（或必须不）记录未解决的未知。",
    ),
    "counterargument_present": _spec(
        "counterargument_present",
        ExpectedKind.BOOLEAN,
        "len(judgment.strongest_counterarguments) > 0",
        "判断里必须（或必须不）记录最强反证。",
    ),
    "confidence_band_at_most": _spec(
        "confidence_band_at_most",
        ExpectedKind.STRING,
        "judgment.confidence_band 的序数位置",
        "结论强度不得超过期望档位（不变量 3 / 7 的结构化检查）。",
        allowed_values=_BANDS,
        allows_forbidden=False,
    ),
    "epistemic_action": _spec(
        "epistemic_action",
        ExpectedKind.STRING,
        "judgment.recommended_epistemic_action",
        "推荐认知动作必须恰好等于期望值。",
        allowed_values=_ACTIONS,
    ),
    "response_contains": _spec(
        "response_contains",
        ExpectedKind.STRING,
        "response_text 的**字符串存在性**（不是语义判定）",
        "回答里必须出现指定标记。仅在 deterministic 模式下用于**必要标记**；"
        "live 模式措辞不稳定，不适用。",
        # 多值：可以要求回答里同时出现多个必要标记。
        single_valued=False,
    ),
}

_JUDGES: Final[dict[str, _Judge]] = {
    "final_state": _judge_final_state,
    "depth_level": _judge_depth_level,
    "depth_at_most": _judge_depth_at_most,
    "depth_at_least": _judge_depth_at_least,
    "model_calls_at_most": _judge_model_calls_at_most,
    "model_calls_at_least": _judge_model_calls_at_least,
    "metacognitive_loops_at_least": _judge_loops_at_least,
    "metacognitive_loops_at_most": _judge_loops_at_most,
    "stop_reason_present": _judge_stop_reason_present,
    "response_present": _judge_response_present,
    "judgment_present": _judge_judgment_present,
    "hypothesis_count_at_least": _judge_hypothesis_at_least,
    "hypothesis_count_at_most": _judge_hypothesis_at_most,
    "analysis_module_ran": _judge_analysis_module_ran,
    "unresolved_unknowns_present": _judge_unknowns_present,
    "counterargument_present": _judge_counterargument_present,
    "confidence_band_at_most": _judge_confidence_band_at_most,
    "epistemic_action": _judge_epistemic_action,
    "response_contains": _judge_response_contains,
}


class AssertionResult(BaseModel):
    """一条断言的执行结果。

    🔴 **``observed`` 必须保留真实观测值。**
    只输出 ``{"passed": false}`` 的失败是不可诊断的——S1a 的目的之一
    正是"失败结果是否具有足够的诊断信息"。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    #: ``required`` 或 ``forbidden``——离开它，``passed`` 无法解释。
    mode: str = Field(pattern="^(required|forbidden)$")
    expected: str | int | bool
    #: ``None`` 表示**不可观测**（与"观测到空"不同）。
    observed: str | int | bool | list[str] | None
    passed: bool
    #: 人话判定说明（仅出现在原始输出里，canonical 不含它）。
    detail: str = ""


def assertion_names() -> tuple[str, ...]:
    """注册表里全部断言名（排序后，供错误消息与测试使用）。"""
    return tuple(sorted(ASSERTIONS))


def spec_for(name: str) -> AssertionSpec:
    """取一条断言的定义。

    Raises:
        KeyError: 名字不在注册表里。调用方负责把它转成加载期错误。
    """
    return ASSERTIONS[name]


def evaluate(
    name: str,
    mode: str,
    expected: str | int | bool,
    observation: CaseObservation,
) -> AssertionResult:
    """执行一条断言。

    Args:
        name: 断言名（必须在注册表里）。
        mode: ``required`` 或 ``forbidden``。
        expected: 期望值（类型已在加载期校验过）。
        observation: 本案例的真实观测。

    Returns:
        断言结果。``observed`` 保留真实值；不可观测时为 ``None`` 且 ``passed=False``。

    Raises:
        KeyError: 断言名不在注册表里。
        ValueError: ``mode`` 取值非法。
    """
    if mode not in ("required", "forbidden"):
        msg = f"未知的断言模式：{mode!r}"
        raise ValueError(msg)

    judge = _JUDGES[name]
    outcome = judge(observation, expected)

    if outcome.satisfied is None:
        passed = False
        detail = f"[不可观测] {outcome.detail}"
    else:
        passed = outcome.satisfied if mode == "required" else not outcome.satisfied
        detail = outcome.detail

    return AssertionResult(
        name=name,
        mode=mode,
        expected=expected,
        observed=outcome.observed,
        passed=passed,
        detail=detail,
    )
