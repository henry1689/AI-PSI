"""认知宪法 —— 系统的不可协商边界（ADR-0011）。

🔴 **代码是唯一真相来源。** ``docs/cognitive_constitution.md`` 解释*为什么*，
本模块定义*是什么*；两者冲突时**以本模块为准**。

**修改宪法 = 修改本文件 = 走代码评审。**
不存在配置文件、环境变量或数据库字段能覆盖这里的常量——
这正是"系统不能自行改变认知宪法"（不变量 12）的**可执行**保证，
而不只是一句文档承诺。

设计约束：

* 所有常量使用 ``Final`` 与不可变容器（``frozenset`` / ``tuple``）；
* 学习模块（``learning/``）对本模块**只有读权限**（阶段 6 以导入依赖测试固定）；
* 本模块不依赖任何可变状态，不产生副作用。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from ai_psi.domain.enums import MemoryType, ProposalStatus, SensitivityLevel
from ai_psi.domain.exceptions import ConstitutionViolationError
from ai_psi.domain.hypotheses import Hypothesis
from ai_psi.domain.judgments import Judgment
from ai_psi.domain.memories import Memory
from ai_psi.domain.user_models import UserModel

__all__ = [
    "AUTO_PROMOTION_FORBIDDEN_VALUES",
    "AUTO_WRITABLE_MEMORY_TYPES",
    "CONSTITUTION_VERSION",
    "FORBIDDEN_MEMORY_CONTENT_CLASSES",
    "INVARIANTS",
    "Invariant",
    "assert_hypothesis_not_fact",
    "assert_memory_retrievable_by",
    "assert_no_automatic_promotion",
    "assert_response_not_stronger_than_judgment",
    "assert_user_model_not_confirmed",
    "constitution_fingerprint",
    "invariant",
]

#: 宪法版本。修改任何不变量时递增，并同步更新 ``docs/cognitive_constitution.md``。
CONSTITUTION_VERSION: Final[str] = "1.0.0"


@dataclass(frozen=True, slots=True)
class Invariant:
    """一条认知不变量的元数据。

    Attributes:
        invariant_id: 形如 ``"I07"`` 的标识，用于日志与测试定位。
        statement: 不变量的一句话陈述（与任务书 §14 一致）。
        enforcement: 它在何处被强制——必须是**代码机制**，不是文档约定。
        enforceable_in_stage: 能被自动化测试覆盖的阶段。阶段 1 之外的不变量
            在对应阶段以数据库/集成测试补齐。
    """

    invariant_id: str
    statement: str
    enforcement: str
    enforceable_in_stage: int


#: 任务书 §14 的全部 20 条认知不变量。
#:
#: 🔴 **本元组必须恰好包含 20 项**，由 ``tests/unit/test_constitution.py`` 断言
#: （另有 ``tests/unit/test_invariant_counterexamples.py``，为每条不变量
#: 配一个**只破坏它**的定向反例）。
#: 少一条意味着某个边界失去了记录；多一条意味着任务书被超范围解读。
INVARIANTS: Final[tuple[Invariant, ...]] = (
    Invariant(
        "I01",
        "Hypothesis 不能直接变成已确认事实",
        "HypothesisStatus 无通往'已确认'的状态；Hypothesis.can_be_written_as_fact() 恒为 False",
        1,
    ),
    Invariant(
        "I02",
        "Belief 必须具有依据或明确标记为暂定",
        "Belief 模型校验器：status != TENTATIVE 时 confidence_basis 必填非空",
        1,
    ),
    Invariant(
        "I03",
        "存在高可信冲突时，不得输出无保留的确定结论",
        "Judgment 模型校验器：EpistemicAction.ANSWER 要求 unresolved_unknowns 为空",
        1,
    ),
    Invariant(
        "I04",
        "用户赞同不能将事实状态改为已验证",
        "反馈路径无任何入口可写 VerificationStatus；仅证据变更可驱动",
        2,
    ),
    Invariant(
        "I05",
        "用户纠正必须生成新版本",
        "Memory.supersedes_id 版本链；无就地覆盖分支",
        1,
    ),
    Invariant(
        "I06",
        "被取代的记忆不能作为默认有效记忆返回",
        "Memory.is_default_retrievable 仅对 ACTIVE/DISPUTED 为真",
        1,
    ),
    Invariant(
        "I07",
        "最终回答不能比内部判断更确定",
        "EpistemicAction.allows_strong_conclusion；渲染后一致性校验",
        3,
    ),
    Invariant(
        "I08",
        "没有新证据或新路径时，元认知不得允许无限继续",
        "Reflection.should_force_stop()；强制 STOP 并记 NO_MARGINAL_COGNITIVE_GAIN",
        3,
    ),
    Invariant(
        "I09",
        "D4 哲理分析不能覆盖事实层未知",
        "PhilosophicalAnalyzer 输出不得修改 EpistemicClassification 的 UNKNOWN",
        3,
    ),
    Invariant(
        "I10",
        "用户个体经验不能自动升级为全局策略",
        "PROPOSAL_ESCALATION_THRESHOLD = 3；改进提案门槛",
        1,
    ),
    Invariant(
        "I11",
        "ImprovementProposal 不能自动生效",
        "ProposalStatus 无 ACTIVE 成员；ImprovementProposal.can_become_active 恒为 False",
        1,
    ),
    Invariant(
        "I12",
        "认知宪法不能被学习模块修改",
        (
            "本模块常量为 Final/不可变；learning/ 只从本模块**读取**"
            "（由 tests/unit/test_learning_constitution_boundary.py 的导入扫描与"
            "指纹前后比对共同固定）"
        ),
        1,
    ),
    Invariant(
        "I13",
        "用户模型中的推测不得标记为确认事实",
        "UserModelStatus 无 CONFIRMED 成员；UserModel.is_confirmable 恒为 False",
        1,
    ),
    Invariant(
        "I14",
        "记忆检索必须遵守 user_id 作用域",
        "Memory.belongs_to()；检索接口 user_id 无默认值",
        5,
    ),
    Invariant(
        "I15",
        "删除的记忆不得继续出现在向量检索结果中",
        "删除必须同时作用于主表与向量索引",
        5,
    ),
    Invariant(
        "I16",
        "模型格式错误不得导致部分非法状态写入",
        "Schema 校验先于写入；失败 → 重试 → 降级，无部分写入路径",
        3,
    ),
    Invariant(
        "I17",
        "状态机不得跳过禁止跳过的状态",
        "cognition.state_machine.TRANSITIONS 权威转移表 + assert_transition()",
        1,
    ),
    Invariant(
        "I18",
        "所有模型调用必须记录模型和 Prompt 版本",
        "ModelInvocationInfo.model / prompt_version 均为必填",
        1,
    ),
    Invariant(
        "I19",
        "所有完成回合必须有停止原因",
        "CognitiveRound 模型校验器：COMPLETED 时 stop_reason 必填",
        1,
    ),
    Invariant(
        "I20",
        "所有失败回合必须能查询失败阶段和错误类别",
        "CognitiveRound 模型校验器：FAILED 时 failure_stage 与 error_category 必填",
        1,
    ),
)

_INVARIANTS_BY_ID: Final[dict[str, Invariant]] = {inv.invariant_id: inv for inv in INVARIANTS}


def invariant(invariant_id: str) -> Invariant:
    """按 id 取出一条不变量。

    Args:
        invariant_id: 形如 ``"I11"``。

    Returns:
        对应的 :class:`Invariant`。

    Raises:
        KeyError: 该 id 不存在。
    """
    return _INVARIANTS_BY_ID[invariant_id]


# ---------------------------------------------------------------------------
# 记忆写入白名单（ADR-0004，任务书 §10.2）
# ---------------------------------------------------------------------------

#: **可按规则自动批准**写入的记忆类型。
#:
#: 🔴 这是白名单而非黑名单：未列出的类型**默认拒绝**。
#: 记忆污染的代价远大于漏记的代价，因此宁可保守。
AUTO_WRITABLE_MEMORY_TYPES: Final[frozenset[MemoryType]] = frozenset(
    {
        # 用户在当前系统中明确确认、且非敏感的称呼偏好
        MemoryType.USER_PREFERENCE,
        # 已完成认知回合的事件摘要
        MemoryType.EPISODIC,
        # 系统自身的错误记录
        MemoryType.FAILURE_CASE,
        # 已标记为暂定的候选经验（SEMANTIC 需经核验，不在白名单）
        MemoryType.CONCEPTUAL,
    }
)

#: **必须拒绝或等待用户确认**的记忆内容类别（任务书 §10.3）。
#:
#: 用于内容层面的二次筛查——即使记忆类型在白名单内，
#: 命中以下类别的内容仍不得自动写入。
FORBIDDEN_MEMORY_CONTENT_CLASSES: Final[tuple[str, ...]] = (
    "心理诊断",
    "人格诊断",
    "政治倾向推测",
    "宗教信仰推测",
    "健康状况推测",
    "用户稳定人格结论",
    "未经确认的长期目标",
    "从一次互动推断的价值观",
    "第三方隐私",
    "模型自由联想",
    "未经核验的事实断言",
    "完整隐藏思维链",
    "系统生成的通用策略",
)

#: 自动写入所允许的最高敏感度。SENSITIVE 及以上必须经用户确认。
AUTO_WRITE_MAX_SENSITIVITY: Final[SensitivityLevel] = SensitivityLevel.PERSONAL


# ---------------------------------------------------------------------------
# 不变量断言
# ---------------------------------------------------------------------------


def assert_hypothesis_not_fact(hypothesis: Hypothesis) -> None:
    """🔴 **I01**：假设不得被当作已确认事实。

    Args:
        hypothesis: 待检查的假设。

    Raises:
        ConstitutionViolationError: 该假设试图以事实身份被使用。
    """
    if hypothesis.can_be_written_as_fact() or hypothesis.is_factual_claim:
        raise ConstitutionViolationError(
            "假设不能被当作既定事实使用：状态枚举中没有通往'已确认'的路径",
            invariant_id="I01",
            context={"hypothesis_id": str(hypothesis.id)},
        )


def assert_user_model_not_confirmed(user_model: UserModel) -> None:
    """🔴 **I13**：用户模型中的推测不得标记为确认事实。

    Args:
        user_model: 待检查的用户模型条目。

    Raises:
        ConstitutionViolationError: 该条目试图以确认事实身份被使用。
    """
    if user_model.is_confirmable:
        raise ConstitutionViolationError(
            "用户模型条目不能被标记为确认事实",
            invariant_id="I13",
            context={"user_model_id": str(user_model.id)},
        )


#: 表示"提案已生效"的状态值。
#:
#: 🔴 **这份名单只有一个来源。** 它同时被
#: :func:`assert_no_automatic_promotion` 与
#: :mod:`ai_psi.reliability.invariants` 的运行期自检使用——
#: 两处各写一份，会让"宪法拦不住的那个值恰好不在自检名单里"成为可能，
#: 而那正是自检存在的全部意义。
AUTO_PROMOTION_FORBIDDEN_VALUES: Final[frozenset[str]] = frozenset(
    {"active", "applied", "promoted", "live"}
)


def assert_no_automatic_promotion(status: ProposalStatus) -> None:
    """🔴 **I11**：改进提案不得自动生效。

    V0.1 中不存在 ACTIVE 状态，因此任何"已生效"的提案状态都是非法值。

    Args:
        status: 提案状态。

    Raises:
        ConstitutionViolationError: 状态表示提案已生效。
    """
    if status.value in AUTO_PROMOTION_FORBIDDEN_VALUES:
        raise ConstitutionViolationError(
            f"改进提案不得自动生效：{status.value!r} 不是合法的提案状态",
            invariant_id="I11",
        )


def assert_memory_retrievable_by(memory: Memory, *, requesting_user_id: UUID | None) -> None:
    """🔴 **I06 + I14**：检索必须同时满足作用域与有效状态。

    Args:
        memory: 候选记忆。
        requesting_user_id: 发起检索的用户 id。

    Raises:
        ConstitutionViolationError: 跨用户访问，或返回了非默认可见的记忆。
    """
    if not memory.belongs_to(requesting_user_id):
        raise ConstitutionViolationError(
            "记忆检索必须遵守 user_id 作用域：不能返回其他用户的记忆",
            invariant_id="I14",
            context={"memory_id": str(memory.id)},
        )
    if not memory.is_default_retrievable:
        raise ConstitutionViolationError(
            f"状态为 {memory.status.value} 的记忆不能作为默认有效结果返回",
            invariant_id="I06",
            context={"memory_id": str(memory.id)},
        )


def assert_response_not_stronger_than_judgment(
    *,
    judgment: Judgment,
    response_allows_strong_conclusion: bool,
) -> None:
    """🔴 **I07**：最终回答的结论强度不得高于内部判断。

    Args:
        judgment: 内部判断。
        response_allows_strong_conclusion: 渲染后的回答是否使用了无保留表述。

    Raises:
        ConstitutionViolationError: 回答比内部判断更确定。
    """
    if response_allows_strong_conclusion and not judgment.allows_strong_conclusion:
        raise ConstitutionViolationError(
            (
                "最终回答不得比内部判断更确定："
                f"Judgment 的 EpistemicAction 为 {judgment.recommended_epistemic_action.value}，"
                "不允许无保留的确定表述"
            ),
            invariant_id="I07",
            context={"judgment_id": str(judgment.id)},
        )


def constitution_fingerprint() -> str:
    """返回宪法的内容指纹（SHA-256，取前 16 位）。

    用于审计与漂移检测：把指纹写入阶段报告或日志后，
    任何对宪法内容的修改都会改变它。

    注意：**指纹不是防线**——防线是"宪法即代码、修改必留 git diff"（ADR-0011）。
    指纹只是让变更更容易被注意到。
    """
    payload = "\n".join(
        f"{inv.invariant_id}|{inv.statement}|{inv.enforcement}|{inv.enforceable_in_stage}"
        for inv in INVARIANTS
    )
    payload += "\n"
    payload += "|".join(sorted(t.value for t in AUTO_WRITABLE_MEMORY_TYPES))
    payload += "\n"
    payload += "|".join(FORBIDDEN_MEMORY_CONTENT_CLASSES)
    payload += "\n"
    payload += f"version={CONSTITUTION_VERSION}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
