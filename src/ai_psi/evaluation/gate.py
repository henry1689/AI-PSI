"""版本化、作用域绑定的结构化质量门禁（阶段 7 · S6）。

## 它回答什么

"对于一份合法、完整、可比较、且落在某个**明确策略作用域**内的 S5 对比，
它是否满足该策略定义的规则？"

## 🔴 它不回答什么

Candidate 是否适合生产流量、是否可自动部署、是否可自动合并。
:class:`GateDecision` 里**没有** ``release_allowed`` / ``deploy_allowed`` /
``merge_allowed`` / ``production_ready`` / ``recommendation`` /
``quality_score`` / ``risk_score`` / ``confidence_score`` 这些字段——
不是"暂时没填"，是模型一律 ``extra="forbid"``，多写一个就是校验失败。

**``PASS`` 不是发布许可。** 它只表示"Candidate 没有违反当前这份策略
对当前这个固定评测作用域定义的规则"。

## 三个必须分开的概念

============================  ==========================================
概念                           由谁决定
============================  ==========================================
``comparison_eligible``       S5——两份结果能不能做受控结构化比较
``policy_applicable``         S6——这份策略适不适用于这份对比
``outcome``                   S6——**且只在前两者都成立之后**才判定
============================  ==========================================

把三者混起来会得到最糟的那种工具：一个"因为没法判断所以判失败"的门禁，
或者一个"策略根本不管这个数据集却给了通过"的门禁。

## 闭合集合与 fail-closed

``PASS`` / ``FAIL`` / ``NOT_EVALUATED``。**无法完整评估一律 ``NOT_EVALUATED``**，
绝不降级成 ``FAIL``（那是诬告）也绝不降级成 ``PASS``（那是放行）。

## 策略是可读的 JSON，不是可执行的东西

:class:`GatePolicy` 没有任何表达式、路径、插件或网络能力：规则类型、
证据键与算符都是**闭合注册表**，未知值直接拒绝策略。策略里能写的东西，
读的人一眼能看完全部。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ai_psi.evaluation.comparison import (
    ComparisonInputError,
    EvaluationComparison,
)
from ai_psi.evaluation.metrics import RatioMetric
from ai_psi.evaluation.serialization import dumps

__all__ = [
    "APPLICABILITY_REASONS",
    "EVIDENCE_KINDS",
    "GATE_DECISION_SCHEMA_VERSION",
    "POLICY_SCHEMA_VERSION",
    "ApplicabilityReason",
    "EvidenceKey",
    "EvidenceKind",
    "ExpectedEvidence",
    "GateDecision",
    "GateDecisionIdentity",
    "GateOutcome",
    "GatePolicy",
    "ObservedEvidence",
    "PolicyApplicability",
    "PolicyInputError",
    "PolicyRule",
    "PolicyScope",
    "RatioExpectation",
    "RuleOperator",
    "RuleOutcome",
    "RuleReason",
    "RuleResult",
    "RuleType",
    "ScopeCountRef",
    "SupportedComparisonContract",
    "comparison_fingerprint",
    "decide",
    "evaluate_policy_applicability",
    "gate_definition_digest",
    "load_gate_policy",
    "policy_digest",
    "write_decision",
]

#: 门禁产物自身的版本。**改聚合规则就要改它**（或改定义摘要）。
GATE_DECISION_SCHEMA_VERSION: Final[int] = 1

#: 策略结构自身的版本。
POLICY_SCHEMA_VERSION: Final[int] = 1

#: ``sha256:<64 位小写十六进制>``。所有策略里的摘要都必须是这个形状。
_DIGEST_PATTERN: Final[str] = r"^sha256:[0-9a-f]{64}$"

#: 固定 6 位小数的十进制字符串。**比率阈值不接受浮点数。**
_DECIMAL_PATTERN: Final[str] = r"^\d+\.\d{6}$"

#: 稳定标识符的字符集：小写字母开头，只含小写字母、数字与下划线。
_ID_PATTERN: Final[str] = r"^[a-z][a-z0-9_]*$"


# ---------------------------------------------------------------------------
# 定义身份
# ---------------------------------------------------------------------------

#: 门禁定义的语义摘要输入。
#:
#: 🔴 它回答的是"这个结论是**按什么规则**得出的"。只写一个
#: ``"version": 1`` 是不够的——那没法告诉别人聚合是怎么做的、
#: 缺失证据会被怎么办。
GATE_DEFINITION: Final[dict[str, object]] = {
    "aggregation": {
        "any_rule_fail": "FAIL",
        "any_rule_not_evaluated": "NOT_EVALUATED",
        "not_applicable": "NOT_EVALUATED（不评估任何质量规则）",
        "not_eligible": "NOT_EVALUATED（不评估任何质量规则）",
        "rule_order": "按策略 rules 数组的**声明顺序**，不重排",
        "all_rules_pass": "PASS",
    },
    "applicability": (
        "把 comparison_eligible 与 Comparison 的双方身份逐项对照策略 scope；"
        "任一不匹配即不适用，并给出稳定的原因码"
    ),
    "decimal_comparison": (
        "比率阈值是固定 6 位小数的字符串，用标准库 Decimal 比较；**任何一处都不使用二进制浮点**"
    ),
    "evidence_unavailable": (
        "该规则的证据取不到时，该规则为 NOT_EVALUATED、原因为 evidence_unavailable，"
        "整体 outcome 为 NOT_EVALUATED——**绝不**把缺失证据当成 0，"
        "也**绝不**把未评估的规则当成通过"
    ),
    "forbidden_output": (
        "产物不得含 release_allowed / deploy_allowed / merge_allowed / production_ready / "
        "recommendation / quality_score / risk_score / confidence_score"
    ),
    "not_eligible_policy": "comparison_eligible=false ⇒ policy_applicable=false ⇒ NOT_EVALUATED",
    "operators": ["EQ", "EMPTY"],
    "outcomes": {
        "FAIL": "适用，且至少一条规则明确失败",
        "NOT_EVALUATED": "不可比较 / 策略不适用 / 证据缺失 / 无法完整评估",
        "PASS": "可比较、适用，且**全部**规则通过",
    },
    "rule_types": ["count_equals", "id_set_empty", "ratio_equals", "transition_count_equals"],
    "schema_version": GATE_DECISION_SCHEMA_VERSION,
    "serialization": "UTF-8、键排序、缩进固定、结尾恰好一个换行；无时间戳、无随机 UUID",
    "sorting": (
        "applicability_reasons 去重后按字典序；rule_results 按策略声明顺序；"
        "failed_rule_ids / not_evaluated_rule_ids 按 rule_results 顺序"
    ),
}


def gate_definition_digest() -> str:
    """门禁定义的语义摘要。

    🔴 **改聚合规则、适用性规则或规则类型表必须让它变**：沿用旧摘要等于
    宣称"这两个结论是按同一套规则得出的"，而它们不是。

    Returns:
        形如 ``sha256:<hex>`` 的摘要。
    """
    text = json.dumps(GATE_DEFINITION, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _digest(payload: object) -> str:
    """对任意**已规范化**的结构取 SHA-256。"""
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


# ---------------------------------------------------------------------------
# 输入错误
# ---------------------------------------------------------------------------


class PolicyInputError(ValueError):
    """策略读不出来、结构不合法，或与自身摘要不符。

    ⚠️ 它**不是**"策略不适用"。这个异常意味着这份策略根本不是一个可用的
    契约——此时不产生任何 GateDecision，更不会把它判成 ``FAIL``
    （那是拿使用者的策略错误去指控 Candidate）。
    """


# ---------------------------------------------------------------------------
# 闭合集合
# ---------------------------------------------------------------------------


class GateOutcome(StrEnum):
    """门禁结论。**闭合三值。**"""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUATED = "NOT_EVALUATED"


class RuleOutcome(StrEnum):
    """单条规则的结论。**闭合三值。**"""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUATED = "NOT_EVALUATED"


class RuleType(StrEnum):
    """规则类型注册表。**闭合**：不认识的类型直接拒绝策略。"""

    COUNT_EQUALS = "count_equals"
    TRANSITION_COUNT_EQUALS = "transition_count_equals"
    ID_SET_EMPTY = "id_set_empty"
    RATIO_EQUALS = "ratio_equals"


class RuleOperator(StrEnum):
    """算符注册表。**闭合**，且每种规则类型只允许一个算符。"""

    EQ = "EQ"
    EMPTY = "EMPTY"


class EvidenceKind(StrEnum):
    """证据的形状。"""

    COUNT = "count"
    RATIO = "ratio"
    ID_SET = "id_set"


class ScopeCountRef(StrEnum):
    """比率的分母应当对齐策略作用域里的哪一个计数。

    🔴 它是**闭合引用**，不是任意路径：策略只能引用这几个具名计数，
    不能写表达式去取别的东西。
    """

    NONE = "none"
    DATASET_CASE_COUNT = "dataset_case_count"
    EXPECTED_ASSERTION_COUNT = "expected_assertion_count"


class RuleReason(StrEnum):
    """规则结论的原因码。**闭合且稳定。**"""

    SATISFIED = "rule_satisfied"
    COUNT_MISMATCH = "count_mismatch"
    RATIO_MISMATCH = "ratio_mismatch"
    DENOMINATOR_MISMATCH = "denominator_mismatch"
    UNEXPECTED_IDS_PRESENT = "unexpected_ids_present"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    CONTRACT_INVALID = "rule_contract_invalid"


class EvidenceKey(StrEnum):
    """**证据键注册表**。

    🔴 策略只能从这个闭集里挑。不允许任意路径、属性访问或表达式——
    策略文件里出现一个这里没有的键，那份策略就是无效的。
    """

    # ---- 相对回归证据（来自转换汇总与失败索引差异）----
    CASE_REGRESSION_TRANSITION_COUNT = "case.regression_transition_count"
    ASSERTION_REGRESSION_TRANSITION_COUNT = "assertion.regression_transition_count"
    ASSERTION_CHANGED_UNRESOLVED_COUNT = "assertion.changed_unresolved_count"
    FAILURE_NEWLY_FAILED_CASE_IDS = "failure.newly_failed_case_ids"
    FAILURE_NEWLY_EXECUTION_ERROR_CASE_IDS = "failure.newly_execution_error_case_ids"
    FAILURE_NEWLY_UNOBSERVABLE_CASE_IDS = "failure.newly_unobservable_case_ids"
    # ---- Candidate 的**绝对**状态（不是相对差异）----
    #
    # 🔴 这两类必须同时在策略里出现。只比相对差异，会把
    # "Baseline 本来就很差、Candidate 只是少差一点"判成通过。
    CANDIDATE_CASE_PASS_RATE = "candidate.case_pass_rate"
    CANDIDATE_EXECUTION_COVERAGE = "candidate.execution_coverage"
    CANDIDATE_ASSERTION_PASS_RATE = "candidate.assertion_pass_rate"
    CANDIDATE_OBSERVATION_COVERAGE = "candidate.observation_coverage"
    CANDIDATE_EXECUTION_ERROR_CASES = "candidate.execution_error_cases"
    CANDIDATE_NOT_EXECUTED_CASES = "candidate.not_executed_cases"
    CANDIDATE_FAILED_ASSERTIONS = "candidate.failed_assertions"
    CANDIDATE_UNOBSERVABLE_ASSERTIONS = "candidate.unobservable_assertions"


#: 每个证据键的形状。**与 :class:`EvidenceKey` 一一对应，缺一不可。**
EVIDENCE_KINDS: Final[dict[EvidenceKey, EvidenceKind]] = {
    EvidenceKey.CASE_REGRESSION_TRANSITION_COUNT: EvidenceKind.COUNT,
    EvidenceKey.ASSERTION_REGRESSION_TRANSITION_COUNT: EvidenceKind.COUNT,
    EvidenceKey.ASSERTION_CHANGED_UNRESOLVED_COUNT: EvidenceKind.COUNT,
    EvidenceKey.FAILURE_NEWLY_FAILED_CASE_IDS: EvidenceKind.ID_SET,
    EvidenceKey.FAILURE_NEWLY_EXECUTION_ERROR_CASE_IDS: EvidenceKind.ID_SET,
    EvidenceKey.FAILURE_NEWLY_UNOBSERVABLE_CASE_IDS: EvidenceKind.ID_SET,
    EvidenceKey.CANDIDATE_CASE_PASS_RATE: EvidenceKind.RATIO,
    EvidenceKey.CANDIDATE_EXECUTION_COVERAGE: EvidenceKind.RATIO,
    EvidenceKey.CANDIDATE_ASSERTION_PASS_RATE: EvidenceKind.RATIO,
    EvidenceKey.CANDIDATE_OBSERVATION_COVERAGE: EvidenceKind.RATIO,
    EvidenceKey.CANDIDATE_EXECUTION_ERROR_CASES: EvidenceKind.COUNT,
    EvidenceKey.CANDIDATE_NOT_EXECUTED_CASES: EvidenceKind.COUNT,
    EvidenceKey.CANDIDATE_FAILED_ASSERTIONS: EvidenceKind.COUNT,
    EvidenceKey.CANDIDATE_UNOBSERVABLE_ASSERTIONS: EvidenceKind.COUNT,
}

#: 转换汇总专用键——``transition_count_equals`` 只能用这几个。
_TRANSITION_EVIDENCE: Final[frozenset[EvidenceKey]] = frozenset(
    {
        EvidenceKey.CASE_REGRESSION_TRANSITION_COUNT,
        EvidenceKey.ASSERTION_REGRESSION_TRANSITION_COUNT,
        EvidenceKey.ASSERTION_CHANGED_UNRESOLVED_COUNT,
    }
)

#: 每种规则类型只允许一个算符。
_RULE_OPERATOR: Final[dict[RuleType, RuleOperator]] = {
    RuleType.COUNT_EQUALS: RuleOperator.EQ,
    RuleType.TRANSITION_COUNT_EQUALS: RuleOperator.EQ,
    RuleType.ID_SET_EMPTY: RuleOperator.EMPTY,
    RuleType.RATIO_EQUALS: RuleOperator.EQ,
}


class ApplicabilityReason(StrEnum):
    """策略**不适用**的原因码。闭合集合，输出时按字典序稳定排序。

    ⚠️ 这里**没有** ``rule_evidence_unavailable``。任务书 §九 的建议清单里
    有它，但在当前契约下它**不可达**：``comparison_eligible=true`` 时，
    S5 的模型不变量保证五组差异结构全部非空，因此"规则证据取不到"只可能
    发生在规则层——那是 :class:`RuleReason.EVIDENCE_UNAVAILABLE`，配的是
    ``policy_applicable=true``（策略管得着，只是这次没查完）。
    放进这个枚举会让"这份策略不适用"与"这次没查完"混成同一句话。
    """

    COMPARISON_NOT_ELIGIBLE = "comparison_not_eligible"
    COMPARISON_CONTRACT_UNSUPPORTED = "comparison_contract_unsupported"
    DATASET_OUT_OF_SCOPE = "dataset_out_of_scope"
    DATASET_CASE_COUNT_OUT_OF_SCOPE = "dataset_case_count_out_of_scope"
    ASSERTION_REGISTRY_OUT_OF_SCOPE = "assertion_registry_out_of_scope"
    PROMPT_OUT_OF_SCOPE = "prompt_out_of_scope"
    PROVIDER_OUT_OF_SCOPE = "provider_out_of_scope"
    MODEL_OUT_OF_SCOPE = "model_out_of_scope"
    PROVIDER_CONFIGURATION_OUT_OF_SCOPE = "provider_configuration_out_of_scope"
    EXECUTION_MODE_OUT_OF_SCOPE = "execution_mode_out_of_scope"
    STORAGE_BACKEND_OUT_OF_SCOPE = "storage_backend_out_of_scope"
    MIGRATION_REVISION_OUT_OF_SCOPE = "migration_revision_out_of_scope"
    METRICS_CONTRACT_OUT_OF_SCOPE = "metrics_contract_out_of_scope"
    COMPARISON_IDENTITY_INCOMPLETE = "comparison_identity_incomplete"
    POLICY_IDENTITY_INVALID = "policy_identity_invalid"
    POLICY_DIGEST_MISMATCH = "policy_digest_mismatch"
    COMPARISON_INPUT_INVALID = "comparison_input_invalid"
    POLICY_INPUT_INVALID = "policy_input_invalid"


#: 全部适用性原因码（供文档与测试遍历）。
APPLICABILITY_REASONS: Final[tuple[str, ...]] = tuple(
    sorted(member.value for member in ApplicabilityReason)
)


# ---------------------------------------------------------------------------
# 策略模型
# ---------------------------------------------------------------------------


class PolicyScope(BaseModel):
    """策略**作用域**：这份策略管的是哪一类评测。

    🔴 每个字段都对应 Comparison 里一个**已验证的结构化身份**，
    没有任何一个是猜的或按文件名推的。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    dataset_digest: str = Field(pattern=_DIGEST_PATTERN)
    dataset_case_count: int = Field(ge=0)
    #: 固定作用域的断言条数。**不硬编码在规则里**——它是 scope 的一部分，
    #: 因此改它会让 ``policy_digest`` 变。
    expected_assertion_count: int = Field(gt=0)
    assertion_registry_digest: str = Field(pattern=_DIGEST_PATTERN)
    prompt_digest: str = Field(pattern=_DIGEST_PATTERN)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    provider_configuration_digest: str = Field(pattern=_DIGEST_PATTERN)
    execution_mode: str = Field(min_length=1)
    storage_backend: str = Field(min_length=1)
    #: 内存后端没有迁移版本；此时两边都必须是 ``null``。
    migration_revision: str | None
    metrics_schema_version: int = Field(ge=1)
    metrics_definition_digest: str = Field(pattern=_DIGEST_PATTERN)


