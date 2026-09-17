"""关切检测（任务书 §9.1）。

关切是"值不值得启动一次认知"的判定结果。本模块的职责不只是**产生**关切，
更是**过滤**它们——任务书明确列出四类必须被挡住的关切：

* 无依据的主动问题；
* 纯粹为了表现聪明的探索；
* 不相关旧记忆引发的联想；
* 未授权的隐私推测。

前三条靠模型判断（写进了提示词），第四条靠**代码**兜住：
:class:`~ai_psi.domain.concerns.Concern` 的 ``source_event_ids`` 必填非空，
因此"无依据的关切"在类型层面就构造不出来。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final
from uuid import UUID

from ai_psi.cognition.base import ModuleOutcome, invocation_context
from ai_psi.domain.concerns import Concern
from ai_psi.domain.events import Event
from ai_psi.prompts.schemas import ConcernDetectorInput, ConcernDetectorOutput
from ai_psi.providers.gateway import ModelGateway

__all__ = ["MAX_CONCERNS_PER_ROUND", "ConcernDetector"]

#: 一次回合最多采纳的关切数。
#:
#: 关切本身不消耗模型调用，但每一个都会向下游膨胀成完整分析。
#: 限制在 3 个以内，与"不要为了显得聪明而扩大范围"一致。
MAX_CONCERNS_PER_ROUND: Final[int] = 3


class ConcernDetector:
    """从当前事件与会话状态中识别值得投入认知资源的关切。"""

    def __init__(self, gateway: ModelGateway) -> None:
        """初始化。

        Args:
            gateway: 模型调用网关。
        """
        self._gateway = gateway

    async def detect(
        self,
        *,
        trigger_event: Event,
        user_message: str,
        conversation_summary: Sequence[str] = (),
        open_questions: Sequence[str] = (),
        confirmed_user_goals: Sequence[str] = (),
        system_status: Sequence[str] = (),
        user_id: UUID | None = None,
        conversation_id: UUID | None = None,
        cognitive_round_id: UUID | None = None,
        correlation_id: UUID | None = None,
    ) -> ModuleOutcome[list[Concern]]:
        """检测本回合值得处理的关切。

        Args:
            trigger_event: 触发本回合的事件。**所有关切都以它为来源**——
                这保证每个关切都能回答"它为什么被创建"。
            user_message: 用户消息原文。
            conversation_summary: 最近会话摘要。
            open_questions: 未完成的问题。
            confirmed_user_goals: 已确认的用户目标。
            system_status: 系统运行状态。
            user_id: 归属用户。
            conversation_id: 所属会话。
            cognitive_round_id: 当前回合 id。
            correlation_id: 关联链标识。

        Returns:
            关切列表（可能为空）与调用记录。
            空列表是**合法结论**——"这件事不值得启动认知"。
        """
        payload = ConcernDetectorInput(
            user_message=user_message,
            conversation_summary=list(conversation_summary),
            open_questions=list(open_questions),
            confirmed_user_goals=list(confirmed_user_goals),
            system_status=list(system_status),
        )
        call = await self._gateway.structured(
            task_name="concern_detector",
            payload=payload,
            response_model=ConcernDetectorOutput,
            context=invocation_context(
                cognitive_round_id=cognitive_round_id,
                conversation_id=conversation_id,
                user_id=user_id,
                correlation_id=correlation_id,
            ),
        )

        concerns = [
            Concern(
                created_by="concern_detector",
                source_event_ids=[trigger_event.id],
                category=draft.category,
                statement=draft.statement,
                why_it_matters=draft.why_it_matters,
                impact=draft.impact,
                urgency=draft.urgency,
                uncertainty=draft.uncertainty,
                expected_information_value=draft.expected_information_value,
                cognitive_cost=draft.cognitive_cost,
            )
            for draft in call.value.concerns
            if draft.should_start_round
        ][:MAX_CONCERNS_PER_ROUND]

        return ModuleOutcome(value=concerns, invocations=(call.invocation,))
