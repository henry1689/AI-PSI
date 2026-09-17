"""记忆应用服务（任务书 §10.1、§10.4、§10.5）。

🔴 本文件里最重要的一组是 :class:`TestAtomicity`：它验证的是
**阶段 5 相对阶段 3 的那处修复**——"取代旧记忆 + 写入新记忆 + 记录事件"
必须在同一个事务里。阶段 3 用"先做可能失败的操作、后写事件"的顺序
把风险压小（ADR-0015 §5），但事件写入失败仍会留下一次
**改了却没记录**的纠正。那正是用户最无法察觉、也最无法解释的状态。
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

import pytest

from ai_psi.application.memory_service import MemoryService
from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.domain.enums import (
    EventType,
    MemoryStatus,
    MemoryType,
    SensitivityLevel,
)
from ai_psi.domain.exceptions import NotFoundError
from ai_psi.infrastructure.in_memory.store import InMemoryStore
from ai_psi.infrastructure.in_memory.unit_of_work import (
    InMemoryUnitOfWork,
    make_in_memory_unit_of_work_factory,
)
from ai_psi.memory.write_policy import MemoryWriteProposal, WritePolicy
from ai_psi.providers.embeddings import EmbeddingProvider, LocalHashingEmbedding

pytestmark = pytest.mark.unit


class FailingEventStore:
    """一写事件就失败的事件存储。

    🔴 存在的唯一目的是制造"**记忆已写、事件写不进去**"这个中间态——
    也就是阶段 3 曾经真实存在过的那个窗口。没有它就无法断言修复有效：
    "正常情况下两者都成功"既可能来自事务，也可能来自运气。
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner

    async def append(self, event: object) -> None:
        msg = "模拟事件写入失败"
        raise RuntimeError(msg)

    async def append_many(self, events: object) -> None:
        msg = "模拟事件写入失败"
        raise RuntimeError(msg)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


@pytest.fixture
def embeddings() -> LocalHashingEmbedding:
    return LocalHashingEmbedding()


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def uow_factory(store: InMemoryStore, embeddings: EmbeddingProvider) -> UnitOfWorkFactory:
    return make_in_memory_unit_of_work_factory(store, embeddings)


@pytest.fixture
def service(uow_factory: UnitOfWorkFactory, embeddings: EmbeddingProvider) -> MemoryService:
    return MemoryService(uow_factory, embeddings)


def _approved(**overrides: object) -> MemoryWriteProposal:
    payload: dict[str, object] = {
        "user_id": uuid4(),
        "memory_type": MemoryType.USER_PREFERENCE,
        "content": "用户偏好简洁回答",
        "sensitivity": SensitivityLevel.PERSONAL,
        "user_confirmed": True,
    }
    payload.update(overrides)
    return MemoryWriteProposal(**payload)  # type: ignore[arg-type]


