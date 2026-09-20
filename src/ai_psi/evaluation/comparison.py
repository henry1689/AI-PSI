"""Baseline / Candidate 的结构化对比（阶段 7 · S5）。

## 它回答什么

在**实验条件一致**的前提下，两份评测结果之间**发生了什么变化**？

## 🔴 它不回答什么

* Candidate 是否**允许发布**；
* 差异是否达到**发布阈值**；
* 模型总体质量是否提高；
* 10 个案例是否代表生产质量。

本模块**没有**任何阈值、总分、加权分数、风险分数、发布建议字段——
不是"暂时没填"，是结构上不存在。模型一律 ``extra="forbid"``，
多写一个这样的字段就是校验失败。

## S3 comparability 与 S5 eligibility 是两回事

:func:`~ai_psi.evaluation.manifest.compare_manifests` 回答的是
"这两份结果是否具有**完全相同的版本身份**"，因此不同提交必然返回
``comparable=False``。

本模块回答的是另一个问题："这两份**不同或相同**代码版本的评测结果，
是否具备进行受控差异分析的条件？"

======  ========================  ==============================
情形     S3 ``comparable``         S5 ``comparison_eligible``
======  ========================  ==============================
仅 SHA  ``False``                 **``True``**（进 ``allowed_differences``）
不同
数据集   ``False``                 ``False``（``dataset_differs`` 进 ``blockers``）
也变
完全     ``True``                  ``True``
相同
======  ========================  ==============================

🔴 **S3 的语义一行未改**：本模块调用 :func:`compare_manifests` 并原样保留
它返回的每一个原因码，只是把 ``code_revision_differs`` 从"阻塞"改判为
"允许但必须记录"。``manifest_identity_equal`` 就是 S3 的 ``comparable``，
所以"不同 SHA"**不会**被伪装成"身份相同"。

## 三条硬规矩

1. **方向统一是 ``candidate - baseline``。** 角色由调用方显式给出，
   不按文件名猜、不按参数顺序猜、不自动交换。
2. **不可比较时一个数字都不出。** :class:`EvaluationComparison` 在构造时
   校验：``comparison_eligible`` 为假时，五组差异结构必须**全部**为 ``None``。
   这比"记得别填"可靠——它让"输出误导性的部分差异"变成构造失败。
3. **分类一律来自结构化字段。** 案例失败种类看 ``failure_kind``，
   断言状态看 ``observation_status``。**没有一处**读 ``failure_reason``、
   ``detail`` 或 ``response_text``。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ai_psi.domain.enums import CognitiveDepth, RoundState
from ai_psi.evaluation.manifest import (
    REASON_ASSERTION_REGISTRY_DIFFERS,
    REASON_CODE_REVISION_DIFFERS,
    REASON_DATASET_DIFFERS,
    REASON_DIRTY_WORKTREE,
    REASON_EXECUTION_MODE_DIFFERS,
    REASON_IDENTITY_UNAVAILABLE,
    REASON_MANIFEST_SCHEMA_DIFFERS,
    REASON_MIGRATION_REVISION_DIFFERS,
    REASON_MODEL_DIFFERS,
    REASON_PROMPT_VERSIONS_DIFFER,
    REASON_PROVIDER_CONFIGURATION_DIFFERS,
    REASON_PROVIDER_DIFFERS,
    REASON_STORAGE_BACKEND_DIFFERS,
    compare_manifests,
)
from ai_psi.evaluation.metrics import (
    EvaluationMetrics,
    RatioMetric,
)
from ai_psi.evaluation.runner import RESULT_SCHEMA_VERSION, RunResult
from ai_psi.evaluation.serialization import ReportFormatError, dumps, parse_report

__all__ = [
    "ALLOWED_DIFFERENCE_CODES",
    "BLOCKER_CODES",
    "COMPARISON_SCHEMA_VERSION",
    "DELTA_PRECISION",
    "AssertionGroupDelta",
    "AssertionNameDelta",
    "AssertionStatus",
    "AssertionTransition",
    "AssertionTransitionSummary",
    "AssertionTransitionType",
    "CaseMetricsDelta",
    "CaseTransition",
    "CaseTransitionSummary",
    "CaseTransitionType",
    "CategoryDelta",
    "ComparisonEligibility",
    "ComparisonIdentity",
    "ComparisonInputError",
    "ComparisonRole",
    "CountDelta",
    "DeltaDirection",
    "DistributionDelta",
    "EvaluationComparison",
    "FailureIndexDelta",
    "MetricDelta",
    "MetricsComparison",
    "RatioDelta",
    "assertion_key",
    "compare_run_results",
    "comparison_definition_digest",
    "evaluate_comparison_eligibility",
    "load_run_result",
    "write_comparison",
]

#: 对比契约自身的版本。**改公式或阻塞规则就要改它**（或改定义摘要）。
COMPARISON_SCHEMA_VERSION: Final[int] = 1

#: 比率差异保留的小数位数。与 S4 的 ``RATIO_PRECISION`` **必须一致**：
#: 差异是两个同精度数相减，精度不同会让最后一位出现无法解释的差。
DELTA_PRECISION: Final[int] = 6

_DELTA_QUANTUM: Final[Decimal] = Decimal(1).scaleb(-DELTA_PRECISION)

#: 差异方向：**统一**为 ``candidate - baseline``。
DELTA_DIRECTION: Final[str] = "candidate - baseline"

#: 完整 Git SHA 的形状。40 位十六进制——**不接受短 SHA**。
_FULL_SHA: Final[re.Pattern[str]] = re.compile(r"^[0-9a-fA-F]{40}$")


# ---------------------------------------------------------------------------
# 允许差异与阻塞原因
# ---------------------------------------------------------------------------

#: 🔴 **唯一被允许的身份差异。**
#:
#: 代码提交不同正是版本对比的**主题**：拒绝它等于拒绝做这件事。
#: 它不构成阻塞，但**必须**出现在 ``allowed_differences`` 里——
#: "允许"不等于"没发生"。
ALLOWED_DIFFERENCE_CODES: Final[tuple[str, ...]] = (REASON_CODE_REVISION_DIFFERS,)

#: 以下编码由 S3 原样给出，本模块不做任何改写。
BLOCKER_MANIFEST_SCHEMA_DIFFERS: Final[str] = REASON_MANIFEST_SCHEMA_DIFFERS
BLOCKER_DATASET_DIFFERS: Final[str] = REASON_DATASET_DIFFERS
BLOCKER_ASSERTION_REGISTRY_DIFFERS: Final[str] = REASON_ASSERTION_REGISTRY_DIFFERS
BLOCKER_PROMPT_VERSIONS_DIFFER: Final[str] = REASON_PROMPT_VERSIONS_DIFFER
BLOCKER_PROVIDER_DIFFERS: Final[str] = REASON_PROVIDER_DIFFERS
BLOCKER_MODEL_DIFFERS: Final[str] = REASON_MODEL_DIFFERS
BLOCKER_PROVIDER_CONFIGURATION_DIFFERS: Final[str] = REASON_PROVIDER_CONFIGURATION_DIFFERS
BLOCKER_EXECUTION_MODE_DIFFERS: Final[str] = REASON_EXECUTION_MODE_DIFFERS
BLOCKER_STORAGE_BACKEND_DIFFERS: Final[str] = REASON_STORAGE_BACKEND_DIFFERS
BLOCKER_MIGRATION_REVISION_DIFFERS: Final[str] = REASON_MIGRATION_REVISION_DIFFERS
BLOCKER_DIRTY_WORKTREE: Final[str] = REASON_DIRTY_WORKTREE
BLOCKER_IDENTITY_UNAVAILABLE: Final[str] = REASON_IDENTITY_UNAVAILABLE

#: S3 **不比较**、由本模块补上的身份项。
BLOCKER_DATASET_CASE_COUNT_DIFFERS: Final[str] = "dataset_case_count_differs"
BLOCKER_PACKAGE_VERSION_DIFFERS: Final[str] = "package_version_differs"
BLOCKER_PYTHON_VERSION_DIFFERS: Final[str] = "python_version_differs"
BLOCKER_NETWORK_POLICY_DIFFERS: Final[str] = "network_policy_differs"
BLOCKER_METRICS_SCHEMA_DIFFERS: Final[str] = "metrics_schema_differs"
BLOCKER_METRICS_DEFINITION_DIFFERS: Final[str] = "metrics_definition_differs"

#: 🔴 **部分运行**：这份结果没有跑完数据集（``not_executed_cases > 0``）。
#:
#: 它既不是"数据损坏"，也不是"数据集变了"——它是一份**合法但证据不全**的
#: 结果。单独给它一个码，是因为把它报成 ``case_set_inconsistent`` 会把
#: 使用者引向"去查数据集是不是被改过"这个**错误方向**。
#:
#: 任一侧部分运行即阻塞。理由是差异表会描述一个**不同的证据基础**：
#: 完整运行的 ``10/10`` 与部分运行的 ``5/5`` 都是 ``1.000000``，
#: ``case_pass_rate`` 的 delta 会是 0，而一半案例根本没跑。
BLOCKER_PARTIAL_RUN: Final[str] = "partial_run"

#: 输入**自身**的完整性问题——与"两份结果之间的差异"是两回事。
BLOCKER_INPUT_SCHEMA_INVALID: Final[str] = "input_schema_invalid"
BLOCKER_INPUT_METRICS_MISMATCH: Final[str] = "input_metrics_mismatch"
BLOCKER_DUPLICATE_CASE_ID: Final[str] = "duplicate_case_id"
BLOCKER_CASE_SET_INCONSISTENT: Final[str] = "case_set_inconsistent"
BLOCKER_ASSERTION_SET_INCONSISTENT: Final[str] = "assertion_set_inconsistent"
BLOCKER_ASSERTION_IDENTITY_AMBIGUOUS: Final[str] = "assertion_identity_ambiguous"

#: 全部阻塞码。**用于 :func:`comparison_definition_digest`**：规则表变了摘要必须变。
BLOCKER_CODES: Final[tuple[str, ...]] = (
    BLOCKER_ASSERTION_IDENTITY_AMBIGUOUS,
    BLOCKER_ASSERTION_REGISTRY_DIFFERS,
    BLOCKER_ASSERTION_SET_INCONSISTENT,
    BLOCKER_CASE_SET_INCONSISTENT,
    BLOCKER_DATASET_CASE_COUNT_DIFFERS,
    BLOCKER_DATASET_DIFFERS,
    BLOCKER_DIRTY_WORKTREE,
    BLOCKER_DUPLICATE_CASE_ID,
    BLOCKER_EXECUTION_MODE_DIFFERS,
    BLOCKER_IDENTITY_UNAVAILABLE,
    BLOCKER_INPUT_METRICS_MISMATCH,
    BLOCKER_INPUT_SCHEMA_INVALID,
    BLOCKER_MANIFEST_SCHEMA_DIFFERS,
    BLOCKER_METRICS_DEFINITION_DIFFERS,
    BLOCKER_METRICS_SCHEMA_DIFFERS,
    BLOCKER_MIGRATION_REVISION_DIFFERS,
    BLOCKER_MODEL_DIFFERS,
    BLOCKER_NETWORK_POLICY_DIFFERS,
    BLOCKER_PACKAGE_VERSION_DIFFERS,
    BLOCKER_PARTIAL_RUN,
    BLOCKER_PROMPT_VERSIONS_DIFFER,
    BLOCKER_PROVIDER_CONFIGURATION_DIFFERS,
    BLOCKER_PROVIDER_DIFFERS,
    BLOCKER_PYTHON_VERSION_DIFFERS,
    BLOCKER_STORAGE_BACKEND_DIFFERS,
)

#: 定义摘要覆盖的内容。
#:
#: 🔴 它回答的是"这些差异是**按什么规则**算出来的"。只写一个
#: ``"version": 1`` 是不够的——那没法告诉别人方向是怎么定的、
#: 哪些身份差异会拦下整次比较。
COMPARISON_DEFINITION: Final[dict[str, object]] = {
    "added_removed_policy": (
        "同一 dataset_digest 下新出现或消失的案例/断言是**完整性错误**，"
        "不是性能变化：记入 added/removed 并阻塞，不产生任何 delta"
    ),
    "allowed_differences": list(ALLOWED_DIFFERENCE_CODES),
    "assertion_transitions": {
        "failed_to_failed": "unchanged",
        "failed_to_passed": "improvement_transition",
        "failed_to_unobservable": "changed_unresolved",
        "passed_to_failed": "regression_transition",
        "passed_to_passed": "unchanged",
        "passed_to_unobservable": "regression_transition",
        "unobservable_to_failed": "changed_unresolved",
        "unobservable_to_passed": "improvement_transition",
        "unobservable_to_unobservable": "unchanged",
    },
    "blockers": list(BLOCKER_CODES),
    "case_transitions": {
        "failed_to_failed": "unchanged_fail",
        "failed_to_passed": "improvement_transition",
        "passed_to_failed": "regression_transition",
        "passed_to_passed": "unchanged_pass",
    },
    "code_revision_policy": (
        "SHA 相同或不同都允许比较；不同时记入 allowed_differences。"
        "SHA 缺失或不是完整 40 位十六进制时阻塞"
    ),
    "count_delta": "delta = candidate - baseline",
    "delta_direction": DELTA_DIRECTION,
    "delta_precision": DELTA_PRECISION,
    "delta_rounding": "ROUND_HALF_UP",
    "direction_vocabulary": "increased / decreased / unchanged / unavailable（纯描述，不含好/坏）",
    "ineligible_policy": "comparison_eligible=false 时全部差异结构为 null，不产生任何部分 delta",
    "null_ratio_delta": "任一侧 value 为 null 时 delta = null（绝不写成 0.000000）",
    "ordering": (
        "案例按 case_id 字典序；断言按 (case_id, 声明索引)；类别与断言名按名字字典序；"
        "分布键按枚举正式顺序、其余按字典序；列表类字段去重后按字典序"
    ),
    "partial_run_policy": (
        "任一侧 not_executed_cases > 0（未跑完数据集）即阻塞："
        "差异表会描述一个不同的证据基础，而 case_pass_rate 可能毫无变化"
    ),
    "python_version_policy": "major/minor 不同阻塞；patch 不同不阻塞",
    "ratio_delta": "delta = Decimal(candidate.value) - Decimal(baseline.value)",
    "schema_version": COMPARISON_SCHEMA_VERSION,
}


def comparison_definition_digest() -> str:
    """对比定义的语义摘要。

    🔴 **改公式或阻塞规则必须让它变**：沿用旧摘要等于宣称
    "这两份差异是按同一套规则算出来的"，而它们不是。

    Returns:
        形如 ``sha256:<hex>`` 的摘要。
    """
    text = json.dumps(
        COMPARISON_DEFINITION, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


# ---------------------------------------------------------------------------
# 输入加载
# ---------------------------------------------------------------------------


class ComparisonInputError(ValueError):
    """一份输入结果**读不出来**或结构不合法。

    🔴 它不是"两份结果不可比较"。这个异常意味着**连读都没读成**：
    JSON 坏了、顶层字段不认识、manifest 或 metrics 缺失。

    这类错误**不产生 Comparison JSON**——一份基于未解析输入的"对比结果"
    无论长什么样都是误导，而"解析失败"尤其不能被当成"没有差异"。
    """


def _reject_constant(name: str) -> object:
    """拒绝 ``NaN`` / ``Infinity`` / ``-Infinity``。

    ⚠️ Python 的 ``json`` 模块**默认接受**这三个常量（这是它对标准的扩展），
    而它们一旦进了结果就是灾难：``NaN != NaN``，任何基于等值的比较都会
    静默为假，并且它会顺着差异计算污染整份产物。
    """
    msg = f"JSON 里不允许出现 {name}（NaN / Infinity 不是合法的评测结果）"
    raise ComparisonInputError(msg)


def _first_error(exc: ValidationError) -> str:
    """取第一处校验错误的一句话描述。

    ⚠️ 只取 ``type`` 与字段位置，**不带输入值**：错误消息会被打到 stderr，
    而输入值可能含回答正文之类不该扩散的东西。
    """
    errors = exc.errors()
    if not errors:
        return "未提供细节"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    return f"{location}: {first.get('type', 'unknown')}"


def load_run_result(path: Path) -> RunResult:
    """严格加载一份**原始**评测结果。

    🔴 **必须传原始结果，不能传 canonical。** canonical 是给"两次运行
    逐字节比对"用的稳定子集，它**刻意**不含 ``working_tree_clean``
    （那是"能不能当基线"的判定）——而 S5 的完整性校验正需要它。
    用 canonical 会让那条校验永远无从执行，那种"校验通过"是假的。

    校验顺序：UTF-8 → JSON → 顶层结构 → 逐字段模型 → 结果版本 →
    manifest / metrics 必须存在。

    Args:
        path: 结果文件路径。

    Returns:
        已通过结构校验的运行结果。

    Raises:
        ComparisonInputError: 读不了、不是 UTF-8、JSON 损坏、含 NaN/Infinity、
            顶层结构不符合 :class:`~ai_psi.evaluation.runner.RunResult`、
            结果版本不认识、或 manifest / metrics 缺失。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        msg = "不是合法的 UTF-8 文本"
        raise ComparisonInputError(msg) from exc
    except OSError as exc:
        msg = f"读取失败（{type(exc).__name__}）"
        raise ComparisonInputError(msg) from exc

    try:
        payload = json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        # 🔴 被截断的 JSON 走的就是这条路。**不**把解析失败当成"没有差异"。
        msg = f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）"
        raise ComparisonInputError(msg) from exc

    if not isinstance(payload, dict):
        msg = f"顶层必须是对象，实际是 {type(payload).__name__}"
        raise ComparisonInputError(msg)

    try:
        result = parse_report(payload)
    except ReportFormatError as exc:
        raise ComparisonInputError(str(exc)) from exc
    except ValidationError as exc:
        msg = f"结果结构校验失败：{exc.error_count()} 处（第一处：{_first_error(exc)}）"
        raise ComparisonInputError(msg) from exc

    if result.schema_version != RESULT_SCHEMA_VERSION:
        msg = f"不认识的结果版本：{result.schema_version}；本工具只认识 {RESULT_SCHEMA_VERSION}"
        raise ComparisonInputError(msg)

    if result.manifest is None:
        msg = "缺少可复现性清单（manifest）——没有身份的两次运行无法被比较"
        raise ComparisonInputError(msg)
    if result.metrics is None:
        msg = "缺少指标层结果（metrics）——S5 需要它来核对结构化结果是否自洽"
        raise ComparisonInputError(msg)

    return result


