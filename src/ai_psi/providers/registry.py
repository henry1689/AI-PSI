"""Provider 工厂。

🔴 **上层通过本模块拿到 Provider，而不是直接 import 具体实现。**

⚠️ **缺少 API Key 时"明确失败"，不静默回落到 Mock。**
任务书 §18 允许"自动使用 Mock **或**明确失败"二选一，本项目选后者：

静默回落会让一次配置失误伪装成"系统跑得很好"——
用户以为在跟真实模型对话，实际拿到的是规则引擎拼出来的结构化占位输出，
而且**没有任何地方会告诉他**。Mock 仍然可以通过显式配置
（``AI_PSI_LLM_PROVIDER=mock``）使用，只是它必须是一个**被选择**的结果。
（决策记录：ADR-0016）

⚠️ Anthropic Provider **本阶段未实现**（ADR-0016）：用户指定用 DeepSeek，
而一个既没有测试、又无法实际跑通的实现只是"看起来有能力"的空壳。
"""

from __future__ import annotations

from typing import Final, cast

import httpx
from pydantic import SecretStr

from ai_psi.config import Settings
from ai_psi.domain.exceptions import ConfigurationError
from ai_psi.providers.base import LLMProvider
from ai_psi.providers.embeddings import (
    AVAILABLE_EMBEDDING_PROVIDERS,
    EmbeddingProvider,
    LocalHashingEmbedding,
    OpenAICompatibleEmbedding,
)
from ai_psi.providers.mock import MockProvider
from ai_psi.providers.openai_compatible import (
    DEFAULT_REASONING_HEADROOM,
    OpenAICompatibleProvider,
)
from ai_psi.providers.resilience import ResilientProvider
from ai_psi.reliability.circuit_breaker import CircuitBreaker

__all__ = [
    "AVAILABLE_EMBEDDING_PROVIDERS",
    "AVAILABLE_PROVIDERS",
    "DEFAULT_MODELS",
    "NOT_IMPLEMENTED_PROVIDERS",
    "build_embedding_provider",
    "build_provider_with_client",
    "resolve_model",
]

#: 当前可用的 Provider 名称。
AVAILABLE_PROVIDERS: Final[frozenset[str]] = frozenset({"mock", "deepseek", "openai_compatible"})

#: 任务书要求但**本阶段未实现**的 Provider（ADR-0016）。
NOT_IMPLEMENTED_PROVIDERS: Final[frozenset[str]] = frozenset({"anthropic"})

#: 各 Provider 的默认模型。
DEFAULT_MODELS: Final[dict[str, str]] = {
    "mock": "mock-model-v1",
    # DeepSeek 的 v4 flash 档。实测该 id 与 ``deepseek-flash`` 等价。
    "deepseek": "deepseek-v4-flash",
    "openai_compatible": "gpt-4o-mini",
}

#: 各 Provider 默认的 base_url。
DEFAULT_BASE_URLS: Final[dict[str, str]] = {
    "deepseek": "https://api.deepseek.com/v1",
    "openai_compatible": "https://api.openai.com/v1",
}


def resolve_model(settings: Settings) -> str:
    """返回当前 Provider 实际使用的模型标识。

    Args:
        settings: 运行时配置。

    Returns:
        模型标识：显式配置优先，否则取该 Provider 的默认值。
    """
    name = settings.llm_provider.strip().lower()
    return settings.llm_model or DEFAULT_MODELS.get(name, "unknown-model")


def build_provider_with_client(settings: Settings, client: httpx.AsyncClient) -> LLMProvider:
    """按配置构造 Provider，并使用给定的 HTTP 客户端。

    Args:
        settings: 运行时配置。
        client: 共享的 HTTP 客户端。

    Returns:
        套了熔断的 Provider。

    Raises:
        ConfigurationError: 配置缺失或 Provider 未实现。
    """
    name = settings.llm_provider.strip().lower()
    breaker = CircuitBreaker(
        failure_threshold=settings.llm_circuit_failure_threshold,
        recovery_seconds=settings.llm_circuit_recovery_seconds,
    )

    if name in NOT_IMPLEMENTED_PROVIDERS:
        msg = (
            f"Provider {name!r} 尚未实现（ADR-0016：阶段 4 按用户指定改用 DeepSeek）。"
            f"当前可用：{sorted(AVAILABLE_PROVIDERS)}"
        )
        raise ConfigurationError(msg)

    if name not in AVAILABLE_PROVIDERS:
        msg = f"未知的 Provider：{name!r}。当前可用：{sorted(AVAILABLE_PROVIDERS)}"
        raise ConfigurationError(msg)

    inner: LLMProvider
    if name == "mock":
        inner = MockProvider(model=resolve_model(settings))
    else:
        inner = OpenAICompatibleProvider(
            client=client,
            api_key=_require_api_key(settings, name),
            base_url=_resolve_base_url(settings, name),
            name=name,
            use_json_mode=settings.llm_json_mode,
            reasoning_headroom_tokens=_headroom(settings),
        )

    return ResilientProvider(inner, breaker)


