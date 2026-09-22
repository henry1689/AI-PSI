"""结构化评测资格判定（阶段 8 · S8）。

## 它回答什么

> 在指定的 ``EvaluationQualificationPolicy`` 下，这条**已经被重新验证过**的
> 评测证据链，是否具备被后续人工审查或 CI 质量流程消费的资格？

## 🔴 它不回答什么

是否允许合并、是否允许部署、是否允许生产发布、是否满足法规审计、
文件是否来自可信主体、是否有数字签名、分支是否受保护、Candidate 是否
具备生产质量、真实 Provider 是否通过、是否应覆盖人工审批。

:class:`EvaluationQualificationDecision` 里**没有** ``release_allowed`` /
``deploy_allowed`` / ``merge_allowed`` / ``production_ready`` /
``auto_merge`` / ``auto_deploy`` / ``approved_for_release`` /
``release_recommendation`` / ``quality_score`` / ``risk_score`` /
``trust_score`` 这些字段——不是"暂时没填"，是模型一律 ``extra="forbid"``，
多写一个就是校验失败。

**``QUALIFIED`` 不是发布许可。** 它只表示"在当前策略的作用域内，这条链
已被重新验证，且它的门禁结论满足该策略规定的最低资格条件"。

## 四个必须分开的维度

============================  ================================================
维度                           由谁决定，回答什么
============================  ================================================
``gate_outcome``              S6——质量门禁规则过不过
``verification_outcome``      S7——证据链一不一致、能不能重算
``qualification_outcome``     S8——**前两者 + 作用域**聚合出的资格
release authorization         **S8 不提供**
============================  ================================================

把前三者混成一个，就会得到一个"因为证据坏了所以候选不合格"的工具，
或者一个"证据根本读不出来却给了合格"的工具。

## 🔴 S8 从不相信磁盘上的 ``VerificationReport``

它**自己调用** S7 的纯函数
:func:`~ai_psi.evaluation.evidence.verify_evidence_bundle` 重新验证一遍，
只用**本次调用返回**的报告。CLI 没有 ``--verification-report`` 参数，
没有 ``--trust-verification``，也没有任何"跳过验证"的开关。

## 它不碰外部世界

不运行评测、不调用 Provider、不连数据库、不读网络、不通过子进程驱动任何
CLI、不生成 S5／S6／S7 的产物。它只读文件、跑纯函数、写一份 JSON。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ai_psi.evaluation.comparison import (
    ComparisonInputError,
    EvaluationComparison,
    load_comparison,
)
from ai_psi.evaluation.evidence import (
    EvaluationEvidenceBundle,
    EvidenceBundleInvariantError,
    EvidenceInputError,
    EvidenceInputs,
    EvidenceVerificationInputs,
    EvidenceVerificationReport,
    VerificationOutcome,
    load_evidence_bundle,
    verify_evidence_bundle,
)
from ai_psi.evaluation.gate import (
    GateDecision,
    GateDecisionInputError,
    GateOutcome,
    load_gate_decision,
)
from ai_psi.evaluation.serialization import dumps

__all__ = [
    "QUALIFICATION_CHECKS",
    "QUALIFICATION_DECISION_SCHEMA_VERSION",
    "QUALIFICATION_DECISION_TOP_LEVEL_FIELDS",
    "QUALIFICATION_DEFINITION",
    "QUALIFICATION_POLICY_DEFINITION",
    "QUALIFICATION_POLICY_SCHEMA_VERSION",
    "QUALIFICATION_POLICY_TOP_LEVEL_FIELDS",
    "AllowedOutcomes",
    "EvaluationQualificationDecision",
    "EvaluationQualificationOutcome",
    "EvaluationQualificationPolicy",
    "QualificationApplicabilityStatus",
    "QualificationCheck",
    "QualificationCheckOutcome",
    "QualificationDecisionIdentity",
    "QualificationEvidenceIdentity",
    "QualificationGateIdentity",
    "QualificationInputError",
    "QualificationInputs",
    "QualificationPolicyApplicability",
    "QualificationPolicyApplicabilityReason",
    "QualificationPolicyIdentity",
    "QualificationPolicyScope",
    "QualificationReason",
    "build_qualification_decision",
    "evaluate_policy_applicability",
    "load_qualification_decision",
    "load_qualification_policy",
    "parse_qualification_decision",
    "parse_qualification_policy",
    "qualification_decision_digest",
    "qualification_decision_payload",
    "qualification_definition_digest",
    "qualification_policy_definition_digest",
    "qualification_policy_digest",
    "write_qualification_decision",
]

#: 资格策略自身的版本。
QUALIFICATION_POLICY_SCHEMA_VERSION: Final[int] = 1

#: 资格结论产物自身的版本。
QUALIFICATION_DECISION_SCHEMA_VERSION: Final[int] = 1

#: ``sha256:<64 位小写十六进制>``。
_DIGEST_PATTERN: Final[str] = r"^sha256:[0-9a-f]{64}$"

#: 稳定标识符：小写字母开头，只含小写字母、数字与下划线。
_ID_PATTERN: Final[str] = r"^[a-z][a-z0-9_]*$"


def _digest(payload: object) -> str:
    """对任意**已规范化**的结构取 SHA-256。"""
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


# ---------------------------------------------------------------------------
# 检查项固定顺序
# ---------------------------------------------------------------------------

#: 资格检查项的**固定顺序**。它进 :data:`QUALIFICATION_DEFINITION`。
QUALIFICATION_CHECKS: Final[tuple[str, ...]] = (
    "evidence_verification_completed",
    "evidence_verification_verified",
    "qualification_policy_schema_supported",
    "qualification_policy_digest_valid",
    "qualification_policy_applicable",
    "evidence_bundle_definition_allowed",
    "verification_definition_allowed",
    "comparison_definition_allowed",
    "gate_definition_allowed",
    "gate_policy_identity_allowed",
    "experiment_identity_allowed",
    "partial_run_absent",
    "gate_outcome_eligible",
)


# ---------------------------------------------------------------------------
# 定义身份
# ---------------------------------------------------------------------------


#: **资格策略**定义的语义摘要输入。
#:
#: 🔴 它回答的是"这份策略是**按什么规则**说自己管不管得着"。
QUALIFICATION_POLICY_DEFINITION: Final[dict[str, object]] = {
    "applicability": (
        "把证据链、对比与门禁结论的结构化身份逐项对照策略 scope；"
        "任一不匹配即 NOT_APPLICABLE，并给出稳定的原因码"
    ),
    "applicability_statuses": ["APPLICABLE", "NOT_APPLICABLE", "NOT_EVALUATED"],
    "digest": (
        "qualification_policy_digest 覆盖**除它自己以外**的全部字段；"
        "规范化 JSON（键排序、紧凑分隔符）取 SHA-256；自排除"
    ),
    "no_scope_relaxation": (
        "策略不适用时**不**自动换一份策略、**不**放宽作用域、**不**只凭 "
        "policy_id 判定适用；结论为 NOT_EVALUATED"
    ),
    "scope_identity_binding": (
        "首个策略固定绑定已封存的 S6 ``s6_mock_golden`` revision 1 与 "
        "Mock Golden 实验身份；不接受真实 Provider、未知 Provider、未知模型、"
        "partial_run 或未验证证据"
    ),
    "schema_version": QUALIFICATION_POLICY_SCHEMA_VERSION,
}


def qualification_policy_definition_digest() -> str:
    """资格策略定义的语义摘要。

    🔴 **改作用域、可适用性规则或聚合口径必须让它变**：沿用旧摘要等于
    宣称"这两份策略是按同一套规则判的"，而它们不是。
    """
    return _digest(QUALIFICATION_POLICY_DEFINITION)


#: 资格结论定义的语义摘要输入。
QUALIFICATION_DEFINITION: Final[dict[str, object]] = {
    "aggregation": {
        "all_checks_pass": "QUALIFIED",
        "any_check_fail": "DISQUALIFIED",
        "any_check_not_evaluated": "NOT_EVALUATED（且没有任何 FAIL）",
    },
    "check_order": list(QUALIFICATION_CHECKS),
    "evidence_invalid": (
        "S7 verification_outcome=INVALID ⇒ 对应检查 FAIL ⇒ DISQUALIFIED；"
        "**不是**输入解析失败，CLI 用 DISQUALIFIED 的业务退出码"
    ),
    "evidence_not_verifiable": (
        "S7 verification_outcome=NOT_VERIFIABLE ⇒ 对应检查 NOT_EVALUATED ⇒ NOT_EVALUATED"
    ),
    "forbidden_output": (
        "不得含 release_allowed / deploy_allowed / merge_allowed / production_ready / "
        "auto_merge / auto_deploy / approved_for_release / release_recommendation / "
        "quality_score / risk_score / trust_score，也不得含自由文本 recommendation"
    ),
    "gate_outcome": {
        "FAIL": "对应检查 FAIL ⇒ DISQUALIFIED",
        "NOT_EVALUATED": "对应检查 NOT_EVALUATED ⇒ 整体 NOT_EVALUATED（未评估 ≠ 不成立）",
        "PASS": "在策略允许集合内则 PASS",
    },
    "not_a_release_authorization": (
        "QUALIFIED 只表示'在该策略作用域内、证据已重新验证、门禁结论达标'，"
        "不表示可以合并、部署或发布，也不证明来源真实性"
    ),
    "not_evaluated_checks": (
        "上游检查无法完成时，下游为 NOT_EVALUATED；**绝不**把未执行的检查标成 PASS"
    ),
    "policy_applicability": (
        "仅在 S7 判定 VERIFIED、策略 Schema 受支持、且三组身份都读得出来时才安全计算；"
        "否则整体 NOT_EVALUATED，且**不得**从不可信证据推导出 APPLICABLE"
    ),
    "reverification": (
        "本层**自己调用** S7 纯函数 verify_evidence_bundle 重新验证；"
        "不接受 --verification-report 参数、不信任磁盘上的旧报告、"
        "不通过子进程驱动 CLI、不从 stdout/stderr 解析结果"
    ),
    "schema_version": QUALIFICATION_DECISION_SCHEMA_VERSION,
    "scope_mismatch": (
        "作用域不匹配记作 NOT_EVALUATED 而不是 FAIL：那是'这份策略管不着这类评测'，"
        "不是'这条链有问题'"
    ),
    "sorting": (
        "checks 按固定顺序；reason_codes 去重后按字典序；applicability.reasons 去重后按字典序"
    ),
}


def qualification_definition_digest() -> str:
    """资格结论定义的语义摘要（含检查项固定顺序与聚合规则）。"""
    return _digest(QUALIFICATION_DEFINITION)


# ---------------------------------------------------------------------------
# 输入错误
# ---------------------------------------------------------------------------


class QualificationInputError(ValueError):
    """一份资格策略或资格结论**读不出来**或结构不合法。

    ⚠️ 它**不是**"这条链没资格"。这个异常意味着连读都没读成：JSON 坏了、
    顶层字段不对、摘要不符。此时**不产生**任何 QualificationDecision——
    一份基于未解析输入的"资格结论"无论长什么样都是误导。
    """


def _reject_constant(name: str) -> object:
    """拒绝 ``NaN`` / ``Infinity`` / ``-Infinity``。"""
    msg = f"JSON 里不允许出现 {name}（NaN / Infinity 不是合法的策略或结论内容）"
    raise QualificationInputError(msg)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝同一个对象里重复的键。

    ⚠️ 默认 ``json`` 的行为是**后者覆盖前者**，于是"策略里写了两遍 scope"
    会静默变成一遍。对一份要扮演契约的文件而言，那种宽容是有害的。
    """
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            msg = f"JSON 对象里出现重复的键：{key!r}"
            raise QualificationInputError(msg)
        seen.add(key)
    return dict(pairs)


