"""改进提案路由（任务书 §12.4）。

```
GET  /api/v1/improvement-proposals
GET  /api/v1/improvement-proposals/{proposal_id}
POST /api/v1/improvement-proposals/{proposal_id}/evaluate
POST /api/v1/improvement-proposals/{proposal_id}/approve-for-manual-trial
POST /api/v1/improvement-proposals/{proposal_id}/reject
```

🔴 **没有任何一条路由能让提案生效。**

不是"没做"，而是 ``ProposalStatus`` 里不存在 ``ACTIVE``——
状态空间到此为止（不变量 11）。``approve-for-manual-trial``
批准的是**做一次人工试验**，之后发生什么由人决定，
不由这个系统自动完成。

🔴 **驳回必须在评估之后**，批准也是。``REJECTED`` 与
``APPROVED_FOR_MANUAL_TRIAL`` 都只能从 ``EVALUATED`` 到达
（ADR-0005）。在 ``DRAFT`` 上直接调这两个接口会得到 409，
理由写在响应里。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from ai_psi.api.dependencies import ContainerDep
from ai_psi.api.mappers import proposal_view
from ai_psi.api.schemas import (
    ApproveProposalRequest,
    EvaluateProposalRequest,
    ProposalListResponse,
    ProposalTransitionResponse,
    ProposalView,
    RejectProposalRequest,
)
from ai_psi.application.proposal_service import ProposalEvaluation, ProposalTransition
from ai_psi.domain.enums import ErrorType, ProposalStatus

__all__ = ["router"]

router = APIRouter(tags=["improvement-proposals"])

StatusQuery = Annotated[
    ProposalStatus | None,
    Query(description="按状态过滤；不传则返回全部"),
]

ErrorClassQuery = Annotated[
    ErrorType | None,
    Query(description="按错误类别过滤"),
]

LimitQuery = Annotated[
    int | None,
    Query(ge=0, description="条数上限；不传则返回全部"),
]


@router.get("/improvement-proposals", response_model=ProposalListResponse)
async def list_proposals(
    container: ContainerDep,
    status: StatusQuery = None,
    error_class: ErrorClassQuery = None,
    limit: LimitQuery = None,
) -> ProposalListResponse:
    """列出改进提案（最新的在前）。

    Args:
        container: 依赖容器。
        status: 按状态过滤。
        error_class: 按错误类别过滤。
        limit: 条数上限。

    Returns:
        提案列表。
    """
    proposals = await container.proposal_service.list_all(
        status=status, error_class=error_class, limit=limit
    )
    return ProposalListResponse(
        count=len(proposals),
        proposals=[proposal_view(item) for item in proposals],
    )


@router.get("/improvement-proposals/{proposal_id}", response_model=ProposalView)
async def get_proposal(proposal_id: UUID, container: ContainerDep) -> ProposalView:
    """读取一条提案。

    Raises:
        NotFoundError: 提案不存在 → 404。
    """
    return proposal_view(await container.proposal_service.get(proposal_id))


@router.post(
    "/improvement-proposals/{proposal_id}/evaluate",
    response_model=ProposalTransitionResponse,
)
async def evaluate_proposal(
    proposal_id: UUID,
    body: EvaluateProposalRequest,
    container: ContainerDep,
) -> ProposalTransitionResponse:
    """记录一次离线评估，把提案推进到 ``EVALUATED``。

    🔴 评估结论里的 ``evidence`` 会被完整保存。没有口径的结论
    无法被复核，因此也无法在日后被推翻——而不可推翻的结论
    会永久影响策略。

    Raises:
        NotFoundError: 提案不存在 → 404。
        IllegalStateTransitionError: 提案已处于终态 → 409。
    """
    transition = await container.proposal_service.evaluate(
        proposal_id,
        evaluation=ProposalEvaluation(
            verdict=body.verdict,
            evidence=tuple(body.evidence),
            notes=body.notes,
        ),
        actor_id=body.actor_id,
    )
    return _transition_view(transition)


@router.post(
    "/improvement-proposals/{proposal_id}/approve-for-manual-trial",
    response_model=ProposalTransitionResponse,
)
async def approve_proposal(
    proposal_id: UUID,
    body: ApproveProposalRequest,
    container: ContainerDep,
) -> ProposalTransitionResponse:
    """批准进行**人工试验**。

    🔴 这是状态机的终点，**不是上线**。批准之后发生什么由人决定：
    这个系统没有任何自动把改动推到生产的路径（不变量 11、ADR-0005）。

    🔴 **要求先评估。** 未经评估的批准等于凭印象拍板。

    Raises:
        NotFoundError: 提案不存在 → 404。
        IllegalStateTransitionError: 尚未评估，或已处于终态 → 409。
    """
    transition = await container.proposal_service.approve_for_manual_trial(
        proposal_id,
        approved_by=body.approved_by,
        note=body.note,
    )
    return _transition_view(transition)


@router.post(
    "/improvement-proposals/{proposal_id}/reject",
    response_model=ProposalTransitionResponse,
)
async def reject_proposal(
    proposal_id: UUID,
    body: RejectProposalRequest,
    container: ContainerDep,
) -> ProposalTransitionResponse:
    """驳回一条提案。

    🔴 **理由必填。** 驳回不需要理由的话，下一个人再遇到同类问题时
    只会看到"这条被拒了"，然后重新走一遍同样的路。

    Raises:
        NotFoundError: 提案不存在 → 404。
        IllegalStateTransitionError: 尚未评估，或已处于终态 → 409。
    """
    transition = await container.proposal_service.reject(
        proposal_id,
        rejected_by=body.rejected_by,
        reason=body.reason,
    )
    return _transition_view(transition)


def _transition_view(transition: ProposalTransition) -> ProposalTransitionResponse:
    """把服务层的流转结果映射为响应。

    ``can_become_active`` 原样透传提案的取值——它**恒为 false**，
    而把它写进响应而不是省略，是为了让客户端能检查它（不变量 11）。
    """
    return ProposalTransitionResponse(
        proposal_id=transition.proposal.id,
        status=transition.proposal.status,
        version=transition.proposal.version,
        audit_event_id=transition.event.id,
        can_become_active=transition.proposal.can_become_active,
    )
