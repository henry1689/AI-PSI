"""FastAPI 依赖注入。

容器在应用启动时构造一次，挂在 ``app.state`` 上；路由通过
:func:`get_container` 拿到它。

⚠️ 刻意**不**用 ``@lru_cache`` 的全局单例：那会让测试无法为每个用例
装上自己的容器（内存存储），从而迫使测试去连真实数据库。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from ai_psi.container import Container

__all__ = ["ContainerDep", "get_container"]


def get_container(request: Request) -> Container:
    """从应用状态取出容器。

    Args:
        request: 当前请求。

    Returns:
        装配好的依赖容器。

    Raises:
        RuntimeError: 应用未初始化容器（说明没有走 ``create_app``）。
    """
    container = getattr(request.app.state, "container", None)
    if not isinstance(container, Container):
        msg = "应用尚未初始化依赖容器，请通过 create_app() 构造应用"
        raise RuntimeError(msg)
    return container


#: 路由里声明容器依赖的标准写法。
#:
#: 🔴 用 ``Annotated`` 而不是 ``container: Container = Depends(...)``：
#: 后者把 ``Depends`` 调用写在默认值里（ruff B008 会拦），
#: 而且默认值对静态检查与文档生成都是噪声。``Annotated`` 把
#: "这是依赖"这件事放在类型位置上，语义也更准确。
ContainerDep = Annotated[Container, Depends(get_container)]
