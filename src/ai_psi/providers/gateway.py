"""模型调用网关。

它把四件事收在一处，让认知模块不必各自实现：

1. **渲染提示词** —— 通过 :class:`~ai_psi.prompts.registry.PromptRegistry`，
   保证每次调用都有任务名与版本号（不变量 18）；
2. **预算记账** —— 调用 Provider **之前**先扣额度（"循环永不超预算"）；
3. **超时与重试** —— 有限重试，**每次重试都换新的 ``invocation_id``**
   并单独消耗一次预算（任务书 §6.3 明确要求重试不得复用 invocation_id）；
4. **调用记录** —— 产出 :class:`~ai_psi.domain.events.ModelInvocationInfo`，
   交给调用方挂到事件上。

🔴 **只记录响应哈希，不记录响应内容**（任务书 §8.2，
`docs/cognitive_constitution.md` 红线一）。原始响应可能包含模型自由联想，
把它落库等于绕开"不保存完整隐藏思维链"。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from ai_psi.domain.events import ModelInvocationInfo
from ai_psi.domain.exceptions import ProviderError, ProviderTimeoutError
from ai_psi.prompts.registry import PromptRegistry
from ai_psi.providers.base import InvocationContext, LLMProvider
from ai_psi.reliability.budgets import BudgetTracker

__all__ = ["ModelGateway", "StructuredCall", "TextCall"]


@dataclass(frozen=True, slots=True)
class StructuredCall[T: BaseModel]:
    """一次结构化调用的结果。

    Attributes:
        value: 通过 Schema 校验的对象。
        invocation: 调用审计记录（不含响应内容，只有哈希）。
    """

    value: T
    invocation: ModelInvocationInfo


@dataclass(frozen=True, slots=True)
class TextCall:
    """一次文本调用的结果。"""

    text: str
    invocation: ModelInvocationInfo


class ModelGateway:
    """一次认知回合内的模型调用入口。

    与 :class:`~ai_psi.reliability.budgets.BudgetTracker` 一样，
    **每个回合一个实例**——预算记账必须与回合同生命周期。
    """

    def __init__(
        self,
        *,
        provider: LLMProvider,
        prompts: PromptRegistry,
        budget: BudgetTracker,
        model: str,
        max_retries: int = 2,
        timeout_seconds: float | None = None,
    ) -> None:
        """初始化。

        Args:
            provider: 具体的 LLM Provider。
            prompts: Prompt 注册表。
            budget: 本回合的预算记账器。
            model: 模型标识，写入调用记录（不变量 18）。
            max_retries: 额外重试次数（总尝试次数 = ``max_retries + 1``）。
            timeout_seconds: 单次调用超时；``None`` 表示不设超时。
        """
        self._provider = provider
        self._prompts = prompts
        self._budget = budget
        self._model = model
        self._max_retries = max(0, max_retries)
        self._timeout = timeout_seconds

    @property
    def budget(self) -> BudgetTracker:
        """本网关使用的预算记账器。"""
        return self._budget

    @property
    def provider_name(self) -> str:
        """Provider 名称。"""
        return self._provider.name

    # ------------------------------------------------------------------
    # 结构化调用
    # ------------------------------------------------------------------

    async def structured[T: BaseModel](
        self,
        *,
        task_name: str,
        payload: BaseModel | dict[str, Any],
        response_model: type[T],
        context: InvocationContext,
    ) -> StructuredCall[T]:
        """渲染、调用、校验，并返回结果与调用记录。

        Raises:
            ConfigurationError: 任务名未注册。
            BudgetExhaustedError: 预算不足（**在任何调用发生之前**抛出）。
            StructuredOutputError: 重试耗尽后仍未通过 Schema 校验。
            ProviderTimeoutError: 重试耗尽后仍然超时。
            ProviderError: 其他不可重试的 Provider 错误。
        """
        contract = self._prompts.get(task_name)
        last_error: ProviderError | None = None

        for attempt in range(self._max_retries + 1):
            self._budget.spend_model_call(task_name=task_name)
            messages = self._prompts.render(task_name, payload)
            model_config = self._prompts.model_config(
                task_name, model=self._model, timeout_seconds=self._timeout
            )
            started_at = datetime.now(UTC)
            monotonic_start = time.perf_counter()

            try:
                value = await self._await_with_timeout(
                    self._provider.generate_structured(
                        task_name=task_name,
                        messages=messages,
                        response_model=response_model,
                        model_config=model_config,
                        invocation_context=context.model_copy(update={"attempt": attempt}),
                    ),
                    task_name=task_name,
                )
            except ProviderError as exc:
                last_error = exc
                if not exc.retryable:
                    raise
                continue

            return StructuredCall(
                value=value,
                invocation=self._build_info(
                    task_name=task_name,
                    prompt_version=contract.version,
                    started_at=started_at,
                    monotonic_start=monotonic_start,
                    retry_count=attempt,
                    result_status="success",
                    response_hash=_hash_text(value.model_dump_json()),
                ),
            )

        assert last_error is not None  # 循环至少执行一次
        raise last_error

    # ------------------------------------------------------------------
    # 文本调用
    # ------------------------------------------------------------------

    async def text(
        self,
        *,
        task_name: str,
        payload: BaseModel | dict[str, Any],
        context: InvocationContext,
    ) -> TextCall:
        """渲染、调用，并返回文本与调用记录。

        Raises:
            ConfigurationError: 任务名未注册。
            BudgetExhaustedError: 预算不足。
            ProviderError: 重试耗尽后的 Provider 错误。
        """
        contract = self._prompts.get(task_name)
        last_error: ProviderError | None = None

        for attempt in range(self._max_retries + 1):
            self._budget.spend_model_call(task_name=task_name)
            messages = self._prompts.render(task_name, payload)
            model_config = self._prompts.model_config(
                task_name, model=self._model, timeout_seconds=self._timeout
            )
            started_at = datetime.now(UTC)
            monotonic_start = time.perf_counter()

            try:
                value = await self._await_with_timeout(
                    self._provider.generate_text(
                        task_name=task_name,
                        messages=messages,
                        model_config=model_config,
                        invocation_context=context.model_copy(update={"attempt": attempt}),
                    ),
                    task_name=task_name,
                )
            except ProviderError as exc:
                last_error = exc
                if not exc.retryable:
                    raise
                continue

            return TextCall(
                text=value,
                invocation=self._build_info(
                    task_name=task_name,
                    prompt_version=contract.version,
                    started_at=started_at,
                    monotonic_start=monotonic_start,
                    retry_count=attempt,
                    result_status="success",
                    response_hash=_hash_text(value),
                ),
            )

        assert last_error is not None  # 循环至少执行一次
        raise last_error

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _await_with_timeout[T](
        self,
        coroutine: Coroutine[Any, Any, T],
        *,
        task_name: str,
    ) -> T:
        """给 Provider 调用套上超时。

        超时**不破坏已有状态**（任务书 §19.4）：已写入的事件保持有效，
        回合进入可查询的失败路径。
        """
        if self._timeout is None:
            return await coroutine
        try:
            return await asyncio.wait_for(coroutine, timeout=self._timeout)
        except TimeoutError as exc:
            msg = f"模型调用超时（任务 {task_name}，{self._timeout} 秒）"
            raise ProviderTimeoutError(
                msg,
                provider=self.provider_name,
                model=self._model,
                task_name=task_name,
            ) from exc

    def _build_info(
        self,
        *,
        task_name: str,
        prompt_version: str,
        started_at: datetime,
        monotonic_start: float,
        retry_count: int,
        result_status: str,
        response_hash: str | None,
    ) -> ModelInvocationInfo:
        """构造调用审计记录。"""
        return ModelInvocationInfo(
            invocation_id=uuid4(),
            provider=self.provider_name,
            model=self._model,
            task_name=task_name,
            prompt_version=prompt_version,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            latency_ms=max(0, int((time.perf_counter() - monotonic_start) * 1000)),
            retry_count=retry_count,
            result_status=result_status,
            response_hash=response_hash,
        )


def _hash_text(text: str) -> str:
    """返回响应内容的 SHA-256（只存哈希，不存内容）。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
