"""学习链路路由（任务书 §11.1–§11.4，阶段 6.5 §四/§七）。

```
POST /api/v1/learning/runs
```

🔴 **这条路由补的是一个"能力存在但没有入口"的洞。**

`PatternDetector` / `PromotionPolicy` / `ProposalGenerator` /
`OfflineEvaluator` 在阶段 6 收尾时在 `src/` 里**零调用者**——
"三次同类错误可生成 Proposal"这条验收条件只在测试里成立，
在跑起来的系统里**不可操作**。阶段 6.5 的能力证据矩阵把
它记为 E1（仅测试），而 §七 要求黑盒验收必须走
"正式 API + 真实 PostgreSQL"。

因此这里加一条**运维入口**。它做三件事，但**不自动做任何决定**：

1. 读事件流里的经验与它们的评价；
2. 跑离线评测（基线快照；带窗口时做对照）；
3. 对达到模式门槛的观察**逐个过门禁**，授权后才生成 `DRAFT`。

🔴 **它不能批准任何东西。** 生成的提案停在 `DRAFT`，
评估与批准仍然要人来做（不变量 11）。本路由没有任何
通往"生效"的参数、字段或副作用。

⚠️ **它不是定时任务。** V0.1 没有 worker，也没有调度器——
这条路由是**被调用一次跑一次**的（任务书 §12.1：回合同步执行）。
"让系统从经验里学到东西"这件事因此仍然是一个**人的动作**，
而这是刻意的（ADR-0018 §10：自动生成提案等于用噪声喂评审）。

## 权限边界

V0.1 **没有认证层**（R40）。这条路由不额外暴露任何数据——
它读的都是事件流里已有的、由同一个进程服务的经验与回合。
但它**会写**（生成提案），因此在有认证层之前，
它应当只绑定在运维接口上。这一点写在 `docs/security.md` 的
"已知限制"里。
"""

from __future__ import annotations

from fastapi import APIRouter

from ai_psi.api.dependencies import ContainerDep
from ai_psi.api.schemas import LearningRunRequest, LearningRunResponse
from ai_psi.application.learning_service import EvaluationWindow

__all__ = ["router"]

router = APIRouter(prefix="/learning", tags=["learning"])


@router.post("/runs", response_model=LearningRunResponse)
async def run_learning(
    container: ContainerDep,
    request: LearningRunRequest | None = None,
) -> LearningRunResponse:
    """跑一次学习链路。

    Args:
        container: 依赖容器。
        request: 本次运行的输入；全部可选。

    Returns:
        本次运行的结果——**包括"什么都没生成"的原因**。
    """
    body = request if request is not None else LearningRunRequest()
    window = (
        None
        if body.baseline_round_ids is None
        else EvaluationWindow(
            baseline=tuple(body.baseline_round_ids),
            candidate=(
                None if body.candidate_round_ids is None else tuple(body.candidate_round_ids)
            ),
        )
    )
    run = await container.learning_service.review(
        fix_direction=body.fix_direction,
        evaluation_window=window,
        actor_id=body.actor_id,
    )
    return LearningRunResponse.from_run(run)
