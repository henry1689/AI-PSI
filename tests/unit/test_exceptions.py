"""异常层次的单元测试。

重点：异常必须携带**机器可读的 code**，且 ``to_dict()`` 的输出
**不含堆栈**——它会被直接返回给普通 API 客户端（任务书 §17.1）。
"""

from __future__ import annotations

import pytest

from ai_psi.domain.exceptions import (
    AIPsiError,
    ApplicationError,
    BudgetExhaustedError,
    ConfigurationError,
    ConflictError,
    ConstitutionViolationError,
    DomainError,
    IllegalStateTransitionError,
    InvariantViolationError,
    NotFoundError,
    OptimisticLockError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
    ScopeViolationError,
    StructuredOutputError,
)

pytestmark = pytest.mark.unit


class TestHierarchy:
    def test_all_errors_share_a_base(self) -> None:
        for exc_type in (
            DomainError,
            ApplicationError,
            ProviderError,
            ConfigurationError,
        ):
            assert issubclass(exc_type, AIPsiError)

    def test_constitution_violation_is_an_invariant_violation(self) -> None:
        assert issubclass(ConstitutionViolationError, InvariantViolationError)
        assert issubclass(InvariantViolationError, DomainError)

    def test_provider_errors_are_grouped(self) -> None:
        for exc_type in (
            ProviderTimeoutError,
            ProviderRateLimitError,
            ProviderUnavailableError,
            StructuredOutputError,
        ):
            assert issubclass(exc_type, ProviderError)


class TestErrorCodes:
    def test_each_type_has_a_distinct_default_code(self) -> None:
        codes = {
            cls.default_code
            for cls in (
                DomainError,
                ApplicationError,
                ProviderError,
                ConfigurationError,
                IllegalStateTransitionError,
                OptimisticLockError,
                ScopeViolationError,
                BudgetExhaustedError,
                InvariantViolationError,
                ConstitutionViolationError,
                NotFoundError,
                ConflictError,
                ProviderTimeoutError,
                ProviderRateLimitError,
                ProviderUnavailableError,
                StructuredOutputError,
            )
        }
        assert len(codes) == 16, "异常码应当互不重复，便于按码定位"

    def test_code_can_be_overridden(self) -> None:
        err = DomainError("x", code="custom_code")
        assert err.code == "custom_code"


class TestSerialization:
    def test_to_dict_omits_stack(self) -> None:
        """🔴 错误堆栈不得返回给普通 API 客户端。"""
        try:
            raise IllegalStateTransitionError(
                "非法转移",
                from_state="created",
                to_state="completed",
            )
        except IllegalStateTransitionError as exc:
            payload = exc.to_dict()

        assert "traceback" not in payload
        assert "__traceback__" not in payload
        assert set(payload) <= {"code", "message", "context"}

    def test_structured_context_is_carried(self) -> None:
        err = IllegalStateTransitionError(
            "非法转移",
            from_state="created",
            to_state="completed",
        )
        assert err.context["from_state"] == "created"
        assert err.context["to_state"] == "completed"

    def test_repr_is_informative(self) -> None:
        err = OptimisticLockError(
            "版本冲突",
            entity_type="Memory",
            entity_id="m-1",
            expected_version=1,
            actual_version=2,
        )
        assert "optimistic_lock_conflict" in repr(err)


class TestOptimisticLockError:
    def test_carries_version_details(self) -> None:
        """冲突是正常业务路径，需要足够信息让调用方重试。"""
        err = OptimisticLockError(
            "版本冲突",
            entity_type="Memory",
            entity_id="m-1",
            expected_version=1,
            actual_version=2,
        )
        assert err.expected_version == 1
        assert err.actual_version == 2
        assert err.context["entity_type"] == "Memory"

    def test_note_that_it_is_not_an_unexpected_error(self) -> None:
        """它是 DomainError 而非 ApplicationError——由领域层判定。"""
        err = OptimisticLockError(
            "x", entity_type="t", entity_id="i", expected_version=1, actual_version=2
        )
        assert isinstance(err, DomainError)


class TestScopeViolationError:
    """🔴 不变量 14：跨用户作用域访问是隐私事故，不是过滤问题。"""

    def test_carries_only_ids_never_content(self) -> None:
        err = ScopeViolationError(
            "跨用户访问",
            requested_user_id="user-a",
            resource_user_id="user-b",
        )
        assert err.context["requested_user_id"] == "user-a"
        assert err.context["resource_user_id"] == "user-b"
        # 上下文里不应有任何内容字段
        assert all("content" not in key for key in err.context)

    def test_is_a_domain_error(self) -> None:
        assert issubclass(ScopeViolationError, DomainError)


class TestStructuredOutputError:
    """🔴 不变量 16：模型格式错误不得导致部分非法状态写入。"""

    def test_carries_validation_paths_not_raw_output(self) -> None:
        err = StructuredOutputError(
            "解析失败",
            validation_errors=("category: 非法枚举值", "statement: 缺失"),
            raw_response_hash="sha256:deadbeef",
        )
        assert err.context["validation_errors"] == [
            "category: 非法枚举值",
            "statement: 缺失",
        ]
        assert err.context["raw_response_hash"] == "sha256:deadbeef"
        # 不保存模型原始输出
        assert "raw_response" not in err.context

    def test_is_retryable_by_default(self) -> None:
        assert StructuredOutputError("x").retryable


class TestRetryability:
    def test_transient_provider_errors_are_retryable(self) -> None:
        assert ProviderTimeoutError("timeout").retryable
        assert ProviderRateLimitError("429").retryable
        assert ProviderUnavailableError("down").retryable

    def test_generic_provider_error_is_not_retryable_by_default(self) -> None:
        assert not ProviderError("x").retryable

    def test_rate_limit_carries_retry_after(self) -> None:
        err = ProviderRateLimitError("429", retry_after_seconds=30.0)
        assert err.retry_after_seconds == 30.0
        assert err.context["retry_after_seconds"] == 30.0


class TestBudgetExhausted:
    def test_carries_budget_details(self) -> None:
        err = BudgetExhaustedError(
            "模型调用超限",
            budget_name="max_model_calls",
            limit=12,
            used=13,
        )
        assert err.budget_name == "max_model_calls"
        assert err.context["limit"] == 12
        assert err.context["used"] == 13


class TestInvariantViolation:
    def test_invariant_id_is_recorded(self) -> None:
        err = InvariantViolationError("违反不变量", invariant_id="I11")
        assert err.invariant_id == "I11"
        assert err.context["invariant_id"] == "I11"

    def test_constitution_violation_reports_distinct_code(self) -> None:
        err = ConstitutionViolationError("违反宪法", invariant_id="I12")
        assert err.code == "constitution_violation"
        assert err.invariant_id == "I12"


class TestApplicationErrors:
    def test_not_found_and_conflict_are_distinct(self) -> None:
        assert NotFoundError.default_code == "not_found"
        assert ConflictError.default_code == "conflict"

    def test_message_is_preserved(self) -> None:
        err = NotFoundError("回合不存在")
        assert str(err) == "回合不存在"
        assert err.message == "回合不存在"