def _decode_json(data: bytes) -> dict[str, Any]:
    """UTF-8 → 严格 JSON → 对象。"""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = "不是合法的 UTF-8 文本"
        raise QualificationInputError(msg) from exc

    if text.startswith("﻿"):
        # 🔴 BOM 会让顶层的第一个键前多出一个不可见字符，于是字段集合莫名
        # 对不上。仓库的产物一律不带 BOM，所以这里**拒绝**而不是容忍。
        msg = "JSON 不允许以 BOM 开头"
        raise QualificationInputError(msg)

    try:
        payload = json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        msg = f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）"
        raise QualificationInputError(msg) from exc

    if not isinstance(payload, dict):
        msg = f"顶层必须是对象，实际是 {type(payload).__name__}"
        raise QualificationInputError(msg)
    return payload


def _first_error(exc: ValidationError) -> str:
    """取第一处校验错误的一句话描述。

    🔴 带上 ``msg``：本模块的校验器消息只由**字段名与常量**组成（不嵌输入值），
    因此它可以直接给使用者看。
    """
    errors = exc.errors()
    if not errors:
        return "未提供细节"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ())) or "input"
    return f"{location}: {first.get('msg', 'unknown')}"


# ---------------------------------------------------------------------------
# 闭合结果集
# ---------------------------------------------------------------------------


class EvaluationQualificationOutcome(StrEnum):
    """评测资格的闭合结果集。

    🔴 与 ``GateOutcome``（门禁）和 ``VerificationOutcome``（证据）是
    **三个不同的维度**，谁也不从谁推导（除了本模块显式定义的聚合规则）。
    """

    QUALIFIED = "QUALIFIED"
    DISQUALIFIED = "DISQUALIFIED"
    NOT_EVALUATED = "NOT_EVALUATED"


class QualificationCheckOutcome(StrEnum):
    """单项资格检查的结论。闭合集合。"""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUATED = "NOT_EVALUATED"


class QualificationApplicabilityStatus(StrEnum):
    """QualificationPolicy 对这条链**管不管得着**。

    ⚠️ 第三个取值不是随手加的：**"这次判不了它管不管得着"** 与"它不归我管"
    是两件事。前者被强行写成 ``NOT_APPLICABLE`` 就是一句无法支撑的断言，
    被写成 ``APPLICABLE`` 则会让不可信证据溜进作用域判定。
    """

    APPLICABLE = "APPLICABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    NOT_EVALUATED = "NOT_EVALUATED"


class QualificationPolicyApplicabilityReason(StrEnum):
    """策略**不适用**（或判不了是否适用）的原因码。闭合集合，稳定输出。"""

    EVIDENCE_BUNDLE_SCHEMA_OUT_OF_SCOPE = "evidence_bundle_schema_out_of_scope"
    EVIDENCE_BUNDLE_DEFINITION_OUT_OF_SCOPE = "evidence_bundle_definition_out_of_scope"
    VERIFICATION_DEFINITION_OUT_OF_SCOPE = "verification_definition_out_of_scope"
    COMPARISON_SCHEMA_OUT_OF_SCOPE = "comparison_schema_out_of_scope"
    COMPARISON_DEFINITION_OUT_OF_SCOPE = "comparison_definition_out_of_scope"
    GATE_SCHEMA_OUT_OF_SCOPE = "gate_schema_out_of_scope"
    GATE_DEFINITION_OUT_OF_SCOPE = "gate_definition_out_of_scope"
    GATE_POLICY_OUT_OF_SCOPE = "gate_policy_out_of_scope"
    DATASET_OUT_OF_SCOPE = "dataset_out_of_scope"
    ASSERTION_REGISTRY_OUT_OF_SCOPE = "assertion_registry_out_of_scope"
    PROVIDER_OUT_OF_SCOPE = "provider_out_of_scope"
    MODEL_OUT_OF_SCOPE = "model_out_of_scope"
    EXECUTION_MODE_OUT_OF_SCOPE = "execution_mode_out_of_scope"
    STORAGE_BACKEND_OUT_OF_SCOPE = "storage_backend_out_of_scope"
    PARTIAL_RUN = "partial_run"
    ASSESSMENT_NOT_REACHED = "assessment_not_reached"


