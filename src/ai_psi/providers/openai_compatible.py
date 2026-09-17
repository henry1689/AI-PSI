"""OpenAI 兼容的 HTTP Provider（任务书 §8.1）。

覆盖 DeepSeek、OpenAI 以及绝大多数"兼容 OpenAI 协议"的服务——
它们只在 base_url、模型名与少数细节上不同，因此共用一个实现，
差异通过构造参数表达（`providers/registry.py` 里做预设）。

🔴 **三条硬约束：**

1. **``reasoning_content`` 永不离开本模块。**
   推理模型的响应里带着完整的内部思维链。认知宪法红线一明令禁止
   保存或展示它。因此本模块**只读 ``content``**，
   连"顺手带上"都不做——不读它就写不进日志，写不进事件。
   （用量里的 ``reasoning_tokens`` 只是**计数**，属于成本信息。）

2. **截断不算成功，而且要在解析之前判定。**
   ``finish_reason == "length"`` 时输出是被切断的，半个 JSON 不是结果。
   反过来先解析的话，截断会被误报成"JSON 语法错误"——
   一个指向不存在的问题的诊断，而且它可重试，
   于是同样的上限会被反复撞上。网关另有一道兜底（防第三方实现不守约）。

3. **API Key 只在请求头里出现。** 它以 :class:`~pydantic.SecretStr` 注入，
   任何日志、异常、``repr`` 都不会带出明文。
"""

from __future__ import annotations

import hashlib
from typing import Any, Final

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from ai_psi.domain.exceptions import StructuredOutputError
from ai_psi.providers.base import InvocationContext, LLMMessage, ModelConfig
from ai_psi.providers.http import map_http_error
from ai_psi.providers.parsing import describe_shape, extract_json_object_with_strategy
from ai_psi.providers.response import ProviderResponse, TokenUsage

__all__ = ["DEFAULT_JSON_SCHEMA_HINT", "OpenAICompatibleProvider"]

#: 结构化调用时补在系统消息后的一句提醒。
#:
#: 提示词模板里**已经**写明了输出 Schema 与"只返回 JSON"的要求；
#: 这里补的是给 API 的**开关性提示**：OpenAI 与 DeepSeek 都要求
#: 消息中至少出现一次 "json" 字样，才允许使用 JSON 模式。
#: 它不改变任务语义，因此不会让提示词契约失真。
DEFAULT_JSON_SCHEMA_HINT: Final[str] = (
    "只返回一个 json 对象作为你的最终答复，不要包含任何解释、前言或代码块标记。"
)

#: 推理模型的输出预留（token）。
#:
#: 推理模型把输出预算的一部分花在内部推理上。DeepSeek v4 flash 的**实测**：
#: 一个只要求回一个词的请求烧掉约 50 个推理 token；
#: 关系推测类问题的候选项生成实测推理 2000+。
#:
#: 契约里的 ``max_output_tokens`` 描述的是**答案**的长度，
#: 因此本模块额外加上这段预留，避免答案被推理挤掉后截断。
#:
#: 🔴 **它是"上限"而不是"花费"。** 模型没有生成到那么多 token 就不会被计费，
#: 但上限设小了会**截断**——而截断一次要丢掉一次分析甚至整个回合。
#: 因此这里的取值原则是"宁可宽、不可紧"。
#:
#: 阶段 4 的实测（DeepSeek v4 flash，见 ADR-0016）：
#: ``logical_analyzer`` 在关系推测类问题上单次推理常达 5–8k token，
#: 极端情况下超过 10k；``concept_analyzer`` 约 5k。
#: 8192 仍会在真实回合里偶发截断，故取 16384。
#:
#: ⚠️ 这个数字偏大本身是一个**信号**：说明提示词没有约束输出规模，
#: 模型在自由发挥。真正的解法是让模板显式限定列表长度与条数
#: （需要评测支撑，列为阶段 7 的工作）。
DEFAULT_REASONING_HEADROOM: Final[int] = 16384