# ---------------------------------------------------------------------------
# 契约模型
# ---------------------------------------------------------------------------


class ComparisonRole(StrEnum):
    """一方在对比里的角色。

    🔴 **角色由调用方显式指定**，不从文件名推断、不按参数顺序推断、
    也不自动交换——"哪边是基线"是实验设计的一部分，不是命名约定。
    """

    BASELINE = "baseline"
    CANDIDATE = "candidate"


class ComparisonIdentity(BaseModel):
    """一方在对比中的身份。

    ⚠️ 这里**没有**数据库名、完整 URL、用户名、密码、主机。
    它们既不帮助理解差异，又会把运行环境带进可以公开的产物。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: ComparisonRole
    #: 完整 SHA；``None`` 表示**取不到**（不是 ``"unknown"``）。
    commit_sha: str | None
    package_version: str
    working_tree_clean: bool | None
    case_schema_version: int
    manifest_schema_version: int
    dataset_digest: str
    dataset_case_count: int
    assertion_registry_digest: str
    #: 清单里记的执行模式。
    manifest_execution_mode: str
    #: 结果文件自己记的执行模式。两者不一致说明这份结果内部不自洽。
    result_execution_mode: str
    result_schema_version: int
    prompt_versions_digest: str
    provider_name: str
    model_id: str
    provider_configuration_digest: str
    network_allowed: bool
    storage_backend: str
    alembic_revision: str | None
    python_version: str
    metrics_schema_version: int
    metrics_definition_digest: str


class ComparisonEligibility(BaseModel):
    """这两份结果能不能做受控差异分析。

    🔴 与 S3 的 ``comparable`` **不是同一个概念**：代码提交不同在这里
    是允许的（见 :data:`ALLOWED_DIFFERENCE_CODES`）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: S3 ``compare_manifests`` 的原样结论——**身份契约**是否完全一致。
    manifest_identity_equal: bool
    #: 具备做差异分析的条件。
    comparison_eligible: bool
    #: 被显式允许、但必须记录下来的差异。
    allowed_differences: tuple[str, ...]
    #: 阻塞差异。非空即 ``comparison_eligible=False``。
    blockers: tuple[str, ...]

    @model_validator(mode="after")
    def _check(self) -> Self:
        """可比较与"没有阻塞项"必须等价。

        允许一个"有阻塞却判成可比较"的模型存在，等于把这条判据交给
        每个调用点各写一遍。
        """
        if self.comparison_eligible != (not self.blockers):
            msg = (
                f"comparison_eligible={self.comparison_eligible} 与 "
                f"blockers={list(self.blockers)} 不自洽"
            )
            raise ValueError(msg)
        for name, items in (
            ("allowed_differences", self.allowed_differences),
            ("blockers", self.blockers),
        ):
            if sorted(set(items)) != list(items):
                msg = f"{name} 必须去重并稳定排序"
                raise ValueError(msg)
        return self


