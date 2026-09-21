"""可验证评测证据包与链路校验（阶段 7 · S7）。

## 它回答什么

"给定一组**声称彼此关联**的运行结果、Comparison、Policy 与 GateDecision，
能否通过内容摘要、结构化身份和**重新计算**，证明它们形成一条内部一致的
评测证据链？"

## 🔴 它不回答什么

- 谁创建了这些文件、它们来自哪台机器、是否由可信主体签署；
- Candidate 是否允许部署、合并或发布；
- 当前这 10 个案例是否代表生产质量；
- 真实 Provider 是否通过；
- 证据是否满足任何外部审计法规；
- GitHub、Git、操作系统或存储介质是否可信。

## 四个必须分开的概念

=========================  ====================================================
概念                        由谁回答
=========================  ====================================================
内容完整性                   当前输入文件的**字节**是否与 Bundle 记录的摘要一致
结构完整性                   输入是否通过正式 Schema、角色是否各自对上
可重算一致性                 用正式 S5／S6 引擎重算，结果是否与输入相等
来源真实性                   **S7 不回答**——见下
=========================  ====================================================

## 🔴 SHA-256 内容摘要不是数字签名

``content_sha256`` 与 ``bundle_digest`` 都是**内容摘要**：它们能说明"这份
文件与构建 Bundle 时的那份逐字节相同"，**不能**说明它是谁产出的，也**不能**
阻止任何人在改了文件之后顺手重算一次摘要。``VERIFIED`` 只表示**给定文件
集合内部一致且可重算**，不表示来源可信，更**不表示允许发布**。

本模块因此不使用、也不应出现 signed / signature / authenticated /
trusted source / attested / cryptographically approved / tamper-proof
这类措辞；用的是 content digest、integrity check、internally consistent、
recomputed、verified against supplied bundle。

## Gate 结论与证据结论是两个独立的维度

``gate_outcome`` 可以是 ``PASS`` / ``FAIL`` / ``NOT_EVALUATED``，而
``verification_outcome`` 只回答"这条链是不是内部一致的"。因此::

    {"verification_outcome": "VERIFIED", "gate_outcome": "FAIL"}

是一个**合法且必要**的结果：门禁说候选没通过，证据链说那份"没通过"是
真的、没被改过。把前者塞进后者，等于让"结果不好"变成"证据不可信"。

## 它不碰外部世界

不运行评测、不调用 Provider、不连数据库、不读网络、不通过子进程
调用 S5／S6 的 CLI。它只读五个文件、跑两个**纯函数**
（:func:`~ai_psi.evaluation.comparison.compare_run_results` 与
:func:`~ai_psi.evaluation.gate.decide`），再写一份 JSON。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ai_psi.evaluation.comparison import (
    ComparisonIdentity,
    ComparisonInputError,
    EvaluationComparison,
    compare_run_results,
    comparison_definition_digest,
    load_comparison,
    load_run_result,
)
from ai_psi.evaluation.gate import (
    GATE_DECISION_SCHEMA_VERSION,
    POLICY_SCHEMA_VERSION,
    GateDecision,
    GateDecisionInputError,
    GateOutcome,
    GatePolicy,
    PolicyInputError,
    decide,
    gate_definition_digest,
    load_gate_decision,
    load_gate_policy,
)
from ai_psi.evaluation.runner import RESULT_SCHEMA_VERSION, RunResult
from ai_psi.evaluation.serialization import dumps

__all__ = [
    "ARTIFACT_ROLE_ORDER",
    "EVIDENCE_BUNDLE_SCHEMA_VERSION",
    "EVIDENCE_BUNDLE_TOP_LEVEL_FIELDS",
    "VERIFICATION_CHECK_ORDER",
    "VERIFICATION_SCHEMA_VERSION",
    "ArtifactDescriptor",
    "ArtifactRole",
    "ArtifactSchemaIdentity",
    "ArtifactSemanticIdentity",
    "EvaluationEvidenceBundle",
    "EvidenceBundleEnvelope",
    "EvidenceBundleInvariantError",
    "EvidenceChainIdentity",
    "EvidenceChainMismatchError",
    "EvidenceInputError",
    "EvidenceInputs",
    "EvidenceVerificationInputs",
    "EvidenceVerificationReport",
    "VerificationCheck",
    "VerificationCheckOutcome",
    "VerificationOutcome",
    "VerificationReason",
    "artifact_descriptor_digest",
    "build_evidence_bundle",
    "bundle_digest",
    "bundle_payload",
    "evidence_definition_digest",
    "evidence_verification_definition_digest",
    "load_evidence_bundle",
    "parse_evidence_bundle",
    "parse_evidence_bundle_envelope",
    "promote_evidence_bundle",
    "verify_evidence_bundle",
    "write_bundle",
    "write_verification_report",
]

#: 证据包自身的版本。**改字段或改规则就要改它**（或改定义摘要）。
EVIDENCE_BUNDLE_SCHEMA_VERSION: Final[int] = 1

#: 验证报告自身的版本。
VERIFICATION_SCHEMA_VERSION: Final[int] = 1

#: ``sha256:<64 位小写十六进制>``。
_DIGEST_PATTERN: Final[str] = r"^sha256:[0-9a-f]{64}$"

#: 完整 Git SHA 的形状。**不接受短 SHA。**
_FULL_SHA: Final[re.Pattern[str]] = re.compile(r"^[0-9a-fA-F]{40}$")


# ---------------------------------------------------------------------------
# 定义身份
# ---------------------------------------------------------------------------


#: 证据包**定义**的语义摘要输入。
#:
#: 🔴 它回答的是"这条链是**按什么规则**判出来的"。只写一个
#: ``"version": 1`` 是不够的——那没法告诉别人角色顺序是怎么定的、
#: 摘要覆盖哪些字节、重算不等时会怎么办。
EVIDENCE_DEFINITION: Final[dict[str, object]] = {
    "artifact_order": (
        "artifacts 的顺序必须**严格等于** artifact_roles 列出的顺序；"
        "顺序变化必须让 bundle_digest 变，不得被静默接受、自动重排或自动排序后放行"
    ),
    "artifact_roles": ["BASELINE_RUN", "CANDIDATE_RUN", "COMPARISON", "POLICY", "GATE_DECISION"],
    "bundle_strictness": (
        "正式 Bundle 必须**恰好**包含五个角色、每个恰好一次、且按固定顺序；"
        "缺失、重复、错序三者都**不是**正式 Bundle——不是靠 set 比较或 dict 覆盖来容忍"
    ),
    "baseline_candidate_policy": (
        "角色**只由调用方显式给出的参数**决定，绝不从文件名推断、绝不按目录顺序推断、"
        "绝不自动交换；两边内容相同时两个 ArtifactDescriptor 仍必须分别存在"
    ),
    "bundle_digest": (
        "对**除 bundle_digest 自身以外**的完整 canonical Bundle payload 取 SHA-256；"
        "加载时必须重新计算并核对；artifacts 顺序变化必须改变它、不得被静默接受"
    ),
    "byte_length": "输入文件**原始字节**的长度；不做任何换行或编码转换",
    "content_sha256": (
        "对输入文件**原始字节**取 SHA-256；不是对解析后的对象取，因此改一个空格、一个换行都会让它变"
    ),
    "forbidden_output": (
        "不得含 response_text / Prompt 正文 / failure_detail / Traceback / API key / "
        "Authorization / 数据库密码 / 完整数据库 URL / 数据库名 / 绝对路径 / 用户主目录 / "
        "文件名 / 文件修改时间 / inode / 主机名 / 用户名 / 当前时间 / 随机 UUID / CI Run URL"
    ),
    "gate_decision_recompute": (
        "构建前用正式 S6 纯计算入口（compare 结果 + policy → decide）重算 GateDecision，"
        "并与输入做完整 canonical 结构比较；不一致则不产出 Bundle"
    ),
    "gate_outcome_independence": (
        "GateDecision 的 outcome 不参与证据结论：PASS / FAIL / NOT_EVALUATED 三者在"
        "证据链一致时都可以得到 verification_outcome=VERIFIED"
    ),
    "not_evaluated_checks": (
        "上游检查失败导致下游无法执行时，下游为 NOT_EVALUATED；"
        "**绝不**把未执行的检查标成 PASS，也**绝不**把读不出来的输入标成冲突"
    ),
    "outcome_aggregation": {
        "any_check_fail": "INVALID",
        "any_check_not_evaluated": "NOT_VERIFIABLE（且没有任何 FAIL）",
        "not_verifiable_parse_level": "NOT_VERIFIABLE，退出码 3（连形状都读不出来）",
        "not_verifiable_evidence_level": "NOT_VERIFIABLE，退出码 4（形状读得出，证据不足）",
        "all_checks_pass": "VERIFIED",
    },
    "recompute_comparison": (
        "构建前用正式 S5 纯计算入口（baseline_run + candidate_run → compare_run_results）"
        "重算 EvaluationComparison，并与输入做完整 canonical 结构比较；不一致则不产出 Bundle"
    ),
    "recompute_direction": "compare_run_results(第一个参数是基线，第二个是候选)；方向不得推断",
    "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
    "serialization": "UTF-8、键排序、缩进固定、结尾恰好一个换行；无时间戳、无随机 UUID",
    "sorting": (
        "artifacts 按固定角色顺序；checks 按固定检查顺序；"
        "角色与检查 ID 集合一律按声明顺序而非字典序"
    ),
    "strict_loading": (
        "UTF-8；严格 JSON；拒绝 NaN/Infinity；拒绝重复的键；"
        "顶层字段集合精确匹配；模型不变量在加载时全部重跑"
    ),
    "untrusted_envelope": (
        "验证器先解析**不可信取证 Envelope**（允许角色缺失／重复／错序），"
        "它只用于输出精确诊断、**不是**正式 Bundle、不得直接喂给重算；"
        "只有角色完整＋唯一＋有序时才能提升为严格 Bundle"
    ),
}


def evidence_definition_digest() -> str:
    """证据包定义的语义摘要。

    🔴 **改角色顺序、摘要口径、重算规则或聚合规则必须让它变**：沿用旧摘要
    等于宣称"这两条链是按同一套规则验的"，而它们不是。

    Returns:
        形如 ``sha256:<hex>`` 的摘要。
    """
    return _digest(EVIDENCE_DEFINITION)


#: 验证[报告]定义的语义摘要输入：**检查项顺序**与聚合规则。
#:
#: 🔴 顺序进摘要不是形式主义：检查项的顺序决定了"哪一项先失败"，
#: 而报告是给人按顺序读的。
EVIDENCE_VERIFICATION_DEFINITION: Final[dict[str, object]] = {
    "checks": [
        "bundle_schema_valid",
        "bundle_digest_valid",
        "artifact_roles_complete",
        "artifact_order_valid",
        "baseline_content_digest_valid",
        "candidate_content_digest_valid",
        "comparison_content_digest_valid",
        "policy_content_digest_valid",
        "gate_decision_content_digest_valid",
        "baseline_byte_length_valid",
        "candidate_byte_length_valid",
        "comparison_byte_length_valid",
        "policy_byte_length_valid",
        "gate_decision_byte_length_valid",
        "baseline_schema_valid",
        "candidate_schema_valid",
        "comparison_schema_valid",
        "policy_schema_valid",
        "gate_decision_schema_valid",
        "chain_identity_valid",
        "baseline_role_valid",
        "candidate_role_valid",
        "comparison_recomputed_equal",
        "gate_decision_recomputed_equal",
    ],
    "check_outcomes": ["PASS", "FAIL", "NOT_EVALUATED"],
    "outcome_aggregation": {
        "any_check_fail": "INVALID",
        "any_check_not_evaluated": "NOT_VERIFIABLE",
        "all_checks_pass": "VERIFIED",
    },
    "rule": "FAIL 优先于 NOT_EVALUATED：只要有一项**确定冲突**，结论就是 INVALID",
    "role_failure_policy": (
        "角色缺失／重复／错序 ⇒ 结论 INVALID，且**不进入** S5 Comparison 重算与 "
        "S6 GateDecision 重算；依赖唯一角色映射的检查一律 NOT_EVALUATED"
    ),
    "schema_version": VERIFICATION_SCHEMA_VERSION,
    "stage": "S7",
    "strictness_gate": (
        "只有角色完整、唯一且有序时，Envelope 才被提升为严格 Bundle；"
        "提升失败时不得把检查标为 VERIFIED，也不得自动补齐、去重或排序"
    ),
}


def evidence_verification_definition_digest() -> str:
    """验证报告定义的语义摘要（检查项顺序 + 聚合规则）。"""
    return _digest(EVIDENCE_VERIFICATION_DEFINITION)


def _digest(payload: object) -> str:
    """对任意**已规范化**的结构取 SHA-256。"""
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _sha256(data: bytes) -> str:
    """对**原始字节**取 SHA-256。

    🔴 它算的是字节，不是解析后的对象：只改一个空格或一个换行，
    这个值就会变——那正是"这份文件与构建 Bundle 时的那份是不是同一份"
    这个问题唯一可以回答的方式。
    """
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _text_digest(text: str) -> str:
    """对文本取 SHA-256（供观察值使用，避免把原文写进报告）。"""
    return _sha256(text.encode("utf-8"))


def _canonical(model: BaseModel) -> str:
    """模型的 canonical JSON 文本。**完整结构比较**用的就是它。"""
    return dumps(model.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# 输入错误
# ---------------------------------------------------------------------------


class EvidenceInputError(ValueError):
    """五份输入之一读不出来或结构不合法。

    ⚠️ 它**不是**"这条链对不上"。这个异常意味着**连读都没读成**：
    JSON 坏了、有重复的键、含 NaN、或者不是合法的 UTF-8。
    一条基于未解析输入的"证据链"无论长什么样都是误导。
    """


class EvidenceBundleInvariantError(EvidenceInputError):
    """Envelope **读得出来**，但它不是一份**严格**的正式 Bundle。

    🔴 与 :class:`EvidenceInputError` 的关系是刻意的：调用方（CLI、加载器）
    不必区分"读不出来"与"读出来了但不是正式 Bundle"——两者都是"这份东西
    不能当正式 Bundle 用"。而验证器需要区分，所以它单独捕获这个类型，
    好把角色问题写成**结构化诊断**而不是一句"读不出来"。

    ⚠️ 抛出它时**不做任何修复**：不补齐缺失角色、不去重、不重排。
    """


class EvidenceChainMismatchError(ValueError):
    """重算结果与输入**对不上**。

    🔴 它带着 ``mismatches``：只说"链对不上"而不说哪一段，读者既没法修，
    也没法判断这是不是误报。里面**只有字段名**，没有字段值。
    """

    def __init__(self, message: str, *, mismatches: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.mismatches = mismatches


def _reject_constant(name: str) -> object:
    """拒绝 ``NaN`` / ``Infinity`` / ``-Infinity``。

    ⚠️ Python 的 ``json`` 模块默认接受它们；而 ``NaN != NaN``，
    任何基于等值的比较都会在那些输入上静默为假。
    """
    msg = f"JSON 里不允许出现 {name}（NaN / Infinity 不是合法的评测证据）"
    raise EvidenceInputError(msg)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝同一个对象里重复的键。

    ⚠️ 默认行为是**后者覆盖前者**，于是"链里写了两份 chain_identity"
    会静默变成一份。对一份要扮演契约的文件而言，那种宽容是有害的。

    🔴 S5 的两份加载器**没有**这道关（只有策略加载器有），所以 S7 在
    自己的读取层统一补上——不是替换它们的校验，是在它们之前多加一道。
    """
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            msg = f"JSON 对象里出现重复的键：{key!r}"
            raise EvidenceInputError(msg)
        seen.add(key)
    return dict(pairs)


