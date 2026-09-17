"""健康检查路由（任务书 §12.5）。

三个接口的语义**刻意不同**：

* ``/health/live`` —— 进程活着吗？永远只回答这个问题。
* ``/health/ready`` —— 依赖就绪吗（数据库、Provider）？
* ``/health/cognitive`` —— **认知系统**健康吗？检查宪法指纹、
  Prompt 契约完整性与预算配置。

⚠️ ``DEGRADED``（任务书 §13.2）是**系统健康状态**，不是回合状态
（ADR-0012）——它出现在这里，不出现在状态机里。
"""

from __future__ import annotations

from fastapi import APIRouter

from ai_psi.api.dependencies import ContainerDep
from ai_psi.api.schemas import DimensionStatus, HealthResponse
from ai_psi.cognition.constitution import INVARIANTS, constitution_fingerprint
from ai_psi.container import Container
from ai_psi.domain.cognitive_rounds import CognitiveBudget
from ai_psi.domain.enums import CognitiveDepth
from ai_psi.reliability import health as health_module

__all__ = ["router"]

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    """存活探针。

    Returns:
        只要进程能响应就返回 ``ok``。**不检查任何依赖**——
        把依赖检查塞进存活探针会导致依赖抖动时进程被反复重启。
    """
    return HealthResponse(status="ok", checks=[DimensionStatus(name="process", ok=True)])


@router.get("/ready", response_model=HealthResponse)
async def ready(container: ContainerDep) -> HealthResponse:
    """就绪探针。

    Args:
        container: 依赖容器。

    Returns:
        各依赖的可用性。**任一依赖不可用时 ``status`` 为 ``degraded``**。
    """
    checks = [
        DimensionStatus(
            name="storage_backend",
            ok=True,
            detail=container.settings.storage_backend,
        ),
        _provider_check(container),
    ]
    if container.engine is not None:
        checks.append(await _check_database(container))
    else:
        checks.append(
            DimensionStatus(
                name="database",
                ok=True,
                detail="内存后端，未使用数据库",
            )
        )

    return HealthResponse(
        status="ok" if all(check.ok for check in checks) else "degraded",
        checks=checks,
    )


@router.get("/cognitive", response_model=HealthResponse)
async def cognitive(container: ContainerDep) -> HealthResponse:
    """认知系统健康检查。

    🔴 检查的是**认知资产**而不是网络连通性：
    宪法指纹是否存在、Prompt 契约是否完整、预算配置是否自洽、
    向量 Provider 是哪个空间，以及**不变量的结构性保证还在不在**。
    一台"网络全通但宪法被改坏了"的机器**不健康**。

    ⚠️ 响应里的 ``unchecked_invariants`` 列出**未做运行期检查**的不变量。
    它们是显式列出的，而不是被省略——"报告里没有这一项"与
    "这一项没检查"必须能区分开。

    Args:
        container: 依赖容器。

    Returns:
        各认知维度的检查结果。
    """
    settings = container.settings
    embeddings = container.embeddings
    dimensions = [
        health_module.constitution_dimension(fingerprint=constitution_fingerprint()),
        health_module.prompt_contract_dimension(task_names=container.prompts.task_names()),
        health_module.budget_dimension(
            repetition_threshold=settings.repetition_threshold,
            model_call_ceilings=[
                CognitiveBudget.for_depth(depth).max_model_calls for depth in CognitiveDepth
            ],
        ),
        health_module.embedding_dimension(
            provider=embeddings.name,
            version=embeddings.version,
            dimension=embeddings.dimension,
        ),
        health_module.invariant_dimension(),
    ]
    report = health_module.report(
        dimensions, all_invariant_ids=[item.invariant_id for item in INVARIANTS]
    )
    return HealthResponse(
        status=report.status,
        checks=[
            DimensionStatus(name=item.name, ok=item.ok, detail=item.detail)
            for item in report.dimensions
        ]
        + [
            DimensionStatus(
                name="unchecked_invariants",
                ok=True,
                detail=(
                    "未做运行期检查（由测试覆盖）：" + "、".join(report.unchecked_invariants)
                    if report.unchecked_invariants
                    else "全部不变量均有运行期检查"
                ),
            )
        ],
    )


def _provider_check(container: Container) -> DimensionStatus:
    """Provider 健康检查。

    🔴 **熔断打开时状态是 ``degraded``**（任务书 §13.2）。
    注意 ``DEGRADED`` 是**系统健康状态**，不是回合状态（ADR-0012）——
    它出现在这里，不出现在状态机里。
    """
    status, detail = container.provider_status()
    dimension = health_module.provider_dimension(status=status, detail=detail)
    return DimensionStatus(name=dimension.name, ok=dimension.ok, detail=dimension.detail)


async def _check_database(container: Container) -> DimensionStatus:
    """检查数据库连通性。"""
    from sqlalchemy import text

    engine = container.engine
    if engine is None:  # pragma: no cover - 调用方已判空
        return DimensionStatus(name="database", ok=False, detail="引擎未初始化")
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as exc:
        return DimensionStatus(
            name="database",
            ok=False,
            detail=f"连接失败：{type(exc).__name__}",
        )
    return DimensionStatus(name="database", ok=True, detail="连接正常")
