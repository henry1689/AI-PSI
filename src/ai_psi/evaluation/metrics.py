"""确定性指标层：一次 Golden Eval 运行产生了什么可解释的聚合结果（阶段 7 · S4）。

## 这个模块不做什么

🔴 **它不评价系统好不好。** 它只回答"这次运行里发生了什么"：
跑了多少条、通过多少、失败是哪一类、分布是什么样。

* 不比较两次运行；
* 不判断回归或改进；
* 不设发布阈值；
* 不计算 attribution 漏报率（R46/R58）。

"10 个案例全过"**不是**"系统质量达标"——那需要 50+ 个案例、
真实 Provider、以及有标签的评测集。本层只保证**统计是对的**。

## 三条硬规矩

1. **每个比率都带分子与分母。** 只输出 ``80%`` 的报告没法回答
   "80% 的什么"，而"80% 的案例通过"与"80% 的断言通过"是两回事。
2. **分母为 0 时值是 ``null``，不是 ``0``。** "一条都没跑"与"跑了但全错"
   在报告里必须长得不一样——把 0/0 写成 0% 是最常见的一种假指标。
3. **分类来自结构化字段，不来自文本。** 失败是"没跑起来"还是"跑起来
   但对不上"，由 ``CaseResult.failure_kind`` 决定；断言的"不可观测"
   由 ``AssertionResult.observation_status`` 决定。**没有一处**
   ``in`` 字符串判定。

## 比率的精度

固定 **6 位小数**，``ROUND_HALF_UP``，用标准库 :class:`decimal.Decimal`
计算。不用二进制浮点：``0.1 + 0.2`` 那种误差会在"两次运行必须逐字节
一致"这条要求上直接暴露出来。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_psi.domain.enums import CognitiveDepth, RoundState
from ai_psi.evaluation.assertions import AssertionResult
from ai_psi.evaluation.loader import GoldenDataset

if TYPE_CHECKING:
    # ⚠️ **只在类型检查时导入**：``runner`` 需要在 ``RunResult`` 上挂
    # ``EvaluationMetrics``，因此它在运行时导入本模块。这里若也运行时导入
    # 就成了循环。函数体里只读入参的属性，运行期不需要这两个名字。
    from ai_psi.evaluation.runner import RunResult

__all__ = [
    "METRICS_SCHEMA_VERSION",
    "RATIO_PRECISION",
    "AssertionGroupMetrics",
    "AssertionMetrics",
    "AssertionNameMetrics",
    "CaseMetrics",
    "CategoryMetrics",
    "DistributionMetrics",
    "EvaluationMetrics",
    "FailedAssertionRef",
    "FailureIndex",
    "RatioMetric",
    "compute_metrics",
    "metrics_definition_digest",
]

#: 指标契约自身的版本。**改公式就要改它**（或改定义摘要）。
METRICS_SCHEMA_VERSION: Final[int] = 1

#: 比率固定保留的小数位数。写进契约，也进定义摘要。
RATIO_PRECISION: Final[int] = 6

#: 舍入规则。明确写出来是因为"四舍五入"在二进制浮点下根本不是一回事。
_ROUNDING: Final[str] = "ROUND_HALF_UP"

_QUANTUM: Final[Decimal] = Decimal(1).scaleb(-RATIO_PRECISION)

#: 空值在分布里的键。用 ``__none__`` 而不是 ``null``：
#: JSON 对象的键必须是字符串，"没有停止原因"需要一个**明确**的名字，
#: 而不是一个恰好为空的字符串。
NONE_KEY: Final[str] = "__none__"

#: 定义摘要覆盖的内容。
#:
#: 🔴 它回答的是"这些数字是**按什么规则**算出来的"。只写一个
#: ``"version": 1`` 是不够的——那没法告诉别人分母取的是哪个口径。
METRICS_DEFINITION: Final[dict[str, object]] = {
    "assertion_pass_rate_denominator": "evaluated_assertions",
    "case_pass_rate_denominator": "executed_cases",
    "execution_coverage_denominator": "total_cases",
    "execution_error_vs_assertion_failure": "互斥且完备（failure_kind）",
    "observation_coverage_denominator": "total_assertions",
    "partial_run_policy": "not_executed_cases = total_cases - executed_cases",
    "ratio_precision": RATIO_PRECISION,
    "rounding": _ROUNDING,
    "schema_version": METRICS_SCHEMA_VERSION,
    "unobservable_classification": "observation_status == 'unobservable'",
    "zero_denominator": "value = null（绝不写成 0）",
}


def metrics_definition_digest() -> str:
    """指标定义的语义摘要。

    🔴 **改公式必须让它变**：改了分母口径却沿用旧摘要，等于宣称
    "这两份数字可以放在一起看"，而它们不是。

    Returns:
        形如 ``sha256:<hex>`` 的摘要。
    """
    payload = json.dumps(
        METRICS_DEFINITION, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _ratio(numerator: int, denominator: int) -> str | None:
    """把分数格式化成固定精度的十进制字符串；分母为 0 时返回 ``None``。

    ⚠️ 用 :class:`~decimal.Decimal` 而不是 ``numerator / denominator``：
    后者是二进制浮点，``1/3`` 会得到一长串 3 后面跟着噪声，
    而"两次运行逐字节一致"要求这里**没有任何**平台相关的抖动。
    """
    if denominator == 0:
        return None
    value = (Decimal(numerator) / Decimal(denominator)).quantize(_QUANTUM, rounding=ROUND_HALF_UP)
    return f"{value:.{RATIO_PRECISION}f}"


class RatioMetric(BaseModel):
    """一个比率：**分子、分母、值**三者齐备。

    🔴 只给百分比是不够的。``80%`` 无法回答"80% 的什么"，
    而"80% 的案例"与"80% 的断言"是完全不同的两个结论。
    保留分子分母还让"这个数字是从哪些计数算出来的"可被复算。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    #: 固定 6 位小数的字符串；分母为 0 时是 ``None``（**不是** ``"0.000000"``）。
    value: str | None

    @model_validator(mode="after")
    def _check(self) -> Self:
        """构造时校验：分子不得大于分母，且值必须与分数一致。

        ⚠️ 校验值而不只是校验范围：一份"分子分母都对、值算错了"的结果
        比一份明显越界的结果更难发现。
        """
        if self.numerator > self.denominator:
            msg = f"分子 {self.numerator} 大于分母 {self.denominator}——比率不可能超过 1"
            raise ValueError(msg)
        expected = _ratio(self.numerator, self.denominator)
        if self.value != expected:
            msg = (
                f"值与分数不一致：{self.numerator}/{self.denominator} 应为 {expected!r}，"
                f"收到 {self.value!r}"
            )
            raise ValueError(msg)
        return self


