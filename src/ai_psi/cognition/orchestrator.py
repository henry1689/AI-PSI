"""认知流水线的**模块编排表**（纯函数，零 IO）。

本模块只回答一个问题：**给定深度，这次回合要跑哪些模块、按什么顺序？**

它不执行任何模块、不写任何事件、不知道仓储与 Provider 的存在。
真正的执行在 :class:`ai_psi.application.cognitive_runtime.CognitiveRuntime`——
那个位置由架构规则 3（"只有 application/ 能发起持久化写入"）决定，
而任务书 §4 的目录结构也正是这么划分的。

🔴 **矩阵与预算表必须一致。** 每个深度的模型调用数由
:data:`~ai_psi.cognition.depth_router.NOMINAL_MODEL_CALLS_BY_DEPTH` 声明，
由 ``tests/unit/test_orchestrator.py`` 断言两者不漂移。
矩阵多一个模块而预算没跟上，"循环永不超预算"就会在运行期破功。
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Final

from ai_psi.domain.enums import CognitiveDepth, RoundState

__all__ = [
    "MODULE_MATRIX",
    "CognitiveStep",
    "StepKind",
    "StepSpec",
    "plan_for_depth",
    "requires_model",
    "state_for_step",
]


class CognitiveStep(StrEnum):
    """流水线中的一个步骤。"""

    CONCERN_DETECTION = "concern_detection"
    INQUIRY_FRAMING = "inquiry_framing"
    CONTEXT_BUILDING = "context_building"
    EPISTEMIC_ANALYSIS = "epistemic_analysis"
    HYPOTHESIS_GENERATION = "hypothesis_generation"
    LOGICAL_ANALYSIS = "logical_analysis"
    CAUSAL_ANALYSIS = "causal_analysis"
    CONCEPT_ANALYSIS = "concept_analysis"
    DIALECTICAL_ANALYSIS = "dialectical_analysis"
    PHILOSOPHICAL_ANALYSIS = "philosophical_analysis"
    HYPOTHESIS_EVALUATION = "hypothesis_evaluation"
    JUDGMENT_SYNTHESIS = "judgment_synthesis"
    METACOGNITION = "metacognition"
    RESPONSE_PLANNING = "response_planning"
    RESPONSE_RENDERING = "response_rendering"


class StepKind(StrEnum):
    """步骤的性质——决定它在预算不足时的处理方式。"""

    DETERMINISTIC = "deterministic"
    """纯代码，不消耗模型调用。"""

    MODEL = "model"
    """需要一次模型调用。"""

    MODEL_REQUIRED = "model_required"
    """需要模型调用，且**不可跳过**——预算不足即回合失败。

    只有判断合成与回答渲染属于这一类：跳过它们，回合就拿不出结论，
    那与失败没有区别。
    """


class StepSpec:
    """一个步骤的静态描述。

    Attributes:
        step: 步骤标识。
        kind: 步骤性质。
        state: 该步骤所属的回合状态。
        prompt_task: 对应的 Prompt 任务名；确定性步骤为 ``None``。
    """

    __slots__ = ("kind", "prompt_task", "state", "step")

    def __init__(
        self,
        step: CognitiveStep,
        kind: StepKind,
        state: RoundState,
        prompt_task: str | None = None,
    ) -> None:
        self.step = step
        self.kind = kind
        self.state = state
        self.prompt_task = prompt_task

    def __repr__(self) -> str:
        return f"StepSpec(step={self.step.value!r}, kind={self.kind.value!r})"


_CONCERN = StepSpec(
    CognitiveStep.CONCERN_DETECTION, StepKind.MODEL, RoundState.TRIAGING, "concern_detector"
)
_FRAME = StepSpec(
    CognitiveStep.INQUIRY_FRAMING, StepKind.MODEL, RoundState.FRAMING, "inquiry_framer"
)
_CONTEXT = StepSpec(CognitiveStep.CONTEXT_BUILDING, StepKind.DETERMINISTIC, RoundState.RETRIEVING)
_EPISTEMIC = StepSpec(
    CognitiveStep.EPISTEMIC_ANALYSIS, StepKind.DETERMINISTIC, RoundState.ANALYZING
)
_HYPOTHESES = StepSpec(
    CognitiveStep.HYPOTHESIS_GENERATION,
    StepKind.MODEL,
    RoundState.ANALYZING,
    "hypothesis_generator",
)
_LOGICAL = StepSpec(
    CognitiveStep.LOGICAL_ANALYSIS, StepKind.MODEL, RoundState.ANALYZING, "logical_analyzer"
)
_CAUSAL = StepSpec(
    CognitiveStep.CAUSAL_ANALYSIS, StepKind.MODEL, RoundState.ANALYZING, "causal_analyzer"
)
_CONCEPT = StepSpec(
    CognitiveStep.CONCEPT_ANALYSIS, StepKind.MODEL, RoundState.ANALYZING, "concept_analyzer"
)
_DIALECTIC = StepSpec(
    CognitiveStep.DIALECTICAL_ANALYSIS,
    StepKind.MODEL,
    RoundState.ANALYZING,
    "dialectical_analyzer",
)
_PHILOSOPHY = StepSpec(
    CognitiveStep.PHILOSOPHICAL_ANALYSIS,
    StepKind.MODEL,
    RoundState.ANALYZING,
    "philosophical_analyzer",
)
_EVALUATE = StepSpec(
    CognitiveStep.HYPOTHESIS_EVALUATION, StepKind.DETERMINISTIC, RoundState.DELIBERATING
)
_JUDGMENT = StepSpec(
    CognitiveStep.JUDGMENT_SYNTHESIS,
    StepKind.MODEL_REQUIRED,
    RoundState.DELIBERATING,
    "judgment_synthesizer",
)
_METACOGNITION = StepSpec(
    CognitiveStep.METACOGNITION,
    StepKind.MODEL,
    RoundState.METACOGNITIVE_REVIEW,
    "metacognition",
)
_PLAN = StepSpec(CognitiveStep.RESPONSE_PLANNING, StepKind.DETERMINISTIC, RoundState.SYNTHESIZING)
_RENDER = StepSpec(
    CognitiveStep.RESPONSE_RENDERING,
    StepKind.MODEL_REQUIRED,
    RoundState.RESPONDING,
    "response_renderer",
)

#: 深度 → 步骤序列（任务书 §6.3 的模块启用矩阵，ADR-0008）。
MODULE_MATRIX: Final[MappingProxyType[CognitiveDepth, tuple[StepSpec, ...]]] = MappingProxyType(
    {
        # D0：直接回答。不做假设、不做逻辑检查、不做元认知（循环上限为 0）。
        CognitiveDepth.D0: (
            _CONCERN,
            _FRAME,
            _CONTEXT,
            _EPISTEMIC,
            _EVALUATE,
            _JUDGMENT,
            _PLAN,
            _RENDER,
        ),
        # D1：加基础逻辑分析与一次元认知复核。
        CognitiveDepth.D1: (
            _CONCERN,
            _FRAME,
            _CONTEXT,
            _EPISTEMIC,
            _LOGICAL,
            _EVALUATE,
            _JUDGMENT,
            _METACOGNITION,
            _PLAN,
            _RENDER,
        ),
        # D2：加多假设与因果分析。
        CognitiveDepth.D2: (
            _CONCERN,
            _FRAME,
            _CONTEXT,
            _EPISTEMIC,
            _HYPOTHESES,
            _LOGICAL,
            _CAUSAL,
            _EVALUATE,
            _JUDGMENT,
            _METACOGNITION,
            _PLAN,
            _RENDER,
        ),
        # D3：加概念澄清与辩证分析。
        CognitiveDepth.D3: (
            _CONCERN,
            _FRAME,
            _CONTEXT,
            _EPISTEMIC,
            _CONCEPT,
            _HYPOTHESES,
            _LOGICAL,
            _CAUSAL,
            _DIALECTIC,
            _EVALUATE,
            _JUDGMENT,
            _METACOGNITION,
            _PLAN,
            _RENDER,
        ),
        # D4：加哲理框架分析。
        CognitiveDepth.D4: (
            _CONCERN,
            _FRAME,
            _CONTEXT,
            _EPISTEMIC,
            _CONCEPT,
            _HYPOTHESES,
            _LOGICAL,
            _CAUSAL,
            _DIALECTIC,
            _PHILOSOPHY,
            _EVALUATE,
            _JUDGMENT,
            _METACOGNITION,
            _PLAN,
            _RENDER,
        ),
    }
)


def plan_for_depth(depth: CognitiveDepth) -> tuple[StepSpec, ...]:
    """返回该深度的完整步骤序列。

    Args:
        depth: 认知深度。

    Returns:
        按执行顺序排列的步骤。
    """
    return MODULE_MATRIX[depth]


def requires_model(spec: StepSpec) -> bool:
    """该步骤是否需要模型调用。

    Args:
        spec: 步骤描述。

    Returns:
        需要返回 ``True``。
    """
    return spec.kind is not StepKind.DETERMINISTIC


def state_for_step(step: CognitiveStep, depth: CognitiveDepth) -> RoundState:
    """返回该步骤所属的回合状态。

    Args:
        step: 步骤。
        depth: 认知深度。

    Returns:
        该步骤对应的回合状态。

    Raises:
        KeyError: 该深度下不存在此步骤。
    """
    for spec in MODULE_MATRIX[depth]:
        if spec.step is step:
            return spec.state
    msg = f"深度 {depth.value} 的模块矩阵中不存在步骤 {step.value!r}"
    raise KeyError(msg)


def nominal_model_calls(depth: CognitiveDepth) -> int:
    """该深度**单次分析**所需的模型调用数。

    Args:
        depth: 认知深度。

    Returns:
        需要模型调用的步骤数。
    """
    return sum(1 for spec in MODULE_MATRIX[depth] if requires_model(spec))
