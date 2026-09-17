"""长期记忆仓储的内存实现。

🔴 **本实现承载两条最关键的认知不变量，且与 PostgreSQL + pgvector 实现
跑的是同一组契约断言：**

* **不变量 14**：检索强制 ``user_id`` 作用域。返回结果逐条经过
  :func:`~ai_psi.cognition.constitution.assert_memory_retrievable_by`——
  这是**防御性断言**：过滤逻辑写错时它会立刻抛错，
  而不是安静地把别人的记忆交出去；
* **不变量 15**：删除必须同时作用于主表与检索索引。

**它现在是工作单元的一部分。** 阶段 3 时它是独立 Port（每个方法自带一个
事务），代价是"取代旧记忆 + 写入新记忆 + 记录事件"跨在三个事务上
（ADR-0015 §5 登记的边界）。阶段 5 把它挂进
:class:`~ai_psi.infrastructure.in_memory.unit_of_work.InMemoryUnitOfWork`，
四步变一个事务；PostgreSQL 侧同步做了同样的改造。

**向量索引与记忆本体分开存放**（:class:`~ai_psi.infrastructure.in_memory.store.MemoryIndexEntry`）。
把它们合成一个对象，会让"逻辑删除但保留审计"与"物理移除索引"
这两个要求直接冲突。

⚠️ 排序在 :mod:`ai_psi.memory.ranking` 里，两个实现共用——
让两边各写一份排序，"同一查询给出不同结果"就会成为契约测试
**看不到**的那类差异。
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from ai_psi.cognition.constitution import assert_memory_retrievable_by
from ai_psi.domain.common import utc_now
from ai_psi.domain.enums import MemoryStatus
from ai_psi.domain.exceptions import (
    ConflictError,
    NotFoundError,
    OptimisticLockError,
    ProviderError,
)
from ai_psi.domain.memories import Memory
from ai_psi.infrastructure.in_memory.store import MemoryIndexEntry
from ai_psi.memory.ranking import rank_candidates
from ai_psi.memory.retrieval import cosine_similarity, is_zero_vector, recall_size
from ai_psi.providers.embeddings import EmbeddingProvider

if TYPE_CHECKING:  # 运行期不导入，避免与工作单元互相引用
    from ai_psi.infrastructure.in_memory.unit_of_work import InMemoryUnitOfWork

__all__ = ["InMemoryMemoryRepository"]


class InMemoryMemoryRepository:
    """长期记忆的内存实现。

    生命周期与 :class:`~ai_psi.infrastructure.in_memory.store.InMemoryStore`
    一致；读写都经由所属工作单元的暂存区，因此**未提交的写入在事务外不可见**。
    """

    def __init__(self, uow: InMemoryUnitOfWork, embeddings: EmbeddingProvider) -> None:
        """初始化。

        Args:
            uow: 所属工作单元。
            embeddings: 向量 Provider。
        """
        self._uow = uow
        self.embeddings = embeddings

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def add(self, memory: Memory) -> None:
        """写入一条记忆并建立它的向量索引。

        Raises:
            ConflictError: 主键已存在。
            ProviderError: 向量计算失败。记忆不会被写入。
        """
        if self._uow.visible_memory(memory.id) is not None:
            msg = f"记忆已存在：{memory.id}"
            raise ConflictError(msg, context={"memory_id": str(memory.id)})

        vector = await self._embed(memory) if memory.is_default_retrievable else None
        self._uow.stage_memory(memory)
        if vector is not None:
            self._uow.stage_index(memory.id, self._index_entry(vector))

    async def save(self, memory: Memory, *, expected_version: int) -> None:
        """带乐观锁的更新，并同步向量索引。

        Raises:
            NotFoundError: 记忆不存在。
            OptimisticLockError: 版本不匹配。
        """
        current = self._uow.visible_memory(memory.id)
        if current is None:
            msg = f"记忆不存在：{memory.id}"
            raise NotFoundError(msg, context={"memory_id": str(memory.id)})
        if current.version != expected_version:
            msg = (
                f"乐观锁冲突：记忆 {memory.id} 期望版本 {expected_version}，"
                f"实际版本 {current.version}。这说明期间有其他写入者提交了变更"
            )
            raise OptimisticLockError(
                msg,
                entity_type="Memory",
                entity_id=str(memory.id),
                expected_version=expected_version,
                actual_version=current.version,
            )

        self._uow.stage_memory(memory)
        await self._sync_index(memory)

    async def delete(self, memory_id: UUID) -> None:
        """删除一条记忆，并**同步作用于检索索引**（不变量 15）。

        V0.1 采用逻辑删除：本体保留（标记为 ``DELETED``）以便追溯
        "为什么发生过修正"（任务书 §10.4），索引侧则是真正的移除——
        所以它不会再出现在任何检索结果里。

        Raises:
            NotFoundError: 记忆不存在。
        """
        current = self._uow.visible_memory(memory_id)
        if current is None:
            msg = f"记忆不存在，无法删除：{memory_id}"
            raise NotFoundError(msg, context={"memory_id": str(memory_id)})
        await self.save(
            current.bumped(status=MemoryStatus.DELETED),
            expected_version=current.version,
        )

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    async def get(self, memory_id: UUID) -> Memory | None:
        """按 id 读取记忆。"""
        return self._uow.visible_memory(memory_id)

    async def retrieve(
        self,
        *,
        user_id: UUID | None,
        query: str,
        limit: int,
    ) -> list[Memory]:
        """按作用域检索默认有效的记忆。

        🔴 过滤发生在**打分之前**：先把不属于该作用域、或处于非默认可检索
        状态的记忆排除掉，再排序取前 N 条。顺序反过来（先取前 N 再过滤）
        会让"某个用户的高分记忆"挤掉本用户的低分记忆，
        从而在结果里形成可观测的信息泄漏。

        Raises:
            ScopeViolationError: 过滤逻辑失效，结果会包含他人的记忆。
        """
        query_vector = (await self._embed_texts([query]))[0]
        eligible = [
            memory
            for memory in self._uow.visible_memories()
            if memory.is_default_retrievable and memory.belongs_to(user_id)
        ]

        # 🔴 两段式：先召回 recall_size(limit) 条，再交由通用排序。
        # 直接对全部候选排序会与 SQL 实现产生**真实差异**——
        # SQL 侧没法把整张表拉回来算余弦，它只能先按向量取 Top-K。
        # 若内存实现全量参与排序，一个向量上排第 50、词面上完全匹配的
        # 记忆会在内存里胜出、在 Postgres 里落选。契约测试看不到这种差异，
        # 因为断言只覆盖"能不能查到"，不覆盖"为什么它排前面"。
        recalled = self._recall(query_vector=query_vector, eligible=eligible, limit=limit)
        ranked = rank_candidates(recalled, query=query, now=utc_now(), limit=limit)
        results = [item.memory for item in ranked]

        # 🔴 防御性断言：过滤写错时立刻失败，而不是把别人的记忆交出去
        for memory in results:
            assert_memory_retrievable_by(memory, requesting_user_id=user_id)
        return results

    async def list_for_user(
        self,
        *,
        user_id: UUID,
        include_inactive: bool = False,
    ) -> list[Memory]:
        """列出某用户的记忆。"""
        memories = [
            memory
            for memory in self._uow.visible_memories()
            if memory.belongs_to(user_id) and (include_inactive or memory.is_default_retrievable)
        ]
        memories.sort(key=lambda item: (-item.created_at.timestamp(), str(item.id)))
        return memories

    async def indexed_ids(self) -> set[UUID]:
        """返回当前向量索引中的记忆 id 集合。

        供不变量 15 的测试断言使用——它是"删除是否真的传播到索引"
        唯一的直接证据。
        """
        return set(self._uow.visible_index())

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _recall(
        self,
        *,
        query_vector: list[float],
        eligible: list[Memory],
        limit: int,
    ) -> list[tuple[Memory, float]]:
        """召回候选：``(记忆, 余弦相似度)``。

        与 SQL 实现采用同一条两段式规则，包括零向量的退化路径：
        查询文本为空时向量没有方向，按**生效时间倒序**取最近的那些，
        而不是让未定义的相似度参与排序。
        """
        size = recall_size(limit)
        if is_zero_vector(query_vector):
            ordered = sorted(
                eligible,
                key=lambda item: (-item.valid_from.timestamp(), str(item.id)),
            )
            return [(memory, 0.0) for memory in ordered[:size]]

        scored: list[tuple[Memory, float]] = []
        for memory in eligible:
            similarity = self._similarity(query_vector, memory)
            if similarity is not None:
                scored.append((memory, similarity))
        # 排序必须补 id 兜底：相似度相同的两条记忆若顺序不定，
        # 同一批数据在不同运行里会给出不同的召回集。
        scored.sort(key=lambda pair: (-pair[1], str(pair[0].id)))
        return scored[:size]

    def _similarity(self, query_vector: list[float], memory: Memory) -> float | None:
        """返回与查询向量的余弦相似度；索引中没有同版本向量时返回 ``None``。"""
        entry = self._uow.visible_index().get(memory.id)
        if entry is None or entry.embedding_version != self.embeddings.version:
            return None
        return cosine_similarity(query_vector, entry.vector)

    async def _sync_index(self, memory: Memory) -> None:
        """让向量索引与记忆的当前状态一致。"""
        if not memory.is_default_retrievable:
            self._uow.stage_index(memory.id, None)
            return

        entry = self._uow.visible_index().get(memory.id)
        if entry is not None and entry.embedding_version == self.embeddings.version:
            return
        vector = await self._embed(memory)
        self._uow.stage_index(memory.id, self._index_entry(vector))

    def _index_entry(self, vector: list[float]) -> MemoryIndexEntry:
        """按当前 Provider 构造索引项。"""
        return MemoryIndexEntry(
            vector=vector,
            embedding_version=self.embeddings.version,
            provider=self.embeddings.name,
        )

    async def _embed(self, memory: Memory) -> list[float]:
        """为一条记忆算向量，并校验版本声明一致。"""
        if (
            memory.embedding_version is not None
            and memory.embedding_version != self.embeddings.version
        ):
            msg = (
                f"记忆 {memory.id} 声明的向量版本 {memory.embedding_version!r} "
                f"与当前 Provider 的 {self.embeddings.version!r} 不一致"
            )
            raise ProviderError(msg, code="embedding_version_mismatch")
        return (await self._embed_texts([memory.content]))[0]

    async def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        """调用向量 Provider。"""
        batch = await self.embeddings.embed(texts=texts)
        if len(batch.vectors) != len(texts):
            msg = f"向量 Provider 返回了 {len(batch.vectors)} 条向量，而输入是 {len(texts)} 条"
            raise ProviderError(msg, code="embedding_count_mismatch")
        return batch.vectors
