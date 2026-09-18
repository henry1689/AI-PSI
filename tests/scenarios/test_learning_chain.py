"""学习链路端到端（任务书 §11、阶段 6 验收条件二）。

🔴 **本文件要回答的是一个三位独立评审都指出的问题：**
**「三次同类错误可生成 Proposal」这条验收条件，在跑起来的系统里成立吗？**

阶段 6 初版把 `learning/` 六个模块写成了**没有上游的纯函数库**——
`src/` 里除了一处常量导入之外没有任何调用者，而运行时唯一产出的
`experience.created` 负载把 `error_type` 硬编码成 `None`、
`attribution_confidence` 硬编码成 `"very_low"`。

后果是：即便手工把运行时产出的经验喂给 `PatternDetector`，
它们也会因为"不可归因"被**全部过滤掉**——模式发现永远不会说话。

本文件因此**只用真实回合产出的事件**做这件事：
跑回合 → 读事件 → 还原 `Experience` → 模式发现 → 门槛裁决 → 生成提案。
中间不手工构造任何经验。
"""

from __future__ import annotations

from typing import Any

import pytest

from ai_psi.domain.enums import CognitiveDepth, ErrorType, EventType
from ai_psi.domain.experiences import Experience
from ai_psi.learning.pattern_detector import PatternDetector
from ai_psi.learning.promotion_policy import (
    PromotionEvidence,
    PromotionPolicy,
    PromotionTrigger,
)
from ai_psi.learning.proposal_generator import ProposalGenerator
from tests.scenarios.conftest import (
    Harness,
    hypotheses_response,
    hypothesis,
    inquiry_response,
    judgment_response,
    metacognition_response,
)

pytestmark = pytest.mark.scenario


def _scripted_round(*, missing_counterexample: bool) -> dict[str, Any]:
    """脚本化一次 D2 回合；元认知信号可控。

    三个回合用**完全相同**的脚本，让它们在
    ``(错误类别, 情境签名)`` 上落到同一组——那正是"同类错误"的定义。
    """
    return {
        "inquiry_framer": [
            inquiry_response(
                question="朋友的简短回复有哪些可能的解释",
                depth=CognitiveDepth.D2,
                key_unknowns=["朋友当时的处境"],
            )
        ],
        "hypothesis_generator": [
            hypotheses_response(
                hypothesis(
                    "朋友对用户有负面看法",
                    category="intentional",
                    falsification=["朋友在其它场合表现出持续的回避"],
                ),
                hypothesis(
                    "对方当时正在忙，无暇长回复",
                    category="non_agentic",
                    falsification=["对方当天有充裕的空闲时间"],
                ),
            )
        ],
        "judgment_synthesizer": [
            judgment_response(
                conclusion="仅凭一次简短回复，无法判断对方的意图",
                rationale=["单一观察不足以支撑人际推断"],
                counterarguments=["也可能只是当时不方便"],
                unknowns=["朋友当时的处境"],
                applicability=["仅基于这一次通信记录"],
            )
        ],
        "metacognition": [
            metacognition_response(
                proposed="stop",
                missing_counterexample_detected=missing_counterexample,
            )
        ],
    }


async def _experiences_from(harness: Harness, round_id: Any) -> list[Experience]:
    """从**真实回合**的事件流里还原经验——不做任何手工构造。"""
    events = await harness.posted_events(round_id, EventType.EXPERIENCE_CREATED)
    restored: list[Experience] = []
    for event in events:
        payload = dict(event.payload["experience"])
        # 事件负载里比 Experience 多两个审计字段，它们不属于领域对象
        payload.pop("stop_reason", None)
        payload.pop("attribution_reasons", None)
        restored.append(Experience.model_validate(payload))
    return restored