class QualificationReason(StrEnum):
    """资格**检查项**的原因码。闭合集合，稳定输出。"""

    SATISFIED = "satisfied"
    # ---- 证据 ----
    EVIDENCE_VERIFICATION_INVALID = "evidence_verification_invalid"
    EVIDENCE_VERIFICATION_NOT_VERIFIABLE = "evidence_verification_not_verifiable"
    # ---- 策略自身 ----
    POLICY_SCHEMA_UNSUPPORTED = "qualification_policy_schema_unsupported"
    # ---- 作用域 ----
    POLICY_NOT_APPLICABLE = "qualification_policy_not_applicable"
    EVIDENCE_BUNDLE_DEFINITION_NOT_ALLOWED = "evidence_bundle_definition_not_allowed"
    VERIFICATION_DEFINITION_NOT_ALLOWED = "verification_definition_not_allowed"
    COMPARISON_DEFINITION_NOT_ALLOWED = "comparison_definition_not_allowed"
    GATE_DEFINITION_NOT_ALLOWED = "gate_definition_not_allowed"
    GATE_POLICY_IDENTITY_NOT_ALLOWED = "gate_policy_identity_not_allowed"
    EXPERIMENT_IDENTITY_NOT_ALLOWED = "experiment_identity_not_allowed"
    PARTIAL_RUN_PRESENT = "partial_run_present"
    # ---- 门禁结论 ----
    GATE_OUTCOME_NOT_ELIGIBLE = "gate_outcome_not_eligible"
    GATE_OUTCOME_NOT_EVALUATED = "gate_outcome_not_evaluated"
    # ---- 未到达 ----
    NOT_REACHED = "not_reached"


#: 通过时唯一允许的原因码。
_SATISFIED_REASONS: Final[frozenset[QualificationReason]] = frozenset(
    {QualificationReason.SATISFIED}
)

#: 🔴 ``FAIL`` 只能是这两条——``FAIL`` 的含义是"这条链**确定**不合资格"。
#: 把"读不出来""作用域不匹配""门禁没判成"塞进来，就会得到那个最糟的工具：
#: 一个"因为没法判断所以判不合格"的资格层。
_MISMATCH_REASONS: Final[frozenset[QualificationReason]] = frozenset(
    {
        QualificationReason.EVIDENCE_VERIFICATION_INVALID,
        QualificationReason.GATE_OUTCOME_NOT_ELIGIBLE,
    }
)

#: ``NOT_EVALUATED`` 只能是这些——全部是"这次没得到结论"。
_UNAVAILABLE_REASONS: Final[frozenset[QualificationReason]] = frozenset(
    {
        QualificationReason.EVIDENCE_VERIFICATION_NOT_VERIFIABLE,
        QualificationReason.POLICY_SCHEMA_UNSUPPORTED,
        QualificationReason.POLICY_NOT_APPLICABLE,
        QualificationReason.EVIDENCE_BUNDLE_DEFINITION_NOT_ALLOWED,
        QualificationReason.VERIFICATION_DEFINITION_NOT_ALLOWED,
        QualificationReason.COMPARISON_DEFINITION_NOT_ALLOWED,
        QualificationReason.GATE_DEFINITION_NOT_ALLOWED,
        QualificationReason.GATE_POLICY_IDENTITY_NOT_ALLOWED,
        QualificationReason.EXPERIMENT_IDENTITY_NOT_ALLOWED,
        QualificationReason.PARTIAL_RUN_PRESENT,
        QualificationReason.GATE_OUTCOME_NOT_EVALUATED,
        QualificationReason.NOT_REACHED,
    }
)


# ---------------------------------------------------------------------------
# 策略模型
# ---------------------------------------------------------------------------


