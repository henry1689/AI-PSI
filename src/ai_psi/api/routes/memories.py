"""长期记忆路由（任务书 §12.3）。

```
GET    /api/v1/users/{user_id}/memories
POST   /api/v1/memories/{memory_id}/correct
DELETE /api/v1/memories/{memory_id}
POST   /api/v1/users/{user_id}/export
DELETE /api/v1/users/{user_id}/data
```

🔴 **V0.1 没有认证层，作用域由调用方显式声明。**

``user_id`` 出现在路径或查询参数里，服务端照此执行作用域过滤。
这意味着**它防的是"代码写错导致的串号"，不是"恶意调用者"**——
后者要靠认证，而认证不在 V0.1 范围内。这条边界必须写在这里，
而不是留给读者从"为什么 user_id 是个参数"里自己推断：

* 它确实挡住了真实的一类缺陷（检索忘记带作用域、纠正打错用户）；
* 它挡不住一个**故意**传入别人 user_id 的调用方。

（登记于 ADR-0017 与 risks.md R40。）

⚠️ **``GET /users/{user_id}/beliefs``（§12.3 的第一条）本阶段不实现。**
信念目前只存在于事件流里，不是记忆；把它列出来需要另建一套判断投影，
那是另一件事，不是本阶段交付的一部分（ADR-0017）。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from ai_psi.api.dependencies import ContainerDep
from ai_psi.api.mappers import memory_view
from ai_psi.api.schemas import (
    CorrectMemoryRequest,
    CorrectMemoryResponse,
    DeleteMemoryResponse,
    MemoryListResponse,
    UserDataDeletionResponse,
    UserDataExportResponse,
)
from ai_psi.memory.ranking import conflicting_ids

__all__ = ["router"]

router = APIRouter(tags=["memories"])

#: 查询参数一律用 ``Annotated`` 声明，不用 ``x: T = Query(...)``。
#:
#: 🔴 后者把函数调用写在默认值里：ruff 的 B008 会拦（默认值在导入时求值，
#: 而 ``Query()`` 每次调用都产生一个新对象），而且默认值对静态检查
#: 与文档生成都是噪声。``Annotated`` 把"这是查询参数"放在类型位置上。
IncludeInactiveQuery = Annotated[
    bool,
    Query(
        description=(
            "是否包含已被取代/删除的记忆。用户查看「为什么发生过修正」时需要 true（任务书 §10.4）"
        )
    ),
]

UserIdQuery = Annotated[
    UUID,
    Query(description="发起删除的用户；必须与记忆的作用域一致"),
]


@router.get("/users/{user_id}/memories", response_model=MemoryListResponse)
async def list_memories(
    user_id: UUID,
    container: ContainerDep,
    include_inactive: IncludeInactiveQuery = False,
) -> MemoryListResponse:
    """列出某用户的记忆。

    Args:
        user_id: 目标用户。
        container: 依赖容器。
        include_inactive: 是否包含非活跃记忆。

    Returns:
        记忆列表；``conflicts_within_results`` 标注结果集内的显式冲突。
    """
    memories = await container.memory_service.list_for_user(
        user_id=user_id, include_inactive=include_inactive
    )
    conflicting = conflicting_ids(memories)
    return MemoryListResponse(
        user_id=user_id,
        count=len(memories),
        include_inactive=include_inactive,
        memories=[
            memory_view(memory, conflicts_within_results=memory.id in conflicting)
            for memory in memories
        ],
    )


@router.post("/memories/{memory_id}/correct", response_model=CorrectMemoryResponse)
async def correct_memory(
    memory_id: UUID,
    body: CorrectMemoryRequest,
    container: ContainerDep,
) -> CorrectMemoryResponse:
    """按用户纠正创建记忆的新版本。

    🔴 **不变量 5：不就地覆盖。** 旧记忆被标记为 ``SUPERSEDED``，
    新记忆指向它。纠错痕迹完整保留，且旧记忆不再出现在默认检索里（不变量 6）。

    Raises:
        NotFoundError: 记忆不存在，或不属于该用户 → 404
            （**不是 403**：说"无权限"本身就泄漏了"这条记录存在"）。
    """
    correction = await container.memory_service.correct(
        user_id=body.user_id,
        memory_id=memory_id,
        new_content=body.new_content,
    )
    return CorrectMemoryResponse(
        corrected_memory_id=correction.superseded.id,
        replacement_memory_id=correction.replacement.id,
        superseded_status=correction.superseded.status,
        replacement_status=correction.replacement.status,
        audit_event_ids=[event.id for event in correction.events],
    )


@router.delete("/memories/{memory_id}", response_model=DeleteMemoryResponse)
async def delete_memory(
    memory_id: UUID,
    container: ContainerDep,
    user_id: UserIdQuery,
) -> DeleteMemoryResponse:
    """删除一条记忆（不变量 15）。

    逻辑删除：本体保留为 ``DELETED`` 以便追溯，**向量索引物理移除**——
    删除之后它不会再出现在任何检索结果里，这一点是结构性的，
    而不是"查询记得加状态过滤"。

    Raises:
        NotFoundError: 记忆不存在，或不属于该用户 → 404。
    """
    deletion = await container.memory_service.delete(user_id=user_id, memory_id=memory_id)
    return DeleteMemoryResponse(
        memory_id=memory_id,
        status=deletion.memory.status,
        audit_event_id=deletion.audit_event.id,
    )


@router.post("/users/{user_id}/export", response_model=UserDataExportResponse)
async def export_user_data(user_id: UUID, container: ContainerDep) -> UserDataExportResponse:
    """导出某用户的全部记忆（任务书 §10.5）。

    🔴 导出**包含被取代与已删除的记忆**：版本链是"为什么发生过修正"
    的唯一答案，只导出有效记忆会让纠错痕迹凭空消失。

    🔴 导出本身也留下审计事件，且该事件**不含正文**——
    否则"导出"就成了把内容抄一份留在事件表里的后门。
    """
    result = await container.memory_service.export_user_data(user_id=user_id)
    return UserDataExportResponse(
        user_id=user_id,
        audit_event_id=result.audit_event.id,
        export=dict(result.payload),
    )


@router.delete("/users/{user_id}/data", response_model=UserDataDeletionResponse)
async def delete_user_data(
    user_id: UUID,
    container: ContainerDep,
) -> UserDataDeletionResponse:
    """删除某用户的全部记忆（任务书 §10.5）。

    ⚠️ **只覆盖记忆。** 事件流、认知回合与判断是系统运行史，不是"用户的记忆"；
    把删除请求扩大成"抹掉所有相关记录"会与"事件只追加"这条根本约束冲突
    （ADR-0002），也会让审计变得不可能。这一点在响应与文档里都写明，
    不留给调用方去猜。
    """
    result = await container.memory_service.delete_user_data(user_id=user_id)
    return UserDataDeletionResponse(
        user_id=user_id,
        deleted_count=result.deleted_count,
        memory_ids=list(result.memory_ids),
        audit_event_ids=[event.id for event in result.audit_events],
    )
