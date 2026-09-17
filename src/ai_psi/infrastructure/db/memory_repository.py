"""长期记忆的 PostgreSQL + pgvector 实现（任务书 §5.11、§10，ADR-0017）。

与 :class:`~ai_psi.infrastructure.in_memory.memory_store.InMemoryMemoryRepository`
跑的是**同一组契约断言**（``tests/contract/base.py``）。

🔴 **本类不自行提交事务。** 记忆的写入与它产生的审计事件必须是
一次原子操作（``MemoryService.correct`` 要"取代旧记忆 + 写入新记忆 +
写两条事件"四步全成或全不成），因此本类由
:class:`~ai_psi.infrastructure.db.unit_of_work.SqlAlchemyUnitOfWork`
提供，事务边界归工作单元——这是架构规则 3 在阶段 5 的落实，
也闭合了 ADR-0015 §5 登记的"记忆写入与事件写入不在同一事务"。

**向量索引的维护是仓储的职责，不是调用方的。**
:meth:`add` / :meth:`save` / :meth:`delete` 各自负责让
``memory_embeddings`` 与 ``memories`` 保持一致。把它交给调用方，
就意味着"忘了同步索引"是一个随时可能发生的错误——
而它的后果是"删了还检索得到"（不变量 15）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_psi.cognition.constitution import assert_memory_retrievable_by
from ai_psi.domain.enums import MemoryStatus
from ai_psi.domain.exceptions import (
    ConflictError,
    NotFoundError,
    OptimisticLockError,
    ProviderError,
)
from ai_psi.domain.memories import Memory
from ai_psi.infrastructure.db.errors import is_unique_violation
from ai_psi.infrastructure.db.mappers import memory_to_row, memory_to_values, row_to_memory
from ai_psi.infrastructure.db.models import MemoryEmbeddingRow, MemoryRow
from ai_psi.memory.ranking import rank_candidates
from ai_psi.memory.retrieval import is_zero_vector, recall_size
from ai_psi.providers.embeddings import EmbeddingProvider

__all__ = ["SqlAlchemyMemoryRepository"]

#: 默认可检索的状态（不变量 6）。由枚举本身派生，不手工维护——
#: 手工维护的名单会在枚举新增成员时悄悄过期。
_RETRIEVABLE_STATUSES: tuple[str, ...] = tuple(
    status.value for status in MemoryStatus if status.is_default_retrievable
)


class SqlAlchemyMemoryRepository:
    """长期记忆仓储（PostgreSQL + pgvector）。

    Attributes:
        embeddings: 向量 Provider。写入时算向量，检索时算查询向量。
    """

    def __init__(self, session: AsyncSession, embeddings: EmbeddingProvider) -> None:
        """初始化。

        Args:
            session: 由工作单元管理的会话。
            embeddings: 向量 Provider。
        """
        self._session = session
        self.embeddings = embeddings

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def add(self, memory: Memory) -> None:
        """写入一条记忆并建立它的向量索引。

        🔴 **先算向量，再写库。** 向量计算是唯一可能慢、也可能失败的步骤
        （外部 Provider 会超时）。把它放在前面有两个好处：
        失败时事务里什么都没发生，不必回滚；而且数据库连接是在
        第一条 SQL 执行时才真正从池里取出来的，先做网络调用
        意味着不会攥着一条连接去做一件可能超时的事。

        Raises:
            ConflictError: 主键已存在。
            ProviderError: 向量计算失败。**记忆不会被写入**——
                "写进去了但检索不到"比"写入失败、用户重试"更糟：
                前者是一条用户永远找不回来的数据。
        """
        # 只有可检索的记忆才需要向量。为一条注定进不了索引的记忆
        # （比如刚写入就是 SUPERSEDED 的历史版本）算向量是纯浪费。
        vector = await self._embed(memory) if memory.is_default_retrievable else None

        self._session.add(memory_to_row(memory))
        try:
            await self._session.flush()
        except Exception as exc:
            if is_unique_violation(exc):
                msg = f"记忆已存在：{memory.id}"
                raise ConflictError(msg, context={"memory_id": str(memory.id)}) from exc
            raise
        if vector is not None:
            await self._write_index(memory.id, vector)

    async def save(self, memory: Memory, *, expected_version: int) -> None:
        """带乐观锁的更新，并同步向量索引。

        🔴 索引同步在这里是必须的：把一条记忆标记为 ``SUPERSEDED``
        或 ``DELETED`` 之后，它的向量**必须**从索引里消失。
        否则不变量 15 就只剩下"查询恰好带了状态过滤"这一个前提。

        Raises:
            OptimisticLockError: 版本不匹配。
            NotFoundError: 记忆不存在。
        """
        stmt = (
            update(MemoryRow)
            .where(MemoryRow.id == memory.id, MemoryRow.version == expected_version)
            .values(**memory_to_values(memory))
        )
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        if result.rowcount == 0:
            await self._raise_save_failure(memory.id, expected_version)
        await self._session.flush()
        await self._sync_index(memory)

    async def delete(self, memory_id: UUID) -> None:
        """删除一条记忆，**并同步作用于向量索引**（不变量 15）。

        V0.1 采用逻辑删除：主表保留（状态置为 ``DELETED``）以便追溯
        "为什么发生过修正"（任务书 §10.4），而**索引行是物理删除**——
        于是"删了还检索得到"在结构上不可能发生，而不是靠查询记得过滤。

        Raises:
            NotFoundError: 记忆不存在。
        """
        current = await self.get(memory_id)
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
        """按 id 读取记忆（**不区分作用域**，调用方负责校验）。"""
        row = await self._session.get(MemoryRow, memory_id)
        return None if row is None else row_to_memory(row)

    async def retrieve(
        self,
        *,
        user_id: UUID | None,
        query: str,
        limit: int,
    ) -> list[Memory]:
        """按作用域检索默认有效的记忆。

        🔴 过滤条件全部写在 SQL 的 ``WHERE`` 里，
        **绝不在取回之后过滤**：先取前 N 条再筛掉别人的记忆，
        会让"结果条数的变化"本身泄漏其他用户记忆的存在性。

        Raises:
            ScopeViolationError: 过滤逻辑失效，结果会包含他人的记忆。
        """
        candidates = await self._recall(user_id=user_id, query=query, limit=limit)
        if not candidates:
            return []

        ranked = rank_candidates(
            candidates,
            query=query,
            now=await self._now(),
            limit=limit,
        )
        results = [item.memory for item in ranked]

        # 🔴 防御性断言：过滤写错时立刻失败，而不是把别人的记忆交出去。
        # 与内存实现跑的是同一个断言。
        for memory in results:
            assert_memory_retrievable_by(memory, requesting_user_id=user_id)
        return results

    async def list_for_user(
        self,
        *,
        user_id: UUID,
        include_inactive: bool = False,
    ) -> list[Memory]:
        """列出某用户的记忆。

        Args:
            user_id: 目标用户。
            include_inactive: 是否包含已被取代/删除的记忆。
        """
        stmt = select(MemoryRow).where(MemoryRow.user_id == user_id)
        if not include_inactive:
            stmt = stmt.where(MemoryRow.status.in_(_RETRIEVABLE_STATUSES))
        stmt = stmt.order_by(MemoryRow.created_at.desc(), MemoryRow.id)
        rows = (await self._session.scalars(stmt)).all()
        return [row_to_memory(row) for row in rows]

    async def indexed_ids(self) -> set[UUID]:
        """返回当前向量索引中的记忆 id 集合。

        供不变量 15 的测试断言使用——**这是"删除是否真的传播到索引"
        唯一的直接证据**：查询结果为空可以有很多原因（过滤条件、
        相似度排序、limit），而这一个集合只回答"索引里还有没有它"。
        """
        return set((await self._session.scalars(select(MemoryEmbeddingRow.memory_id))).all())

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _recall(
        self,
        *,
        user_id: UUID | None,
        query: str,
        limit: int,
    ) -> list[tuple[Memory, float]]:
        """召回候选：``(记忆, 余弦相似度)``。

        查询向量为零（查询文本为空）时**不走向量路径**，
        退回按生效时间倒序取最近的那些。

        理由：零向量在 pgvector 里的余弦距离是未定义的，
        ``ORDER BY embedding <=> 'zero'`` 会得到 NaN，而 NaN 在排序里
        "排在哪都不确定"——结果集看起来正常，实际是随机的。
        与其返回一堆随机记忆，不如明确地返回"最近的那些"。
        """
        query_vector = (await self._embed_texts([query]))[0]
        vector_order = not is_zero_vector(query_vector)

        scope = MemoryRow.user_id.is_not_distinct_from(user_id)
        common_where = [scope, MemoryRow.status.in_(_RETRIEVABLE_STATUSES)]

        if not vector_order:
            stmt = (
                select(MemoryRow)
                .where(*common_where)
                .order_by(MemoryRow.valid_from.desc(), MemoryRow.id)
                .limit(recall_size(limit))
            )
            rows = (await self._session.scalars(stmt)).all()
            return [(row_to_memory(row), 0.0) for row in rows]

        distance = MemoryEmbeddingRow.embedding.cosine_distance(query_vector).label("distance")
        stmt = (
            select(MemoryRow, distance)
            .join(MemoryEmbeddingRow, MemoryEmbeddingRow.memory_id == MemoryRow.id)
            .where(
                *common_where,
                # 🔴 只比对**同版本**的向量。换模型后旧向量自动失效，
                # 而不是被拿去和一个语义空间已经不同的查询向量比较。
                MemoryEmbeddingRow.embedding_version == self.embeddings.version,
            )
            .order_by(distance, MemoryRow.id)
            .limit(recall_size(limit))
        )
        pairs = (await self._session.execute(stmt)).all()
        # pgvector 返回的是**余弦距离**（越小越近），转成相似度
        return [(row_to_memory(row), 1.0 - float(dist)) for row, dist in pairs]

    async def _sync_index(self, memory: Memory) -> None:
        """让向量索引与记忆的当前状态一致。

        * 记忆**可检索** → 确保索引行存在（缺了就补算向量）；
        * 记忆**不可检索** → 删除索引行。

        索引行存在且版本正确时不重算向量：``save`` 绝大多数调用
        只是改了状态或时间戳，为此重算一次向量既浪费又慢。
        （V0.1 中记忆正文一经写入就不再改动——纠正走的是新建版本，
        所以"内容变了但版本没变"这种情况不存在。）
        """
        if not memory.is_default_retrievable:
            await self._drop_index(memory.id)
            return

        existing = await self._session.get(MemoryEmbeddingRow, memory.id)
        if existing is not None and existing.embedding_version == self.embeddings.version:
            return
        vector = await self._embed(memory)
        await self._write_index(memory.id, vector)

    async def _write_index(self, memory_id: UUID, vector: list[float]) -> None:
        """插入或覆盖一条索引行。

        用 ``ON CONFLICT DO UPDATE`` 而不是"先删后插"：后者在
        ``save`` 路径上会短暂地让一条可检索的记忆从索引里消失，
        若此时事务被并发读取，就会看到一个不存在的中间态。
        """
        stmt = (
            pg_insert(MemoryEmbeddingRow)
            .values(
                memory_id=memory_id,
                embedding=vector,
                embedding_version=self.embeddings.version,
                provider=self.embeddings.name,
                created_at=await self._now(),
            )
            .on_conflict_do_update(
                index_elements=[MemoryEmbeddingRow.memory_id],
                set_={
                    "embedding": vector,
                    "embedding_version": self.embeddings.version,
                    "provider": self.embeddings.name,
                },
            )
        )
        await self._session.execute(stmt)
        await self._session.flush()

    async def _drop_index(self, memory_id: UUID) -> None:
        """物理删除索引行。"""
        await self._session.execute(
            delete(MemoryEmbeddingRow).where(MemoryEmbeddingRow.memory_id == memory_id)
        )
        await self._session.flush()

    async def _embed(self, memory: Memory) -> list[float]:
        """为一条记忆算向量，并校验版本声明一致。"""
        if (
            memory.embedding_version is not None
            and memory.embedding_version != self.embeddings.version
        ):
            msg = (
                f"记忆 {memory.id} 声明的向量版本 {memory.embedding_version!r} "
                f"与当前 Provider 的 {self.embeddings.version!r} 不一致。"
                "两者必须由同一个 Provider 产生——"
                "否则写进索引的向量与它在记忆上标注的版本对不上，"
                "而检索正是按版本筛选的"
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

    async def _raise_save_failure(self, memory_id: UUID, expected_version: int) -> None:
        """受影响行数为 0 时，判定真实原因并抛出对应异常。"""
        actual = await self._session.scalar(
            select(MemoryRow.version).where(MemoryRow.id == memory_id)
        )
        if actual is None:
            msg = f"记忆不存在：{memory_id}"
            raise NotFoundError(msg, context={"memory_id": str(memory_id)})

        msg = (
            f"乐观锁冲突：记忆 {memory_id} 期望版本 {expected_version}，"
            f"实际版本 {actual}。这说明期间有其他写入者提交了变更"
        )
        raise OptimisticLockError(
            msg,
            entity_type="Memory",
            entity_id=str(memory_id),
            expected_version=expected_version,
            actual_version=actual,
        )

    async def _now(self) -> datetime:
        """取数据库的当前时间。

        🔴 用 ``now()`` 而不是 Python 的 ``datetime.now()``：
        排序的"现在"如果来自应用进程，多个实例之间的时钟偏移会让
        同一批数据在不同的机器上排出不同的顺序。
        数据库时间是**一个**时钟。
        """
        value = await self._session.scalar(select(func.now()))
        if value is None:  # pragma: no cover - now() 永远不会返回 NULL
            msg = "数据库未返回当前时间"
            raise ProviderError(msg, code="database_clock_unavailable")
        return value