class TestTheRuntimeProducesAttributableExperiences:
    """🔴 学习链路**接上了**——这是它此前不成立的地方。"""

    async def test_a_quiet_round_is_recorded_but_not_attributed(self, harness_factory) -> None:
        """没有元认知信号时**不归因**——这是"判不了就不猜"的正常表现。"""
        harness: Harness = harness_factory(responses=_scripted_round(missing_counterexample=False))
        outcome = await harness.run("朋友今天只回复了一个「嗯」，他是不是讨厌我？")

        experiences = await _experiences_from(harness, outcome.cognitive_round_id)
        assert len(experiences) == 1
        assert experiences[0].error_type is None
        assert experiences[0].is_attributable is False

    async def test_a_metacognitive_signal_produces_a_real_attribution(
        self, harness_factory
    ) -> None:
        """🔴 元认知检出"关键反例被忽略" → 归为 ``reasoning_error``。

        这是阶段 6 之前不可能发生的事：那时 ``error_type`` 是硬编码的 ``None``。
        """
        harness: Harness = harness_factory(responses=_scripted_round(missing_counterexample=True))
        outcome = await harness.run("朋友今天只回复了一个「嗯」，他是不是讨厌我？")

        experiences = await _experiences_from(harness, outcome.cognitive_round_id)
        assert len(experiences) == 1
        assert experiences[0].error_type is ErrorType.REASONING_ERROR
        assert experiences[0].is_attributable is True

    async def test_the_event_carries_why_it_was_attributed(self, harness_factory) -> None:
        """🔴 只存 ``error_type``，理由就丢了——而不可解释的归因无法被推翻。"""
        harness: Harness = harness_factory(responses=_scripted_round(missing_counterexample=True))
        outcome = await harness.run("朋友今天只回复了一个「嗯」，他是不是讨厌我？")

        events = await harness.posted_events(
            outcome.cognitive_round_id, EventType.EXPERIENCE_CREATED
        )
        payload = events[0].payload["experience"]
        assert payload["attribution_reasons"]
        assert any("反例" in reason for reason in payload["attribution_reasons"])

    async def test_the_experience_has_an_id(self, harness_factory) -> None:
        """⚠️ 初版的事件负载里**没有经验 id**——

        那意味着提案的 ``supporting_experience_ids`` 根本无从引用它。
        """
        harness: Harness = harness_factory(responses=_scripted_round(missing_counterexample=True))
        outcome = await harness.run("朋友今天只回复了一个「嗯」，他是不是讨厌我？")

        events = await harness.posted_events(
            outcome.cognitive_round_id, EventType.EXPERIENCE_CREATED
        )
        assert events[0].payload["experience"]["id"]
        assert events[0].payload["experience"]["created_at"]

    async def test_the_recorded_evidence_is_the_evidence_at_the_time(self, harness_factory) -> None:
        """🔴 区分"当时判断错了"与"当时信息本就不足"的那个字段。

        本回合没有外部证据，因此它是**空列表**——而不是"没记录"。
        少了它，系统会把所有后来被推翻的判断都记成错误。
        """
        harness: Harness = harness_factory(responses=_scripted_round(missing_counterexample=True))
        outcome = await harness.run("朋友今天只回复了一个「嗯」，他是不是讨厌我？")

        experiences = await _experiences_from(harness, outcome.cognitive_round_id)
        assert experiences[0].evidence_available_at_time == []


class TestThreeRealRoundsCanProduceAProposal:
    """🔴 **阶段 6 验收条件二，在真实运行数据上的端到端证明。**

    中间不手工构造任何一条经验：三次同类错误全部来自真实回合的
    ``experience.created`` 事件。
    """

    async def test_end_to_end_from_real_rounds_to_a_draft_proposal(self, harness_factory) -> None:
        harness: Harness = harness_factory(responses=_scripted_round(missing_counterexample=True))

        collected: list[Experience] = []
        for _ in range(3):
            outcome = await harness.run("朋友今天只回复了一个「嗯」，他是不是讨厌我？")
            collected += await _experiences_from(harness, outcome.cognitive_round_id)

        assert len(collected) == 3
        # 三个不同的回合——门槛的计量单位是"在不同回合里发生过几次"
        assert len({item.cognitive_round_id for item in collected}) == 3

        # 1) 模式发现认出"同类错误发生了三次"
        patterns = PatternDetector().detect(collected)
        assert len(patterns) == 1
        assert patterns[0].count == 3
        assert patterns[0].error_type is ErrorType.REASONING_ERROR

        # 2) 门槛裁决放行
        decision = PromotionPolicy().decide(PromotionEvidence(pattern=patterns[0]))
        assert decision.allowed is True
        assert PromotionTrigger.REPEATED_SAME_ERROR in decision.triggers

        # 3) 生成提案——它引用的是**真实回合产出的**那几条经验
        proposal = ProposalGenerator().generate(pattern=patterns[0], decision=decision)
        assert proposal is not None
        assert proposal.status.value == "draft"
        assert set(proposal.supporting_experience_ids) == {item.id for item in collected}
        assert proposal.error_class is ErrorType.REASONING_ERROR

    async def test_two_real_rounds_are_not_enough(self, harness_factory) -> None:
        """🔴 不变量 10：两次不够。这条与上一条一起，才是门槛的两个方向。"""
        harness: Harness = harness_factory(responses=_scripted_round(missing_counterexample=True))

        collected: list[Experience] = []
        for _ in range(2):
            outcome = await harness.run("朋友今天只回复了一个「嗯」，他是不是讨厌我？")
            collected += await _experiences_from(harness, outcome.cognitive_round_id)

        assert PatternDetector().detect(collected) == []

    async def test_unattributed_rounds_never_reach_the_threshold(self, harness_factory) -> None:
        """判不了的经验不参与计数——跑多少次都不会"凑"出一个模式。"""
        harness: Harness = harness_factory(responses=_scripted_round(missing_counterexample=False))

        collected: list[Experience] = []
        for _ in range(3):
            outcome = await harness.run("朋友今天只回复了一个「嗯」，他是不是讨厌我？")
            collected += await _experiences_from(harness, outcome.cognitive_round_id)

        assert len(collected) == 3
        assert PatternDetector().detect(collected) == []