class TestPropose:
    async def test_approved_write_persists_memory_and_event(
        self, service: MemoryService, uow_factory: UnitOfWorkFactory
    ) -> None:
        outcome = await service.propose(proposal=_approved())
        assert outcome.written
        assert outcome.memory is not None
        assert outcome.memory.embedding_version == service._embeddings.version

        # 记忆真的落库了，并且可以按作用域检索到
        found = await service.retrieve(user_id=outcome.memory.user_id, query="简洁回答", limit=5)
        assert [item.id for item in found] == [outcome.memory.id]

    async def test_rejected_write_leaves_no_memory_but_leaves_an_event(
        self, service: MemoryService
    ) -> None:
        """🔴 被拒绝的写入同样要留痕。

        "系统曾经想记住什么但被挡住了"与"系统记住了什么"一样重要——
        少了它，策略调整的效果就无从评估。
        """
        outcome = await service.propose(
            proposal=_approved(
                content="用户是一个天生焦虑的人",  # 命中"用户稳定人格结论"
            )
        )
        assert not outcome.written
        assert outcome.memory is None

    async def test_rejected_write_does_not_store_content(self, service: MemoryService) -> None:
        """🔴 被拒绝内容的正文**绝不落库**——它往往正是最不该落库的那一类。"""
        secret = "用户是一个天生焦虑的人"
        await service.propose(proposal=_approved(content=secret))
        assert await service.retrieve(user_id=None, query=secret, limit=10) == []

    async def test_identical_content_is_not_stored_twice(self, service: MemoryService) -> None:
        user = uuid4()
        first = await service.propose(proposal=_approved(user_id=user))
        second = await service.propose(proposal=_approved(user_id=user))

        assert first.written
        assert not second.written
        assert second.duplicate_of == first.memory.id if first.memory else False
        assert len(await service.list_for_user(user_id=user)) == 1

    async def test_duplicate_is_reported_not_silently_ignored(self, service: MemoryService) -> None:
        """重复必须**报告**。

        静默成功会让调用方以为产生了新记忆，而实际的记忆条数没变——
        而"为什么写着写着条数不涨了"是极难排查的一类问题。
        """
        user = uuid4()
        await service.propose(proposal=_approved(user_id=user))
        second = await service.propose(proposal=_approved(user_id=user))
        assert second.duplicate_of is not None
        assert any("完全相同" in reason for reason in second.reasons)

    async def test_same_content_for_another_user_is_not_a_duplicate(
        self, service: MemoryService
    ) -> None:
        """🔴 作用域不同就不是重复——这是隔离的一部分。"""
        await service.propose(proposal=_approved(user_id=uuid4()))
        second = await service.propose(proposal=_approved(user_id=uuid4()))
        assert second.written

    async def test_conflict_leads_do_not_become_contradiction_facts(
        self, service: MemoryService
    ) -> None:
        """🔴 相似度线索**不得**写进 ``contradicts_ids``。

        "这两条矛盾"是一个关于语义的断言，而系统能算的只有"它们很接近"。
        把相似度高写成冲突是在伪造一个事实（不变量 3）。
        """
        user = uuid4()
        first = await service.propose(
            proposal=_approved(user_id=user, content="用户喜欢非常详细的回答")
        )
        second = await service.propose(
            proposal=_approved(user_id=user, content="用户喜欢非常详细的回答哦")
        )
        assert first.memory is not None and second.memory is not None
        for memory in (first.memory, second.memory):
            assert memory.contradicts_ids == []


class TestCorrect:
    async def test_correction_supersedes_instead_of_overwriting(
        self, service: MemoryService
    ) -> None:
        """🔴 不变量 5：用户纠正必须生成新版本，**不就地覆盖**。"""
        user = uuid4()
        original = await service.propose(
            proposal=_approved(user_id=user, content="用户喜欢非常详细的回答")
        )
        assert original.memory is not None

        correction = await service.correct(
            user_id=user, memory_id=original.memory.id, new_content="用户喜欢简洁回答"
        )

        assert correction.superseded.status is MemoryStatus.SUPERSEDED
        assert correction.superseded.id == original.memory.id
        assert correction.replacement.supersedes_id == original.memory.id

    async def test_superseded_memory_is_no_longer_retrievable(self, service: MemoryService) -> None:
        """🔴 不变量 6：被取代的记忆不能作为默认有效记忆返回。"""
        user = uuid4()
        original = await service.propose(
            proposal=_approved(user_id=user, content="用户喜欢非常详细的回答")
        )
        assert original.memory is not None
        await service.correct(
            user_id=user, memory_id=original.memory.id, new_content="用户喜欢简洁回答"
        )

        found = await service.retrieve(user_id=user, query="回答", limit=10)
        assert [item.content for item in found] == ["用户喜欢简洁回答"]

    async def test_old_version_is_still_visible_when_asked_for(
        self, service: MemoryService
    ) -> None:
        """🔴 纠错痕迹保留：用户查看"为什么被纠正"时必须看得到旧版本。"""
        user = uuid4()
        original = await service.propose(
            proposal=_approved(user_id=user, content="用户喜欢非常详细的回答")
        )
        assert original.memory is not None
        await service.correct(
            user_id=user, memory_id=original.memory.id, new_content="用户喜欢简洁回答"
        )

        everything = await service.list_for_user(user_id=user, include_inactive=True)
        assert len(everything) == 2
        assert len(await service.list_for_user(user_id=user)) == 1

    async def test_correction_of_another_users_memory_looks_like_not_found(
        self, service: MemoryService
    ) -> None:
        """🔴 404 而不是 403：说"无权限"本身就泄漏了"这条记录存在"。"""
        owner, intruder = uuid4(), uuid4()
        original = await service.propose(proposal=_approved(user_id=owner))
        assert original.memory is not None
        with pytest.raises(NotFoundError):
            await service.correct(
                user_id=intruder, memory_id=original.memory.id, new_content="改动"
            )

    async def test_correction_unknown_memory(self, service: MemoryService) -> None:
        with pytest.raises(NotFoundError):
            await service.correct(user_id=uuid4(), memory_id=uuid4(), new_content="改动")

    async def test_correction_events_carry_no_content(self, service: MemoryService) -> None:
        """🔴 审计信息不得保留正文（任务书 §10.5）。"""
        user = uuid4()
        secret = "用户喜欢非常详细的回答"
        original = await service.propose(proposal=_approved(user_id=user, content=secret))
        assert original.memory is not None
        correction = await service.correct(
            user_id=user, memory_id=original.memory.id, new_content="用户喜欢简洁回答"
        )
        for event in correction.events:
            assert secret not in str(event.payload)
            assert "用户喜欢简洁回答" not in str(event.payload)

    async def test_correction_records_both_events(self, service: MemoryService) -> None:
        user = uuid4()
        original = await service.propose(proposal=_approved(user_id=user))
        assert original.memory is not None
        correction = await service.correct(
            user_id=user, memory_id=original.memory.id, new_content="换一个说法"
        )
        assert [event.event_type for event in correction.events] == [
            EventType.USER_CORRECTION_RECEIVED,
            EventType.MEMORY_CORRECTED,
        ]


