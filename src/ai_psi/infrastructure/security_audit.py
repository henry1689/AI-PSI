"""非法请求的**独立安全审计通道**（阶段 6.5 §三.7）。

🔴 **为什么它必须独立于领域事件流。**

一个被拒绝的请求有两个特征，恰好与领域事务不相容：

1. **它没有回合、没有用户、没有关联链。** 往领域事件流里写它，
   就要么伪造这些字段（那是在审计记录里撒谎），要么让它们为空
   （那会让"这个回合发生了什么"的查询多出一堆无关的行）。
2. **它不该有能力影响任何领域事务。** 请求被拒绝的原因可能是
   恶意的、畸形的、或超长的——让这样一个输入参与事务，
   等于给它一个"让别人的写入回滚"的机会。

因此本通道**只写结构化日志，不写数据库、不参与任何事务**。

## 通道名是接口的一部分

日志走独立的 ``ai_psi.security`` logger。运维可以据此单独配置
落盘、告警与保留策略，而不必去领域日志里做关键词过滤。

## 记什么、不记什么

| 记 | 不记 |
|---|---|
| 路径、方法、错误码 | 请求正文 |
| **出错字段的路径**（``body.evidence.0``） | 字段的**值** |
| 客户端标识（若有且已通过长度校验） | 任何用户原文片段 |

🔴 **只记字段路径，不记字段值。** 被拒绝的输入里最常见的正是
"不该被存下来的东西"——把它的值记进日志，等于用一个拒绝动作
完成了那次本被拒绝的写入。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from ai_psi.infrastructure.logging import get_logger

__all__ = ["SECURITY_LOGGER_NAME", "record_rejected_request"]

#: 安全审计通道名。
SECURITY_LOGGER_NAME: Final[str] = "ai_psi.security"

#: 记录出错字段路径的最大条数。
#:
#: 一次畸形请求可能带出成百上千条校验错误（例如一个超长数组的
#: 每一项都非法）。全记下来会让日志本身成为一条放大攻击的通道。
_MAX_FIELD_PATHS: Final[int] = 20


def record_rejected_request(
    *,
    path: str,
    method: str,
    code: str,
    reason: str,
    field_paths: Sequence[str] = (),
) -> None:
    """把一次被拒绝的请求记进安全审计通道。

    Args:
        path: 请求路径。
        method: HTTP 方法。
        code: 机器可读的错误码（与响应里给客户端的**同一个**）。
        reason: 拒绝原因（受控文本，不含用户输入）。
        field_paths: 出错字段的路径；**不是**它们的值。
    """
    truncated = list(field_paths[:_MAX_FIELD_PATHS])
    logger = get_logger(SECURITY_LOGGER_NAME)
    logger.warning(
        "request_rejected",
        path=path,
        method=method,
        code=code,
        reason=reason,
        field_paths=truncated,
        # 🔴 截断本身要被记录。只报前 20 条而不说明"还有多少条"，
        # 会让一次千字段的畸形请求在审计里看起来只是一次普通的 20 字段错误。
        field_path_overflow=max(0, len(field_paths) - len(truncated)),
    )
