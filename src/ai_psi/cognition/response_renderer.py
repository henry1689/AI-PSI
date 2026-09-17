"""回答渲染与一致性校验（任务书 §9.12）。

Renderer 只负责把 :class:`~ai_psi.prompts.schemas.ResponsePlan` 变成自然语言。
**它不得改变 Judgment 的事实内容与置信等级。**

这句话不能只写在文档里。本模块用两道检查把它变成可执行的：

1. **结构性**：语气、长度、可否使用确定表述，全部由 Planner 从 Judgment 推出，
   Renderer 拿到的只是"照做"（提示词里逐条写明）；
2. **词面**：渲染完成后再扫一遍文本，发现无保留的确定表述而 Judgment
   不允许时，直接判为违反不变量 7。

第 2 道防线是**必要的兜底**：提示词是请求，模型可以不遵守。
它也是**故意从简**的——见 :data:`STRONG_ASSERTION_MARKERS` 的说明。
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from ai_psi.cognition.base import ModuleOutcome, invocation_context
from ai_psi.cognition.constitution import assert_response_not_stronger_than_judgment
from ai_psi.domain.judgments import Judgment
from ai_psi.prompts.schemas import ResponsePlan, ResponseRendererInput
from ai_psi.providers.gateway import ModelGateway

__all__ = [
    "STRONG_ASSERTION_MARKERS",
    "ResponseRenderer",
    "assert_render_consistency",
    "detect_strong_assertion_markers",
]

#: 无保留的确定表述。
#:
#: 🔴 **刻意只用多字短语，不用"一定""必然"这类单字词根。**
#: ``一定程度上``、``不一定``、``未必``都是**弱化**表述，
#: 用词根匹配会把它们全部误判成"过度确定"，
#: 从而让一个措辞谨慎的回答因为防护本身而作废。
#: 宁可漏掉一些真阳性，也不要制造假阳性——
#: 假阳性的代价是整个回合失败。
STRONG_ASSERTION_MARKERS: Final[tuple[str, ...]] = (
    "一定是",
    "必然是",
    "必定是",
    "肯定是",
    "绝对是",
    "毫无疑问",
    "无可置疑",
    "不容置疑",
    "绝对正确",
    "百分之百",
    "definitely",
    "certainly",
    "undoubtedly",
    "unquestionably",
    "without a doubt",
    "there is no doubt",
)

#: 出现这些前缀时，上面的标记不算作无保留表述。
_NEGATION_PREFIXES: Final[tuple[str, ...]] = ("不", "没", "未", "非", "无", "并非", "not ")

#: 回溯检查的字符数（``not `` 含空格，故为 4）。
_NEGATION_LOOKBACK: Final[int] = 4


def detect_strong_assertion_markers(text: str) -> tuple[str, ...]:
    """检出文本中的无保留确定表述（带否定前缀排除）。

    Args:
        text: 渲染后的回答文本。

    Returns:
        命中的标记列表；没有命中返回空元组。
    """
    lowered = text.lower()
    hits: list[str] = []
    for marker in STRONG_ASSERTION_MARKERS:
        needle = marker.lower()
        start = 0
        while True:
            index = lowered.find(needle, start)
            if index < 0:
                break
            prefix = lowered[max(0, index - _NEGATION_LOOKBACK) : index]
            if not any(prefix.endswith(negation.lower()) for negation in _NEGATION_PREFIXES):
                hits.append(marker)
                break
            start = index + len(needle)
    return tuple(hits)


def assert_render_consistency(*, judgment: Judgment, text: str) -> None:
    """渲染后一致性校验（不变量 7）。

    Args:
        judgment: 内部判断。
        text: 渲染后的回答文本。

    Raises:
        ConstitutionViolationError: 回答比内部判断更确定。
    """
    markers = detect_strong_assertion_markers(text)
    assert_response_not_stronger_than_judgment(
        judgment=judgment,
        response_allows_strong_conclusion=bool(markers),
    )


class ResponseRenderer:
    """把回答方案渲染为自然语言。"""

    def __init__(self, gateway: ModelGateway) -> None:
        """初始化。

        Args:
            gateway: 模型调用网关。
        """
        self._gateway = gateway

    async def render(
        self,
        *,
        plan: ResponsePlan,
        judgment: Judgment,
        user_id: UUID | None = None,
        conversation_id: UUID | None = None,
        cognitive_round_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> ModuleOutcome[str]:
        """渲染回答。

        Args:
            plan: 回答方案。
            judgment: 内部判断。
            user_id: 归属用户。
            conversation_id: 所属会话。
            cognitive_round_id: 当前回合 id。
            correlation_id: 关联链标识。

        Returns:
            回答文本与调用记录。

        Raises:
            ConstitutionViolationError: 渲染结果违反不变量 7。
        """
        payload = ResponseRendererInput(
            plan=plan,
            conclusion=judgment.conclusion,
            rationale_summary=list(judgment.rationale_summary),
            applicability=list(judgment.applicability),
            confidence_band=judgment.confidence_band,
            epistemic_action=judgment.recommended_epistemic_action,
            response_style=plan.response_style,
        )
        call = await self._gateway.text(
            task_name="response_renderer",
            payload=payload,
            context=invocation_context(
                cognitive_round_id=cognitive_round_id,
                conversation_id=conversation_id,
                user_id=user_id,
                correlation_id=correlation_id,
            ),
        )

        assert_render_consistency(judgment=judgment, text=call.text)
        return ModuleOutcome(value=call.text, invocations=(call.invocation,))
