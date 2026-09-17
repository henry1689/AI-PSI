"""结构化输出的提取修复，以及 Provider 工厂（任务书 §8.1、§18）。

两件事放在一起测，是因为它们共同回答同一个问题：
**"真实模型不按约定说话时，系统还能不能安全地继续？"**
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from ai_psi.config import Settings
from ai_psi.domain.exceptions import ConfigurationError, StructuredOutputError
from ai_psi.providers.mock import MockProvider
from ai_psi.providers.openai_compatible import OpenAICompatibleProvider
from ai_psi.providers.parsing import (
    describe_shape,
    extract_json_object,
    extract_json_object_with_strategy,
    strip_code_fence,
)
from ai_psi.providers.registry import (
    AVAILABLE_PROVIDERS,
    DEFAULT_MODELS,
    NOT_IMPLEMENTED_PROVIDERS,
    build_provider_with_client,
    provider_health,
    resolve_model,
)
from ai_psi.providers.resilience import ResilientProvider

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 提取与修复
# ---------------------------------------------------------------------------


class TestExtraction:
    def test_plain_json(self) -> None:
        assert extract_json_object('{"a": 1}') == {"a": 1}

    def test_whitespace_is_tolerated(self) -> None:
        assert extract_json_object('\n\n  {"a": 1}  \n') == {"a": 1}

    def test_code_fence_is_stripped(self) -> None:
        assert extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
        assert extract_json_object('```\n{"a": 1}\n```') == {"a": 1}

    def test_prose_around_json(self) -> None:
        text = '好的，结果如下：\n{"a": 1}\n希望有帮助。'
        assert extract_json_object(text) == {"a": 1}

    def test_nested_objects(self) -> None:
        text = '前言 {"a": {"b": {"c": 1}}} 后记'
        assert extract_json_object(text) == {"a": {"b": {"c": 1}}}

    def test_braces_inside_strings_do_not_confuse_the_scanner(self) -> None:
        """🔴 字符串里的 ``}`` 如果被当成结束符，提取会截断 JSON。"""
        payload = '{"note": "这里的 } 是内容的一部分", "ok": true}'
        assert extract_json_object(payload) == {
            "note": "这里的 } 是内容的一部分",
            "ok": True,
        }

    def test_escaped_quotes_are_handled(self) -> None:
        payload = '{"note": "他说 \\"你好\\"", "ok": true}'
        assert extract_json_object(payload)["ok"] is True

    def test_unicode_is_preserved(self) -> None:
        assert extract_json_object('{"文": "标准大气压"}') == {"文": "标准大气压"}

    def test_empty_input_raises(self) -> None:
        with pytest.raises(StructuredOutputError, match="空内容"):
            extract_json_object("   ")

    def test_non_json_prose_raises(self) -> None:
        with pytest.raises(StructuredOutputError, match="无法解析"):
            extract_json_object("我无法回答这个问题。")

    def test_top_level_array_is_rejected(self) -> None:
        with pytest.raises(StructuredOutputError, match="无法解析"):
            extract_json_object("[1, 2, 3]")

    def test_truncated_json_raises(self) -> None:
        with pytest.raises(StructuredOutputError):
            extract_json_object('{"a": {"b": 1')

    def test_error_never_contains_the_input(self) -> None:
        """🔴 输入可能含用户正文或注入文本，错误里不得带出来。"""
        secret = "用户的私密内容"
        with pytest.raises(StructuredOutputError) as excinfo:
            extract_json_object(f"不是 JSON：{secret}")
        assert secret not in str(excinfo.value)
        assert secret not in str(excinfo.value.context)

    def test_strip_code_fence_is_idempotent(self) -> None:
        assert strip_code_fence(strip_code_fence('```json\n{"a":1}\n```')) == '{"a":1}'


class TestExtractionStrategy:
    def test_clean_json_reports_no_repair(self) -> None:
        _, strategy = extract_json_object_with_strategy('{"a": 1}')
        assert strategy is None

    def test_fenced_json_reports_repair(self) -> None:
        _, strategy = extract_json_object_with_strategy('```json\n{"a": 1}\n```')
        assert strategy == "stripped_code_fence"

    def test_prose_wrapped_reports_scan(self) -> None:
        _, strategy = extract_json_object_with_strategy('前言 {"a": 1} 后记')
        assert strategy == "scanned_balanced_object"

    def test_strategy_is_audit_information(self) -> None:
        """修复方式要被记录：需要修复说明模型没按约定格式作答。"""
        shape = describe_shape("")
        assert shape == ("len=0 fenced=False starts_with_brace=False ends_with_brace=False")

    def test_describe_shape_does_not_leak_content(self) -> None:
        """🔴 连"开头几个字符"都不给——开头完全可能就是用户正文。"""
        secret = "用户的私密内容在此"
        shape = describe_shape(f"{secret} {{}}")
        assert secret not in shape
        assert "len=" in shape


# ---------------------------------------------------------------------------
# Provider 工厂
# ---------------------------------------------------------------------------


def _settings(**overrides: object) -> Settings:
    payload: dict[str, object] = {"llm_provider": "mock"}
    payload.update(overrides)
    return Settings(**payload)  # type: ignore[arg-type]


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))


class TestProviderFactory:
    def test_mock_provider(self) -> None:
        provider = build_provider_with_client(_settings(), _client())
        assert isinstance(provider, ResilientProvider)
        assert isinstance(provider.inner, MockProvider)

    def test_deepseek_provider(self) -> None:
        provider = build_provider_with_client(
            _settings(llm_provider="deepseek", deepseek_api_key=SecretStr("k")),
            _client(),
        )
        assert isinstance(provider, ResilientProvider)
        assert isinstance(provider.inner, OpenAICompatibleProvider)
        assert provider.name == "deepseek"
        assert provider.inner.base_url == "https://api.deepseek.com/v1"

    def test_deepseek_base_url_can_be_overridden(self) -> None:
        provider = build_provider_with_client(
            _settings(
                llm_provider="deepseek",
                deepseek_api_key=SecretStr("k"),
                deepseek_base_url="https://proxy.internal/v1",
            ),
            _client(),
        )
        assert isinstance(provider, ResilientProvider)
        assert isinstance(provider.inner, OpenAICompatibleProvider)
        assert provider.inner.base_url == "https://proxy.internal/v1"

    def test_available_providers_are_documented(self) -> None:
        for name in AVAILABLE_PROVIDERS:
            assert name in DEFAULT_MODELS

    def test_unimplemented_provider_fails_loudly(self) -> None:
        """🔴 Anthropic 未实现——**明确报错**，不静默回落。"""
        assert "anthropic" in NOT_IMPLEMENTED_PROVIDERS
        with pytest.raises(ConfigurationError, match="尚未实现"):
            build_provider_with_client(_settings(llm_provider="anthropic"), _client())

    def test_unknown_provider_fails_loudly(self) -> None:
        with pytest.raises(ConfigurationError, match="未知的 Provider"):
            build_provider_with_client(_settings(llm_provider="gpt5-turbo-ultra"), _client())

    def test_missing_api_key_fails_loudly(self) -> None:
        """🔴 缺 Key 时**绝不**静默回落到 Mock。

        静默回落会让一次配置失误伪装成"系统跑得很好"：
        用户以为在跟真实模型对话，实际拿到的是规则引擎拼出来的占位输出。
        """
        with pytest.raises(ConfigurationError) as excinfo:
            build_provider_with_client(_settings(llm_provider="deepseek"), _client())
        assert "AI_PSI_DEEPSEEK_API_KEY" in str(excinfo.value)
        assert "不会" in str(excinfo.value)  # 明确说明不会回落

    def test_model_resolution(self) -> None:
        assert resolve_model(_settings(llm_provider="deepseek")) == "deepseek-v4-flash"
        assert resolve_model(_settings(llm_provider="mock")) == "mock-model-v1"
        assert resolve_model(_settings(llm_provider="deepseek", llm_model="custom")) == "custom"

    def test_json_mode_can_be_disabled_via_settings(self) -> None:
        provider = build_provider_with_client(
            _settings(
                llm_provider="deepseek",
                deepseek_api_key=SecretStr("k"),
                llm_json_mode=False,
            ),
            _client(),
        )
        assert isinstance(provider, ResilientProvider)
        assert isinstance(provider.inner, OpenAICompatibleProvider)

    def test_breaker_is_configured_from_settings(self) -> None:
        provider = build_provider_with_client(
            _settings(llm_circuit_failure_threshold=7, llm_circuit_recovery_seconds=5.0),
            _client(),
        )
        assert isinstance(provider, ResilientProvider)
        assert provider.breaker.failure_threshold == 7
        assert provider.breaker.recovery_seconds == 5.0


class TestProviderHealth:
    def test_healthy_provider(self) -> None:
        provider = build_provider_with_client(_settings(), _client())
        status, detail = provider_health(provider)
        assert status == "ok"
        assert detail == "mock"

    def test_open_circuit_is_degraded(self) -> None:
        """🔴 熔断打开 → 健康状态 ``degraded``（任务书 §13.2）。

        ``DEGRADED`` 是**系统健康状态**，不是回合状态（ADR-0012）——
        它出现在健康接口，不出现在状态机里。
        """
        provider = build_provider_with_client(_settings(llm_circuit_failure_threshold=1), _client())
        assert isinstance(provider, ResilientProvider)
        provider.breaker.record_failure()
        status, detail = provider_health(provider)
        assert status == "degraded"
        assert "open" in detail


class TestReasoningHeadroomPlumbing:
    """🔴 推理预留**只能有一个默认值**。

    阶段 4 踩过一次：配置层与 Provider 层各写了一个默认值，
    改 Provider 的那个"毫无效果"——因为配置层的 1024 静默覆盖了它。
    两个都看起来权威的默认值，比没有默认值更难排查。
    """

    def _headroom_of(self, settings: Settings) -> int:
        provider = build_provider_with_client(settings, _client())
        assert isinstance(provider, ResilientProvider)
        assert isinstance(provider.inner, OpenAICompatibleProvider)
        return provider.inner.reasoning_headroom_tokens

    def test_defaults_to_the_provider_value(self) -> None:
        from ai_psi.providers.openai_compatible import DEFAULT_REASONING_HEADROOM

        settings = _settings(llm_provider="deepseek", deepseek_api_key=SecretStr("k"))
        assert settings.llm_reasoning_headroom_tokens is None
        assert self._headroom_of(settings) == DEFAULT_REASONING_HEADROOM

    def test_explicit_value_wins(self) -> None:
        settings = _settings(
            llm_provider="deepseek",
            deepseek_api_key=SecretStr("k"),
            llm_reasoning_headroom_tokens=777,
        )
        assert self._headroom_of(settings) == 777

    def test_zero_is_honoured_not_treated_as_unset(self) -> None:
        """``0`` 是合法配置（关掉预留）——不能用 ``or`` 把它当成"没配"。"""
        settings = _settings(
            llm_provider="deepseek",
            deepseek_api_key=SecretStr("k"),
            llm_reasoning_headroom_tokens=0,
        )
        assert self._headroom_of(settings) == 0
