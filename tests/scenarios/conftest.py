"""场景测试的公共装配。

任务书 §15.4 的十个场景是**阶段 3 的硬性验收条件**。
它们必须在不连数据库、不调外部 API 的前提下全部通过，
因此这里用 ADR-0009 的内存适配器 + Mock Provider 搭一个完整运行时。

**关于"脚本化 Mock"的一个说明**：
内容敏感的任务（问题框定、判断合成、回答渲染）由测试直接给出固定响应。
这不是在"把答案喂给系统"——脚本扮演的是**模型本该提供的那部分判断**
（"这个问题有多个合理解释"、"标准大气压下的沸点是 100 度"），
而流程、约束、不变量全部由真实代码执行。
凡是能由规则决定的（深度路由、置信度钳制、反刍检测、记忆策略），
测试都**不**提供脚本，走的都是生产路径。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest

from ai_psi.application.cognitive_runtime import (
    CognitiveRuntime,
    RoundOutcome,
    RoundRequest,
)
from ai_psi.application.memory_service import MemoryService
from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.cognition.projection import ArtifactView, project_artifacts
from ai_psi.config import Settings
from ai_psi.domain.enums import CognitiveDepth, EventType
from ai_psi.domain.events import Event
from ai_psi.domain.memories import Memory
from ai_psi.infrastructure.in_memory.store import InMemoryStore
from ai_psi.infrastructure.in_memory.unit_of_work import make_in_memory_unit_of_work_factory
from ai_psi.prompts.registry import PromptRegistry
from ai_psi.prompts.versions import build_default_registry
from ai_psi.providers.embeddings import EmbeddingProvider, LocalHashingEmbedding
from ai_psi.providers.mock import MockFault, MockProvider, MockResponse

__all__ = [
    "Harness",
    "hypotheses_response",
    "hypothesis",
    "inquiry_response",
    "judgment_response",
    "metacognition_response",
    "signals_for_depth",
]


# ---------------------------------------------------------------------------
# 脚本化响应的构造工具
# ---------------------------------------------------------------------------


def signals_for_depth(depth: CognitiveDepth) -> dict[str, Any]:
    """构造一组能让深度路由稳定落在指定档位的信号。

    每个场景都用它**显式声明**自己期望的深度。
    🔴 深度仍然由 :func:`~ai_psi.cognition.depth_router.route_depth`
    的真实规则算出——这里给的是"问题的性质"，不是"路由的结果"。

    Args:
        depth: 期望的深度。

    Returns:
        深度信号字典。
    """
    signals: dict[str, Any] = {
        "simple_fact_with_sufficient_evidence": False,
        "needs_explanation_or_comparison": False,
        "multiple_plausible_interpretations": False,
        "user_explicitly_philosophical": False,
        "framework_conflict": False,
        "estimated_impact": "moderate",
        "ambiguity": "low",
        "evidence_conflict": "low",
        "value_conflict": "low",
        "long_term_relevance": "low",
    }
    if depth is CognitiveDepth.D0:
        signals["simple_fact_with_sufficient_evidence"] = True
    elif depth is CognitiveDepth.D1:
        signals["needs_explanation_or_comparison"] = True
    elif depth is CognitiveDepth.D2:
        signals["multiple_plausible_interpretations"] = True
    elif depth is CognitiveDepth.D3:
        signals["value_conflict"] = "high"
    else:
        signals["user_explicitly_philosophical"] = True
    return signals


def inquiry_response(
    *,
    question: str,
    depth: CognitiveDepth = CognitiveDepth.D1,
    ambiguous_concepts: Sequence[str] = (),
    key_unknowns: Sequence[str] = (),
    stop_conditions: Sequence[str] = ("已给出可回答的结论",),
    signals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一次 ``inquiry_framer`` 的脚本响应。"""
    return {
        "inquiry": {
            "question": question,
            "why_it_matters": "用户明确提出的问题",
            "scope": ["与本问题直接相关的可观察信息"],
            "out_of_scope": ["无法获取的第三方内部状态"],
            "known_observations": [],
            "key_unknowns": list(key_unknowns),
            "ambiguous_concepts": list(ambiguous_concepts),
            "assumptions_to_check": [],
            "expected_output_type": "direct_answer",
            "verification_method": None,
            "stop_conditions": list(stop_conditions),
            "reopen_conditions": ["出现新的相关材料"],
            "depth_signals": signals if signals is not None else signals_for_depth(depth),
        }
    }


def hypotheses_response(*drafts: dict[str, Any]) -> dict[str, Any]:
    """构造一次 ``hypothesis_generator`` 的脚本响应。"""
    return {"hypotheses": list(drafts)}


def hypothesis(
    statement: str,
    *,
    category: str = "alternative",
    falsification: Sequence[str] = ("出现可核验的一手材料",),
    predictions: Sequence[str] = (),
    applicability: Sequence[str] = (),
) -> dict[str, Any]:
    """构造一个假设草稿。"""
    return {
        "statement": statement,
        "category": category,
        "predicted_observations": list(predictions),
        "falsification_conditions": list(falsification),
        "applicability": list(applicability),
        "uncertainty_type": "alethic",
    }