def _decode_json(data: bytes) -> dict[str, Any]:
    """UTF-8 → 严格 JSON → 对象。

    Raises:
        EvidenceInputError: 不是 UTF-8、JSON 损坏、含 NaN/Infinity、
            有重复的键，或顶层不是对象。
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        msg = "不是合法的 UTF-8 文本"
        raise EvidenceInputError(msg) from exc

    try:
        payload = json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        # 🔴 被截断的 JSON 走的就是这条路。
        msg = f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）"
        raise EvidenceInputError(msg) from exc

    if not isinstance(payload, dict):
        msg = f"顶层必须是对象，实际是 {type(payload).__name__}"
        raise EvidenceInputError(msg)
    return payload


# ---------------------------------------------------------------------------
# 角色与描述符
# ---------------------------------------------------------------------------


class ArtifactRole(StrEnum):
    """一份产物在证据链里扮演的角色。**闭合集合。**

    🔴 角色由调用方**显式**给出，不从文件名推断、不按目录顺序推断、
    也不自动交换。"哪份是基线"是实验设计的一部分，不是命名约定。
    """

    BASELINE_RUN = "BASELINE_RUN"
    CANDIDATE_RUN = "CANDIDATE_RUN"
    COMPARISON = "COMPARISON"
    POLICY = "POLICY"
    GATE_DECISION = "GATE_DECISION"


#: ``artifacts`` 的**固定角色顺序**。它进定义摘要。
ARTIFACT_ROLE_ORDER: Final[tuple[ArtifactRole, ...]] = (
    ArtifactRole.BASELINE_RUN,
    ArtifactRole.CANDIDATE_RUN,
    ArtifactRole.COMPARISON,
    ArtifactRole.POLICY,
    ArtifactRole.GATE_DECISION,
)


class ArtifactSchemaIdentity(BaseModel):
    """这份产物自称是哪一版、按**哪一套定义**算出来的。

    ⚠️ 两个字段都是**产物自己声明的**，不是 S7 猜的；S7 的职责是核对
    这份声明与文件的实际内容是否一致。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(ge=1)
    #: 该产物若自带"按哪套定义算出来"这一栏，就记在这里；否则为 ``None``。
    #: 🔴 ``None`` **是信息**："这份产物没有定义摘要这一栏"，
    #: 不是"没读到"。
    definition_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)


#: 哪些角色**必须**有定义摘要。运行结果没有这一栏（它的身份在清单里）。
_DEFINITION_DIGEST_ROLES: Final[frozenset[ArtifactRole]] = frozenset(
    {ArtifactRole.COMPARISON, ArtifactRole.POLICY, ArtifactRole.GATE_DECISION}
)


class ArtifactSemanticIdentity(BaseModel):
    """这份产物在证据链里**自称的身份**。

    🔴 它是一组**闭合**字段，每个角色只允许填自己那一组（见
    :data:`_REQUIRED_SEMANTIC_FIELDS`）。写成自由 ``dict`` 会让
    "把整份输入抄进 Bundle"变成一件顺手就能做到的事，而那样一份
    Bundle 会带着回答正文、Prompt 正文与绝对路径一起流转。

    ⚠️ 这里**没有**数据库名、完整 URL、用户名、密码、主机名、文件路径、
    时间戳——它们既不帮助判断这条链对不对，又会把运行环境带进一份
    本来可以公开的产物。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # ---- 运行结果 ----
    commit_sha: str | None = None
    dataset_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    assertion_registry_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    prompt_versions_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    provider_name: str | None = None
    model_id: str | None = None
    execution_mode: str | None = None
    storage_backend: str | None = None
    # ---- 对比产物 ----
    baseline_commit_sha: str | None = None
    candidate_commit_sha: str | None = None
    comparison_eligible: bool | None = None
    # ---- 策略 ----
    policy_id: str | None = None
    policy_revision: int | None = Field(default=None, ge=1)
    # ---- 门禁结论 ----
    gate_outcome: GateOutcome | None = None


#: ``ArtifactSemanticIdentity`` 的全部字段名。与模型**必须一一对应**。
_SEMANTIC_FIELDS: Final[tuple[str, ...]] = (
    "assertion_registry_digest",
    "baseline_commit_sha",
    "candidate_commit_sha",
    "commit_sha",
    "comparison_eligible",
    "dataset_digest",
    "execution_mode",
    "gate_outcome",
    "model_id",
    "policy_id",
    "policy_revision",
    "prompt_versions_digest",
    "provider_name",
    "storage_backend",
)

#: 每个角色**恰好**允许填哪一组字段。多填一个就是构造失败。
_REQUIRED_SEMANTIC_FIELDS: Final[dict[ArtifactRole, frozenset[str]]] = {
    ArtifactRole.BASELINE_RUN: frozenset(
        {
            "commit_sha",
            "dataset_digest",
            "assertion_registry_digest",
            "prompt_versions_digest",
            "provider_name",
            "model_id",
            "execution_mode",
            "storage_backend",
        }
    ),
    ArtifactRole.CANDIDATE_RUN: frozenset(
        {
            "commit_sha",
            "dataset_digest",
            "assertion_registry_digest",
            "prompt_versions_digest",
            "provider_name",
            "model_id",
            "execution_mode",
            "storage_backend",
        }
    ),
    ArtifactRole.COMPARISON: frozenset(
        {
            "baseline_commit_sha",
            "candidate_commit_sha",
            "dataset_digest",
            "assertion_registry_digest",
            "prompt_versions_digest",
            "provider_name",
            "model_id",
            "execution_mode",
            "storage_backend",
            "comparison_eligible",
        }
    ),
    ArtifactRole.POLICY: frozenset(
        {"policy_id", "policy_revision", "dataset_digest", "assertion_registry_digest"}
    ),
    ArtifactRole.GATE_DECISION: frozenset(
        {
            "gate_outcome",
            "baseline_commit_sha",
            "candidate_commit_sha",
            "policy_id",
            "policy_revision",
        }
    ),
}


class ArtifactDescriptor(BaseModel):
    """一份产物在 Bundle 里的**完整记录**。

    🔴 只记四样东西：角色、内容摘要、字节长度、安全的结构与语义身份。
    **不记路径、不记时间、不记主机、不记用户名、不嵌入文件内容。**

    ⚠️ 它不是签名，也不该被叫作签名：它记录的是"这份文件的字节长什么样"，
    不是"这份文件是谁给的"。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: ArtifactRole
    #: 输入文件**原始字节**的 SHA-256。
    content_sha256: str = Field(pattern=_DIGEST_PATTERN)
    #: 输入文件**原始字节**的长度。空文件不是合法的证据，直接拒绝。
    byte_length: int = Field(gt=0)
    schema_identity: ArtifactSchemaIdentity
    semantic_identity: ArtifactSemanticIdentity

    @model_validator(mode="after")
    def _check(self) -> Self:
        """角色与两套身份的**形状**必须对得上。"""
        has_definition = self.schema_identity.definition_digest is not None
        if self.role in _DEFINITION_DIGEST_ROLES and not has_definition:
            msg = f"{self.role} 必须记录定义摘要（definition_digest）"
            raise ValueError(msg)
        if self.role not in _DEFINITION_DIGEST_ROLES and has_definition:
            msg = f"{self.role} 没有'定义摘要'这一栏，不应记录 definition_digest"
            raise ValueError(msg)

        required = _REQUIRED_SEMANTIC_FIELDS[self.role]
        present = frozenset(
            name for name in _SEMANTIC_FIELDS if getattr(self.semantic_identity, name) is not None
        )
        if present != required:
            msg = (
                f"{self.role} 的语义身份字段不对："
                f"缺少 {sorted(required - present)}，多出 {sorted(present - required)}"
            )
            raise ValueError(msg)
        return self


