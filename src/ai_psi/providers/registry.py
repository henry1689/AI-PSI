"""Provider 工厂。

🔴 **上层通过本模块拿到 Provider，而不是直接 import 具体实现。**
阶段 4 增加 ``anthropic`` / ``openai_compatible`` 时，
只需要在这里加一个分支，认知层与领域层一行都不用改（ADR-0003）。

未实现的 Provider 名称**显式报错**，不静默回落到 Mock——
静默回落会让"配了真实模型却一直跑 Mock"变成一类极难发现的问题。
"""

from __future__ import annotations

from typing import Final

from ai_psi.config import Settings
from ai_psi.domain.exceptions import ConfigurationError
from ai_psi.providers.base import LLMProvider
from ai_psi.providers.mock import MockProvider

__all__ = ["AVAILABLE_PROVIDERS", "FUTURE_PROVIDERS", "build_provider"]

#: 当前可用的 Provider 名称。
AVAILABLE_PROVIDERS: Final[frozenset[str]] = frozenset({"mock"})

#: 计划在后续阶段提供的 Provider 名称。配置成它们会得到明确提示，
#: 而不是"未知配置项"这种误导性的报错。
FUTURE_PROVIDERS: Final[frozenset[str]] = frozenset({"anthropic", "openai_compatible"})

_DEFAULT_MOCK_MODEL: Final[str] = "mock-model-v1"


def build_provider(settings: Settings) -> LLMProvider:
    """按配置构造 Provider。

    Args:
        settings: 运行时配置。

    Returns:
        可用的 :class:`~ai_psi.providers.base.LLMProvider`。

    Raises:
        ConfigurationError: 配置了尚未实现或未知的 Provider。
    """
    name = settings.llm_provider.strip().lower()

    if name in FUTURE_PROVIDERS:
        msg = (
            f"Provider {name!r} 将在阶段 4 提供。"
            f"当前可用：{sorted(AVAILABLE_PROVIDERS)}（AI_PSI_LLM_PROVIDER）"
        )
        raise ConfigurationError(msg)

    if name not in AVAILABLE_PROVIDERS:
        msg = (
            f"未知的 Provider：{name!r}。"
            f"当前可用：{sorted(AVAILABLE_PROVIDERS)}，"
            f"计划支持：{sorted(FUTURE_PROVIDERS)}"
        )
        raise ConfigurationError(msg)

    return MockProvider(model=settings.llm_model or _DEFAULT_MOCK_MODEL)