def _ratio_metric(numerator: int, denominator: int) -> RatioMetric:
    return RatioMetric(
        numerator=numerator, denominator=denominator, value=_ratio(numerator, denominator)
    )


class CaseMetrics(BaseModel):
    """案例层面的计数与比率。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: 数据集**加载成功**后的案例总数（含未执行的）。
    total_cases: int = Field(ge=0)
    #: 真的进入执行并产出了 ``CaseResult`` 的数量。
    executed_cases: int = Field(ge=0)
    passed_cases: int = Field(ge=0)
    failed_cases: int = Field(ge=0)
    #: 加载成功但因运行级中止而没进入执行的。
    #: 🔴 它们**不是**失败案例——把它们记成失败会低估通过率。
    not_executed_cases: int = Field(ge=0)
    #: 失败中属于"没跑起来"的（应用异常 / HTTP 错误 / 存储错误 / 无观测）。
    execution_error_cases: int = Field(ge=0)
    #: 失败中属于"跑起来了但断言没过"的。
    assertion_failed_cases: int = Field(ge=0)
    #: ``passed_cases / executed_cases``。
    case_pass_rate: RatioMetric
    #: ``executed_cases / total_cases``——"这次运行覆盖了多少数据集"。
    execution_coverage: RatioMetric

    @model_validator(mode="after")
    def _check(self) -> Self:
        """构造时校验互斥与完备性。

        这些不变量是**汇总值可信**的全部理由：一个 ``passed + failed``
        对不上 ``executed`` 的报告，无论看起来多合理都是错的。
        """
        if self.passed_cases + self.failed_cases != self.executed_cases:
            msg = (
                f"passed({self.passed_cases}) + failed({self.failed_cases}) "
                f"!= executed({self.executed_cases})"
            )
            raise ValueError(msg)
        if self.executed_cases + self.not_executed_cases != self.total_cases:
            msg = (
                f"executed({self.executed_cases}) + not_executed({self.not_executed_cases}) "
                f"!= total({self.total_cases})"
            )
            raise ValueError(msg)
        # 🔴 两种失败互斥且完备——由 ``CaseResult.failure_kind`` 保证。
        if self.execution_error_cases + self.assertion_failed_cases != self.failed_cases:
            msg = (
                f"execution_error({self.execution_error_cases}) + "
                f"assertion_failed({self.assertion_failed_cases}) != failed({self.failed_cases})；"
                "每个失败案例必须恰好属于其中一类"
            )
            raise ValueError(msg)
        return self


class CategoryMetrics(BaseModel):
    """一个类别下的案例计数与通过率。

    ⚠️ **它不是权重。** 某个类别案例多，不代表系统在该类别上更重要；
    本切片只有 10 个案例，任何"某类别能力稳定"的说法都缺乏依据。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: str
    total: int = Field(ge=0)
    executed: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    not_executed: int = Field(ge=0)
    pass_rate: RatioMetric

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.passed + self.failed != self.executed:
            msg = f"类别 {self.category!r}：passed + failed != executed"
            raise ValueError(msg)
        if self.executed + self.not_executed != self.total:
            msg = f"类别 {self.category!r}：executed + not_executed != total"
            raise ValueError(msg)
        return self