class TestDelete:
    async def test_delete_removes_it_from_retrieval(
        self, service: MemoryService, uow_factory: UnitOfWorkFactory
    ) -> None:
        """🔴 不变量 15：删除必须同时作用于检索索引。"""
        user = uuid4()
        written = await service.propose(proposal=_approved(user_id=user))
        assert written.memory is not None

        deletion = await service.delete(user_id=user, memory_id=written.memory.id)
        assert deletion.memory.status is MemoryStatus.DELETED

        assert await service.retrieve(user_id=user, query="简洁回答", limit=10) == []
        async with uow_factory() as uow:
            assert written.memory.id not in await uow.memories.indexed_ids()

    async def test_delete_keeps_the_body_for_audit(self, service: MemoryService) -> None:
        """逻辑删除：本体保留以便追溯"为什么发生过修正"。"""
        user = uuid4()
        written = await service.propose(proposal=_approved(user_id=user))
        assert written.memory is not None
        await service.delete(user_id=user, memory_id=written.memory.id)
        stored = await service.list_for_user(user_id=user, include_inactive=True)
        assert [item.status for item in stored] == [MemoryStatus.DELETED]

    async def test_delete_event_carries_no_content(self, service: MemoryService) -> None:
        user = uuid4()
        written = await service.propose(proposal=_approved(user_id=user))
        assert written.memory is not None
        deletion = await service.delete(user_id=user, memory_id=written.memory.id)
        assert written.memory.content not in str(deletion.audit_event.payload)
        assert deletion.audit_event.payload["content_length"] == len(written.memory.content)

    async def test_delete_of_another_users_memory_is_not_found(
        self, service: MemoryService
    ) -> None:
        user = uuid4()
        written = await service.propose(proposal=_approved(user_id=user))
        assert written.memory is not None
        with pytest.raises(NotFoundError):
            await service.delete(user_id=uuid4(), memory_id=written.memory.id)


