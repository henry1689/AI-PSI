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

from ai_psi.application.proposal_service import ProposalEvaluation
from ai_psi.domain.enums import (
    CognitiveDepth,
    ErrorType,
    EvaluationVerdict,
    EventType,
    ExperienceEvaluation,
    FeedbackType,
)
from ai_psi.domain.experiences import Experience
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


async def _rounds_with_correction(harness: Harness, *, count: int, correction: bool) -> list[Any]:
    """跑 ``count`` 个真实回合，可选地给每个回合一条用户纠正。

    🔴 **纠正不是装饰。** 阶段 6.5 §二.6–7 起，只有被用户纠正、
    后续证据或独立评测抬到 ``SUPPORTED``/``CONFIRMED`` 的经验才计权；
    内部元认知单独产出的 ``SUSPECTED`` 权重为 0。
    因此"三次同类错误 → 提案"这条验收条件**必须**包含纠正这一步，
    少了它，测试证明的是一个在系统里不会发生的场景。
    """
    round_ids: list[Any] = []
    for _ in range(count):
        outcome = await harness.run("朋友今天只回复了一个「嗯」，他是不是讨厌我？")
        round_ids.append(outcome.cognitive_round_id)

    if correction:
        for round_id in round_ids:
            await harness.feedback_service.record(
                round_id=round_id,
                feedback_type=FeedbackType.CORRECTION,
                content="你这里判断错了：单一观察不该推出意图",
                actor_id="test_user",
            )
    return round_ids


