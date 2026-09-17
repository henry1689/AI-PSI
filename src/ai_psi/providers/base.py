"""LLM Provider 抽象（任务书 §8.1，ADR-0003）。

🔴 **Provider 是可替换组件。** 领域层与认知层都不 import 具体实现，
只依赖 :class:`LLMProvider` 协议。阶段 4 加入 Anthropic /
OpenAI-compatible 实现时，业务代码一行都不用改。

🔴 **Mock 与真实 Provider 必须看到同样的输入。**
因此 :class:`LLMMessage` 只承载 ``role`` 与 ``content``——
**没有任何"只给 Mock 用"的旁路字段**。Mock 解析的是**渲染后的提示词文本**，
与真实模型看到的完全一致。

这条约束看似多余，实则是整条 Mock 路线的可信度来源：若 Mock 能拿到
真实模型拿不到的结构化输入，那么"阶段 3 场景全绿"就只证明了"Mock 与
流水线自洽"，而不是"提示词里真的含有所需信息"（risks.md R13）。
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ai_psi.providers.response import ProviderResponse

__all__ = [
    "InvocationContext",
    "LLMMessage",
    "LLMProvider",
    "ModelConfig",
]


class LLMMessage(BaseModel):
    """一条模型消息。

    🔴 **刻意只有 ``role`` 与 ``content`` 两个字段。**
    任何额外的结构化字段都会成为"Mock 看得见、真实模型看不见"的旁路，
    从而让 Mock 测试失去意义。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1)


class ModelConfig(BaseModel):
    """单次调用的模型参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str = Field(min_length=1)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=2048, ge=1)
    timeout_seconds: float | None = Field(default=None, gt=0)


class InvocationContext(BaseModel):
    """调用上下文，用于审计与关联。

    🔴 **不包含任何提示词内容或用户正文**——它只承载标识符，
    因此可以安全地写入日志与模型调用记录（任务书 §8.2）。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cognitive_round_id: UUID | None = None
    conversation_id: UUID | None = None
    user_id: UUID | None = None
    correlation_id: UUID | None = None
    attempt: int = Field(default=0, ge=0, description="重试序号。**重试必须换新的 invocation_id**")


@runtime_checkable
class LLMProvider(Protocol):
    """模型调用协议（任务书 §8.1 的逐字实现）。"""

    @property
    def name(self) -> str:
        """Provider 名称，写入 :class:`~ai_psi.domain.events.ModelInvocationInfo`。"""
        ...

    async def generate_structured[T: BaseModel](
        self,
        *,
        task_name: str,
        messages: list[LLMMessage],
        response_model: type[T],
        model_config: ModelConfig,
        invocation_context: InvocationContext,
    ) -> ProviderResponse[T]:
        """调用模型并返回**经过 Schema 校验**的结构化对象。

        🔴 校验失败必须抛 :class:`~ai_psi.domain.exceptions.StructuredOutputError`，
        而不是返回部分构造的对象——不变量 16 要求"模型格式错误不得导致
        部分非法状态写入"。

        ⚠️ **返回值是 :class:`~ai_psi.providers.response.ProviderResponse`
        而不是裸的 ``T``。** 任务书 §8.1 的签名写的是 ``-> T``；
        阶段 4 为了把 token 用量随返回值一起带回来而改了它
        （只有 Provider 知道用量，用 "last_usage" 之类的状态会在并发下串号）。
        这是有意偏离，登记在 ADR-0016。

        Raises:
            StructuredOutputError: 返回内容不符合 ``response_model``。
            ProviderTimeoutError: 调用超时。
            ProviderUnavailableError: Provider 不可用（含熔断开启）。
            ProviderRateLimitError: 触发速率限制。
        """
        ...

    async def generate_text(
        self,
        *,
        task_name: str,
        messages: list[LLMMessage],
        model_config: ModelConfig,
        invocation_context: InvocationContext,
    ) -> ProviderResponse[str]:
        """调用模型并返回纯文本（同样带用量与结束原因）。"""
        ...
