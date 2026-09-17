"""FastAPI 应用装配（任务书 §12）。

统一前缀 ``/api/v1``。错误处理见 :mod:`ai_psi.api.errors`——
**任何异常都不会把堆栈或内部控制信息返回给客户端**（任务书 §17.1）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ai_psi.api.errors import register_error_handlers
from ai_psi.api.routes import cognitive_rounds, conversations, health, memories, replay
from ai_psi.config import Settings, get_settings
from ai_psi.container import build_container
from ai_psi.infrastructure.logging import configure_logging, get_logger

__all__ = ["API_PREFIX", "create_app"]

#: 任务书 §12 规定的统一前缀。
API_PREFIX = "/api/v1"


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造 FastAPI 应用。

    Args:
        settings: 运行时配置；``None`` 时读取进程配置。

    Returns:
        可直接交给 ASGI 服务器运行的应用。
    """
    resolved = settings if settings is not None else get_settings()
    configure_logging(
        level=resolved.log_level,
        json_output=not resolved.debug,
        include_user_content=resolved.log_include_user_content,
    )
    logger = get_logger(__name__)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """应用生命周期。

        🔴 容器在**启动时**构造、**关闭时**释放。
        连接池不关闭会让进程无法干净退出，而在请求里惰性构造容器
        则会让每个请求都拿到一套新依赖——包括全新的内存存储。
        """
        container = build_container(resolved)
        app.state.container = container
        logger.info(
            "app_started",
            config=container.settings.redacted_summary(),
            prompts=len(container.prompts.task_names()),
        )
        try:
            yield
        finally:
            await container.aclose()
            logger.info("app_stopped")

    app = FastAPI(
        title="AI-PSI Cognitive Runtime",
        version="0.1.0",
        description=(
            "可追踪、可测试、可纠正的认知运行时 V0.1。"
            "**不是聊天机器人**——每次回答都对应一次结构化的认知回合。"
        ),
        lifespan=lifespan,
    )

    register_error_handlers(app)
    app.include_router(conversations.router, prefix=API_PREFIX)
    app.include_router(cognitive_rounds.router, prefix=API_PREFIX)
    app.include_router(memories.router, prefix=API_PREFIX)
    app.include_router(replay.router, prefix=API_PREFIX)
    app.include_router(health.router, prefix=API_PREFIX)

    return app
