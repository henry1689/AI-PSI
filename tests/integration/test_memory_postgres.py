"""长期记忆的 PostgreSQL + pgvector 集成测试。

契约测试（``test_contract_postgres.py``）保证的是**两个实现语义一致**；
本文件保证的是**真实数据库上那些只有真实数据库才有的东西**：

* 向量列的实际维度与 ``NULL`` 语义；
* 不变量 15 的**物理证据**——索引行是不是真的被删掉了；
* 删除传播、外键级联、检索时的版本过滤；
* 零向量查询会不会让 pgvector 返回 NaN 进而毁掉排序。

🔴 **这些断言没有一条能靠内存实现或 Mock 覆盖。**
阶段 4 的教训是"Mock 对参数是否合理一无所知"；这里同理——
内存字典不会告诉你 pgvector 对零向量算不算得出来。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from ai_psi.domain.enums import MemoryStatus, MemoryType
from ai_psi.domain.exceptions import ProviderError
from ai_psi.domain.memories import Memory
from ai_psi.providers.embeddings import DEFAULT_EMBEDDING_DIMENSION, EmbeddingProvider

pytestmark = pytest.mark.integration


def _memory(
    *,
    user_id: UUID | None = None,
    content: str = "用户偏好简洁回答",
    status: MemoryStatus = MemoryStatus.ACTIVE,
    embedding_version: str | None = None,
) -> Memory:
    return Memory(
        created_by="test",
        user_id=user_id,
        memory_type=MemoryType.USER_PREFERENCE,
        content=content,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        status=status,
        embedding_version=embedding_version,
    )


class TestVectorColumn:
    async def test_stored_vector_has_the_migrated_dimension(
        self, uow_factory, session_factory, embeddings: EmbeddingProvider
    ) -> None:
        """🔴 写进去的向量维度必须与**迁移建出来的列**一致。

        这是"配置里的维度"与"数据库里的维度"之间唯一的连接点：
        两者不一致时，写入会被 PostgreSQL 直接拒绝——
        而那时错误信息来自数据库驱动，离真正的原因已经很远。
        """
        memory = _memory()
        async with uow_factory() as uow:
            await uow.memories.add(memory)
            await uow.commit()

        async with session_factory() as session:
            dims = await session.scalar(
                text("SELECT vector_dims(embedding) FROM memory_embeddings WHERE memory_id = :id"),
                {"id": memory.id},
            )
        assert dims == DEFAULT_EMBEDDING_DIMENSION
        assert dims == embeddings.dimension

    async def test_hnsw_index_exists_with_cosine_ops(self, session_factory) -> None:
        """余弦距离的 HNSW 索引必须真的建出来了。

        ``CREATE INDEX`` 写着 ``vector_cosine_ops`` 但建不出来（比如扩展没装）
        是一个"迁移看起来成功了"的静默失败——索引不在，
        查询照样能跑，只是每一次都是全表扫描。
        """
        async with session_factory() as session:
            definition = await session.scalar(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_memory_embeddings_hnsw'"
                )
            )
        assert definition is not None
        assert "hnsw" in definition
        assert "vector_cosine_ops" in definition


class TestDeletePropagation:
    async def test_delete_physically_removes_the_index_row(
        self, uow_factory, session_factory
    ) -> None:
        """🔴 不变量 15 的**结构性证据**。

        ``retrieve`` 返回空可以有很多原因（相似度、limit、状态过滤），
        任何一条都能让"删了还检索得到"这个缺陷**看起来是被修好的**。
        这里直接查索引表——它只有一个问题要回答：**那一行还在不在**。

        这就是把向量单独存一张表换来的东西：删除传播是一次
        真实执行过的 ``DELETE``，而不是"查询恰好带了状态过滤"。
        """
        memory = _memory()
        async with uow_factory() as uow:
            await uow.memories.add(memory)
            await uow.commit()
        assert await self._index_rows(session_factory, memory.id) == 1

        async with uow_factory() as uow:
            await uow.memories.delete(memory.id)
            await uow.commit()

        assert await self._index_rows(session_factory, memory.id) == 0
        # 本体保留：逻辑删除是为了追溯"为什么发生过修正"
        async with uow_factory() as uow:
            stored = await uow.memories.get(memory.id)
        assert stored is not None
        assert stored.status is MemoryStatus.DELETED

    async def test_superseding_removes_the_old_vector(self, uow_factory, session_factory) -> None:
        """被取代的旧版本同样要从索引里消失（不变量 6 的物理保证）。"""
        old = _memory()
        async with uow_factory() as uow:
            await uow.memories.add(old)
            await uow.commit()

        replacement = _memory(content="用户喜欢详细回答", status=MemoryStatus.ACTIVE)
        async with uow_factory() as uow:
            await uow.memories.add(replacement)
            await uow.memories.save(
                old.superseded_by(replacement_id=replacement.id), expected_version=old.version
            )
            await uow.commit()

        assert await self._index_rows(session_factory, old.id) == 0
        assert await self._index_rows(session_factory, replacement.id) == 1

    async def test_physical_row_deletion_cascades_to_the_index(
        self, uow_factory, session_factory
    ) -> None:
        """外键上的 ``ON DELETE CASCADE`` 保证索引不会成为孤儿。

        应用层走的是逻辑删除，但"物理删掉主表行"这条路径仍然存在
        （手工清理、未来的留存策略）。少了级联，残留的向量会永远
        占据索引位置，并且指向一条不存在的记忆。
        """
        memory = _memory()
        async with uow_factory() as uow:
            await uow.memories.add(memory)
            await uow.commit()

        async with session_factory() as session:
            await session.execute(text("DELETE FROM memories WHERE id = :id"), {"id": memory.id})
            await session.commit()

        assert await self._index_rows(session_factory, memory.id) == 0

    @staticmethod
    async def _index_rows(session_factory, memory_id: UUID) -> int:
        async with session_factory() as session:
            count = await session.scalar(
                text("SELECT count(*) FROM memory_embeddings WHERE memory_id = :id"),
                {"id": memory_id},
            )
        return int(count or 0)


class TestRetrieval:
    async def test_scope_filter_happens_in_sql(self, uow_factory) -> None:
        """🔴 两个用户主题相同但内容不同，检索完全隔离。"""
        mine, theirs = uuid4(), uuid4()
        async with uow_factory() as uow:
            await uow.memories.add(_memory(user_id=mine, content="我的偏好：周末喜欢独自爬山"))
            await uow.memories.add(_memory(user_id=theirs, content="他的偏好：周末喜欢独自爬山"))
            await uow.commit()

        async with uow_factory() as uow:
            results = await uow.memories.retrieve(user_id=mine, query="周末喜欢独自爬山", limit=10)
        assert [item.user_id for item in results] == [mine]

    async def test_system_scope_memories_are_found(self, uow_factory) -> None:
        """🔴 ``user_id IS NULL``（系统级记忆）必须查得出来。

        用 ``WHERE user_id = :uid`` 而不是 ``IS NOT DISTINCT FROM``
        会让系统级记忆**一条都查不到**——而这个错误不会报任何错，
        只会让用户觉得"系统什么都没记住"。
        """
        async with uow_factory() as uow:
            await uow.memories.add(_memory(user_id=None, content="系统级：当前使用中文回答"))
            await uow.commit()

        async with uow_factory() as uow:
            results = await uow.memories.retrieve(user_id=None, query="中文回答", limit=10)
        assert len(results) == 1
        assert results[0].user_id is None

    async def test_system_scope_does_not_leak_into_a_user_scope(self, uow_factory) -> None:
        """系统级记忆与某个用户的作用域**不是同一个作用域**。"""
        async with uow_factory() as uow:
            await uow.memories.add(_memory(user_id=None, content="系统级：当前使用中文回答"))
            await uow.commit()

        async with uow_factory() as uow:
            results = await uow.memories.retrieve(user_id=uuid4(), query="中文回答", limit=10)
        assert results == []

    async def test_superseded_memories_are_filtered_in_sql(self, uow_factory) -> None:
        user = uuid4()
        async with uow_factory() as uow:
            await uow.memories.add(
                _memory(user_id=user, status=MemoryStatus.SUPERSEDED, content="旧偏好")
            )
            await uow.memories.add(
                _memory(user_id=user, status=MemoryStatus.EXPIRED, content="过期偏好")
            )
            await uow.commit()

        async with uow_factory() as uow:
            assert await uow.memories.retrieve(user_id=user, query="偏好", limit=10) == []

    async def test_empty_query_does_not_produce_nan_ordering(self, uow_factory) -> None:
        """🔴 空查询绝不能走向量路径。

        空文本编码成零向量，而零向量在 pgvector 里的余弦距离是未定义的：
        ``ORDER BY embedding <=> 'zero'`` 会得到 NaN，而 NaN 在排序里
        "排在哪都不确定"——结果集看起来正常，实际是随机的。

        这里要求的是**明确退化成"最近的那些"**，而不是一堆随机记忆。
        """
        user = uuid4()
        async with uow_factory() as uow:
            await uow.memories.add(_memory(user_id=user, content="偏好一"))
            await uow.memories.add(_memory(user_id=user, content="偏好二"))
            await uow.commit()

        async with uow_factory() as uow:
            results = await uow.memories.retrieve(user_id=user, query="   ", limit=5)
        assert len(results) == 2
        assert all(item.user_id == user for item in results)

    async def test_zero_limit_returns_nothing(self, uow_factory) -> None:
        user = uuid4()
        async with uow_factory() as uow:
            await uow.memories.add(_memory(user_id=user))
            await uow.commit()
            assert await uow.memories.retrieve(user_id=user, query="偏好", limit=0) == []


class TestEmbeddingVersion:
    async def test_only_matching_versions_are_retrieved(self, uow_factory, session_factory) -> None:
        """🔴 换 Provider 之后，旧记忆会**静默地全部检索不到**。

        这是本阶段最难被发现的一条失效模式：写入照旧成功、
        `list_for_user` 照旧列得出来、系统不报任何错——
        只是 `retrieve` 永远返回空。用户会觉得"系统突然失忆了"，
        而日志里什么都看不到。

        测试用 SQL 直接改索引行的版本，模拟"这些向量是上一个
        Provider 写的"——这正是切换向量服务之后数据库里的真实样子，
        也顺带说明了为什么必须有 ``reindex`` 这个显式动作。
        """
        user = uuid4()
        async with uow_factory() as uow:
            await uow.memories.add(_memory(user_id=user, content="旧空间的记忆"))
            await uow.commit()

        async with session_factory() as session:
            await session.execute(
                text("UPDATE memory_embeddings SET embedding_version = 'some-old-space'")
            )
            await session.commit()

        async with uow_factory() as uow:
            assert await uow.memories.retrieve(user_id=user, query="空间", limit=10) == []
            # 但 list_for_user 仍然看得到——它不依赖向量
            listed = await uow.memories.list_for_user(user_id=user)
        assert [item.content for item in listed] == ["旧空间的记忆"]

    async def test_write_rejects_a_mismatched_version(self, uow_factory) -> None:
        """🔴 声明的版本与仓储实际算出的版本不一致时**当场失败**。

        放过它的后果是：索引里存着新空间的向量，记忆上却标着旧空间的版本——
        检索按版本筛选，于是这条记忆永远查不到，而且看不出为什么。
        """
        async with uow_factory() as uow:
            with pytest.raises(ProviderError) as excinfo:
                await uow.memories.add(_memory(embedding_version="not-the-current-space"))
        assert excinfo.value.code == "embedding_version_mismatch"

    async def test_unversioned_memory_is_accepted(self, uow_factory) -> None:
        """没有声明版本时按当前 Provider 处理（写入路径自己定的就是它）。"""
        memory = _memory()
        async with uow_factory() as uow:
            await uow.memories.add(memory)
            await uow.commit()
            assert memory.id in await uow.memories.indexed_ids()


class TestListForUser:
    async def test_orders_newest_first(self, uow_factory) -> None:
        """最新的排在前面，且顺序确定（同一时间按 id 兜底）。"""
        user = uuid4()
        memories = [_memory(user_id=user, content=f"偏好 {index}") for index in range(5)]
        # 直接写不同的创建时间，让顺序有可断言的含义
        for index, memory in enumerate(memories):
            memory.created_at = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=index)
        async with uow_factory() as uow:
            for memory in memories:
                await uow.memories.add(memory)
            await uow.commit()

        async with uow_factory() as uow:
            listed = await uow.memories.list_for_user(user_id=user)
        expected = [f"偏好 {index}" for index in reversed(range(5))]
        assert [item.content for item in listed] == expected
