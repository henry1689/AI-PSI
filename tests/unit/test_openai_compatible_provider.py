"""OpenAI 兼容 Provider（阶段 4）。

🔴 **全程使用 ``httpx.MockTransport``，不打真实网络。**

这不是为了图快：真实 API 的响应不可复现，而这里要验证的恰恰是
**错误分支**——429、500、超时、截断、非法 JSON。这些分支在真实调用里
要么很难触发，要么触发一次就要花钱。用 MockTransport 可以把它们
穷尽地、确定地覆盖。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from ai_psi.domain.exceptions import (
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    StructuredOutputError,
)
from ai_psi.prompts.schemas import ConcernDetectorOutput
from ai_psi.providers.base import InvocationContext, LLMMessage, ModelConfig
from ai_psi.providers.openai_compatible import (
    DEFAULT_JSON_SCHEMA_HINT,
    OpenAICompatibleProvider,
)
from ai_psi.providers.response import ProviderResponse

pytestmark = pytest.mark.unit

BASE_URL = "https://provider.test/v1"
MODEL = "test-model"
CONFIG = ModelConfig(model=MODEL, max_output_tokens=500, timeout_seconds=5.0)

#: 出现在响应里的"隐藏思维链"。**任何地方都不该看到它。**
REASONING_SENTINEL = "这是模型的内部推理过程不应被保存"


def _completion(
    content: str,
    *,
    finish_reason: str = "stop",
    reasoning: str | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一个标准的 chat completion 响应体。"""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage
        or {
            "prompt_tokens": 120,
            "completion_tokens": 40,
            "completion_tokens_details": {"reasoning_tokens": 25},
        },
    }


def _valid_payload() -> str:
    return json.dumps(
        {
            "concerns": [
                {
                    "statement": "用户想知道沸点",
                    "why_it_matters": "用户明确提问",
                    "category": "user_request",
                }
            ]
        },
        ensure_ascii=False,
    )


def _provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    use_json_mode: bool = True,
    headroom: int = 1024,
) -> tuple[OpenAICompatibleProvider, list[httpx.Request]]:
    """构造 Provider 并捕获它发出的请求。"""
    captured: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    provider = OpenAICompatibleProvider(
        client=client,
        api_key=SecretStr("sk-test-key"),
        base_url=BASE_URL,
        name="test",
        use_json_mode=use_json_mode,
        reasoning_headroom_tokens=headroom,
    )
    return provider, captured


def _messages() -> list[LLMMessage]:
    return [LLMMessage(role="user", content="水在标准大气压下的沸点是多少？")]


async def _structured(
    provider: OpenAICompatibleProvider,
) -> ProviderResponse[ConcernDetectorOutput]:
    return await provider.generate_structured(
        task_name="concern_detector",
        messages=_messages(),
        response_model=ConcernDetectorOutput,
        model_config=CONFIG,
        invocation_context=InvocationContext(),
    )


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


class TestSuccessPath:
    async def test_parses_clean_json(self) -> None:
        provider, _ = _provider(lambda _: httpx.Response(200, json=_completion(_valid_payload())))
        response = await _structured(provider)
        assert isinstance(response.value, ConcernDetectorOutput)
        assert response.value.concerns[0].category.value == "user_request"

    async def test_reports_token_usage(self) -> None:
        """任务书 §8.2 要求记录 token 用量——只有 Provider 知道它。"""
        provider, _ = _provider(lambda _: httpx.Response(200, json=_completion(_valid_payload())))
        response = await _structured(provider)
        assert response.usage.input_tokens == 120
        assert response.usage.output_tokens == 40
        assert response.usage.reasoning_tokens == 25
        assert response.usage.total_tokens == 160
        assert response.usage.is_reported is True

    async def test_clean_json_needs_no_repair(self) -> None:
        provider, _ = _provider(lambda _: httpx.Response(200, json=_completion(_valid_payload())))
        response = await _structured(provider)
        assert response.output_repair is None

    async def test_response_hash_is_sha256_of_content(self) -> None:
        provider, _ = _provider(lambda _: httpx.Response(200, json=_completion(_valid_payload())))
        response = await _structured(provider)
        assert response.raw_hash is not None
        assert len(response.raw_hash) == 64

    async def test_text_call(self) -> None:
        provider, _ = _provider(lambda _: httpx.Response(200, json=_completion("这是一段回答")))
        response = await provider.generate_text(
            task_name="response_renderer",
            messages=_messages(),
            model_config=CONFIG,
            invocation_context=InvocationContext(),
        )
        assert response.value == "这是一段回答"


