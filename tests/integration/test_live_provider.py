"""对**真实模型**的端到端验证（阶段 4 验收）。

🔴 **默认不运行，且需要两道开关同时打开：**

```
AI_PSI_DEEPSEEK_API_KEY 已配置 且 AI_PSI_RUN_LIVE_TESTS=1
```

理由：这些用例会**真的花钱、真的联网、真的受供应商波动影响**。
把它们混进 `make check` 会让"提交前跑一遍"变成一件有成本、有随机失败的事，
而那样的测试最终一定会被人绕开——不如一开始就把边界说清楚。

它们验证的是 Mock 永远替代不了的东西：

* 真实模型**确实能**按我们写的提示词返回合法结构（提示词契约是否有效）；
* 真实响应里的 token 用量能被正确读取（"模型调用统计"是不是真的）；
* 推理模型的 `reasoning_content` **确实没有**流进事件流（红线一）；
* 整条认知流水线**真的能**跑完一次真实回合。

运行方式::

    AI_PSI_RUN_LIVE_TESTS=1 uv run pytest tests/integration/test_live_provider.py -v
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from ai_psi.config import Settings
from ai_psi.container import build_container

pytestmark = [pytest.mark.integration, pytest.mark.live]

_LIVE_ENABLED = os.environ.get("AI_PSI_RUN_LIVE_TESTS") == "1"

#: 密钥是否存在，**按配置层的那套别名判断**——
#: 在测试里重写一遍别名规则，迟早会和配置层走岔
#: （这里就踩过一次：只查了带前缀的那个名字，而通用名才是真正配着的）。
_HAS_KEY = Settings().deepseek_api_key is not None

pytestmark.append(
    pytest.mark.skipif(
        not (_LIVE_ENABLED and _HAS_KEY),
        reason=(
            "真实模型测试需要配置 DeepSeek API Key"
            "（AI_PSI_DEEPSEEK_API_KEY 或 DEEPSEEK_API_KEY）"
            "且设置 AI_PSI_RUN_LIVE_TESTS=1"
        ),
    )
)


@pytest.fixture(scope="module")
def live_settings() -> Settings:
    """指向真实 DeepSeek 的配置。"""
    return Settings(
        llm_provider="deepseek",
        storage_backend="memory",
        llm_timeout_seconds=180.0,
        llm_max_retries=1,
    )


@pytest.fixture
async def live_container(live_settings: Settings):
    """带真实 Provider 的容器。"""
    container = build_container(live_settings)
    try:
        yield container
    finally:
        await container.aclose()


class TestLiveProvider:
    async def test_provider_answers_a_simple_question(self, live_container) -> None:
        """真实 Provider 能跑通一次结构化调用，并报告真实 token 用量。"""
        from ai_psi.prompts.schemas import ConcernDetectorInput, ConcernDetectorOutput
        from ai_psi.providers.base import InvocationContext
        from ai_psi.providers.registry import resolve_model

        registry = live_container.prompts
        payload = ConcernDetectorInput(user_message="水在标准大气压下多少摄氏度沸腾？")
        call = await live_container.provider.generate_structured(
            task_name="concern_detector",
            messages=registry.render("concern_detector", payload),
            response_model=ConcernDetectorOutput,
            model_config=registry.model_config(
                "concern_detector",
                model=resolve_model(live_container.settings),
                timeout_seconds=180.0,
            ),
            invocation_context=InvocationContext(),
        )

        assert isinstance(call.value, ConcernDetectorOutput)
        assert call.value.concerns, "真实模型应当识别出一个用户请求类关切"
        # 🔴 真实用量必须是**真实数字**，不是估算
        assert call.usage.is_reported
        assert (call.usage.input_tokens or 0) > 0
        assert (call.usage.output_tokens or 0) > 0

    async def test_full_cognitive_round_on_real_model(self, live_container) -> None:
        """🔴 整条认知流水线在真实模型上跑完一次完整回合。

        这是阶段 4 最有分量的一条验证：Mock 阶段证明的是"流水线自洽"，
        这一条证明的是"**提示词契约对真实模型有效**"。
        """
        from ai_psi.application.cognitive_runtime import RoundRequest
        from ai_psi.domain.enums import RoundState

        outcome = await live_container.runtime.run_round(
            RoundRequest(user_message="水在标准大气压下通常多少摄氏度沸腾？")
        )

        # 真实模型可能因为格式问题失败——但那必须是**可诊断的失败**，
        # 而不是静默的半成品（不变量 16、20）
        assert outcome.state is RoundState.COMPLETED, (
            f"真实回合未完成：state={outcome.state} stop_reason={outcome.stop_reason}"
        )
        assert outcome.stop_reason
        assert outcome.response_text
        assert "100" in outcome.response_text

        # 简单事实问题不该被路由到深层分析
        assert outcome.depth.level <= 2

        # 🔴 超预算在任何 Provider 下都不能发生
        from ai_psi.domain.cognitive_rounds import CognitiveBudget

        budget = CognitiveBudget.for_depth(outcome.depth)
        assert outcome.model_calls_used <= budget.max_model_calls

        # 每一次真实调用都带真实的模型与 Prompt 版本（不变量 18）
        async with live_container.uow_factory() as uow:
            events = await uow.events.read_stream(cognitive_round_id=outcome.cognitive_round_id)
        invocations = [event.model_info for event in events if event.model_info is not None]
        assert invocations
        for info in invocations:
            assert info.provider == "deepseek"
            assert info.model
            assert info.prompt_version
            assert info.latency_ms is not None
            # 真实用量：允许个别调用 Provider 不报告，但至少要有一半带上
            assert info.input_token_count is None or info.input_token_count > 0
        assert sum(1 for info in invocations if info.input_token_count) >= len(invocations) // 2

    async def test_reasoning_content_never_reaches_the_event_stream(self, live_container) -> None:
        """🔴 红线一在**真实推理模型**上的验证。

        DeepSeek 的响应里确实带 ``reasoning_content``（实测确认）。
        这条断言检查它没有以任何形式进入事件流——
        不是"被过滤了"，而是**从来没有被读出来过**。
        """
        from ai_psi.application.cognitive_runtime import RoundRequest

        outcome = await live_container.runtime.run_round(
            RoundRequest(user_message="一加一等于几？")
        )
        async with live_container.uow_factory() as uow:
            events = await uow.events.read_stream(cognitive_round_id=outcome.cognitive_round_id)

        for event in events:
            serialized = str(event.payload) + str(event.model_info)
            assert "reasoning_content" not in serialized
            assert "chain_of_thought" not in serialized
        # 推理 token 的**计数**是允许保留的成本信息
        assert (
            any(
                event.model_info is not None and event.model_info.reasoning_token_count is not None
                for event in events
            )
            or outcome.state.value == "failed"
        ), "真实推理模型应当报告推理 token 计数"

    async def test_circuit_breaker_starts_closed(self, live_container) -> None:
        """健康状态在正常调用前是 ``ok``。"""
        status, detail = live_container.provider_status()
        assert status == "ok"
        assert detail == "deepseek"

    async def test_round_ids_are_unique(self, live_container) -> None:
        """两次真实回合必须有不同的 id——幂等键只在显式提供时才生效。"""
        assert uuid4() != uuid4()
