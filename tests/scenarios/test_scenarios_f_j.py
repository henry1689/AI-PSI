"""任务书 §15.4 场景 F–J。

标记 ``scenario``：阶段 3 的硬性验收条件。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from ai_psi.application.memory_service import MemoryService
from ai_psi.domain.cognitive_rounds import CognitiveBudget
from ai_psi.domain.enums import (
    CognitiveDepth,
    ConfidenceBand,
    ErrorType,
    EventType,
    MemoryStatus,
    MemoryType,
    ProposalStatus,
    RoundState,
    SensitivityLevel,
    VerificationStatus,
)
from ai_psi.domain.improvement_proposals import (
    PROPOSAL_ESCALATION_THRESHOLD,
    ImprovementProposal,
)
from ai_psi.domain.memories import Memory
from ai_psi.infrastructure.in_memory.memory_store import InMemoryMemoryRepository
from ai_psi.memory.write_policy import MemoryWriteProposal, WriteDecision, WritePolicy
from ai_psi.providers.mock import MockFault
from tests.scenarios.conftest import (
    Harness,
    hypotheses_response,
    hypothesis,
    inquiry_response,
    judgment_response,
    metacognition_response,
    signals_for_depth,
)

pytestmark = pytest.mark.scenario


# ---------------------------------------------------------------------------
# 场景 F：用户纠正长期记忆
# ---------------------------------------------------------------------------


async def test_scenario_f_user_corrects_long_term_memory(harness: Harness) -> None:
    """场景 F：先记录"用户喜欢非常详细的回答"，随后用户要求更正。

    预期：
    * 旧记忆被 **superseded**；
    * 新偏好**生效**；
    * 旧偏好**保留纠错痕迹但不默认检索**；
    * 后续回答采用新偏好。
    """
    user_id = uuid4()
    service = MemoryService(harness.uow_factory, harness.memory)

    # 先写入旧偏好（用户当初明确确认过 → 策略批准）
    old = await service.propose(
        proposal=MemoryWriteProposal(
            user_id=user_id,
            memory_type=MemoryType.USER_PREFERENCE,
            content="用户喜欢非常详细的回答",
            sensitivity=SensitivityLevel.PERSONAL,
            user_confirmed=True,
        )
    )
    assert old.decision is WriteDecision.APPROVED
    assert old.memory is not None
    # 策略批准 = 可以生效，因此写入时直接是 ACTIVE（PROPOSED 是"尚未裁决"）
    assert old.memory.status is MemoryStatus.ACTIVE
    assert [
        item.content for item in await service.retrieve(user_id=user_id, query="回答偏好", limit=5)
    ] == ["用户喜欢非常详细的回答"]

    # 用户纠正
    correction = await service.correct(
        user_id=user_id,
        memory_id=old.memory.id,
        new_content="用户更喜欢简洁回答",
    )

    # 旧记忆被取代，而不是被就地覆盖（不变量 5）
    assert correction.superseded.status is MemoryStatus.SUPERSEDED
    assert correction.superseded.id == old.memory.id
    assert correction.superseded.content == "用户喜欢非常详细的回答"

    # 新记忆生效并指回旧版本
    assert correction.replacement.status is MemoryStatus.ACTIVE
    assert correction.replacement.supersedes_id == old.memory.id

    # 默认检索只返回新偏好
    retrieved = await service.retrieve(user_id=user_id, query="回答偏好", limit=5)
    assert [item.content for item in retrieved] == ["用户更喜欢简洁回答"]

    # 纠错痕迹完整保留（含未生效的旧版本）
    history = await service.list_for_user(user_id=user_id, include_inactive=True)
    contents = {item.content for item in history}
    assert "用户喜欢非常详细的回答" in contents
    assert "用户更喜欢简洁回答" in contents

    # 🔴 审计事件不留被纠正内容的正文
    events = correction.events
    assert any(event.event_type is EventType.USER_CORRECTION_RECEIVED for event in events)
    assert any(event.event_type is EventType.MEMORY_CORRECTED for event in events)
    for event in events:
        assert "非常详细" not in str(event.payload)

    # 新偏好进入下一轮检索，因此后续回答会采用简洁风格
    assert retrieved[0].content.endswith("简洁回答")


# ---------------------------------------------------------------------------
# 场景 G：反刍停止
# ---------------------------------------------------------------------------


async def test_scenario_g_rumination_is_stopped(harness_factory) -> None:
    """场景 G：Mock 连续返回同样观点，无新证据。

    预期：
    * **重复检测触发**；
    * 元认知决定 STOP；
    * **不超过最大循环次数**；
    * 停止原因清楚。
    """
    fixed_conclusion = "目前只能给出一个暂定看法，还需要更多材料"
    harness: Harness = harness_factory(
        responses={
            "inquiry_framer": [
                inquiry_response(
                    question="这件事可能的解释是什么",
                    depth=CognitiveDepth.D2,
                    key_unknowns=["缺少可核验的一手材料"],
                )
            ],
            "hypothesis_generator": [
                hypotheses_response(
                    hypothesis("解释甲", category="non_agentic"),
                    hypothesis("解释乙", category="alternative"),
                )
            ],
            # 🔴 单元素脚本 → 之后每次调用都返回**完全相同**的响应
            "judgment_synthesizer": [
                judgment_response(
                    conclusion=fixed_conclusion,
                    unknowns=["缺少可核验的一手材料"],
                )
            ],
        }
    )

    outcome = await harness.run("这件事可能的解释是什么？")

    assert outcome.state is RoundState.COMPLETED
    # 停止原因清楚：任务书 §13.3 指定的字面量
    assert outcome.stop_reason == "NO_MARGINAL_COGNITIVE_GAIN"

    # 不超过最大循环次数
    budget = CognitiveBudget.for_depth(outcome.depth)
    assert outcome.metacognitive_loops <= budget.max_metacognitive_loops
    assert outcome.metacognitive_loops == budget.max_metacognitive_loops

    # 重复检测确实触发了
    assert outcome.reflection is not None
    assert outcome.reflection.repeated_claim_score >= harness.settings.repetition_threshold
    assert outcome.reflection.new_evidence_present is False
    assert outcome.reflection.decision.value == "stop"

    # 循环确实跑了不止一轮（否则"停止"就不是防反刍的结果，而是第一次就停了）
    judgments = await harness.posted_events(outcome.cognitive_round_id, EventType.JUDGMENT_CREATED)
    assert len(judgments) == 2

    # 🔴 超预算认知回合率必须为 0
    assert outcome.model_calls_used <= budget.max_model_calls


# ---------------------------------------------------------------------------
# 场景 H：错误学习防护
# ---------------------------------------------------------------------------


async def test_scenario_h_single_experience_cannot_become_policy(harness: Harness) -> None:
    """场景 H：一次回答成功后模型提出全局策略。

    预期：
    * 创建 Experience；
    * **不满足全局提案门槛**；
    * **策略不得生效**。
    """
    outcome = await harness.run("请给我一个简单的建议。")

    # 回合正常结束并留下经验记录
    assert outcome.state is RoundState.COMPLETED
    experiences = await harness.posted_events(
        outcome.cognitive_round_id, EventType.EXPERIENCE_CREATED
    )
    assert len(experiences) == 1
    experience_payload = experiences[0].payload["experience"]
    assert experience_payload["cognitive_round_id"] == str(outcome.cognitive_round_id)
    # 阶段 3 尚不做错误归因，因此 error_type 为空
    assert experience_payload["error_type"] is None

    # 单次经验不足以升级为全局提案（不变量 10）
    experience_id = uuid4()
    proposal = ImprovementProposal(
        created_by="test",
        target_component="prompt:hypothesis_generator",
        observed_problem="希望把这次的成功做法推广到所有场景",
        error_class=ErrorType.REASONING_ERROR,
        supporting_experience_ids=[experience_id],
        proposed_change="把该做法写成通用策略",
        expected_benefit="似乎能提升质量",
    )
    assert PROPOSAL_ESCALATION_THRESHOLD == 3
    assert not proposal.meets_escalation_threshold()

    # 门槛不得低于 2——传 1 等于允许单次经验推广为全局策略
    with pytest.raises(ValueError, match="不得低于 2"):
        proposal.meets_escalation_threshold(threshold=1)

    # 三次同类经验才够格
    enough = proposal.model_copy(
        update={
            "supporting_experience_ids": [experience_id, uuid4(), uuid4()],
        }
    )
    assert enough.meets_escalation_threshold()

    # 🔴 提案永远不能自动生效（不变量 11）
    assert proposal.status is ProposalStatus.DRAFT
    assert proposal.can_become_active is False
    assert not proposal.status.is_terminal
    assert "active" not in {status.value for status in ProposalStatus}

    # 🔴 全局策略不得作为记忆写入：策略类型需要人工评审
    policy = WritePolicy()
    decision = policy.decide(
        MemoryWriteProposal(
            user_id=None,
            memory_type=MemoryType.STRATEGY,
            content="所有场景都应当先列出反证",
            sensitivity=SensitivityLevel.INTERNAL,
            user_confirmed=True,
        )
    )
    assert decision.decision is WriteDecision.REQUIRES_REVIEW
    assert not decision.allows_write


# ---------------------------------------------------------------------------
# 场景 I：模型输出结构损坏
# ---------------------------------------------------------------------------


async def test_scenario_i_malformed_output_fails_cleanly(harness_factory) -> None:
    """场景 I：Provider 返回缺失字段。

    预期：
    * 解析失败；
    * **有限重试**；
    * **不写入部分非法对象**；
    * 回合失败；
    * **事件日志完整**。
    """
    harness: Harness = harness_factory(
        responses={
            "inquiry_framer": [
                inquiry_response(
                    question="这件事的解释是什么",
                    depth=CognitiveDepth.D1,
                    key_unknowns=["缺少材料"],
                )
            ],
        },
        faults=[
            # 必须删一个**没有默认值**的字段：删 `confidence_band` 之类
            # 带默认值的字段，Schema 会把它补回来，故障根本不生效
            MockFault(
                task_name="judgment_synthesizer",
                mode="drop_field",
                field="judgment.conclusion",
                times=10,
            )
        ],
    )

    outcome = await harness.run("这件事的解释是什么？")

    # 回合失败，且**可诊断**（不变量 20）
    assert outcome.state is RoundState.FAILED
    assert outcome.stop_reason is None
    events = await harness.events(outcome.cognitive_round_id)
    failed = [event for event in events if event.event_type is EventType.COGNITIVE_ROUND_FAILED]
    assert len(failed) == 1
    assert failed[0].payload["failure_stage"] == "deliberate"
    assert failed[0].payload["error_category"] == "reasoning_error"

    # 🔴 没有部分非法对象被写入
    assert await harness.posted_events(outcome.cognitive_round_id, EventType.JUDGMENT_CREATED) == []

    # 有限重试：恰好 max_retries + 1 次
    calls = harness.provider.call_counts["judgment_synthesizer"]
    assert calls == harness.settings.llm_max_retries + 1

    # 事件日志完整：起始、观察、关切、问题、失败都在
    types = await harness.event_types(outcome.cognitive_round_id)
    assert EventType.COGNITIVE_ROUND_STARTED.value in types
    assert EventType.OBSERVATION_CREATED.value in types
    assert EventType.INQUIRY_CREATED.value in types
    assert EventType.COGNITIVE_ROUND_FAILED.value in types

    assert outcome.model_calls_used <= CognitiveBudget.for_depth(outcome.depth).max_model_calls


async def test_scenario_i_invalid_enum_is_rejected_then_recovered(harness_factory) -> None:
    """场景 I（变体）：非法枚举 → 拒绝且**不做模糊匹配**，重试后恢复。

    🔴 非法枚举值必须导致解析失败。把 ``high`` 之类的近似值"猜"成
    最接近的合法成员，会让一个错误静默通过——失败比猜测安全。
    """
    harness: Harness = harness_factory(
        responses={
            "inquiry_framer": [
                inquiry_response(
                    question="这件事的解释是什么",
                    depth=CognitiveDepth.D1,
                    key_unknowns=["缺少材料"],
                )
            ],
            "judgment_synthesizer": [
                judgment_response(conclusion="第一次调用返回非法枚举"),
                judgment_response(conclusion="重试后给出合法结论"),
            ],
        },
        faults=[
            MockFault(
                task_name="judgment_synthesizer",
                mode="invalid_enum",
                field="judgment.confidence_band",
                times=1,
            )
        ],
    )

    outcome = await harness.run("这件事的解释是什么？")

    assert outcome.state is RoundState.COMPLETED
    joined = await harness.posted_events(outcome.cognitive_round_id, EventType.JUDGMENT_CREATED)
    assert len(joined) == 1
    assert joined[0].payload["judgment"]["conclusion"] == "重试后给出合法结论"
    assert joined[0].model_info is not None
    assert joined[0].model_info.retry_count == 1


async def test_scenario_i_chain_of_thought_field_is_rejected(harness_factory) -> None:
    """场景 I（变体）：模型返回 ``reasoning`` 字段 → 被 Schema 拒绝。

    这是"不保存完整隐藏思维链"的第一道防线：
    输出 Schema 里根本不存在这个字段，``extra="forbid"`` 让它无法通过。
    """
    harness: Harness = harness_factory(
        responses={
            "inquiry_framer": [
                inquiry_response(
                    question="这件事的解释是什么",
                    depth=CognitiveDepth.D1,
                    key_unknowns=["缺少材料"],
                )
            ],
            "judgment_synthesizer": [
                judgment_response(conclusion="第一次带了思维链字段"),
                judgment_response(conclusion="重试后只返回结构化理由"),
            ],
        },
        faults=[
            MockFault(
                task_name="judgment_synthesizer",
                mode="forbidden_extra_field",
                times=1,
            )
        ],
    )

    outcome = await harness.run("这件事的解释是什么？")

    assert outcome.state is RoundState.COMPLETED
    events = await harness.posted_events(outcome.cognitive_round_id, EventType.JUDGMENT_CREATED)
    assert events[0].payload["judgment"]["conclusion"] == "重试后只返回结构化理由"
    # 思维链内容没有进入任何事件负载
    assert "思维链" not in str(events[0].payload)


async def test_budget_guard_skips_optional_modules(harness_factory) -> None:
    """预算不足以支撑全部模块时，**跳过的步骤必须被记录**，而不是静默消失。

    一次额外的重试会吃掉预算，于是元认知复核被跳过——
    这正是"循环永不超预算"在真实路径上的表现。
    """
    harness: Harness = harness_factory(
        responses={
            "inquiry_framer": [
                inquiry_response(
                    question="这件事的解释是什么",
                    depth=CognitiveDepth.D1,
                    key_unknowns=["缺少材料"],
                )
            ],
            "judgment_synthesizer": [
                judgment_response(conclusion="第一次调用返回非法枚举"),
                judgment_response(conclusion="重试后给出合法结论"),
            ],
        },
        faults=[
            MockFault(
                task_name="judgment_synthesizer",
                mode="invalid_enum",
                field="judgment.confidence_band",
                times=1,
            )
        ],
    )

    outcome = await harness.run("这件事的解释是什么？")

    assert outcome.state is RoundState.COMPLETED
    budget = CognitiveBudget.for_depth(outcome.depth)
    assert outcome.model_calls_used <= budget.max_model_calls
    assert any("预算不足" in item for item in outcome.skipped_steps)


# ---------------------------------------------------------------------------
# 场景 J：用户隔离
# ---------------------------------------------------------------------------


class RecordingMemory(InMemoryMemoryRepository):
    """记录每次检索所用作用域的记忆仓储。

    用它来断言"运行时确实按发起用户的作用域检索"，
    而不是只看检索结果——后者在数据恰好不重叠时也会通过。
    """

    def __init__(self) -> None:
        super().__init__()
        self.scopes: list[UUID | None] = []

    async def retrieve(self, *, user_id: UUID | None, query: str, limit: int) -> list[Memory]:
        self.scopes.append(user_id)
        return await super().retrieve(user_id=user_id, query=query, limit=limit)


async def test_scenario_j_cross_user_isolation(harness_factory) -> None:
    """场景 J：两个用户存在相似主题但不同私人记忆。

    预期：
    * **检索完全隔离**；
    * **不发生交叉引用**；
    * **日志中不泄露另一用户内容**。
    """
    user_a = uuid4()
    user_b = uuid4()
    content_a = "用户 A 的私人偏好：周末喜欢独自爬山"
    content_b = "用户 B 的私人偏好：周末喜欢独自看电影"

    memory = RecordingMemory()
    await memory.add(_active_memory(user_a, content_a))
    await memory.add(_active_memory(user_b, content_b))

    harness: Harness = harness_factory(
        memory=memory,
        responses={
            "inquiry_framer": [
                inquiry_response(
                    question="周末适合做什么",
                    depth=CognitiveDepth.D1,
                    key_unknowns=["不知道用户的具体偏好"],
                )
            ],
            "judgment_synthesizer": [
                judgment_response(
                    conclusion="这取决于你个人的偏好",
                    unknowns=["不知道用户的具体偏好"],
                )
            ],
        },
    )

    outcome = await harness.run("周末适合做什么？", user_id=user_a)
    assert outcome.state is RoundState.COMPLETED

    # 🔴 检索只以发起用户为作用域
    assert memory.scopes
    assert set(memory.scopes) == {user_a}
    assert user_b not in memory.scopes

    # 检索结果不含另一用户的记忆
    assert [item.id for item in await memory.retrieve(user_id=user_a, query="周末", limit=10)] == [
        m.id for m in await memory.list_for_user(user_id=user_a)
    ]

    # 另一用户的内容没有出现在本回合的任何产物或事件里
    assert outcome.response_text is not None
    assert content_b not in outcome.response_text
    for event in await harness.events(outcome.cognitive_round_id):
        serialized = str(event.payload)
        assert content_b not in serialized
        assert str(user_b) not in serialized


def _active_memory(user_id: UUID, content: str) -> Memory:
    """构造一条 ACTIVE 状态的用户偏好记忆。"""
    from datetime import UTC, datetime

    return Memory(
        created_by="test",
        user_id=user_id,
        memory_type=MemoryType.USER_PREFERENCE,
        content=content,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        status=MemoryStatus.ACTIVE,
        verification_status=VerificationStatus.SELF_REPORTED,
    )


async def test_scenario_j_confidence_band_is_ordinal() -> None:
    """补充断言：置信度是**分档**而不是百分比。

    模型不应伪造精确概率——``ConfidenceBand`` 没有浮点成员，
    这是结构性的，不靠提示词约定。
    """
    assert all(isinstance(band, ConfidenceBand) for band in ConfidenceBand)
    assert ConfidenceBand.VERY_LOW.rank < ConfidenceBand.HIGH.rank
    assert ConfidenceBand.HIGH.at_least(ConfidenceBand.MODERATE)
    assert not ConfidenceBand.LOW.at_least(ConfidenceBand.HIGH)


async def test_signals_for_depth_helper_covers_all_levels() -> None:
    """场景工具自身的完整性：五档深度信号都能构造出来。"""
    for depth in CognitiveDepth:
        signals = signals_for_depth(depth)
        assert set(signals) >= {
            "simple_fact_with_sufficient_evidence",
            "multiple_plausible_interpretations",
            "user_explicitly_philosophical",
            "value_conflict",
        }


# ---------------------------------------------------------------------------
# 阶段 4 回归：元认知裁定"换方法"时的预算保留
# ---------------------------------------------------------------------------


async def test_change_method_keeps_the_mandatory_tail(harness_factory) -> None:
    """元认知裁定 ``CHANGE_METHOD`` 时，回合**仍然必须拿得出结论**。

    🔴 这条用例来自阶段 4 跑真实回合时发现的缺陷：

    换方法会**再次**进入 ANALYZING，而那时上一次判断已经把保留额度
    降到了"只剩渲染"。于是一轮可选分析刚好把额度花到 0，
    接下来的判断合成与回答渲染都没有额度了——回合以
    ``BudgetExhaustedError`` 失败，用户什么也拿不到。

    正确行为：宁可少跑一个可选分析（跳过会被记录），也不能拿不出结论。
    """
    harness: Harness = harness_factory(
        responses={
            "inquiry_framer": [
                inquiry_response(
                    question="这件事可能的解释是什么",
                    depth=CognitiveDepth.D2,
                    key_unknowns=["缺少可核验的一手材料"],
                )
            ],
            "hypothesis_generator": [
                hypotheses_response(
                    hypothesis("解释甲", category="non_agentic"),
                    hypothesis("解释乙", category="alternative"),
                )
            ],
            "judgment_synthesizer": [
                judgment_response(
                    conclusion="目前只能给出暂定看法",
                    unknowns=["缺少可核验的一手材料"],
                )
            ],
            # 🔴 第一次复核就要求换方法——这正是会踩中该缺陷的路径
            "metacognition": [
                metacognition_response(
                    proposed="change_method",
                    reasons=["当前方法不足以推进"],
                )
            ],
        }
    )

    outcome = await harness.run("这件事可能的解释是什么？")

    # 回合必须拿出结论，而不是因为预算花光而失败
    assert outcome.state is RoundState.COMPLETED
    assert outcome.response_text
    assert outcome.stop_reason

    # 无论走了哪条路径，都不得超预算
    budget = CognitiveBudget.for_depth(outcome.depth)
    assert outcome.model_calls_used <= budget.max_model_calls


async def test_optional_module_failure_degrades_instead_of_failing_the_round(
    harness_factory,
) -> None:
    """可选分析模块失败时**降级**，而不是丢掉整个回合。

    🔴 这条用例来自阶段 4 跑真实模型时的实测：推理模型的输出偶尔会
    超过 ``max_tokens`` 被截断，于是 ``logical_analyzer`` 失败，
    整个回合跟着失败——用户明明只差最后一步就能拿到回答。

    任务书 §13.2 明确允许降级（"可返回当前认知服务降级一类的提示"），
    阶段 4 的验收条件也写着"解析异常不会污染状态"。
    可选模块本来就设计成"预算不够就跳过"，模型侧出问题应当同样处理。

    ⚠️ **降级必须留痕**：被跳过的步骤进入 ``skipped_steps`` 并落库，
    否则事后无法分辨"少做了一个分析"与"分析跑了但没产出"。
    """
    harness: Harness = harness_factory(
        responses={
            "inquiry_framer": [
                inquiry_response(
                    question="这件事可能的解释是什么",
                    depth=CognitiveDepth.D2,
                    key_unknowns=["缺少材料"],
                )
            ],
            "judgment_synthesizer": [
                judgment_response(conclusion="目前只能给出暂定看法", unknowns=["缺少材料"])
            ],
        },
        faults=[
            # logical_analyzer 永远返回非法结构 —— 重试也修不好
            MockFault(
                task_name="logical_analyzer",
                mode="invalid_enum",
                field="valid_links",
                times=99,
            )
        ],
    )

    outcome = await harness.run("这件事可能的解释是什么？")

    # 🔴 回合必须完成并给出回答
    assert outcome.state is RoundState.COMPLETED
    assert outcome.response_text
    assert outcome.stop_reason

    # 降级被记录，且指得出是哪一个步骤、为什么
    assert any(
        "logical_analysis" in item and "模型调用失败" in item for item in outcome.skipped_steps
    ), outcome.skipped_steps

    # 降级事实随终态事件落库（回放与审计都看得见）
    events = await harness.events(outcome.cognitive_round_id)
    terminal = [e for e in events if e.event_type is EventType.COGNITIVE_ROUND_COMPLETED]
    assert terminal
    assert any("logical_analysis" in str(item) for item in terminal[0].payload["skipped_steps"])

    # 无论走了哪条路径，都不得超预算
    assert outcome.model_calls_used <= CognitiveBudget.for_depth(outcome.depth).max_model_calls