class DeltaDirection(StrEnum):
    """数值方向。**纯描述，不含好/坏。**

    🔴 这里**没有** ``favorable`` / ``unfavorable`` / ``neutral``：
    本切片不判断哪个方向"更好"。通过率上升是不是好事、某类分布变化
    是不是退化，都需要有标签的评测集与更多案例才能回答，而那两样
    现在都没有。少一个词，就少一处被摘出来当发布依据的可能。
    """

    INCREASED = "increased"
    DECREASED = "decreased"
    UNCHANGED = "unchanged"
    UNAVAILABLE = "unavailable"


def _direction_of(delta: Decimal | int | None) -> DeltaDirection:
    """由数值差异定方向；``None`` 表示**不可用**（不是"没变化"）。"""
    if delta is None:
        return DeltaDirection.UNAVAILABLE
    if delta > 0:
        return DeltaDirection.INCREASED
    if delta < 0:
        return DeltaDirection.DECREASED
    return DeltaDirection.UNCHANGED


class CountDelta(BaseModel):
    """一个计数项的差异：``delta = candidate - baseline``。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline: int = Field(ge=0)
    candidate: int = Field(ge=0)
    delta: int
    direction: DeltaDirection

    @model_validator(mode="after")
    def _check(self) -> Self:
        """🔴 ``delta`` 由构造方传入，但**必须**等于 ``candidate - baseline``。

        允许调用者传一个不一致的 delta，等于让"差异"变成一个可以被写错
        而没人发现的字段。
        """
        if self.delta != self.candidate - self.baseline:
            msg = (
                f"delta={self.delta} 与 candidate-baseline={self.candidate - self.baseline} 不一致"
            )
            raise ValueError(msg)
        if self.direction != _direction_of(self.delta):
            msg = f"direction={self.direction!r} 与 delta={self.delta} 不一致"
            raise ValueError(msg)
        return self


def _ratio_delta(baseline: RatioMetric, candidate: RatioMetric) -> str | None:
    """算两个比率之差；任一侧不可用时返回 ``None``。

    ⚠️ ``None`` **不是** ``"0.000000"``：一份"0/0"与一份"两边都是 0%"
    是两回事，把前者写成零差异正是本模块要防的那种假指标。
    """
    if baseline.value is None or candidate.value is None:
        return None
    value = (Decimal(candidate.value) - Decimal(baseline.value)).quantize(
        _DELTA_QUANTUM, rounding=ROUND_HALF_UP
    )
    if value == 0:
        # 避免 ``-0.000000``：它和 ``0.000000`` 数值相同，却会让
        # "逐字节一致"这类判据在一个没有语义差异的地方失败。
        value = value.copy_abs()
    return f"{value:.{DELTA_PRECISION}f}"


class RatioDelta(BaseModel):
    """一个比率项的差异。

    🔴 **保留双方的完整比率**（分子、分母、值），不只给差值。
    只看 ``0.100000`` 无法回答"这是分子涨了还是分母缩了"——
    而"10/10 变 9/9"与"9/10 变 9/9"是完全不同的两件事。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline: RatioMetric
    candidate: RatioMetric
    #: 固定 6 位小数；任一侧 ``value`` 为 ``None`` 时是 ``None``。
    delta: str | None
    direction: DeltaDirection

    @model_validator(mode="after")
    def _check(self) -> Self:
        expected = _ratio_delta(self.baseline, self.candidate)
        if self.delta != expected:
            msg = f"delta={self.delta!r} 与两侧比率算出的 {expected!r} 不一致"
            raise ValueError(msg)
        numeric = None if self.delta is None else Decimal(self.delta)
        if self.direction != _direction_of(numeric):
            msg = f"direction={self.direction!r} 与 delta={self.delta!r} 不一致"
            raise ValueError(msg)
        return self


#: 数值差异的两种形态。用它标注"这里接受任一种"。
MetricDelta = CountDelta | RatioDelta


def _count_delta(baseline: int, candidate: int) -> CountDelta:
    delta = candidate - baseline
    return CountDelta(
        baseline=baseline, candidate=candidate, delta=delta, direction=_direction_of(delta)
    )


def _ratio_delta_of(baseline: RatioMetric, candidate: RatioMetric) -> RatioDelta:
    delta = _ratio_delta(baseline, candidate)
    return RatioDelta(
        baseline=baseline,
        candidate=candidate,
        delta=delta,
        direction=_direction_of(None if delta is None else Decimal(delta)),
    )


class CaseTransitionType(StrEnum):
    """案例判定的四类转换。

    ⚠️ 这些名字**只描述判定转换**，不是发布结论。
    ``regression_transition`` 不等于"禁止发布"，``improvement_transition``
    也不等于"允许发布"。
    """

    UNCHANGED_PASS = "unchanged_pass"
    REGRESSION = "regression_transition"
    IMPROVEMENT = "improvement_transition"
    UNCHANGED_FAIL = "unchanged_fail"


def _case_transition_type(baseline_passed: bool, candidate_passed: bool) -> CaseTransitionType:
    if baseline_passed and candidate_passed:
        return CaseTransitionType.UNCHANGED_PASS
    if baseline_passed:
        return CaseTransitionType.REGRESSION
    if candidate_passed:
        return CaseTransitionType.IMPROVEMENT
    return CaseTransitionType.UNCHANGED_FAIL


class CaseTransition(BaseModel):
    """同一个 ``case_id`` 在两份结果之间的转换。

    🔴 只有**稳定结构字段**：没有 ``response_text``、没有完整
    ``failure_detail``、没有异常堆栈、没有数据库信息。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    category: str
    baseline_passed: bool
    candidate_passed: bool
    transition: CaseTransitionType
    baseline_failure_kind: str | None
    candidate_failure_kind: str | None
    baseline_final_state: str | None
    candidate_final_state: str | None
    baseline_depth: str | None
    candidate_depth: str | None
    baseline_stop_reason: str | None
    candidate_stop_reason: str | None
    #: 本案例里状态**发生了变化**的断言键（稳定排序）。
    changed_assertion_keys: tuple[str, ...]

    @model_validator(mode="after")
    def _check(self) -> Self:
        expected = _case_transition_type(self.baseline_passed, self.candidate_passed)
        if self.transition != expected:
            msg = f"transition={self.transition!r} 与双方判定不一致（应为 {expected!r}）"
            raise ValueError(msg)
        if sorted(set(self.changed_assertion_keys)) != list(self.changed_assertion_keys):
            msg = "changed_assertion_keys 必须去重并稳定排序"
            raise ValueError(msg)
        return self


class CaseTransitionSummary(BaseModel):
    """四类案例转换的计数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_cases: int = Field(ge=0)
    unchanged_pass_count: int = Field(ge=0)
    regression_transition_count: int = Field(ge=0)
    improvement_transition_count: int = Field(ge=0)
    unchanged_fail_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        parts = (
            self.unchanged_pass_count
            + self.regression_transition_count
            + self.improvement_transition_count
            + self.unchanged_fail_count
        )
        if parts != self.total_cases:
            msg = f"四类转换之和 {parts} 不等于案例总数 {self.total_cases}"
            raise ValueError(msg)
        return self


class AssertionStatus(StrEnum):
    """一条断言在某一侧的状态。

    🔴 三态，不是两态：``failed`` 是"比较过了、确实对不上"，
    ``unobservable`` 是"压根没读到"。混成一类会让"系统在这个断言上
    表现如何"无从判断。
    """

    PASSED = "passed"
    FAILED = "failed"
    UNOBSERVABLE = "unobservable"


class AssertionTransitionType(StrEnum):
    """断言状态的转换分类。"""

    UNCHANGED = "unchanged"
    REGRESSION = "regression_transition"
    IMPROVEMENT = "improvement_transition"
    CHANGED_UNRESOLVED = "changed_unresolved"


#: 九种状态转移 → 分类。闭集，逐条写死。
_ASSERTION_TRANSITIONS: Final[
    dict[tuple[AssertionStatus, AssertionStatus], AssertionTransitionType]
] = {
    (AssertionStatus.PASSED, AssertionStatus.PASSED): AssertionTransitionType.UNCHANGED,
    (AssertionStatus.PASSED, AssertionStatus.FAILED): AssertionTransitionType.REGRESSION,
    (AssertionStatus.PASSED, AssertionStatus.UNOBSERVABLE): AssertionTransitionType.REGRESSION,
    (AssertionStatus.FAILED, AssertionStatus.PASSED): AssertionTransitionType.IMPROVEMENT,
    (AssertionStatus.FAILED, AssertionStatus.FAILED): AssertionTransitionType.UNCHANGED,
    (AssertionStatus.FAILED, AssertionStatus.UNOBSERVABLE): (
        AssertionTransitionType.CHANGED_UNRESOLVED
    ),
    (AssertionStatus.UNOBSERVABLE, AssertionStatus.PASSED): AssertionTransitionType.IMPROVEMENT,
    (AssertionStatus.UNOBSERVABLE, AssertionStatus.FAILED): (
        AssertionTransitionType.CHANGED_UNRESOLVED
    ),
    (AssertionStatus.UNOBSERVABLE, AssertionStatus.UNOBSERVABLE): AssertionTransitionType.UNCHANGED,
}


class AssertionTransition(BaseModel):
    """同一条断言在两份结果之间的转换。

    🔴 **不复制 ``detail``，也不复制 ``expected`` / ``observed`` 的原文。**
    "哪条断言变了、变成了什么"是结论；把它当时的期望值与观测值再抄一遍，
    等于把易变内容搬进了一份号称确定性的产物。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    #: 稳定断言键（见 :func:`assertion_key`）。
    assertion_key: str
    assertion_name: str
    expectation_kind: str = Field(pattern="^(required|forbidden)$")
    baseline_status: AssertionStatus
    candidate_status: AssertionStatus
    transition: AssertionTransitionType

    @model_validator(mode="after")
    def _check(self) -> Self:
        expected = _ASSERTION_TRANSITIONS[(self.baseline_status, self.candidate_status)]
        if self.transition != expected:
            msg = (
                f"transition={self.transition!r} 与 "
                f"{self.baseline_status}→{self.candidate_status} 不一致（应为 {expected!r}）"
            )
            raise ValueError(msg)
        return self


class AssertionTransitionSummary(BaseModel):
    """断言转换的计数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_assertions: int = Field(ge=0)
    unchanged_count: int = Field(ge=0)
    regression_transition_count: int = Field(ge=0)
    improvement_transition_count: int = Field(ge=0)
    changed_unresolved_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        parts = (
            self.unchanged_count
            + self.regression_transition_count
            + self.improvement_transition_count
            + self.changed_unresolved_count
        )
        if parts != self.total_assertions:
            msg = f"四类断言转换之和 {parts} 不等于断言总数 {self.total_assertions}"
            raise ValueError(msg)
        return self


