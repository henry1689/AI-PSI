"""HTTP 错误到领域异常的映射（任务书 §8.1"速率限制错误映射"）。

🔴 **调用方不该知道 HTTP 状态码。**

认知模块按领域异常写 ``except``：可重试的
:class:`~ai_psi.domain.exceptions.ProviderUnavailableError`、
需要退避的 :class:`~ai_psi.domain.exceptions.ProviderRateLimitError`、
不可重试的 :class:`~ai_psi.domain.exceptions.ProviderError`。
把 ``httpx.HTTPStatusError`` 透给上层，等于让每一层都要懂 HTTP，
而且要懂"429 到底算不算可重试"这种属于 Provider 的知识。

⚠️ **错误信息里不放响应体。** 供应商的错误响应可能回显请求内容
（包括用户正文）。只保留状态码与**头部**里的重试提示。
"""

from __future__ import annotations

from typing import Final

import httpx

from ai_psi.domain.exceptions import (
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

__all__ = ["map_http_error", "parse_retry_after"]

#: 服务端错误的下界（含）。
_SERVER_ERROR: Final[int] = 500

#: 需要退避的状态码：请求本身没问题，只是时机不对。
_BACKOFF_CODES: Final[frozenset[int]] = frozenset({408, 409, 425})


def parse_retry_after(headers: httpx.Headers) -> float | None:
    """从响应头解析建议的重试等待秒数。

    只认 ``Retry-After`` 的**秒数**形式；HTTP-date 形式返回 ``None``
    （解析日期需要可信的时钟，而"再等多久"用秒数已经足够）。

    Args:
        headers: 响应头。

    Returns:
        建议等待秒数；没有或不可解析时为 ``None``。
    """
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def map_http_error(
    exc: httpx.HTTPError,
    *,
    provider: str,
    model: str,
    task_name: str,
) -> ProviderError:
    """把 httpx 异常映射为领域 Provider 异常。

    Args:
        exc: httpx 抛出的异常。
        provider: Provider 名称（写入错误上下文）。
        model: 模型标识。
        task_name: 当前任务名。

    Returns:
        对应的领域异常（调用方负责 ``raise``）。
    """
    if isinstance(exc, httpx.TimeoutException):
        return ProviderTimeoutError(
            "模型调用超时", provider=provider, model=model, task_name=task_name
        )

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        retry_after = parse_retry_after(exc.response.headers)

        if status == 429:
            return ProviderRateLimitError(
                "触发供应商速率限制",
                retry_after_seconds=retry_after,
                provider=provider,
                model=model,
                task_name=task_name,
            )
        if status >= _SERVER_ERROR or status in _BACKOFF_CODES:
            return ProviderUnavailableError(
                f"供应商暂时不可用（{status}）",
                provider=provider,
                model=model,
                task_name=task_name,
            )
        # 4xx 中剩下的都是"这个请求本身有问题"：重试多少次都一样。
        return ProviderError(
            f"供应商拒绝了请求（{status}）",
            provider=provider,
            model=model,
            task_name=task_name,
            retryable=False,
            context={"status": status},
        )

    # 连接错误、DNS 失败、协议异常……都当作暂时不可用。
    return ProviderUnavailableError(
        f"无法连接供应商（{type(exc).__name__}）",
        provider=provider,
        model=model,
        task_name=task_name,
    )