class OpenAICompatibleProvider:
    """基于 ``/chat/completions`` 的 Provider。"""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        api_key: SecretStr,
        base_url: str,
        name: str = "openai_compatible",
        use_json_mode: bool = True,
        reasoning_headroom_tokens: int = DEFAULT_REASONING_HEADROOM,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        """初始化。

        Args:
            client: 共享的 HTTP 客户端（生命周期由容器管理）。
            api_key: 供应商密钥。
            base_url: 形如 ``https://api.deepseek.com/v1`` 的基础地址。
            name: Provider 名称，写入模型调用记录。
            use_json_mode: 是否启用供应商的 JSON 输出模式。
                不支持该参数的服务器应置为 ``False``——
                那会退化为"只靠提示词约束 + 提取修复"，成功率低一些但仍可用。
            reasoning_headroom_tokens: 给推理模型的额外输出预留。
            extra_headers: 额外的请求头（部分供应商需要）。
        """
        self._client = client
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._name = name
        self._use_json_mode = use_json_mode
        self._headroom = max(0, reasoning_headroom_tokens)
        self._extra_headers = dict(extra_headers or {})

    @property
    def name(self) -> str:
        """Provider 名称。"""
        return self._name

    @property
    def base_url(self) -> str:
        """基础地址（不含密钥，可安全记录）。"""
        return self._base_url

    @property
    def reasoning_headroom_tokens(self) -> int:
        """给推理模型的额外输出预留。"""
        return self._headroom

    # ------------------------------------------------------------------
    # LLMProvider 协议
    # ------------------------------------------------------------------

    async def generate_structured[T: BaseModel](
        self,
        *,
        task_name: str,
        messages: list[LLMMessage],
        response_model: type[T],
        model_config: ModelConfig,
        invocation_context: InvocationContext,
    ) -> ProviderResponse[T]:
        """调用模型并返回经 Schema 校验的对象。

        Raises:
            StructuredOutputError: 输出无法解析或不满足 Schema。
            ProviderTimeoutError: 超时。
            ProviderRateLimitError: 触发速率限制。
            ProviderUnavailableError: 供应商不可用。
            ProviderError: 其他调用失败，或请求本身被拒绝。
        """
        del invocation_context
        payload = self._build_payload(
            messages=messages,
            model_config=model_config,
            structured=True,
        )
        content, usage, finish_reason, raw_hash = await self._post(
            task_name=task_name, payload=payload, model_config=model_config
        )

        # 🔴 **先判截断，再解析。**
        #
        # 顺序反了会把"输出被切断"误报成"JSON 语法错误"：
        # 截断的 JSON 当然解析不了，于是错误信息指向一个不存在的问题，
        # 而真正的原因（max_tokens 太小）无从得知。
        # 更糟的是，解析失败默认**可重试**——同样的上限会得到同样的截断，
        # 重试只是把预算烧掉。
        #
        # 这个顺序问题是阶段 4 跑真实回合时发现的：一次截断被报成
        # "无法解析为 JSON 对象（已尝试原样 / 去代码块 / 括号配平扫描）"。
        self._reject_truncation(finish_reason, task_name=task_name, model=model_config.model)

        if not content.strip():
            msg = (
                "模型返回了空内容——通常是内部推理占满了输出预算，"
                "或触发了长度上限。这不是可重试的格式问题，而是预算问题"
            )
            raise StructuredOutputError(
                msg, task_name=task_name, provider=self._name, model=model_config.model
            )

        parsed, repair = extract_json_object_with_strategy(content)
        try:
            value = response_model.model_validate(parsed)
        except ValidationError as exc:
            # 🔴 只记录**字段路径与原因**，绝不记录模型原始输出。
            details = tuple(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            msg = (
                f"任务 {task_name!r} 的输出不符合 {response_model.__name__} Schema"
                f"（{describe_shape(content)}）"
            )
            raise StructuredOutputError(
                msg,
                validation_errors=details,
                task_name=task_name,
                provider=self._name,
                model=model_config.model,
            ) from exc

        return ProviderResponse(
            value=value,
            usage=usage,
            finish_reason=finish_reason,
            raw_hash=raw_hash,
            output_repair=repair,
        )

    async def generate_text(
        self,
        *,
        task_name: str,
        messages: list[LLMMessage],
        model_config: ModelConfig,
        invocation_context: InvocationContext,
    ) -> ProviderResponse[str]:
        """调用模型并返回纯文本。

        Raises:
            ProviderTimeoutError: 超时。
            ProviderRateLimitError: 触发速率限制。
            ProviderUnavailableError: 供应商不可用。
            ProviderError: 其他调用失败。
        """
        del invocation_context
        payload = self._build_payload(
            messages=messages, model_config=model_config, structured=False
        )
        content, usage, finish_reason, raw_hash = await self._post(
            task_name=task_name, payload=payload, model_config=model_config
        )
        # 文本调用同样不能被截断当作成功——半截回答不是回答。
        self._reject_truncation(finish_reason, task_name=task_name, model=model_config.model)
        if not content.strip():
            msg = "模型返回了空文本响应"
            raise StructuredOutputError(
                msg, task_name=task_name, provider=self._name, model=model_config.model
            )
        return ProviderResponse(
            value=content,
            usage=usage,
            finish_reason=finish_reason,
            raw_hash=raw_hash,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _reject_truncation(
        self,
        finish_reason: str | None,
        *,
        task_name: str,
        model: str,
    ) -> None:
        """输出因长度上限被截断时**直接判失败**。

        🔴 **不可重试。** 同样的提示词 + 同样的上限会得到同样的截断；
        重试只会烧掉预算，同时让真正的原因继续隐藏。

        Raises:
            StructuredOutputError: ``finish_reason`` 表明输出被截断。
        """
        if finish_reason is None or finish_reason.lower() not in {
            "length",
            "max_tokens",
            "max_output_tokens",
        }:
            return
        msg = (
            f"任务 {task_name!r} 的输出被长度上限截断（finish_reason={finish_reason!r}）。"
            "推理模型的推理 token 也计入上限——请提高该任务的 max_output_tokens，"
            "或调大 AI_PSI_LLM_REASONING_HEADROOM_TOKENS"
        )
        raise StructuredOutputError(
            msg,
            validation_errors=(f"finish_reason={finish_reason}",),
            task_name=task_name,
            provider=self._name,
            model=model,
            retryable=False,
        )

    def _build_payload(
        self,
        *,
        messages: list[LLMMessage],
        model_config: ModelConfig,
        structured: bool,
    ) -> dict[str, Any]:
        """构造 ``/chat/completions`` 的请求体。"""
        payload: dict[str, Any] = {
            "model": model_config.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": model_config.temperature,
            # 推理模型的推理 token 也计入 max_tokens，因此要加上预留，
            # 否则"答案还没开始写"就已经被截断。
            "max_tokens": model_config.max_output_tokens + self._headroom,
            "stream": False,
        }
        if structured and self._use_json_mode:
            payload["messages"] = [
                *payload["messages"],
                {"role": "system", "content": DEFAULT_JSON_SCHEMA_HINT},
            ]
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def _post(
        self,
        *,
        task_name: str,
        payload: dict[str, Any],
        model_config: ModelConfig,
    ) -> tuple[str, TokenUsage, str | None, str]:
        """执行一次 HTTP 调用并返回 ``(content, usage, finish_reason, hash)``。

        Raises:
            ProviderError: 各种调用失败（已映射为领域异常）。
        """
        headers = {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Content-Type": "application/json",
            **self._extra_headers,
        }
        try:
            response = await self._client.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=model_config.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise map_http_error(
                exc, provider=self._name, model=model_config.model, task_name=task_name
            ) from exc

        body = response.json()
        return self._read_choice(body, task_name=task_name, model=model_config.model)

    def _read_choice(
        self,
        body: dict[str, Any],
        *,
        task_name: str,
        model: str,
    ) -> tuple[str, TokenUsage, str | None, str]:
        """从响应体里取出内容、用量与结束原因。

        Raises:
            StructuredOutputError: 响应结构不符合 OpenAI 协议。
        """
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            msg = "供应商响应中没有 choices"
            raise StructuredOutputError(msg, task_name=task_name, provider=self._name, model=model)

        choice = choices[0]
        message = choice.get("message") or {}
        # 🔴 只读 content。reasoning_content 是模型的完整隐藏思维链，
        # 认知宪法红线一明令不保存——不读它就写不进任何地方。
        content = message.get("content") or ""

        usage_payload = body.get("usage") or {}
        details = usage_payload.get("completion_tokens_details") or {}
        usage = TokenUsage(
            input_tokens=_as_int(usage_payload.get("prompt_tokens")),
            output_tokens=_as_int(usage_payload.get("completion_tokens")),
            reasoning_tokens=_as_int(details.get("reasoning_tokens")),
        )

        return (
            str(content),
            usage,
            choice.get("finish_reason"),
            hashlib.sha256(str(content).encode("utf-8")).hexdigest(),
        )


def _as_int(value: object) -> int | None:
    """把可选的计数字段转为 ``int``。"""
    return value if isinstance(value, int) else None
