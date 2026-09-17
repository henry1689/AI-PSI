"""运行时把**哪个模型标识**发给供应商（阶段 4 回归测试）。

🔴 **这条测试的由来是一个只有真实 Provider 才能暴露的缺陷。**

阶段 3 的 `_make_gateway` 写的是
``model=settings.llm_model or "mock-model-v1"``。
只要用户没有显式配 ``AI_PSI_LLM_MODEL``，网关就会把
``"mock-model-v1"`` 发给**真实供应商**——DeepSeek 直接返回 400。

Mock Provider 对这个字符串照单全收，因此整个阶段 3 的 869 个测试
没有一个能发现它；它是在阶段 4 第一次跑**真实回合**时被抓住的。

本文件把这个缺陷钉死：它不联网、不花钱，只需要断言
"记录下来的模型标识 == 按 Provider 解析出来的默认值"。
"""

from __future__ import annotations

import pytest

from ai_psi.application.cognitive_runtime import CognitiveRuntime, RoundRequest
from ai_psi.config import Settings
from ai_psi.domain.enums import EventType
from ai_psi.infrastructure.in_memory.memory_store import InMemoryMemoryRepository
from ai_psi.infrastructure.in_memory.store import InMemoryStore
from ai_psi.infrastructure.in_memory.unit_of_work import make_in_memory_unit_of_work_factory
from ai_psi.prompts.versions import build_default_registry
from ai_psi.providers.mock import MockProvider
from ai_psi.providers.registry import resolve_model

pytestmark = pytest.mark.unit


async def _run_with(settings: Settings) -> list[str]:
    """在给定配置下跑一次回合，返回事件里记录的全部模型标识。"""
    store = InMemoryStore()
    uow_factory = make_in_memory_unit_of_work_factory(store)
    runtime = CognitiveRuntime(
        uow_factory=uow_factory,
        # 用 Mock 作为**传输层替身**：本测试关心的是"发出去的模型名是什么"，
        # 而不是"谁来回应它"。
        provider=MockProvider(),
        prompts=build_default_registry(),
        memory=InMemoryMemoryRepository(),
        settings=settings,
    )
    outcome = await runtime.run_round(RoundRequest(user_message="水在标准大气压下多少摄氏度沸腾？"))
    async with uow_factory() as uow:
        events = await uow.events.read_stream(cognitive_round_id=outcome.cognitive_round_id)
    return [
        info.model
        for event in events
        if event.event_type is not EventType.COGNITIVE_ROUND_STARTED and event.model_info
        for info in [event.model_info]
    ]


class TestRuntimeModelBinding:
    async def test_deepseek_default_model_is_used_when_unset(self) -> None:
        """🔴 没显式配 ``llm_model`` 时，必须用该 Provider 的**默认模型**。

        绝不能用 Mock 的模型名——那会以 400 的形式砸在真实供应商脸上。
        """
        models = await _run_with(Settings(llm_provider="deepseek", storage_backend="memory"))
        assert models, "回合应当产生了至少一次模型调用"
        assert set(models) == {"deepseek-v4-flash"}
        assert "mock-model-v1" not in models

    async def test_mock_provider_uses_its_own_model(self) -> None:
        models = await _run_with(Settings(llm_provider="mock", storage_backend="memory"))
        assert set(models) == {"mock-model-v1"}

    async def test_explicit_model_overrides_the_default(self) -> None:
        models = await _run_with(
            Settings(
                llm_provider="deepseek",
                llm_model="deepseek-v4-pro",
                storage_backend="memory",
            )
        )
        assert set(models) == {"deepseek-v4-pro"}

    def test_resolve_model_matches_the_runtime(self) -> None:
        """运行时用的必须是同一个解析函数，不能在两处各写一套规则。"""
        settings = Settings(llm_provider="deepseek", storage_backend="memory")
        assert resolve_model(settings) == "deepseek-v4-flash"

    def test_all_available_providers_resolve_a_model(self) -> None:
        from ai_psi.providers.registry import AVAILABLE_PROVIDERS

        for name in AVAILABLE_PROVIDERS:
            settings = Settings(llm_provider=name, storage_backend="memory")
            resolved = resolve_model(settings)
            assert resolved and resolved != "unknown-model", name
