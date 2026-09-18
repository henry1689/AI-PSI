"""结构化日志配置与脱敏。

🔴 **默认脱敏**（任务书 §17.1）。

脱敏不是"记得别打印密钥"，而是**在日志管线上强制**：
任何进入日志的字典都会被逐层检查，命中敏感键名的值一律替换为 ``"***"``。
这比依赖每个调用点自觉可靠得多——人总会忘。
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any, Final

import structlog

__all__ = [
    "NEVER_LOGGED_KEYS",
    "SENSITIVE_KEYS",
    "configure_logging",
    "get_logger",
    "make_redact_processor",
]


#: 命中即脱敏的键名（大小写不敏感，子串匹配）。
SENSITIVE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "auth",
        "credential",
        "private_key",
        "access_key",
        "session_id",
        "cookie",
        "database_url",
        "dsn",
        "connection_string",
    }
)

#: 日志中**永不出现**的键——无论其值是什么。
#: 思维链与用户正文都不应进日志（`docs/cognitive_constitution.md` 红线一）。
NEVER_LOGGED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "reasoning",
        "thinking",
        "chain_of_thought",
        "raw_response",
        "prompt_text",
        "user_content",
    }
)

_REDACTED = "***"
_DROPPED = "<redacted>"
_MAX_DEPTH = "<max-depth>"

#: 递归深度上限。防御自引用结构与病态深嵌套——日志管线卡死会拖垮整个进程。
_MAX_REDACT_DEPTH: Final[int] = 6


def _normalize_key(key: str) -> str:
    """归一化键名以便匹配。

    🔴 **必须把 ``-`` 归一成 ``_``。** HTTP 头用连字符（``X-Api-Key``），
    配置项用下划线（``api_key``）——只匹配后者会让真实存在的泄漏路径漏网。
    同时去掉常见的前缀/后缀噪声，让 ``db_password``、``userToken`` 这类
    命名都能命中同一个标记。
    """
    return key.lower().replace("-", "_").replace(" ", "")


def _is_sensitive(key: str) -> bool:
    """键名是否属于需要掩码的敏感字段。"""
    normalized = _normalize_key(key)
    return any(marker in normalized for marker in SENSITIVE_KEYS)


def _is_never_logged(key: str) -> bool:
    """键名是否属于**永不输出**的字段（思维链、原始响应等）。"""
    return _normalize_key(key) in NEVER_LOGGED_KEYS


def _redact_entry(key: str, value: Any, *, include_user_content: bool, depth: int = 0) -> Any:
    """对单个键值对做脱敏，并**递归**处理容器。

    🔴 **每一次递归都要重新判定键名。** 早期实现只在顶层判键、嵌套层只递归，
    结果是 ``{"request": {"headers": {"authorization": "Bearer ..."}}}``
    里的 authorization **原样进了日志**——密钥恰恰最常出现在嵌套结构里。

    Args:
        key: 当前键名。
        value: 当前值。
        include_user_content: 是否保留用户正文。
        depth: 当前递归深度。

    Returns:
        脱敏后的值。
    """
    lowered = _normalize_key(key)

    if _is_never_logged(key) and not (include_user_content and lowered == "user_content"):
        return _DROPPED
    if _is_sensitive(key):
        return _REDACTED

    if depth >= _MAX_REDACT_DEPTH:
        return _MAX_DEPTH

    if isinstance(value, dict):
        return {
            str(k): _redact_entry(
                str(k), v, include_user_content=include_user_content, depth=depth + 1
            )
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        # 列表元素沿用父键名判定：["secret"] 出现在哪个键下，
        # 说明这些元素就是那个键的内容
        return [
            _redact_entry(key, item, include_user_content=include_user_content, depth=depth + 1)
            for item in value
        ]
    return value


# ⚠️ 这里曾经有一个模块级的 ``redact_processor``，它的文档写着
# **"这是默认配置使用的处理器"**——而那是**假的**：
# ``configure_logging`` 装的是 ``make_redact_processor(...)``，
# 全仓没有任何地方用它。
#
# 阶段 6.5 §八 评审 A 的评语值得原样留着：**一个死函数，其文档却
# 声称自己站在脱敏路径上。** 这类谎报比"少一个函数"危险得多——
# 读代码的人会以为"用户正文默认被丢弃"这条保证落在这里，
# 于是既不会去 `configure_logging` 核对，也不会去测它。
#
# 处置按 §四 的三选一：删除（它的行为等价于
# ``make_redact_processor(include_user_content=False)``，
# 而后者才是真正被装配的那个）。原有测试已改为直接测后者。


def make_redact_processor(*, include_user_content: bool) -> Any:
    """构造一个按配置决定是否丢弃用户正文的脱敏处理器。

    Args:
        include_user_content: 为 ``True`` 时保留 ``user_content``。

    Returns:
        可直接放进 structlog 处理链的可调用对象。

    Note:
        🔴 即使 ``include_user_content=True``，思维链类字段
        （``reasoning`` / ``thinking`` / ``raw_response``）**依然被丢弃**——
        那一条没有开关（`docs/cognitive_constitution.md` 红线一）。
    """

    def _processor(
        _logger: Any,
        _method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        return _redact(event_dict, include_user_content=include_user_content)

    return _processor


def _redact(
    event_dict: MutableMapping[str, Any],
    *,
    include_user_content: bool,
) -> MutableMapping[str, Any]:
    """逐键脱敏。

    Args:
        event_dict: 事件字典（原地修改）。
        include_user_content: 是否保留用户正文。

    Returns:
        同一个字典。
    """
    for key in list(event_dict):
        event_dict[key] = _redact_entry(
            key, event_dict[key], include_user_content=include_user_content
        )
    return event_dict


def configure_logging(
    *,
    level: str = "INFO",
    json_output: bool = True,
    include_user_content: bool = False,
) -> None:
    """配置进程级结构化日志。

    Args:
        level: 日志级别。
        json_output: 是否输出 JSON（生产 ``True``；本地调试可设 ``False`` 便于阅读）。
        include_user_content: 是否允许用户正文进入日志。

    Note:
        🔴 ``include_user_content`` 默认为 ``False``。即使设为 ``True``，
        :data:`NEVER_LOGGED_KEYS` 中的键（思维链、原始响应）依然永不输出——
        那一条**没有开关**。
    """
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        # 脱敏必须在渲染之前——渲染之后就是字符串，无法再安全处理
        make_redact_processor(include_user_content=include_user_content),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    processors.append(
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    """返回一个按名字绑定的结构化日志器。

    统一入口的意义在于：**日志器的配置只有一处**。
    如果各处自行调用 ``structlog.get_logger`` 或标准库 logging，
    脱敏处理器就可能在某条路径上被绕过——而脱敏一旦被绕过，
    泄漏的就是密钥或用户正文。

    Args:
        name: 通常传 ``__name__``。

    Returns:
        已绑定名字的 structlog 日志器。
    """
    return structlog.get_logger(name)
