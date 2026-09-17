"""共享测试夹具。

本模块提供**工厂夹具**（fixture 返回可调用对象），而不是固定的对象实例。
这样每个测试都能拿到语义正确、字段合法的对象，同时可以按需覆盖任意字段，
把注意力集中在被测的那条规则上。

约定：所有工厂的 ``**overrides`` 都会覆盖默认值，
因此"构造一个违反了某条规则的实例"是显式且易读的。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from ai_psi.domain.beliefs import Belief
from ai_psi.domain.cognitive_rounds import CognitiveBudget, CognitiveRound
from ai_psi.domain.concerns import Concern
from ai_psi.domain.enums import (
    ActorType,
    ConcernCategory,
    EventType,
    MemoryType,
    SourceType,
)
from ai_psi.domain.events import Event, ModelInvocationInfo
from ai_psi.domain.evidence import Evidence
from ai_psi.domain.experiences import Experience
from ai_psi.domain.hypotheses import Hypothesis
from ai_psi.domain.improvement_proposals import ImprovementProposal
from ai_psi.domain.inquiries import Inquiry
from ai_psi.domain.judgments import Judgment
from ai_psi.domain.memories import Memory
from ai_psi.domain.observations import Observation
from ai_psi.domain.reflections import Reflection
from ai_psi.domain.situations import Situation
from ai_psi.domain.user_models import UserModel

Factory = Callable[..., Any]

CREATED_BY = "test_suite"


@pytest.fixture
def user_id() -> UUID:
    """主用户 id。"""
    return uuid4()


@pytest.fixture
def other_user_id() -> UUID:
    """另一个用户 id，用于作用域隔离测试。"""
    return uuid4()


@pytest.fixture
def make_observation() -> Factory:
    def _make(**overrides: Any) -> Observation:
        payload: dict[str, Any] = {
            "created_by": CREATED_BY,
            "content": "朋友今天只回复了一个「嗯」",
            "source_id": "msg-001",
            "source_type": SourceType.USER_MESSAGE,
        }
        payload.update(overrides)
        return Observation(**payload)

    return _make


@pytest.fixture
def make_concern() -> Factory:
    def _make(**overrides: Any) -> Concern:
        payload: dict[str, Any] = {
            "created_by": CREATED_BY,
            "source_event_ids": [uuid4()],
            "category": ConcernCategory.USER_REQUEST,
            "statement": "用户想知道朋友是否对他有负面看法",
            "why_it_matters": "影响用户的人际判断",
        }
        payload.update(overrides)
        return Concern(**payload)

    return _make


@pytest.fixture
def make_inquiry() -> Factory:
    def _make(**overrides: Any) -> Inquiry:
        payload: dict[str, Any] = {
            "created_by": CREATED_BY,
            "concern_id": uuid4(),
            "question": "朋友的简短回复有哪些可能的解释？",
            "why_it_matters": "避免对第三方做出无依据的心理推断",
            "scope": ["可观察的通信行为"],
            "out_of_scope": ["朋友的真实心理状态"],
            "stop_conditions": ["已列出至少一个非人格化解释"],
        }
        payload.update(overrides)
        return Inquiry(**payload)

    return _make


@pytest.fixture
def make_evidence() -> Factory:
    def _make(**overrides: Any) -> Evidence:
        payload: dict[str, Any] = {
            "created_by": CREATED_BY,
            "source_name": "某研究",
            "content_summary": "简短回复有多种常见原因",
        }
        payload.update(overrides)
        return Evidence(**payload)

    return _make


@pytest.fixture
def make_hypothesis() -> Factory:
    def _make(**overrides: Any) -> Hypothesis:
        payload: dict[str, Any] = {
            "created_by": CREATED_BY,
            "inquiry_id": uuid4(),
            "statement": "朋友当时正在忙，无暇长回复",
            "falsification_conditions": ["朋友当天有充裕空闲时间"],
        }
        payload.update(overrides)
        return Hypothesis(**payload)

    return _make


@pytest.fixture
def make_belief() -> Factory:
    def _make(**overrides: Any) -> Belief:
        payload: dict[str, Any] = {
            "created_by": CREATED_BY,
            "statement": "标准大气压下水的沸点约为 100 摄氏度",
            "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
            "confidence_basis": ["物理学常识"],
        }
        payload.update(overrides)
        return Belief(**payload)

    return _make


@pytest.fixture
def make_judgment() -> Factory:
    def _make(**overrides: Any) -> Judgment:
        payload: dict[str, Any] = {
            "created_by": CREATED_BY,
            "inquiry_id": uuid4(),
            "conclusion": "目前没有足够依据判断朋友的意图",
            "rationale_summary": ["单一观察不足以支撑人际推断"],
            "confidence_basis": ["样本只有一条消息"],
        }
        payload.update(overrides)
        return Judgment(**payload)

    return _make


@pytest.fixture
def make_memory() -> Factory:
    def _make(**overrides: Any) -> Memory:
        payload: dict[str, Any] = {
            "created_by": CREATED_BY,
            "memory_type": MemoryType.USER_PREFERENCE,
            "content": "用户偏好简洁回答",
            "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
        }
        payload.update(overrides)
        return Memory(**payload)

    return _make


@pytest.fixture
def make_experience() -> Factory:
    def _make(**overrides: Any) -> Experience:
        payload: dict[str, Any] = {
            "created_by": CREATED_BY,
            "cognitive_round_id": uuid4(),
            "judgment_id": uuid4(),
            "situation_signature": "factual|d0|single_source",
            "inquiry_type": "factual",
        }
        payload.update(overrides)
        return Experience(**payload)

    return _make


@pytest.fixture
def make_proposal() -> Factory:
    def _make(**overrides: Any) -> ImprovementProposal:
        payload: dict[str, Any] = {
            "created_by": CREATED_BY,
            "target_component": "prompt:hypothesis_generator",
            "observed_problem": "同类错误重复出现",
            "error_class": "reasoning_error",
            "proposed_change": "要求生成器显式列出反证",
            "expected_benefit": "降低 reasoning_error 复发率",
        }
        payload.update(overrides)
        return ImprovementProposal(**payload)

    return _make


@pytest.fixture
def make_reflection() -> Factory:
    def _make(**overrides: Any) -> Reflection:
        payload: dict[str, Any] = {
            "created_by": CREATED_BY,
            "cognitive_round_id": uuid4(),
            "reasons": ["已达停止条件"],
        }
        payload.update(overrides)
        return Reflection(**payload)

    return _make


@pytest.fixture
def make_round() -> Factory:
    def _make(**overrides: Any) -> CognitiveRound:
        payload: dict[str, Any] = {
            "created_by": CREATED_BY,
            "budget": CognitiveBudget(),
        }
        payload.update(overrides)
        return CognitiveRound(**payload)

    return _make


@pytest.fixture
def make_user_model() -> Factory:
    def _make(**overrides: Any) -> UserModel:
        payload: dict[str, Any] = {
            "created_by": CREATED_BY,
            "user_id": uuid4(),
            "attribute": "preferred_response_length",
            "value": "concise",
            "evidence_ids": [uuid4()],
            "valid_from": datetime(2026, 1, 1, tzinfo=UTC),
        }
        payload.update(overrides)
        return UserModel(**payload)

    return _make


@pytest.fixture
def make_situation() -> Factory:
    def _make(**overrides: Any) -> Situation:
        payload: dict[str, Any] = {
            "created_by": CREATED_BY,
            "situation_signature": "relational|d2|third_party",
            "inquiry_type": "relational",
        }
        payload.update(overrides)
        return Situation(**payload)

    return _make


@pytest.fixture
def make_event() -> Factory:
    def _make(**overrides: Any) -> Event:
        payload: dict[str, Any] = {
            "event_type": EventType.USER_MESSAGE_RECEIVED,
            "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
            "actor_type": ActorType.USER,
            "actor_id": "user-001",
        }
        payload.update(overrides)
        return Event(**payload)

    return _make


@pytest.fixture
def make_invocation_info() -> Factory:
    def _make(**overrides: Any) -> ModelInvocationInfo:
        payload: dict[str, Any] = {
            "provider": "mock",
            "model": "mock-model-v1",
            "task_name": "hypothesis_generator",
            "prompt_version": "1.0.0",
            "started_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
        payload.update(overrides)
        return ModelInvocationInfo(**payload)

    return _make


@pytest.fixture
def utc_now_fixed() -> datetime:
    """固定时间点，避免测试依赖真实时钟。"""
    return datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def one_hour_later(utc_now_fixed: datetime) -> datetime:
    return utc_now_fixed + timedelta(hours=1)


#: 会被开发机环境**真实设置**的供应商密钥变量名。
#:
#: 配置层刻意同时接受业界通用名（``DEEPSEEK_API_KEY``）与本项目的
#: ``AI_PSI_`` 前缀名，于是这些变量在开发机上通常是真的存在的。
_AMBIENT_CREDENTIAL_VARS = (
    "AI_PSI_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "AI_PSI_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "AI_PSI_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
)


@pytest.fixture(autouse=True)
def _isolate_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """把供应商密钥从测试环境里摘掉。

    🔴 开发机上往往**真的**配着 ``DEEPSEEK_API_KEY``。如果测试依赖
    "环境里恰好没有密钥"，它们就会在开发机上失败、在 CI 上通过——
    那是最难解释的一类测试失败，而且会诱使人把断言改松。

    标记为 ``live`` 的用例是例外：它们要的就是真实密钥。
    """
    if request.node.get_closest_marker("live") is not None:
        return
    for name in _AMBIENT_CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
