"""依赖装配（组合根）。

🔴 **这是整个系统里唯一知道"哪个实现"的地方。**

其他地方只依赖 Port：应用服务认
:class:`~ai_psi.application.ports.UnitOfWorkFactory`，
认知模块认 :class:`~ai_psi.providers.base.LLMProvider`，
检索认 :class:`~ai_psi.application.ports.MemoryRepository`。
换实现只改这一个文件——阶段 5 把记忆换成 PostgreSQL + pgvector 时，
改动面就是这里的几行（ADR-0009）。

⚠️ **阶段 3 的存储后端边界（登记于 ADR-0015）**：

* ``storage_backend=memory``：事件、回合、幂等、记忆**全部**在内存中，
  零外部依赖，进程重启即清空；
* ``storage_backend=postgres``：事件与回合走阶段 2 的 PostgreSQL 实现，
  但**记忆仍走内存实现**——记忆表要到阶段 5 才建。
  这个混合是刻意的：它让"认知闭环已经跑通"这件事可以在真实数据库上验证，
  同时不会假装记忆已经持久化。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ai_psi.application.artifact_service import ArtifactService
from ai_psi.application.cognitive_runtime import CognitiveRuntime
from ai_psi.application.memory_service import MemoryService
from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.application.round_service import CognitiveRoundService
from ai_psi.config import Settings, get_settings
from ai_psi.infrastructure.in_memory.store import InMemoryStore
from ai_psi.infrastructure.in_memory.unit_of_work import make_in_memory_unit_of_work_factory
from ai_psi.prompts.registry import PromptRegistry
from ai_psi.prompts.versions import build_default_registry
from ai_psi.providers.base import LLMProvider
from ai_psi.providers.embeddings import EmbeddingProvider
from ai_psi.providers.registry import (
    build_embedding_provider,
    build_provider_with_client,
    provider_health,
)

__all__ = ["Container", "build_container"]


@dataclass
class Container:
    """运行时依赖的装配结果。

    Attributes:
        settings: 配置。
        prompts: Prompt 注册表。
        provider: LLM Provider。
        embeddings: 向量 Provider（长期记忆检索）。
        uow_factory: 工作单元工厂。
        round_service: 回合服务。
        artifact_service: 产物记录服务。
        memory_service: 记忆服务。
        runtime: 认知运行时。
        engine: PostgreSQL 引擎（``storage_backend=memory`` 时为 ``None``）。
        http_client: 共享的 HTTP 客户端（真实 Provider 用；Mock 下也存在但不用）。
    """

    settings: Settings
    prompts: PromptRegistry
    provider: LLMProvider
    embeddings: EmbeddingProvider
    uow_factory: UnitOfWorkFactory
    round_service: CognitiveRoundService
    artifact_service: ArtifactService
    memory_service: MemoryService
    runtime: CognitiveRuntime
    engine: AsyncEngine | None = field(default=None)
    http_client: httpx.AsyncClient | None = field(default=None)

    def provider_status(self) -> tuple[str, str]:
        """返回 Provider 的运行健康状况（任务书 §13.2）。

        Returns:
            ``(状态, 说明)``；状态取 ``ok`` 或 ``degraded``。
        """
        return provider_health(self.provider)

    async def aclose(self) -> None:
        """释放资源。

        🔴 必须由应用生命周期调用——连接池不关闭会让进程无法干净退出。
        """
        if self.http_client is not None:
            await self.http_client.aclose()
        if self.engine is not None:
            await self.engine.dispose()


def build_container(settings: Settings | None = None) -> Container:
    """按配置装配全部依赖。

    Args:
        settings: 运行时配置；``None`` 时读取进程配置。

    Returns:
        装配好的容器。
    """
    resolved = settings if settings is not None else get_settings()
    prompts = build_default_registry()
    # 一个进程一个 HTTP 客户端：连接复用、生命周期可控。
    # 每次调用新建客户端会丢掉连接池，也会让超时配置失去统一入口。
    http_client = httpx.AsyncClient()
    provider = build_provider_with_client(resolved, http_client)
    embeddings = build_embedding_provider(resolved, http_client)

    engine: AsyncEngine | None = None
    uow_factory: UnitOfWorkFactory
    if resolved.storage_backend == "postgres":
        # 连接池交给 SQLAlchemy 默认实现：这是长驻进程，不是测试。
        engine = create_async_engine(resolved.database_url, pool_pre_ping=True)
        from ai_psi.infrastructure.db.session import create_session_factory
        from ai_psi.infrastructure.db.unit_of_work import make_unit_of_work_factory

        uow_factory = make_unit_of_work_factory(create_session_factory(engine), embeddings)
    else:
        store = InMemoryStore()
        uow_factory = make_in_memory_unit_of_work_factory(store, embeddings)

    round_service = CognitiveRoundService(uow_factory)
    artifact_service = ArtifactService(uow_factory)
    # 🔴 一个容器一个 MemoryService：它同时被 API 路由与认知运行时使用，
    # 两个实例会各自持有一份写入策略，策略一旦被局部替换就会分家。
    memory_service = MemoryService(uow_factory, embeddings)
    runtime = CognitiveRuntime(
        uow_factory=uow_factory,
        provider=provider,
        prompts=prompts,
        memory_service=memory_service,
        settings=resolved,
        round_service=round_service,
        artifacts=artifact_service,
    )

    return Container(
        settings=resolved,
        prompts=prompts,
        provider=provider,
        embeddings=embeddings,
        uow_factory=uow_factory,
        round_service=round_service,
        artifact_service=artifact_service,
        memory_service=memory_service,
        runtime=runtime,
        engine=engine,
        http_client=http_client,
    )
