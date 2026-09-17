"""长期记忆的应用服务（任务书 §10.1、§10.4、§10.5）。

🔴 **模型不能直接写记忆。** 所有写入都必须经过：

```
MemoryProposal → WritePolicy → 敏感性与作用域检查 → 应用服务写入
```

这是 ADR-0004 的核心，也是不变量 14/15 的执行入口。

关于"用户纠正"（§10.4）：纠正**不修改旧记忆**，而是
**取代它并创建新版本**（不变量 5）。旧记录保留为 ``SUPERSEDED``，
因此"为什么发生过修正"永远可以追溯——而它不会再出现在默认检索里（不变量 6）。

⚠️ **事务边界（阶段 3 的已知边界，登记于 ADR-0015）**：
:class:`~ai_psi.application.ports.MemoryRepository` 的每个方法自身是原子的，
但它与事件写入不在同一个事务里。阶段 5 把记忆仓储纳入工作单元后闭合这一点。
当前实现用**先做可能失败的操作、后写事件**的顺序把风险压到最小。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from ai_psi.application.ports import MemoryRepository, UnitOfWorkFactory
from ai_psi.domain.common import utc_now
from ai_psi.domain.enums import (
    ActorType,
    EventType,
    MemoryStatus,
    RetentionPolicy,
    SensitivityLevel,
    VerificationStatus,
)
from ai_psi.domain.events import Event
from ai_psi.domain.exceptions import NotFoundError
from ai_psi.domain.memories import Memory
from ai_psi.memory.write_policy import MemoryWriteProposal, WriteDecision, WritePolicy

__all__ = ["MemoryCorrection", "MemoryService", "MemoryWriteOutcome"]


@dataclass(frozen=True, slots=True)
class MemoryWriteOutcome:
    """一次记忆写入请求的处理结果。

    Attributes:
        decision: 策略裁决。
        memory: 实际写入的记忆；未获批准时为 ``None``。
        reasons: 裁决理由。
    """

    decision: WriteDecision
    memory: Memory | None
    reasons: tuple[str, ...] = ()

    @property
    def written(self) -> bool:
        """是否真的写入了记忆。"""
        return self.memory is not None


@dataclass(frozen=True, slots=True)
class MemoryCorrection:
    """一次用户纠正的结果。

    Attributes:
        superseded: 被取代的旧记忆（状态已置为 ``SUPERSEDED``）。
        replacement: 新记忆。
        events: 本次纠正产生的事件。
    """

    superseded: Memory
    replacement: Memory
    events: tuple[Event, ...] = ()


class MemoryService:
    """长期记忆的读写入口。"""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        memory: MemoryRepository,
        *,
        policy: WritePolicy | None = None,
    ) -> None:
        """初始化。

        Args:
            uow_factory: 工作单元工厂（用于记录审计事件）。
            memory: 记忆仓储。
            policy: 写入策略；``None`` 时使用默认策略（默认拒绝）。
        """
        self._uow_factory = uow_factory
        self._memory = memory
        self._policy = policy if policy is not None else WritePolicy()

    @property
    def policy(self) -> WritePolicy:
        """当前使用的写入策略。"""
        return self._policy

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def propose(
        self,
        *,
        proposal: MemoryWriteProposal,
        actor_id: str = "memory_service",
        correlation_id: UUID | None = None,
    ) -> MemoryWriteOutcome:
        """处理一条记忆写入请求。

        🔴 **无论批准与否都会留下事件。**
        被拒绝的写入同样是审计信息——"系统曾经想记住什么但被挡住了"
        与"系统记住了什么"一样重要。

        Args:
            proposal: 写入提案。
            actor_id: 发起者标识。
            correlation_id: 关联链标识。

        Returns:
            处理结果。
        """
        decision = self._policy.decide(proposal)
        correlation = correlation_id or uuid4()

        if not decision.allows_write:
            await self._record(
                event_type=EventType.MEMORY_REJECTED,
                user_id=proposal.user_id,
                correlation_id=correlation,
                actor_id=actor_id,
                sensitivity=SensitivityLevel.INTERNAL,
                payload={
                    "memory_type": proposal.memory_type.value,
                    "decision": decision.decision.value,
                    "reasons": list(decision.reasons),
                    "forbidden_class": decision.forbidden_class,
                    # 🔴 记录**内容长度而非内容**：被拒绝的内容
                    # 往往正是最不该落库的那一类
                    "content_length": len(proposal.content),
                },
            )
            return MemoryWriteOutcome(
                decision=decision.decision, memory=None, reasons=decision.reasons
            )

        memory = Memory(
            created_by=actor_id,
            user_id=proposal.user_id,
            memory_type=proposal.memory_type,
            content=proposal.content,
            source_event_ids=list(proposal.source_event_ids),
            sensitivity=proposal.sensitivity,
            retention_policy=RetentionPolicy.USER_CONTROLLED,
            valid_from=utc_now(),
            status=MemoryStatus.ACTIVE,
            verification_status=(
                VerificationStatus.SELF_REPORTED
                if proposal.user_confirmed
                else VerificationStatus.UNVERIFIED
            ),
        )
        # 先做可能失败的一步（主键冲突等），再写审计事件——
        # 顺序反了会在冲突时留下一条"已批准"的记录，而记忆其实没写进去。
        await self._memory.add(memory)
        await self._record(
            event_type=EventType.MEMORY_APPROVED,
            user_id=proposal.user_id,
            correlation_id=correlation,
            actor_id=actor_id,
            sensitivity=memory.sensitivity,
            payload={
                "memory_id": str(memory.id),
                "memory_type": memory.memory_type.value,
                "decision": decision.decision.value,
                "reasons": list(decision.reasons),
            },
        )
        return MemoryWriteOutcome(
            decision=decision.decision, memory=memory, reasons=decision.reasons
        )

    # ------------------------------------------------------------------
    # 用户纠正（任务书 §10.4，场景 F）
    # ------------------------------------------------------------------

    async def correct(
        self,
        *,
        user_id: UUID,
        memory_id: UUID,
        new_content: str,
        actor_id: str = "user",
        correlation_id: UUID | None = None,
    ) -> MemoryCorrection:
        """按用户纠正创建记忆的新版本。

        🔴 **不变量 5：不就地覆盖。** 旧记忆被标记为 ``SUPERSEDED``，
        新记忆通过 ``supersedes_id`` 指向它。纠错痕迹完整保留。

        🔴 作用域检查：``memory_id`` 不属于 ``user_id`` 时抛
        :class:`~ai_psi.domain.exceptions.NotFoundError`（404 而不是 403）——
        "无权限"本身就会泄漏"这条记录存在"。

        Args:
            user_id: 发起纠正的用户。
            memory_id: 被纠正的记忆。
            new_content: 新的内容。
            actor_id: 发起者标识。
            correlation_id: 关联链标识。

        Returns:
            纠正结果。

        Raises:
            NotFoundError: 记忆不存在，或不属于该用户。
        """
        correlation = correlation_id or uuid4()

        existing = await self._memory.get(memory_id)
        if existing is None or not existing.belongs_to(user_id):
            msg = f"记忆不存在：{memory_id}"
            raise NotFoundError(msg, context={"memory_id": str(memory_id)})

        replacement = Memory(
            created_by=actor_id,
            user_id=existing.user_id,
            memory_type=existing.memory_type,
            content=new_content,
            source_event_ids=existing.source_event_ids,
            sensitivity=existing.sensitivity,
            retention_policy=existing.retention_policy,
            valid_from=utc_now(),
            # 🔴 版本链：新记忆指回旧记忆
            supersedes_id=existing.id,
            status=MemoryStatus.ACTIVE,
            verification_status=VerificationStatus.SELF_REPORTED,
        )
        superseded = existing.superseded_by(replacement_id=replacement.id)

        await self._memory.add(replacement)
        await self._memory.save(superseded, expected_version=existing.version)

        correction_event = await self._record(
            event_type=EventType.USER_CORRECTION_RECEIVED,
            user_id=user_id,
            correlation_id=correlation,
            actor_id=actor_id,
            sensitivity=existing.sensitivity,
            payload={
                "corrected_memory_id": str(existing.id),
                "replacement_memory_id": str(replacement.id),
                # 🔴 不含被纠正内容的正文——审计信息不得保留被删除内容
                # （任务书 §10.5 的同一条原则同样适用于纠正）
            },
        )
        corrected_event = await self._record(
            event_type=EventType.MEMORY_CORRECTED,
            user_id=user_id,
            correlation_id=correlation,
            actor_id=actor_id,
            sensitivity=replacement.sensitivity,
            payload={
                "memory_id": str(replacement.id),
                "supersedes_id": str(existing.id),
                "status": replacement.status.value,
            },
        )

        return MemoryCorrection(
            superseded=superseded,
            replacement=replacement,
            events=(correction_event, corrected_event),
        )

    # ------------------------------------------------------------------
    # 读取与删除
    # ------------------------------------------------------------------

    async def retrieve(
        self,
        *,
        user_id: UUID | None,
        query: str,
        limit: int,
    ) -> list[Memory]:
        """按作用域检索（🔴 不变量 14：``user_id`` 无默认值）。

        Args:
            user_id: 检索发起者。
            query: 检索文本。
            limit: 条数上限。

        Returns:
            默认有效的记忆。
        """
        return await self._memory.retrieve(user_id=user_id, query=query, limit=limit)

    async def list_for_user(
        self,
        *,
        user_id: UUID,
        include_inactive: bool = False,
    ) -> list[Memory]:
        """列出某用户的记忆。

        Args:
            user_id: 目标用户。
            include_inactive: 是否包含被取代/删除的记忆。
                用户查看"为什么被纠正"时需要 ``True``。

        Returns:
            记忆列表。
        """
        return await self._memory.list_for_user(user_id=user_id, include_inactive=include_inactive)

    async def delete(
        self,
        *,
        user_id: UUID,
        memory_id: UUID,
        actor_id: str = "user",
        correlation_id: UUID | None = None,
    ) -> Event:
        """删除一条记忆（不变量 15）。

        🔴 审计事件**只记录 id 与动作，不记录被删除内容的正文**（任务书 §10.5）。

        Args:
            user_id: 发起删除的用户。
            memory_id: 目标记忆。
            actor_id: 发起者标识。
            correlation_id: 关联链标识。

        Returns:
            删除事件。

        Raises:
            NotFoundError: 记忆不存在，或不属于该用户。
        """
        existing = await self._memory.get(memory_id)
        if existing is None or not existing.belongs_to(user_id):
            msg = f"记忆不存在：{memory_id}"
            raise NotFoundError(msg, context={"memory_id": str(memory_id)})

        await self._memory.delete(memory_id)
        return await self._record(
            event_type=EventType.MEMORY_EXPIRED,
            user_id=user_id,
            correlation_id=correlation_id or uuid4(),
            actor_id=actor_id,
            sensitivity=existing.sensitivity,
            payload={"memory_id": str(memory_id), "action": "deleted"},
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _record(
        self,
        *,
        event_type: EventType,
        user_id: UUID | None,
        correlation_id: UUID,
        actor_id: str,
        payload: dict[str, object],
        sensitivity: SensitivityLevel,
    ) -> Event:
        """写入一条记忆相关的审计事件。"""
        event = Event(
            event_type=event_type,
            occurred_at=utc_now(),
            actor_type=ActorType.SYSTEM,
            actor_id=actor_id,
            user_id=user_id,
            conversation_id=None,
            cognitive_round_id=None,
            correlation_id=correlation_id,
            payload=dict(payload),
            sensitivity=sensitivity,
        )
        async with self._uow_factory() as uow:
            await uow.events.append(event)
            await uow.commit()
        return event
