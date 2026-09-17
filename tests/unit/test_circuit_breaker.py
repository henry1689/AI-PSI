"""熔断器与带熔断的 Provider 装饰器（任务书 §8.1、§13.2）。

熔断器是**纯状态机**（时钟可注入），因此这里可以穷尽地走完
关闭 → 打开 → 半开 → 关闭/重新打开 的每一条分支，
不依赖真实时间，也不需要打网络。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from ai_psi.domain.exceptions import (
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    StructuredOutputError,
)
from ai_psi.providers.base import InvocationContext, LLMMessage, ModelConfig
from ai_psi.providers.resilience import ResilientProvider
from ai_psi.providers.response import ProviderResponse, TokenUsage
from ai_psi.reliability.circuit_breaker import CircuitBreaker, CircuitState

pytestmark = pytest.mark.unit


def _state(source: CircuitBreaker | ResilientProvider) -> CircuitState:
    """读取熔断状态（接受熔断器或带熔断的 Provider）。

    ⚠️ 通过函数读取而不是直接访问属性：mypy 会对**成员表达式**做类型收窄，
    连续两次 ``assert _state(x) is Y`` 会让第二次被判定为"不可能发生"，
    进而报 non-overlapping 或 unreachable。函数调用会打断这种收窄。
    """
    return source.state


class _Output(BaseModel):
    value: str = "ok"


class _Clock:
    """可手动推进的时钟。"""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _breaker(*, threshold: int = 3, recovery: float = 30.0) -> tuple[CircuitBreaker, _Clock]:
    clock = _Clock()
    return CircuitBreaker(
        failure_threshold=threshold, recovery_seconds=recovery, clock=clock
    ), clock


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------


class TestCircuitStateMachine:
    def test_starts_closed_and_allows(self) -> None:
        breaker, _ = _breaker()
        assert _state(breaker) is CircuitState.CLOSED
        assert breaker.allow() is True

    def test_opens_after_threshold_failures(self) -> None:
        breaker, _ = _breaker(threshold=3)
        for _ in range(2):
            breaker.record_failure()
        assert _state(breaker) is CircuitState.CLOSED
        breaker.record_failure()
        assert _state(breaker) is CircuitState.OPEN
        assert breaker.allow() is False

    def test_success_resets_the_counter(self) -> None:
        breaker, _ = _breaker(threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        assert breaker.consecutive_failures == 0
        breaker.record_failure()
        assert _state(breaker) is CircuitState.CLOSED

    def test_cooldown_moves_to_half_open(self) -> None:
        breaker, clock = _breaker(threshold=1, recovery=30.0)
        breaker.record_failure()
        assert _state(breaker) is CircuitState.OPEN
        clock.advance(30.0)
        assert _state(breaker) is CircuitState.HALF_OPEN

    def test_half_open_allows_exactly_one_probe(self) -> None:
        breaker, clock = _breaker(threshold=1, recovery=30.0)
        breaker.record_failure()
        clock.advance(30.0)
        assert breaker.allow() is True  # 探针
        assert breaker.allow() is False  # 同一个探针还没回来
        assert breaker.allow() is False

    def test_probe_success_closes(self) -> None:
        breaker, clock = _breaker(threshold=1, recovery=30.0)
        breaker.record_failure()
        clock.advance(30.0)
        breaker.allow()
        breaker.record_success()
        assert _state(breaker) is CircuitState.CLOSED
        assert breaker.allow() is True

    def test_probe_failure_reopens_immediately(self) -> None:
        """探针失败说明故障仍在，没必要再凑满阈值。"""
        breaker, clock = _breaker(threshold=5, recovery=30.0)
        for _ in range(5):
            breaker.record_failure()
        clock.advance(30.0)
        assert breaker.allow() is True
        breaker.record_failure()
        assert _state(breaker) is CircuitState.OPEN
        assert breaker.allow() is False

    def test_reopen_restarts_the_cooldown(self) -> None:
        breaker, clock = _breaker(threshold=1, recovery=30.0)
        breaker.record_failure()
        clock.advance(30.0)
        breaker.allow()
        breaker.record_failure()
        clock.advance(29.0)
        assert _state(breaker) is CircuitState.OPEN
        clock.advance(1.0)
        assert _state(breaker) is CircuitState.HALF_OPEN

    def test_manual_reset(self) -> None:
        breaker, _ = _breaker(threshold=1)
        breaker.record_failure()
        breaker.reset()
        assert _state(breaker) is CircuitState.CLOSED
        assert breaker.allow() is True

    def test_threshold_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="至少为 1"):
            CircuitBreaker(failure_threshold=0)

    def test_recovery_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="必须为正"):
            CircuitBreaker(recovery_seconds=0)


# ---------------------------------------------------------------------------
# 装饰器
# ---------------------------------------------------------------------------


class _CountingProvider:
    """可编程的假 Provider，用来驱动熔断。"""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.calls = 0
        self._error = error

    @property
    def name(self) -> str:
        return "counting"

    async def generate_structured(
        self, *, task_name, messages, response_model, model_config, invocation_context
    ) -> ProviderResponse[Any]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return ProviderResponse(value=_Output(), usage=TokenUsage(1, 1), finish_reason="stop")

    async def generate_text(
        self, *, task_name, messages, model_config, invocation_context
    ) -> ProviderResponse[str]:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return ProviderResponse(value="文本", usage=TokenUsage(1, 1), finish_reason="stop")


async def _call(provider: ResilientProvider) -> ProviderResponse[_Output]:
    return await provider.generate_structured(
        task_name="concern_detector",
        messages=[LLMMessage(role="user", content="hi")],
        response_model=_Output,
        model_config=ModelConfig(model="m"),
        invocation_context=InvocationContext(),
    )


class TestResilientProvider:
    async def test_delegates_name(self) -> None:
        wrapped = ResilientProvider(_CountingProvider(), CircuitBreaker())
        assert wrapped.name == "counting"

    async def test_passes_through_on_success(self) -> None:
        inner = _CountingProvider()
        wrapped = ResilientProvider(inner, CircuitBreaker())
        response = await _call(wrapped)
        assert response.value.value == "ok"
        assert inner.calls == 1

    async def test_refuses_when_open_without_calling(self) -> None:
        """🔴 熔断打开时**不发起调用**——这正是它省下超时等待的方式。"""
        inner = _CountingProvider(error=ProviderUnavailableError("down"))
        wrapped = ResilientProvider(inner, CircuitBreaker(failure_threshold=2))

        for _ in range(2):
            with pytest.raises(ProviderUnavailableError):
                await _call(wrapped)
        assert inner.calls == 2

        with pytest.raises(ProviderUnavailableError, match="熔断开启"):
            await _call(wrapped)
        assert inner.calls == 2, "熔断打开后不应再发起调用"

    async def test_circuit_refusal_is_not_retryable(self) -> None:
        """🔴 打开时重试只是把等待时间乘上重试次数。"""
        inner = _CountingProvider(error=ProviderUnavailableError("down"))
        wrapped = ResilientProvider(inner, CircuitBreaker(failure_threshold=1))
        with pytest.raises(ProviderUnavailableError):
            await _call(wrapped)
        with pytest.raises(ProviderUnavailableError) as excinfo:
            await _call(wrapped)
        assert excinfo.value.retryable is False

    async def test_structured_output_errors_do_not_open_the_circuit(self) -> None:
        """🔴 **模型答得不好不等于供应商宕机。**

        把格式错误计入熔断，会导致"一个写得不好的 Prompt 把整个 Provider 熔断"，
        连本来能正常工作的其他任务也一起被拒。
        """
        inner = _CountingProvider(error=StructuredOutputError("bad json"))
        wrapped = ResilientProvider(inner, CircuitBreaker(failure_threshold=2))
        for _ in range(5):
            with pytest.raises(StructuredOutputError):
                await _call(wrapped)
        assert _state(wrapped) is CircuitState.CLOSED
        assert inner.calls == 5

    async def test_non_retryable_provider_errors_do_not_open_the_circuit(self) -> None:
        """400 是"请求本身有问题"，不是供应商不健康。"""
        inner = _CountingProvider(error=ProviderError("bad request", retryable=False))
        wrapped = ResilientProvider(inner, CircuitBreaker(failure_threshold=2))
        for _ in range(5):
            with pytest.raises(ProviderError):
                await _call(wrapped)
        assert _state(wrapped) is CircuitState.CLOSED

    @pytest.mark.parametrize(
        "error",
        [
            ProviderTimeoutError("timeout"),
            ProviderRateLimitError("slow down"),
            ProviderUnavailableError("down"),
        ],
    )
    async def test_health_failures_do_open_the_circuit(self, error: ProviderError) -> None:
        inner = _CountingProvider(error=error)
        wrapped = ResilientProvider(inner, CircuitBreaker(failure_threshold=2))
        for _ in range(2):
            with pytest.raises(type(error)):
                await _call(wrapped)
        assert _state(wrapped) is CircuitState.OPEN

    async def test_success_closes_a_half_open_circuit(self) -> None:
        clock = _Clock()
        inner = _CountingProvider(error=ProviderUnavailableError("down"))
        wrapped = ResilientProvider(
            inner,
            CircuitBreaker(failure_threshold=1, recovery_seconds=10.0, clock=clock),
        )
        with pytest.raises(ProviderUnavailableError):
            await _call(wrapped)

        clock.advance(10.0)
        inner._error = None  # 供应商恢复
        response = await _call(wrapped)
        assert response.value.value == "ok"
        assert _state(wrapped) is CircuitState.CLOSED

    async def test_text_calls_also_go_through_the_breaker(self) -> None:
        inner = _CountingProvider(error=ProviderUnavailableError("down"))
        wrapped = ResilientProvider(inner, CircuitBreaker(failure_threshold=1))
        with pytest.raises(ProviderUnavailableError):
            await wrapped.generate_text(
                task_name="response_renderer",
                messages=[LLMMessage(role="user", content="hi")],
                model_config=ModelConfig(model="m"),
                invocation_context=InvocationContext(),
            )
        with pytest.raises(ProviderUnavailableError, match="熔断开启"):
            await wrapped.generate_text(
                task_name="response_renderer",
                messages=[LLMMessage(role="user", content="hi")],
                model_config=ModelConfig(model="m"),
                invocation_context=InvocationContext(),
            )