class DistributionDelta(BaseModel):
    """三项分布的逐键差异。

    键集合取双方**并集**，缺失的键按 0 处理；枚举键按正式枚举顺序，
    其余按字典序。⚠️ 序列化时 ``sort_keys=True`` 会再按字典序排一遍——
    两份产物因此仍然逐字节可比。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    final_state: dict[str, CountDelta]
    depth: dict[str, CountDelta]
    stop_reason: dict[str, CountDelta]


class FailureIndexDelta(BaseModel):
    """失败索引的集合差异。

    ⚠️ ``persistently_failed`` **不等于**"行为没变"：两次都没通过，
    但失败的断言可能换了一条。要回答那个问题得看断言转换。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    newly_failed_case_ids: tuple[str, ...]
    resolved_failed_case_ids: tuple[str, ...]
    persistently_failed_case_ids: tuple[str, ...]
    newly_execution_error_case_ids: tuple[str, ...]
    resolved_execution_error_case_ids: tuple[str, ...]
    newly_unobservable_case_ids: tuple[str, ...]
    resolved_unobservable_case_ids: tuple[str, ...]


class CaseMetricsDelta(BaseModel):
    """案例层计数的差异。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total_cases: CountDelta
    executed_cases: CountDelta
    passed_cases: CountDelta
    failed_cases: CountDelta
    not_executed_cases: CountDelta
    execution_error_cases: CountDelta
    assertion_failed_cases: CountDelta
    case_pass_rate: RatioDelta
    execution_coverage: RatioDelta


class AssertionGroupDelta(BaseModel):
    """一组断言的计数与比率差异。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    total: CountDelta
    evaluated: CountDelta
    passed: CountDelta
    failed: CountDelta
    unobservable: CountDelta
    pass_rate: RatioDelta
    observation_coverage: RatioDelta


class CategoryDelta(BaseModel):
    """一个类别的差异。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: str
    total: CountDelta
    executed: CountDelta
    passed: CountDelta
    failed: CountDelta
    not_executed: CountDelta
    pass_rate: RatioDelta


class AssertionNameDelta(BaseModel):
    """一个断言名下的差异。

    🔴 ``pass_rate`` 与 ``observation_coverage`` **分开输出**：
    前者回答"观测到之后有多少通过"，后者回答"有多少被观测到"。
    把它们并成一个数字，等于把两种不同的失败混为一谈。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    total: CountDelta
    required_total: CountDelta
    forbidden_total: CountDelta
    evaluated: CountDelta
    passed: CountDelta
    failed: CountDelta
    unobservable: CountDelta
    pass_rate: RatioDelta
    observation_coverage: RatioDelta


class MetricsComparison(BaseModel):
    """指标层的完整差异。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cases: CaseMetricsDelta
    categories: tuple[CategoryDelta, ...]
    assertions_overall: AssertionGroupDelta
    assertions_required: AssertionGroupDelta
    assertions_forbidden: AssertionGroupDelta
    assertion_names: tuple[AssertionNameDelta, ...]

    @model_validator(mode="after")
    def _check(self) -> Self:
        """明细与汇总的差异必须对得上。

        与 S4 同样的理由：汇总对不上明细的差异表，只是一堆看起来合理的
        数字。⚠️ 这里**不**重新校验 ``delta == candidate - baseline``——
        那由每个 :class:`CountDelta` 在构造时各自保证。
        """
        if sum(item.total.delta for item in self.categories) != self.cases.total_cases.delta:
            msg = "类别 total 差异之和与总体 total_cases 差异不一致"
            raise ValueError(msg)
        for field in ("total", "evaluated", "passed", "failed", "unobservable"):
            parts = sum(getattr(item, field).delta for item in self.assertion_names)
            whole = getattr(self.assertions_overall, field).delta
            if parts != whole:
                msg = f"按名称的 {field} 差异之和 {parts} 与总体 {whole} 不一致"
                raise ValueError(msg)
        if [item.category for item in self.categories] != sorted(
            item.category for item in self.categories
        ):
            msg = "类别必须按名字稳定排序"
            raise ValueError(msg)
        if [item.name for item in self.assertion_names] != sorted(
            item.name for item in self.assertion_names
        ):
            msg = "断言名必须稳定排序"
            raise ValueError(msg)
        return self


class EvaluationComparison(BaseModel):
    """一次 Baseline / Candidate 对比的完整产物。

    🔴 **它不含发布结论**：没有 ``release_allowed``、``gate_passed``、
    ``quality_score``、``risk_score``、``recommendation``。这些字段
    **根本不存在**于这个模型里，而不是"存在但没填"。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    comparison_schema_version: int = COMPARISON_SCHEMA_VERSION
    comparison_definition_digest: str
    baseline: ComparisonIdentity
    candidate: ComparisonIdentity
    manifest_identity_equal: bool
    comparison_eligible: bool
    allowed_differences: tuple[str, ...]
    blockers: tuple[str, ...]
    #: 本轮**没能独立复核**的项。非空不等于出错，但必须被看见——
    #: "没查"与"查过没问题"是两件事，把它们写成一个样子就是在假装。
    integrity_notes: tuple[str, ...] = ()
    #: 🔴 重算结果与文件里那份**对不上**的具体项。非空即 ``input_metrics_mismatch``。
    #: 它存在的理由：只说"指标不一致"而不说哪一项，读者既没法修，
    #: 也没法判断这是不是误报。
    integrity_mismatches: tuple[str, ...] = ()

    # ---- 五组差异结构：可比较时全部非空，不可比较时全部为 null ----
    metrics_comparison: MetricsComparison | None
    case_transitions: tuple[CaseTransition, ...] | None
    assertion_transitions: tuple[AssertionTransition, ...] | None
    distribution_deltas: DistributionDelta | None
    failure_index_delta: FailureIndexDelta | None

    case_transition_summary: CaseTransitionSummary | None
    assertion_transition_summary: AssertionTransitionSummary | None

    # ---- 集合完整性：**总是**输出，空集合就是空元组 ----
    added_case_ids: tuple[str, ...] = ()
    removed_case_ids: tuple[str, ...] = ()
    added_assertion_keys: tuple[str, ...] = ()
    removed_assertion_keys: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> Self:
        """🔴 三条不变量。

        1. 可比较 ⇔ 没有阻塞项；
        2. 可比较 ⇒ 五组差异结构**全部**存在；
        3. 不可比较 ⇒ 五组差异结构**全部**为 ``None``。

        第 3 条是这个模型存在的核心理由：它把"不可比较时输出了部分
        改善/退化数字"变成**构造失败**，而不是一条要靠自觉遵守的纪律。
        """
        if self.comparison_eligible != (not self.blockers):
            msg = "comparison_eligible 与 blockers 不自洽"
            raise ValueError(msg)

        deltas: tuple[object, ...] = (
            self.metrics_comparison,
            self.case_transitions,
            self.assertion_transitions,
            self.distribution_deltas,
            self.failure_index_delta,
        )
        present = [item for item in deltas if item is not None]
        if self.comparison_eligible and len(present) != len(deltas):
            msg = "可比较的结果必须产出全部五组差异结构"
            raise ValueError(msg)
        if not self.comparison_eligible and present:
            msg = "不可比较时不得产出任何数值差异（五组结构必须全为 null）"
            raise ValueError(msg)

        if self.baseline.role is not ComparisonRole.BASELINE:
            msg = "baseline 的身份角色必须是 baseline"
            raise ValueError(msg)
        if self.candidate.role is not ComparisonRole.CANDIDATE:
            msg = "candidate 的身份角色必须是 candidate"
            raise ValueError(msg)

        for name, items in (
            ("added_case_ids", self.added_case_ids),
            ("removed_case_ids", self.removed_case_ids),
            ("added_assertion_keys", self.added_assertion_keys),
            ("removed_assertion_keys", self.removed_assertion_keys),
            ("allowed_differences", self.allowed_differences),
            ("blockers", self.blockers),
            ("integrity_notes", self.integrity_notes),
            ("integrity_mismatches", self.integrity_mismatches),
        ):
            if sorted(set(items)) != list(items):
                msg = f"{name} 必须去重并稳定排序"
                raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# 稳定的断言身份
# ---------------------------------------------------------------------------


def assertion_key(case_id: str, index: int, *, mode: str, name: str) -> str:
    """一条断言结果的**稳定键**。

    🔴 **声明索引是键，名字与模式只是可读性。**

    为什么不用 ``(case_id, name, mode)`` 当键：同一个案例里，
    ``analysis_module_ran`` 与 ``response_contains`` 是**多值**断言
    （``single_valued=False``），"逻辑**和**因果都要跑"是完全正常的期望，
    于是同一案例内会出现同名、同 mode 的两条断言。用名字当键会静默覆盖。

    为什么可以相信索引：断言按**声明顺序**求值（``required`` 在前、
    ``forbidden`` 在后），这是 ``GoldenRunner`` 的既有契约，且被
    ``test_assertion_order_is_the_declaration_order`` 钉住。
    ⚠️ 键取的是**产出顺序**的位置，代码里**没有**对断言排序这一步——
    它不是"排序后的偶然位置"。

    Args:
        case_id: 案例 id。
        index: 该断言在结果里的**声明索引**（从 0 起）。
        mode: ``required`` 或 ``forbidden``，只为可读性。
        name: 断言名，只为可读性。

    Returns:
        形如 ``case#idx#mode#name`` 的键。
    """
    return f"{case_id}#{index}#{mode}#{name}"


def _status_of(observation_status: str, passed: bool) -> AssertionStatus:
    """``AssertionResult`` 的三态。

    🔴 **只看结构化字段**：``observation_status`` 与 ``passed``。
    不读 ``detail``（那是措辞），也不靠 ``observed is None`` 反推
    （S4 已经把它从隐式约定升格为显式字段，这里就用那个字段）。
    """
    if observation_status == "unobservable":
        return AssertionStatus.UNOBSERVABLE
    return AssertionStatus.PASSED if passed else AssertionStatus.FAILED


