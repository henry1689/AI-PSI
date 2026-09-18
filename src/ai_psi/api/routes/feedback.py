"""反馈路由（任务书 §12.2）。

```
POST /api/v1/cognitive-rounds/{round_id}/feedback
```

🔴 **``allow_memory_update`` 不是"允许写记忆"。**

它是"允许把这次反馈**提给**记忆写入流程"。写入仍然要过
``MemoryWriteProposal → WritePolicy``（ADR-0004）——
反馈是用户操作，而用户操作同样不能绕过记忆红线。
把这条路径短路掉会让反馈变成一条看起来无害的旁路。

🔴 **不是所有反馈类型都能带出记忆。** 只有 ``CORRECTION`` 与
``CLARIFICATION`` 携带新信息（"我说的其实是 X"）；
赞同、评分说的是"你做得对不对"，异议只说"你错了"——
拿它们写记忆等于把噪声记成事实。不满足时响应里会**明确说明原因**，
而不是静默成功。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from ai_psi.api.dependencies import ContainerDep
from ai_psi.api.schemas import FeedbackRequest, FeedbackResponse

__all__ = ["router"]

router = APIRouter(tags=["feedback"])


@router.post(
    "/cognitive-rounds/{round_id}/feedback",
    response_model=FeedbackResponse,
    status_code=201,
)
async def submit_feedback(
    round_id: UUID,
    body: FeedbackRequest,
    container: ContainerDep,
) -> FeedbackResponse:
    """记录一次反馈，并在允许时尝试更新长期记忆。

    🔴 响应里的 ``memory_effect`` **必须读**：
    ``written`` 表示确实产生了一条记忆，``not_eligible`` /
    ``rejected_by_policy`` 表示没有——而 ``reasons`` 会说明为什么。
    把"什么都没做"和"记下来了"在响应里做成同一个样子，
    等于让调用方去猜。

    ⚠️ **任务书 §12.2 的示例把 ``feedback_type`` 写成 ``"CORRECTION"``（大写）。**
    本接口收的是**枚举值** ``"correction"``——与任务书 §12.1 的 ``status``
    同一种偏离（那里返回的是真实终态而不是固定的 ``"CREATED"``）。
    本服务所有枚举在 HTTP 面上统一用小写值，破例一处会让客户端
    需要记住"哪些字段是大写"。

    Raises:
        NotFoundError: 回合不存在 → 404。
    """
    outcome = await container.feedback_service.record(
        round_id=round_id,
        feedback_type=body.feedback_type,
        content=body.content,
        related_claim=body.related_claim,
        allow_memory_update=body.allow_memory_update,
    )
    return FeedbackResponse(
        round_id=outcome.round_id,
        feedback_type=outcome.feedback_type,
        audit_event_id=outcome.event.id,
        memory_effect=outcome.memory_effect,
        memory_id=outcome.memory.id if outcome.memory is not None else None,
        memory_written=outcome.memory_written,
        reasons=list(outcome.reasons),
    )