class AssertionGroupMetrics(BaseModel):
    """一组断言（全部 / required / forbidden）的计数与比率。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: 案例里**声明**的断言数量。
    total: int = Field(ge=0)
    #: 真的得到了可观测判定结果的数量。
    evaluated: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    #: 🔴 观测取不到的数量。**与 failed 分开**：没读到与读到了但对不上
    #: 是两回事，混在一起会让"系统在这个断言上表现如何"无从判断。
    unobservable: int = Field(ge=0)
    #: ``passed / evaluated``——**分母是 evaluated，不是 total**。
    #: 用 total 会把"没观测到"算成"失败"，那是两种不同的坏。
    pass_rate: RatioMetric
    #: ``evaluated / total``——"有多少断言真的被观测到了"。
    observation_coverage: RatioMetric

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.passed + self.failed != self.evaluated:
            msg = f"passed({self.passed}) + failed({self.failed}) != evaluated({self.evaluated})"
            raise ValueError(msg)
        if self.evaluated + self.unobservable != self.total:
            msg = (
                f"evaluated({self.evaluated}) + unobservable({self.unobservable}) "
                f"!= total({self.total})"
            )
            raise ValueError(msg)
        return self


class AssertionMetrics(BaseModel):
    """断言的总体统计，按模式拆分。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    overall: AssertionGroupMetrics
    required: AssertionGroupMetrics
    forbidden: AssertionGroupMetrics

    @model_validator(mode="after")
    def _check(self) -> Self:
        """required + forbidden 必须恰好等于 overall。

        三者对不上，说明有一类断言在统计里丢了或被算了两次。
        """
        for field in ("total", "evaluated", "passed", "failed", "unobservable"):
            parts = getattr(self.required, field) + getattr(self.forbidden, field)
            whole = getattr(self.overall, field)
            if parts != whole:
                msg = f"required + forbidden 的 {field} 是 {parts}，总体却是 {whole}"
                raise ValueError(msg)
        return self


