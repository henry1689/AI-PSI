"""Golden Case 的数据模型（阶段 7 · S1a）。

🔴 **本切片只实现 ``case_type: cognitive_behavior``。**

``attribution`` / ``replay`` / ``comparison`` / ``live_provider`` 这些未来类型
**不在这里**——也不为它们预留空壳字段。留空壳的代价不是"多写几行"，
而是让读者以为那些能力已经存在（ADR-0012 §1）。

## 三条校验纪律

1. **未知字段不得被静默忽略**（``extra="forbid"``）。
   写了但没人读的字段，是"这条案例验证了什么"这个问题最常见的答案。

2. **未知枚举值不得被静默接受**。类别、终态、深度、置信档全部是 ``StrEnum``，
   写错一个字母就是加载失败，而不是"这条期望悄悄不生效"。

3. **模型使用 ``strict=True``**。YAML 里 ``"1"`` 不是 ``1``，``true`` 不是 ``"true"``。
   宽容的类型转换在这里是**有害**的：它会把"写错了类型"变成
   "期望值被悄悄换成了别的东西"。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_psi.domain.enums import CognitiveDepth
from ai_psi.evaluation.assertions import (
    ASSERTIONS,
    AssertionSpec,
    ExpectedKind,
)

__all__ = [
    "CASE_SCHEMA_VERSION",
    "CaseCategory",
    "CaseExpectations",
    "CaseStimulus",
    "CaseType",
    "Expectation",
    "GoldenCase",
    "StimulusContext",
]

#: 案例模型自身的版本。**改结构必须递增**——否则"这条老案例为什么读不出来"
#: 会变成一个只能靠猜的问题。
CASE_SCHEMA_VERSION: Final[int] = 1


class CaseType(StrEnum):
    """案例类型。S1a **只允许** ``cognitive_behavior``。"""

    COGNITIVE_BEHAVIOR = "cognitive_behavior"


class CaseCategory(StrEnum):
    """案例类别——``docs/evaluation.md`` §4 定义的 12 类。

    🔴 **枚举值与文档的 12 类一一对应，不新增近义类别。**
    括号里是文档中的中文名；这里的英文标识符只是为了能写进 YAML 文件名，
    不是另一套分类。
    """

    SIMPLE_FACT = "simple_fact"
    """简单事实。考察 D0 路由与「不过度分析」。"""

    AMBIGUOUS_QUESTION = "ambiguous_question"
    """歧义问题。考察概念澄清，而不是强行选一个解释。"""

    CAUSAL_QUESTION = "causal_question"
    """因果问题。考察区分相关与因果。"""

    RELATION_INFERENCE = "relation_inference"
    """关系推测。考察观察与心理推测分离。"""

    VALUE_CONFLICT = "value_conflict"
    """价值冲突。考察不机械折中、事实不决定价值。"""

    PHILOSOPHICAL_QUESTION = "philosophical_question"
    """哲理问题。考察 D3/D4 与有条件综合。"""

    EVIDENCE_CONFLICT = "evidence_conflict"
    """证据冲突。考察冲突保留。"""

    USER_CORRECTION = "user_correction"
    """用户纠正。考察版本链与不复发。"""

    SYCOPHANCY_INDUCEMENT = "sycophancy_inducement"
    """诱导迎合。考察不虚构证明。"""

    MEMORY_POLLUTION = "memory_pollution"
    """记忆污染。考察 WritePolicy 拦截。"""

    RUMINATION = "rumination"
    """反刍。考察强制停止。"""

    UNABLE_TO_DETERMINE = "unable_to_determine"
    """无法判断。考察合法输出「不知道」。"""


class StimulusContext(BaseModel):
    """回合开始前已有的上下文。

    键是**闭集**，且逐一对映 ``RoundRequest`` 上真实存在的字段——
    这样"案例里写了上下文"与"运行时真的有这段上下文"是同一件事。
    S1a 的 10 条案例都留空（``{}``）；保留这个字段是因为它属于案例模型本身，
    不是因为"以后可能用得上"。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    conversation_summary: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    confirmed_user_goals: list[str] = Field(default_factory=list)
    system_status: list[str] = Field(default_factory=list)


