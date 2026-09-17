"""长期记忆的应用服务（任务书 §10.1、§10.4、§10.5）。

🔴 **模型不能直接写记忆。** 所有写入都必须经过：

```
MemoryProposal → WritePolicy → 敏感性与作用域检查 → 重复与冲突检查 → 应用服务写入
```

这是 ADR-0004 的核心，也是不变量 14/15 的执行入口。

🔴 **阶段 5 起，记忆写入与它的审计事件在同一个事务里。**

阶段 3 的实现让"取代旧记忆 + 写入新记忆 + 写两条事件"跨在三个事务上，
用"先做可能失败的操作、后写事件"的顺序把风险压小（ADR-0015 §5）。
那只是压小，不是消除：事件写入失败会留下一次**改了却没记录**的纠正。
现在四步在同一个工作单元里，要么全成、要么全不成。

关于"用户纠正"（§10.4）：纠正**不修改旧记忆**，而是
**取代它并创建新版本**（不变量 5）。旧记录保留为 ``SUPERSEDED``，
因此"为什么发生过修正"永远可以追溯——而它不会再出现在默认检索里（不变量 6）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from ai_psi.application.ports import UnitOfWorkFactory
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
from ai_psi.memory.conflict_detection import (
    PotentialConflict,
    find_exact_duplicate,
    find_potential_conflicts,
)
from ai_psi.memory.redaction import (
    assert_audit_payload_safe,
    audit_payload_for_memory,
    export_bundle,
)
from ai_psi.memory.retrieval import cosine_similarity
from ai_psi.memory.write_policy import (
    MemoryWriteProposal,
    WriteDecision,
    WritePolicy,
    WritePolicyDecision,
)
from ai_psi.providers.embeddings import EmbeddingProvider

__all__ = [
    "MemoryCorrection",
    "MemoryDeletion",
    "MemoryExport",
    "MemoryService",
    "MemoryWriteOutcome",
    "UserDataDeletion",
]

#: 写入一条记忆时，向既有记忆检索的条数。
#:
#: 用于重复与冲突检查。刻意比检索路径小得多：这里要回答的是
#: "是不是已经记过同一件事"，只需要看最接近的少数几条。
_DUPLICATE_SCAN_LIMIT = 20


@dataclass(frozen=True, slots=True)
class MemoryWriteOutcome:
    """一次记忆写入请求的处理结果。

    Attributes:
        decision: 策略裁决。
        memory: 实际写入的记忆；未获批准时为 ``None``。
        reasons: 裁决理由。
        duplicate_of: 因内容完全相同而未写入时，指向既有记忆。
        potential_conflicts: 写入时观察到的疑似冲突线索。
            **只是线索**——它们进审计，不进 ``contradicts_ids``
            （见 :mod:`ai_psi.memory.conflict_detection`）。
    """

    decision: WriteDecision
    memory: Memory | None
    reasons: tuple[str, ...] = ()
    duplicate_of: UUID | None = None
    potential_conflicts: tuple[PotentialConflict, ...] = field(default=())

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


@dataclass(frozen=True, slots=True)
class MemoryExport:
    """一次用户数据导出。

    Attributes:
        payload: 可直接序列化的导出包（**含正文**：这是用户自己的数据）。
        audit_event: 本次导出留下的审计事件（**不含正文**）。
    """

    #: 值是任意 JSON 可序列化对象（嵌套列表/字典），因此用 ``Any``。
    payload: dict[str, Any]
    audit_event: Event


@dataclass(frozen=True, slots=True)
class MemoryDeletion:
    """一次单条删除的结果。

    🔴 把 ``memory`` 一并带回来，而不是只返回事件 id：
    调用方（API）需要知道删除**之后**的状态才能回应客户端，
    而"再去查一次"既是多余的往返，也可能读到与本次删除之间
    又发生了变化的数据。
    """

    memory: Memory
    audit_event: Event


@dataclass(frozen=True, slots=True)
class UserDataDeletion:
    """一次用户数据删除。

    Attributes:
        deleted_count: 被删除的记忆条数。
        memory_ids: 被删除的记忆 id。
        audit_events: 每条记忆留下的删除事件（**不含正文**）。
    """

    deleted_count: int
    memory_ids: tuple[UUID, ...] = ()
    audit_events: tuple[Event, ...] = ()


class MemoryService:
    """长期记忆的读写入口。"""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        embeddings: EmbeddingProvider,
        *,
        policy: WritePolicy | None = None,
    ) -> None:
        """初始化。

        Args:
            uow_factory: 工作单元工厂。记忆仓储从 ``uow.memories`` 取——
                **不能**另注入一个仓储实例：那样记忆写入与事件写入
                又会落到两个事务上，"原子"就成了句空话。
            embeddings: 向量 Provider，用于重复与冲突检查时的相似度计算。
            policy: 写入策略；``None`` 时使用默认策略（默认拒绝）。
        """
        self._uow_factory = uow_factory
        self._embeddings = embeddings
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
            await self._record_standalone(
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

        return await self._write(proposal, decision, actor_id=actor_id, correlation=correlation)

    async def _write(
        self,
        proposal: MemoryWriteProposal,
        decision: WritePolicyDecision,
        *,
        actor_id: str,
        correlation: UUID,
    ) -> MemoryWriteOutcome:
        """在**一个事务内**完成重复检查、写入与审计。

        重复检查必须与写入同事务：分成两次事务的话，两个并发的
        相同写入会各自查到"没有重复"然后都写进去——
        而重复检查存在的全部理由就是防这个。
        """
        async with self._uow_factory() as uow:
            candidates = await uow.memories.retrieve(
                user_id=proposal.user_id,
                query=proposal.content,
                limit=_DUPLICATE_SCAN_LIMIT,
            )
            duplicate = find_exact_duplicate(
                content=proposal.content,
                memory_type=proposal.memory_type,
                user_id=proposal.user_id,
                existing=candidates,
            )
            if duplicate is not None:
                # 完全相同的内容已经记过。**不写第二条**，也不静默成功——
                # 静默成功会让调用方以为产生了新记忆，而实际的记忆条数没变。
                payload = {
                    "memory_type": proposal.memory_type.value,
                    "decision": "duplicate",
                    "reasons": ["内容与既有记忆完全相同"],
                    "duplicate_of": str(duplicate.id),
                    "content_length": len(proposal.content),
                }
                assert_audit_payload_safe(payload, operation="memory_propose_duplicate")
                await uow.events.append(
                    self._event(
                        event_type=EventType.MEMORY_REJECTED,
                        user_id=proposal.user_id,
                        correlation_id=correlation,
                        actor_id=actor_id,
                        sensitivity=SensitivityLevel.INTERNAL,
                        payload=payload,
                    )
                )
                await uow.commit()
                return MemoryWriteOutcome(
                    decision=decision.decision,
                    memory=None,
                    reasons=("内容与既有记忆完全相同，未重复写入",),
                    duplicate_of=duplicate.id,
                )

            conflicts = await self._detect_conflicts(proposal=proposal, existing=candidates)

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
                # 🔴 向量版本必须与仓储实际使用的 Provider 一致，
                # 否则仓储会明确报错（见 memory_repository._embed）。
                embedding_version=self._embeddings.version,
            )
            await uow.memories.add(memory)
            payload = {
                "memory_id": str(memory.id),
                "memory_type": memory.memory_type.value,
                "decision": decision.decision.value,
                "reasons": list(decision.reasons),
                "embedding_version": self._embeddings.version,
                "potential_conflict_ids": [str(item.memory_id) for item in conflicts],
            }
            assert_audit_payload_safe(payload, operation="memory_propose_approved")
            await uow.events.append(
                self._event(
                    event_type=EventType.MEMORY_APPROVED,
                    user_id=proposal.user_id,
                    correlation_id=correlation,
                    actor_id=actor_id,
                    sensitivity=memory.sensitivity,
                    payload=payload,
                )
            )
            await uow.commit()

        return MemoryWriteOutcome(
            decision=decision.decision,
            memory=memory,
            reasons=decision.reasons,
            potential_conflicts=tuple(conflicts),
        )

    async def _detect_conflicts(
        self,
        *,
        proposal: MemoryWriteProposal,
        existing: list[Memory],
    ) -> list[PotentialConflict]:
        """计算待写入内容与既有记忆的相似度，挑出冲突线索。

        一次批量调用算完（待写入 + 全部候选），而不是每条算一次——
        向量 Provider 可能是外部服务，N+1 次调用既慢又贵。
        """
        if not existing:
            return []
        batch = await self._embeddings.embed(
            texts=[proposal.content, *(memory.content for memory in existing)]
        )
        if len(batch.vectors) != len(existing) + 1:
            return []
        probe = batch.vectors[0]
        scored = [
            (memory, cosine_similarity(probe, vector))
            for memory, vector in zip(existing, batch.vectors[1:], strict=True)
        ]
        return find_potential_conflicts(scored)

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

        async with self._uow_factory() as uow:
            existing = await uow.memories.get(memory_id)
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
                embedding_version=self._embeddings.version,
            )
            superseded = existing.superseded_by(replacement_id=replacement.id)

            await uow.memories.add(replacement)
            await uow.memories.save(superseded, expected_version=existing.version)

            correction_event = self._event(
                event_type=EventType.USER_CORRECTION_RECEIVED,
                user_id=user_id,
                correlation_id=correlation,
                actor_id=actor_id,
                sensitivity=existing.sensitivity,
                payload={
                    "corrected_memory_id": str(existing.id),
                    "replacement_memory_id": str(replacement.id),
                    # 🔴 不含被纠正内容的正文——审计信息不得保留正文
                    # （任务书 §10.5 的同一条原则同样适用于纠正）
                    "content_length": len(new_content),
                },
            )
            corrected_event = self._event(
                event_type=EventType.MEMORY_CORRECTED,
                user_id=user_id,
                correlation_id=correlation,
                actor_id=actor_id,
                sensitivity=replacement.sensitivity,
                payload={
                    "memory_id": str(replacement.id),
                    "supersedes_id": str(existing.id),
                    "status": replacement.status.value,
                    "embedding_version": self._embeddings.version,
                },
            )
            for event in (correction_event, corrected_event):
                assert_audit_payload_safe(event.payload, operation="memory_correct")
                await uow.events.append(event)
            await uow.commit()

        return MemoryCorrection(
            superseded=superseded,
            replacement=replacement,
            events=(correction_event, corrected_event),
        )

    # ------------------------------------------------------------------
    # 读取
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
        async with self._uow_factory() as uow:
            return await uow.memories.retrieve(user_id=user_id, query=query, limit=limit)

    async def conflicting_memory_ids(
        self,
        *,
        user_id: UUID | None,
        query: str,
        limit: int,
    ) -> frozenset[UUID]:
        """检索并返回结果集中**互相存在显式冲突**的记忆 id。

        供上层在回答里标注冲突（任务书 §5.11）——冲突要被呈现，
        而不是被排序掩盖。
        """
        async with self._uow_factory() as uow:
            memories = await uow.memories.retrieve(user_id=user_id, query=query, limit=limit)
        ids = {memory.id for memory in memories}
        return frozenset(
            memory.id for memory in memories if ids.intersection(memory.contradicts_ids)
        )

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
        async with self._uow_factory() as uow:
            return await uow.memories.list_for_user(
                user_id=user_id, include_inactive=include_inactive
            )

    # ------------------------------------------------------------------
    # 删除与导出（任务书 §10.5）
    # ------------------------------------------------------------------

    async def delete(
        self,
        *,
        user_id: UUID,
        memory_id: UUID,
        actor_id: str = "user",
        correlation_id: UUID | None = None,
    ) -> MemoryDeletion:
        """删除一条记忆（不变量 15）。

        🔴 审计事件**只记录 id 与动作，不记录被删除内容的正文**（任务书 §10.5）。

        Args:
            user_id: 发起删除的用户。
            memory_id: 目标记忆。
            actor_id: 发起者标识。
            correlation_id: 关联链标识。

        Returns:
            删除后的记忆状态与审计事件。

        Raises:
            NotFoundError: 记忆不存在，或不属于该用户。
        """
        async with self._uow_factory() as uow:
            existing = await uow.memories.get(memory_id)
            if existing is None or not existing.belongs_to(user_id):
                msg = f"记忆不存在：{memory_id}"
                raise NotFoundError(msg, context={"memory_id": str(memory_id)})

            event = self._event(
                event_type=EventType.MEMORY_EXPIRED,
                user_id=user_id,
                correlation_id=correlation_id or uuid4(),
                actor_id=actor_id,
                sensitivity=existing.sensitivity,
                payload=audit_payload_for_memory(existing, operation="memory_delete"),
            )
            await uow.memories.delete(memory_id)
            deleted = await uow.memories.get(memory_id)
            if deleted is None:  # pragma: no cover - 逻辑删除必然读得回来
                msg = f"删除后未能读回记忆：{memory_id}"
                raise NotFoundError(msg, context={"memory_id": str(memory_id)})
            await uow.events.append(event)
            await uow.commit()
        return MemoryDeletion(memory=deleted, audit_event=event)

    async def export_user_data(
        self,
        *,
        user_id: UUID,
        actor_id: str = "user",
        correlation_id: UUID | None = None,
    ) -> MemoryExport:
        """导出某用户的全部记忆（任务书 §10.5）。

        导出**包含被取代与已删除的记忆**：用户要"导出我的数据"时，
        他有权看到完整的版本链——"为什么发生过修正"正是靠这条链回答的。
        只导出有效记忆会让纠错痕迹凭空消失。

        导出本身也留下审计事件，且该事件**不含正文**——
        否则"导出"就成了把内容抄一份留在事件表里的后门。

        Args:
            user_id: 导出目标。
            actor_id: 发起者标识。
            correlation_id: 关联链标识。

        Returns:
            导出包与审计事件。
        """
        now = utc_now()
        async with self._uow_factory() as uow:
            memories = await uow.memories.list_for_user(user_id=user_id, include_inactive=True)
            payload: dict[str, object] = {
                "operation": "user_data_export",
                "user_id": str(user_id),
                "memory_count": len(memories),
                "active_count": sum(1 for memory in memories if memory.is_default_retrievable),
            }
            assert_audit_payload_safe(payload, operation="user_data_export")
            event = self._event(
                event_type=EventType.MEMORY_EXPORTED,
                user_id=user_id,
                correlation_id=correlation_id or uuid4(),
                actor_id=actor_id,
                sensitivity=SensitivityLevel.INTERNAL,
                payload=payload,
            )
            await uow.events.append(event)
            await uow.commit()

        bundle = export_bundle(user_id=user_id, memories=memories, now=now)
        return MemoryExport(payload=dict(bundle), audit_event=event)

    async def delete_user_data(
        self,
        *,
        user_id: UUID,
        actor_id: str = "user",
        correlation_id: UUID | None = None,
    ) -> UserDataDeletion:
        """删除某用户的全部记忆（任务书 §10.5）。

        每条记忆各留一条审计事件：删除是低频操作，而"这条记忆是什么时候
        被删掉的"是审计真正要回答的问题。一条汇总事件回答不了它，
        除非事后去扫描它的负载列表。

        Args:
            user_id: 目标用户。
            actor_id: 发起者标识。
            correlation_id: 关联链标识。

        Returns:
            删除结果与全部审计事件。

        Note:
            **只处理记忆。** 事件流、认知回合与判断不在删除范围内——
            它们是系统运行史，不是"用户的记忆"。把用户的删除请求
            扩大成"抹掉所有相关记录"，会与"事件只追加"这条根本约束冲突
            （ADR-0002），也会让审计变得不可能。这一点必须在 API 文档
            与导出结果里说清楚，不能靠用户自己猜。
        """
        correlation = correlation_id or uuid4()
        events: list[Event] = []
        deleted: list[UUID] = []

        async with self._uow_factory() as uow:
            memories = await uow.memories.list_for_user(user_id=user_id, include_inactive=True)
            for memory in memories:
                if memory.status is MemoryStatus.DELETED:
                    # 已经删过的不再重复删：重复的删除事件会让
                    # "什么时候删的"变得模糊（有多个时间点都声称是删除时刻）
                    continue
                event = self._event(
                    event_type=EventType.MEMORY_EXPIRED,
                    user_id=user_id,
                    correlation_id=correlation,
                    actor_id=actor_id,
                    sensitivity=memory.sensitivity,
                    payload=audit_payload_for_memory(memory, operation="user_data_deletion"),
                )
                assert_audit_payload_safe(event.payload, operation="user_data_deletion")
                await uow.memories.delete(memory.id)
                await uow.events.append(event)
                events.append(event)
                deleted.append(memory.id)
            await uow.commit()

        return UserDataDeletion(
            deleted_count=len(deleted),
            memory_ids=tuple(deleted),
            audit_events=tuple(events),
        )

    # ------------------------------------------------------------------
    # 维护
    # ------------------------------------------------------------------

    async def reindex(self, *, user_id: UUID) -> int:
        """把某用户过期的向量重新算一遍。

        🔴 **换向量 Provider 之后必须跑它。**

        检索只比对**同版本**的向量（防止拿两个不同语义空间的向量做比较），
        因此换 Provider 后所有旧向量会立刻全部失效——**旧记忆一条都检索不到**，
        而系统不会报任何错。这是一个"静默的全面失效"，
        必须有一个显式的动作把它修回来。

        Args:
            user_id: 目标用户。

        Returns:
            被重新索引的记忆条数。
        """
        rebuilt = 0
        async with self._uow_factory() as uow:
            memories = await uow.memories.list_for_user(user_id=user_id, include_inactive=True)
            for memory in memories:
                if not memory.is_default_retrievable:
                    continue
                if memory.embedding_version == self._embeddings.version:
                    continue
                await uow.memories.save(
                    memory.bumped(embedding_version=self._embeddings.version),
                    expected_version=memory.version,
                )
                rebuilt += 1
            await uow.commit()
        return rebuilt

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _record_standalone(
        self,
        *,
        event_type: EventType,
        user_id: UUID | None,
        correlation_id: UUID,
        actor_id: str,
        payload: Mapping[str, object],
        sensitivity: SensitivityLevel,
    ) -> Event:
        """写入一条独立的审计事件（不与任何记忆写入绑定）。"""
        event = self._event(
            event_type=event_type,
            user_id=user_id,
            correlation_id=correlation_id,
            actor_id=actor_id,
            sensitivity=sensitivity,
            payload=payload,
        )
        async with self._uow_factory() as uow:
            await uow.events.append(event)
            await uow.commit()
        return event

    def _event(
        self,
        *,
        event_type: EventType,
        user_id: UUID | None,
        correlation_id: UUID,
        actor_id: str,
        payload: Mapping[str, object],
        sensitivity: SensitivityLevel,
    ) -> Event:
        """构造一条记忆相关的审计事件对象（不落库）。"""
        return Event(
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
