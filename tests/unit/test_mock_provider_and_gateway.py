"""Mock Provider 与模型调用网关。

🔴 **网关是"模型"与"系统"之间的阀门**，它负责四件不能出错的事：

1. 每次调用都带上任务名与 Prompt 版本（不变量 18）；
2. 调用**之前**先扣预算（超预算回合率必须为 0）；
3. 有限重试，且每次重试都是**一次新的调用**（新的 invocation_id）；
4. 只记录响应哈希，**不记录响应内容**（认知宪法红线一）。
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from pydantic import BaseModel

from ai_psi.domain.cognitive_rounds import CognitiveBudget
from ai_psi.domain.enums import EventType
from ai_psi.domain.exceptions import (
    BudgetExhaustedError,
    ProviderUnavailableError,
    StructuredOutputError,
)
from ai_psi.prompts.schemas import ConcernDetectorInput, ConcernDetectorOutput
from ai_psi.prompts.versions import CONTRACTS, build_default_registry
from ai_psi.providers.base import InvocationContext, LLMMessage, ModelConfig
from ai_psi.providers.gateway import ModelGateway
from ai_psi.providers.mock import MockFault, MockProvider
from ai_psi.reliability.budgets import BudgetTracker

pytestmark = pytest.mark.unit

TASK = "concern_detector"
MODEL_CONFIG = ModelConfig(model="mock-model-v1")


def _context() -> InvocationContext:
    return InvocationContext(cognitive_round_id=uuid4())


def _messages() -> list[LLMMessage]:
    return build_default_registry().render(TASK, ConcernDetectorInput(user_message="你好"))


async def _call(
    provider: MockProvider,
    *,
    response_model: type[ConcernDetectorOutput] = ConcernDetectorOutput,
    messages: list[LLMMessage] | None = None,
) -> ConcernDetectorOutput:
    return await provider.generate_structured(
        task_name=TASK,
        messages=messages if messages is not None else _messages(),
        response_model=response_model,
        model_config=MODEL_CONFIG,
        invocation_context=_context(),
    )


class TestMockProvider:
    async def test_default_engine_produces_valid_output(self) -> None:
        provider = MockProvider()
        result = await _call(provider)
        assert isinstance(result, ConcernDetectorOutput)
        assert result.concerns

    async def test_scripted_response_wins(self) -> None:
        provider = MockProvider(responses={TASK: [{"concerns": []}]})
        result = await _call(provider)
        assert result.concerns == []

    async def test_scripted_responses_are_consumed_in_order(self) -> None:
        provider = MockProvider(
            responses={TASK: [{"concerns": []}, {"concerns": []}]},
        )
        await _call(provider)

    async def test_last_scripted_response_repeats(self) -> None:
        """🔴 "模型连续返回同一观点"这类场景依赖这个行为。"""
        first: dict[str, object] = {"concerns": []}
        provider = MockProvider(responses={TASK: [{"concerns": []}, first]})
        await _call(provider)
        second = await _call(provider)
        third = await _call(provider)
        assert second == third

    async def test_call_counts_are_tracked(self) -> None:
        provider = MockProvider()
        await _call(provider)
        await _call(provider)
        assert provider.call_counts[TASK] == 2

    async def test_reset_counters(self) -> None:
        provider = MockProvider()
        await _call(provider)
        provider.reset_counters()
        assert provider.call_counts == {}

    async def test_missing_payload_raises(self) -> None:
        provider = MockProvider()
        with pytest.raises(StructuredOutputError, match="找不到"):
            await _call(provider, messages=[LLMMessage(role="user", content="没有数据块")])

    async def test_unknown_task_raises(self) -> None:
        provider = MockProvider()
        with pytest.raises(StructuredOutputError, match="不认识"):
            await provider.generate_structured(
                task_name="不存在的任务",
                messages=_messages(),
                response_model=ConcernDetectorOutput,
                model_config=MODEL_CONFIG,
                invocation_context=_context(),
            )

    async def test_string_response_for_structured_task_raises(self) -> None:
        provider = MockProvider(responses={TASK: ["这是文本"]})
        with pytest.raises(StructuredOutputError, match="期望结构化响应"):
            await _call(provider)

    async def test_text_task_rejects_structured_response(self) -> None:
        provider = MockProvider(responses={"response_renderer": [{"a": 1}]})
        registry = build_default_registry()
        with pytest.raises(StructuredOutputError, match="期望文本响应"):
            await provider.generate_text(
                task_name="response_renderer",
                messages=registry.render("response_renderer", {"plan": {}}),
                model_config=MODEL_CONFIG,
                invocation_context=_context(),
            )

    async def test_validation_errors_do_not_leak_raw_output(self) -> None:
        """🔴 错误上下文只含**字段路径与原因**，不含模型原始输出。"""
        provider = MockProvider(responses={TASK: [{"concerns": [{"statement": 1}]}]})
        with pytest.raises(StructuredOutputError) as excinfo:
            await _call(provider)
        assert excinfo.value.validation_errors
        assert "statement" in excinfo.value.validation_errors[0]

    async def test_provider_name(self) -> None:
        assert MockProvider().name == "mock"
        assert MockProvider(name="custom").name == "custom"


async def _call_task[T: BaseModel](
    provider: MockProvider,
    task_name: str,
    payload: dict[str, object],
    response_model: type[T],
) -> T:
    """按任意任务发起一次结构化调用。"""
    registry = build_default_registry()
    return await provider.generate_structured(
        task_name=task_name,
        messages=registry.render(task_name, payload),
        response_model=response_model,
        model_config=MODEL_CONFIG,
        invocation_context=_context(),
    )


class TestMockFaults:
    async def test_drop_field(self) -> None:
        """⚠️ 必须挑一个**没有默认值**的字段来删。

        删 ``concerns`` 之类的带默认值字段，Schema 会把它补回来——
        故障根本不生效，测试也就变成了空转。
        """
        from ai_psi.prompts.schemas import InquiryFramerOutput

        provider = MockProvider(
            faults=[
                MockFault(
                    task_name="inquiry_framer",
                    mode="drop_field",
                    field="inquiry.question",
                    times=1,
                )
            ]
        )
        with pytest.raises(StructuredOutputError):
            await _call_task(
                provider,
                "inquiry_framer",
                {"concern_statement": "x", "why_it_matters": "y", "user_message": "z"},
                InquiryFramerOutput,
            )

    async def test_fault_only_lasts_for_configured_attempts(self) -> None:
        from ai_psi.prompts.schemas import InquiryFramerOutput

        provider = MockProvider(
            faults=[
                MockFault(
                    task_name="inquiry_framer",
                    mode="drop_field",
                    field="inquiry.question",
                    times=1,
                )
            ]
        )
        payload: dict[str, object] = {
            "concern_statement": "x",
            "why_it_matters": "y",
            "user_message": "z",
        }
        with pytest.raises(StructuredOutputError):
            await _call_task(provider, "inquiry_framer", payload, InquiryFramerOutput)
        # 第二次尝试故障已过期
        assert isinstance(
            await _call_task(provider, "inquiry_framer", payload, InquiryFramerOutput),
            InquiryFramerOutput,
        )

    async def test_forbidden_extra_field(self) -> None:
        """``extra="forbid"`` 让模型多返回的字段无法通过——红线一的第一道防线。"""
        provider = MockProvider(
            faults=[MockFault(task_name=TASK, mode="forbidden_extra_field", times=1)]
        )
        with pytest.raises(StructuredOutputError):
            await _call(provider)

    async def test_invalid_enum(self) -> None:
        provider = MockProvider(
            responses={
                TASK: [
                    {
                        "concerns": [
                            {
                                "statement": "x",
                                "why_it_matters": "y",
                                "category": "not_a_category",
                            }
                        ]
                    }
                ]
            }
        )
        with pytest.raises(StructuredOutputError):
            await _call(provider)

    async def test_not_an_object(self) -> None:
        provider = MockProvider(faults=[MockFault(task_name=TASK, mode="not_an_object")])
        with pytest.raises(StructuredOutputError):
            await _call(provider)

    async def test_fault_requires_field(self) -> None:
        with pytest.raises(ValueError, match="必须指定 field"):
            MockFault(task_name=TASK, mode="drop_field")

    async def test_not_an_object_rejects_field(self) -> None:
        with pytest.raises(ValueError, match="不接受 field"):
            MockFault(task_name=TASK, mode="not_an_object", field="x")


# ---------------------------------------------------------------------------
# 网关
# ---------------------------------------------------------------------------


class _FlakyProvider:
    """可编程的假 Provider：控制失败次数、失败类型与耗时。"""

    def __init__(
        self,
        *,
        failures: int = 0,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self._failures = failures
        self._error = error or StructuredOutputError("模拟失败")
        self._delay = delay
        self.calls = 0

    @property
    def name(self) -> str:
        return "flaky"

    async def generate_structured(
        self,
        *,
        task_name,
        messages,
        response_model,
        model_config,
        invocation_context,
    ):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self.calls <= self._failures:
            raise self._error
        return ConcernDetectorOutput(concerns=[])

    async def generate_text(
        self,
        *,
        task_name,
        messages,
        model_config,
        invocation_context,
    ):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self.calls <= self._failures:
            raise self._error
        return "文本"


def _gateway(
    provider: object,
    *,
    budget: BudgetTracker | None = None,
    max_retries: int = 2,
    timeout: float | None = None,
) -> ModelGateway:
    return ModelGateway(
        provider=provider,  # type: ignore[arg-type]
        prompts=build_default_registry(),
        budget=budget if budget is not None else BudgetTracker(CognitiveBudget(max_model_calls=20)),
        model="test-model",
        max_retries=max_retries,
        timeout_seconds=timeout,
    )


class TestGatewaySuccess:
    async def test_returns_value_and_invocation(self) -> None:
        gateway = _gateway(MockProvider())
        call = await gateway.structured(
            task_name=TASK,
            payload=ConcernDetectorInput(user_message="你好"),
            response_model=ConcernDetectorOutput,
            context=_context(),
        )
        assert isinstance(call.value, ConcernDetectorOutput)
        assert call.invocation.model == "test-model"
        assert call.invocation.task_name == TASK
        assert call.invocation.prompt_version
        assert call.invocation.result_status == "success"

    async def test_invocation_records_hash_not_content(self) -> None:
        """🔴 只记录响应哈希。原始响应可能包含模型自由联想。"""
        gateway = _gateway(MockProvider())
        call = await gateway.structured(
            task_name=TASK,
            payload=ConcernDetectorInput(user_message="你好"),
            response_model=ConcernDetectorOutput,
            context=_context(),
        )
        assert call.invocation.response_hash is not None
        assert len(call.invocation.response_hash) == 64  # SHA-256 十六进制
        assert "你好" not in str(call.invocation.model_dump())

    async def test_every_prompt_version_is_recorded(self) -> None:
        """不变量 18：所有模型调用必须记录模型与 Prompt 版本。"""
        versions = {contract.task_name: contract.version for contract in CONTRACTS}
        gateway = _gateway(MockProvider())
        call = await gateway.structured(
            task_name=TASK,
            payload=ConcernDetectorInput(user_message="你好"),
            response_model=ConcernDetectorOutput,
            context=_context(),
        )
        assert call.invocation.prompt_version == versions[TASK]

    async def test_text_call(self) -> None:
        gateway = _gateway(MockProvider())
        call = await gateway.text(
            task_name="response_renderer",
            payload={"plan": {}, "conclusion": "结论"},
            context=_context(),
        )
        assert call.text
        assert call.invocation.response_hash


class TestGatewayBudget:
    async def test_spends_before_calling(self) -> None:
        budget = BudgetTracker(CognitiveBudget(max_model_calls=3))
        gateway = _gateway(MockProvider(), budget=budget)
        await gateway.structured(
            task_name=TASK,
            payload=ConcernDetectorInput(user_message="你好"),
            response_model=ConcernDetectorOutput,
            context=_context(),
        )
        assert budget.model_calls_used == 1

    async def test_exhausted_budget_blocks_before_spending(self) -> None:
        """🔴 预算不足时必须**在调用发生之前**失败，而不是"先花了再说"。"""
        budget = BudgetTracker(CognitiveBudget(max_model_calls=1))
        budget.spend_model_call(task_name="先花掉")
        provider = _FlakyProvider()
        gateway = _gateway(provider, budget=budget)
        with pytest.raises(BudgetExhaustedError):
            await gateway.structured(
                task_name=TASK,
                payload=ConcernDetectorInput(user_message="你好"),
                response_model=ConcernDetectorOutput,
                context=_context(),
            )
        assert provider.calls == 0

    async def test_retries_consume_budget(self) -> None:
        """每次重试都是一次真实的调用，因此也必须扣额度。"""
        budget = BudgetTracker(CognitiveBudget(max_model_calls=10))
        gateway = _gateway(_FlakyProvider(failures=2), budget=budget, max_retries=2)
        await gateway.structured(
            task_name=TASK,
            payload=ConcernDetectorInput(user_message="你好"),
            response_model=ConcernDetectorOutput,
            context=_context(),
        )
        assert budget.model_calls_used == 3


class TestGatewayRetry:
    async def test_retries_then_succeeds(self) -> None:
        provider = _FlakyProvider(failures=2)
        gateway = _gateway(provider, max_retries=3)
        call = await gateway.structured(
            task_name=TASK,
            payload=ConcernDetectorInput(user_message="你好"),
            response_model=ConcernDetectorOutput,
            context=_context(),
        )
        assert call.invocation.retry_count == 2
        assert provider.calls == 3

    async def test_retries_are_bounded(self) -> None:
        provider = _FlakyProvider(failures=99)
        gateway = _gateway(provider, max_retries=2)
        with pytest.raises(StructuredOutputError):
            await gateway.structured(
                task_name=TASK,
                payload=ConcernDetectorInput(user_message="你好"),
                response_model=ConcernDetectorOutput,
                context=_context(),
            )
        assert provider.calls == 3  # 1 次 + 2 次重试

    async def test_non_retryable_error_raises_immediately(self) -> None:
        provider = _FlakyProvider(
            failures=99,
            error=ProviderUnavailableError("熔断开启", retryable=False),
        )
        gateway = _gateway(provider, max_retries=3)
        with pytest.raises(ProviderUnavailableError):
            await gateway.structured(
                task_name=TASK,
                payload=ConcernDetectorInput(user_message="你好"),
                response_model=ConcernDetectorOutput,
                context=_context(),
            )
        assert provider.calls == 1

    async def test_timeout_maps_to_provider_timeout(self) -> None:
        """超时**不破坏已有状态**（§19.4），但必须是一个可识别的 Provider 错误。"""
        from ai_psi.domain.exceptions import ProviderTimeoutError

        provider = _FlakyProvider(failures=99, delay=0.2)
        gateway = _gateway(provider, max_retries=0, timeout=0.01)
        with pytest.raises(ProviderTimeoutError):
            await gateway.structured(
                task_name=TASK,
                payload=ConcernDetectorInput(user_message="你好"),
                response_model=ConcernDetectorOutput,
                context=_context(),
            )

    async def test_no_timeout_when_disabled(self) -> None:
        provider = _FlakyProvider(delay=0.01)
        gateway = _gateway(provider, timeout=None)
        call = await gateway.structured(
            task_name=TASK,
            payload=ConcernDetectorInput(user_message="你好"),
            response_model=ConcernDetectorOutput,
            context=_context(),
        )
        assert call.value.concerns == []


class TestGatewayErrors:
    async def test_unknown_task_raises_configuration_error(self) -> None:
        from ai_psi.domain.exceptions import ConfigurationError

        gateway = _gateway(MockProvider())
        with pytest.raises(ConfigurationError):
            await gateway.structured(
                task_name="nope",
                payload={},
                response_model=ConcernDetectorOutput,
                context=_context(),
            )


class TestProviderEvents:
    def test_event_type_for_analysis_is_available(self) -> None:
        """阶段 3 补齐的事件类型必须真的存在于枚举里。"""
        assert EventType.COGNITION_ANALYSIS_COMPLETED.value == "cognition.analysis.completed"
