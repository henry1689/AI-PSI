"""事件循环兼容层。

🔴 **为什么需要这个模块：**

psycopg3 的异步驱动**不能**在 Windows 默认的 ``ProactorEventLoop`` 上运行——
它会直接抛 ``InterfaceError``。这是 Windows 特有的问题，
Linux / CI（ubuntu-latest）上一切照常，因此这类缺陷**只会在开发机上出现**，
而开发机恰恰是最容易被忽略的环境。

把兼容逻辑集中在这里，而不是散落在 alembic、测试、应用入口各写一遍，
是为了让"为什么要有这段代码"只有一处解释。

上游参考：https://www.psycopg.org/psycopg3/docs/advanced/async.html
"""

from __future__ import annotations

import asyncio
import selectors
import sys

__all__ = ["install_selector_loop_policy", "make_selector_loop"]

_IS_WINDOWS = sys.platform == "win32"


def make_selector_loop() -> asyncio.AbstractEventLoop:
    """创建一个 psycopg 异步驱动可用的事件循环。

    Returns:
        在 Windows 上返回绑定 ``SelectSelector`` 的 ``SelectorEventLoop``；
        其他平台返回平台默认的 selector 事件循环。
    """
    if _IS_WINDOWS:
        return asyncio.SelectorEventLoop(selectors.SelectSelector())
    return asyncio.SelectorEventLoop()


def install_selector_loop_policy() -> None:
    """为**由框架创建**事件循环的场景安装 selector 策略。

    适用场景：pytest-asyncio、uvicorn 等自行创建事件循环的框架——
    我们无法向它们传递 ``loop_factory``，只能改策略。

    Note:
        直接使用 :func:`make_selector_loop` 的地方（如 alembic 的
        ``asyncio.Runner(loop_factory=...)``）**不需要**调用本函数。
        能用 ``loop_factory`` 就优先用它：不产生全局副作用。
    """
    if not _IS_WINDOWS:
        return
    policy_cls = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy_cls is not None:
        asyncio.set_event_loop_policy(policy_cls())