# ---------------------------------------------------------------------------
# 🔴 红线一：隐藏思维链绝不外泄
# ---------------------------------------------------------------------------


class TestReasoningContentIsDropped:
    async def test_reasoning_never_reaches_the_caller(self) -> None:
        """推理模型的 ``reasoning_content`` 是完整隐藏思维链，**不得保存**。"""
        provider, _ = _provider(
            lambda _: httpx.Response(
                200, json=_completion(_valid_payload(), reasoning=REASONING_SENTINEL)
            )
        )
        response = await _structured(provider)
        assert REASONING_SENTINEL not in repr(response)
        assert REASONING_SENTINEL not in response.value.model_dump_json()

    async def test_reasoning_is_not_part_of_the_response_object(self) -> None:
        """不是"过滤掉了"，而是**根本没有这个字段**。"""
        provider, _ = _provider(lambda _: httpx.Response(200, json=_completion(_valid_payload())))
        response = await _structured(provider)
        assert not hasattr(response, "reasoning_content")
        assert "reasoning" not in set(response.__dataclass_fields__)

    async def test_only_the_count_is_kept(self) -> None:
        """用量里的推理 token **计数**属于成本信息，可以保留。"""
        provider, _ = _provider(lambda _: httpx.Response(200, json=_completion(_valid_payload())))
        response = await _structured(provider)
        assert response.usage.reasoning_tokens == 25


# ---------------------------------------------------------------------------
# 结构化输出修复
# ---------------------------------------------------------------------------


class TestOutputRepair:
    async def test_fenced_json_is_repaired(self) -> None:
        content = f"```json\n{_valid_payload()}\n```"
        provider, _ = _provider(lambda _: httpx.Response(200, json=_completion(content)))
        response = await _structured(provider)
        assert response.output_repair == "stripped_code_fence"

    async def test_prose_wrapped_json_is_repaired(self) -> None:
        content = f"好的，结果如下：\n{_valid_payload()}\n希望有帮助。"
        provider, _ = _provider(lambda _: httpx.Response(200, json=_completion(content)))
        response = await _structured(provider)
        assert response.output_repair is not None

    async def test_unparseable_output_raises(self) -> None:
        provider, _ = _provider(
            lambda _: httpx.Response(200, json=_completion("我无法回答这个问题。"))
        )
        with pytest.raises(StructuredOutputError, match="无法解析"):
            await _structured(provider)

    async def test_error_does_not_leak_model_output(self) -> None:
        """🔴 模型输出可能包含用户正文或注入文本，错误里不得带出来。"""
        secret = "用户的私密内容不应出现在错误信息里"
        provider, _ = _provider(lambda _: httpx.Response(200, json=_completion(secret)))
        with pytest.raises(StructuredOutputError) as excinfo:
            await _structured(provider)
        assert secret not in str(excinfo.value)
        assert secret not in str(excinfo.value.context)

    async def test_schema_violation_reports_field_paths_only(self) -> None:
        payload = json.dumps({"concerns": [{"statement": "缺字段"}]}, ensure_ascii=False)
        provider, _ = _provider(lambda _: httpx.Response(200, json=_completion(payload)))
        with pytest.raises(StructuredOutputError) as excinfo:
            await _structured(provider)
        assert excinfo.value.validation_errors
        assert any("why_it_matters" in item for item in excinfo.value.validation_errors)

    async def test_illegal_enum_is_rejected_not_guessed(self) -> None:
        """🔴 不做模糊匹配——把 MECHANISTIC 猜成 MECHANICAL 会让错误静默通过。"""
        payload = json.dumps(
            {
                "concerns": [
                    {
                        "statement": "x",
                        "why_it_matters": "y",
                        "category": "not_a_real_category",
                    }
                ]
            }
        )
        provider, _ = _provider(lambda _: httpx.Response(200, json=_completion(payload)))
        with pytest.raises(StructuredOutputError):
            await _structured(provider)