def artifact_descriptor_digest(descriptor: ArtifactDescriptor) -> str:
    """一个描述符内容的摘要。**只用于报告里的观察值**，不参与 Bundle 摘要。"""
    return _digest(descriptor.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# 证据链身份
# ---------------------------------------------------------------------------


class EvidenceChainIdentity(BaseModel):
    """五个输入**合起来**给出的实验身份。

    🔴 它全部来自产物**自己声明的结构字段**，没有一个是按文件名或目录
    猜出来的；每一项都能在某个输入里被逐字找到。

    ⚠️ ``dataset_digest`` / ``assertion_registry_digest`` 取**对比产物
    baseline 一侧**的值——对比是这条链的支点。两侧数据集不同时对比必然
    不可比较（``dataset_differs`` 阻塞），那份事实由 GateDecision 如实记录，
    本字段不去掩盖它；而候选一侧的绑定由 ``candidate_role_valid`` 单独核对。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    baseline_commit_sha: str | None
    candidate_commit_sha: str | None
    dataset_digest: str = Field(pattern=_DIGEST_PATTERN)
    assertion_registry_digest: str = Field(pattern=_DIGEST_PATTERN)
    comparison_schema_version: int = Field(ge=1)
    comparison_definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    policy_schema_version: int = Field(ge=1)
    policy_id: str = Field(min_length=1)
    policy_revision: int = Field(ge=1)
    policy_digest: str = Field(pattern=_DIGEST_PATTERN)
    gate_decision_schema_version: int = Field(ge=1)
    gate_definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    gate_outcome: GateOutcome


# ---------------------------------------------------------------------------
# 证据包
# ---------------------------------------------------------------------------


#: 计算 ``bundle_digest`` 时的占位值。它随即被换成真正的摘要——
#: :func:`bundle_digest` **自排除**，所以这个值不参与计算。
_DIGEST_PLACEHOLDER: Final[str] = "sha256:" + "0" * 64


def bundle_payload(bundle: EvaluationEvidenceBundle) -> dict[str, Any]:
    """Bundle 的 canonical payload。

    🔴 **写入磁盘与计算摘要用的是同一份。** 两处各写一遍 dump 逻辑，
    迟早会出现"写出来的"与"摘要覆盖的"不是同一个东西——那种不一致会
    让**每一份** Bundle 都验不过，而原因极难定位。

    ⚠️ ``exclude_none=True``：语义身份里每个角色**只出现自己那一组**
    字段。一份满是 ``null`` 的身份记录读起来像"这些字段查过了、没有值"，
    而不是"这些字段不属于这个角色"——那个区别正是闭合形状的意义。
    """
    return bundle.model_dump(mode="json", exclude_none=True)


def bundle_digest(payload: dict[str, Any]) -> str:
    """由**除 ``bundle_digest`` 以外**的全部字段算出 Bundle 摘要。

    🔴 **自排除**：摘要是对"这份 Bundle 的其余全部内容"取的。含自己会
    让它无法被独立重算——一个没有人能复核的摘要，只是看着像保障。

    ⚠️ 它**不是签名**：任何人改了内容都可以重算一次。它的用途是
    "这两份文件是不是同一份"，不是"这份文件可不可信"。

    Args:
        payload: Bundle 的 payload（可以带也可以不带 ``bundle_digest``）。

    Returns:
        形如 ``sha256:<hex>`` 的摘要。
    """
    without = {key: value for key, value in payload.items() if key != "bundle_digest"}
    return _digest(without)


#: Bundle JSON 的顶层字段。**与模型一一对应**。
EVIDENCE_BUNDLE_TOP_LEVEL_FIELDS: Final[tuple[str, ...]] = (
    "artifacts",
    "bundle_digest",
    "chain_identity",
    "evidence_bundle_definition_digest",
    "evidence_bundle_schema_version",
)


class EvidenceBundleEnvelope(BaseModel):
    """**不可信**证据包：只解析形状，不假定它是一份有效的正式 Bundle。

    🔴 它存在的理由只有一个：**验证器必须能读进一份坏掉的 Bundle，并说清
    坏在哪。** 直接拿严格模型去读，"少了一个角色"会变成一个"读取失败"，
    而那两件事的处置完全不同——前者是一条可以定位的冲突结论，后者是一次
    看不清的解析故障。

    ⚠️ 它**不是**新的对外产物格式：字段集与
    :class:`EvaluationEvidenceBundle` **逐字相同**，区别只在**不变量**——
    这里允许角色缺失、重复、错序。

    🔴 **它不得被当作正式 Bundle 使用**：不得由 ``build`` 输出、不得直接
    喂给 Comparison／GateDecision 重算、不得绕过
    :func:`promote_evidence_bundle` 的完整性检查。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_bundle_schema_version: int = EVIDENCE_BUNDLE_SCHEMA_VERSION
    evidence_bundle_definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    bundle_digest: str = Field(pattern=_DIGEST_PATTERN)
    #: ⚠️ **没有** ``min_length``：一个角色都没有也是一份**可诊断**的
    #: Envelope（结论是"五个角色全缺"），而不是"读不出来"。把这两种情形
    #: 分开，是这一层存在的全部意义。
    artifacts: tuple[ArtifactDescriptor, ...] = ()
    chain_identity: EvidenceChainIdentity


class EvaluationEvidenceBundle(BaseModel):
    """一条评测证据链的**严格正式契约**。

    🔴 **五个角色必须完整、唯一且按固定顺序**（见 :data:`ARTIFACT_ROLE_ORDER`）。
    缺失、重复、错序都让构造失败——不是靠 set 比较（那会掩盖重复），
    也不是靠 dict 转换（那会静默覆盖重复），而是一次**序列整体比较**：
    ``roles != ARTIFACT_ROLE_ORDER`` 同时覆盖三种错误，且无法被任何一种
    取巧写法规避。

    🔴 ``bundle_digest`` 也必须自洽。校验放在 ``mode="before"`` 上，因为
    摘要的定义域是**别人交给我们那份 bytes 解析出来的对象**，而模型自己
    dump 出来的是**规范化之后**的形状——两者在"文件里多写了一个 null"
    这类情形下并不相同。

    ⚠️ 模型**不**自动补齐角色、**不**去重、**不**重排。它只会拒绝。

    🔴 它**不嵌入**五个输入文件的完整内容，也**不含**路径、时间戳、
    主机名、用户名、数据库 URL、``response_text``、Prompt 正文或异常堆栈。
    这不是靠"写的时候小心"，而是靠这个模型里**根本没有**承载这些内容的
    字段。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_bundle_schema_version: int = EVIDENCE_BUNDLE_SCHEMA_VERSION
    evidence_bundle_definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    #: 由**除它自己以外**的全部字段算出（见 :func:`bundle_digest`）。
    bundle_digest: str = Field(pattern=_DIGEST_PATTERN)
    artifacts: tuple[ArtifactDescriptor, ...] = Field(min_length=1)
    chain_identity: EvidenceChainIdentity

    @model_validator(mode="before")
    @classmethod
    def _require_a_correct_bundle_digest(cls, data: Any) -> Any:
        """🔴 对**原始 payload** 重算摘要并比对。

        摘要**自排除**，所以这里不算循环：被比较的那个字段不参与计算。
        """
        if not isinstance(data, dict):
            return data
        declared = data.get("bundle_digest")
        if not isinstance(declared, str):
            # 类型不对交给字段校验去报；这里只负责"值对不对得上"。
            return data
        recomputed = bundle_digest(data)
        if declared != recomputed:
            msg = f"bundle_digest 与证据包内容不符：写的是 {declared}，按内容算出的是 {recomputed}"
            raise ValueError(msg)
        return data

    @model_validator(mode="after")
    def _require_the_five_roles_in_order(self) -> Self:
        """🔴 完整性、唯一性与顺序，一次序列比较全部覆盖。"""
        roles = tuple(item.role for item in self.artifacts)
        if roles != ARTIFACT_ROLE_ORDER:
            msg = (
                "artifacts 必须是 "
                f"{[role.value for role in ARTIFACT_ROLE_ORDER]} 各一次、且按此顺序；"
                f"实际是 {[role.value for role in roles]}"
            )
            raise ValueError(msg)
        return self


def _require_bundle_top_level_fields(payload: dict[str, Any]) -> None:
    """🔴 顶层字段必须恰好是 :data:`EVIDENCE_BUNDLE_TOP_LEVEL_FIELDS` 那个集合。"""
    unknown = sorted(set(payload) - set(EVIDENCE_BUNDLE_TOP_LEVEL_FIELDS))
    missing = sorted(set(EVIDENCE_BUNDLE_TOP_LEVEL_FIELDS) - set(payload))
    if unknown or missing:
        msg = (
            f"证据包顶层字段不符合本版本的格式（多出：{unknown or '无'}；缺少：{missing or '无'}）"
        )
        raise EvidenceInputError(msg)


def parse_evidence_bundle_envelope(payload: dict[str, Any]) -> EvidenceBundleEnvelope:
    """把一份 Bundle payload 读成**不可信取证 Envelope**。

    只做形状解析：顶层字段集合、字段类型、Descriptor 自身的角色形状。
    **不检查**角色是否完整、唯一或有序——那正是它要留给诊断的东西。

    Args:
        payload: 已解析的 JSON 对象。

    Returns:
        取证 Envelope。⚠️ **它不是正式 Bundle。**

    Raises:
        EvidenceInputError: 顶层字段集合不对。
        pydantic.ValidationError: 字段值不合法。
    """
    _require_bundle_top_level_fields(payload)
    return EvidenceBundleEnvelope.model_validate(payload)


def parse_evidence_bundle(payload: dict[str, Any]) -> EvaluationEvidenceBundle:
    """把一份 Bundle payload 还原成**严格**的 :class:`EvaluationEvidenceBundle`。

    🔴 角色完整、唯一、固定顺序，且 ``bundle_digest`` 正确——任一不成立都
    拒绝。这是**正式**读取路径；要诊断一份坏掉的 Bundle 请用
    :func:`parse_evidence_bundle_envelope`。

    Args:
        payload: 已解析的 JSON 对象。

    Returns:
        还原后的证据包。

    Raises:
        EvidenceInputError: 字段集合不对。
        pydantic.ValidationError: 字段值不合法，或违反模型不变量。
    """
    _require_bundle_top_level_fields(payload)
    return EvaluationEvidenceBundle.model_validate(payload)


def load_evidence_bundle(path: Path) -> EvaluationEvidenceBundle:
    """严格加载一份证据包：形状 → 提升 → 返回**严格**模型。

    校验顺序：UTF-8 → JSON（拒绝 NaN/Infinity 与重复的键）→ 顶层字段 →
    取证 Envelope → **提升**（角色完整、唯一、有序，且 ``bundle_digest``
    正确）。

    Args:
        path: 证据包路径。

    Returns:
        已通过校验的**严格**证据包。

    Raises:
        EvidenceInputError: 读不了、格式不对、字段集合不对，或摘要不符。
        EvidenceBundleInvariantError: 读得出来，但不是一份严格的正式 Bundle。
    """
    try:
        data = path.read_bytes()
    except OSError as exc:
        msg = f"读取失败（{type(exc).__name__}）"
        raise EvidenceInputError(msg) from exc

    payload = _decode_json(data)
    try:
        envelope = parse_evidence_bundle_envelope(payload)
    except ValidationError as exc:
        msg = f"证据包结构校验失败：{exc.error_count()} 处"
        raise EvidenceInputError(msg) from exc
    return promote_evidence_bundle(envelope)


def promote_evidence_bundle(envelope: EvidenceBundleEnvelope) -> EvaluationEvidenceBundle:
    """把取证 Envelope **提升**为严格正式 Bundle。

    提升条件（全部成立才行）：

    * 五个角色全部存在；
    * 每个角色恰好一次；
    * 顺序严格等于 :data:`ARTIFACT_ROLE_ORDER`；
    * 顶层字段与 Descriptor 字段合法；
    * Schema 受支持；
    * ``bundle_digest`` 正确。

    🔴 **提升不做任何修复**：不补齐缺失角色、不去重、不重排、不把错误摘要
    换成正确摘要。它只会拒绝——一条被"修好"的 Bundle 已经不是别人交给我们
    的那一份了。

    ⚠️ 角色三项在前、模型校验在后：这样"少了哪个角色"与"摘要对不上"会给出
    不同的错误话术，而不是被一句笼统的"结构校验失败"盖住。

    Args:
        envelope: 已通过形状解析的取证 Envelope。

    Returns:
        严格正式 Bundle。

    Raises:
        EvidenceBundleInvariantError: 任一不变量不成立。
    """
    roles = tuple(item.role for item in envelope.artifacts)
    missing = [role for role in ARTIFACT_ROLE_ORDER if role not in roles]
    if missing:
        msg = f"证据包缺少这些角色：{[role.value for role in missing]}"
        raise EvidenceBundleInvariantError(msg)
    if len(set(roles)) != len(roles):
        msg = f"证据包里有重复的角色：{[role.value for role in roles]}"
        raise EvidenceBundleInvariantError(msg)
    if roles != ARTIFACT_ROLE_ORDER:
        msg = (
            "证据包的 artifacts 顺序不对："
            f"必须是 {[role.value for role in ARTIFACT_ROLE_ORDER]}，"
            f"实际是 {[role.value for role in roles]}"
        )
        raise EvidenceBundleInvariantError(msg)

    payload = envelope.model_dump(mode="json", exclude_none=True)
    recomputed = bundle_digest(payload)
    if envelope.bundle_digest != recomputed:
        msg = (
            "bundle_digest 与证据包内容不符："
            f"写的是 {envelope.bundle_digest}，按内容算出的是 {recomputed}"
        )
        raise EvidenceBundleInvariantError(msg)

    try:
        return EvaluationEvidenceBundle.model_validate(payload)
    except ValidationError as exc:
        msg = f"证据包不满足正式契约（{exc.error_count()} 处）"
        raise EvidenceBundleInvariantError(msg) from exc


#: 原子写的临时文件前缀——以点开头，`evals/reports/*` 的忽略规则照样覆盖它。
_TEMPORARY_PREFIX: Final[str] = "."


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写：先写同目录临时文件，再 ``os.replace`` 顶替目标。

    🔴 **不用"直接打开目标文件写"**：那条路径在中途失败时会留下一个
    半截的产物，而半截的证据包比没有证据包更危险——它读起来像一份完整的
    记录。

    ⚠️ 与 :mod:`ai_psi.evaluation.gate` 里那个同名私有函数是**同一段逻辑**
    的两份拷贝。S7 不把它抽成公共工具，是为了不为了八行代码去改动 S5／S6
    已经封存的模块；统一它们是一次独立的维护动作，不是本切片的一部分。
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


def write_bundle(bundle: EvaluationEvidenceBundle, path: Path) -> None:
    """把证据包原子地写到磁盘。

    与 S1a—S6 的产物用**同一个** :func:`~ai_psi.evaluation.serialization.dumps`：
    UTF-8、键排序、缩进固定、结尾恰好一个换行。同样五个输入跑两次，
    产物因此逐字节一致。

    Args:
        bundle: 证据包。
        path: 输出路径。父目录不存在时会被创建。

    Raises:
        OSError: 写盘失败。**不吞掉**——"看起来建好了却没有产物"是最坏的结果。
    """
    _atomic_write_text(path, dumps(bundle_payload(bundle)))


# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceInputs:
    """五个**角色显式**的输入路径。

    🔴 没有默认值、没有"自动找最新的那份"、没有按文件名推断角色。
    角色由**参数位置**决定，而参数由调用方给出。
    """

    baseline_run: Path
    candidate_run: Path
    comparison: Path
    policy: Path
    gate_decision: Path

    def by_role(self) -> tuple[tuple[ArtifactRole, Path], ...]:
        """按固定角色顺序返回 ``(角色, 路径)``。"""
        return (
            (ArtifactRole.BASELINE_RUN, self.baseline_run),
            (ArtifactRole.CANDIDATE_RUN, self.candidate_run),
            (ArtifactRole.COMPARISON, self.comparison),
            (ArtifactRole.POLICY, self.policy),
            (ArtifactRole.GATE_DECISION, self.gate_decision),
        )


@dataclass(frozen=True, slots=True)
class _Chain:
    """五份**已通过正式加载器**的输入。"""

    baseline_run: RunResult
    candidate_run: RunResult
    comparison: EvaluationComparison
    policy: GatePolicy
    gate_decision: GateDecision


# ---------------------------------------------------------------------------
# 身份提取
# ---------------------------------------------------------------------------


def _run_identity(result: RunResult) -> ArtifactSemanticIdentity:
    """从一份运行结果里取出它在证据链里的身份。"""
    manifest = result.manifest
    if manifest is None:  # pragma: no cover - 加载器已保证
        msg = "运行结果缺少可复现性清单"
        raise EvidenceInputError(msg)
    return ArtifactSemanticIdentity(
        commit_sha=manifest.code.commit_sha,
        dataset_digest=manifest.evaluation.dataset_digest,
        assertion_registry_digest=manifest.evaluation.assertion_registry_digest,
        prompt_versions_digest=manifest.prompts.digest,
        provider_name=manifest.provider.provider_name,
        model_id=manifest.provider.model_id,
        execution_mode=manifest.evaluation.execution_mode,
        storage_backend=manifest.storage.backend,
    )


def _comparison_side_identity(side: ComparisonIdentity) -> ArtifactSemanticIdentity:
    """把对比产物里**一侧**的身份映射成同样的形状。"""
    return ArtifactSemanticIdentity(
        commit_sha=side.commit_sha,
        dataset_digest=side.dataset_digest,
        assertion_registry_digest=side.assertion_registry_digest,
        prompt_versions_digest=side.prompt_versions_digest,
        provider_name=side.provider_name,
        model_id=side.model_id,
        execution_mode=side.manifest_execution_mode,
        storage_backend=side.storage_backend,
    )


def _comparison_identity(comparison: EvaluationComparison) -> ArtifactSemanticIdentity:
    """对比产物在证据链里的身份（以 baseline 一侧为锚）。"""
    side = _comparison_side_identity(comparison.baseline)
    return side.model_copy(
        update={
            "commit_sha": None,
            "baseline_commit_sha": comparison.baseline.commit_sha,
            "candidate_commit_sha": comparison.candidate.commit_sha,
            "comparison_eligible": comparison.comparison_eligible,
        }
    )


def _policy_identity(policy: GatePolicy) -> ArtifactSemanticIdentity:
    """策略在证据链里的身份。"""
    return ArtifactSemanticIdentity(
        policy_id=policy.policy_id,
        policy_revision=policy.policy_revision,
        dataset_digest=policy.scope.dataset_digest,
        assertion_registry_digest=policy.scope.assertion_registry_digest,
    )


def _decision_identity(decision: GateDecision) -> ArtifactSemanticIdentity:
    """门禁结论在证据链里的身份。"""
    return ArtifactSemanticIdentity(
        gate_outcome=decision.outcome,
        baseline_commit_sha=decision.identity.baseline_commit_sha,
        candidate_commit_sha=decision.identity.candidate_commit_sha,
        policy_id=decision.identity.policy_id,
        policy_revision=decision.identity.policy_revision,
    )


def _chain_identity(chain: _Chain) -> EvidenceChainIdentity:
    """从五份已经加载的输入**重算**证据链身份。"""
    decision = chain.gate_decision
    return EvidenceChainIdentity(
        baseline_commit_sha=chain.comparison.baseline.commit_sha,
        candidate_commit_sha=chain.comparison.candidate.commit_sha,
        dataset_digest=chain.comparison.baseline.dataset_digest,
        assertion_registry_digest=chain.comparison.baseline.assertion_registry_digest,
        comparison_schema_version=chain.comparison.comparison_schema_version,
        comparison_definition_digest=chain.comparison.comparison_definition_digest,
        policy_schema_version=chain.policy.policy_schema_version,
        policy_id=chain.policy.policy_id,
        policy_revision=chain.policy.policy_revision,
        policy_digest=chain.policy.policy_digest,
        gate_decision_schema_version=decision.gate_decision_schema_version,
        gate_definition_digest=decision.gate_definition_digest,
        gate_outcome=decision.outcome,
    )


def _schema_identity(
    *, schema_version: int, definition_digest: str | None
) -> ArtifactSchemaIdentity:
    return ArtifactSchemaIdentity(
        schema_version=schema_version, definition_digest=definition_digest
    )


def _descriptors(chain: _Chain, raw: dict[ArtifactRole, bytes]) -> tuple[ArtifactDescriptor, ...]:
    """按固定角色顺序构造五个描述符。"""
    run_schemas = (chain.baseline_run, chain.candidate_run)
    identities: dict[ArtifactRole, ArtifactSemanticIdentity] = {
        ArtifactRole.BASELINE_RUN: _run_identity(chain.baseline_run),
        ArtifactRole.CANDIDATE_RUN: _run_identity(chain.candidate_run),
        ArtifactRole.COMPARISON: _comparison_identity(chain.comparison),
        ArtifactRole.POLICY: _policy_identity(chain.policy),
        ArtifactRole.GATE_DECISION: _decision_identity(chain.gate_decision),
    }
    schemas: dict[ArtifactRole, ArtifactSchemaIdentity] = {
        ArtifactRole.BASELINE_RUN: _schema_identity(
            schema_version=run_schemas[0].schema_version, definition_digest=None
        ),
        ArtifactRole.CANDIDATE_RUN: _schema_identity(
            schema_version=run_schemas[1].schema_version, definition_digest=None
        ),
        ArtifactRole.COMPARISON: _schema_identity(
            schema_version=chain.comparison.comparison_schema_version,
            definition_digest=chain.comparison.comparison_definition_digest,
        ),
        ArtifactRole.POLICY: _schema_identity(
            schema_version=chain.policy.policy_schema_version,
            definition_digest=chain.policy.policy_digest,
        ),
        ArtifactRole.GATE_DECISION: _schema_identity(
            schema_version=chain.gate_decision.gate_decision_schema_version,
            definition_digest=chain.gate_decision.gate_definition_digest,
        ),
    }

    return tuple(
        ArtifactDescriptor(
            role=role,
            content_sha256=_sha256(raw[role]),
            byte_length=len(raw[role]),
            schema_identity=schemas[role],
            semantic_identity=identities[role],
        )
        for role in ARTIFACT_ROLE_ORDER
    )


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------


def _read_raw(inputs: EvidenceInputs) -> dict[ArtifactRole, bytes]:
    """按固定角色顺序读原始字节。空文件不是合法的证据。"""
    raw: dict[ArtifactRole, bytes] = {}
    for role, path in inputs.by_role():
        try:
            data = path.read_bytes()
        except OSError as exc:
            msg = f"{role} 读取失败（{type(exc).__name__}）"
            raise EvidenceInputError(msg) from exc
        if not data:
            msg = f"{role} 是空文件——空文件不是合法的证据"
            raise EvidenceInputError(msg)
        # 🔴 在正式加载器之前先过一遍严格 JSON：S5 的两份加载器不拒绝
        # 重复的键，而"链里写了两份 chain_identity"必须被拦下。
        _decode_json(data)
        raw[role] = data
    return raw


def _load_chain(inputs: EvidenceInputs) -> _Chain:
    """用**正式加载器**把五份输入读成模型。"""
    try:
        baseline = load_run_result(inputs.baseline_run)
        candidate = load_run_result(inputs.candidate_run)
    except ComparisonInputError as exc:
        msg = f"运行结果不可用：{exc}"
        raise EvidenceInputError(msg) from exc

    try:
        comparison = load_comparison(inputs.comparison)
    except ComparisonInputError as exc:
        msg = f"对比产物不可用：{exc}"
        raise EvidenceInputError(msg) from exc

    try:
        policy = load_gate_policy(inputs.policy)
    except PolicyInputError as exc:
        msg = f"策略不可用：{exc}"
        raise EvidenceInputError(msg) from exc

    try:
        decision = load_gate_decision(inputs.gate_decision)
    except GateDecisionInputError as exc:
        msg = f"门禁结论不可用：{exc}"
        raise EvidenceInputError(msg) from exc

    return _Chain(
        baseline_run=baseline,
        candidate_run=candidate,
        comparison=comparison,
        policy=policy,
        gate_decision=decision,
    )


def _differing_fields(left: BaseModel, right: BaseModel) -> tuple[str, ...]:
    """两份模型**canonical 结构**里取值不同的顶层字段名。

    ⚠️ 只回字段名，**不回字段值**：失败信息会被打到 stderr，而字段值可能
    含回答正文之类不该扩散的东西。
    """
    left_payload = left.model_dump(mode="json")
    right_payload = right.model_dump(mode="json")
    keys = set(left_payload) | set(right_payload)
    return tuple(sorted(key for key in keys if left_payload.get(key) != right_payload.get(key)))


def _require_recomputed_equal(chain: _Chain) -> None:
    """🔴 构建前**端到端重算**，任一环节对不上就拒绝构建。

    用**完整 canonical 结构**比较，不是只比 outcome、不是只比摘要、
    不是只比 commit SHA——那些比法都会放过"规则明细被改了"这类改动。
    """
    recomputed_comparison = compare_run_results(chain.baseline_run, chain.candidate_run)
    if _canonical(recomputed_comparison) != _canonical(chain.comparison):
        mismatches = _differing_fields(recomputed_comparison, chain.comparison)
        msg = f"重算的对比与输入不一致——这条链不是一条链。（不一致的顶层字段：{list(mismatches)}）"
        raise EvidenceChainMismatchError(msg, mismatches=mismatches)

    recomputed_decision = decide(chain.comparison, chain.policy)
    if _canonical(recomputed_decision) != _canonical(chain.gate_decision):
        mismatches = _differing_fields(recomputed_decision, chain.gate_decision)
        msg = (
            "重算的门禁结论与输入不一致——这条链不是一条链。"
            f"（不一致的顶层字段：{list(mismatches)}）"
        )
        raise EvidenceChainMismatchError(msg, mismatches=mismatches)


def build_evidence_bundle(inputs: EvidenceInputs) -> EvaluationEvidenceBundle:
    """读五个输入、**端到端重算**、产出一份确定性的证据包。

    🔴 **它不只是把五个文件哈希一遍装进 JSON。** 构建之前必须：
    严格加载五份输入 → 用正式 S5 引擎从两份 Run 重算 Comparison 并逐字段
    比对 → 用正式 S6 引擎从 Comparison + Policy 重算 GateDecision 并逐字段
    比对。**只有全部一致时才产出 Bundle。**

    不一致时：不产出成功 Bundle、不自动覆盖输入、**不输出"修正后"的**
    Comparison 或 GateDecision。那两份"修正后"的产物一旦流出，就会被当成
    "评测真的跑出了这个结果"。

    ⚠️ GateDecision 的 ``outcome`` **不影响构建能否成功**：PASS / FAIL /
    NOT_EVALUATED 三条自洽的链都可以建出 Bundle。

    Args:
        inputs: 五个角色显式的输入路径。

    Returns:
        证据包。

    Raises:
        EvidenceInputError: 某份输入读不出来、空文件、格式不对。
        EvidenceChainMismatchError: 重算结果与输入对不上。
    """
    raw = _read_raw(inputs)
    chain = _load_chain(inputs)
    _require_recomputed_equal(chain)

    # 🔴 摘要是**自排除**的，所以先造一份**不经校验**的草稿（``model_construct``
    # 正是为"模型还没法被校验、但它得先存在"这种场合准备的），按它算出摘要
    # 回填，再让**严格模型**把整份 payload 完整校验一遍。
    #
    # ⚠️ 不能用 ``model_copy`` 回填：它按设计**不重跑校验器**，那样写出来的
    # 会是一份从未被严格模型检查过的产物——"build 只输出严格模型"就成了空话。
    draft = EvaluationEvidenceBundle.model_construct(
        evidence_bundle_schema_version=EVIDENCE_BUNDLE_SCHEMA_VERSION,
        evidence_bundle_definition_digest=evidence_definition_digest(),
        bundle_digest=_DIGEST_PLACEHOLDER,
        artifacts=_descriptors(chain, raw),
        chain_identity=_chain_identity(chain),
    )
    payload = bundle_payload(draft)
    payload["bundle_digest"] = bundle_digest(payload)
    return EvaluationEvidenceBundle.model_validate(payload)


# ---------------------------------------------------------------------------
# 验证契约
# ---------------------------------------------------------------------------


class VerificationOutcome(StrEnum):
    """一条证据链的**完整性结论**。闭合集合。

    🔴 它与质量门禁的结论**没有关系**：``VERIFIED`` 不表示 Candidate 好、
    不表示可以发布，只表示"给定文件集合内部一致且可重算"。
    """

    VERIFIED = "VERIFIED"
    INVALID = "INVALID"
    NOT_VERIFIABLE = "NOT_VERIFIABLE"


class VerificationCheckOutcome(StrEnum):
    """单项检查的结论。闭合集合。"""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_EVALUATED = "NOT_EVALUATED"


class VerificationReason(StrEnum):
    """检查项的**原因码**。闭合集合，稳定输出。"""

    SATISFIED = "satisfied"

    # ---- Bundle 自身：读不出来 ----
    BUNDLE_UNAVAILABLE = "bundle_unavailable"
    BUNDLE_NOT_PARSABLE = "bundle_not_parsable"
    BUNDLE_UNSUPPORTED_VERSION = "bundle_unsupported_version"
    # ---- Bundle 自身：确定冲突 ----
    BUNDLE_DIGEST_MISMATCH = "bundle_digest_mismatch"
    ARTIFACT_ROLE_MISSING = "artifact_role_missing"
    ARTIFACT_ROLE_DUPLICATED = "artifact_role_duplicated"
    ARTIFACT_ORDER_INVALID = "artifact_order_invalid"

    # ---- 输入：读不出来 ----
    ARTIFACT_UNAVAILABLE = "artifact_unavailable"
    ARTIFACT_NOT_PARSABLE = "artifact_not_parsable"
    ARTIFACT_UNSUPPORTED_VERSION = "artifact_unsupported_version"
    RECOMPUTATION_UNAVAILABLE = "recomputation_unavailable"
    NOT_REACHED = "not_reached"

    # ---- 输入：确定冲突 ----
    CONTENT_DIGEST_MISMATCH = "content_digest_mismatch"
    BYTE_LENGTH_MISMATCH = "byte_length_mismatch"
    CHAIN_IDENTITY_MISMATCH = "chain_identity_mismatch"
    ROLE_BINDING_MISMATCH = "role_binding_mismatch"
    RECOMPUTATION_MISMATCH = "recomputation_mismatch"


#: 通过时唯一允许的原因码。
_SATISFIED_REASONS: Final[frozenset[VerificationReason]] = frozenset({VerificationReason.SATISFIED})

#: ``FAIL`` 只能是这些原因——``FAIL`` 的含义是**确定冲突**，
#: 而"读不出来"永远不构成冲突。
_MISMATCH_REASONS: Final[frozenset[VerificationReason]] = frozenset(
    {
        VerificationReason.BUNDLE_DIGEST_MISMATCH,
        VerificationReason.ARTIFACT_ROLE_MISSING,
        VerificationReason.ARTIFACT_ROLE_DUPLICATED,
        VerificationReason.ARTIFACT_ORDER_INVALID,
        VerificationReason.CONTENT_DIGEST_MISMATCH,
        VerificationReason.BYTE_LENGTH_MISMATCH,
        VerificationReason.CHAIN_IDENTITY_MISMATCH,
        VerificationReason.ROLE_BINDING_MISMATCH,
        VerificationReason.RECOMPUTATION_MISMATCH,
    }
)

#: ``NOT_EVALUATED`` 只能是这些原因——全部是"这次没得到结论"。
_UNAVAILABLE_REASONS: Final[frozenset[VerificationReason]] = frozenset(
    {
        VerificationReason.BUNDLE_UNAVAILABLE,
        VerificationReason.BUNDLE_NOT_PARSABLE,
        VerificationReason.BUNDLE_UNSUPPORTED_VERSION,
        VerificationReason.ARTIFACT_UNAVAILABLE,
        VerificationReason.ARTIFACT_NOT_PARSABLE,
        VerificationReason.ARTIFACT_UNSUPPORTED_VERSION,
        VerificationReason.RECOMPUTATION_UNAVAILABLE,
        VerificationReason.NOT_REACHED,
    }
)

#: 🔴 这些原因说明"**连形状都读不出来**"，退出码因此是 ``3`` 而不是 ``4``：
#: 与"文件缺失""版本不受支持""证据不足"是两种不同的事。
_PARSE_LEVEL_REASONS: Final[frozenset[VerificationReason]] = frozenset(
    {
        VerificationReason.BUNDLE_UNAVAILABLE,
        VerificationReason.BUNDLE_NOT_PARSABLE,
        VerificationReason.ARTIFACT_NOT_PARSABLE,
    }
)


class VerificationCheck(BaseModel):
    """一项检查的结构化结论。

    🔴 **只放稳定结构**：检查 ID、结论、原因码，以及两个**安全的**观察值。
    没有文件内容、没有回答正文、没有异常堆栈、没有绝对路径。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    check_id: str = Field(min_length=1)
    outcome: VerificationCheckOutcome
    reason_code: VerificationReason
    #: 观察到的值：摘要、字节长度、角色名、版本号，或某个身份的摘要。
    observed: str | None = None
    expected: str | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        """结论与原因码必须自洽。

        🔴 允许一个"通过但原因码写着冲突"的检查存在，等于把"什么算通过"
        交给每个调用点各写一遍。
        """
        if self.outcome is VerificationCheckOutcome.PASS:
            if self.reason_code not in _SATISFIED_REASONS:
                msg = f"检查 {self.check_id!r} 通过了，原因码却是 {self.reason_code}"
                raise ValueError(msg)
        elif self.outcome is VerificationCheckOutcome.FAIL:
            if self.reason_code not in _MISMATCH_REASONS:
                msg = (
                    f"检查 {self.check_id!r} 判为冲突（FAIL），"
                    f"而 {self.reason_code} 不是冲突原因——读不出来的东西不构成冲突"
                )
                raise ValueError(msg)
        elif self.reason_code not in _UNAVAILABLE_REASONS:
            msg = f"检查 {self.check_id!r} 未得到结论，原因码却是 {self.reason_code}"
            raise ValueError(msg)
        return self


#: 检查项的**固定顺序**。它进 :data:`EVIDENCE_VERIFICATION_DEFINITION`。
VERIFICATION_CHECK_ORDER: Final[tuple[str, ...]] = (
    "bundle_schema_valid",
    "bundle_digest_valid",
    "artifact_roles_complete",
    "artifact_order_valid",
    "baseline_content_digest_valid",
    "candidate_content_digest_valid",
    "comparison_content_digest_valid",
    "policy_content_digest_valid",
    "gate_decision_content_digest_valid",
    "baseline_byte_length_valid",
    "candidate_byte_length_valid",
    "comparison_byte_length_valid",
    "policy_byte_length_valid",
    "gate_decision_byte_length_valid",
    "baseline_schema_valid",
    "candidate_schema_valid",
    "comparison_schema_valid",
    "policy_schema_valid",
    "gate_decision_schema_valid",
    "chain_identity_valid",
    "baseline_role_valid",
    "candidate_role_valid",
    "comparison_recomputed_equal",
    "gate_decision_recomputed_equal",
)

_CHECK_BUNDLE_SCHEMA_VALID: Final[str] = "bundle_schema_valid"
_CHECK_BUNDLE_DIGEST_VALID: Final[str] = "bundle_digest_valid"
_CHECK_ARTIFACT_ROLES_COMPLETE: Final[str] = "artifact_roles_complete"
_CHECK_ARTIFACT_ORDER_VALID: Final[str] = "artifact_order_valid"
_CHECK_CHAIN_IDENTITY_VALID: Final[str] = "chain_identity_valid"
_CHECK_BASELINE_ROLE_VALID: Final[str] = "baseline_role_valid"
_CHECK_CANDIDATE_ROLE_VALID: Final[str] = "candidate_role_valid"
_CHECK_COMPARISON_RECOMPUTED_EQUAL: Final[str] = "comparison_recomputed_equal"
_CHECK_GATE_DECISION_RECOMPUTED_EQUAL: Final[str] = "gate_decision_recomputed_equal"

#: 角色 → 三组按角色展开的检查 ID。**逐字**对应任务书 §十三。
_CONTENT_DIGEST_CHECK: Final[dict[ArtifactRole, str]] = {
    ArtifactRole.BASELINE_RUN: "baseline_content_digest_valid",
    ArtifactRole.CANDIDATE_RUN: "candidate_content_digest_valid",
    ArtifactRole.COMPARISON: "comparison_content_digest_valid",
    ArtifactRole.POLICY: "policy_content_digest_valid",
    ArtifactRole.GATE_DECISION: "gate_decision_content_digest_valid",
}
_BYTE_LENGTH_CHECK: Final[dict[ArtifactRole, str]] = {
    ArtifactRole.BASELINE_RUN: "baseline_byte_length_valid",
    ArtifactRole.CANDIDATE_RUN: "candidate_byte_length_valid",
    ArtifactRole.COMPARISON: "comparison_byte_length_valid",
    ArtifactRole.POLICY: "policy_byte_length_valid",
    ArtifactRole.GATE_DECISION: "gate_decision_byte_length_valid",
}
_SCHEMA_CHECK: Final[dict[ArtifactRole, str]] = {
    ArtifactRole.BASELINE_RUN: "baseline_schema_valid",
    ArtifactRole.CANDIDATE_RUN: "candidate_schema_valid",
    ArtifactRole.COMPARISON: "comparison_schema_valid",
    ArtifactRole.POLICY: "policy_schema_valid",
    ArtifactRole.GATE_DECISION: "gate_decision_schema_valid",
}
_ROLE_CHECK: Final[dict[ArtifactRole, str]] = {
    ArtifactRole.BASELINE_RUN: _CHECK_BASELINE_ROLE_VALID,
    ArtifactRole.CANDIDATE_RUN: _CHECK_CANDIDATE_ROLE_VALID,
}


class EvidenceVerificationReport(BaseModel):
    """一次证据链校验的完整产物。

    🔴 ``verification_outcome`` 与 ``gate_outcome`` 是**两个独立维度**：
    前者说"这条链是不是内部一致"，后者说"门禁判了什么"。两者一起出现，
    但谁也不决定谁。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    verification_schema_version: int = VERIFICATION_SCHEMA_VERSION
    verification_definition_digest: str = Field(pattern=_DIGEST_PATTERN)
    #: 本次校验**实际读进来**的那份 Bundle 的内容摘要（由 payload 重算）。
    #: Bundle 读不出来时为 ``None``。⚠️ 它不是签名。
    bundle_digest: str | None = Field(default=None, pattern=_DIGEST_PATTERN)
    #: 门禁结论的 outcome。它是**被校验的数据**，不是本次校验的结论。
    gate_outcome: GateOutcome | None = None
    verification_outcome: VerificationOutcome
    checks: tuple[VerificationCheck, ...]
    failed_check_ids: tuple[str, ...]
    not_evaluated_check_ids: tuple[str, ...]

    @model_validator(mode="after")
    def _check(self) -> Self:
        """🔴 把"结论与检查明细必须一致"变成**构造失败**。

        一份"整体 INVALID 却没有任何一项 FAIL"的报告，读起来像一次
        严肃的判定，实际什么也没判。
        """
        expected_ids = VERIFICATION_CHECK_ORDER
        if tuple(item.check_id for item in self.checks) != expected_ids:
            msg = "检查项必须完整、且按固定顺序出现"
            raise ValueError(msg)

        failed = tuple(
            item.check_id for item in self.checks if item.outcome is VerificationCheckOutcome.FAIL
        )
        not_evaluated = tuple(
            item.check_id
            for item in self.checks
            if item.outcome is VerificationCheckOutcome.NOT_EVALUATED
        )
        if failed != self.failed_check_ids:
            msg = f"failed_check_ids={list(self.failed_check_ids)} 与检查明细不一致"
            raise ValueError(msg)
        if not_evaluated != self.not_evaluated_check_ids:
            msg = f"not_evaluated_check_ids={list(self.not_evaluated_check_ids)} 与检查明细不一致"
            raise ValueError(msg)

        if self.verification_outcome is VerificationOutcome.VERIFIED:
            if failed or not_evaluated:
                msg = "VERIFIED 要求**全部**检查通过"
                raise ValueError(msg)
        elif self.verification_outcome is VerificationOutcome.INVALID:
            if not failed:
                msg = "INVALID 要求至少一项检查明确冲突（FAIL）"
                raise ValueError(msg)
        else:  # NOT_VERIFIABLE
            if failed:
                msg = "存在确定冲突时结论必须是 INVALID，不能降级成 NOT_VERIFIABLE"
                raise ValueError(msg)
            if not not_evaluated:
                msg = "NOT_VERIFIABLE 要求至少一项检查没得到结论"
                raise ValueError(msg)
        return self

    def parse_level_failure(self) -> bool:
        """是否属于"连形状都读不出来"那一类 ``NOT_VERIFIABLE``。

        它决定 verify CLI 的退出码是 ``3`` 还是 ``4``。
        """
        return any(
            item.outcome is VerificationCheckOutcome.NOT_EVALUATED
            and item.reason_code in _PARSE_LEVEL_REASONS
            for item in self.checks
        )


def write_verification_report(report: EvidenceVerificationReport, path: Path) -> None:
    """把验证报告原子地写到磁盘（格式约定同 :func:`write_bundle`）。

    Raises:
        OSError: 写盘失败。**不吞掉**。
    """
    _atomic_write_text(path, dumps(report.model_dump(mode="json")))


# ---------------------------------------------------------------------------
# 验证
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceVerificationInputs:
    """要验的证据包 + 五个角色显式的输入路径。"""

    bundle: Path
    artifacts: EvidenceInputs


def _check(
    check_id: str,
    outcome: VerificationCheckOutcome,
    reason: VerificationReason,
    *,
    observed: str | None = None,
    expected: str | None = None,
) -> VerificationCheck:
    return VerificationCheck(
        check_id=check_id,
        outcome=outcome,
        reason_code=reason,
        observed=observed,
        expected=expected,
    )


def _not_evaluated(
    check_id: str,
    reason: VerificationReason,
    *,
    observed: str | None = None,
    expected: str | None = None,
) -> VerificationCheck:
    return _check(
        check_id,
        VerificationCheckOutcome.NOT_EVALUATED,
        reason,
        observed=observed,
        expected=expected,
    )


def _verdict(
    check_id: str,
    ok: bool,
    reason: VerificationReason,
    *,
    observed: str | None = None,
    expected: str | None = None,
) -> VerificationCheck:
    """二元检查的构造器：通过就是 ``SATISFIED``，否则就是给定的冲突原因。"""
    outcome = VerificationCheckOutcome.PASS if ok else VerificationCheckOutcome.FAIL
    return _check(
        check_id,
        outcome,
        VerificationReason.SATISFIED if ok else reason,
        observed=observed,
        expected=expected,
    )


def _role_check(
    role: ArtifactRole,
    run: RunResult | None,
    comparison: EvaluationComparison | None,
    descriptors: dict[ArtifactRole, ArtifactDescriptor],
) -> VerificationCheck:
    """🔴 一方在对比里坐的是不是**它自己那个位置**。

    两件事一起核对：

    1. 运行结果的身份与对比里**同角色**那一侧是否逐字段相同；
    2. Bundle 里记的语义身份与实际读出来的运行结果是否一致。

    第 1 条防的是**角色交换**：把两份结果对调之后，候选的提交号会坐到
    基线那一栏上，两边对不上。第 2 条防的是 Bundle 被改过。
    """
    check_id = _ROLE_CHECK[role]
    item = descriptors.get(role)
    if run is None or comparison is None or item is None:
        return _not_evaluated(check_id, VerificationReason.NOT_REACHED)
    side = comparison.baseline if role is ArtifactRole.BASELINE_RUN else comparison.candidate
    from_run = _run_identity(run)
    from_comparison = _comparison_side_identity(side)
    return _verdict(
        check_id,
        from_run == from_comparison and item.semantic_identity == from_run,
        VerificationReason.ROLE_BINDING_MISMATCH,
        observed=_digest(from_run.model_dump(mode="json")),
        expected=_digest(from_comparison.model_dump(mode="json")),
    )


def _safe_version_text(payload: dict[str, Any] | None, field: str) -> str | None:
    """从**未受信**的 payload 里取一个版本号，且**只接受整数**。

    把未受信 payload 里的任意值原样写进报告，等于给了一份外部文件一条
    通往产物的通道。版本号是整数，就只回整数。
    """
    if payload is None:
        return None
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return str(value)


#: 角色 → 该产物声明自身版本时用的字段名。
_VERSION_FIELD: Final[dict[ArtifactRole, str]] = {
    ArtifactRole.BASELINE_RUN: "schema_version",
    ArtifactRole.CANDIDATE_RUN: "schema_version",
    ArtifactRole.COMPARISON: "comparison_schema_version",
    ArtifactRole.POLICY: "policy_schema_version",
    ArtifactRole.GATE_DECISION: "gate_decision_schema_version",
}

#: 角色 → 本版本支持的版本号。
_SUPPORTED_VERSION: Final[dict[ArtifactRole, int]] = {
    ArtifactRole.BASELINE_RUN: RESULT_SCHEMA_VERSION,
    ArtifactRole.CANDIDATE_RUN: RESULT_SCHEMA_VERSION,
    ArtifactRole.COMPARISON: 1,
    ArtifactRole.POLICY: POLICY_SCHEMA_VERSION,
    ArtifactRole.GATE_DECISION: GATE_DECISION_SCHEMA_VERSION,
}

#: 角色 → 该产物"按哪套定义算出来"的字段名（没有这一栏的记为 ``None``）。
_DEFINITION_FIELD: Final[dict[ArtifactRole, str | None]] = {
    ArtifactRole.BASELINE_RUN: None,
    ArtifactRole.CANDIDATE_RUN: None,
    ArtifactRole.COMPARISON: "comparison_definition_digest",
    ArtifactRole.POLICY: None,
    ArtifactRole.GATE_DECISION: "gate_definition_digest",
}


def _supported_definition(role: ArtifactRole) -> str | None:
    """本版本该角色的定义摘要，没有这一栏的返回 ``None``。"""
    if role is ArtifactRole.COMPARISON:
        return comparison_definition_digest()
    if role is ArtifactRole.GATE_DECISION:
        return gate_definition_digest()
    return None


def _precheck(
    data: bytes | None, role: ArtifactRole
) -> tuple[dict[str, Any] | None, VerificationReason | None]:
    """正式加载器之前的形状检查。

    它区分三件必须在报告里分开的事：**读不到**、**读得出但版本不认识**、
    **压根不是 JSON**。三者的处置不同，混成一个码就等于说不清发生了什么。

    Returns:
        ``(payload, None)`` 表示可以继续走正式加载器；
        ``(payload 或 None, 原因码)`` 表示到此为止。
    """
    if data is None:
        return None, VerificationReason.ARTIFACT_UNAVAILABLE
    try:
        payload = _decode_json(data)
    except EvidenceInputError:
        return None, VerificationReason.ARTIFACT_NOT_PARSABLE

    # ⚠️ ``bool`` 是 ``int`` 的子类，必须排掉，否则 ``true`` 会被当成版本 1。
    version = payload.get(_VERSION_FIELD[role])
    if (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version != _SUPPORTED_VERSION[role]
    ):
        return payload, VerificationReason.ARTIFACT_UNSUPPORTED_VERSION

    field = _DEFINITION_FIELD[role]
    if field is not None:
        declared = payload.get(field)
        supported = _supported_definition(role)
        if isinstance(declared, str) and supported is not None and declared != supported:
            # 契约换了：这份产物不是按本版本的规则算出来的，重算无从谈起。
            return payload, VerificationReason.ARTIFACT_UNSUPPORTED_VERSION
    return payload, None


def _load_run(
    path: Path, data: bytes | None, role: ArtifactRole
) -> tuple[RunResult | None, VerificationReason | None, dict[str, Any] | None]:
    payload, reason = _precheck(data, role)
    if reason is not None:
        return None, reason, payload
    try:
        return load_run_result(path), None, payload
    except ComparisonInputError:
        return None, VerificationReason.ARTIFACT_NOT_PARSABLE, payload


def _load_comparison(
    path: Path, data: bytes | None
) -> tuple[EvaluationComparison | None, VerificationReason | None, dict[str, Any] | None]:
    payload, reason = _precheck(data, ArtifactRole.COMPARISON)
    if reason is not None:
        return None, reason, payload
    try:
        return load_comparison(path), None, payload
    except ComparisonInputError:
        return None, VerificationReason.ARTIFACT_NOT_PARSABLE, payload


def _load_policy(
    path: Path, data: bytes | None
) -> tuple[GatePolicy | None, VerificationReason | None, dict[str, Any] | None]:
    payload, reason = _precheck(data, ArtifactRole.POLICY)
    if reason is not None:
        return None, reason, payload
    try:
        return load_gate_policy(path), None, payload
    except PolicyInputError:
        return None, VerificationReason.ARTIFACT_NOT_PARSABLE, payload


def _load_decision(
    path: Path, data: bytes | None
) -> tuple[GateDecision | None, VerificationReason | None, dict[str, Any] | None]:
    payload, reason = _precheck(data, ArtifactRole.GATE_DECISION)
    if reason is not None:
        return None, reason, payload
    try:
        return load_gate_decision(path), None, payload
    except GateDecisionInputError:
        return None, VerificationReason.ARTIFACT_NOT_PARSABLE, payload


def _read_artifact(
    path: Path, unavailable: VerificationReason
) -> tuple[bytes | None, VerificationReason | None]:
    try:
        return path.read_bytes(), None
    except OSError:
        return None, unavailable


def verify_evidence_bundle(inputs: EvidenceVerificationInputs) -> EvidenceVerificationReport:
    """对一条证据链做端到端核验，产出确定性报告。

    🔴 两个维度**互不推断**：本函数不看 ``gate_outcome`` 来决定
    ``verification_outcome``，也不因为 ``verification_outcome=VERIFIED``
    就认为候选没问题。

    Args:
        inputs: Bundle 路径 + 五个角色显式的输入路径。

    Returns:
        结构化验证报告。**无论结论是三种里的哪一种都会返回**——
        失败路径也要有报告，"没有报告"会让人分不清"没跑"与"跑挂了"。
    """
    checks: list[VerificationCheck] = []

    # ---- Bundle 自身 ----
    raw_bundle, _ = _read_artifact(inputs.bundle, VerificationReason.BUNDLE_UNAVAILABLE)
    bundle_document: dict[str, Any] | None = None
    bundle_reason: VerificationReason | None = None
    if raw_bundle is None:
        bundle_reason = VerificationReason.BUNDLE_UNAVAILABLE
    else:
        try:
            bundle_document = _decode_json(raw_bundle)
        except EvidenceInputError:
            bundle_reason = VerificationReason.BUNDLE_NOT_PARSABLE

    # 🔴 先解析**不可信取证 Envelope**：验证器的职责是"读进一份坏掉的
    # Bundle 并说清坏在哪"，所以在这一步**不**假定角色是完整的。
    envelope: EvidenceBundleEnvelope | None = None
    if bundle_document is not None:
        try:
            envelope = parse_evidence_bundle_envelope(bundle_document)
        except (EvidenceInputError, ValidationError):
            bundle_reason = VerificationReason.BUNDLE_NOT_PARSABLE
        else:
            if envelope.evidence_bundle_schema_version != EVIDENCE_BUNDLE_SCHEMA_VERSION:
                envelope = None
                bundle_reason = VerificationReason.BUNDLE_UNSUPPORTED_VERSION

    # 🔴 读不出来的 Bundle 记作 **NOT_EVALUATED**，不是 FAIL：FAIL 的含义是
    # "确定冲突"，而"这份文件根本读不出来"是另一件事。它的原因码会把
    # 究竟是哪一种说清楚（缺失／不是 JSON／版本不认识）。
    if envelope is None:
        schema_check = _not_evaluated(
            _CHECK_BUNDLE_SCHEMA_VALID,
            bundle_reason or VerificationReason.BUNDLE_NOT_PARSABLE,
            observed=_safe_version_text(bundle_document, "evidence_bundle_schema_version"),
            expected=str(EVIDENCE_BUNDLE_SCHEMA_VERSION),
        )
    else:
        schema_check = _check(
            _CHECK_BUNDLE_SCHEMA_VALID,
            VerificationCheckOutcome.PASS,
            VerificationReason.SATISFIED,
            observed=_safe_version_text(bundle_document, "evidence_bundle_schema_version"),
            expected=str(EVIDENCE_BUNDLE_SCHEMA_VERSION),
        )
    checks.append(schema_check)

    recomputed_bundle_digest: str | None = None
    if envelope is None or bundle_document is None:
        checks.append(_not_evaluated(_CHECK_BUNDLE_DIGEST_VALID, VerificationReason.NOT_REACHED))
    else:
        recomputed_bundle_digest = bundle_digest(bundle_document)
        checks.append(
            _verdict(
                _CHECK_BUNDLE_DIGEST_VALID,
                envelope.bundle_digest == recomputed_bundle_digest,
                VerificationReason.BUNDLE_DIGEST_MISMATCH,
                observed=recomputed_bundle_digest,
                expected=envelope.bundle_digest,
            )
        )

    # ---- 角色集合与顺序 ----
    roles: tuple[ArtifactRole, ...] = (
        () if envelope is None else tuple(item.role for item in envelope.artifacts)
    )
    roles_complete = False
    roles_ordered = False
    if envelope is None:
        checks.append(
            _not_evaluated(_CHECK_ARTIFACT_ROLES_COMPLETE, VerificationReason.NOT_REACHED)
        )
        checks.append(_not_evaluated(_CHECK_ARTIFACT_ORDER_VALID, VerificationReason.NOT_REACHED))
    else:
        missing = [role for role in ARTIFACT_ROLE_ORDER if role not in roles]
        # ⚠️ 用**计数**而不是集合：集合会把"同一角色出现两次"悄悄抹平，
        # 而那正是这里要抓的东西之一。
        duplicated = sorted({role for role in roles if roles.count(role) > 1})
        roles_complete = not missing and not duplicated
        if missing:
            roles_reason = VerificationReason.ARTIFACT_ROLE_MISSING
        elif duplicated:
            roles_reason = VerificationReason.ARTIFACT_ROLE_DUPLICATED
        else:
            roles_reason = VerificationReason.SATISFIED
        checks.append(
            _verdict(
                _CHECK_ARTIFACT_ROLES_COMPLETE,
                roles_complete,
                roles_reason,
                observed=",".join(role.value for role in roles),
                expected=",".join(role.value for role in ARTIFACT_ROLE_ORDER),
            )
        )
        if not roles_complete:
            checks.append(
                _not_evaluated(_CHECK_ARTIFACT_ORDER_VALID, VerificationReason.NOT_REACHED)
            )
        else:
            roles_ordered = roles == ARTIFACT_ROLE_ORDER
            checks.append(
                _verdict(
                    _CHECK_ARTIFACT_ORDER_VALID,
                    roles_ordered,
                    VerificationReason.ARTIFACT_ORDER_INVALID,
                    observed=",".join(role.value for role in roles),
                    expected=",".join(role.value for role in ARTIFACT_ROLE_ORDER),
                )
            )

    # ---- 提升：只有角色完整、唯一、有序时才把它当正式 Bundle ----
    #
    # 🔴 提升失败（角色不对，或 ``bundle_digest`` 对不上）时**绝不继续**：
    # 不去重、不重排、不补齐，也不拿重复角色里的"第一个"或"最后一个"凑合。
    # 后续所有依赖唯一角色映射的检查一律 NOT_EVALUATED——包括 S5 与 S6 的
    # 重算入口，那两处**一次都不会被调用**。
    bundle: EvaluationEvidenceBundle | None = None
    if envelope is not None and roles_complete and roles_ordered:
        try:
            bundle = promote_evidence_bundle(envelope)
        except EvidenceBundleInvariantError:
            bundle = None

    descriptors: dict[ArtifactRole, ArtifactDescriptor] = (
        {} if bundle is None else {item.role: item for item in bundle.artifacts}
    )

    # ---- 读五个输入（只有拿到严格 Bundle 才读）----
    read: dict[ArtifactRole, bytes | None] = {}
    if bundle is not None:
        for role, path in inputs.artifacts.by_role():
            data, _ = _read_artifact(path, VerificationReason.ARTIFACT_UNAVAILABLE)
            read[role] = data

    def descriptor(role: ArtifactRole) -> ArtifactDescriptor | None:
        return descriptors.get(role)

    # ---- 5-9：内容摘要 ----
    for role in ARTIFACT_ROLE_ORDER:
        check_id = _CONTENT_DIGEST_CHECK[role]
        item = descriptor(role)
        data = read.get(role)
        if bundle is None or item is None:
            checks.append(_not_evaluated(check_id, VerificationReason.NOT_REACHED))
        elif data is None:
            checks.append(_not_evaluated(check_id, VerificationReason.ARTIFACT_UNAVAILABLE))
        else:
            actual = _sha256(data)
            checks.append(
                _verdict(
                    check_id,
                    actual == item.content_sha256,
                    VerificationReason.CONTENT_DIGEST_MISMATCH,
                    observed=actual,
                    expected=item.content_sha256,
                )
            )

    # ---- 10-14：字节长度 ----
    for role in ARTIFACT_ROLE_ORDER:
        check_id = _BYTE_LENGTH_CHECK[role]
        item = descriptor(role)
        data = read.get(role)
        if bundle is None or item is None:
            checks.append(_not_evaluated(check_id, VerificationReason.NOT_REACHED))
        elif data is None:
            checks.append(_not_evaluated(check_id, VerificationReason.ARTIFACT_UNAVAILABLE))
        else:
            actual_length = str(len(data))
            checks.append(
                _verdict(
                    check_id,
                    len(data) == item.byte_length,
                    VerificationReason.BYTE_LENGTH_MISMATCH,
                    observed=actual_length,
                    expected=str(item.byte_length),
                )
            )

    # ---- 15-19：结构（走正式加载器）----
    skipped: tuple[None, VerificationReason, None] = (None, VerificationReason.NOT_REACHED, None)
    baseline_run, baseline_reason, baseline_doc = (
        skipped
        if bundle is None
        else _load_run(
            inputs.artifacts.baseline_run,
            read.get(ArtifactRole.BASELINE_RUN),
            ArtifactRole.BASELINE_RUN,
        )
    )
    candidate_run, candidate_reason, candidate_doc = (
        skipped
        if bundle is None
        else _load_run(
            inputs.artifacts.candidate_run,
            read.get(ArtifactRole.CANDIDATE_RUN),
            ArtifactRole.CANDIDATE_RUN,
        )
    )
    comparison, comparison_reason, comparison_doc = (
        skipped
        if bundle is None
        else _load_comparison(inputs.artifacts.comparison, read.get(ArtifactRole.COMPARISON))
    )
    policy, policy_reason, policy_doc = (
        skipped
        if bundle is None
        else _load_policy(inputs.artifacts.policy, read.get(ArtifactRole.POLICY))
    )
    decision, decision_reason, decision_doc = (
        skipped
        if bundle is None
        else _load_decision(inputs.artifacts.gate_decision, read.get(ArtifactRole.GATE_DECISION))
    )

    for role, loaded, reason, document in (
        (ArtifactRole.BASELINE_RUN, baseline_run, baseline_reason, baseline_doc),
        (ArtifactRole.CANDIDATE_RUN, candidate_run, candidate_reason, candidate_doc),
        (ArtifactRole.COMPARISON, comparison, comparison_reason, comparison_doc),
        (ArtifactRole.POLICY, policy, policy_reason, policy_doc),
        (ArtifactRole.GATE_DECISION, decision, decision_reason, decision_doc),
    ):
        check_id = _SCHEMA_CHECK[role]
        observed = _safe_version_text(document, _VERSION_FIELD[role])
        expected = str(_SUPPORTED_VERSION[role])
        # 🔴 "结构合法"不是"内容没被改"：这一项只回答"这份文件是不是
        # 它自称的那种产物"。内容有没有被改由摘要那几项回答。
        if loaded is not None and reason is None:
            checks.append(
                _check(
                    check_id,
                    VerificationCheckOutcome.PASS,
                    VerificationReason.SATISFIED,
                    observed=observed,
                    expected=expected,
                )
            )
        else:
            checks.append(
                _not_evaluated(
                    check_id,
                    reason or VerificationReason.NOT_REACHED,
                    observed=observed,
                    expected=expected,
                )
            )

    # ---- 20：链身份 ----
    if (
        bundle is None
        or baseline_run is None
        or candidate_run is None
        or comparison is None
        or policy is None
        or decision is None
    ):
        checks.append(_not_evaluated(_CHECK_CHAIN_IDENTITY_VALID, VerificationReason.NOT_REACHED))
    else:
        expected_chain = _chain_identity(
            _Chain(
                baseline_run=baseline_run,
                candidate_run=candidate_run,
                comparison=comparison,
                policy=policy,
                gate_decision=decision,
            )
        )
        checks.append(
            _verdict(
                _CHECK_CHAIN_IDENTITY_VALID,
                bundle.chain_identity == expected_chain,
                VerificationReason.CHAIN_IDENTITY_MISMATCH,
                observed=_digest(expected_chain.model_dump(mode="json")),
                expected=_digest(bundle.chain_identity.model_dump(mode="json")),
            )
        )

    # ---- 21-22：角色绑定 ----
    checks.append(_role_check(ArtifactRole.BASELINE_RUN, baseline_run, comparison, descriptors))
    checks.append(_role_check(ArtifactRole.CANDIDATE_RUN, candidate_run, comparison, descriptors))

    # ---- 23：重算对比 ----
    if baseline_run is None or candidate_run is None or comparison is None:
        checks.append(
            _not_evaluated(
                _CHECK_COMPARISON_RECOMPUTED_EQUAL, VerificationReason.RECOMPUTATION_UNAVAILABLE
            )
        )
    else:
        recomputed_comparison = compare_run_results(baseline_run, candidate_run)
        checks.append(
            _verdict(
                _CHECK_COMPARISON_RECOMPUTED_EQUAL,
                _canonical(recomputed_comparison) == _canonical(comparison),
                VerificationReason.RECOMPUTATION_MISMATCH,
                observed=_text_digest(_canonical(recomputed_comparison)),
                expected=_text_digest(_canonical(comparison)),
            )
        )

    # ---- 24：重算门禁结论 ----
    if comparison is None or policy is None or decision is None:
        checks.append(
            _not_evaluated(
                _CHECK_GATE_DECISION_RECOMPUTED_EQUAL,
                VerificationReason.RECOMPUTATION_UNAVAILABLE,
            )
        )
    else:
        recomputed_decision = decide(comparison, policy)
        checks.append(
            _verdict(
                _CHECK_GATE_DECISION_RECOMPUTED_EQUAL,
                _canonical(recomputed_decision) == _canonical(decision),
                VerificationReason.RECOMPUTATION_MISMATCH,
                observed=_text_digest(_canonical(recomputed_decision)),
                expected=_text_digest(_canonical(decision)),
            )
        )

    return _finalize(
        tuple(checks),
        bundle_digest=recomputed_bundle_digest,
        gate_outcome=None if decision is None else decision.outcome,
    )


def _finalize(
    checks: tuple[VerificationCheck, ...],
    *,
    bundle_digest: str | None,
    gate_outcome: GateOutcome | None,
) -> EvidenceVerificationReport:
    """按固定规则聚合结论。**FAIL 优先于 NOT_EVALUATED。**"""
    failed = tuple(
        item.check_id for item in checks if item.outcome is VerificationCheckOutcome.FAIL
    )
    not_evaluated = tuple(
        item.check_id for item in checks if item.outcome is VerificationCheckOutcome.NOT_EVALUATED
    )
    if failed:
        outcome = VerificationOutcome.INVALID
    elif not_evaluated:
        outcome = VerificationOutcome.NOT_VERIFIABLE
    else:
        outcome = VerificationOutcome.VERIFIED

    return EvidenceVerificationReport(
        verification_schema_version=VERIFICATION_SCHEMA_VERSION,
        verification_definition_digest=evidence_verification_definition_digest(),
        bundle_digest=bundle_digest,
        gate_outcome=gate_outcome,
        verification_outcome=outcome,
        checks=checks,
        failed_check_ids=failed,
        not_evaluated_check_ids=not_evaluated,
    )
