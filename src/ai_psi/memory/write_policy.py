"""记忆写入策略（任务书 §10.1–§10.3，ADR-0004）。

🔴 **默认拒绝。**

:data:`~ai_psi.cognition.constitution.AUTO_WRITABLE_MEMORY_TYPES` 是**白名单**：
未列出的类型一律不自动写入。理由是记忆污染的代价远大于漏记的代价——
漏掉一条该记的东西，用户顶多重复说一遍；
写错一条不该记的东西，它会在此后每一轮里持续影响判断，而且用户很难发现。

策略分四档，粒度比"批准/拒绝"更细：

======================================  ==========================================
决定                                    含义
======================================  ==========================================
``APPROVED``                            可按规则自动写入
``REQUIRES_USER_CONFIRMATION``          等用户在当前系统中明确确认
``REQUIRES_REVIEW``                     需要人工评审（不是"用户点一下"能解决的）
``REJECTED``                            不得写入
======================================  ==========================================

**这些是策略，不是防线。** 真正的防线是：
模型不能直接创建可用的 Memory，所有写入必须经过本策略与应用服务（架构规则 3）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from ai_psi.cognition.constitution import (
    AUTO_WRITABLE_MEMORY_TYPES,
    AUTO_WRITE_MAX_SENSITIVITY,
    FORBIDDEN_MEMORY_CONTENT_CLASSES,
)
from ai_psi.domain.enums import MemoryType, SensitivityLevel

__all__ = [
    "FORBIDDEN_CONTENT_KEYWORDS",
    "STRUCTURALLY_ENFORCED_CLASSES",
    "MemoryWriteProposal",
    "WriteDecision",
    "WritePolicy",
    "WritePolicyDecision",
]


class WriteDecision(StrEnum):
    """记忆写入的裁决。"""

    APPROVED = "approved"
    REQUIRES_USER_CONFIRMATION = "requires_user_confirmation"
    REQUIRES_REVIEW = "requires_review"
    REJECTED = "rejected"

    @property
    def allows_write(self) -> bool:
        """是否允许写入（无论是否需要额外确认）。

        V0.1 中只有 ``APPROVED`` 会真正落库——其余三档都只产生
        ``memory.proposed`` 事件而不写入记忆本体。
        """
        return self is WriteDecision.APPROVED


#: 内容层面的粗粒度筛查关键词。
#:
#: 🔴 **这是第二道滤网，不是第一道。** 第一道是按类型的白名单——
#: 关键词匹配必然漏（换个说法就绕过了），所以它只用来拦住最明显的越界，
#: 拦不住的一律由白名单兜住。
#:
#: 键是 :data:`~ai_psi.cognition.constitution.FORBIDDEN_MEMORY_CONTENT_CLASSES`
#: 中的类别名，值是该类别最容易出现的说法。
FORBIDDEN_CONTENT_KEYWORDS: Final[dict[str, tuple[str, ...]]] = {
    "心理诊断": ("抑郁症", "焦虑症", "心理疾病", "精神问题", "有心理障碍"),
    "人格诊断": ("人格障碍", "人格类型", "性格缺陷", "他是那种人"),
    "政治倾向推测": ("政治倾向", "政治立场", "党派", "他支持哪个党"),
    "宗教信仰推测": ("宗教信仰", "信教", "皈依"),
    "健康状况推测": ("健康状况不佳", "患有", "确诊"),
    "用户稳定人格结论": ("用户是一个", "用户本质上", "用户天生"),
    "从一次互动推断的价值观": ("用户认为一切", "用户的价值观是"),
    "第三方隐私": ("他的家人", "他的住址", "他的电话", "他的收入"),
    "完整隐藏思维链": ("思维链", "推理过程如下", "chain of thought"),
    "系统生成的通用策略": ("通用策略", "所有用户都", "一律应当"),
}

#: 靠**结构**而非关键词强制的禁止类别。
#:
#: 🔴 不是每条红线都能靠词面匹配拦住。这两条的本质是"有没有经过某道程序"，
#: 而不是"文字里出现了什么词"——试图用关键词去猜它们，只会得到一份
#: 看起来很长、实际拦不住东西的清单。把它们显式列出来，
#: 比假装关键词覆盖了全部类别要诚实。
#:
#: :data:`FORBIDDEN_CONTENT_KEYWORDS` 的键与该字典的键**合起来**
#: 必须恰好覆盖 :data:`~ai_psi.cognition.constitution.FORBIDDEN_MEMORY_CONTENT_CLASSES`，
#: 由 ``tests/unit/test_write_policy.py`` 断言。
STRUCTURALLY_ENFORCED_CLASSES: Final[dict[str, str]] = {
    "未经确认的长期目标": "由 USER_GOAL 类型的确认要求强制（_CONFIRMATION_REQUIRED_TYPES）",
    "未经核验的事实断言": "由 SEMANTIC 类型的评审要求强制（_REVIEW_REQUIRED_TYPES）",
    "模型自由联想": (
        "由类型白名单强制：自由联想既没有可追溯的 source_event_ids，"
        "也不落入任何可自动写入的记忆类型（AUTO_WRITABLE_MEMORY_TYPES）"
    ),
}

#: 必须人工评审（不是"用户点一下"就能解决的）的记忆类型。
_REVIEW_REQUIRED_TYPES: Final[frozenset[MemoryType]] = frozenset(
    {
        # 需要核验的事实断言——未经核验的事实写入记忆是污染的主要来源
        MemoryType.SEMANTIC,
        # 🔴 系统生成的通用策略（不变量 10：单次经验不得升级为全局策略）
        MemoryType.STRATEGY,
        # 自我模型。V0.1 不允许系统对自己形成稳定结论
        MemoryType.SELF_MODEL,
    }
)

#: 需要用户在当前系统中明确确认才能写入的记忆类型。
_CONFIRMATION_REQUIRED_TYPES: Final[frozenset[MemoryType]] = frozenset(
    {
        # §10.3：未经确认的长期目标
        MemoryType.USER_GOAL,
        MemoryType.USER_CONFIRMED_FACT,
    }
)

#: 即使类型在白名单内、也不需要用户确认，仍然只能作为事件记录的类型。
_PASSIVELY_RECORDABLE_TYPES: Final[frozenset[MemoryType]] = frozenset(
    {
        MemoryType.EPISODIC,
        MemoryType.FAILURE_CASE,
    }
)


@dataclass(frozen=True, slots=True)
class MemoryWriteProposal:
    """一条待裁决的记忆写入请求。

    🔴 **它不是** :class:`~ai_psi.domain.memories.Memory`。
    提案是"想要写什么"，记忆是"已经写入什么"。
    把两者合成一个对象，就等于让"提案"自带写入权限。
    """

    user_id: UUID | None
    memory_type: MemoryType
    content: str
    sensitivity: SensitivityLevel
    source_event_ids: tuple[UUID, ...] = ()
    user_confirmed: bool = False


@dataclass(frozen=True, slots=True)
class WritePolicyDecision:
    """写作策略的裁决结果。

    Attributes:
        decision: 四档裁决。
        reasons: 裁决理由，逐条可读——策略必须能解释自己为什么这样判。
        forbidden_class: 命中禁止类别时，该类别的名字。
    """

    decision: WriteDecision
    reasons: tuple[str, ...] = ()
    forbidden_class: str | None = None

    @property
    def allows_write(self) -> bool:
        """是否允许自动写入。"""
        return self.decision.allows_write


class WritePolicy:
    """默认拒绝的记忆写入策略。"""

    def decide(self, proposal: MemoryWriteProposal) -> WritePolicyDecision:
        """裁决一条写入请求。

        判定顺序：**内容红线 → 敏感度 → 类型白名单 → 确认状态**。
        顺序不能反：一条命中内容红线的请求，
        无论类型多么"安全"，都不该因为走到白名单分支而被放行。

        Args:
            proposal: 写入提案。

        Returns:
            裁决结果。
        """
        if not proposal.content.strip():
            # 🔴 策略放行的东西，下游必须**能构造出一个 Memory**。
            #
            # 空白内容连 `Memory` 都构造不出来（`content` 有 min_length=1），
            # 让策略对它说"批准"等于把失败推迟到一个更晚、更难看的位置：
            # 应用服务会撞上 pydantic 的 ValidationError，而那不是一个
            # 领域异常，最终表现为 **500**——一次用户输入被报成服务端故障。
            return WritePolicyDecision(
                decision=WriteDecision.REJECTED,
                reasons=(
                    "内容为空（或只有空白字符）",
                    "空白内容连一条记忆都构造不出来，在策略层拒绝它比让下游崩溃早一步",
                ),
            )

        forbidden = _matched_forbidden_class(proposal.content)
        if forbidden is not None:
            return WritePolicyDecision(
                decision=WriteDecision.REJECTED,
                reasons=(
                    f"内容命中禁止类别「{forbidden}」（任务书 §10.3）",
                    "这类内容必须由用户本人明确陈述并确认，系统不得自行记录",
                ),
                forbidden_class=forbidden,
            )

        if proposal.sensitivity.requires_user_confirmation and not proposal.user_confirmed:
            return WritePolicyDecision(
                decision=WriteDecision.REQUIRES_USER_CONFIRMATION,
                reasons=(
                    f"敏感度为 {proposal.sensitivity.value}，高于自动写入上限 "
                    f"{AUTO_WRITE_MAX_SENSITIVITY.value}",
                    "敏感内容必须先获得用户在当前系统中的明确确认",
                ),
            )

        if proposal.memory_type in _REVIEW_REQUIRED_TYPES:
            return WritePolicyDecision(
                decision=WriteDecision.REQUIRES_REVIEW,
                reasons=(
                    f"记忆类型 {proposal.memory_type.value} 需要人工评审才能写入",
                    "这类内容的错误代价是累积的：一次写入会影响此后所有回合",
                ),
            )

        if proposal.memory_type in _CONFIRMATION_REQUIRED_TYPES and not proposal.user_confirmed:
            return WritePolicyDecision(
                decision=WriteDecision.REQUIRES_USER_CONFIRMATION,
                reasons=(f"记忆类型 {proposal.memory_type.value} 需要用户明确确认",),
            )

        if proposal.memory_type not in AUTO_WRITABLE_MEMORY_TYPES:
            return WritePolicyDecision(
                decision=WriteDecision.REQUIRES_USER_CONFIRMATION,
                reasons=(
                    f"记忆类型 {proposal.memory_type.value} 不在自动写入白名单内",
                    "白名单之外一律默认拒绝（ADR-0004）",
                ),
            )

        if proposal.memory_type in _PASSIVELY_RECORDABLE_TYPES:
            return WritePolicyDecision(
                decision=WriteDecision.APPROVED,
                reasons=(f"{proposal.memory_type.value} 属于可自动记录的回合事实",),
            )

        if proposal.user_confirmed:
            return WritePolicyDecision(
                decision=WriteDecision.APPROVED,
                reasons=(
                    f"{proposal.memory_type.value} 在白名单内，且用户已明确确认",
                    f"敏感度 {proposal.sensitivity.value} 未超过自动写入上限",
                ),
            )

        return WritePolicyDecision(
            decision=WriteDecision.REQUIRES_USER_CONFIRMATION,
            reasons=(
                f"{proposal.memory_type.value} 虽在白名单内，但尚无用户明确确认",
                "偏好类记忆必须由用户说出，系统不得从行为反推",
            ),
        )


def _matched_forbidden_class(content: str) -> str | None:
    """返回内容命中的禁止类别名；未命中返回 ``None``。"""
    for class_name, keywords in FORBIDDEN_CONTENT_KEYWORDS.items():
        if any(keyword in content for keyword in keywords):
            return class_name
    # 类别名本身出现在内容里时同样命中——"这是一条心理诊断"这种元陈述
    # 也是要拦的，因为它同样会把类别概念带进记忆。
    return next(
        (name for name in FORBIDDEN_MEMORY_CONTENT_CLASSES if name in content),
        None,
    )