class TestExportAndDeletion:
    async def test_export_includes_the_version_chain(self, service: MemoryService) -> None:
        user = uuid4()
        original = await service.propose(
            proposal=_approved(user_id=user, content="用户喜欢非常详细的回答")
        )
        assert original.memory is not None
        await service.correct(
            user_id=user, memory_id=original.memory.id, new_content="用户喜欢简洁回答"
        )

        result = await service.export_user_data(user_id=user)
        contents = [record["content"] for record in result.payload["memories"]]
        assert "用户喜欢简洁回答" in contents
        assert "用户喜欢非常详细的回答" in contents
        assert result.payload["active_count"] == 1

    async def test_export_audit_event_carries_no_content(self, service: MemoryService) -> None:
        """🔴 否则"导出"就成了把内容抄一份留在事件表里的后门。"""
        user = uuid4()
        written = await service.propose(proposal=_approved(user_id=user))
        assert written.memory is not None
        result = await service.export_user_data(user_id=user)
        assert written.memory.content not in str(result.audit_event.payload)

    async def test_export_only_covers_the_requested_user(self, service: MemoryService) -> None:
        mine, theirs = uuid4(), uuid4()
        await service.propose(proposal=_approved(user_id=mine, content="我的偏好"))
        await service.propose(proposal=_approved(user_id=theirs, content="他的偏好"))

        result = await service.export_user_data(user_id=mine)
        contents = [record["content"] for record in result.payload["memories"]]
        assert contents == ["我的偏好"]

    async def test_delete_user_data_removes_everything(self, service: MemoryService) -> None:
        user = uuid4()
        for index in range(3):
            await service.propose(proposal=_approved(user_id=user, content=f"偏好 {index}"))
        result = await service.delete_user_data(user_id=user)
        assert result.deleted_count == 3
        assert await service.retrieve(user_id=user, query="偏好", limit=10) == []
        assert await service.list_for_user(user_id=user) == []

    async def test_delete_user_data_leaves_no_content_in_events(
        self, service: MemoryService
    ) -> None:
        user = uuid4()
        await service.propose(proposal=_approved(user_id=user, content="我的私人偏好"))
        result = await service.delete_user_data(user_id=user)
        assert result.audit_events
        for event in result.audit_events:
            assert "我的私人偏好" not in str(event.payload)

    async def test_delete_user_data_does_not_double_count(self, service: MemoryService) -> None:
        """重复删除不再产生第二条删除事件。

        多个"删除时刻"会让"这条记忆是什么时候被删的"变得模糊，
        而审计要回答的正是这个问题。
        """
        user = uuid4()
        await service.propose(proposal=_approved(user_id=user))
        first = await service.delete_user_data(user_id=user)
        second = await service.delete_user_data(user_id=user)
        assert first.deleted_count == 1
        assert second.deleted_count == 0

    async def test_delete_user_data_leaves_other_users_alone(self, service: MemoryService) -> None:
        mine, theirs = uuid4(), uuid4()
        await service.propose(proposal=_approved(user_id=theirs))
        await service.delete_user_data(user_id=mine)
        assert len(await service.list_for_user(user_id=theirs)) == 1


class OtherVectorSpace(LocalHashingEmbedding):
    """模拟"换了一个向量空间"的 Provider。

    换的是**版本标识**，向量本身照旧——测试关心的是
    "版本不同就不参与检索"这条规则，不是两个向量空间的具体差异。
    """

    @property
    def version(self) -> str:
        return "local-hashing-v1-d512-alternate"


class TestReindex:
    async def test_reindex_rebuilds_stale_embeddings(
        self,
        store: InMemoryStore,
        uow_factory: UnitOfWorkFactory,
        embeddings: EmbeddingProvider,
    ) -> None:
        """🔴 换向量 Provider 之后必须能修回来。

        检索只比对**同版本**的向量。换 Provider 后旧向量会立刻全部失效——
        **旧记忆一条都检索不到**，而系统不会报任何错。
        这是一个静默的全面失效，必须有显式的动作把它修回来。

        注意这里换的是**整机**：真实场景下 Provider 是容器装配的，
        换它意味着工作单元工厂与服务拿到的是同一个新实例。
        只换其中一个会让记忆声明的版本与仓储实际算出的版本分家，
        仓储会当场报错（而不是悄悄写进一个对不上的版本）。
        """
        service = MemoryService(uow_factory, embeddings)
        user = uuid4()
        written = await service.propose(proposal=_approved(user_id=user))
        assert written.memory is not None
        assert await service.retrieve(user_id=user, query="简洁回答", limit=5) != []

        other = OtherVectorSpace()
        swapped = MemoryService(make_in_memory_unit_of_work_factory(store, other), other)
        assert await swapped.retrieve(user_id=user, query="简洁回答", limit=5) == []

        assert await swapped.reindex(user_id=user) == 1
        assert await swapped.retrieve(user_id=user, query="简洁回答", limit=5) != []

    async def test_reindex_is_idempotent(
        self, uow_factory: UnitOfWorkFactory, embeddings: EmbeddingProvider
    ) -> None:
        """已经是最新版本的记忆不重算——否则每次调用都会无谓地重算全表。"""
        service = MemoryService(uow_factory, embeddings)
        user = uuid4()
        await service.propose(proposal=_approved(user_id=user))
        assert await service.reindex(user_id=user) == 0

    async def test_reindex_skips_non_retrievable_memories(
        self, uow_factory: UnitOfWorkFactory, embeddings: EmbeddingProvider
    ) -> None:
        """已删除/被取代的记忆没有索引，也就不需要重建。"""
        service = MemoryService(uow_factory, embeddings)
        user = uuid4()
        written = await service.propose(proposal=_approved(user_id=user))
        assert written.memory is not None
        await service.delete(user_id=user, memory_id=written.memory.id)
        assert await service.reindex(user_id=user) == 0


