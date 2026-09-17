"""回放路由（任务书 §12.5）。

🔴 **回放是只读的，且不会覆盖原始事件。**
:func:`~ai_psi.cognition.projection.project_round` 是纯函数：
它重建状态、**并且审计**每一次转移是否合法。因此回放不只是"重放历史"，
它同时是"这段历史有没有被绕过状态机"的一致性检查。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from ai_psi.api.dependencies import ContainerDep
from ai_psi.api.schemas import ReplayResponse
from ai_psi.cognition.projection import project_round
from ai_psi.domain.common import utc_now
from ai_psi.domain.exceptions import NotFoundError

__all__ = ["router"]

router = APIRouter(prefix="/replay", tags=["replay"])


@router.post("/cognitive-rounds/{round_id}", response_model=ReplayResponse)
async def replay_round(
    round_id: UUID,
    container: ContainerDep,
) -> ReplayResponse:
    """回放一个认知回合的事件流。

    Args:
        round_id: 回合 id。
        container: 依赖容器。

    Returns:
        重建出的状态、转移序列与失败诊断。

    Raises:
        NotFoundError: 事件流为空。
        IllegalStateTransitionError: 事件流中包含非法转移
            （说明有写入绕过了状态机）。
    """
    async with container.uow_factory() as uow:
        events = await uow.events.read_stream(cognitive_round_id=round_id)

    if not events:
        msg = f"回合没有事件流，无法回放：{round_id}"
        raise NotFoundError(msg, context={"cognitive_round_id": str(round_id)})

    projection = project_round(events)
    return ReplayResponse(
        cognitive_round_id=projection.cognitive_round_id,
        state=projection.state,
        transition_count=projection.transition_count,
        event_count=projection.event_count,
        transitions=[
            {
                "from_state": item.from_state.value,
                "to_state": item.to_state.value,
                "reason": item.reason,
            }
            for item in projection.transitions
        ],
        stop_reason=projection.stop_reason,
        failure_stage=projection.failure_stage,
        error_category=(
            projection.error_category.value if projection.error_category is not None else None
        ),
        projected_at=utc_now(),
    )
