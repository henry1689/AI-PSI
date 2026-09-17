"""进程入口。

🔴 **Windows 上必须先装 selector 事件循环策略，再启动 uvicorn。**

psycopg 的异步驱动不支持 Windows 默认的 ``ProactorEventLoop``
（见 :mod:`ai_psi.infrastructure.asyncio_compat`）。uvicorn 的
``--loop asyncio`` 在 Windows 上会新建默认循环，因此策略必须在
``uvicorn.run`` **之前**装好——晚一步就来不及了。

用法::

    uv run python -m ai_psi.main          # 等价于 uv run python src/ai_psi/main.py
    uv run uvicorn ai_psi.api.app:create_app --factory --port 8000
"""

from __future__ import annotations

from ai_psi.infrastructure.asyncio_compat import install_selector_loop_policy

__all__ = ["main"]


def main() -> None:
    """启动 HTTP 服务。"""
    import uvicorn

    from ai_psi.api.app import create_app
    from ai_psi.config import get_settings

    # 顺序要求：先装事件循环策略，再构造应用（create_app 里会配置日志）
    install_selector_loop_policy()

    settings = get_settings()
    uvicorn.run(
        create_app(settings),
        host="127.0.0.1",
        port=8000,
        log_config=None,  # 日志统一走 structlog，不让 uvicorn 覆盖配置
    )


if __name__ == "__main__":
    main()
