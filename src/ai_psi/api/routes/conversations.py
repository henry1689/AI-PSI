"""会话与消息路由（任务书 §12.1）。"""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, Header

from ai_psi.api.dependencies import ContainerDep
from ai_psi.api.schemas import (
    ConversationCreatedResponse,
    SubmitMessageRequest,
    SubmitMessageResponse,
)
from ai_psi.application.cognitive_runtime import RoundRequest

__all__ = ["router"]

router = APIRouter(tags=["conversations"])


@router.post("/conversations", response_model=ConversationCreatedResponse, status_code=201)
async def create_conversation() -> ConversationCreatedResponse:
    """创建一个会话分组键。

    ⚠️ V0.1 不持久化会话实体——见
    :class:`~ai_psi.api.schemas.ConversationCreatedResponse` 的说明。

    Returns:
        新会话的 id 与说明。
    """
    return ConversationCreatedResponse(conversation_id=uuid4())


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=SubmitMessageResponse,
    status_code=201,
)
async def submit_message(
    conversation_id: UUID,
    body: SubmitMessageRequest,
    container: ContainerDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> SubmitMessageResponse:
    """提交用户消息并启动一次认知回合。

    🔴 ``Idempotency-Key`` 存在时，重复提交同一请求**不会**创建第二个回合
    （任务书 §13.4）。同一 key 搭配不同请求体会得到 409，而不是静默返回旧结果。

    Args:
        conversation_id: 所属会话。
        body: 请求体。
        container: 依赖容器。
        idempotency_key: 客户端幂等键。

    Returns:
        回合标识、真实终态与最终回答。
    """
    outcome = await container.runtime.run_round(
        RoundRequest(
            user_message=body.content,
            user_id=body.user_id,
            conversation_id=conversation_id,
            requested_depth=body.requested_depth,
            response_style=body.response_style,
            idempotency_key=idempotency_key,
            allow_long_term_memory=body.allow_long_term_memory,
        )
    )

    return SubmitMessageResponse(
        message_id=outcome.trigger_event_id or outcome.cognitive_round_id,
        cognitive_round_id=outcome.cognitive_round_id,
        status=outcome.state,
        depth=outcome.depth,
        stop_reason=outcome.stop_reason,
        response=outcome.response_text,
    )
