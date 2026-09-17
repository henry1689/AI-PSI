"""测试辅助工具。

本模块只放一类东西：**让"故意构造非法对象"这件事有一个显式出口。**

为什么需要它——验证"非法输入必须被拒绝"的测试，本质上就是要把
静态类型检查不认可的值传进构造函数。mypy 会正确地报错，但这里它
报的不是缺陷，而是测试的**意图**。

两个可选做法各有一个缺点：

* ``# type: ignore`` —— 会同时屏蔽该行的**真实**类型错误，
  而且无法从代码上区分"我故意的"与"我写错了"；
* 对测试整体关闭 ``arg-type`` —— 会放过测试代码里真正的类型错误。

因此改为一个带类型的显式出口：函数名本身说明了意图，
调用点一眼可辨，且不影响其他任何检查。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

__all__ = ["construct", "rejects"]


def construct[T: BaseModel](model: type[T], /, **fields: Any) -> T:
    """构造领域对象，**有意绕过静态类型检查**。

    用于「字段值合法，但静态类型看不出来」的场景，例如从
    异构字典解包构造。若只是想验证非法输入被拒绝，请用 :func:`rejects`。

    Args:
        model: 目标领域对象类型。
        **fields: 传给构造函数的字段。

    Returns:
        构造出的实例。

    Raises:
        pydantic.ValidationError: 输入非法。
    """
    return model(**fields)


def rejects[T: BaseModel](model: type[T], /, **fields: Any) -> ValidationError:
    """断言构造**必定失败**，并返回捕获到的校验错误。

    🔴 不要用裸 ``construct()`` 来测拒绝路径——
    如果校验意外地没有触发，裸调用会**静默通过**，
    测试就成了摆设。本函数在构造成功时主动失败。

    Args:
        model: 目标领域对象类型。
        **fields: 应当触发校验失败的字段。

    Returns:
        捕获到的 ``ValidationError``，供进一步断言字段路径。

    Raises:
        AssertionError: 构造**成功**了——即系统接受了一个非法输入。
    """
    try:
        model(**fields)
    except ValidationError as exc:
        return exc
    pytest.fail(f"{model.__name__} 接受了本应被拒绝的输入：{sorted(fields)}")
