"""记忆写入策略（任务书 §10.2、§10.3，ADR-0004）。

🔴 **默认拒绝。** 本文件的多数用例都在验证"什么不会被写进去"——
这与直觉相反，但符合代价不对称：漏记一条用户顶多重复说一遍，
写错一条会在此后每一轮里持续影响判断，而且用户很难发现。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from ai_psi.domain.enums import MemoryType, SensitivityLevel
from ai_psi.memory.write_policy import (
    FORBIDDEN_CONTENT_KEYWORDS,
    MemoryWriteProposal,
    WriteDecision,
    WritePolicy,
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