def _headroom(settings: Settings) -> int:
    """解析推理输出预留。

    🔴 配置为 ``None`` 时用 Provider 的默认值。**不要写成
    ``settings.x or DEFAULT``**——``0`` 是合法配置（关掉预留），
    用 ``or`` 会把它当成"没配"。

    Args:
        settings: 运行时配置。

    Returns:
        预留 token 数。
    """
    configured = settings.llm_reasoning_headroom_tokens
    if configured is None:
        return DEFAULT_REASONING_HEADROOM
    return configured


def _require_api_key(settings: Settings, name: str) -> SecretStr:
    """取出该 Provider 需要的密钥。

    Args:
        settings: 运行时配置。
        name: Provider 名称。

    Returns:
        密钥。

    Raises:
        ConfigurationError: 未配置密钥。
    """
    key: SecretStr | None = None
    if name == "deepseek":
        key = settings.deepseek_api_key
    elif name == "openai_compatible":
        key = settings.openai_api_key

    if key is None:
        env_name = f"AI_PSI_{name.upper()}_API_KEY"
        msg = (
            f"Provider {name!r} 需要 API Key，但未配置 {env_name}。"
            "本项目**不会**静默回落到 Mock——那会让你以为在跟真实模型对话。"
            "若确实想用规则引擎，请显式设置 AI_PSI_LLM_PROVIDER=mock"
        )
        raise ConfigurationError(msg)
    return key


def _resolve_base_url(settings: Settings, name: str) -> str:
    """解析 base_url。

    Args:
        settings: 运行时配置。
        name: Provider 名称。

    Returns:
        基础地址。
    """
    if name == "deepseek" and settings.deepseek_base_url:
        return settings.deepseek_base_url
    if name == "openai_compatible" and settings.openai_base_url:
        return settings.openai_base_url
    return DEFAULT_BASE_URLS[name]


def build_embedding_provider(
    settings: Settings,
    client: httpx.AsyncClient,
) -> EmbeddingProvider:
    """按配置构造向量 Provider（阶段 5）。

    🔴 **不套熔断。** 熔断保护的是"供应商不稳定时不要反复打"，
    而向量 Provider 的失败会**直接让记忆写入失败**——
    熔断打开只会让它更快失败，不会让任何一次写入更可能成功。
    对写入路径而言，"明确失败、用户重试"本来就是想要的语义。

    Args:
        settings: 运行时配置。
        client: 共享的 HTTP 客户端。

    Returns:
        向量 Provider。

    Raises:
        ConfigurationError: 外部 Provider 的必要配置缺失。
    """
    name = settings.embedding_provider.strip().lower()
    # ⚠️ 这里**不再**重复判断名称是否合法：``Settings`` 的校验器已经用
    # 同一份 ``AVAILABLE_EMBEDDING_PROVIDERS`` 拦过了。两处各写一份规则，
    # 除了会各自漂移，还会制造一段**永远执行不到**的分支——
    # 而"看起来在防、其实不会触发"的检查比没有检查更让人放心得过头。
    if name == "local":
        return LocalHashingEmbedding(dimension=settings.embedding_dimension)

    model = settings.embedding_model
    base_url = settings.embedding_base_url
    api_key = settings.embedding_api_key
    missing = [
        env
        for env, value in (
            ("AI_PSI_EMBEDDING_MODEL", model),
            ("AI_PSI_EMBEDDING_BASE_URL", base_url),
            ("AI_PSI_EMBEDDING_API_KEY", api_key),
        )
        if not value
    ]
    if missing:
        msg = (
            f"embedding_provider={name!r} 需要配置 {missing}。"
            "本项目**不会**静默回落到本地向量——那会让检索质量悄悄变成另一个样子，"
            "而「改错了看不出来」正是记忆系统最不能接受的失效方式"
        )
        raise ConfigurationError(msg)

    return OpenAICompatibleEmbedding(
        client=client,
        api_key=cast(SecretStr, api_key),
        base_url=cast(str, base_url),
        model=cast(str, model),
        dimension=settings.embedding_dimension,
    )


def provider_health(provider: LLMProvider) -> tuple[str, str]:
    """返回 Provider 的运行健康状况。

    Args:
        provider: Provider 实例。

    Returns:
        ``(状态, 说明)``；状态取 ``ok`` 或 ``degraded``（任务书 §13.2）。
    """
    if isinstance(provider, ResilientProvider):
        state = provider.state.value
        if state != "closed":
            return "degraded", f"{provider.name}: 熔断状态 {state}"
        return "ok", provider.name
    return "ok", getattr(provider, "name", "unknown")
