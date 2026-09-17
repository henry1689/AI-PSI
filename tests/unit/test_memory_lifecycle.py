"""记忆的生命周期与重复/冲突检查（任务书 §10.1、§5.11）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ai_psi.domain.enums import MemoryStatus, MemoryType, RetentionPolicy
from ai_psi.domain.memories import Memory
from ai_psi.memory.conflict_detection import (
    find_exact_duplicate,
    find_potential_conflicts,
    normalize_for_comparison,
)
from ai_psi.memory.lifecycle import (
    due_for_expiry,
    is_expired,
    plan_expiry,
    retention_requirements,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _memory(
    content: str = "用户偏好简洁回答",
    *,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    valid_until: datetime | None = None,
    user_id: object = None,
    memory_type: MemoryType = MemoryType.USER_PREFERENCE,
    retention_policy: RetentionPolicy = RetentionPolicy.USER_CONTROLLED,
    **overrides: object,
) -> Memory:
    payload: dict[str, object] = {
        "created_by": "test",
        "memory_type": memory_type,
        "content": content,
        "valid_from": NOW - timedelta(days=10),
        "valid_until": valid_until,
        "status": status,
        "user_id": user_id,
        "retention_policy": retention_policy,
    }
    payload.update(overrides)
    return Memory(**payload)  # type: ignore[arg-type]


class TestExpiry:
    def test_no_valid_until_never_expires(self) -> None:
        assert not is_expired(_memory(), now=NOW)

    def test_past_valid_until_expires(self) -> None:
        memory = _memory(valid_until=NOW - timedelta(seconds=1))
        assert is_expired(memory, now=NOW)

    def test_future_valid_until_does_not_expire(self) -> None:
        memory = _memory(valid_until=NOW + timedelta(days=1))
        assert not is_expired(memory, now=NOW)

    @pytest.mark.parametrize(
        "status",
        [MemoryStatus.SUPERSEDED, MemoryStatus.DELETED, MemoryStatus.EXPIRED],
    )
    def test_terminal_statuses_are_never_re_expired(self, status: MemoryStatus) -> None:
        """🔴 已处于终态的记忆不再判定过期。

        一条被用户删除的记忆，把它的状态再改写成 ``EXPIRED`` 会**覆盖掉
        "用户主动删除"这个更有信息量的状态**——而"为什么它不在了"
        正是事后唯一要回答的问题。
        """
        memory = _memory(status=status, valid_until=NOW - timedelta(days=1))
        assert not is_expired(memory, now=NOW)
        plan = plan_expiry(memory, now=NOW)
        assert not plan.should_expire
        assert "已是终态" in plan.reason

    def test_plan_reason_names_the_fact_not_the_conclusion(self) -> None:
        """理由必须是**具体事实**，而不是"过期了"这种同义反复。"""
        memory = _memory(valid_until=NOW - timedelta(minutes=5))
        expiry = memory.valid_until
        assert expiry is not None
        plan = plan_expiry(memory, now=NOW)
        assert plan.should_expire
        assert expiry.isoformat() in plan.reason

    def test_due_for_expiry_filters_and_is_deterministic(self) -> None:
        alive = _memory()
        due_a = _memory(valid_until=NOW - timedelta(days=1))
        due_b = _memory(valid_until=NOW - timedelta(days=2))
        plans = due_for_expiry([due_a, alive, due_b], now=NOW)
        assert [plan.memory_id for plan in plans] == sorted([due_a.id, due_b.id], key=str)

    def test_due_for_expiry_on_empty(self) -> None:
        assert due_for_expiry([], now=NOW) == []


class TestRetentionRequirements:
    @pytest.mark.parametrize("policy", list(RetentionPolicy))
    def test_every_policy_has_an_explanation(self, policy: RetentionPolicy) -> None:
        assert retention_requirements(_memory(retention_policy=policy))

    def test_session_policy_admits_it_is_not_enforced(self) -> None:
        """🔴 不描述没有实现的规则。

        ``SESSION`` 与 ``UNTIL_SUPERSEDED`` 在 V0.1 里没有区别对待——
        按会话失效需要"会话"这个并不存在的一等对象。
        导出是给用户看的，在这里说一句做不到的话，
        用户会据此做出错误的预期。
        """
        note = retention_requirements(_memory(retention_policy=RetentionPolicy.SESSION))
        assert "尚未实现" in note


class TestNormalizeForComparison:
    def test_strips_whitespace_and_lowercases(self) -> None:
        assert normalize_for_comparison(" 用户 偏好 ABC  ") == "用户偏好abc"

    def test_punctuation_is_kept(self) -> None:
        """标点不归一——"是不是同一句话"必须是一个有确定答案的问题。"""
        assert normalize_for_comparison("好。") != normalize_for_comparison("好")


class TestFindExactDuplicate:
    def test_detects_the_same_content(self) -> None:
        user = uuid4()
        existing = _memory("用户偏好简洁回答", user_id=user)
        found = find_exact_duplicate(
            content="用户偏好简洁回答",
            memory_type=MemoryType.USER_PREFERENCE,
            user_id=user,
            existing=[existing],
        )
        assert found is not None
        assert found.id == existing.id

    def test_whitespace_differences_still_count_as_duplicate(self) -> None:
        user = uuid4()
        existing = _memory("用户 偏好 简洁回答", user_id=user)
        assert (
            find_exact_duplicate(
                content="用户偏好简洁回答",
                memory_type=MemoryType.USER_PREFERENCE,
                user_id=user,
                existing=[existing],
            )
            is not None
        )

    def test_different_memory_type_is_not_a_duplicate(self) -> None:
        """同一句话可以既是**偏好**又是**已确认事实**，它们是两件事。

        只看内容做去重会丢掉类型信息，而类型正是写策略与检索的依据。
        """
        user = uuid4()
        existing = _memory("用户偏好简洁回答", user_id=user, memory_type=MemoryType.SEMANTIC)
        assert (
            find_exact_duplicate(
                content="用户偏好简洁回答",
                memory_type=MemoryType.USER_PREFERENCE,
                user_id=user,
                existing=[existing],
            )
            is None
        )

    def test_different_scope_is_not_a_duplicate(self) -> None:
        """🔴 另一个用户的同句话不是重复。"""
        existing = _memory("用户偏好简洁回答", user_id=uuid4())
        assert (
            find_exact_duplicate(
                content="用户偏好简洁回答",
                memory_type=MemoryType.USER_PREFERENCE,
                user_id=uuid4(),
                existing=[existing],
            )
            is None
        )

    def test_no_candidates(self) -> None:
        assert (
            find_exact_duplicate(
                content="任意",
                memory_type=MemoryType.USER_PREFERENCE,
                user_id=None,
                existing=[],
            )
            is None
        )


class TestFindPotentialConflicts:
    def test_flags_similar_memories(self) -> None:
        memory = _memory("用户喜欢详细回答")
        findings = find_potential_conflicts([(memory, 0.6)], threshold=0.75)
        # 0.6 归一化后是 0.8，超过 0.75
        assert len(findings) == 1
        assert findings[0].memory_id == memory.id
        assert findings[0].similarity == pytest.approx(0.8)

    def test_ignores_dissimilar_memories(self) -> None:
        memory = _memory("今天股市大跌")
        assert find_potential_conflicts([(memory, 0.0)], threshold=0.75) == []

    def test_is_sorted_and_deterministic(self) -> None:
        first = _memory("A")
        second = _memory("B")
        findings = find_potential_conflicts([(first, 0.6), (second, 0.8)], threshold=0.5)
        assert [item.similarity for item in findings] == sorted(
            [item.similarity for item in findings], reverse=True
        )

    def test_reason_says_it_is_only_a_lead(self) -> None:
        """🔴 线索必须自称是线索。

        这条字符串是给事后排查的人看的：它要能解释
        "为什么系统认为这两条可能冲突"，并且明确它不是判定。
        """
        finding = find_potential_conflicts([(_memory("A"), 0.9)], threshold=0.5)[0]
        assert "线索" in finding.reason

    def test_empty_candidates(self) -> None:
        assert find_potential_conflicts([], threshold=0.5) == []