class TestAtomicity:
    """🔴 阶段 5 相对阶段 3 的那处修复（ADR-0015 §5）。"""

    @pytest.fixture
    def failing_uow_factory(
        self, store: InMemoryStore, embeddings: EmbeddingProvider
    ) -> Callable[[], InMemoryUnitOfWork]:
        def factory() -> InMemoryUnitOfWork:
            uow = InMemoryUnitOfWork(store, embeddings)
            uow.events = FailingEventStore(uow.events)  # type: ignore[assignment]
            return uow

        return factory

    async def test_failed_audit_rolls_back_the_memory_write(
        self,
        store: InMemoryStore,
        failing_uow_factory: Callable[[], InMemoryUnitOfWork],
        embeddings: EmbeddingProvider,
    ) -> None:
        """记忆写入与事件写入**要么全成、要么全不成**。

        这条断言直接对着阶段 3 的缺陷：那时记忆仓储自带事务，
        `add` 提交之后事件才写；事件失败会留下一条**已经写进去、
        却查不到为什么写进去**的记忆。记忆的后果是累积的，
        这种"改都改了、却说不清为什么改"的状态比一次整体失败糟糕得多。
        """
        service = MemoryService(failing_uow_factory, embeddings)
        with pytest.raises(RuntimeError, match="模拟事件写入失败"):
            await service.propose(proposal=_approved())

        assert store.memories == {}
        assert store.index == {}

    async def test_failed_audit_rolls_back_a_correction(
        self,
        store: InMemoryStore,
        embeddings: EmbeddingProvider,
        uow_factory: UnitOfWorkFactory,
    ) -> None:
        """纠正的**四步**（取代、写入、两条事件）必须同生共死。"""
        service = MemoryService(uow_factory, embeddings)
        user = uuid4()
        original = await service.propose(proposal=_approved(user_id=user))
        assert original.memory is not None

        def failing_factory() -> InMemoryUnitOfWork:
            uow = InMemoryUnitOfWork(store, embeddings)
            uow.events = FailingEventStore(uow.events)  # type: ignore[assignment]
            return uow

        broken = MemoryService(failing_factory, embeddings)
        with pytest.raises(RuntimeError, match="模拟事件写入失败"):
            await broken.correct(user_id=user, memory_id=original.memory.id, new_content="新内容")

        # 旧记忆仍然是 ACTIVE（没有被取代），也没有多出版本
        after = await service.list_for_user(user_id=user, include_inactive=True)
        assert [item.status for item in after] == [MemoryStatus.ACTIVE]
        assert after[0].version == original.memory.version

    async def test_failed_audit_rolls_back_a_deletion(
        self,
        store: InMemoryStore,
        embeddings: EmbeddingProvider,
        uow_factory: UnitOfWorkFactory,
    ) -> None:
        """🔴 删除失败时**索引不能被清掉**。

        比"删了一半"更糟的情况是：索引已移除、状态没改——
        那条记忆从此再也检索不到，但系统里没有任何地方显示它被删过。
        """
        service = MemoryService(uow_factory, embeddings)
        user = uuid4()
        written = await service.propose(proposal=_approved(user_id=user))
        assert written.memory is not None

        def failing_factory() -> InMemoryUnitOfWork:
            uow = InMemoryUnitOfWork(store, embeddings)
            uow.events = FailingEventStore(uow.events)  # type: ignore[assignment]
            return uow

        broken = MemoryService(failing_factory, embeddings)
        with pytest.raises(RuntimeError, match="模拟事件写入失败"):
            await broken.delete(user_id=user, memory_id=written.memory.id)

        assert await service.retrieve(user_id=user, query="简洁回答", limit=5) != []


class TestPolicyIsInjectable:
    async def test_custom_policy_is_used(
        self, uow_factory: UnitOfWorkFactory, embeddings: EmbeddingProvider
    ) -> None:
        service = MemoryService(uow_factory, embeddings, policy=WritePolicy())
        assert isinstance(service.policy, WritePolicy)
