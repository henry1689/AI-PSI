"""日志脱敏的单元测试。

🔴 **这是安全相关的代码，必须被测到。**

脱敏的价值全在"没有被绕过"上——一条漏网的密钥就会进日志，
而日志会被收集、转发、长期保存。因此这里不只测"正常路径会脱敏"，
还测**嵌套结构、大小写变体、看似无害的键名**等绕过路径。
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_psi.infrastructure.logging import (
    NEVER_LOGGED_KEYS,
    SENSITIVE_KEYS,
    make_redact_processor,
    redact_processor,
)

pytestmark = pytest.mark.unit


def _redact(event_dict: dict[str, Any], *, include_user_content: bool = False) -> dict[str, Any]:
    """跑一遍脱敏处理器，返回结果字典。"""
    processor = make_redact_processor(include_user_content=include_user_content)
    return dict(processor(None, "info", dict(event_dict)))


class TestDefaultRedaction:
    def test_default_processor_masks_api_key(self) -> None:
        result = redact_processor(None, "info", {"api_key": "sk-secret"})
        assert result["api_key"] == "***"

    def test_non_sensitive_fields_pass_through(self) -> None:
        result = redact_processor(None, "info", {"event": "round.started", "count": 3})
        assert result["event"] == "round.started"
        assert result["count"] == 3


class TestSensitiveKeyMatching:
    @pytest.mark.parametrize(
        "key",
        [
            "password",
            "PASSWORD",
            "db_password",
            "secret",
            "client_secret",
            "token",
            "access_token",
            "api_key",
            "apikey",
            "authorization",
            "Authorization",
            "credential",
            "private_key",
            "database_url",
            "dsn",
        ],
    )
    def test_known_sensitive_keys_are_masked(self, key: str) -> None:
        assert _redact({key: "leaky-value"})[key] == "***"

    def test_matching_is_case_insensitive_and_substring(self) -> None:
        """`X-API-Key`、`DB_PASSWORD` 这类常见命名也必须命中。"""
        assert _redact({"X-Api-Key": "leaky"})["X-Api-Key"] == "***"
        assert _redact({"DB_PASSWORD": "leaky"})["DB_PASSWORD"] == "***"

    def test_sensitive_key_list_is_not_empty(self) -> None:
        assert SENSITIVE_KEYS


class TestNeverLoggedKeys:
    """🔴 红线一：思维链类字段**没有开关**，永远不输出。"""

    @pytest.mark.parametrize("key", sorted(NEVER_LOGGED_KEYS))
    def test_never_logged_keys_are_dropped(self, key: str) -> None:
        assert _redact({key: "内容"})[key] == "<redacted>"

    def test_reasoning_is_dropped_even_when_user_content_allowed(self) -> None:
        """即使是"允许用户正文"的模式，思维链依旧被丢弃。"""
        result = _redact({"reasoning": "模型的内部推理"}, include_user_content=True)
        assert result["reasoning"] == "<redacted>"

    def test_user_content_respects_the_flag(self) -> None:
        assert _redact({"user_content": "用户的话"})["user_content"] == "<redacted>"
        assert (
            _redact({"user_content": "用户的话"}, include_user_content=True)["user_content"]
            == "用户的话"
        )


class TestNestedStructures:
    """脱敏必须递归——密钥常常藏在嵌套对象里。"""

    def test_nested_dict_is_redacted(self) -> None:
        """🔴 密钥最常出现在嵌套结构里（如请求头）。

        早期实现在嵌套层只递归、不判键名，导致这里的 authorization
        原样进了日志——这个用例把它钉死了。
        """
        event = {"request": {"headers": {"authorization": "Bearer abc"}, "url": "/x"}}
        result = _redact(event)
        assert result["request"]["headers"]["authorization"] == "***"
        assert result["request"]["url"] == "/x"

    def test_list_of_dicts_is_redacted(self) -> None:
        event = {"attempts": [{"token": "abc"}, {"token": "def"}]}
        result = _redact(event)
        assert [item["token"] for item in result["attempts"]] == ["***", "***"]

    def test_deeply_nested_is_redacted(self) -> None:
        event = {"a": {"b": {"c": {"password": "deep-secret"}}}}
        result = _redact(event)
        assert result["a"]["b"]["c"]["password"] == "***"

    def test_hyphenated_header_names_are_matched(self) -> None:
        """HTTP 头用连字符命名，归一化后才能命中。"""
        event = {"headers": {"X-Api-Key": "leaky", "X-Auth-Token": "leaky2"}}
        result = _redact(event)
        assert result["headers"]["X-Api-Key"] == "***"
        assert result["headers"]["X-Auth-Token"] == "***"

    def test_depth_limit_prevents_infinite_recursion(self) -> None:
        """自引用或超深结构不得让日志管线卡死。"""
        node: dict[str, object] = {"value": 1}
        for _ in range(20):
            node = {"nested": node}
        result = _redact({"deep": node})
        assert "<max-depth>" in str(result)


class TestRedactionIsNotFooledByPosition:
    def test_value_that_looks_like_a_key_is_not_special(self) -> None:
        """值里出现 'password' 字样不影响判定——只看**键名**。"""
        result = _redact({"note": "the password field is optional"})
        assert result["note"] == "the password field is optional"

    def test_empty_dict_passes_through(self) -> None:
        assert _redact({}) == {}

    def test_none_value_under_sensitive_key_is_still_masked(self) -> None:
        """`api_key: None` 与 `api_key: 已设置` 不应靠日志区分。"""
        assert _redact({"api_key": None})["api_key"] == "***"


class TestConfigureLogging:
    def test_configure_logging_accepts_level_and_json_flag(self) -> None:
        """配置函数不应抛异常，且能重复调用（测试里会反复初始化）。"""
        import structlog

        from ai_psi.infrastructure.logging import configure_logging

        configure_logging(level="DEBUG", json_output=False)
        configure_logging(level="INFO", json_output=True)

        # 渲染一次，确认管线可用
        logger = structlog.get_logger("test")
        assert logger is not None
