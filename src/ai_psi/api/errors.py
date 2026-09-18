"""API 错误处理（任务书 §17.1）。

🔴 **错误堆栈不得返回给普通 API 客户端。**

实现方式不是"记得别把 traceback 塞进响应"，而是：
**:class:`~ai_psi.domain.exceptions.AIPsiError` 的 ``to_dict()`` 本来就不含堆栈**，
本模块只负责把它映射到一个合适的状态码。未预期的异常另有兜底处理器，
它返回**完全固定**的消息，真实原因只写进服务端日志。

审计事件与错误响应共用同一套 ``code``：客户端看到的错误码
与服务端事件流里的错误码是同一个，排查时不需要做映射。

**不做的事**：不把内部异常文本原样透给客户端。领域异常的 message
是写给开发者看的（可能包含 id、字段名），但它是**受控文本**——
构造异常时就不允许写入用户正文或密钥（见模块文档），因此可以安全外发。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ai_psi.domain.exceptions import (
    AIPsiError,
    ApplicationError,
    BudgetExhaustedError,
    ConflictError,
    ConstitutionViolationError,
    DomainError,
    IllegalStateTransitionError,
    InvalidRequestError,
    InvariantViolationError,
    NotFoundError,
    OptimisticLockError,
    ProviderError,
    ScopeViolationError,
)
from ai_psi.infrastructure.security_audit import record_rejected_request

__all__ = [
    "HTTP_STATUS_BY_EXCEPTION",
    "INVALID_REQUEST_CODE",
    "register_error_handlers",
]

#: 请求体/参数未通过 Schema 校验时的错误码。
#:
#: 🔴 **与 :class:`~ai_psi.domain.exceptions.InvalidRequestError` 的
#: ``code`` 保持同一个值。**
#:
#: 两者都表示"这次请求的输入不合法"，区别只在**哪一层发现的**：
#: 一个是 FastAPI/pydantic 在校验 Schema 时，一个是服务层在语义上。
#: 给它们两个不同的码，会让客户端必须知道"服务端把这类校验放在哪一层"
#: 才能正确处理错误——而那是一个实现细节，不该泄漏到契约里。
INVALID_REQUEST_CODE = InvalidRequestError.default_code

#: 异常类型到 HTTP 状态码的映射。
#:
#: 顺序有意义：``isinstance`` 从上到下匹配，**子类必须排在父类之前**。
HTTP_STATUS_BY_EXCEPTION: Mapping[type[AIPsiError], int] = {
    NotFoundError: 404,
    # 🔴 作用域违规对客户端一律表现为 404：
    # 返回 403 等于确认"这条记录存在"，那本身就是信息泄漏
    # （见 NotFoundError 的文档）。
    ScopeViolationError: 404,
    # 乐观锁冲突与非法转移都是**可恢复的业务路径**，不是 500（ADR-0002）。
    OptimisticLockError: 409,
    IllegalStateTransitionError: 409,
    ConflictError: 409,
    # 🔴 语义无效的输入是**调用方的问题**，不是服务端故障。
    # 绕过它会让一个纯空白的驳回理由变成 500 + 一整条堆栈。
    InvalidRequestError: 422,
    BudgetExhaustedError: 503,
    ProviderError: 502,
    # 宪法违反是系统缺陷，不是用户的问题——对用户只说"服务内部错误"，
    # 真实原因（哪条不变量）留在服务端。
    ConstitutionViolationError: 500,
    InvariantViolationError: 500,
    DomainError: 400,
    ApplicationError: 500,
}

#: 未预期的异常统一用它回应，避免任何内部信息外泄。
_UNEXPECTED_MESSAGE = "服务内部错误，请稍后重试或联系管理员"


def _status_for(exc: AIPsiError) -> int:
    """返回异常对应的 HTTP 状态码。"""
    for exception_type, status in HTTP_STATUS_BY_EXCEPTION.items():
        if isinstance(exc, exception_type):
            return status
    return 500


def register_error_handlers(app: FastAPI) -> None:
    """注册错误处理器。

    Args:
        app: FastAPI 应用。
    """

    @app.exception_handler(AIPsiError)
    async def _handle_ai_psi_error(request: Request, exc: AIPsiError) -> JSONResponse:
        """把领域异常映射为结构化错误响应。

        🔴 ``to_dict()`` 不含堆栈，因此这里可以安全地直接外发。
        """
        status = _status_for(exc)
        payload: dict[str, Any] = exc.to_dict()
        if status >= 500:
            # 5xx 的具体原因只留在日志里：它可能是内部缺陷的线索
            _log_server_error(request, exc)
            payload = {"code": exc.code, "message": _UNEXPECTED_MESSAGE}
        return JSONResponse(status_code=status, content=payload)

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """把 Schema 校验失败映射成**带机器可读错误码**的 422。

        🔴 **FastAPI 的默认响应没有 ``code``。**

        它返回 ``{"detail": [...]}``——于是客户端要区分
        "字段缺失"与"值非法"只能去解析那句英文消息，
        而那句消息会随 pydantic 版本变化。阶段 6.5 §三.2 要求
        "客户端输入错误统一返回明确 4xx + 机器可读 error_code"。

        ⚠️ **响应里只回字段路径与错误类型，不回输入的值。**
        pydantic 默认的 ``detail`` 含 ``input``——那是**用户原文**。
        把它原样回显给客户端会把一次"格式错误"变成一次
        XSS/日志注入的载体，而且服务端的错误响应是最容易被
        完整记录下来的东西之一。
        """
        fields = [_field_path(item.get("loc", ())) for item in exc.errors()]
        record_rejected_request(
            path=request.url.path,
            method=request.method,
            code=INVALID_REQUEST_CODE,
            reason="请求未通过 Schema 校验",
            field_paths=fields,
        )
        return JSONResponse(
            status_code=422,
            content={
                "code": INVALID_REQUEST_CODE,
                "message": "请求格式或取值不合法",
                "fields": sorted(set(fields)),
            },
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """兜底处理器。

        🔴 **不返回任何异常细节。** 未预期异常的文本可能包含
        文件路径、SQL 片段甚至数据内容——那不属于客户端需要知道的信息。
        """
        _log_unexpected(request, exc)
        return JSONResponse(
            status_code=500,
            content={"code": "internal_error", "message": _UNEXPECTED_MESSAGE},
        )


def _field_path(location: object) -> str:
    """把 pydantic 的 ``loc`` 元组拼成一个点分路径。

    形如 ``body.evidence.0`` —— **只有路径，没有值**（见处理器文档）。
    """
    if not isinstance(location, (list, tuple)):  # pragma: no cover - pydantic 恒给序列
        return str(location)
    return ".".join(str(item) for item in location)


def _log_server_error(request: Request, exc: AIPsiError) -> None:
    """记录 5xx 领域错误。"""
    from ai_psi.infrastructure.logging import get_logger

    get_logger(__name__).error(
        "api_domain_error",
        code=exc.code,
        path=request.url.path,
        method=request.method,
        context=exc.context,
    )


def _log_unexpected(request: Request, exc: Exception) -> None:
    """记录未预期异常（含堆栈，仅服务端可见）。"""
    from ai_psi.infrastructure.logging import get_logger

    get_logger(__name__).exception(
        "api_unexpected_error",
        path=request.url.path,
        method=request.method,
        error_type=type(exc).__name__,
    )