# ---------------------------------------------------------------------------
# 截断与空响应
# ---------------------------------------------------------------------------


class TestTruncation:
    async def test_truncated_response_is_flagged(self) -> None:
        """``finish_reason=length`` 意味着输出被切断——半个 JSON 不是结果。"""
        provider, _ = _provider(
            lambda _: httpx.Response(
                200, json=_completion('{"concerns": [', finish_reason="length")
            )
        )
        with pytest.raises(StructuredOutputError):
            await _structured(provider)

    async def test_truncated_text_call_also_raises(self) -> None:
        """半截回答不是回答——文本调用同样不能被截断当作成功。"""
        provider, _ = _provider(
            lambda _: httpx.Response(200, json=_completion("写了一半", finish_reason="length"))
        )
        with pytest.raises(StructuredOutputError, match="截断"):
            await provider.generate_text(
                task_name="response_renderer",
                messages=_messages(),
                model_config=CONFIG,
                invocation_context=InvocationContext(),
            )

    async def test_truncation_is_not_retryable(self) -> None:
        """🔴 同样的上限会得到同样的截断——重试只是把预算烧掉。"""
        provider, _ = _provider(
            lambda _: httpx.Response(200, json=_completion("{}", finish_reason="length"))
        )
        with pytest.raises(StructuredOutputError) as excinfo:
            await _structured(provider)
        assert excinfo.value.retryable is False

    async def test_truncation_is_reported_before_parsing(self) -> None:
        """🔴 **顺序很重要。**

        截断的 JSON 当然解析不了。如果先解析，报出来的会是
        "JSON 语法错误"——一个指向不存在的问题的诊断，
        而真正的原因（max_tokens 太小）无从得知。
        """
        provider, _ = _provider(
            lambda _: httpx.Response(
                200, json=_completion('{"concerns": [{"statement": "被切', finish_reason="length")
            )
        )
        with pytest.raises(StructuredOutputError) as excinfo:
            await _structured(provider)
        assert "截断" in str(excinfo.value)
        assert "无法解析" not in str(excinfo.value)

    async def test_empty_content_is_reported_as_a_budget_problem(self) -> None:
        """推理占满预算时 ``content`` 会是空的——这是预算问题，不是格式问题。"""
        provider, _ = _provider(
            lambda _: httpx.Response(
                200, json=_completion("", finish_reason="stop", reasoning=REASONING_SENTINEL)
            )
        )
        with pytest.raises(StructuredOutputError, match="空内容"):
            await _structured(provider)


# ---------------------------------------------------------------------------
# HTTP 错误映射
# ---------------------------------------------------------------------------


