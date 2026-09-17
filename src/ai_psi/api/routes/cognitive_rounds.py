"""认知回合路由（任务书 §12.1）。

🔴 **摘要接口不返回完整隐藏思维链**，只返回允许审计的结构化理由。
这一条由 :func:`~ai_psi.api.mappers.judgment_view` 的逐字段映射保证——
不存在"把领域对象整个序列化出去"的路径。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter

from ai_psi.api.dependencies import ContainerDep
from ai_psi.api.mappers import judgment_view, model_invocation_view, reflection_view
from ai_psi.api.schemas import (
    RoundResponseBody,
    RoundStatusResponse,
    RoundSummaryResponse,
)
from ai_psi.cognition.projection import project_artifacts
from ai_psi.container import Container
from ai_psi.domain.enums import EventType
from ai_psi.domain.exceptions import NotFoundError

__all__ = ["router"]

router = APIRouter(prefix="/cognitive-rounds", tags=["cognitive-rounds"])


@router.get("/{round_id}", response_model=RoundStatusResponse)
async def get_round_status(
    round_id: UUID,
    container: ContainerDep,
) -> RoundStatusResponse:
    """获取回合状态。

    Args:
        round_id: 回合 id。
        container: 依赖容器。

    Returns:
        回合的运行状态与预算消耗。
    """
    round_ = await _load_round(container, round_id)
    return RoundStatusResponse(
        cognitive_round_id=round_.id,
        state=round_.state,
        depth_level=round_.depth_level,
        stop_reason=round_.stop_reason,
        failure_stage=round_.failure_stage,
        error_category=(round_.error_category.value if round_.error_category is not None else None),
        model_calls_used=round_.model_calls_used,
        max_model_calls=round_.budget.max_model_calls,
        metacognitive_loops=round_.metacognitive_loops,
        max_metacognitive_loops=round_.budget.max_metacognitive_loops,
        version=round_.version,
    )


@router.get("/{round_id}/response", response_model=RoundResponseBody)
async def get_round_response(
    round_id: UUID,
    container: ContainerDep,
) -> RoundResponseBody:
    """获取最终回答。

    Args:
        round_id: 回合 id。
        container: 依赖容器。

    Returns:
        回答文本与内部判断的对外视图。
    """
    round_ = await _load_round(container, round_id)
    view = await _artifacts(container, round_id)
    return RoundResponseBody(
        cognitive_round_id=round_id,
        state=round_.state,
        text=view.response_text,
        judgment=judgment_view(view.latest_judgment),
    )


@router.get("/{round_id}/summary", response_model=RoundSummaryResponse)
async def get_round_summary(
    round_id: UUID,
    container: ContainerDep,
) -> RoundSummaryResponse:
    """获取结构化认知摘要。

    Args:
        round_id: 回合 id。
        container: 依赖容器。

    Returns:
        结构化理由、元认知信号与模型调用元信息。
    """
    round_ = await _load_round(container, round_id)
    view = await _artifacts(container, round_id)

    judgments = view.of_type(EventType.JUDGMENT_CREATED)
    adjustments: list[str] = []
    for record in judgments:
        adjustments.extend(str(item) for item in record.payload.get("adjustments", []))

    # 分析结果来自各自的 ``cognition.analysis.completed`` 事件——
    # 这样每次分析调用的模型与 Prompt 版本才有地方可查（不变量 18）
    analyses: dict[str, Any] = {
        str(record.payload["analysis_kind"]): record.payload["analysis"]
        for record in view.of_type(EventType.COGNITION_ANALYSIS_COMPLETED)
        if "analysis_kind" in record.payload
    }

    inquiries = view.of_type(EventType.INQUIRY_CREATED)
    inquiry_payload = inquiries[-1].payload.get("inquiry") if inquiries else None

    return RoundSummaryResponse(
        cognitive_round_id=round_id,
        state=round_.state,
        depth_level=round_.depth_level,
        stop_reason=round_.stop_reason,
        concerns=[
            record.payload["concern"]
            for record in view.of_type(EventType.CONCERN_CREATED)
            if isinstance(record.payload.get("concern"), dict)
        ],
        inquiry=inquiry_payload if isinstance(inquiry_payload, dict) else None,
        hypotheses=[
            record.payload["hypothesis"]
            for record in view.of_type(EventType.HYPOTHESIS_CREATED)
            if isinstance(record.payload.get("hypothesis"), dict)
        ],
        judgment=judgment_view(view.latest_judgment),
        reflection=reflection_view(view.latest_reflection),
        analyses=analyses,
        response_text=view.response_text,
        adjustments=adjustments,
        model_invocations=[model_invocation_view(item) for item in view.model_invocations],
    )


async def _load_round(container: Container, round_id: UUID):  # type: ignore[no-untyped-def]
    """读取回合；不存在时抛 404。"""
    async with container.uow_factory() as uow:
        round_ = await uow.rounds.get(round_id)
    if round_ is None:
        msg = f"认知回合不存在：{round_id}"
        raise NotFoundError(msg, context={"cognitive_round_id": str(round_id)})
    return round_


async def _artifacts(container: Container, round_id: UUID):  # type: ignore[no-untyped-def]
    """读取并投影某个回合的认知产物。"""
    async with container.uow_factory() as uow:
        events = await uow.events.read_stream(cognitive_round_id=round_id)
    return project_artifacts(events)
