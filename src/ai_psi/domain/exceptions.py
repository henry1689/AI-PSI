"""AI-PSI 结构化异常层次。

任务书开发原则第 15 条要求"清晰异常类型"。本模块把异常组织为
一棵有语义的小树，使调用方可以按**类别**捕获，而不是按字符串匹配消息。

设计约定：

* 每个异常携带机器可读的 ``code``，供 API 层映射为错误响应；
* ``context`` 携带结构化上下文，但**绝不放入用户正文或密钥**；
* :meth:`AIPsiError.to_dict` 输出**不含堆栈**，可直接返回给普通 API 客户端
  （任务书 §17.1："错误堆栈不得返回给普通 API 客户端"）。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AIPsiError",
    "ApplicationError",
    "BudgetExhaustedError",
    "ConfigurationError",
    "ConflictError",
    "ConstitutionViolationError",
    "DomainError",
    "IllegalStateTransitionError",
    "InvariantViolationError",
    "NotFoundError",
    "OptimisticLockError",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderTimeoutError",
    "ProviderUnavailableError",
    "ScopeViolationError",
    "StructuredOutputError",
]


class AIPsiError(Exception):
    """所有 AI-PSI 异常的基类。

    Attributes:
        message: 人类可读的说明（不得包含用户正文或密钥）。
        code: 机器可读的错误码，默认取类的 ``default_code``。
        context: 结构化上下文，用于日志与诊断。
    """

    default_code: str = "ai_psi_error"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code if code is not None else self.default_code
        self.context: dict[str, Any] = dict(context) if context else {}

    def to_dict(self) -> dict[str, Any]:
        """转换为可安全返回给 API 客户端的字典（**不含堆栈**）。"""
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.context:
            payload["context"] = self.context
        return payload

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# ---------------------------------------------------------------------------
# 领域错误
# ---------------------------------------------------------------------------


class DomainError(AIPsiError):
    """领域规则被违反。调用方通常是领域逻辑本身，属于编程错误或数据错误。"""

    default_code = "domain_error"


class InvariantViolationError(DomainError):
    """认知不变量被违反（任务书 §14）。

    这是最严重的一类领域错误：它意味着系统可能会说出或写入
    与其自身规则相矛盾的内容。
    """

    default_code = "invariant_violation"

    def __init__(
        self,
        message: str,
        *,
        invariant_id: str | None = None,
        code: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, context=context)
        self.invariant_id = invariant_id
        if invariant_id is not None:
            self.context.setdefault("invariant_id", invariant_id)


class ConstitutionViolationError(InvariantViolationError):
    """认知宪法被违反（ADR-0011）。

    与普通不变量违反的区别：宪法是**不可通过配置放宽**的边界，
    因此本异常不会被任何降级策略吞掉。
    """

    default_code = "constitution_violation"


class IllegalStateTransitionError(DomainError):
    """非法的认知回合状态转移（不变量 17，`docs/state_machine.md` §3）。"""

    default_code = "illegal_state_transition"

    def __init__(
        self,
        message: str,
        *,
        from_state: str,
        to_state: str,
        code: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, context=context)
        self.from_state = from_state
        self.to_state = to_state
        self.context.setdefault("from_state", from_state)
        self.context.setdefault("to_state", to_state)


class OptimisticLockError(DomainError):
    """乐观锁冲突（ADR-0002）。

    🔴 这是**正常业务路径**，不是异常情况——并发写入时必然发生。
    API 层应将其映射为可重试的明确错误，而不是 500。
    """

    default_code = "optimistic_lock_conflict"

    def __init__(
        self,
        message: str,
        *,
        entity_type: str,
        entity_id: str,
        expected_version: int,
        actual_version: int,
        code: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, context=context)
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        self.context.setdefault("entity_type", entity_type)
        self.context.setdefault("entity_id", entity_id)
        self.context.setdefault("expected_version", expected_version)
        self.context.setdefault("actual_version", actual_version)


class BudgetExhaustedError(DomainError):
    """认知预算耗尽（任务书 §13.1，ADR-0008）。

    注意：**元认知循环超限不抛本异常**——那会降级为强制 STOP 进入
    ``SYNTHESIZING``，而不是让整个回合失败（ADR-0008）。
    """

    default_code = "budget_exhausted"

    def __init__(
        self,
        message: str,
        *,
        budget_name: str,
        limit: float,
        used: float,
        code: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, context=context)
        self.budget_name = budget_name
        self.limit = limit
        self.used = used
        self.context.setdefault("budget_name", budget_name)
        self.context.setdefault("limit", limit)
        self.context.setdefault("used", used)


class ScopeViolationError(DomainError):
    """跨用户作用域访问（不变量 14，`docs/security.md` §4.1）。

    🔴 这**不是**"过滤条件没加"这类小问题——它意味着隐私事故。
    一旦抛出，应当被视为缺陷而非可恢复错误，**不得**降级后继续。
    """

    default_code = "scope_violation"

    def __init__(
        self,
        message: str,
        *,
        requested_user_id: str,
        resource_user_id: str,
        code: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, context=context)
        self.requested_user_id = requested_user_id
        self.resource_user_id = resource_user_id
        # 只记录 id，不记录任何用户内容
        self.context.setdefault("requested_user_id", requested_user_id)
        self.context.setdefault("resource_user_id", resource_user_id)


# ---------------------------------------------------------------------------
# 应用层错误
# ---------------------------------------------------------------------------


class ApplicationError(AIPsiError):
    """应用服务层的错误。"""

    default_code = "application_error"


class NotFoundError(ApplicationError):
    """请求的实体不存在。

    注意：在用户作用域下，**他人的资源应当表现为不存在**（404），
    而不是"无权限"（403）——后者会泄漏资源的存在性。
    """

    default_code = "not_found"


class ConflictError(ApplicationError):
    """幂等键冲突或其他业务冲突（任务书 §13.4）。"""

    default_code = "conflict"


# ---------------------------------------------------------------------------
# Provider 错误
# ---------------------------------------------------------------------------


class ProviderError(AIPsiError):
    """LLM Provider 相关错误的基类。"""

    default_code = "provider_error"

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        task_name: str | None = None,
        retryable: bool = False,
        code: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, context=context)
        self.provider = provider
        self.model = model
        self.task_name = task_name
        self.retryable = retryable
        if provider is not None:
            self.context.setdefault("provider", provider)
        if model is not None:
            self.context.setdefault("model", model)
        if task_name is not None:
            self.context.setdefault("task_name", task_name)


class ProviderTimeoutError(ProviderError):
    """Provider 调用超时。

    🔴 超时必须**不破坏已有状态**（任务书 §19.4）：
    已写入的事件保持有效，回合进入可查询的失败路径。
    """

    default_code = "provider_timeout"

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)


class ProviderRateLimitError(ProviderError):
    """Provider 速率限制。

    Provider 层必须把供应商各自的限流错误**映射**到本类型
    （任务书 §8.1："速率限制错误映射"）。
    """

    default_code = "provider_rate_limit"

    def __init__(
        self, message: str, *, retry_after_seconds: float | None = None, **kwargs: Any
    ) -> None:
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)
        self.retry_after_seconds = retry_after_seconds
        if retry_after_seconds is not None:
            self.context.setdefault("retry_after_seconds", retry_after_seconds)


class ProviderUnavailableError(ProviderError):
    """Provider 不可用（网络故障、服务中断、熔断开启）。

    连续失败将触发熔断，系统进入 ``DEGRADED`` 健康状态
    （注意：``DEGRADED`` 是系统状态，**不是**回合状态，见 ADR-0012）。
    """

    default_code = "provider_unavailable"

    def __init__(self, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)


class StructuredOutputError(ProviderError):
    """模型返回的内容无法解析为期望的结构化对象。

    🔴 不变量 16：模型格式错误**不得导致部分非法状态写入**。
    本异常抛出时，调用方必须确保没有任何部分对象被持久化。
    """

    default_code = "structured_output_invalid"

    def __init__(
        self,
        message: str,
        *,
        validation_errors: tuple[str, ...] = (),
        raw_response_hash: str | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)
        self.validation_errors = validation_errors
        self.raw_response_hash = raw_response_hash
        if validation_errors:
            # 只记录校验错误的**字段路径与原因**，不记录模型原始输出
            self.context.setdefault("validation_errors", list(validation_errors))
        if raw_response_hash is not None:
            self.context.setdefault("raw_response_hash", raw_response_hash)


# ---------------------------------------------------------------------------
# 配置错误
# ---------------------------------------------------------------------------


class ConfigurationError(AIPsiError):
    """配置缺失或非法（如生产环境缺少必需的环境变量）。"""

    default_code = "configuration_error"