# ---------------------------------------------------------------------------
# 稳定性：排序与集合
# ---------------------------------------------------------------------------


def _ordered_keys(
    left: Mapping[str, int],
    right: Mapping[str, int],
    preferred: Sequence[str] | None,
) -> tuple[str, ...]:
    """两个映射的键并集，按**正式顺序**排。

    枚举键按枚举定义顺序（``d0`` 在 ``d1`` 前），其余按字典序。
    ⚠️ **不丢弃任何真实出现的值**——出现了一个枚举里没有的状态，
    那是需要被看见的事实，不是需要抹掉的噪声。
    """
    keys = set(left) | set(right)
    ordered = [key for key in (preferred or ()) if key in keys]
    ordered.extend(sorted(keys - set(ordered)))
    return tuple(ordered)


def _assertion_entries(result: RunResult) -> dict[str, tuple[str, str, str]]:
    """断言键 → ``(mode, name, expected 的规范 JSON)``。

    ⚠️ 用 ``json.dumps`` 而不是 ``repr``：``repr`` 的写法随对象类型变化，
    而这里要的是一个**跨版本稳定**的比较值。它只用于**交叉校验**
    （见 :func:`_assertion_set_problems`），**不是**唯一键。
    """
    entries: dict[str, tuple[str, str, str]] = {}
    for case in result.cases:
        for index, assertion in enumerate(case.assertions):
            key = assertion_key(case.case_id, index, mode=assertion.mode, name=assertion.name)
            entries[key] = (
                assertion.mode,
                assertion.name,
                json.dumps(assertion.expected, ensure_ascii=False, sort_keys=True),
            )
    return entries


# ---------------------------------------------------------------------------
# 输入自洽性
# ---------------------------------------------------------------------------


def _expect(mismatches: set[str], name: str, stored: object, actual: object) -> None:
    """记录一处"文件里写的"与"由结构化结果重算的"不一致。

    ⚠️ 收的是 ``set`` 而不是 ``list``：同一项可能在多处被判定不一致，
    重复的项名既没有信息量，又会让输出长度取决于检查顺序。

    🔴 **记的是项名，不是值。** 只报一个 ``input_metrics_mismatch``
    等于告诉读者"有问题但别问是哪儿"——而定位不到具体项的不一致，
    既没法修也没法判断是不是误报。
    """
    if stored != actual:
        mismatches.add(name)


def _expect_ratio(
    mismatches: set[str],
    name: str,
    metric: RatioMetric,
    numerator: int,
    denominator: int,
) -> None:
    """复核一个比率的**分子与分母**。

    🔴 ``RatioMetric`` 的模型校验只保证 ``value`` 与 ``numerator/denominator``
    **自洽**——它管不了"分子分母本身是不是这次运行的真实计数"。
    一份"2/2 写成 1/1、value 也跟着改对"的指标能顺利通过模型校验，
    却与结构化案例结果对不上。这就是那道校验管不到的地方。
    """
    _expect(mismatches, f"{name}.numerator", metric.numerator, numerator)
    _expect(mismatches, f"{name}.denominator", metric.denominator, denominator)


def _duplicate_case_ids(result: RunResult) -> tuple[str, ...]:
    """同一份结果里重复出现的 ``case_id``（稳定排序）。"""
    seen: set[str] = set()
    duplicates: set[str] = set()
    for case in result.cases:
        if case.case_id in seen:
            duplicates.add(case.case_id)
        seen.add(case.case_id)
    return tuple(sorted(duplicates))


def _ambiguous_assertions(result: RunResult) -> tuple[str, ...]:
    """同一案例内**无法唯一识别**的断言三元组。

    ``(mode, name, expected)`` 在同一案例里唯一，是**案例模型**的校验器
    保证的（``CaseExpectations._check_shape``）。结果 JSON 没有那道校验，
    所以这里补上：出现重复说明这份结果不是那个模型产出的，
    而不是"断言多了几条"。

    Returns:
        形如 ``case_id::mode::name`` 的问题位置（稳定排序）。
    """
    problems: set[str] = set()
    for case in result.cases:
        seen: set[tuple[str, str, str]] = set()
        for assertion in case.assertions:
            triple = (
                assertion.mode,
                assertion.name,
                json.dumps(assertion.expected, ensure_ascii=False, sort_keys=True),
            )
            if triple in seen:
                problems.add(f"{case.case_id}::{assertion.mode}::{assertion.name}")
            seen.add(triple)
    return tuple(sorted(problems))


def _complete_run(result: RunResult) -> bool:
    """这次运行是否覆盖了数据集里的**全部**案例。

    🔴 判据是 ``len(cases) == manifest.dataset_case_count``——
    **不**读 ``metrics.cases.not_executed_cases``。用被检验的那份
    ``metrics`` 去决定"要不要检验它"是循环论证。
    """
    manifest = result.manifest
    if manifest is None:  # pragma: no cover - 加载器已保证
        return False
    return len(result.cases) == manifest.evaluation.dataset_case_count


