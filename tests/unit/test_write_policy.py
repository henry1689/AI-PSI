"""记忆写入策略（任务书 §10.2、§10.3，ADR-0004）。

🔴 **默认拒绝。** 本文件的多数用例都在验证"什么不会被写进去"——
这与直觉相反，但符合代价不对称：漏记一条用户顶多重复说一遍，
写错一条会在此后每一轮里持续影响判断，而且用户很难发现。
"""

from __future__ import annotations

import dataclasses
from uuid import uuid4

import pytest

from ai_psi.domain.enums import MemoryType, SensitivityLevel
from ai_psi.memory.write_policy import (
    FORBIDDEN_CONTENT_KEYWORDS,
    MemoryWriteProposal,
    WriteDecision,
    WritePolicy,
    WritePolicyDecision,
)

pytestmark = pytest.mark.unit


def _proposal(**overrides: object) -> MemoryWriteProposal:
    payload: dict[str, object] = {
        "user_id": uuid4(),
        "memory_type": MemoryType.USER_PREFERENCE,
        "content": "用户偏好先给结论再给理由",
        "sensitivity": SensitivityLevel.PERSONAL,
        "user_confirmed": True,
    }
    payload.update(overrides)
    return MemoryWriteProposal(**payload)  # type: ignore[arg-type]


class TestApproved:
    def test_confirmed_preference_is_approved(self) -> None:
        decision = WritePolicy().decide(_proposal())
        assert decision.decision is WriteDecision.APPROVED
        assert decision.allows_write

    def test_episode_summary_is_approved_without_confirmation(self) -> None:
        """已完成回合的事件摘要是可自动记录的回合事实（§10.2）。"""
        decision = WritePolicy().decide(
            _proposal(memory_type=MemoryType.EPISODIC, user_confirmed=False)
        )
        assert decision.decision is WriteDecision.APPROVED

    def test_failure_case_is_approved(self) -> None:
        """系统自身的错误记录同样可自动写入。"""
        decision = WritePolicy().decide(
            _proposal(memory_type=MemoryType.FAILURE_CASE, user_confirmed=False)
        )
        assert decision.decision is WriteDecision.APPROVED

    def test_every_approved_decision_explains_itself(self) -> None:
        decision = WritePolicy().decide(_proposal())
        assert decision.reasons


class TestRejected:
    @pytest.mark.parametrize(
        "content",
        [
            "用户可能有抑郁症",
            "用户属于那种回避型人格类型",
            "他支持某个党派",
            "用户的宗教信仰是……",
            "他的住址在某某小区",
            "推理过程如下：首先……",
            "所有用户都应当先看反证",
            "这是一条心理诊断",
        ],
    )
    def test_forbidden_content_is_rejected(self, content: str) -> None:
        """§10.3 的禁止类别。**即使类型在白名单内也要拦。**"""
        decision = WritePolicy().decide(_proposal(content=content))
        assert decision.decision is WriteDecision.REJECTED
        assert decision.forbidden_class is not None

    def test_every_forbidden_class_is_enforced_somehow(self) -> None:
        """🔴 每个禁止类别都必须**有地方拦它**——要么关键词，要么结构。

        宪法里的类别清单是防线清单。清单上有一条而代码里没有任何地方
        实现它，就等于写了一条没人执行的规则——比不写更危险，
        因为它会让人以为已经拦住了。
        """
        from ai_psi.cognition.constitution import FORBIDDEN_MEMORY_CONTENT_CLASSES
        from ai_psi.memory.write_policy import STRUCTURALLY_ENFORCED_CLASSES

        covered = set(FORBIDDEN_CONTENT_KEYWORDS) | set(STRUCTURALLY_ENFORCED_CLASSES)
        assert covered == set(FORBIDDEN_MEMORY_CONTENT_CLASSES)

    def test_structural_classes_explain_their_mechanism(self) -> None:
        from ai_psi.memory.write_policy import STRUCTURALLY_ENFORCED_CLASSES

        for class_name, mechanism in STRUCTURALLY_ENFORCED_CLASSES.items():
            assert class_name
            assert mechanism

    def test_rejection_takes_precedence_over_whitelist(self) -> None:
        """顺序不能反：命中内容红线的请求不该因为走到白名单分支被放行。"""
        decision = WritePolicy().decide(
            _proposal(
                memory_type=MemoryType.EPISODIC,  # 在白名单内
                content="用户的健康状况不佳，患有某种疾病",
                user_confirmed=True,
            )
        )
        assert decision.decision is WriteDecision.REJECTED