class TestThreeRealRoundsCanProduceAProposal:
    """🔴 **阶段 6 验收条件二，在真实运行数据上的端到端证明。**

    中间不手工构造任何一条经验：三个回合全部真实跑过流水线，
    经验从 ``experience.created`` 事件里长出来，评价从
    ``experience.evaluated`` 事件里长出来，提案由**门禁**复核后落库。
    """

    @pytest.mark.parametrize("again", [False, True], ids=["首次运行", "重复运行"])
    async def test_end_to_end_from_real_rounds_to_a_draft_proposal(
        self, harness_factory, again: bool
    ) -> None:
        """三个独立回合 + 各自的用户纠正 → **恰好一条** DRAFT 提案。

        🔴 **阶段 6.6 起，提案是"自己出现"的。**

        第三次纠正提交之后，``FeedbackService`` 在**提交之后**触发一次
        学习运行——本用例**一次都没有调用过 `review()`**，
        提案却已经在库里。这正是本次唯一的交付目标。

        ``again=True`` 那一支再显式跑一次，断言的是**同一件事不会
        因为多跑一遍就多出一条提案**：第二次运行看到的必须是
        「已覆盖」，而不是又生成一条。
        """
        harness: Harness = harness_factory(responses=_scripted_round(missing_counterexample=True))
        round_ids = await _rounds_with_correction(harness, count=3, correction=True)

        if again:
            rerun = await harness.learning_service.review()
            assert rerun.created == (), "重复运行不该再造一条提案"
            assert len(rerun.already_covered) == 1, rerun.summary()

        listing = await harness.proposal_service.list_all()
        assert len(listing) == 1, f"期望恰好一条提案，实际 {len(listing)} 条"
        proposal = listing[0]
        assert proposal.status.value == "draft"
        assert proposal.error_class is ErrorType.REASONING_ERROR

        # 🔴 提案引用的经验必须**正是**那三个回合产出的那几条。
        # 情境签名不写死：它由深度、证据量、假设量派生，
        # 写死它会让"规则一改签名就变"变成一次无意义的红灯。
        produced: list[Experience] = []
        for round_id in round_ids:
            produced.extend(await _experiences_from(harness, round_id))
        assert set(proposal.supporting_experience_ids) == {item.id for item in produced}

        # ⚠️ 适用条件由**经验自己的签名**推出，而不是从 `run.patterns` 取：
        # 自动触发那一支根本没有 `run`。三个回合用的是同一份脚本，
        # 因此它们的签名必须相同——那正是"同类"的定义。
        signatures = {item.situation_signature for item in produced}
        assert len(signatures) == 1, signatures
        assert proposal.applicability == [next(iter(signatures))]

    async def test_the_third_correction_brings_the_proposal_into_being_by_itself(
        self, harness_factory
    ) -> None:
        """🔴 **P0：第三次纠正之后，提案自己出现——没有人调用过 `review()`。**

        这是阶段 6.6 唯一的交付目标。此前那条链路**只有**两个入口
        （CLI 与 `POST /learning/runs`），**都不是自动的**：
        第三次纠正到达后库里会有三条归因，而提案不会出现——
        那正是"数据可读但生产链路不会运行"的失败形态。

        本用例分三次**逐步**推进，而不是一次性跑三个回合再断言：
        中间那一步（两个回合时**不该**有提案）证明门槛仍然成立，
        少了它，"每来一次反馈就发一条提案"的实现也会全绿。
        """
        harness: Harness = harness_factory(responses=_scripted_round(missing_counterexample=True))
        assert await harness.proposal_service.list_all() == []

        await _rounds_with_correction(harness, count=2, correction=True)
        assert await harness.proposal_service.list_all() == [], "两个回合不该够门槛"

        # 第三次纠正：反馈提交之后自动跑一次学习链路
        await _rounds_with_correction(harness, count=1, correction=True)
        created = await harness.proposal_service.list_all()
        assert len(created) == 1, f"期望恰好一条提案，实际 {len(created)} 条"
        assert created[0].status.value == "draft"

    async def test_suspected_rounds_alone_never_reach_the_threshold(self, harness_factory) -> None:
        """🔴 **阶段 6.5 §二.6–7：三次内部怀疑不是三次证据。**

        这是本节最要紧的一条。阶段 6 的实现里，元认知怀疑三次就
        足以生成一条提案——**系统用自己的假设给自己发了通行证**。
        """
        harness: Harness = harness_factory(responses=_scripted_round(missing_counterexample=True))
        await _rounds_with_correction(harness, count=3, correction=False)

        run = await harness.learning_service.review()

        assert run.patterns == ()
        assert run.created == ()
        # 🔴 而且必须说清是**权重**挡下的，不是"经验不够多"——
        # 否则外部看起来与"历史上压根没这些经验"一模一样。
        reasons = [reason for _, items in run.suppressed for reason in items]
        assert any("权重" in reason for reason in reasons), reasons

    async def test_two_real_rounds_are_not_enough(self, harness_factory) -> None:
        """🔴 不变量 10：两次不够。这条与第一条一起，才是门槛的两个方向。

        ⚠️ 注意这里是**加了纠正**的两次——如果不加，
        它测的会是权重而不是次数。
        """
        harness: Harness = harness_factory(responses=_scripted_round(missing_counterexample=True))
        await _rounds_with_correction(harness, count=2, correction=True)

        run = await harness.learning_service.review()

        assert run.patterns == ()
        assert run.created == ()

    async def test_one_round_with_one_correction_is_still_one_occurrence(
        self, harness_factory
    ) -> None:
        """🔴 §八 评审 B 的反例：**1 个回合 + 1 次真实纠正**凑不出提案。

        修复前，评选用这条从**正式学习入口**落库过一条提案：
        同一个回合被抽取三次，每次填一个不同的 ``independence_group``，
        门禁就看到"独立发生 3 次"，而 ``data_quality``、经验条数、
        评价档位**全都正常**——那条路径上没有任何地方看得出来。

        修复后分组是派生值（``round:<回合>``）：同一回合的任意多次抽取
        落在同一个分组里，塌缩成一次发生。

        ⚠️ 本用例与单元层的"伪造的分组构造不出来"**不重复**。
        单元层证明的是对象建不出来；这里证明的是**正式入口那一侧
        也算不出三次**——两条链路的失败模式完全不同，
        一条挡住构造、一条挡住计数，缺任何一条，另一条都是纸糊的。
        """
        harness: Harness = harness_factory(responses=_scripted_round(missing_counterexample=True))
        round_ids = await _rounds_with_correction(harness, count=1, correction=True)

        # 前提一：这一回合**确实**产出了经验，而且它的分组就是那个回合。
        # 少了这条断言，「没有提案」会被"压根没有经验"冒充。
        experiences = await _experiences_from(harness, round_ids[0])
        assert len(experiences) == 1, [item.canonical_key for item in experiences]
        assert experiences[0].independence_group == f"round:{round_ids[0]}"

        run = await harness.learning_service.review()

        assert run.patterns == ()
        assert run.created == ()
        # 前提二：门禁看到的**发生次数是 1，不是 3**。
        # 一条真实纠正已经把这条经验抬到计权的档位（加权计数 1），
        # 所以这里唯一能挡下它的就是次数——而那句措辞里带着门禁
        # 实际数出来的数字，正是修复前会是 3 的那个位置。
        reasons = [reason for _, items in run.suppressed for reason in items]
        assert any("发生 1 次" in reason for reason in reasons), reasons
        assert not any("发生 3 次" in reason for reason in reasons), reasons

    async def test_unattributed_rounds_never_reach_the_threshold(self, harness_factory) -> None:
        """判不了的经验不参与计数——跑多少次、纠正多少次都不会"凑"出一个模式。"""
        harness: Harness = harness_factory(responses=_scripted_round(missing_counterexample=False))
        await _rounds_with_correction(harness, count=3, correction=True)

        run = await harness.learning_service.review()

        assert run.patterns == ()
        assert run.created == ()

    async def test_an_adjudicated_pattern_is_not_proposed_again(self, harness_factory) -> None:
        """🔴 被驳回过的模式**不会**在下一次运行里重新递上来。

        阶段 6.5 §四 修正了一处与自身文档矛盾的行为：
        `_covered_keys` 原本排除了终态，而"只有新经验会改变计数、
        情境签名没变"意味着一条被驳回的提案会**每次运行都重新生成**——
        恰好是那段代码声称要避免的"浪费评审的时间"。

        这条用例钉住修正后的语义：人做过决定的事，不再自动回到队列里。

        ⚠️ 代价也一并钉住：**即使此后又发生了很多次，也不会再提议。**
        V0.1 没有"重新开启"机制，因此这是"少打扰评审"与
        "漏掉新证据"之间的取舍——它是被选择过的，不是被忽略的。
        """
        harness: Harness = harness_factory(responses=_scripted_round(missing_counterexample=True))
        await _rounds_with_correction(harness, count=3, correction=True)

        # ⚠️ 阶段 6.6 起第三次纠正会**自动**跑一次学习链路，
        # 提案在 `_rounds_with_correction` 返回时就已经存在。
        # 这里取回来，而不是再调一次 `review()`——再调一次拿到的是
        # "已覆盖"，那是另一条断言（下面的 `second`）。
        listing = await harness.proposal_service.list_all()
        assert len(listing) == 1, listing
        proposal = listing[0]
        await harness.proposal_service.evaluate(
            proposal.id,
            evaluation=ProposalEvaluation(
                verdict=EvaluationVerdict.IMPROVED, evidence=("历史回放 200 回合",)
            ),
        )
        await harness.proposal_service.reject(
            proposal.id, rejected_by="评审", reason="代价大于收益"
        )

        second = await harness.learning_service.review()

        assert second.created == ()
        assert len(second.already_covered) == 1, second.summary()

    async def test_disagreement_alone_reaches_supported_not_confirmed(
        self, harness_factory
    ) -> None:
        """🔴 §二.5 的档位区分：只说"我不同意"抬到 ``SUPPORTED``。

        它仍然计权（``SUPPORTED`` 权重为 1），但评价里**不该**出现
        ``CONFIRMED``——用户没有指出哪里错了，把他的一句话升格成
        "确认"是替他把话说满了。
        """
        harness: Harness = harness_factory(responses=_scripted_round(missing_counterexample=True))
        round_ids: list[Any] = []
        for _ in range(3):
            outcome = await harness.run("朋友今天只回复了一个「嗯」，他是不是讨厌我？")
            round_ids.append(outcome.cognitive_round_id)
        for round_id in round_ids:
            await harness.feedback_service.record(
                round_id=round_id,
                feedback_type=FeedbackType.DISAGREEMENT,
                content="我不同意",
                actor_id="test_user",
            )

        run = await harness.learning_service.review()

        # ⚠️ 阶段 6.6 起第三次不同意已经在**提交之后**自动跑过一次，
        # 提案那时就生成了。这一次显式运行看到的是"已覆盖"——
        # 而模式本身仍在 `run.patterns` 里，档位断言因此照旧。
        assert len(run.already_covered) == 1, run.summary()
        assert run.patterns[0].evaluations == (ExperienceEvaluation.SUPPORTED,)
        assert (await harness.proposal_service.list_all())[0].status.value == "draft"