def _integrity_problems(
    result: RunResult,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """校验一份结果**内部自洽**，并如实报告查不了的部分。

    🔴 **不拿文件里那份 ``metrics`` 当自己的证据。** 计数一律由
    ``result.cases`` 与 ``result.cases[].assertions`` 重新数出来
    （失败种类看 ``failure_kind``，断言状态看 ``observation_status``），
    再与存储值逐项比对。

    ⚠️ **有一项确实重算不了**：类别的 ``total`` 来自**数据集**（含未执行的
    案例），而结果文件里只有执行过的案例。完整运行时它可以由结果推出；
    部分运行时**无从独立复核**，此时如实记进 notes，而不是假装查过。

    Args:
        result: 已通过结构校验的结果。

    Returns:
        ``(阻塞码, 未复核说明, 具体不一致项)``，三者都稳定排序。
        🔴 第三项不是装饰：只给一个 ``input_metrics_mismatch`` 而不说
        哪一项对不上，读者无从下手——定位不了的不一致既没法修，
        也没法判断是不是误报。
    """
    problems: set[str] = set()
    notes: set[str] = set()
    mismatches: set[str] = set()

    manifest = result.manifest
    metrics = result.metrics
    if manifest is None or metrics is None:  # pragma: no cover - 加载器已保证
        return (BLOCKER_INPUT_SCHEMA_INVALID,), (), ()

    # ---- 部分运行（S5 补丁）----
    #
    # 🔴 判据是 ``len(cases) != manifest.dataset_case_count``，**不读**
    # ``metrics.cases.not_executed_cases``——用被检验的那份指标去决定
    # "这份结果跑完了没有"，等于让被检验者给自己作证。
    if not _complete_run(result):
        problems.add(BLOCKER_PARTIAL_RUN)

    # ---- 身份可用性（§九 第 17、18 项）----
    sha = manifest.code.commit_sha
    if sha is None or not _FULL_SHA.match(sha):
        # 缺失与"格式不对"都归到这里：两者都让"这份结果来自哪个提交"
        # 变成不可回答的问题，而不可回答的身份不是身份。
        problems.add(BLOCKER_IDENTITY_UNAVAILABLE)
    if manifest.code.working_tree_clean is not True:
        problems.add(
            BLOCKER_DIRTY_WORKTREE
            if manifest.code.working_tree_clean is False
            else BLOCKER_IDENTITY_UNAVAILABLE
        )

    # ---- 结果与清单的内部一致性 ----
    if result.execution_mode != manifest.evaluation.execution_mode:
        problems.add(BLOCKER_INPUT_SCHEMA_INVALID)

    # ---- 重复与歧义身份 ----
    if _duplicate_case_ids(result):
        problems.add(BLOCKER_DUPLICATE_CASE_ID)
        problems.add(BLOCKER_CASE_SET_INCONSISTENT)
    if _ambiguous_assertions(result):
        problems.add(BLOCKER_ASSERTION_IDENTITY_AMBIGUOUS)

    # ---- 逐项重算并与存储值比对 ----
    cases = metrics.cases
    dataset_case_count = manifest.evaluation.dataset_case_count
    # §九 第 1 项：清单里的案例总数必须与指标里的**总案例数**一致——
    # 前者是数据集的权威计数，后者由它推出。
    _expect(mismatches, "cases.total_cases", cases.total_cases, dataset_case_count)
    _expect(mismatches, "cases.executed_cases", cases.executed_cases, len(result.cases))
    passed = sum(1 for case in result.cases if case.passed)
    _expect(mismatches, "cases.passed_cases", cases.passed_cases, passed)
    _expect(mismatches, "cases.failed_cases", cases.failed_cases, len(result.cases) - passed)
    _expect(
        mismatches,
        "cases.execution_error_cases",
        cases.execution_error_cases,
        sum(1 for case in result.cases if case.failure_kind == "execution_error"),
    )
    _expect(
        mismatches,
        "cases.assertion_failed_cases",
        cases.assertion_failed_cases,
        sum(1 for case in result.cases if case.failure_kind == "assertion_failure"),
    )
    # 比率的分子分母也要复核——模型只保证它与 value 自洽，管不了它是不是真的。
    _expect_ratio(
        mismatches, "cases.case_pass_rate", cases.case_pass_rate, passed, len(result.cases)
    )
    _expect_ratio(
        mismatches,
        "cases.execution_coverage",
        cases.execution_coverage,
        len(result.cases),
        dataset_case_count,
    )

    # ---- 类别 ----
    category_totals = _category_totals(result)
    if category_totals is None:
        notes.add("部分运行：类别 total / not_executed 无从由结构化结果独立复核")
    recomputed_categories: dict[str, int] = {}
    for case in result.cases:
        recomputed_categories[case.category] = recomputed_categories.get(case.category, 0) + 1

    # 🔴 已执行案例的类别必须是存储类别的**子集**，而不是**相等**。
    #
    # 存储的类别来自**数据集**：部分运行时，它会包含"一条都没跑"的类别
    # （``executed == 0``、``not_executed == 该类别全部``）。而重算的类别
    # 只能来自执行过的案例——那些类别在结果里**根本不存在**。
    #
    # 要求两者相等，会让**每一次部分运行**都被报成 ``input_metrics_mismatch``：
    # 那是**对合法输入的假阳性指控**，等于告诉使用者"你的指标文件坏了"，
    # 而真正的问题只是这次没跑完（现在由 ``partial_run`` 单独报告）。
    stored_categories = {item.category for item in metrics.categories}
    unaccounted = sorted(set(recomputed_categories) - stored_categories)
    if unaccounted:
        mismatches.add("categories")
    else:
        for item in metrics.categories:
            executed = recomputed_categories.get(item.category, 0)
            in_category = [case for case in result.cases if case.category == item.category]
            _expect(mismatches, f"categories.{item.category}.executed", item.executed, executed)
            passed_in = sum(1 for case in in_category if case.passed)
            _expect(mismatches, f"categories.{item.category}.passed", item.passed, passed_in)
            _expect(
                mismatches, f"categories.{item.category}.failed", item.failed, executed - passed_in
            )
            if category_totals is not None:
                _expect(
                    mismatches,
                    f"categories.{item.category}.total",
                    item.total,
                    category_totals[item.category],
                )
            _expect_ratio(
                mismatches,
                f"categories.{item.category}.pass_rate",
                item.pass_rate,
                passed_in,
                executed,
            )
            if item.total != executed + item.not_executed:
                mismatches.add(f"categories.{item.category}.total")

    # ---- 断言 ----
    stored_assertions = metrics.assertions
    required = [a for case in result.cases for a in case.assertions if a.mode == "required"]
    forbidden = [a for case in result.cases for a in case.assertions if a.mode == "forbidden"]
    for label, group, items in (
        ("overall", stored_assertions.overall, required + forbidden),
        ("required", stored_assertions.required, required),
        ("forbidden", stored_assertions.forbidden, forbidden),
    ):
        evaluated = [a for a in items if a.observation_status != "unobservable"]
        _expect(mismatches, f"assertions.{label}.total", group.total, len(items))
        _expect(mismatches, f"assertions.{label}.evaluated", group.evaluated, len(evaluated))
        _expect(
            mismatches,
            f"assertions.{label}.passed",
            group.passed,
            sum(1 for a in evaluated if a.passed),
        )
        _expect(
            mismatches,
            f"assertions.{label}.unobservable",
            group.unobservable,
            len(items) - len(evaluated),
        )
        _expect_ratio(
            mismatches,
            f"assertions.{label}.pass_rate",
            group.pass_rate,
            sum(1 for a in evaluated if a.passed),
            len(evaluated),
        )
        _expect_ratio(
            mismatches,
            f"assertions.{label}.observation_coverage",
            group.observation_coverage,
            len(evaluated),
            len(items),
        )

    by_name: dict[str, list[Any]] = {}
    for case in result.cases:
        for assertion in case.assertions:
            by_name.setdefault(assertion.name, []).append(assertion)
    if sorted(by_name) != sorted(named.name for named in metrics.assertion_names):
        mismatches.add("assertion_names")
    else:
        for named in metrics.assertion_names:
            items = by_name[named.name]
            unobservable = sum(1 for a in items if a.observation_status == "unobservable")
            evaluated_items = [a for a in items if a.observation_status != "unobservable"]
            _expect(mismatches, f"assertion_names.{named.name}.total", named.total, len(items))
            _expect(
                mismatches,
                f"assertion_names.{named.name}.required_total",
                named.required_total,
                sum(1 for a in items if a.mode == "required"),
            )
            _expect(
                mismatches,
                f"assertion_names.{named.name}.forbidden_total",
                named.forbidden_total,
                sum(1 for a in items if a.mode == "forbidden"),
            )
            _expect(
                mismatches,
                f"assertion_names.{named.name}.evaluated",
                named.evaluated,
                len(evaluated_items),
            )
            _expect(
                mismatches,
                f"assertion_names.{named.name}.passed",
                named.passed,
                sum(1 for a in evaluated_items if a.passed),
            )
            _expect(
                mismatches,
                f"assertion_names.{named.name}.unobservable",
                named.unobservable,
                unobservable,
            )
            _expect_ratio(
                mismatches,
                f"assertion_names.{named.name}.pass_rate",
                named.pass_rate,
                sum(1 for a in evaluated_items if a.passed),
                len(evaluated_items),
            )
            _expect_ratio(
                mismatches,
                f"assertion_names.{named.name}.observation_coverage",
                named.observation_coverage,
                len(evaluated_items),
                len(items),
            )

    # ---- 分布与失败索引 ----
    _expect(mismatches, "distributions", metrics.distributions, _recomputed_distributions(result))
    _expect(mismatches, "failures", metrics.failures, _recomputed_failure_index(result))

    if mismatches:
        # 🔴 存储的指标与重算的指标对不上：**整个输入都不可信**了。
        # 自动覆盖文件里的那份是绝对不行的——那等于用"重算的"替换掉
        # "被测系统当时记录的"，把一次数据损坏变成一次静默修正。
        problems.add(BLOCKER_INPUT_METRICS_MISMATCH)
    return tuple(sorted(problems)), tuple(sorted(notes)), tuple(sorted(mismatches))


def _category_totals(result: RunResult) -> dict[str, int] | None:
    """类别的**总数**（含未执行）。

    完整运行时等于各类别已执行数；部分运行时无从推出——返回 ``None``
    而不是猜一个数，因为"猜的数"会被当成"查过了"。
    """
    if not _complete_run(result):
        return None
    totals: dict[str, int] = {}
    for case in result.cases:
        totals[case.category] = totals.get(case.category, 0) + 1
    return totals


def _recomputed_distributions(result: RunResult) -> object:
    """由案例结果重算三项分布（口径与 S4 一致：只统计已执行的案例）。"""
    from ai_psi.evaluation.metrics import (
        NONE_KEY,
        DistributionMetrics,
    )

    states: dict[str, int] = {}
    depths: dict[str, int] = {}
    stop_reasons: dict[str, int] = {}
    for case in result.cases:
        observation = case.observation
        if observation is None:
            # 没有观测就没有分布项可记，**不填 __none__**。
            continue
        states[observation.state] = states.get(observation.state, 0) + 1
        depths[observation.depth] = depths.get(observation.depth, 0) + 1
        key = observation.stop_reason or NONE_KEY
        stop_reasons[key] = stop_reasons.get(key, 0) + 1
    return DistributionMetrics(
        final_state=_ordered_counts(states, [m.value for m in RoundState]),
        depth=_ordered_counts(depths, [m.value for m in CognitiveDepth]),
        stop_reason=_ordered_counts(stop_reasons, None),
    )


def _ordered_counts(counts: Mapping[str, int], preferred: Sequence[str] | None) -> dict[str, int]:
    """按正式顺序整理计数（口径与 S4 的 ``_ordered_distribution`` 一致）。"""
    return {key: counts[key] for key in _ordered_keys(counts, {}, preferred)}


def _recomputed_failure_index(result: RunResult) -> object:
    """由案例结果重算失败索引（口径与 S4 一致）。"""
    from ai_psi.evaluation.metrics import (
        FailedAssertionRef,
        FailureIndex,
    )

    failed = [case for case in result.cases if not case.passed]
    by_case: dict[str, tuple[FailedAssertionRef, ...]] = {}
    for case in result.cases:
        broken = tuple(
            FailedAssertionRef(
                name=assertion.name,
                expectation_kind=assertion.mode,
                observation_status=assertion.observation_status,
            )
            for assertion in case.assertions
            if not assertion.passed
        )
        if broken:
            by_case[case.case_id] = broken
    return FailureIndex(
        failed_case_ids=tuple(sorted(case.case_id for case in failed)),
        execution_error_case_ids=tuple(
            sorted(case.case_id for case in failed if case.failure_kind == "execution_error")
        ),
        assertion_failure_case_ids=tuple(
            sorted(case.case_id for case in failed if case.failure_kind == "assertion_failure")
        ),
        unobservable_case_ids=tuple(
            sorted(
                case.case_id
                for case in result.cases
                if any(a.observation_status == "unobservable" for a in case.assertions)
            )
        ),
        failed_assertions_by_case={case_id: by_case[case_id] for case_id in sorted(by_case)},
    )


# ---------------------------------------------------------------------------
# 身份与可比较性
# ---------------------------------------------------------------------------


def _identity(role: ComparisonRole, result: RunResult) -> ComparisonIdentity:
    """从一份结果里取出对比用的身份。

    ⚠️ 只有**稳定且非敏感**的字段：没有数据库名、没有 URL、没有凭据、
    没有环境变量集合。
    """
    manifest = result.manifest
    metrics = result.metrics
    if manifest is None or metrics is None:  # pragma: no cover - 加载器已保证
        msg = "结果缺少清单或指标"
        raise ComparisonInputError(msg)
    return ComparisonIdentity(
        role=role,
        commit_sha=manifest.code.commit_sha,
        package_version=manifest.code.package_version,
        working_tree_clean=manifest.code.working_tree_clean,
        case_schema_version=manifest.evaluation.case_schema_version,
        manifest_schema_version=manifest.manifest_schema_version,
        dataset_digest=manifest.evaluation.dataset_digest,
        dataset_case_count=manifest.evaluation.dataset_case_count,
        assertion_registry_digest=manifest.evaluation.assertion_registry_digest,
        manifest_execution_mode=manifest.evaluation.execution_mode,
        result_execution_mode=result.execution_mode,
        result_schema_version=result.schema_version,
        prompt_versions_digest=manifest.prompts.digest,
        provider_name=manifest.provider.provider_name,
        model_id=manifest.provider.model_id,
        provider_configuration_digest=manifest.provider.configuration_digest,
        network_allowed=manifest.provider.network_allowed,
        storage_backend=manifest.storage.backend,
        alembic_revision=manifest.storage.alembic_revision,
        python_version=manifest.runtime.python_version,
        metrics_schema_version=metrics.metrics_schema_version,
        metrics_definition_digest=metrics.metrics_definition_digest,
    )


def _python_major_minor(version: str) -> tuple[int, int] | None:
    """把 ``"3.13.2"`` 解析成 ``(3, 13)``；解析不了返回 ``None``。

    🔴 **结构化解析，不按字符串前缀比较**：``"3.13"`` 与 ``"3.130"``
    的前缀关系会骗人，而补丁版本本来就允许不同。
    """
    parts = version.split(".")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class _Analysis:
    """内部结论：可比较性 + 集合差异 + 未复核说明。"""

    eligibility: ComparisonEligibility
    added_case_ids: tuple[str, ...]
    removed_case_ids: tuple[str, ...]
    added_assertion_keys: tuple[str, ...]
    removed_assertion_keys: tuple[str, ...]
    integrity_notes: tuple[str, ...]
    integrity_mismatches: tuple[str, ...]


def _analyse(baseline: RunResult, candidate: RunResult) -> _Analysis:
    """判定两份结果能不能比，并算出它们的集合差异。"""
    allowed: set[str] = set()
    blocked: set[str] = set()
    notes: set[str] = set()
    mismatches: set[str] = set()

    # ---- 1. 复用 S3：**原样**接收它的每一个原因码 ----
    s3 = compare_manifests(baseline.manifest, candidate.manifest)
    for reason in s3.reasons:
        if reason in ALLOWED_DIFFERENCE_CODES:
            allowed.add(reason)
        else:
            blocked.add(reason)

    # ---- 2. 各自的自洽性 ----
    for result in (baseline, candidate):
        problems, result_notes, result_mismatches = _integrity_problems(result)
        blocked.update(problems)
        notes.update(result_notes)
        mismatches.update(result_mismatches)

    # ---- 3. S3 不比较、由 S5 补上的身份项 ----
    left = _identity(ComparisonRole.BASELINE, baseline)
    right = _identity(ComparisonRole.CANDIDATE, candidate)

    if left.dataset_case_count != right.dataset_case_count:
        blocked.add(BLOCKER_DATASET_CASE_COUNT_DIFFERS)
    if left.package_version != right.package_version:
        # ⚠️ 默认阻塞。审计未确认"包版本与代码 SHA 同步变化"，
        # 因此**不自行放宽**——那需要一份正式的版本契约，现在没有。
        blocked.add(BLOCKER_PACKAGE_VERSION_DIFFERS)
    if left.metrics_schema_version != right.metrics_schema_version:
        blocked.add(BLOCKER_METRICS_SCHEMA_DIFFERS)
    if left.metrics_definition_digest != right.metrics_definition_digest:
        blocked.add(BLOCKER_METRICS_DEFINITION_DIFFERS)
    if left.network_allowed != right.network_allowed:
        blocked.add(BLOCKER_NETWORK_POLICY_DIFFERS)

    left_python = _python_major_minor(left.python_version)
    right_python = _python_major_minor(right.python_version)
    if left_python is None or right_python is None:
        blocked.add(BLOCKER_IDENTITY_UNAVAILABLE)
    elif left_python != right_python:
        # 🔴 只看 major/minor：补丁版本是运行环境的属性，换个补丁号
        # 不该让两份结果变成不可比。
        blocked.add(BLOCKER_PYTHON_VERSION_DIFFERS)

    # ---- 4. 案例集合（§十一）----
    baseline_cases = {case.case_id: case for case in baseline.cases}
    candidate_cases = {case.case_id: case for case in candidate.cases}
    added_case_ids = tuple(sorted(set(candidate_cases) - set(baseline_cases)))
    removed_case_ids = tuple(sorted(set(baseline_cases) - set(candidate_cases)))
    if added_case_ids or removed_case_ids:
        # 🔴 同一数据集下案例多了或少了，是**完整性错误**，不是性能变化。
        blocked.add(BLOCKER_CASE_SET_INCONSISTENT)

    category_mismatch = sorted(
        {case.category for case in baseline_cases.values()}
        ^ {case.category for case in candidate_cases.values()}
    )
    if category_mismatch:
        blocked.add(BLOCKER_CASE_SET_INCONSISTENT)

    for case_id in sorted(set(baseline_cases) & set(candidate_cases)):
        if baseline_cases[case_id].category != candidate_cases[case_id].category:
            blocked.add(BLOCKER_CASE_SET_INCONSISTENT)
            break

    # ---- 5. 断言集合（§十三/§十四）----
    baseline_assertions = _assertion_entries(baseline)
    candidate_assertions = _assertion_entries(candidate)
    added_assertions = tuple(sorted(set(candidate_assertions) - set(baseline_assertions)))
    removed_assertions = tuple(sorted(set(baseline_assertions) - set(candidate_assertions)))
    if added_assertions or removed_assertions:
        blocked.add(BLOCKER_ASSERTION_SET_INCONSISTENT)
    for key in set(baseline_assertions) & set(candidate_assertions):
        # 同一个键（同案例、同声明位置）却指向不同的断言，说明两份结果
        # 不是在同一个案例契约下产出的——这**不是**"断言换了"。
        if baseline_assertions[key] != candidate_assertions[key]:
            blocked.add(BLOCKER_ASSERTION_IDENTITY_AMBIGUOUS)
            break

    eligible = not blocked
    return _Analysis(
        eligibility=ComparisonEligibility(
            manifest_identity_equal=s3.comparable,
            comparison_eligible=eligible,
            allowed_differences=tuple(sorted(allowed)),
            blockers=tuple(sorted(blocked)),
        ),
        added_case_ids=added_case_ids,
        removed_case_ids=removed_case_ids,
        added_assertion_keys=added_assertions,
        removed_assertion_keys=removed_assertions,
        integrity_notes=tuple(sorted(notes)),
        integrity_mismatches=tuple(sorted(mismatches)),
    )


def evaluate_comparison_eligibility(
    baseline: RunResult, candidate: RunResult
) -> ComparisonEligibility:
    """判定两份结果具备做受控差异分析的条件。

    🔴 与 S3 的 ``comparable`` 的区别见模块文档。本函数**不修改**
    S3 的任何语义：它调用 :func:`compare_manifests` 并逐字保留其原因码，
    只把 :data:`ALLOWED_DIFFERENCE_CODES` 里的那些从"阻塞"改判为"允许"。

    Args:
        baseline: 基线结果。
        candidate: 候选结果。

    Returns:
        可比较性结论（含 ``manifest_identity_equal``、允许差异与阻塞项）。
    """
    return _analyse(baseline, candidate).eligibility


# ---------------------------------------------------------------------------
# 差异计算
# ---------------------------------------------------------------------------


def _case_metrics_delta(
    baseline: EvaluationMetrics, candidate: EvaluationMetrics
) -> CaseMetricsDelta:
    left = baseline.cases
    right = candidate.cases
    return CaseMetricsDelta(
        total_cases=_count_delta(left.total_cases, right.total_cases),
        executed_cases=_count_delta(left.executed_cases, right.executed_cases),
        passed_cases=_count_delta(left.passed_cases, right.passed_cases),
        failed_cases=_count_delta(left.failed_cases, right.failed_cases),
        not_executed_cases=_count_delta(left.not_executed_cases, right.not_executed_cases),
        execution_error_cases=_count_delta(left.execution_error_cases, right.execution_error_cases),
        assertion_failed_cases=_count_delta(
            left.assertion_failed_cases, right.assertion_failed_cases
        ),
        case_pass_rate=_ratio_delta_of(left.case_pass_rate, right.case_pass_rate),
        execution_coverage=_ratio_delta_of(left.execution_coverage, right.execution_coverage),
    )


def _assertion_group_delta(left: Any, right: Any) -> AssertionGroupDelta:
    return AssertionGroupDelta(
        total=_count_delta(left.total, right.total),
        evaluated=_count_delta(left.evaluated, right.evaluated),
        passed=_count_delta(left.passed, right.passed),
        failed=_count_delta(left.failed, right.failed),
        unobservable=_count_delta(left.unobservable, right.unobservable),
        pass_rate=_ratio_delta_of(left.pass_rate, right.pass_rate),
        observation_coverage=_ratio_delta_of(left.observation_coverage, right.observation_coverage),
    )


def _metrics_comparison(
    baseline: EvaluationMetrics, candidate: EvaluationMetrics
) -> MetricsComparison:
    """构造指标层的完整差异。

    ⚠️ 类别与断言名按**名字**匹配（两侧集合已在可比较性判定里确认一致），
    顺序按名字排序。
    """
    left_categories = {item.category: item for item in baseline.categories}
    right_categories = {item.category: item for item in candidate.categories}
    categories = tuple(
        CategoryDelta(
            category=name,
            total=_count_delta(left_categories[name].total, right_categories[name].total),
            executed=_count_delta(left_categories[name].executed, right_categories[name].executed),
            passed=_count_delta(left_categories[name].passed, right_categories[name].passed),
            failed=_count_delta(left_categories[name].failed, right_categories[name].failed),
            not_executed=_count_delta(
                left_categories[name].not_executed, right_categories[name].not_executed
            ),
            pass_rate=_ratio_delta_of(
                left_categories[name].pass_rate, right_categories[name].pass_rate
            ),
        )
        for name in sorted(set(left_categories) | set(right_categories))
    )

    left_names = {item.name: item for item in baseline.assertion_names}
    right_names = {item.name: item for item in candidate.assertion_names}
    assertion_names = tuple(
        AssertionNameDelta(
            name=name,
            total=_count_delta(left_names[name].total, right_names[name].total),
            required_total=_count_delta(
                left_names[name].required_total, right_names[name].required_total
            ),
            forbidden_total=_count_delta(
                left_names[name].forbidden_total, right_names[name].forbidden_total
            ),
            evaluated=_count_delta(left_names[name].evaluated, right_names[name].evaluated),
            passed=_count_delta(left_names[name].passed, right_names[name].passed),
            failed=_count_delta(left_names[name].failed, right_names[name].failed),
            unobservable=_count_delta(
                left_names[name].unobservable, right_names[name].unobservable
            ),
            pass_rate=_ratio_delta_of(left_names[name].pass_rate, right_names[name].pass_rate),
            observation_coverage=_ratio_delta_of(
                left_names[name].observation_coverage, right_names[name].observation_coverage
            ),
        )
        for name in sorted(set(left_names) | set(right_names))
    )

    return MetricsComparison(
        cases=_case_metrics_delta(baseline, candidate),
        categories=categories,
        assertions_overall=_assertion_group_delta(
            baseline.assertions.overall, candidate.assertions.overall
        ),
        assertions_required=_assertion_group_delta(
            baseline.assertions.required, candidate.assertions.required
        ),
        assertions_forbidden=_assertion_group_delta(
            baseline.assertions.forbidden, candidate.assertions.forbidden
        ),
        assertion_names=assertion_names,
    )


def _distribution_deltas(
    baseline: EvaluationMetrics, candidate: EvaluationMetrics
) -> DistributionDelta:
    """三项分布的逐键差异（键取并集，缺失按 0）。"""
    left = baseline.distributions
    right = candidate.distributions
    return DistributionDelta(
        final_state=_map_deltas(left.final_state, right.final_state, [m.value for m in RoundState]),
        depth=_map_deltas(left.depth, right.depth, [m.value for m in CognitiveDepth]),
        # 停止原因是自由字符串（不是枚举），因此按字典序稳定排序。
        stop_reason=_map_deltas(left.stop_reason, right.stop_reason, None),
    )


def _map_deltas(
    left: Mapping[str, int], right: Mapping[str, int], preferred: Sequence[str] | None
) -> dict[str, CountDelta]:
    return {
        key: _count_delta(left.get(key, 0), right.get(key, 0))
        for key in _ordered_keys(left, right, preferred)
    }


def _assertion_transitions(
    baseline: RunResult, candidate: RunResult
) -> tuple[tuple[AssertionTransition, ...], tuple[str, ...], dict[str, tuple[str, ...]]]:
    """逐条断言比较状态。

    Returns:
        ``(转换列表, 发生了变化的键, 案例 → 该案例变化的键)``。
    """
    left_entries = _assertion_entries(baseline)
    right_entries = _assertion_entries(candidate)
    shared = sorted(set(left_entries) & set(right_entries))

    left_status = _assertion_states(baseline)
    right_status = _assertion_states(candidate)

    transitions: list[AssertionTransition] = []
    changed: list[str] = []
    by_case: dict[str, list[str]] = {}
    for key in shared:
        before = left_status[key]
        after = right_status[key]
        if before != after:
            changed.append(key)
            by_case.setdefault(key.split("#", 1)[0], []).append(key)
        mode, name, _ = left_entries[key]
        case_id = key.split("#", 1)[0]
        transitions.append(
            AssertionTransition(
                case_id=case_id,
                assertion_key=key,
                assertion_name=name,
                expectation_kind=mode,
                baseline_status=before,
                candidate_status=after,
                transition=_ASSERTION_TRANSITIONS[(before, after)],
            )
        )
    ordered = tuple(sorted(transitions, key=lambda item: (item.case_id, item.assertion_key)))
    return (
        ordered,
        tuple(changed),
        {case_id: tuple(sorted(keys)) for case_id, keys in by_case.items()},
    )


def _assertion_states(result: RunResult) -> dict[str, AssertionStatus]:
    """断言键 → 状态。**只看结构化字段。**"""
    states: dict[str, AssertionStatus] = {}
    for case in result.cases:
        for index, assertion in enumerate(case.assertions):
            key = assertion_key(case.case_id, index, mode=assertion.mode, name=assertion.name)
            states[key] = _status_of(assertion.observation_status, assertion.passed)
    return states


def _case_transitions(
    baseline: RunResult, candidate: RunResult, changed_by_case: Mapping[str, tuple[str, ...]]
) -> tuple[CaseTransition, ...]:
    baseline_cases = {case.case_id: case for case in baseline.cases}
    candidate_cases = {case.case_id: case for case in candidate.cases}
    transitions: list[CaseTransition] = []
    for case_id in sorted(set(baseline_cases) & set(candidate_cases)):
        before = baseline_cases[case_id]
        after = candidate_cases[case_id]
        transitions.append(
            CaseTransition(
                case_id=case_id,
                category=before.category,
                baseline_passed=before.passed,
                candidate_passed=after.passed,
                transition=_case_transition_type(before.passed, after.passed),
                baseline_failure_kind=before.failure_kind,
                candidate_failure_kind=after.failure_kind,
                baseline_final_state=_state_of(before),
                candidate_final_state=_state_of(after),
                baseline_depth=_depth_of(before),
                candidate_depth=_depth_of(after),
                baseline_stop_reason=_stop_reason_of(before),
                candidate_stop_reason=_stop_reason_of(after),
                changed_assertion_keys=changed_by_case.get(case_id, ()),
            )
        )
    return tuple(transitions)


def _state_of(case: Any) -> str | None:
    return None if case.observation is None else case.observation.state


def _depth_of(case: Any) -> str | None:
    return None if case.observation is None else case.observation.depth


def _stop_reason_of(case: Any) -> str | None:
    return None if case.observation is None else case.observation.stop_reason


def _case_transition_summary(transitions: Sequence[CaseTransition]) -> CaseTransitionSummary:
    return CaseTransitionSummary(
        total_cases=len(transitions),
        unchanged_pass_count=sum(
            1 for item in transitions if item.transition is CaseTransitionType.UNCHANGED_PASS
        ),
        regression_transition_count=sum(
            1 for item in transitions if item.transition is CaseTransitionType.REGRESSION
        ),
        improvement_transition_count=sum(
            1 for item in transitions if item.transition is CaseTransitionType.IMPROVEMENT
        ),
        unchanged_fail_count=sum(
            1 for item in transitions if item.transition is CaseTransitionType.UNCHANGED_FAIL
        ),
    )


def _assertion_transition_summary(
    transitions: Sequence[AssertionTransition],
) -> AssertionTransitionSummary:
    return AssertionTransitionSummary(
        total_assertions=len(transitions),
        unchanged_count=sum(
            1 for item in transitions if item.transition is AssertionTransitionType.UNCHANGED
        ),
        regression_transition_count=sum(
            1 for item in transitions if item.transition is AssertionTransitionType.REGRESSION
        ),
        improvement_transition_count=sum(
            1 for item in transitions if item.transition is AssertionTransitionType.IMPROVEMENT
        ),
        changed_unresolved_count=sum(
            1
            for item in transitions
            if item.transition is AssertionTransitionType.CHANGED_UNRESOLVED
        ),
    )


def _failure_index_delta(
    baseline: EvaluationMetrics, candidate: EvaluationMetrics
) -> FailureIndexDelta:
    """失败索引的集合差异。

    ⚠️ **不按 ``failure_reason`` 文本分类**——"新出现失败"看的是
    ``passed``，"变成执行错误"看的是 ``failure_kind``，
    "新出现不可观测"看的是 ``observation_status``。
    """
    left = baseline.failures
    right = candidate.failures
    left_failed = set(left.failed_case_ids)
    right_failed = set(right.failed_case_ids)
    left_errors = set(left.execution_error_case_ids)
    right_errors = set(right.execution_error_case_ids)
    left_unobservable = set(left.unobservable_case_ids)
    right_unobservable = set(right.unobservable_case_ids)

    return FailureIndexDelta(
        newly_failed_case_ids=tuple(sorted(right_failed - left_failed)),
        resolved_failed_case_ids=tuple(sorted(left_failed - right_failed)),
        persistently_failed_case_ids=tuple(sorted(left_failed & right_failed)),
        newly_execution_error_case_ids=tuple(sorted(right_errors - left_errors)),
        resolved_execution_error_case_ids=tuple(sorted(left_errors - right_errors)),
        newly_unobservable_case_ids=tuple(sorted(right_unobservable - left_unobservable)),
        resolved_unobservable_case_ids=tuple(sorted(left_unobservable - right_unobservable)),
    )


def compare_run_results(baseline: RunResult, candidate: RunResult) -> EvaluationComparison:
    """比较两份运行结果，产出确定性、机器可读的结构化差异。

    🔴 **方向是 ``candidate - baseline``。** 角色由参数位置**显式**给出：
    第一个参数永远是基线，第二个永远是候选。**不自动交换。**

    Args:
        baseline: 基线结果。
        candidate: 候选结果。

    Returns:
        对比产物。不可比较时，五组差异结构全部为 ``None``——
        该约束由 :class:`EvaluationComparison` 在构造时强制。
    """
    analysis = _analyse(baseline, candidate)
    baseline_identity = _identity(ComparisonRole.BASELINE, baseline)
    candidate_identity = _identity(ComparisonRole.CANDIDATE, candidate)

    kwargs: dict[str, Any] = {
        "comparison_definition_digest": comparison_definition_digest(),
        "baseline": baseline_identity,
        "candidate": candidate_identity,
        "manifest_identity_equal": analysis.eligibility.manifest_identity_equal,
        "comparison_eligible": analysis.eligibility.comparison_eligible,
        "allowed_differences": analysis.eligibility.allowed_differences,
        "blockers": analysis.eligibility.blockers,
        "integrity_notes": analysis.integrity_notes,
        "integrity_mismatches": analysis.integrity_mismatches,
        "added_case_ids": analysis.added_case_ids,
        "removed_case_ids": analysis.removed_case_ids,
        "added_assertion_keys": analysis.added_assertion_keys,
        "removed_assertion_keys": analysis.removed_assertion_keys,
    }

    if not analysis.eligibility.comparison_eligible:
        # 🔴 不可比较：**一个数字都不出**。身份与阻塞原因照常输出——
        # 那是"为什么不能比"的证据，不是差异结论。
        return EvaluationComparison(
            metrics_comparison=None,
            case_transitions=None,
            assertion_transitions=None,
            distribution_deltas=None,
            failure_index_delta=None,
            # 汇总也是差异的一部分，同样必须为空——"没有明细却有一份汇总"
            # 正是"不可比较时输出了部分数字"最容易溜进来的形态。
            case_transition_summary=None,
            assertion_transition_summary=None,
            **kwargs,
        )

    baseline_metrics = baseline.metrics
    candidate_metrics = candidate.metrics
    if baseline_metrics is None or candidate_metrics is None:  # pragma: no cover - 加载器已保证
        msg = "结果缺少指标"
        raise ComparisonInputError(msg)

    assertion_transitions, _changed, changed_by_case = _assertion_transitions(baseline, candidate)
    case_transitions = _case_transitions(baseline, candidate, changed_by_case)

    return EvaluationComparison(
        metrics_comparison=_metrics_comparison(baseline_metrics, candidate_metrics),
        case_transitions=case_transitions,
        assertion_transitions=assertion_transitions,
        distribution_deltas=_distribution_deltas(baseline_metrics, candidate_metrics),
        failure_index_delta=_failure_index_delta(baseline_metrics, candidate_metrics),
        case_transition_summary=_case_transition_summary(case_transitions),
        assertion_transition_summary=_assertion_transition_summary(assertion_transitions),
        **kwargs,
    )


def write_comparison(comparison: EvaluationComparison, path: Path) -> None:
    """把对比结果写到磁盘。

    与 S1a—S4 的报告用**同一个** :func:`~ai_psi.evaluation.serialization.dumps`：
    UTF-8、键排序、缩进固定、结尾恰好一个换行。同样两份输入跑两次，
    产物因此逐字节一致——这是 :func:`compare_run_results` 之外、
    落在文件层面的那条确定性承诺。

    ⚠️ 产物里没有时间戳、没有随机 UUID、没有绝对路径、没有数据库名或 URL、
    没有 secret、没有 ``response_text``、没有 Prompt 正文、没有异常堆栈。
    这不是靠"写的时候小心"，而是靠 :class:`EvaluationComparison` 里
    **根本没有**承载这些内容的字段。

    Args:
        comparison: 对比产物。
        path: 输出路径。父目录不存在时会被创建。

    Raises:
        OSError: 写盘失败。**不吞掉**——"看起来跑完了却没有产物"是最坏的结果。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = comparison.model_dump(mode="json")
    # newline="\n"：Windows 上也不写 CRLF，否则跨平台比较会先败在行尾上。
    path.write_text(dumps(payload), encoding="utf-8", newline="\n")