class TestRequiresReview:
    @pytest.mark.parametrize(
        "memory_type",
        [MemoryType.STRATEGY, MemoryType.SELF_MODEL, MemoryType.SEMANTIC],
    )
    def test_review_required_types(self, memory_type: MemoryType) -> None:
        decision = WritePolicy().decide(_proposal(memory_type=memory_type))
        assert decision.decision is WriteDecision.REQUIRES_REVIEW
        assert not decision.allows_write

    def test_global_strategy_is_never_auto_written(self) -> None:
        """🔴 不变量 10：单次经验不得升级为全局策略。

        即使"用户确认"了也不行——它不是用户点一下就能成立的事。
        """
        decision = WritePolicy().decide(
            _proposal(
                memory_type=MemoryType.STRATEGY,
                content="在关系类问题上总是先给出非人格化解释",
                user_confirmed=True,
            )
        )
        assert decision.decision is WriteDecision.REQUIRES_REVIEW


class TestRequiresConfirmation:
    def test_user_goal_without_confirmation(self) -> None:
        decision = WritePolicy().decide(
            _proposal(memory_type=MemoryType.USER_GOAL, user_confirmed=False)
        )
        assert decision.decision is WriteDecision.REQUIRES_USER_CONFIRMATION

    def test_unconfirmed_preference_is_not_written(self) -> None:
        """偏好类记忆必须由用户说出，系统不得从行为反推。"""
        decision = WritePolicy().decide(_proposal(user_confirmed=False))
        assert decision.decision is WriteDecision.REQUIRES_USER_CONFIRMATION

    def test_sensitive_content_needs_confirmation(self) -> None:
        decision = WritePolicy().decide(
            _proposal(sensitivity=SensitivityLevel.SENSITIVE, user_confirmed=False)
        )
        assert decision.decision is WriteDecision.REQUIRES_USER_CONFIRMATION

    def test_sensitive_content_with_confirmation_still_needs_whitelist(self) -> None:
        decision = WritePolicy().decide(
            _proposal(
                memory_type=MemoryType.USER_PREFERENCE,
                sensitivity=SensitivityLevel.SENSITIVE,
                user_confirmed=True,
            )
        )
        assert decision.decision is WriteDecision.APPROVED

    def test_type_outside_whitelist_defaults_to_confirmation(self) -> None:
        decision = WritePolicy().decide(
            _proposal(memory_type=MemoryType.CONCEPTUAL, user_confirmed=False)
        )
        assert decision.decision is WriteDecision.REQUIRES_USER_CONFIRMATION

    def test_only_approved_allows_write(self) -> None:
        assert WriteDecision.APPROVED.allows_write
        assert not WriteDecision.REQUIRES_REVIEW.allows_write
        assert not WriteDecision.REQUIRES_USER_CONFIRMATION.allows_write
        assert not WriteDecision.REJECTED.allows_write


class TestThePolicyObjectsAreImmutableValues:
    """🔴 阶段 6.5 §六 的变异测试发现：这两个 dataclass 的
    ``frozen=True`` / ``slots=True`` **被改成 False 时全部存活**——
    即没有任何测试检查过它们的不可变性。

    `MemoryWriteProposal` 是一条**待裁决的请求**，裁决者拿到它之后
    再改它的内容，等于让"我批准了什么"与"实际写进去的是什么"分家。
    `WritePolicyDecision` 是**裁决结果**，它被就地改写意味着
    同一次裁决在传递途中可以变成另一个结论。
    """

    def test_the_proposal_cannot_be_mutated(self) -> None:
        proposal = _proposal()
        with pytest.raises(dataclasses.FrozenInstanceError):
            proposal.content = "改过的内容"  # type: ignore[misc]

    def test_the_decision_cannot_be_mutated(self) -> None:
        decision = WritePolicy().decide(_proposal())
        with pytest.raises(dataclasses.FrozenInstanceError):
            decision.decision = WriteDecision.APPROVED  # type: ignore[misc]

    def test_neither_has_an_instance_dict(self) -> None:
        """``slots=True``：多一个 ``__dict__`` 就多一条绕过冻结的路径。"""
        assert not hasattr(_proposal(), "__dict__")
        assert not hasattr(WritePolicy().decide(_proposal()), "__dict__")