class AssertionNameMetrics(BaseModel):
    """单个断言名下的统计。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    total: int = Field(ge=0)
    #: 同一个断言名可以同时以 required 与 forbidden 出现，必须分开记。
    required_total: int = Field(ge=0)
    forbidden_total: int = Field(ge=0)
    evaluated: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    unobservable: int = Field(ge=0)
    pass_rate: RatioMetric
    observation_coverage: RatioMetric

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.required_total + self.forbidden_total != self.total:
            msg = f"断言 {self.name!r}：required + forbidden != total"
            raise ValueError(msg)
        if self.passed + self.failed != self.evaluated:
            msg = f"断言 {self.name!r}：passed + failed != evaluated"
            raise ValueError(msg)
        if self.evaluated + self.unobservable != self.total:
            msg = f"断言 {self.name!r}：evaluated + unobservable != total"
            raise ValueError(msg)
        return self


class FailedAssertionRef(BaseModel):
    """失败索引里对一条断言的**引用**。

    🔴 只放稳定分类，**不放** ``detail``、``observed`` 原文或任何
    可能含路径与措辞的字段——索引的用途是"去哪查"，不是"把报告再抄一遍"。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    expectation_kind: str = Field(pattern="^(required|forbidden)$")
    observation_status: str = Field(pattern="^(observed|unobservable)$")


class FailureIndex(BaseModel):
    """结构化失败索引：便于定位，而不是输出一份报告。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    failed_case_ids: tuple[str, ...]
    execution_error_case_ids: tuple[str, ...]
    assertion_failure_case_ids: tuple[str, ...]
    #: 有断言**不可观测**的案例。⚠️ 这可能包含最终通过了的案例——
    #: 一条 ``forbidden`` 断言不可观测时，它判为不通过，案例会失败；
    #: 但这个集合表达的是"这里没观测到"，与"哪里错了"是两回事。
    unobservable_case_ids: tuple[str, ...]
    failed_assertions_by_case: dict[str, tuple[FailedAssertionRef, ...]]


class DistributionMetrics(BaseModel):
    """已执行案例的分布。

    🔴 **只统计已执行的案例**：没跑起来的案例没有终态、没有深度、
    没有停止原因。把它们的 ``None`` 计进分布会让"没跑"看起来像
    "跑出了一个空状态"。

    键的顺序：枚举类按**正式枚举顺序**（``d0`` 在 ``d1`` 前面），
    其余按字典序，空值统一用 :data:`NONE_KEY`。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    final_state: dict[str, int]
    depth: dict[str, int]
    stop_reason: dict[str, int]