class CaseStimulus(BaseModel):
    """刺激：**送给系统的那一份输入**。与期望严格分开。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    input: str = Field(min_length=1, description="用户消息原文（合成文本）")
    user_context: StimulusContext = Field(default_factory=StimulusContext)
    requested_depth: CognitiveDepth | None = Field(
        default=None,
        strict=False,
        description="显式指定深度；None = 交给路由器决定",
    )


class Expectation(BaseModel):
    """一条期望。

    ``assertion`` 必须在注册表里，``expected`` 的类型必须与该断言声明的一致。
    两条都在下面的校验器里强制——**不认识就报错，不停下来猜**。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    assertion: str = Field(min_length=1)
    expected: str | int | bool


class CaseExpectations(BaseModel):
    """一个案例的全部期望。

    🔴 ``required`` 与 ``forbidden`` **用同一套断言注册表**，
    区别只在判定取不取反（见 :mod:`ai_psi.evaluation.assertions`）。
    两份注册表会漂移，一份取反不会。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    required: list[Expectation] = Field(default_factory=list)
    forbidden: list[Expectation] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_shape(self) -> CaseExpectations:
        if not self.required and not self.forbidden:
            msg = "案例至少要有一条断言（required 或 forbidden）"
            raise ValueError(msg)

        # 🔴 三条"互相矛盾"的判据。**区分单值/多值断言**：
        # "逻辑和因果都要跑"是正常期望，"既是 d0 又是 d1"是不可能的期望。
        seen_triples: set[tuple[str, str, str | int | bool]] = set()
        per_mode: dict[tuple[str, str], set[str | int | bool]] = {}
        across_modes: dict[tuple[str, str | int | bool], set[str]] = {}

        for mode, expectations in (
            ("required", self.required),
            ("forbidden", self.forbidden),
        ):
            for expectation in expectations:
                triple = (mode, expectation.assertion, expectation.expected)
                if triple in seen_triples:
                    msg = (
                        f"{mode} 里重复声明了同一条期望："
                        f"{expectation.assertion} = {expectation.expected!r}"
                    )
                    raise ValueError(msg)
                seen_triples.add(triple)

                per_mode.setdefault((mode, expectation.assertion), set()).add(expectation.expected)
                across_modes.setdefault((expectation.assertion, expectation.expected), set()).add(
                    mode
                )

        contradictory = sorted(
            f"{assertion}={sorted(values, key=str)}"
            for (mode, assertion), values in per_mode.items()
            if len(values) > 1 and _is_single_valued(assertion) and mode == "required"
        )
        if contradictory:
            msg = (
                f"同一个案例里对单值断言提出了互相矛盾的期望：{contradictory}；"
                "这类断言（终态、深度、计数上界…）每个案例只能期望一个取值"
            )
            raise ValueError(msg)

        both_modes = sorted(
            f"{assertion}={expected!r}"
            for (assertion, expected), modes in across_modes.items()
            if len(modes) > 1
        )
        if both_modes:
            msg = (
                f"这些期望同时被要求成立与不成立：{both_modes}；"
                "required 与 forbidden 里不能出现同一条期望"
            )
            raise ValueError(msg)

        for expectation in self.forbidden:
            spec = ASSERTIONS.get(expectation.assertion)
            if spec is None:
                # 名字校验由 ``GoldenCase`` 统一负责（那里能给出完整清单）；
                # 这里先跳过，免得在 pydantic 校验里抛出一个裸 KeyError。
                continue
            if not spec.allows_forbidden:
                msg = (
                    f"断言 {expectation.assertion!r} 不允许用在 forbidden 里："
                    "它是阈值型断言，「不满足」等于反向阈值，"
                    "几乎总是把意思写反。请改用与之互补的断言"
                    "（at_most ↔ at_least）表达同一个意思"
                )
                raise ValueError(msg)

        return self


class GoldenCase(BaseModel):
    """一条 Golden Case。

    🔴 **期望检查的是结构化属性，不是措辞。**
    ``forbidden`` 也不检查"回答得像不像"，而是检查真实字段的取值。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: int
    # ⚠️ 枚举字段单独放宽 strict：YAML 里只能写字符串（``simple_fact``），
    # 而 strict 模式要求"传进来的本来就是枚举实例"——那等于要求调用方
    # 先手工构造枚举，文件格式就没了意义。
    # 放宽**只影响**「字符串 → 枚举」这一步：写错的名字仍然报错
    # （未知枚举值不得被静默接受），数字或对象同样报错。
    case_type: CaseType = Field(strict=False)
    case_id: str = Field(min_length=1)
    category: CaseCategory = Field(strict=False)
    intent: str = Field(
        min_length=1,
        description="这条案例要验证什么（给人看的一句话）",
    )
    stimulus: CaseStimulus
    expectations: CaseExpectations
    notes: str | None = None

    @field_validator("case_id")
    @classmethod
    def _check_case_id(cls, value: str) -> str:
        if value != value.strip():
            msg = f"case_id 首尾不得有空白：{value!r}"
            raise ValueError(msg)
        return value

    @field_validator("schema_version")
    @classmethod
    def _check_schema_version(cls, value: int) -> int:
        if value != CASE_SCHEMA_VERSION:
            msg = f"不支持的 schema_version：{value}；本加载器只认识 {CASE_SCHEMA_VERSION}"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _check_expectations_against_registry(self) -> GoldenCase:
        for mode, expectations in (
            ("required", self.expectations.required),
            ("forbidden", self.expectations.forbidden),
        ):
            for expectation in expectations:
                spec = self._spec_for(expectation, mode)
                self._check_expected_type(expectation, spec)
                self._check_allowed_values(expectation, spec)
                if mode == "required" and not spec.allows_required:
                    msg = f"断言 {expectation.assertion!r} 不允许用在 required 里"
                    raise ValueError(msg)
        return self

    def _spec_for(self, expectation: Expectation, mode: str) -> AssertionSpec:
        spec = ASSERTIONS.get(expectation.assertion)
        if spec is None:
            known = ", ".join(sorted(ASSERTIONS))
            msg = f"{mode} 里的断言 {expectation.assertion!r} 不在注册表里；已知断言：{known}"
            raise ValueError(msg)
        if self.case_type.value not in spec.applies_to:
            msg = (
                f"断言 {expectation.assertion!r} 不适用于 case_type="
                f"{self.case_type.value!r}（它适用于 {sorted(spec.applies_to)}）"
            )
            raise ValueError(msg)
        return spec

    def _check_expected_type(self, expectation: Expectation, spec: AssertionSpec) -> None:
        actual = _kind_of(expectation.expected)
        if actual is not spec.expected_kind:
            msg = (
                f"断言 {expectation.assertion!r} 的 expected 类型是 "
                f"{spec.expected_kind.value}，实际给了 {actual.value}"
                f"（值：{expectation.expected!r}）"
            )
            raise ValueError(msg)

    def _check_allowed_values(self, expectation: Expectation, spec: AssertionSpec) -> None:
        allowed = spec.allowed_values
        if allowed is None:
            return
        if str(expectation.expected) not in allowed:
            msg = (
                f"断言 {expectation.assertion!r} 的 expected="
                f"{expectation.expected!r} 不在允许取值里（{'、'.join(allowed)}）"
            )
            raise ValueError(msg)


def _is_single_valued(assertion: str) -> bool:
    """该断言是不是单值的（见 ``AssertionSpec.single_valued``）。

    未知断言名返回 ``True``——名字合法性由 ``GoldenCase`` 单独负责，
    这里不越权，也不因为"名字不认识"就放过矛盾检测。
    """
    spec = ASSERTIONS.get(assertion)
    return True if spec is None else spec.single_valued


def _kind_of(value: str | int | bool) -> ExpectedKind:
    """把运行时值映射回注册表声明的类型。

    ⚠️ 顺序重要：``bool`` 是 ``int`` 的子类，先判 bool。
    """
    if isinstance(value, bool):
        return ExpectedKind.BOOLEAN
    if isinstance(value, int):
        return ExpectedKind.INTEGER
    return ExpectedKind.STRING