class QualificationPolicyScope(BaseModel):
    """这份资格策略**管的是哪一类**评测。

    🔴 每个字段都对应 S5／S6／S7 里一个**已验证的结构化身份**，没有任何
    一个是猜的、按文件名推的或从路径里读的。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # ---- S7 证据层契约 ----
    evidence_bundle_schema_version: int = Field(ge=1)
    evidence_bundle_definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    verification_definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    # ---- S5 对比契约 ----
    comparison_schema_version: int = Field(ge=1)
    comparison_definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    # ---- S6 门禁契约 ----
    gate_decision_schema_version: int = Field(ge=1)
    gate_definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    gate_policy_schema_version: int = Field(ge=1)
    gate_policy_id: str = Field(pattern=_ID_PATTERN)
    gate_policy_revision: int = Field(ge=1)
    gate_policy_digest: str = Field(pattern=_DIGEST_PATTERN)
    # ---- Mock Golden 实验身份 ----
    dataset_digest: str = Field(pattern=_DIGEST_PATTERN)
    assertion_registry_digest: str = Field(pattern=_DIGEST_PATTERN)
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    execution_mode: str = Field(min_length=1)
    storage_backend: str = Field(min_length=1)


class AllowedOutcomes(BaseModel):
    """这份策略**接受**哪几种结论。

    🔴 声明式：写进去什么就只接受什么。首个策略只接受 ``VERIFIED`` 与
    ``PASS``——"未验证的证据"与"没判成的门禁"都不在可接受集合里。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    verification_outcomes: tuple[VerificationOutcome, ...] = Field(min_length=1)
    gate_outcomes: tuple[GateOutcome, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Self:
        for name, items in (
            ("verification_outcomes", self.verification_outcomes),
            ("gate_outcomes", self.gate_outcomes),
        ):
            if len(set(items)) != len(items):
                msg = f"{name} 不得重复"
                raise ValueError(msg)
        return self


class EvaluationQualificationPolicy(BaseModel):
    """一份版本化的评测资格策略。

    ⚠️ 它是**声明式的 JSON**：没有表达式、没有 include、没有继承、没有
    插件、没有网络、没有发布许可字段。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    qualification_policy_schema_version: int
    qualification_policy_id: str = Field(pattern=_ID_PATTERN)
    qualification_policy_revision: int = Field(ge=1)
    #: 由**除它自己以外**的全部字段算出（见 :func:`qualification_policy_digest`）。
    qualification_policy_digest: str = Field(pattern=_DIGEST_PATTERN)
    display_name: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    scope: QualificationPolicyScope
    allowed_outcomes: AllowedOutcomes


def qualification_policy_digest(payload: dict[str, Any]) -> str:
    """由**除 ``qualification_policy_digest`` 以外**的全部字段算出策略摘要。

    🔴 **自排除**：含自己会让它无法被独立重算，而一个没有人能复核的摘要
    只是看着像保障。

    ⚠️ 它**不是签名**：任何人改了内容都能重算一次。它的用途是"这两份是不是
    同一份策略"，不是"这份策略可不可信"。
    """
    without = {key: value for key, value in payload.items() if key != "qualification_policy_digest"}
    return _digest(without)


#: 策略 JSON 的顶层字段。**与模型一一对应**。
QUALIFICATION_POLICY_TOP_LEVEL_FIELDS: Final[tuple[str, ...]] = (
    "allowed_outcomes",
    "display_name",
    "purpose",
    "qualification_policy_digest",
    "qualification_policy_id",
    "qualification_policy_revision",
    "qualification_policy_schema_version",
    "scope",
)


def parse_qualification_policy(payload: dict[str, Any]) -> EvaluationQualificationPolicy:
    """把一份策略 payload 还原成 :class:`EvaluationQualificationPolicy`。

    🔴 顶层字段必须**恰好**是 :data:`QUALIFICATION_POLICY_TOP_LEVEL_FIELDS`：
    多一个少一个都拒绝。
    """
    unknown = sorted(set(payload) - set(QUALIFICATION_POLICY_TOP_LEVEL_FIELDS))
    missing = sorted(set(QUALIFICATION_POLICY_TOP_LEVEL_FIELDS) - set(payload))
    if unknown or missing:
        msg = (
            "资格策略顶层字段不符合本版本的格式"
            f"（多出：{unknown or '无'}；缺少：{missing or '无'}）"
        )
        raise QualificationInputError(msg)
    return EvaluationQualificationPolicy.model_validate(payload)


def load_qualification_policy(path: Path) -> EvaluationQualificationPolicy:
    """严格加载一份资格策略。

    校验顺序：UTF-8（拒绝 BOM）→ JSON（拒绝 NaN/Infinity 与重复键）→
    顶层字段集合 → **重算 ``qualification_policy_digest`` 并比对** → 逐字段模型。

    ⚠️ 它**不**校验 schema version 是否为 1：那是
    ``qualification_policy_schema_supported`` 这条**检查**的职责。loader 让它
    顺手把版本也管了，那条检查就永远无法 FAIL。摘要不符则是另一回事——
    那是"这份文件不是它自称的那一份"，属于根输入错误，必须读不进来。

    Raises:
        QualificationInputError: 读不了、格式不对、字段集合不对，或摘要不符。
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        msg = f"读取失败（{type(exc).__name__}）"
        raise QualificationInputError(msg) from exc

    payload = _decode_json(data)
    declared = payload.get("qualification_policy_digest")
    if not isinstance(declared, str):
        msg = "qualification_policy_digest 必须是字符串"
        raise QualificationInputError(msg)
    recomputed = qualification_policy_digest(payload)
    if declared != recomputed:
        msg = (
            "qualification_policy_digest 与策略内容不符："
            f"文件里写的是 {declared}，按内容算出的是 {recomputed}。"
            "⚠️ 改过策略就必须重算摘要——沿用旧摘要等于宣称它是另一份策略"
        )
        raise QualificationInputError(msg)

    try:
        return parse_qualification_policy(payload)
    except QualificationInputError:
        raise
    except ValidationError as exc:
        msg = f"资格策略结构校验失败：{exc.error_count()} 处（第一处：{_first_error(exc)}）"
        raise QualificationInputError(msg) from exc


# ---------------------------------------------------------------------------
# 可适用性
# ---------------------------------------------------------------------------


class QualificationPolicyApplicability(BaseModel):
    """这份资格策略**管不管得着**这条链。

    🔴 ``reasons`` 非空 ⇔ ``status`` 不是 ``APPLICABLE``。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: QualificationApplicabilityStatus
    reasons: tuple[QualificationPolicyApplicabilityReason, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.status is QualificationApplicabilityStatus.APPLICABLE:
            if self.reasons:
                msg = "适用时不得给出不适用原因"
                raise ValueError(msg)
        elif not self.reasons:
            msg = "不适用或判不了时必须给出原因"
            raise ValueError(msg)
        values = [reason.value for reason in self.reasons]
        if values != sorted(set(values)):
            msg = "reasons 必须去重并稳定排序"
            raise ValueError(msg)
        return self


@dataclass(frozen=True, slots=True)
class _ExperimentIdentity:
    """从**对比产物**里读出来的实验身份。"""

    comparison_schema_version: int
    comparison_definition_digest: str
    dataset_digest: str
    assertion_registry_digest: str
    provider: str
    model: str
    execution_mode: str
    storage_backend: str
    blockers: tuple[str, ...]


def _experiment_identity(comparison: EvaluationComparison) -> _ExperimentIdentity:
    """从对比产物里取实验身份。

    ⚠️ 用的是对比**baseline 一侧**的身份——它是对比的支点。候选一侧的
    绑定由 S7 的 ``candidate_role_valid`` 负责，本层不重复它。

    🔴 只在 S7 判定 ``VERIFIED`` 时才调用它：那时对比文件的字节已被
    ``comparison_content_digest_valid`` 钉死，这份身份因而**与证据包记录的
    那一份逐字节一致**，不是"另一份说法"。
    """
    side = comparison.baseline
    return _ExperimentIdentity(
        comparison_schema_version=comparison.comparison_schema_version,
        comparison_definition_digest=comparison.comparison_definition_digest,
        dataset_digest=side.dataset_digest,
        assertion_registry_digest=side.assertion_registry_digest,
        provider=side.provider_name,
        model=side.model_id,
        execution_mode=side.manifest_execution_mode,
        storage_backend=side.storage_backend,
        blockers=comparison.blockers,
    )


def evaluate_policy_applicability(
    policy: EvaluationQualificationPolicy,
    *,
    evidence_bundle_schema_version: int | None,
    evidence_bundle_definition_digest: str | None,
    verification_definition_digest: str | None,
    gate_decision: GateDecision | None,
    comparison: EvaluationComparison | None,
) -> QualificationPolicyApplicability:
    """逐项对照策略 scope。

    🔴 **只回答"管不管得着"**，不回答"这条链好不好"。因此所有不匹配都记成
    ``NOT_APPLICABLE``，而不是某种"不合格"。

    ⚠️ 传 ``None`` 的身份表示**读不出来**（而不是"读到了空值"）：此时结论是
    ``NOT_EVALUATED``，因为"这次判不了它管不管得着"与"它不归我管"是两件事。
    """
    if (
        evidence_bundle_schema_version is None
        or evidence_bundle_definition_digest is None
        or verification_definition_digest is None
        or gate_decision is None
        or comparison is None
    ):
        return QualificationPolicyApplicability(
            status=QualificationApplicabilityStatus.NOT_EVALUATED,
            reasons=(QualificationPolicyApplicabilityReason.ASSESSMENT_NOT_REACHED,),
        )

    scope = policy.scope
    experiment = _experiment_identity(comparison)
    gate = gate_decision.identity
    reasons: list[QualificationPolicyApplicabilityReason] = []

    def want(ok: bool, reason: QualificationPolicyApplicabilityReason) -> None:
        if not ok:
            reasons.append(reason)

    want(
        evidence_bundle_schema_version == scope.evidence_bundle_schema_version,
        QualificationPolicyApplicabilityReason.EVIDENCE_BUNDLE_SCHEMA_OUT_OF_SCOPE,
    )
    want(
        evidence_bundle_definition_digest == scope.evidence_bundle_definition_digest,
        QualificationPolicyApplicabilityReason.EVIDENCE_BUNDLE_DEFINITION_OUT_OF_SCOPE,
    )
    want(
        verification_definition_digest == scope.verification_definition_digest,
        QualificationPolicyApplicabilityReason.VERIFICATION_DEFINITION_OUT_OF_SCOPE,
    )
    want(
        experiment.comparison_schema_version == scope.comparison_schema_version,
        QualificationPolicyApplicabilityReason.COMPARISON_SCHEMA_OUT_OF_SCOPE,
    )
    want(
        experiment.comparison_definition_digest == scope.comparison_definition_digest,
        QualificationPolicyApplicabilityReason.COMPARISON_DEFINITION_OUT_OF_SCOPE,
    )
    want(
        gate_decision.gate_decision_schema_version == scope.gate_decision_schema_version,
        QualificationPolicyApplicabilityReason.GATE_SCHEMA_OUT_OF_SCOPE,
    )
    want(
        gate_decision.gate_definition_digest == scope.gate_definition_digest,
        QualificationPolicyApplicabilityReason.GATE_DEFINITION_OUT_OF_SCOPE,
    )
    want(
        gate.policy_schema_version == scope.gate_policy_schema_version
        and gate.policy_id == scope.gate_policy_id
        and gate.policy_revision == scope.gate_policy_revision
        and gate.policy_digest == scope.gate_policy_digest,
        QualificationPolicyApplicabilityReason.GATE_POLICY_OUT_OF_SCOPE,
    )
    want(
        experiment.dataset_digest == scope.dataset_digest,
        QualificationPolicyApplicabilityReason.DATASET_OUT_OF_SCOPE,
    )
    want(
        experiment.assertion_registry_digest == scope.assertion_registry_digest,
        QualificationPolicyApplicabilityReason.ASSERTION_REGISTRY_OUT_OF_SCOPE,
    )
    want(
        experiment.provider == scope.provider,
        QualificationPolicyApplicabilityReason.PROVIDER_OUT_OF_SCOPE,
    )
    want(
        experiment.model == scope.model,
        QualificationPolicyApplicabilityReason.MODEL_OUT_OF_SCOPE,
    )
    want(
        experiment.execution_mode == scope.execution_mode,
        QualificationPolicyApplicabilityReason.EXECUTION_MODE_OUT_OF_SCOPE,
    )
    want(
        experiment.storage_backend == scope.storage_backend,
        QualificationPolicyApplicabilityReason.STORAGE_BACKEND_OUT_OF_SCOPE,
    )
    want(
        "partial_run" not in experiment.blockers,
        QualificationPolicyApplicabilityReason.PARTIAL_RUN,
    )

    unique = sorted({reason.value for reason in reasons})
    if not unique:
        return QualificationPolicyApplicability(status=QualificationApplicabilityStatus.APPLICABLE)
    return QualificationPolicyApplicability(
        status=QualificationApplicabilityStatus.NOT_APPLICABLE,
        reasons=tuple(QualificationPolicyApplicabilityReason(value) for value in unique),
    )


# ---------------------------------------------------------------------------
# 结论模型
# ---------------------------------------------------------------------------


class QualificationPolicyIdentity(BaseModel):
    """这份结论出自**哪一版策略**。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    qualification_policy_schema_version: int = Field(ge=1)
    qualification_policy_id: str = Field(pattern=_ID_PATTERN)
    qualification_policy_revision: int = Field(ge=1)
    qualification_policy_digest: str = Field(pattern=_DIGEST_PATTERN)


class QualificationEvidenceIdentity(BaseModel):
    """这条结论针对**哪一份证据**。

    ⚠️ 字段可以为 ``None``：当证据链连严格 Bundle 都形不成时，S8 无从知道
    它的身份。**编一个默认值比留空危险得多**——留空说明"没读到"，编一个
    说明"读到了、是这个"。

    🔴 ``QUALIFIED`` **要求四栏齐备**（见
    :meth:`EvaluationQualificationDecision._check`）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    bundle_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    evidence_bundle_schema_version: int | None = Field(default=None, ge=1)
    evidence_bundle_definition_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    verification_definition_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)

    def is_complete(self) -> bool:
        """四栏是否齐备。"""
        return (
            self.bundle_digest is not None
            and self.evidence_bundle_schema_version is not None
            and self.evidence_bundle_definition_digest is not None
            and self.verification_definition_digest is not None
        )


class QualificationGateIdentity(BaseModel):
    """这条结论针对**哪一份门禁结论**。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate_decision_schema_version: int | None = Field(default=None, ge=1)
    gate_definition_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    gate_policy_id: str | None = None
    gate_policy_revision: int | None = Field(default=None, ge=1)
    gate_policy_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    gate_outcome: GateOutcome | None = None

    def is_complete(self) -> bool:
        """六栏是否齐备。"""
        return (
            self.gate_decision_schema_version is not None
            and self.gate_definition_digest is not None
            and self.gate_policy_id is not None
            and self.gate_policy_revision is not None
            and self.gate_policy_digest is not None
            and self.gate_outcome is not None
        )


class QualificationCheck(BaseModel):
    """一项资格检查的结构化结论。

    🔴 **只放稳定结构**：检查 ID、结论、原因码，以及两个**安全的**观察值。
    没有文件内容、没有回答正文、没有异常堆栈、没有绝对路径。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    check_id: str = Field(pattern=_ID_PATTERN)
    outcome: QualificationCheckOutcome
    reason_code: QualificationReason
    #: 观察到的值：结构版本、摘要、身份名、状态名。**不含输入正文**。
    observed: str | None = None
    expected: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        """结论与原因码必须自洽。"""
        if self.outcome is QualificationCheckOutcome.PASS:
            if self.reason_code not in _SATISFIED_REASONS:
                msg = f"检查 {self.check_id!r} 通过了，原因码却是 {self.reason_code}"
                raise ValueError(msg)
        elif self.outcome is QualificationCheckOutcome.FAIL:
            if self.reason_code not in _MISMATCH_REASONS:
                msg = (
                    f"检查 {self.check_id!r} 判为不合格（FAIL），而 {self.reason_code} "
                    "不是确定性冲突原因——读不出来的东西不构成冲突"
                )
                raise ValueError(msg)
        elif self.reason_code not in _UNAVAILABLE_REASONS:
            msg = f"检查 {self.check_id!r} 未得到结论，原因码却是 {self.reason_code}"
            raise ValueError(msg)
        return self


class QualificationDecisionIdentity(BaseModel):
    """这份结论**出自哪三样东西**：哪一版策略、哪一份证据、哪一份门禁结论。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    qualification_policy: QualificationPolicyIdentity
    evidence: QualificationEvidenceIdentity
    gate: QualificationGateIdentity


class EvaluationQualificationDecision(BaseModel):
    """一次评测资格判定的完整产物。

    🔴 **没有发布许可字段。** 见模块文档的禁用列表。

    🔴 结论与明细必须自洽——把"自相矛盾的资格结论"变成**构造失败**：

    1. ``checks`` 完整且按固定顺序；
    2. ``reason_codes`` 与检查明细一致；
    3. ``QUALIFIED`` ⇒ 全部检查 PASS、证据为 ``VERIFIED``、策略适用、
       三组身份齐备、门禁结论是 ``PASS``；
    4. ``DISQUALIFIED`` ⇒ 至少一项确定性 FAIL；
    5. ``NOT_EVALUATED`` ⇒ 至少一项未得到结论，且**没有** FAIL。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    qualification_decision_schema_version: int = QUALIFICATION_DECISION_SCHEMA_VERSION
    qualification_definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    #: 由**除它自己以外**的全部字段算出（见 :func:`qualification_decision_digest`）。
    qualification_decision_digest: str = Field(pattern=_DIGEST_PATTERN)
    identity: QualificationDecisionIdentity
    verification_outcome: VerificationOutcome
    policy_applicability: QualificationPolicyApplicability
    checks: tuple[QualificationCheck, ...]
    qualification_outcome: EvaluationQualificationOutcome
    #: 非 PASS 检查的原因码，去重后按字典序。
    reason_codes: tuple[str, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _require_a_correct_decision_digest(cls, data: Any) -> Any:
        """🔴 对**原始 payload** 重算摘要并比对。摘要自排除，不循环。"""
        if not isinstance(data, dict):
            return data
        declared = data.get("qualification_decision_digest")
        if not isinstance(declared, str):
            return data
        recomputed = qualification_decision_digest(data)
        if declared != recomputed:
            msg = (
                "qualification_decision_digest 与结论内容不符："
                f"写的是 {declared}，按内容算出的是 {recomputed}"
            )
            raise ValueError(msg)
        return data

    @model_validator(mode="after")
    def _check(self) -> Self:
        if tuple(item.check_id for item in self.checks) != QUALIFICATION_CHECKS:
            msg = f"检查项必须完整、且按固定顺序出现（应为 {list(QUALIFICATION_CHECKS)}）"
            raise ValueError(msg)

        failed = [
            item.check_id for item in self.checks if item.outcome is QualificationCheckOutcome.FAIL
        ]
        unevaluated = [
            item.check_id
            for item in self.checks
            if item.outcome is QualificationCheckOutcome.NOT_EVALUATED
        ]
        expected_reasons = tuple(
            sorted(
                {
                    item.reason_code.value
                    for item in self.checks
                    if item.outcome is not QualificationCheckOutcome.PASS
                }
            )
        )
        if self.reason_codes != expected_reasons:
            msg = f"reason_codes={list(self.reason_codes)} 与检查明细不一致"
            raise ValueError(msg)

        if self.qualification_outcome is EvaluationQualificationOutcome.QUALIFIED:
            if failed or unevaluated:
                msg = "QUALIFIED 要求**全部**检查通过"
                raise ValueError(msg)
            if self.verification_outcome is not VerificationOutcome.VERIFIED:
                msg = "证据没有被验证为 VERIFIED 时不得给出 QUALIFIED"
                raise ValueError(msg)
            if self.policy_applicability.status is not QualificationApplicabilityStatus.APPLICABLE:
                msg = "策略不适用（或判不了是否适用）时不得给出 QUALIFIED"
                raise ValueError(msg)
            if not self.identity.evidence.is_complete():
                msg = "QUALIFIED 要求证据身份齐备"
                raise ValueError(msg)
            if not self.identity.gate.is_complete():
                msg = "QUALIFIED 要求门禁身份齐备"
                raise ValueError(msg)
            if self.identity.gate.gate_outcome is not GateOutcome.PASS:
                msg = f"门禁结论是 {self.identity.gate.gate_outcome} 时不得给出 QUALIFIED"
                raise ValueError(msg)
        elif self.qualification_outcome is EvaluationQualificationOutcome.DISQUALIFIED:
            if not failed:
                msg = "DISQUALIFIED 要求至少一项检查明确不合格（FAIL）"
                raise ValueError(msg)
        else:  # NOT_EVALUATED
            if failed:
                msg = "存在确定冲突时结论必须是 DISQUALIFIED，不能降级成 NOT_EVALUATED"
                raise ValueError(msg)
            if not unevaluated:
                msg = "NOT_EVALUATED 要求至少一项检查没得到结论"
                raise ValueError(msg)
        return self


def qualification_decision_digest(payload: dict[str, Any]) -> str:
    """由**除 ``qualification_decision_digest`` 以外**的全部字段算出结论摘要。

    🔴 **自排除**，加载时必须重新计算并核对。⚠️ 它不是签名。
    """
    without = {
        key: value for key, value in payload.items() if key != "qualification_decision_digest"
    }
    return _digest(without)


def qualification_decision_payload(decision: EvaluationQualificationDecision) -> dict[str, Any]:
    """结论的 canonical payload。

    🔴 **写入磁盘与计算摘要用的是同一份**。两处各写一遍 dump 逻辑，迟早会
    出现"写出来的"与"摘要覆盖的"不是同一个东西——那种不一致会让**每一份**
    结论都验不过，而原因极难定位。

    ⚠️ ``exclude_none=True``：读不到的身份字段**不出现**，而不是写成
    ``null``。前者是"没读到"，后者读起来像"读到的是空"。
    """
    return decision.model_dump(mode="json", exclude_none=True)


#: 结论 JSON 的顶层字段。**与模型一一对应**。
QUALIFICATION_DECISION_TOP_LEVEL_FIELDS: Final[tuple[str, ...]] = (
    "checks",
    "identity",
    "policy_applicability",
    "qualification_decision_digest",
    "qualification_decision_schema_version",
    "qualification_definition_digest",
    "qualification_outcome",
    "reason_codes",
    "verification_outcome",
)


def parse_qualification_decision(payload: dict[str, Any]) -> EvaluationQualificationDecision:
    """把一份结论 payload 还原成 :class:`EvaluationQualificationDecision`。

    🔴 顶层字段必须**恰好**是 :data:`QUALIFICATION_DECISION_TOP_LEVEL_FIELDS`；
    ``qualification_decision_digest`` 由模型在构造时重算并核对。
    """
    unknown = sorted(set(payload) - set(QUALIFICATION_DECISION_TOP_LEVEL_FIELDS))
    missing = sorted(set(QUALIFICATION_DECISION_TOP_LEVEL_FIELDS) - set(payload))
    if unknown or missing:
        msg = (
            "资格结论顶层字段不符合本版本的格式"
            f"（多出：{unknown or '无'}；缺少：{missing or '无'}）"
        )
        raise QualificationInputError(msg)
    return EvaluationQualificationDecision.model_validate(payload)


def load_qualification_decision(path: Path) -> EvaluationQualificationDecision:
    """严格加载一份资格结论：形状 → 摘要 → 聚合一致性。

    Raises:
        QualificationInputError: 读不了、格式不对、字段集合不对、摘要不符，
            或违反模型不变量。
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        msg = f"读取失败（{type(exc).__name__}）"
        raise QualificationInputError(msg) from exc

    payload = _decode_json(data)
    try:
        return parse_qualification_decision(payload)
    except QualificationInputError:
        raise
    except ValidationError as exc:
        msg = f"资格结论结构校验失败：{exc.error_count()} 处（第一处：{_first_error(exc)}）"
        raise QualificationInputError(msg) from exc


#: 原子写的临时文件前缀。
_TEMPORARY_PREFIX: Final[str] = "."


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写：先写同目录临时文件，再 ``os.replace`` 顶替目标。

    🔴 **不用"直接打开目标文件写"**：那条路径在中途失败时会留下一个半截的
    产物，而半截的资格结论比没有结论更危险——它读起来像一份完整的结论。

    ⚠️ 这是 :mod:`ai_psi.evaluation.evidence` 与 :mod:`ai_psi.evaluation.gate`
    里同名私有函数的**第三份拷贝**。本层不把它抽成公共工具，是为了不为了
    十行代码去改动已经封存的模块；统一它们是一次独立的维护动作。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f"{_TEMPORARY_PREFIX}{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def write_qualification_decision(decision: EvaluationQualificationDecision, path: Path) -> None:
    """把资格结论原子地写到磁盘。

    与 S1a—S7 的产物用**同一个** :func:`~ai_psi.evaluation.serialization.dumps`：
    UTF-8、键排序、缩进固定、结尾恰好一个换行。

    Raises:
        OSError: 写盘失败。**不吞掉**——"看起来判完了却没有产物"是最坏的结果。
    """
    _atomic_write_text(path, dumps(qualification_decision_payload(decision)))


# ---------------------------------------------------------------------------
# 输入与纯计算入口
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QualificationInputs:
    """七个**角色显式**的输入路径。

    🔴 没有默认值、没有"自动找最新的那份"、没有按文件名推断角色。
    注意这里**没有** ``verification_report``——S8 从不接受一份现成的验证报告。
    """

    bundle: Path
    baseline_run: Path
    candidate_run: Path
    comparison: Path
    gate_policy: Path
    gate_decision: Path
    qualification_policy: Path

    def evidence_inputs(self) -> EvidenceInputs:
        """交给 S7 验证器的五个角色显式输入。"""
        return EvidenceInputs(
            baseline_run=self.baseline_run,
            candidate_run=self.candidate_run,
            comparison=self.comparison,
            policy=self.gate_policy,
            gate_decision=self.gate_decision,
        )


def _check(
    check_id: str,
    outcome: QualificationCheckOutcome,
    reason: QualificationReason,
    *,
    observed: str | None = None,
    expected: str | None = None,
) -> QualificationCheck:
    return QualificationCheck(
        check_id=check_id,
        outcome=outcome,
        reason_code=reason,
        observed=observed,
        expected=expected,
    )


def _not_reached(check_id: str) -> QualificationCheck:
    return _check(
        check_id, QualificationCheckOutcome.NOT_EVALUATED, QualificationReason.NOT_REACHED
    )


def _scope_verdict(
    check_id: str,
    ok: bool,
    reason: QualificationReason,
    *,
    observed: str | None = None,
    expected: str | None = None,
) -> QualificationCheck:
    """作用域类检查：通过是 ``SATISFIED``，不匹配是 **NOT_EVALUATED**。

    🔴 不是 ``FAIL``。"这份策略管不着这类评测"与"这条链确定不合格"是两件事；
    把前者写成后者，就是拿规则不适用去指控候选——正是本层拒绝的那种工具。
    """
    return _check(
        check_id,
        QualificationCheckOutcome.PASS if ok else QualificationCheckOutcome.NOT_EVALUATED,
        QualificationReason.SATISFIED if ok else reason,
        observed=observed,
        expected=expected,
    )


def _aggregate(checks: tuple[QualificationCheck, ...]) -> EvaluationQualificationOutcome:
    """**FAIL 优先于 NOT_EVALUATED。**

    只要有一项确定性冲突，结论就是 ``DISQUALIFIED``，不会被"还有几项没查"
    稀释掉；反过来，没有任何冲突但也没查全时，绝不升格成 ``QUALIFIED``。
    """
    outcomes = {item.outcome for item in checks}
    if QualificationCheckOutcome.FAIL in outcomes:
        return EvaluationQualificationOutcome.DISQUALIFIED
    if QualificationCheckOutcome.NOT_EVALUATED in outcomes:
        return EvaluationQualificationOutcome.NOT_EVALUATED
    return EvaluationQualificationOutcome.QUALIFIED


def _evaluate(
    report: EvidenceVerificationReport,
    policy: EvaluationQualificationPolicy,
    *,
    bundle: EvaluationEvidenceBundle | None,
    comparison: EvaluationComparison | None,
    gate_decision: GateDecision | None,
) -> tuple[tuple[QualificationCheck, ...], QualificationPolicyApplicability]:
    """按**固定顺序**逐项检查，上游没完成时下游一律 ``NOT_EVALUATED``。"""
    checks: list[QualificationCheck] = []
    outcome = report.verification_outcome

    # 1. 验证确实跑过了。
    #
    # ⚠️ 这一项在本实现里必然 PASS：S8 拿到 verification outcome 的唯一途径
    # 就是**自己调用** S7 的纯函数。保留它，是为了让报告**显式**记录
    # "这次结论来自一次真实重算"，而不是靠读者去读代码。
    checks.append(
        _check(
            "evidence_verification_completed",
            QualificationCheckOutcome.PASS,
            QualificationReason.SATISFIED,
            observed=outcome.value,
            expected=VerificationOutcome.VERIFIED.value,
        )
    )

    # 2. 证据链本身。
    if outcome is VerificationOutcome.VERIFIED:
        checks.append(
            _check(
                "evidence_verification_verified",
                QualificationCheckOutcome.PASS,
                QualificationReason.SATISFIED,
                observed=outcome.value,
                expected=VerificationOutcome.VERIFIED.value,
            )
        )
    elif outcome is VerificationOutcome.INVALID:
        # 🔴 INVALID 是**确定冲突** ⇒ FAIL ⇒ DISQUALIFIED。
        # 它不是"输入解析失败"，CLI 用 DISQUALIFIED 的业务退出码。
        checks.append(
            _check(
                "evidence_verification_verified",
                QualificationCheckOutcome.FAIL,
                QualificationReason.EVIDENCE_VERIFICATION_INVALID,
                observed=outcome.value,
                expected=VerificationOutcome.VERIFIED.value,
            )
        )
    else:
        checks.append(
            _check(
                "evidence_verification_verified",
                QualificationCheckOutcome.NOT_EVALUATED,
                QualificationReason.EVIDENCE_VERIFICATION_NOT_VERIFIABLE,
                observed=outcome.value,
                expected=VerificationOutcome.VERIFIED.value,
            )
        )

    # 3. 策略自身的 Schema。
    supported = policy.qualification_policy_schema_version == QUALIFICATION_POLICY_SCHEMA_VERSION
    checks.append(
        _scope_verdict(
            "qualification_policy_schema_supported",
            supported,
            QualificationReason.POLICY_SCHEMA_UNSUPPORTED,
            observed=str(policy.qualification_policy_schema_version),
            expected=str(QUALIFICATION_POLICY_SCHEMA_VERSION),
        )
    )

    # 4. 策略摘要。
    #
    # ⚠️ 这一项必然 PASS：``load_qualification_policy`` 已经把摘要核对过了，
    # 摘要不符的策略**根本读不进来**（根输入错误，CLI 退出码 3，不产生结论）。
    # 保留它是为了让报告显式记录"这份策略是它自称的那一份"。
    checks.append(
        _check(
            "qualification_policy_digest_valid",
            QualificationCheckOutcome.PASS,
            QualificationReason.SATISFIED,
            observed=policy.qualification_policy_digest,
            expected=policy.qualification_policy_digest,
        )
    )

    # ---- 到这里为止回答的都是"判得了吗" ----
    #
    # 🔴 只有证据被验证为 VERIFIED、策略 Schema 受支持、且三组身份都读得出来
    # 时，才**安全地**计算作用域。否则一律 NOT_EVALUATED——绝不从不可信证据
    # 里推导出一个 APPLICABLE。
    # 早退式守卫：写成"任一不满足就返回"，mypy 才能在后面自己收窄类型，
    # 不必到处写 type: ignore——那些注释会让"这里真的可能是 None"变得看不出来。
    if (
        outcome is not VerificationOutcome.VERIFIED
        or not supported
        or bundle is None
        or comparison is None
        or gate_decision is None
    ):
        for check_id in QUALIFICATION_CHECKS[4:]:
            checks.append(_not_reached(check_id))
        return tuple(checks), QualificationPolicyApplicability(
            status=QualificationApplicabilityStatus.NOT_EVALUATED,
            reasons=(QualificationPolicyApplicabilityReason.ASSESSMENT_NOT_REACHED,),
        )

    applicability = evaluate_policy_applicability(
        policy,
        evidence_bundle_schema_version=bundle.evidence_bundle_schema_version,
        evidence_bundle_definition_digest=bundle.evidence_bundle_definition_digest,
        verification_definition_digest=report.verification_definition_digest,
        gate_decision=gate_decision,
        comparison=comparison,
    )
    experiment = _experiment_identity(comparison)
    gate = gate_decision.identity
    scope = policy.scope

    # 5. 策略管不管得着。
    #
    # 🔴 不适用 ⇒ **NOT_EVALUATED**，不是 FAIL。
    checks.append(
        _scope_verdict(
            "qualification_policy_applicable",
            applicability.status is QualificationApplicabilityStatus.APPLICABLE,
            QualificationReason.POLICY_NOT_APPLICABLE,
            observed=applicability.status.value,
            expected=QualificationApplicabilityStatus.APPLICABLE.value,
        )
    )

    checks.append(
        _scope_verdict(
            "evidence_bundle_definition_allowed",
            bundle.evidence_bundle_definition_digest == scope.evidence_bundle_definition_digest,
            QualificationReason.EVIDENCE_BUNDLE_DEFINITION_NOT_ALLOWED,
            observed=bundle.evidence_bundle_definition_digest,
            expected=scope.evidence_bundle_definition_digest,
        )
    )
    checks.append(
        _scope_verdict(
            "verification_definition_allowed",
            report.verification_definition_digest == scope.verification_definition_digest,
            QualificationReason.VERIFICATION_DEFINITION_NOT_ALLOWED,
            observed=report.verification_definition_digest,
            expected=scope.verification_definition_digest,
        )
    )
    checks.append(
        _scope_verdict(
            "comparison_definition_allowed",
            experiment.comparison_definition_digest == scope.comparison_definition_digest,
            QualificationReason.COMPARISON_DEFINITION_NOT_ALLOWED,
            observed=experiment.comparison_definition_digest,
            expected=scope.comparison_definition_digest,
        )
    )
    checks.append(
        _scope_verdict(
            "gate_definition_allowed",
            gate_decision.gate_definition_digest == scope.gate_definition_digest,
            QualificationReason.GATE_DEFINITION_NOT_ALLOWED,
            observed=gate_decision.gate_definition_digest,
            expected=scope.gate_definition_digest,
        )
    )
    checks.append(
        _scope_verdict(
            "gate_policy_identity_allowed",
            (
                gate.policy_schema_version == scope.gate_policy_schema_version
                and gate.policy_id == scope.gate_policy_id
                and gate.policy_revision == scope.gate_policy_revision
                and gate.policy_digest == scope.gate_policy_digest
            ),
            QualificationReason.GATE_POLICY_IDENTITY_NOT_ALLOWED,
            observed=f"{gate.policy_id}@{gate.policy_revision}",
            expected=f"{scope.gate_policy_id}@{scope.gate_policy_revision}",
        )
    )
    checks.append(
        _scope_verdict(
            "experiment_identity_allowed",
            (
                experiment.dataset_digest == scope.dataset_digest
                and experiment.assertion_registry_digest == scope.assertion_registry_digest
                and experiment.provider == scope.provider
                and experiment.model == scope.model
                and experiment.execution_mode == scope.execution_mode
                and experiment.storage_backend == scope.storage_backend
            ),
            QualificationReason.EXPERIMENT_IDENTITY_NOT_ALLOWED,
            observed=f"{experiment.provider}/{experiment.model}/{experiment.execution_mode}",
            expected=f"{scope.provider}/{scope.model}/{scope.execution_mode}",
        )
    )
    checks.append(
        _scope_verdict(
            "partial_run_absent",
            "partial_run" not in experiment.blockers,
            QualificationReason.PARTIAL_RUN_PRESENT,
            observed=",".join(experiment.blockers) or "（无阻塞项）",
            expected="（无阻塞项）",
        )
    )

    # ---- 🔴 策略管不着时，门禁结论这一项**不再评估** ----
    #
    # 「它的门禁结论满足本策略的允许集合吗」这句话，在"本策略压根不覆盖这类
    # 评测"时就**无从谈起**。§三 规则 3 是无条件的：VERIFIED + 不适用 ⇒
    # NOT_EVALUATED——哪怕门禁自己判了 FAIL，也不能借它推出 DISQUALIFIED。
    if applicability.status is not QualificationApplicabilityStatus.APPLICABLE:
        checks.append(_not_reached("gate_outcome_eligible"))
        return tuple(checks), applicability

    # 13. 门禁结论在不在允许集合里。
    gate_outcome = gate_decision.outcome
    allowed_gate = ",".join(item.value for item in policy.allowed_outcomes.gate_outcomes)
    if gate_outcome is GateOutcome.NOT_EVALUATED:
        # 🔴 门禁自己判了 NOT_EVALUATED 时，这一项也是 **NOT_EVALUATED**，
        # 不是 FAIL：「这次没判成」推不出「候选不合格」——那是 S6 一开始就
        # 拒绝的那种工具，本层不把它重新引入一遍。
        checks.append(
            _check(
                "gate_outcome_eligible",
                QualificationCheckOutcome.NOT_EVALUATED,
                QualificationReason.GATE_OUTCOME_NOT_EVALUATED,
                observed=gate_outcome.value,
                expected=allowed_gate,
            )
        )
    else:
        eligible = gate_outcome in policy.allowed_outcomes.gate_outcomes
        checks.append(
            _check(
                "gate_outcome_eligible",
                QualificationCheckOutcome.PASS if eligible else QualificationCheckOutcome.FAIL,
                (
                    QualificationReason.SATISFIED
                    if eligible
                    else QualificationReason.GATE_OUTCOME_NOT_ELIGIBLE
                ),
                observed=gate_outcome.value,
                expected=allowed_gate,
            )
        )

    return tuple(checks), applicability


def build_qualification_decision(
    inputs: QualificationInputs,
) -> EvaluationQualificationDecision:
    """读七个输入、**重新执行 S7 验证**、聚合出评测资格结论。

    🔴 **它自己重新验证证据。** 绝不读取磁盘上任何预先生成的
    ``VerificationReport``：那份东西要么是本次刚算出来的，要么就不可信。
    本函数唯一用的报告来自它**本次**对
    :func:`~ai_psi.evaluation.evidence.verify_evidence_bundle` 的调用。

    ⚠️ 它**不**因为「Bundle 读不出来」就抛异常——那是 S7 判定的事：验证器
    会给出 ``NOT_VERIFIABLE``，本层照它聚合。只有**资格策略**读不出来才是
    根输入错误（没有策略就没有可聚合的规则）。

    Args:
        inputs: 七个角色显式的输入路径。

    Returns:
        资格结论。

    Raises:
        QualificationInputError: 资格策略读不出来。
    """
    policy = load_qualification_policy(inputs.qualification_policy)

    # ---- 重新验证：**本次调用**产生的报告，是这里唯一的证据来源 ----
    report = verify_evidence_bundle(
        EvidenceVerificationInputs(bundle=inputs.bundle, artifacts=inputs.evidence_inputs())
    )

    # 严格 Bundle：读得出来就用它取证据身份。读不出来（含"读得出但不是严格
    # Bundle"）就留空——⚠️ 留空是**如实**，编一个默认值才是撒谎。
    bundle: EvaluationEvidenceBundle | None
    try:
        bundle = load_evidence_bundle(inputs.bundle)
    except EvidenceBundleInvariantError:
        # 读得出来、但不是一份严格的正式 Bundle：这**不是**根输入错误，
        # 交给 S7 去判 INVALID，本层照它的结论聚合。
        bundle = None
    except EvidenceInputError as exc:
        # 🔴 连安全 Envelope 都形不成（缺失、不是 UTF-8／JSON、顶层字段不对）：
        # 这是**根输入错误**。没有证据可验，也就没有结论可写——绝不伪造一份。
        msg = f"证据包根输入无法解析：{exc}"
        raise QualificationInputError(msg) from exc

    gate_decision: GateDecision | None
    try:
        gate_decision = load_gate_decision(inputs.gate_decision)
    except GateDecisionInputError:
        gate_decision = None

    comparison: EvaluationComparison | None
    try:
        comparison = load_comparison(inputs.comparison)
    except ComparisonInputError:
        comparison = None

    checks, applicability = _evaluate(
        report,
        policy,
        bundle=bundle,
        comparison=comparison,
        gate_decision=gate_decision,
    )
    outcome = _aggregate(checks)

    draft = EvaluationQualificationDecision.model_construct(
        qualification_decision_schema_version=QUALIFICATION_DECISION_SCHEMA_VERSION,
        qualification_definition_digest=qualification_definition_digest(),
        qualification_decision_digest="sha256:" + "0" * 64,
        identity=QualificationDecisionIdentity(
            qualification_policy=QualificationPolicyIdentity(
                qualification_policy_schema_version=policy.qualification_policy_schema_version,
                qualification_policy_id=policy.qualification_policy_id,
                qualification_policy_revision=policy.qualification_policy_revision,
                qualification_policy_digest=policy.qualification_policy_digest,
            ),
            evidence=QualificationEvidenceIdentity(
                bundle_digest=report.bundle_digest,
                evidence_bundle_schema_version=(
                    None if bundle is None else bundle.evidence_bundle_schema_version
                ),
                evidence_bundle_definition_digest=(
                    None if bundle is None else bundle.evidence_bundle_definition_digest
                ),
                verification_definition_digest=report.verification_definition_digest,
            ),
            gate=QualificationGateIdentity(
                gate_decision_schema_version=(
                    None if gate_decision is None else gate_decision.gate_decision_schema_version
                ),
                gate_definition_digest=(
                    None if gate_decision is None else gate_decision.gate_definition_digest
                ),
                gate_policy_id=(
                    None if gate_decision is None else gate_decision.identity.policy_id
                ),
                gate_policy_revision=(
                    None if gate_decision is None else gate_decision.identity.policy_revision
                ),
                gate_policy_digest=(
                    None if gate_decision is None else gate_decision.identity.policy_digest
                ),
                gate_outcome=None if gate_decision is None else gate_decision.outcome,
            ),
        ),
        verification_outcome=report.verification_outcome,
        policy_applicability=applicability,
        checks=checks,
        qualification_outcome=outcome,
        reason_codes=tuple(
            sorted(
                {
                    item.reason_code.value
                    for item in checks
                    if item.outcome is not QualificationCheckOutcome.PASS
                }
            )
        ),
    )
    # 摘要**自排除**：先造一份不经校验的草稿算摘要回填，再整份严格校验一遍。
    # ⚠️ 不能用 ``model_copy`` 回填——它按设计不重跑校验器。
    payload = qualification_decision_payload(draft)
    payload["qualification_decision_digest"] = qualification_decision_digest(payload)
    return EvaluationQualificationDecision.model_validate(payload)