def judgment_response(
    *,
    conclusion: str,
    rationale: Sequence[str] = ("现有材料支持有限度的判断",),
    counterarguments: Sequence[str] = (),
    unknowns: Sequence[str] = (),
    applicability: Sequence[str] = (),
    band: str = "low",
    action: str = "answer_with_caveat",
    basis: Sequence[str] = ("结论建立在当前可获得的材料之上",),
    revision: Sequence[str] = (),
    uncertainty_type: str = "alethic",
) -> dict[str, Any]:
    """构造一次 ``judgment_synthesizer`` 的脚本响应。"""
    return {
        "judgment": {
            "conclusion": conclusion,
            "rationale_summary": list(rationale),
            "strongest_counterarguments": list(counterarguments),
            "unresolved_unknowns": list(unknowns),
            "applicability": list(applicability),
            "confidence_band": band,
            "confidence_basis": list(basis),
            "revision_conditions": list(revision),
            "recommended_epistemic_action": action,
            "uncertainty_type": uncertainty_type,
        }
    }


def metacognition_response(*, proposed: str = "stop", **overrides: Any) -> dict[str, Any]:
    """构造一次 ``metacognition`` 的脚本响应。"""
    payload: dict[str, Any] = {
        "confirmation_bias_risk": "very_low",
        "user_pleasing_bias_risk": "very_low",
        "abstraction_escape_risk": "very_low",
        "unsupported_certainty_detected": False,
        "missing_counterexample_detected": False,
        "marginal_value": "low",
        "proposed_decision": proposed,
        "reasons": ["模型层的判断"],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 运行时装配
# ---------------------------------------------------------------------------


@dataclass
class Harness:
    """一次场景测试可用的完整运行时。"""

    runtime: CognitiveRuntime
    provider: MockProvider
    store: InMemoryStore
    memory_service: MemoryService
    embeddings: EmbeddingProvider
    prompts: PromptRegistry
    settings: Settings
    uow_factory: UnitOfWorkFactory

    async def run(self, message: str, **overrides: Any) -> RoundOutcome:
        """跑一次认知回合。

        Args:
            message: 用户消息。
            **overrides: ``RoundRequest`` 的其余字段。

        Returns:
            回合结果。
        """
        return await self.runtime.run_round(RoundRequest(user_message=message, **overrides))

    async def seed_memory(self, memory: Memory) -> None:
        """直接写入一条记忆，**绕过写入策略**。

        场景测试要的是"库里已经有一条记忆"，而不是"某条记忆能被策略批准"——
        后者有它自己的单元测试。用 ``propose`` 造数据会让策略的改动
        波及一大批与策略无关的场景（改一条白名单就能让场景 J 红掉）。
        """
        async with self.uow_factory() as uow:
            await uow.memories.add(memory)
            await uow.commit()

    async def events(self, round_id: UUID) -> list[Event]:
        """读取回合的事件流。"""
        async with self.uow_factory() as uow:
            return await uow.events.read_stream(cognitive_round_id=round_id)

    async def artifacts(self, round_id: UUID) -> ArtifactView:
        """投影回合的认知产物。"""
        return project_artifacts(await self.events(round_id))

    async def event_types(self, round_id: UUID) -> list[str]:
        """回合事件类型序列。"""
        return [event.event_type.value for event in await self.events(round_id)]

    async def model_tasks(self, round_id: UUID) -> list[str]:
        """本回合实际调用过的模型任务名（按调用顺序，**按调用去重**）。

        同一次调用的记录会被挂到多条事件上（例如一次假设生成产出三条假设，
        每条假设事件都带着同一个 invocations），因此必须按 ``invocation_id``
        去重——否则任务列表看起来像"跑了三次假设生成"。
        """
        view = await self.artifacts(round_id)
        seen: dict[UUID, str] = {}
        for info in view.model_invocations:
            seen.setdefault(info.invocation_id, info.task_name)
        return list(seen.values())

    async def posted_events(self, round_id: UUID, event_type: EventType) -> list[Event]:
        """按类型筛选事件。"""
        return [event for event in await self.events(round_id) if event.event_type is event_type]


@pytest.fixture
def harness_factory() -> Callable[..., Harness]:
    """返回一个可以按需构造运行时的工厂。"""

    def _make(
        *,
        responses: dict[str, Sequence[MockResponse]] | None = None,
        faults: Sequence[MockFault] = (),
        settings: Settings | None = None,
        memory_service_factory: Callable[
            [UnitOfWorkFactory, EmbeddingProvider], MemoryService
        ] = MemoryService,
    ) -> Harness:
        resolved = settings or Settings(
            storage_backend="memory",
            llm_provider="mock",
            llm_max_retries=2,
        )
        prompts = build_default_registry()
        provider = MockProvider(responses=responses, faults=faults)
        embeddings = LocalHashingEmbedding()
        store = InMemoryStore()
        uow_factory = make_in_memory_unit_of_work_factory(store, embeddings)
        memory_service = memory_service_factory(uow_factory, embeddings)
        runtime = CognitiveRuntime(
            uow_factory=uow_factory,
            provider=provider,
            prompts=prompts,
            memory_service=memory_service,
            settings=resolved,
        )
        return Harness(
            runtime=runtime,
            provider=provider,
            store=store,
            memory_service=memory_service,
            embeddings=embeddings,
            prompts=prompts,
            settings=resolved,
            uow_factory=uow_factory,
        )

    return _make


@pytest.fixture
def harness(harness_factory: Callable[..., Harness]) -> Harness:
    """一个使用默认规则引擎的运行时。"""
    return harness_factory()
