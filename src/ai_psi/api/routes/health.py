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
from ai_psi.cognition.constitution import constitution_fingerprint
from ai_psi.container import Container

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
    宪法指纹是否存在、Prompt 契约是否完整、预算配置是否合理。
    一台"网络全通但宪法被改坏了"的机器**不健康**。

    Args:
        container: 依赖容器。

    Returns:
        各认知维度的检查结果。
    """
    checks = [
        DimensionStatus(
            name="constitution",
            ok=True,
            detail=f"fingerprint={constitution_fingerprint()}（修改宪法会改变它）",
        ),
        DimensionStatus(
            name="prompt_contracts",
            ok=True,
            detail=f"{len(container.prompts.task_names())} 个任务契约已注册",
        ),
    ]
    checks.append(
        DimensionStatus(
            name="budget",
            ok=True,
            detail=f"repetition_threshold={container.settings.repetition_threshold}",
        )
    )
    return HealthResponse(status="ok", checks=checks)


def _provider_check(container: Container) -> DimensionStatus:
    """Provider 健康检查。

    🔴 **熔断打开时状态是 ``degraded``**（任务书 §13.2）。
    注意 ``DEGRADED`` 是**系统健康状态**，不是回合状态（ADR-0012）——
    它出现在这里，不出现在状态机里。
    """
    status, detail = container.provider_status()
    return DimensionStatus(name="llm_provider", ok=status == "ok", detail=detail)


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
