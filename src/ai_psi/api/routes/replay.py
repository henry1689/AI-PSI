"""回放路由（任务书 §12.5）。

🔴 **回放是只读的，且不会覆盖原始事件。**
:func:`~ai_psi.cognition.projection.project_round` 是纯函数：
它重建状态、**并且审计**每一次转移是否合法。因此回放不只是"重放历史"，
它同时是"这段历史有没有被绕过状态机"的一致性检查。

⚠️ **阶段 6.5 §四：本路由此前把逻辑写在自己身上。**

它内联调 `project_round`，于是
:class:`~ai_psi.application.replay_service.ReplayService` 成了死代码，
而它算出的 ``differs_from_projection``——**"事件流与状态投影是否一致"
这条数据一致性告警**——**没有任何路径能读到**。
一条实现了但没有出口的告警，与没有这条告警是一样的。

现在逻辑收回应用层，告警也一并暴露出来。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from ai_psi.api.dependencies import ContainerDep
from ai_psi.api.schemas import ReplayResponse
from ai_psi.domain.common import utc_now

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
        重建出的状态、转移序列、失败诊断，
        以及**与当前状态投影是否一致**。

    Raises:
        NotFoundError: 事件流为空。
        IllegalStateTransitionError: 事件流中包含非法转移
            （说明有写入绕过了状态机）。
    """
    result = await container.replay_service.replay_round(round_id)
    projection = result.projection
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
        differs_from_projection=result.differs_from_projection,
        projected_at=utc_now(),
    )