class EvaluationMetrics(BaseModel):
    """一次运行的完整指标。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    metrics_schema_version: int = METRICS_SCHEMA_VERSION
    #: 指标定义的语义摘要（见 :func:`metrics_definition_digest`）。
    metrics_definition_digest: str
    cases: CaseMetrics
    #: 按类别，**只输出数据集中实际出现的类别**，按类别名排序。
    categories: tuple[CategoryMetrics, ...]
    assertions: AssertionMetrics
    #: 只输出**实际出现过**的断言名，按名字排序。
    assertion_names: tuple[AssertionNameMetrics, ...]
    distributions: DistributionMetrics
    failures: FailureIndex

    @model_validator(mode="after")
    def _check(self) -> Self:
        """明细与汇总必须对得上。

        🔴 这三条是"汇总值可信"的全部理由。任何一条不成立，
        这份指标就只是一堆看起来合理的数字。
        """
        # ⚠️ 两边的字段名**不一样**（``CategoryMetrics.total`` 对
        # ``CaseMetrics.total_cases``），因此显式配对而不是拼字符串——
        # 拼出来的名字一旦对不上，getattr 抛的是 AttributeError，
        # 而"校验本身崩了"与"校验发现不一致"是两回事。
        for category_field, case_field in (
            ("total", "total_cases"),
            ("executed", "executed_cases"),
            ("passed", "passed_cases"),
            ("failed", "failed_cases"),
            ("not_executed", "not_executed_cases"),
        ):
            parts = sum(getattr(item, category_field) for item in self.categories)
            whole: int = getattr(self.cases, case_field)
            if parts != whole:
                msg = f"类别 {category_field} 之和 {parts} 与总体 {whole} 不一致"
                raise ValueError(msg)

        for field in ("total", "evaluated", "passed", "failed", "unobservable"):
            parts = sum(getattr(item, field) for item in self.assertion_names)
            whole = getattr(self.assertions.overall, field)
            if parts != whole:
                msg = f"按名称的 {field} 之和 {parts} 与总体 {whole} 不一致"
                raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# 计算
# ---------------------------------------------------------------------------


def _ordered_distribution(
    counts: Counter[str],
    preferred: Sequence[str] | None = None,
) -> dict[str, int]:
    """把计数整理成**顺序稳定**的映射。

    Args:
        counts: 原始计数。
        preferred: 优先顺序（枚举的正式顺序）；不在其中的键按字典序追加。

    Returns:
        顺序确定的映射。**不丢弃任何真实出现的值**——出现了一个
        枚举里没有的状态，那是需要被看见的事实，不是需要被抹掉的噪声。
    """
    ordered: dict[str, int] = {}
    if preferred is not None:
        for key in preferred:
            if key in counts:
                ordered[key] = counts[key]
    for key in sorted(set(counts) - set(ordered)):
        ordered[key] = counts[key]
    return ordered


def _group_metrics(results: Iterable[AssertionResult]) -> AssertionGroupMetrics:
    """统计一组断言。

    🔴 三个分类**只**看结构化字段：

    * ``evaluated`` —— ``observation_status == "observed"``；
    * ``unobservable`` —— ``observation_status == "unobservable"``；
    * ``passed`` / ``failed`` —— ``evaluated`` 之内再按 ``passed`` 分。

    没有一处 ``in`` 字符串判定，也没有一处读 ``detail``。
    """
    total = evaluated = passed = unobservable = 0
    for assertion in results:
        total += 1
        if assertion.observation_status == "unobservable":
            unobservable += 1
            continue
        evaluated += 1
        if assertion.passed:
            passed += 1
    return AssertionGroupMetrics(
        total=total,
        evaluated=evaluated,
        passed=passed,
        failed=evaluated - passed,
        unobservable=unobservable,
        pass_rate=_ratio_metric(passed, evaluated),
        observation_coverage=_ratio_metric(evaluated, total),
    )


def _case_metrics(dataset: GoldenDataset, result: RunResult) -> CaseMetrics:
    """统计案例层面。"""
    total = len(dataset.cases)
    executed = len(result.cases)
    passed = sum(1 for case in result.cases if case.passed)
    # 🔴 分类来自**结构化字段** ``failure_kind``，不是 failure_reason 的文本。
    execution_errors = sum(1 for case in result.cases if case.failure_kind == "execution_error")
    assertion_failures = sum(1 for case in result.cases if case.failure_kind == "assertion_failure")
    return CaseMetrics(
        total_cases=total,
        executed_cases=executed,
        passed_cases=passed,
        failed_cases=executed - passed,
        not_executed_cases=total - executed,
        execution_error_cases=execution_errors,
        assertion_failed_cases=assertion_failures,
        case_pass_rate=_ratio_metric(passed, executed),
        execution_coverage=_ratio_metric(executed, total),
    )


def _category_metrics(dataset: GoldenDataset, result: RunResult) -> tuple[CategoryMetrics, ...]:
    """按类别统计。

    只输出**数据集中实际出现的类别**（本切片是 3 类），按类别名排序。
    ⚠️ 不输出"全部 12 个合法类别"的空壳——那会让报告里出现 9 个
    全是 0 的类别，读者分不清"这个类别没案例"与"这个类别全没过"。
    """
    total_by_category: Counter[str] = Counter(case.category.value for case in dataset.cases)
    metrics: list[CategoryMetrics] = []
    for category in sorted(total_by_category):
        in_category = [case for case in result.cases if case.category == category]
        executed = len(in_category)
        passed = sum(1 for case in in_category if case.passed)
        total = total_by_category[category]
        metrics.append(
            CategoryMetrics(
                category=category,
                total=total,
                executed=executed,
                passed=passed,
                failed=executed - passed,
                not_executed=total - executed,
                pass_rate=_ratio_metric(passed, executed),
            )
        )
    return tuple(metrics)


def _assertion_name_metrics(result: RunResult) -> tuple[AssertionNameMetrics, ...]:
    """按断言名统计。

    只输出**实际出现过**的断言名。注册表里的 19 条不必全列——
    一份有 10 行全是 0 的表只会稀释真正要看的那几行。

    ⚠️ 名称必须来自注册表：不在注册表里的断言根本进不了结果
    （加载器会拒绝），所以这里不需要"防御未知名称"的分支。
    """
    buckets: dict[str, list[AssertionResult]] = {}
    for case in result.cases:
        for assertion in case.assertions:
            buckets.setdefault(assertion.name, []).append(assertion)

    metrics: list[AssertionNameMetrics] = []
    for name in sorted(buckets):
        group = _group_metrics(buckets[name])
        required_total = sum(1 for item in buckets[name] if item.mode == "required")
        metrics.append(
            AssertionNameMetrics(
                name=name,
                total=group.total,
                required_total=required_total,
                forbidden_total=group.total - required_total,
                evaluated=group.evaluated,
                passed=group.passed,
                failed=group.failed,
                unobservable=group.unobservable,
                pass_rate=group.pass_rate,
                observation_coverage=group.observation_coverage,
            )
        )
    return tuple(metrics)


def _distributions(result: RunResult) -> DistributionMetrics:
    """已执行案例的三项分布。

    🔴 值一律取自 ``CaseObservation`` 的结构化字段。**没有一处**
    从 ``response_text`` 里抽取，也没有一处从 ``failure_reason``
    推断停止原因。

    ⚠️ 未执行的案例不进任何一项——它们没有这些属性。
    """
    states: Counter[str] = Counter()
    depths: Counter[str] = Counter()
    stop_reasons: Counter[str] = Counter()

    for case in result.cases:
        observation = case.observation
        if observation is None:
            # 没有观测就没有分布项可记。**不填 __none__** ——
            # 那会把"没跑起来"混进"跑起来但没有停止原因"。
            continue
        states[observation.state] += 1
        depths[observation.depth] += 1
        stop_reasons[observation.stop_reason or NONE_KEY] += 1

    return DistributionMetrics(
        final_state=_ordered_distribution(states, [member.value for member in RoundState]),
        depth=_ordered_distribution(depths, [member.value for member in CognitiveDepth]),
        # 停止原因是自由字符串（不是枚举），因此按字典序稳定排序。
        stop_reason=_ordered_distribution(stop_reasons),
    )


def _failure_index(result: RunResult) -> FailureIndex:
    """结构化失败索引。"""
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
                if any(
                    assertion.observation_status == "unobservable" for assertion in case.assertions
                )
            )
        ),
        failed_assertions_by_case={case_id: by_case[case_id] for case_id in sorted(by_case)},
    )


def compute_metrics(dataset: GoldenDataset, result: RunResult) -> EvaluationMetrics:
    """由一次运行的结构化结果算出指标。

    🔴 **纯函数**：只读入参，不重跑案例、不访问网络、不查数据库、
    不读 Provider 与 Prompt，也不修改传进来的对象。

    Args:
        dataset: 已加载的数据集（提供 ``total_cases`` 与类别）。
        result: 本次运行的结果。

    Returns:
        完整指标。

    Raises:
        ValueError: 明细与汇总对不上（由模型的不变量校验抛出）。
    """
    all_assertions = [assertion for case in result.cases for assertion in case.assertions]
    return EvaluationMetrics(
        metrics_schema_version=METRICS_SCHEMA_VERSION,
        metrics_definition_digest=metrics_definition_digest(),
        cases=_case_metrics(dataset, result),
        categories=_category_metrics(dataset, result),
        assertions=AssertionMetrics(
            overall=_group_metrics(all_assertions),
            required=_group_metrics(item for item in all_assertions if item.mode == "required"),
            forbidden=_group_metrics(item for item in all_assertions if item.mode == "forbidden"),
        ),
        assertion_names=_assertion_name_metrics(result),
        distributions=_distributions(result),
        failures=_failure_index(result),
    )