class SupportedComparisonContract(BaseModel):
    """这份策略认得哪一版对比契约。

    🔴 不匹配时的结论是 ``NOT_EVALUATED``（不适用），**不是** ``FAIL``：
    契约换了说明这份策略的规则可能已经不对口，而不是 Candidate 变差了。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    comparison_schema_version: int = Field(ge=1)
    comparison_definition_digest: str = Field(pattern=_DIGEST_PATTERN)


class RatioExpectation(BaseModel):
    """``ratio_equals`` 的期望。

    🔴 **阈值是字符串，不是浮点数。** 而且期望不止一个值：
    分子要等于分母、分母要大于 0、分母还要对齐作用域里的某个具名计数——
    只比 ``value == "1.000000"`` 会漏掉"分母悄悄缩小了"这种情形。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str = Field(pattern=_DECIMAL_PATTERN)
    numerator_equals_denominator: bool = True
    denominator_gt_zero: bool = True
    denominator_ref: ScopeCountRef = ScopeCountRef.NONE


class _RuleBase(BaseModel):
    """规则共有的字段。**不直接出现在策略里**，只作为各类型的基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str = Field(pattern=_ID_PATTERN)
    description: str = Field(min_length=1)
    evidence_key: EvidenceKey


class CountEqualsRule(_RuleBase):
    """Candidate 的某个**绝对计数**必须等于期望值。"""

    rule_type: Literal[RuleType.COUNT_EQUALS]
    operator: Literal[RuleOperator.EQ]
    expected: int = Field(ge=0)


class TransitionCountEqualsRule(_RuleBase):
    """某个**转换汇总计数**必须等于期望值。"""

    rule_type: Literal[RuleType.TRANSITION_COUNT_EQUALS]
    operator: Literal[RuleOperator.EQ]
    expected: int = Field(ge=0)


class IdSetEmptyRule(_RuleBase):
    """某个 ID 集合必须为空。"""

    rule_type: Literal[RuleType.ID_SET_EMPTY]
    operator: Literal[RuleOperator.EMPTY]


class RatioEqualsRule(_RuleBase):
    """某个**绝对比率**必须满足期望。"""

    rule_type: Literal[RuleType.RATIO_EQUALS]
    operator: Literal[RuleOperator.EQ]
    expected: RatioExpectation


#: 规则的判别联合：``rule_type`` 决定 ``expected`` 的形状。
PolicyRule = Annotated[
    CountEqualsRule | TransitionCountEqualsRule | IdSetEmptyRule | RatioEqualsRule,
    Field(discriminator="rule_type"),
]


class GatePolicy(BaseModel):
    """一份版本化的评测门禁策略。

    ⚠️ 它是**声明式的 JSON**：没有表达式、没有 include、没有继承、
    没有插件、没有网络。规则类型、证据键与算符都是闭合注册表。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_schema_version: int
    policy_id: str = Field(pattern=_ID_PATTERN)
    policy_revision: int = Field(ge=1)
    #: 由**除它自己以外**的全部字段算出的摘要（见 :func:`policy_digest`）。
    policy_digest: str = Field(pattern=_DIGEST_PATTERN)
    display_name: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    scope: PolicyScope
    supported_comparison_contract: SupportedComparisonContract
    rules: tuple[PolicyRule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        """结构性校验：版本、规则唯一性、规则类型与证据键的匹配。

        🔴 最后一条是这里最重要的：``id_set_empty`` 配上计数键、
        或 ``transition_count_equals`` 配上 Candidate 绝对计数键，
        都会让"通过"的含义变得说不清楚。**在策略层面拒绝**，
        比在执行期返回一个含糊的结论好。
        """
        if self.policy_schema_version != POLICY_SCHEMA_VERSION:
            msg = (
                f"不支持的 policy_schema_version：{self.policy_schema_version}；"
                f"本版本只认识 {POLICY_SCHEMA_VERSION}"
            )
            raise ValueError(msg)

        seen: set[str] = set()
        for rule in self.rules:
            if rule.rule_id in seen:
                msg = f"重复的 rule_id：{rule.rule_id!r}"
                raise ValueError(msg)
            seen.add(rule.rule_id)
            self._check_rule_contract(rule)
        return self

    @staticmethod
    def _check_rule_contract(rule: PolicyRule) -> None:
        """规则类型、算符与证据键三者必须对得上。"""
        expected_operator = _RULE_OPERATOR[rule.rule_type]
        if rule.operator is not expected_operator:
            msg = (
                f"规则 {rule.rule_id!r}（{rule.rule_type}）只允许算符 "
                f"{expected_operator}，收到 {rule.operator}"
            )
            raise ValueError(msg)

        kind = EVIDENCE_KINDS[rule.evidence_key]
        if rule.rule_type is RuleType.ID_SET_EMPTY and kind is not EvidenceKind.ID_SET:
            msg = f"规则 {rule.rule_id!r} 需要集合型证据，{rule.evidence_key} 是 {kind}"
            raise ValueError(msg)
        if rule.rule_type is RuleType.RATIO_EQUALS and kind is not EvidenceKind.RATIO:
            msg = f"规则 {rule.rule_id!r} 需要比率型证据，{rule.evidence_key} 是 {kind}"
            raise ValueError(msg)
        if rule.rule_type is RuleType.TRANSITION_COUNT_EQUALS and (
            rule.evidence_key not in _TRANSITION_EVIDENCE
        ):
            msg = f"规则 {rule.rule_id!r} 需要转换汇总证据，{rule.evidence_key} 不在转换键集合里"
            raise ValueError(msg)
        if rule.rule_type is RuleType.COUNT_EQUALS and rule.evidence_key in _TRANSITION_EVIDENCE:
            msg = (
                f"规则 {rule.rule_id!r} 用 count_equals 取转换汇总键 "
                f"{rule.evidence_key}；请改用 transition_count_equals"
            )
            raise ValueError(msg)


def policy_digest(payload: dict[str, Any]) -> str:
    """由**除 ``policy_digest`` 以外**的全部字段算出策略摘要。

    🔴 摘要是对**规范化后的整份策略内容**取的，不是一个人工常量：
    改一条规则、改一个 scope 字段、甚至改一句 description 都会让它变。
    因此"这份结论出自哪一版策略"是可复核的，而不是需要相信的。

    Args:
        payload: 策略的原始 JSON 对象（可含 ``policy_digest``）。

    Returns:
        形如 ``sha256:<hex>`` 的摘要。
    """
    body = {key: value for key, value in payload.items() if key != "policy_digest"}
    return _digest(body)


# ---------------------------------------------------------------------------
# 决策模型
# ---------------------------------------------------------------------------


class PolicyApplicability(BaseModel):
    """这份策略适不适用于这份对比。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    applicable: bool
    #: 不适用的原因码；适用时为空。**稳定排序、去重。**
    reasons: tuple[str, ...]

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.applicable != (not self.reasons):
            msg = f"applicable={self.applicable} 与 reasons={list(self.reasons)} 不自洽"
            raise ValueError(msg)
        if sorted(set(self.reasons)) != list(self.reasons):
            msg = "reasons 必须去重并稳定排序"
            raise ValueError(msg)
        return self


class ObservedEvidence(BaseModel):
    """规则**实际观测到**的结构化值。

    ⚠️ 只有一个形状字段是激活的（由 ``kind`` 决定）。这样"计数规则拿到了
    比率"这类错配在构造时就失败，而不是在执行期被静默地当成 0。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EvidenceKind
    #: 🔴 **证据取不到**是一个**显式**的事实，不是"``count`` 恰好是 ``None``"。
    #:
    #: 与 S4 把"没观测到"升格成 ``observation_status`` 是同一个理由：
    #: 隐式推断的形态会让读者分不清"没读到"与"读到了空值"，也会让
    #: 一条期望值为 0 的规则在证据缺失时被填成 0 而**通过**。
    unavailable: bool = False
    count: int | None = None
    #: 完整比率：**分子、分母、值三者齐备**。
    ratio: RatioMetric | None = None
    ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.unavailable:
            if self.count is not None or self.ratio is not None or self.ids:
                msg = "证据不可用时不得携带任何观测值"
                raise ValueError(msg)
            return self
        if self.kind is EvidenceKind.COUNT and (self.count is None or self.ratio is not None):
            msg = "计数证据必须且只能带 count"
            raise ValueError(msg)
        if self.kind is EvidenceKind.RATIO and (self.ratio is None or self.count is not None):
            msg = "比率证据必须且只能带 ratio"
            raise ValueError(msg)
        if self.kind is EvidenceKind.ID_SET and (self.ratio is not None or self.count is not None):
            msg = "集合证据不得带 count 或 ratio"
            raise ValueError(msg)
        if sorted(set(self.ids)) != list(self.ids):
            msg = "ids 必须去重并稳定排序"
            raise ValueError(msg)
        return self


class ExpectedEvidence(BaseModel):
    """规则**期望**的结构化值（来自策略）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EvidenceKind
    count: int | None = None
    value: str | None = None
    numerator_equals_denominator: bool | None = None
    denominator_gt_zero: bool | None = None
    denominator_ref: ScopeCountRef | None = None
    empty: bool | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.kind is EvidenceKind.COUNT and self.count is None:
            msg = "计数期望必须带 count"
            raise ValueError(msg)
        if self.kind is EvidenceKind.RATIO and self.value is None:
            msg = "比率期望必须带 value"
            raise ValueError(msg)
        if self.kind is EvidenceKind.ID_SET and self.empty is not True:
            msg = "集合期望只支持 empty=true"
            raise ValueError(msg)
        return self


class RuleResult(BaseModel):
    """一条规则的结构化结论。

    🔴 **只放稳定结构**：没有回答正文、没有断言判定原文、没有异常堆栈、
    没有数据库信息、没有自然语言发布建议。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    rule_type: RuleType
    outcome: RuleOutcome
    evidence_key: EvidenceKey
    observed: ObservedEvidence
    expected: ExpectedEvidence
    reason_code: RuleReason

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.observed.kind is not self.expected.kind:
            msg = f"规则 {self.rule_id!r} 的观测与期望形状不一致"
            raise ValueError(msg)
        if self.outcome is RuleOutcome.PASS and self.reason_code is not RuleReason.SATISFIED:
            msg = f"规则 {self.rule_id!r} 通过了，原因码却写的是 {self.reason_code}"
            raise ValueError(msg)
        if self.outcome is not RuleOutcome.PASS and self.reason_code is RuleReason.SATISFIED:
            msg = f"规则 {self.rule_id!r} 没通过，原因码却写的是 rule_satisfied"
            raise ValueError(msg)
        return self


class GateDecisionIdentity(BaseModel):
    """这份结论**出自哪两份东西**：哪一版策略、哪一份对比。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_schema_version: int
    policy_id: str
    policy_revision: int
    policy_digest: str
    comparison_schema_version: int
    comparison_definition_digest: str
    #: 对比产物**内容**的摘要。⚠️ 它只是"同一份对比"的标识，
    #: **不是**签名，也不证明来源可信。
    comparison_fingerprint: str
    baseline_commit_sha: str | None
    candidate_commit_sha: str | None


class GateDecision(BaseModel):
    """一次门禁判定的完整产物。

    🔴 **没有发布许可字段。** 见模块文档的禁用列表。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate_decision_schema_version: int = GATE_DECISION_SCHEMA_VERSION
    gate_definition_digest: str
    identity: GateDecisionIdentity
    policy_applicable: bool
    applicability_reasons: tuple[str, ...]
    outcome: GateOutcome
    rule_results: tuple[RuleResult, ...]
    failed_rule_ids: tuple[str, ...]
    not_evaluated_rule_ids: tuple[str, ...]

    @model_validator(mode="after")
    def _check(self) -> Self:
        """🔴 这里把"结论与证据必须一致"变成**构造失败**。

        一条自相矛盾的结论（"通过"但有一条规则失败、"不适用"却给出了
        质量判断）如果能被构造出来，它迟早会被写进某份产物里。
        """
        if self.policy_applicable != (not self.applicability_reasons):
            msg = "policy_applicable 与 applicability_reasons 不自洽"
            raise ValueError(msg)

        outcomes = [rule.outcome for rule in self.rule_results]
        failed = tuple(
            rule.rule_id for rule in self.rule_results if rule.outcome is RuleOutcome.FAIL
        )
        not_evaluated = tuple(
            rule.rule_id for rule in self.rule_results if rule.outcome is RuleOutcome.NOT_EVALUATED
        )
        if failed != self.failed_rule_ids:
            msg = f"failed_rule_ids={list(self.failed_rule_ids)} 与规则结论不一致"
            raise ValueError(msg)
        if not_evaluated != self.not_evaluated_rule_ids:
            msg = f"not_evaluated_rule_ids={list(self.not_evaluated_rule_ids)} 与规则结论不一致"
            raise ValueError(msg)

        if self.outcome is GateOutcome.PASS:
            if not self.policy_applicable:
                msg = "策略不适用时不得给出 PASS"
                raise ValueError(msg)
            if not outcomes or any(item is not RuleOutcome.PASS for item in outcomes):
                msg = "PASS 要求存在规则且**全部**通过"
                raise ValueError(msg)
        elif self.outcome is GateOutcome.FAIL:
            if not self.policy_applicable:
                msg = "策略不适用时不得给出 FAIL——那是拿规则不适用去指控候选"
                raise ValueError(msg)
            if RuleOutcome.FAIL not in outcomes:
                msg = "FAIL 要求至少一条规则明确失败"
                raise ValueError(msg)
        else:  # NOT_EVALUATED
            if not self.applicability_reasons and not not_evaluated:
                msg = "NOT_EVALUATED 必须给出原因：适用性原因或未评估的规则"
                raise ValueError(msg)

        if not self.policy_applicable and self.rule_results:
            # 🔴 不适用时**一条质量规则都不许评估**：评估了就说明有人
            # 绕过适用性判定直接跑了规则，那份结论无论结果如何都是误导。
            msg = "策略不适用时不得评估任何质量规则"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------

#: 策略 JSON 的顶层字段。**与模型一一对应**。
POLICY_TOP_LEVEL_FIELDS: Final[tuple[str, ...]] = (
    "display_name",
    "policy_digest",
    "policy_id",
    "policy_revision",
    "policy_schema_version",
    "purpose",
    "rules",
    "scope",
    "supported_comparison_contract",
)


def _reject_constant(name: str) -> object:
    """拒绝 ``NaN`` / ``Infinity`` / ``-Infinity``。

    ⚠️ Python 的 ``json`` 模块默认接受它们；而 ``NaN`` 进了阈值比较就是灾难：
    ``NaN != NaN``，任何等值判断都会静默为假。
    """
    msg = f"JSON 里不允许出现 {name}（NaN / Infinity 不是合法的策略或对比内容）"
    raise PolicyInputError(msg)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝同一个对象里重复的键。

    ⚠️ 默认的 ``json`` 行为是**后者覆盖前者**，于是"策略里写了两条 scope"
    会静默变成一条。对一份要扮演契约的文件而言，这种宽容是有害的。
    """
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            msg = f"JSON 对象里出现重复的键：{key!r}"
            raise PolicyInputError(msg)
        seen.add(key)
    return dict(pairs)


def load_gate_policy(path: Path) -> GatePolicy:
    """严格加载一份门禁策略。

    校验顺序：UTF-8 → JSON（拒绝 NaN/Infinity 与重复键）→ 顶层字段集合 →
    **重算 ``policy_digest`` 并比对** → 逐字段模型（含规则契约）。

    🔴 摘要比对排在模型校验**之前**：一份内容被改过、摘要却没跟上的策略，
    应当以"这份策略不是它自称的那份"被拒绝，而不是先被一堆字段错误淹没。

    Args:
        path: 策略文件路径。

    Returns:
        已通过校验的策略。

    Raises:
        PolicyInputError: 读不了、格式不对、字段集合不对、或摘要不符。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        msg = "不是合法的 UTF-8 文本"
        raise PolicyInputError(msg) from exc
    except OSError as exc:
        msg = f"读取失败（{type(exc).__name__}）"
        raise PolicyInputError(msg) from exc

    try:
        payload = json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        msg = f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）"
        raise PolicyInputError(msg) from exc

    if not isinstance(payload, dict):
        msg = f"顶层必须是对象，实际是 {type(payload).__name__}"
        raise PolicyInputError(msg)

    unknown = sorted(set(payload) - set(POLICY_TOP_LEVEL_FIELDS))
    missing = sorted(set(POLICY_TOP_LEVEL_FIELDS) - set(payload))
    if unknown or missing:
        msg = f"策略顶层字段不符合本版本的格式（多出：{unknown or '无'}；缺少：{missing or '无'}）"
        raise PolicyInputError(msg)

    declared = payload["policy_digest"]
    if not isinstance(declared, str):
        msg = "policy_digest 必须是字符串"
        raise PolicyInputError(msg)
    recomputed = policy_digest(payload)
    if declared != recomputed:
        msg = (
            "policy_digest 与策略内容不符："
            f"文件里写的是 {declared}，按内容算出的是 {recomputed}。"
            "⚠️ 改过策略就必须重算摘要——沿用旧摘要等于宣称它是另一份策略"
        )
        raise PolicyInputError(msg)

    try:
        return GatePolicy.model_validate(payload)
    except ValidationError as exc:
        msg = f"策略结构校验失败：{exc.error_count()} 处（第一处：{_first_error(exc)}）"
        raise PolicyInputError(msg) from exc


def _first_error(exc: ValidationError) -> str:
    """取第一处校验错误的一句话描述。

    🔴 带上 ``msg``：本模块的校验器消息只由**字段名与常量**组成
    （不嵌输入值），因此它可以直接给使用者看。只报一个 ``value_error``
    等于告诉对方"哪儿不对，但我不说"，而策略是一份要被人改的文件。
    """
    errors = exc.errors()
    if not errors:
        return "未提供细节"
    first = errors[0]
    # 模型级校验器的 ``loc`` 是空的，用 ``policy`` 占位免得显示成 ": ..."。
    location = ".".join(str(part) for part in first.get("loc", ())) or "policy"
    return f"{location}: {first.get('msg', 'unknown')}"


# ---------------------------------------------------------------------------
# 适用性
# ---------------------------------------------------------------------------


def _resolve_scope_count(policy: GatePolicy, ref: ScopeCountRef) -> int | None:
    """把作用域计数引用解析成具体数值。"""
    if ref is ScopeCountRef.DATASET_CASE_COUNT:
        return policy.scope.dataset_case_count
    if ref is ScopeCountRef.EXPECTED_ASSERTION_COUNT:
        return policy.scope.expected_assertion_count
    return None


def evaluate_policy_applicability(
    comparison: EvaluationComparison, policy: GatePolicy
) -> PolicyApplicability:
    """判定这份策略适不适用于这份对比。

    🔴 **对照的是双方身份，不是按文件名或路径猜的**。任何一个字段对不上
    都只说明"这份策略不管这类评测"，因此结论是**不适用**——
    绝不可判成 ``FAIL``（那是拿规则不适用去指控候选）。

    Args:
        comparison: 已通过严格加载的对比产物。
        policy: 已通过严格加载的策略。

    Returns:
        适用性结论；不适用时带稳定排序的原因码。
    """
    reasons: set[str] = set()

    if not comparison.comparison_eligible:
        # 🔴 不可比较的输入**没有合法的指标 delta**，因此不存在"适用"的前提。
        reasons.add(ApplicabilityReason.COMPARISON_NOT_ELIGIBLE.value)

    contract = policy.supported_comparison_contract
    if (
        comparison.comparison_schema_version != contract.comparison_schema_version
        or comparison.comparison_definition_digest != contract.comparison_definition_digest
    ):
        reasons.add(ApplicabilityReason.COMPARISON_CONTRACT_UNSUPPORTED.value)

    # ---- 双方身份逐项对照作用域 ----
    #
    # ⚠️ 两侧都要在范围内：Baseline 不在范围内，说明这份策略针对的
    # 实验条件与这次对比无关，得到的"差异"也就无从解释。
    boundaries = (
        (policy.scope.dataset_digest, ApplicabilityReason.DATASET_OUT_OF_SCOPE),
        (policy.scope.dataset_case_count, ApplicabilityReason.DATASET_CASE_COUNT_OUT_OF_SCOPE),
        (
            policy.scope.assertion_registry_digest,
            ApplicabilityReason.ASSERTION_REGISTRY_OUT_OF_SCOPE,
        ),
        (policy.scope.prompt_digest, ApplicabilityReason.PROMPT_OUT_OF_SCOPE),
        (policy.scope.provider, ApplicabilityReason.PROVIDER_OUT_OF_SCOPE),
        (policy.scope.model, ApplicabilityReason.MODEL_OUT_OF_SCOPE),
        (
            policy.scope.provider_configuration_digest,
            ApplicabilityReason.PROVIDER_CONFIGURATION_OUT_OF_SCOPE,
        ),
        (policy.scope.execution_mode, ApplicabilityReason.EXECUTION_MODE_OUT_OF_SCOPE),
        (policy.scope.storage_backend, ApplicabilityReason.STORAGE_BACKEND_OUT_OF_SCOPE),
        (policy.scope.migration_revision, ApplicabilityReason.MIGRATION_REVISION_OUT_OF_SCOPE),
    )
    for expected, reason in boundaries:
        for side in (comparison.baseline, comparison.candidate):
            actual = _identity_value(side, reason)
            if actual != expected:
                reasons.add(reason.value)

    for side in (comparison.baseline, comparison.candidate):
        if (
            side.metrics_schema_version != policy.scope.metrics_schema_version
            or side.metrics_definition_digest != policy.scope.metrics_definition_digest
        ):
            reasons.add(ApplicabilityReason.METRICS_CONTRACT_OUT_OF_SCOPE.value)

    for side in (comparison.baseline, comparison.candidate):
        if side.commit_sha is None:
            # 少了提交号就没法把这套规则绑到一段具体的代码上。
            reasons.add(ApplicabilityReason.COMPARISON_IDENTITY_INCOMPLETE.value)

    return PolicyApplicability(applicable=not reasons, reasons=tuple(sorted(reasons)))


def _identity_value(side: Any, reason: ApplicabilityReason) -> object:
    """按原因码取出对应的身份值。

    ⚠️ 这张表与上面的 ``boundaries`` 必须一一对应；用原因码当键而不是
    按顺序取，是为了让"加了 scope 项却忘了对照"变成一次**缺失键**，
    而不是一次静默的错位。
    """
    table: dict[ApplicabilityReason, Any] = {
        ApplicabilityReason.DATASET_OUT_OF_SCOPE: side.dataset_digest,
        ApplicabilityReason.DATASET_CASE_COUNT_OUT_OF_SCOPE: side.dataset_case_count,
        ApplicabilityReason.ASSERTION_REGISTRY_OUT_OF_SCOPE: side.assertion_registry_digest,
        ApplicabilityReason.PROMPT_OUT_OF_SCOPE: side.prompt_versions_digest,
        ApplicabilityReason.PROVIDER_OUT_OF_SCOPE: side.provider_name,
        ApplicabilityReason.MODEL_OUT_OF_SCOPE: side.model_id,
        ApplicabilityReason.PROVIDER_CONFIGURATION_OUT_OF_SCOPE: (
            side.provider_configuration_digest
        ),
        ApplicabilityReason.EXECUTION_MODE_OUT_OF_SCOPE: side.result_execution_mode,
        ApplicabilityReason.STORAGE_BACKEND_OUT_OF_SCOPE: side.storage_backend,
        ApplicabilityReason.MIGRATION_REVISION_OUT_OF_SCOPE: side.alembic_revision,
    }
    return table[reason]


# ---------------------------------------------------------------------------
# 证据
# ---------------------------------------------------------------------------


def _observed(comparison: EvaluationComparison, key: EvidenceKey) -> ObservedEvidence | None:
    """取一条证据；取不到时返回 ``None``（**不是 0**）。

    🔴 缺失证据与"观测到 0"是两回事。把前者当成 0，会让一条读不到值的
    规则**通过**——那正是"未评估当成通过"最隐蔽的形态。
    """
    metrics = comparison.metrics_comparison
    cases_delta = None if metrics is None else metrics.cases
    failure_delta = comparison.failure_index_delta

    ratio_table: dict[EvidenceKey, RatioMetric | None] = {
        EvidenceKey.CANDIDATE_CASE_PASS_RATE: (
            None if cases_delta is None else cases_delta.case_pass_rate.candidate
        ),
        EvidenceKey.CANDIDATE_EXECUTION_COVERAGE: (
            None if cases_delta is None else cases_delta.execution_coverage.candidate
        ),
        EvidenceKey.CANDIDATE_ASSERTION_PASS_RATE: (
            None if metrics is None else metrics.assertions_overall.pass_rate.candidate
        ),
        EvidenceKey.CANDIDATE_OBSERVATION_COVERAGE: (
            None if metrics is None else metrics.assertions_overall.observation_coverage.candidate
        ),
    }
    count_table: dict[EvidenceKey, int | None] = {
        EvidenceKey.CANDIDATE_EXECUTION_ERROR_CASES: (
            None if cases_delta is None else cases_delta.execution_error_cases.candidate
        ),
        EvidenceKey.CANDIDATE_NOT_EXECUTED_CASES: (
            None if cases_delta is None else cases_delta.not_executed_cases.candidate
        ),
        EvidenceKey.CANDIDATE_FAILED_ASSERTIONS: (
            None if metrics is None else metrics.assertions_overall.failed.candidate
        ),
        EvidenceKey.CANDIDATE_UNOBSERVABLE_ASSERTIONS: (
            None if metrics is None else metrics.assertions_overall.unobservable.candidate
        ),
    }
    set_table: dict[EvidenceKey, tuple[str, ...] | None] = {
        EvidenceKey.FAILURE_NEWLY_FAILED_CASE_IDS: (
            None if failure_delta is None else failure_delta.newly_failed_case_ids
        ),
        EvidenceKey.FAILURE_NEWLY_EXECUTION_ERROR_CASE_IDS: (
            None if failure_delta is None else failure_delta.newly_execution_error_case_ids
        ),
        EvidenceKey.FAILURE_NEWLY_UNOBSERVABLE_CASE_IDS: (
            None if failure_delta is None else failure_delta.newly_unobservable_case_ids
        ),
    }
    case_summary = comparison.case_transition_summary
    assertion_summary = comparison.assertion_transition_summary
    transition_table: dict[EvidenceKey, int | None] = {
        EvidenceKey.CASE_REGRESSION_TRANSITION_COUNT: (
            None if case_summary is None else case_summary.regression_transition_count
        ),
        EvidenceKey.ASSERTION_REGRESSION_TRANSITION_COUNT: (
            None if assertion_summary is None else assertion_summary.regression_transition_count
        ),
        EvidenceKey.ASSERTION_CHANGED_UNRESOLVED_COUNT: (
            None if assertion_summary is None else assertion_summary.changed_unresolved_count
        ),
    }

    if key in ratio_table:
        ratio = ratio_table[key]
        return None if ratio is None else ObservedEvidence(kind=EvidenceKind.RATIO, ratio=ratio)
    if key in set_table:
        ids = set_table[key]
        return (
            None
            if ids is None
            else ObservedEvidence(kind=EvidenceKind.ID_SET, ids=tuple(sorted(ids)))
        )
    count = count_table.get(key, transition_table.get(key))
    if count is None:
        return None
    return ObservedEvidence(kind=EvidenceKind.COUNT, count=count)


def _expected(rule: PolicyRule) -> ExpectedEvidence:
    """把规则里声明的期望规范化成结构化值。"""
    if rule.rule_type is RuleType.ID_SET_EMPTY:
        return ExpectedEvidence(kind=EvidenceKind.ID_SET, empty=True)
    if rule.rule_type is RuleType.RATIO_EQUALS:
        expectation = rule.expected
        return ExpectedEvidence(
            kind=EvidenceKind.RATIO,
            value=expectation.value,
            numerator_equals_denominator=expectation.numerator_equals_denominator,
            denominator_gt_zero=expectation.denominator_gt_zero,
            denominator_ref=expectation.denominator_ref,
        )
    return ExpectedEvidence(kind=EvidenceKind.COUNT, count=rule.expected)


# ---------------------------------------------------------------------------
# 规则评估
# ---------------------------------------------------------------------------


def _unevaluated(rule: PolicyRule, reason: RuleReason) -> RuleResult:
    """证据取不到时的规则结论。

    🔴 观测值留空（``count=None`` / ``ids=()``）而**不是**填 0——
    填 0 会让这条规则看起来"评估过了，只是没通过"，或者更糟：
    在期望值恰好是 0 的规则上**通过**。
    """
    expected = _expected(rule)
    empty = ObservedEvidence(kind=expected.kind, unavailable=True)
    return RuleResult(
        rule_id=rule.rule_id,
        rule_type=rule.rule_type,
        outcome=RuleOutcome.NOT_EVALUATED,
        evidence_key=rule.evidence_key,
        observed=empty,
        expected=expected,
        reason_code=reason,
    )


def _evaluate_ratio_rule(
    rule: RatioEqualsRule, observed: ObservedEvidence, policy: GatePolicy
) -> tuple[RuleOutcome, RuleReason]:
    """比率规则：值、分子分母关系、分母下界、分母对齐作用域，**逐项都查**。

    ⚠️ 只比 ``value`` 会漏掉"分母悄悄缩小了"：``5/5`` 与 ``10/10``
    的 ``value`` 都是 ``1.000000``。所以分子必须等于分母、分母必须
    大于 0、而且必须等于作用域里那个具名计数。
    """
    ratio = observed.ratio
    if ratio is None or ratio.value is None:
        # 分母为 0 的比率在 S4 里就是 ``null``——它**不是**"通过了"。
        return RuleOutcome.NOT_EVALUATED, RuleReason.EVIDENCE_UNAVAILABLE

    expectation = rule.expected
    if Decimal(ratio.value) != Decimal(expectation.value):
        return RuleOutcome.FAIL, RuleReason.RATIO_MISMATCH
    if expectation.numerator_equals_denominator and ratio.numerator != ratio.denominator:
        return RuleOutcome.FAIL, RuleReason.RATIO_MISMATCH
    if expectation.denominator_gt_zero and ratio.denominator <= 0:
        return RuleOutcome.FAIL, RuleReason.DENOMINATOR_MISMATCH
    required = _resolve_scope_count(policy, expectation.denominator_ref)
    if required is not None and ratio.denominator != required:
        return RuleOutcome.FAIL, RuleReason.DENOMINATOR_MISMATCH
    return RuleOutcome.PASS, RuleReason.SATISFIED


def _evaluate_rule(
    rule: PolicyRule, comparison: EvaluationComparison, policy: GatePolicy
) -> RuleResult:
    """评估一条规则。"""
    observed = _observed(comparison, rule.evidence_key)
    if observed is None:
        return _unevaluated(rule, RuleReason.EVIDENCE_UNAVAILABLE)

    expected = _expected(rule)
    outcome: RuleOutcome
    reason: RuleReason

    if rule.rule_type is RuleType.ID_SET_EMPTY:
        if observed.ids:
            outcome, reason = RuleOutcome.FAIL, RuleReason.UNEXPECTED_IDS_PRESENT
        else:
            outcome, reason = RuleOutcome.PASS, RuleReason.SATISFIED
    elif rule.rule_type is RuleType.RATIO_EQUALS:
        outcome, reason = _evaluate_ratio_rule(rule, observed, policy)
    else:
        # ``count_equals`` 与 ``transition_count_equals`` 的判定相同，
        # 区别在它们各自允许的证据键集合（由策略模型保证，见 _check_rule_contract）。
        if observed.count == rule.expected:
            outcome, reason = RuleOutcome.PASS, RuleReason.SATISFIED
        else:
            outcome, reason = RuleOutcome.FAIL, RuleReason.COUNT_MISMATCH

    return RuleResult(
        rule_id=rule.rule_id,
        rule_type=rule.rule_type,
        outcome=outcome,
        evidence_key=rule.evidence_key,
        observed=observed,
        expected=expected,
        reason_code=reason,
    )


def _aggregate(results: tuple[RuleResult, ...]) -> GateOutcome:
    """把规则结论聚合成门禁结论。

    🔴 顺序**不能**换：只要有规则没被评估，结论就是 ``NOT_EVALUATED``，
    哪怕别的规则明确失败了也不能降级成 ``FAIL``。理由是这两者对使用者的
    含义不同——``FAIL`` 是"查过了，确实不合格"，而
    ``NOT_EVALUATED`` 是"这次没能完整地查"。把后者说成前者是在编造结论。
    """
    outcomes = {rule.outcome for rule in results}
    if RuleOutcome.NOT_EVALUATED in outcomes:
        return GateOutcome.NOT_EVALUATED
    if RuleOutcome.FAIL in outcomes:
        return GateOutcome.FAIL
    return GateOutcome.PASS


def comparison_fingerprint(comparison: EvaluationComparison) -> str:
    """对比产物**内容**的摘要。

    ⚠️ 它只是"同一份对比"的标识符，**不是**签名，也不证明任何来源可信。
    基于规范化后的产物内容计算，因此同一份对比在不同机器上得到同一个值；
    不含文件路径、不含时间。
    """
    text = dumps(comparison.model_dump(mode="json"))
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _identity(comparison: EvaluationComparison, policy: GatePolicy) -> GateDecisionIdentity:
    return GateDecisionIdentity(
        policy_schema_version=policy.policy_schema_version,
        policy_id=policy.policy_id,
        policy_revision=policy.policy_revision,
        policy_digest=policy.policy_digest,
        comparison_schema_version=comparison.comparison_schema_version,
        comparison_definition_digest=comparison.comparison_definition_digest,
        comparison_fingerprint=comparison_fingerprint(comparison),
        baseline_commit_sha=comparison.baseline.commit_sha,
        candidate_commit_sha=comparison.candidate.commit_sha,
    )


def decide(comparison: EvaluationComparison, policy: GatePolicy) -> GateDecision:
    """对一份对比与一份策略做出门禁判定。

    🔴 **先判适用性，再判规则。** 策略不适用时一条质量规则都不跑——
    跑了就会产出一份"看起来评估过了"的结论，而它回答的不是这份策略
    有权回答的问题。

    Args:
        comparison: 已通过严格加载的对比产物。
        policy: 已通过严格加载的策略。

    Returns:
        结构化门禁结论。
    """
    applicability = evaluate_policy_applicability(comparison, policy)
    identity = _identity(comparison, policy)

    if not applicability.applicable:
        return GateDecision(
            gate_definition_digest=gate_definition_digest(),
            identity=identity,
            policy_applicable=False,
            applicability_reasons=applicability.reasons,
            outcome=GateOutcome.NOT_EVALUATED,
            rule_results=(),
            failed_rule_ids=(),
            not_evaluated_rule_ids=(),
        )

    results = tuple(_evaluate_rule(rule, comparison, policy) for rule in policy.rules)
    outcome = _aggregate(results)

    # 🔴 规则层面没能评估完整时，``policy_applicable`` 仍然是 ``True``。
    #
    # "策略不适用"与"策略适用但这次判不完"是**两件事**：前者说这份策略
    # 管不着这类评测，后者说它管得着、只是证据没凑齐。把后者也标成
    # 不适用，等于把"这次没查完"写成了"这不归我管"。
    # NOT_EVALUATED 的"为什么"由 ``not_evaluated_rule_ids`` 逐条给出。
    return GateDecision(
        gate_definition_digest=gate_definition_digest(),
        identity=identity,
        policy_applicable=True,
        applicability_reasons=(),
        outcome=outcome,
        rule_results=results,
        failed_rule_ids=tuple(rule.rule_id for rule in results if rule.outcome is RuleOutcome.FAIL),
        not_evaluated_rule_ids=tuple(
            rule.rule_id for rule in results if rule.outcome is RuleOutcome.NOT_EVALUATED
        ),
    )


# ---------------------------------------------------------------------------
# 写出
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写：先写同目录临时文件，再 ``os.replace`` 顶替目标。

    🔴 **不用"直接打开目标文件写"**：那条路径在中途失败时会留下一个
    半截的产物，而半截的门禁结论比没有结论更危险——它读起来像一份完整的
    结论。``os.replace`` 在同一文件系统内是原子的，因此读者要么看到旧内容，
    要么看到完整的新内容。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def write_decision(decision: GateDecision, path: Path) -> None:
    """把门禁结论原子地写到磁盘。

    与 S1a—S5 的产物用**同一个** :func:`~ai_psi.evaluation.serialization.dumps`：
    UTF-8、键排序、缩进固定、结尾恰好一个换行。同样两份输入跑两次，
    产物因此逐字节一致。

    Args:
        decision: 门禁结论。
        path: 输出路径。

    Raises:
        OSError: 写盘失败。**不吞掉**——"看起来判完了却没有产物"是最坏的结果。
    """
    _atomic_write_text(path, dumps(decision.model_dump(mode="json")))


#: 供 CLI 复用的输入错误类型（对比与策略都归到"输入错误"退出码）。
InputError = ComparisonInputError | PolicyInputError
