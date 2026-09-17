"""Provider 级熔断器（任务书 §8.1、§13.2）。

🔴 **熔断的用途不是"少发请求"，而是"别把故障放大"。**

一个已经不可用的 Provider 每多收一次请求，就要多等一次超时。
在认知流水线里，一次回合有 4–13 次模型调用，
"每次都要等 60 秒超时"会把一个本来只影响一个回合的故障
变成一次用户可感知的长时间卡死。

熔断器在这里只做一件事：**连续失败到阈值后，在冷却期内直接拒绝调用**，
冷却期结束后放**一个**探针请求过去（半开状态），成功则恢复，失败则继续冷却。

⚠️ 本模块是**纯状态机**：没有 IO、没有 asyncio、时钟可注入。
这既让它能被穷尽地测试，也让它与"哪个 Provider"解耦——
阶段 5 换 Provider 时它一行都不用改。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

__all__ = ["CircuitBreaker", "CircuitState"]

#: 连续失败多少次后打开熔断。
DEFAULT_FAILURE_THRESHOLD: Final[int] = 5

#: 打开后冷却多少秒再放探针。
DEFAULT_RECOVERY_SECONDS: Final[float] = 30.0


class CircuitState(StrEnum):
    """熔断器状态。

    注意 ``OPEN`` **不是**回合状态（ADR-0012：``DEGRADED`` 属于系统健康状态）。
    """

    CLOSED = "closed"
    """正常放行。"""

    OPEN = "open"
    """直接拒绝调用，不再等待超时。"""

    HALF_OPEN = "half_open"
    """冷却结束，放**一个**探针请求过去。"""


@dataclass
class CircuitBreaker:
    """连续失败计数式熔断器。

    典型用法::

        if not breaker.allow():
            raise ProviderUnavailableError("熔断开启")
        try:
            result = await provider.call(...)
        except ProviderError:
            breaker.record_failure()
            raise
        breaker.record_success()
    """

    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    recovery_seconds: float = DEFAULT_RECOVERY_SECONDS
    clock: Callable[[], float] = time.monotonic

    _state: CircuitState = field(default=CircuitState.CLOSED, init=False)
    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _probe_in_flight: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        """校验参数。

        Raises:
            ValueError: 阈值小于 1，或冷却时间非正。
        """
        if self.failure_threshold < 1:
            msg = "熔断阈值至少为 1：阈值为 0 意味着一次失败都不容忍，会立刻熔断"
            raise ValueError(msg)
        if self.recovery_seconds <= 0:
            msg = "冷却时间必须为正"
            raise ValueError(msg)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        """当前状态（会顺带处理"冷却是否结束"的转移）。"""
        if self._state is CircuitState.OPEN and self._cooldown_elapsed():
            # 冷却结束后进入半开：状态查询本身不产生副作用，
            # 因此这里只**汇报**半开，真正的放行判定在 allow() 里做。
            return CircuitState.HALF_OPEN
        return self._state

    @property
    def consecutive_failures(self) -> int:
        """当前连续失败次数。"""
        return self._consecutive_failures

    def allow(self) -> bool:
        """是否放行本次调用。

        Returns:
            ``CLOSED`` 一律放行；``HALF_OPEN`` 只放行**一个**探针；
            ``OPEN`` 一律拒绝。
        """
        if self._state is CircuitState.CLOSED:
            return True

        if self._state is CircuitState.OPEN and self._cooldown_elapsed():
            self._state = CircuitState.HALF_OPEN
            self._probe_in_flight = False

        if self._state is CircuitState.HALF_OPEN and not self._probe_in_flight:
            self._probe_in_flight = True
            return True

        return False

    # ------------------------------------------------------------------
    # 记录
    # ------------------------------------------------------------------

    def record_success(self) -> None:
        """记录一次成功。任何状态下都重置为关闭。"""
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        """记录一次失败。

        半开状态下的失败**立即重新打开**（并把冷却重新计时）——
        探针失败说明故障仍在，没有必要再凑满阈值。
        """
        self._consecutive_failures += 1
        if self._state is CircuitState.HALF_OPEN or (
            self._consecutive_failures >= self.failure_threshold
        ):
            self._state = CircuitState.OPEN
            self._opened_at = self.clock()
        self._probe_in_flight = False

    def reset(self) -> None:
        """手动重置（供运维与测试使用）。"""
        self.record_success()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _cooldown_elapsed(self) -> bool:
        """冷却期是否已经过去。"""
        if self._opened_at is None:  # pragma: no cover - 不变量：OPEN 必然有开启时间
            return True
        return (self.clock() - self._opened_at) >= self.recovery_seconds