class TestErrorMapping:
    async def test_rate_limit_carries_retry_after(self) -> None:
        provider, _ = _provider(
            lambda _: httpx.Response(429, headers={"Retry-After": "12"}, json={})
        )
        with pytest.raises(ProviderRateLimitError) as excinfo:
            await _structured(provider)
        assert excinfo.value.retry_after_seconds == 12.0
        assert excinfo.value.retryable is True

    async def test_server_error_is_retryable(self) -> None:
        provider, _ = _provider(lambda _: httpx.Response(503, json={}))
        with pytest.raises(ProviderUnavailableError) as excinfo:
            await _structured(provider)
        assert excinfo.value.retryable is True

    async def test_client_error_is_not_retryable(self) -> None:
        """4xx 里除了 429 都是"请求本身有问题"：重试多少次都一样。"""
        provider, _ = _provider(lambda _: httpx.Response(400, json={}))
        with pytest.raises(ProviderError) as excinfo:
            await _structured(provider)
        assert excinfo.value.retryable is False

    async def test_timeout_maps_to_provider_timeout(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow", request=request)

        provider, _ = _provider(handler)
        with pytest.raises(ProviderTimeoutError):
            await _structured(provider)

    async def test_connection_error_is_unavailable(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        provider, _ = _provider(handler)
        with pytest.raises(ProviderUnavailableError):
            await _structured(provider)

    async def test_missing_choices_is_a_structured_output_error(self) -> None:
        provider, _ = _provider(lambda _: httpx.Response(200, json={"choices": []}))
        with pytest.raises(StructuredOutputError, match="choices"):
            await _structured(provider)

    async def test_error_context_never_contains_the_api_key(self) -> None:
        provider, _ = _provider(lambda _: httpx.Response(500, json={}))
        with pytest.raises(ProviderUnavailableError) as excinfo:
            await _structured(provider)
        assert "sk-test-key" not in str(excinfo.value)
        assert "sk-test-key" not in str(excinfo.value.context)


# ---------------------------------------------------------------------------
# 请求构造
# ---------------------------------------------------------------------------


class TestRequestShape:
    async def test_authorization_header_carries_the_key(self) -> None:
        provider, captured = _provider(
            lambda _: httpx.Response(200, json=_completion(_valid_payload()))
        )
        await _structured(provider)
        assert captured[0].headers["Authorization"] == "Bearer sk-test-key"

    async def test_json_mode_adds_response_format_and_hint(self) -> None:
        provider, captured = _provider(
            lambda _: httpx.Response(200, json=_completion(_valid_payload()))
        )
        await _structured(provider)
        body = json.loads(captured[0].content)
        assert body["response_format"] == {"type": "json_object"}
        assert body["messages"][-1]["content"] == DEFAULT_JSON_SCHEMA_HINT

    async def test_json_mode_can_be_disabled(self) -> None:
        """不支持 ``response_format`` 的兼容服务器必须能用。"""
        provider, captured = _provider(
            lambda _: httpx.Response(200, json=_completion(_valid_payload())),
            use_json_mode=False,
        )
        await _structured(provider)
        body = json.loads(captured[0].content)
        assert "response_format" not in body

    async def test_text_call_does_not_force_json(self) -> None:
        provider, captured = _provider(lambda _: httpx.Response(200, json=_completion("文本")))
        await provider.generate_text(
            task_name="response_renderer",
            messages=_messages(),
            model_config=CONFIG,
            invocation_context=InvocationContext(),
        )
        body = json.loads(captured[0].content)
        assert "response_format" not in body

    async def test_reasoning_headroom_is_added_to_max_tokens(self) -> None:
        """🔴 推理 token 计入 max_tokens——不加预留，答案会被推理挤掉。"""
        provider, captured = _provider(
            lambda _: httpx.Response(200, json=_completion(_valid_payload())), headroom=512
        )
        await _structured(provider)
        body = json.loads(captured[0].content)
        assert body["max_tokens"] == CONFIG.max_output_tokens + 512

    async def test_streaming_is_disabled(self) -> None:
        """结构化输出必须是完整响应，不能是流。"""
        provider, captured = _provider(
            lambda _: httpx.Response(200, json=_completion(_valid_payload()))
        )
        await _structured(provider)
        assert json.loads(captured[0].content)["stream"] is False

    async def test_base_url_trailing_slash_is_tolerated(self) -> None:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=_completion(_valid_payload()))
            )
        )
        provider = OpenAICompatibleProvider(
            client=client, api_key=SecretStr("k"), base_url=f"{BASE_URL}/"
        )
        await _structured(provider)  # 不抛异常即通过

    async def test_default_name(self) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
        provider = OpenAICompatibleProvider(
            client=client, api_key=SecretStr("k"), base_url=BASE_URL
        )
        assert provider.name == "openai_compatible"
        assert provider.base_url == BASE_URL