class TestTheDefaultsAreWhatWeAgreedOn:
    """🔴 默认值也是契约的一部分。

    变异测试发现 ``user_confirmed: bool = False`` 被改成 ``True`` 时存活——
    因为所有用例都显式传了它。

    这个默认值的语义是：**"用户没有确认过"是默认状态**。
    反过来（默认已确认）会让每一条没有显式声明的写入提案
    都被当成"用户说过"，而"用户说过"正是这条策略要检查的东西。
    """

    def test_a_proposal_is_unconfirmed_by_default(self) -> None:
        proposal = MemoryWriteProposal(
            user_id=uuid4(),
            memory_type=MemoryType.USER_PREFERENCE,
            content="用户偏好先给结论再给理由",
            sensitivity=SensitivityLevel.PERSONAL,
        )
        assert proposal.user_confirmed is False
        # 而且是**真的**默认值在起作用：不带确认的偏好不得被自动写入
        assert WritePolicy().decide(proposal).allows_write is False

    def test_source_event_ids_default_to_empty(self) -> None:
        proposal = MemoryWriteProposal(
            user_id=None,
            memory_type=MemoryType.EPISODIC,
            content="某回合的摘要",
            sensitivity=SensitivityLevel.INTERNAL,
        )
        assert proposal.source_event_ids == ()


class TestConfirmationRequiredTypesAlwaysExplainTheRightReason:
    """🔴 变异测试发现的**真实缺口**，而它的形状与直觉不同。

    `decide` 里有一条分支：

    ```
    if memory_type in _CONFIRMATION_REQUIRED_TYPES and not proposal.user_confirmed:
        return REQUIRES_USER_CONFIRMATION（理由：需要用户明确确认）
    ...
    if memory_type not in AUTO_WRITABLE_MEMORY_TYPES:
        return REQUIRES_USER_CONFIRMATION（理由：不在自动写入白名单内）
    ```

    `_CONFIRMATION_REQUIRED_TYPES`（``USER_GOAL`` / ``USER_CONFIRMED_FACT``）
    与 `AUTO_WRITABLE_MEMORY_TYPES`（四个类型）**没有交集**，
    因此这两条分支**给出同样的裁决**——它们只在**理由**上不同。

    删掉那个 `not` 之后：

    * ``user_confirmed=True`` 的请求会掉进第一条分支，
      拿到"需要用户明确确认"这条**错误的理由**——
      而它明明已经确认过了；
    * 裁决结果不变。

    所以这个变异体**只在断言理由时才会被杀**。这不是测试的取巧：
    理由是操作员唯一读得到的东西，一条"让他再去确认一次"的提示
    会让他真的去问用户第二遍。
    """

    @pytest.mark.parametrize(
        "memory_type",
        [MemoryType.USER_CONFIRMED_FACT, MemoryType.USER_GOAL],
    )
    def test_already_confirmed_gets_the_whitelist_reason(
        self, memory_type: MemoryType
    ) -> None:
        """🔴 已经确认过的请求**不该**被要求再确认一次。"""
        decision = WritePolicy().decide(
            _proposal(memory_type=memory_type, user_confirmed=True)
        )
        joined = " ".join(decision.reasons)
        assert "白名单" in joined, decision.reasons
        assert "需要用户明确确认" not in joined, decision.reasons

    @pytest.mark.parametrize(
        "memory_type",
        [MemoryType.USER_CONFIRMED_FACT, MemoryType.USER_GOAL],
    )
    def test_unconfirmed_gets_the_confirmation_reason(self, memory_type: MemoryType) -> None:
        """反方向——两个方向一起才把那个 `not` 钉住。"""
        decision = WritePolicy().decide(
            _proposal(memory_type=memory_type, user_confirmed=False)
        )
        assert "需要用户明确确认" in " ".join(decision.reasons), decision.reasons


class TestAllowsWriteAgreesWithTheDecision:
    """🔴 `allows_write` 必须与 `decision` 一致——四个档位都要走一遍。

    变异测试发现 ``self is WriteDecision.APPROVED`` 被改成 ``==`` 或 ``<=``
    时存活。对枚举成员，``is`` 与 ``==`` 本就等价（**等价变异体**）；
    而 ``<=`` 之所以也存活，是因为四个成员的值按字典序排列时
    "approved" 恰好排在最前——``x <= APPROVED`` 对所有成员给出同样的答案。

    也就是说 ``<=`` 是一个**侥幸等价**的变异：它现在对，但只是
    因为值的大小写与字母顺序恰好如此。下面这组断言把每个档位
    的正反两向都钉住——`<=` 仍然杀不掉，但那是值排序造成的，
    记在 `mutation/report.md` 的等价变异说明里。
    """

    @pytest.mark.parametrize("decision", list(WriteDecision))
    def test_the_flag_matches_the_decision(self, decision: WriteDecision) -> None:
        result = WritePolicyDecision(decision=decision)
        assert result.allows_write is (decision is WriteDecision.APPROVED)

    def test_only_approved_allows_write(self) -> None:
        allowed = {item for item in WriteDecision if item.allows_write}
        assert allowed == {WriteDecision.APPROVED}
