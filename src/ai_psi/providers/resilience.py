"""给任意 Provider 套一层熔断。

装饰器而不是"在 OpenAI Provider 里加熔断"的理由：
熔断是**与协议无关**的策略。套在外面，Mock 也能用它，
阶段 5 换任何 Provider 时都不用重写，而熔断本身能被单独测。

🔴 **只有"供应商不健康"类失败才计入熔断。**

:class:`~ai_psi.domain.exceptions.StructuredOutputError` **不计入**。
模型答得不对是提示词或任务的问题，不是供应商宕机；
把它计入会导致"一个写得不好的 Prompt 把整个 Provider 熔断"，
而熔断打开后连本来能正常工作的其他任务也一起被拒。

**熔断拒绝是不可重试的**：熔断打开时重试只是把等待时间乘上重试次数。
"""

from __future__ import annotations

from pydantic import BaseModel

from ai_psi.domain.exceptions import (
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from ai_psi.providers.base import InvocationContext, LLMMessage, LLMProvider, ModelConfig
from ai_psi.providers.response import ProviderResponse
from ai_psi.reliability.circuit_breaker import CircuitBreaker, CircuitState

__all__ = ["PROVIDER_HEALTH_FAILURES", "ResilientProvider"]

#: 计入熔断的失败类型——它们表示"供应商侧出了问题"。
PROVIDER_HEALTH_FAILURES: tuple[type[ProviderError], ...] = (
    ProviderTimeoutError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)


class ResilientProvider:
    """带熔断的 Provider 装饰器。"""

    def __init__(self, inner: LLMProvider, breaker: CircuitBreaker) -> None:
        """初始化。

        Args:
            inner: 被装饰的 Provider（满足 :class:`~ai_psi.providers.base.LLMProvider`）。
            breaker: 熔断器。**必须与 Provider 同生命周期**——
                每回合新建一个熔断器等于没有熔断。
        """
        self._inner = inner
        self._breaker = breaker

    @property
    def inner(self) -> LLMProvider:
        """被装饰的 Provider。"""
        return self._inner

    @property
    def breaker(self) -> CircuitBreaker:
        """熔断器（供健康检查读取状态）。"""
        return self._breaker

    @property
    def name(self) -> str:
        """Provider 名称（沿用内层的）。"""
        return self._inner.name

    async def generate_structured[T: BaseModel](
        self,
        *,
        task_name: str,
        messages: list[LLMMessage],
        response_model: type[T],
        model_config: ModelConfig,
        invocation_context: InvocationContext,
    ) -> ProviderResponse[T]:
        """带熔断的结构化调用。"""
        self._guard()
        try:
            result = await self._inner.generate_structured(
                task_name=task_name,
                messages=messages,
                response_model=response_model,
                model_config=model_config,
                invocation_context=invocation_context,
            )
        except ProviderError as exc:
            self._record(exc)
            raise
        self._breaker.record_success()
        return result

    async def generate_text(
        self,
        *,
        task_name: str,
        messages: list[LLMMessage],
        model_config: ModelConfig,
        invocation_context: InvocationContext,
    ) -> ProviderResponse[str]:
        """带熔断的文本调用。"""
        self._guard()
        try:
            result = await self._inner.generate_text(
                task_name=task_name,
                messages=messages,
                model_config=model_config,
                invocation_context=invocation_context,
            )
        except ProviderError as exc:
            self._record(exc)
            raise
        self._breaker.record_success()
        return result

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _guard(self) -> None:
        """熔断打开时直接拒绝。

        Raises:
            ProviderUnavailableError: 熔断打开，本次调用被拒。
                刻意标为**不可重试**——重试只会把等待时间乘上重试次数。
        """
        if self._breaker.allow():
            return
        msg = (
            f"Provider {self.name!r} 熔断开启（连续失败 "
            f"{self._breaker.consecutive_failures} 次），本次调用被直接拒绝"
        )
        raise ProviderUnavailableError(
            msg,
            provider=self.name,
            retryable=False,
        )

    def _record(self, exc: ProviderError) -> None:
        """记录一次失败，必要时打开熔断。"""
        if isinstance(exc, PROVIDER_HEALTH_FAILURES):
            self._breaker.record_failure()

    @property
    def state(self) -> CircuitState:
        """当前熔断状态（供健康接口使用）。"""
        return self._breaker.state
