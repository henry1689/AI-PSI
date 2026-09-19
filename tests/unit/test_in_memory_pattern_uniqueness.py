"""内存后端的业务模式唯一性：**提交时必须复核一次**（阶段 7 · R72）。

## 这条用例拦的是什么

内存实现的 ``add()`` 检查读的是"可见提案"（已提交 + 本事务暂存）。
两个工作单元各自暂存一条**同模式**的活跃提案时，谁都看不见对方——
两边都以为自己是唯一的写入者，``commit()`` 之后库里就有两条内容
相同的 DRAFT。

PostgreSQL 不会这样：唯一索引是原子的，第二个 INSERT 要么阻塞到
第一个提交、要么直接违反约束。

## 为什么它不是一个双后端契约用例

两者**在哪个调用上抛**天然不同：

* PostgreSQL：``add()`` 里的 ``flush()`` 就把 INSERT 送出去了，
  冲突在 ``add()`` 那一刻报出来；
* 内存：``add()`` 只是一次字典读，唯一能原子化的地方是
  ``commit()`` 里、锁内的那一次复核。

把它塞进同一组契约断言，只会得到一个要么对 PG 过松、
要么对内存过严的测试。**共同的部分**——"同模式的活跃提案至多一条"
——在 ``ProposalRepositoryContract`` 里按"同一事务内两次 add"断言
（两边都能表达），而"两个事务交错"的那一半写在这里。

⚠️ 与 :mod:`tests.unit.test_in_memory_optimistic_lock` 是同一个形状、
同一个理由，只是复核的对象从"版本"换成了"业务模式"。
"""

from __future__ import annotations

from uuid import UUID

import pytest

from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.domain.enums import ErrorType, ProposalStatus
from ai_psi.domain.exceptions import (
    ConstitutionViolationError,
    ProposalPatternConflictError,
)
from ai_psi.domain.improvement_proposals import ImprovementProposal, active_pattern_key
from ai_psi.infrastructure.in_memory.store import InMemoryStore
from ai_psi.infrastructure.in_memory.unit_of_work import make_in_memory_unit_of_work_factory
from ai_psi.providers.embeddings import LocalHashingEmbedding

pytestmark = pytest.mark.unit

#: 业务模式测试用的情境签名。形状与 ``_situation_signature()`` 一致。
_SIGNATURE = "d2|no_evidence|h2"


@pytest.fixture
def uow_factory() -> UnitOfWorkFactory:
    return make_in_memory_unit_of_work_factory(InMemoryStore(), LocalHashingEmbedding())


def _proposal(
    make_proposal, *, applicability: list[str], **overrides: object
) -> ImprovementProposal:
    """构造一条属于某个**业务模式**的提案。"""
    payload: dict[str, object] = {"applicability": applicability}
    payload.update(overrides)
    proposal: ImprovementProposal = make_proposal(**payload)
    return proposal


class TestTheCommitTimeRecheck:
    """🔴 两个工作单元同时判定"这个模式还没有活跃提案"时，只允许一个提交成功。"""

    async def test_the_loser_raises_instead_of_being_written(
        self, uow_factory, make_proposal
    ) -> None:
        """评审 6.6 §F1 的机制在内存侧的复现：**先各自暂存，再先后提交**。"""
        async with uow_factory() as first, uow_factory() as second:
            # 🔴 两边都**先各自暂存**——真实并发就是这样开始的。
            # 少了这一步（让第二个事务在前一个提交之后才 add），
            # 它测的会是 `add()` 里那条可见性检查，而那条一直是通的。
            await first.proposals.add(_proposal(make_proposal, applicability=[_SIGNATURE]))
            await second.proposals.add(_proposal(make_proposal, applicability=[_SIGNATURE]))
            # 两次 `add()` 都过了——这正是缺陷的形状。

            await first.commit()
            with pytest.raises(ProposalPatternConflictError, match="提交时复核"):
                await second.commit()

    async def test_the_winner_is_the_one_that_committed(self, uow_factory, make_proposal) -> None:
        """正向对照：**赢家的提案必须真的在库里，而且只有一条**。

        少了这一条，一条"两边都拒绝"的实现也能让上一条全绿——
        而那会让所有正常写入失败。
        """
        winner = _proposal(make_proposal, applicability=[_SIGNATURE])
        async with uow_factory() as first, uow_factory() as second:
            await first.proposals.add(winner)
            await second.proposals.add(_proposal(make_proposal, applicability=[_SIGNATURE]))
            await first.commit()
            with pytest.raises(ProposalPatternConflictError):
                await second.commit()

        async with uow_factory() as check:
            stored = await check.proposals.list_all()
        assert [item.id for item in stored] == [winner.id]
        assert len(stored) == 1, "输的那次写入也不得留下痕迹"

    async def test_a_serial_writer_is_not_affected(self, uow_factory, make_proposal) -> None:
        """反向对照：**串行**的两个写入者照常各自成功。

        少了这一条，一条"任何两个事务都冲突"的实现也能让上面两条全绿。
        """
        async with uow_factory() as first:
            await first.proposals.add(_proposal(make_proposal, applicability=["d1|no_evidence|h1"]))
            await first.commit()

        async with uow_factory() as second:
            await second.proposals.add(
                _proposal(make_proposal, applicability=["d2|no_evidence|h2"])
            )
            await second.commit()

        async with uow_factory() as check:
            assert len(await check.proposals.list_all()) == 2


class TestTheFinalStateIsWhatGetsChecked:
    """🔴 复核看的是**提交后的最终状态**，不是"已提交的数据"。"""

    async def test_a_terminal_proposal_frees_the_pattern_in_the_same_transaction(
        self, uow_factory, make_proposal
    ) -> None:
        """同一个事务里"旧提案转终态 + 建同键新提案"必须放行。

        只看**已提交**数据的实现会把那条已经不在活跃集里的旧提案
        算成占用者，于是**误报冲突**——正常路径被自己的复核挡住。
        """
        old = _proposal(make_proposal, applicability=[_SIGNATURE])
        async with uow_factory() as seeding:
            await seeding.proposals.add(old)
            await seeding.commit()

        async with uow_factory() as uow:
            await uow.proposals.save(
                old.bumped(status=ProposalStatus.REJECTED), expected_version=old.version
            )
            await uow.proposals.add(_proposal(make_proposal, applicability=[_SIGNATURE]))
            await uow.commit()

        async with uow_factory() as check:
            stored = await check.proposals.list_all()
        assert len(stored) == 2
        assert sum(1 for item in stored if not item.status.is_terminal) == 1

    async def test_two_new_proposals_for_one_pattern_in_one_transaction_are_refused(
        self, uow_factory, make_proposal
    ) -> None:
        """同一个事务里连建两条同键新提案必须被拦下。

        ⚠️ 这一问在 ``add()`` 就被仓储的可见性检查拦住了，**到不了**
        提交时的复核。两条路径都要留着：``add()`` 那条是正常路径，
        提交时那条是兜底（下面一条直接暂存，绕过 ``add()``）。
        """
        async with uow_factory() as uow:
            await uow.proposals.add(_proposal(make_proposal, applicability=[_SIGNATURE]))
            with pytest.raises(ProposalPatternConflictError):
                await uow.proposals.add(_proposal(make_proposal, applicability=[_SIGNATURE]))
            # 不提交 —— 退出 `async with` 时未提交即回滚

        async with uow_factory() as check:
            assert await check.proposals.list_all() == []

    async def test_the_commit_time_backstop_sees_staged_inserts(
        self, uow_factory, make_proposal
    ) -> None:
        """🔴 **绕过 `add()` 直接暂存两条同键提案，提交时的复核必须拦下。**

        这是"复核看的是最终状态"的直接证据：只看**已提交**数据的实现
        根本看不到这两条暂存中的提案，于是**漏放**——而漏放的结果
        正是两条内容完全相同的 DRAFT。
        """
        async with uow_factory() as uow:
            uow.stage_proposal(_proposal(make_proposal, applicability=[_SIGNATURE]))
            uow.stage_proposal(_proposal(make_proposal, applicability=[_SIGNATURE]))
            with pytest.raises(ProposalPatternConflictError, match="提交时复核"):
                await uow.commit()

        async with uow_factory() as check:
            assert await check.proposals.list_all() == []


class TestProposalsOutsideTheRule:
    """不在唯一性范围内的提案：**空 applicability 与终态**。"""

    async def test_proposals_without_applicability_do_not_collide(
        self, uow_factory, make_proposal
    ) -> None:
        """两个事务各写一条空 applicability 的提案，都应当成功。"""
        async with uow_factory() as first, uow_factory() as second:
            await first.proposals.add(_proposal(make_proposal, applicability=[]))
            await second.proposals.add(_proposal(make_proposal, applicability=[]))
            await first.commit()
            await second.commit()

        async with uow_factory() as check:
            assert len(await check.proposals.list_all()) == 2

    async def test_a_terminal_proposal_does_not_block_a_new_one(
        self, uow_factory, make_proposal
    ) -> None:
        """前一条进了终态之后，同模式的第二条活跃提案可以提交。"""
        old = _proposal(make_proposal, applicability=[_SIGNATURE])
        async with uow_factory() as seeding:
            await seeding.proposals.add(old)
            await seeding.commit()

        async with uow_factory() as uow:
            await uow.proposals.save(
                old.bumped(status=ProposalStatus.REJECTED), expected_version=old.version
            )
            await uow.commit()

        async with uow_factory() as second:
            await second.proposals.add(_proposal(make_proposal, applicability=[_SIGNATURE]))
            await second.commit()


class TestTheLookupRefusesToPickOne:
    """🔴 查出多于一条时**必须报错**，不能挑一条返回。"""

    async def test_more_than_one_active_proposal_is_a_constitution_violation(
        self, uow_factory, make_proposal
    ) -> None:
        """唯一性失效是一种**数据完整性缺陷**，不是"随便取一条"。

        这条分支在正常运行下不可达（提交时复核会先拦住），因此这里
        直接把两条同键活跃提案塞进 store——模拟"复核没生效"或
        "数据被绕过写进来"。挑一条返回会让调用方以为一切正常，
        而唯一性其实已经没了：一个只在并发下才暴露的缺陷，
        会变成永远不被发现的那种。
        """
        key_proposals = [_proposal(make_proposal, applicability=[_SIGNATURE]) for _ in range(2)]
        async with uow_factory() as uow:
            # 绕过仓储与复核，直接改"数据库"——这正是要模拟的损坏状态。
            for item in key_proposals:
                uow._store.proposals[item.id] = item

            with pytest.raises(ConstitutionViolationError, match="唯一性失效"):
                await uow.proposals.find_active_for_pattern(
                    error_class=key_proposals[0].error_class,
                    situation_signature=_SIGNATURE,
                )

    async def test_the_lookup_ignores_terminal_rows(self, uow_factory, make_proposal) -> None:
        """终态不算命中——否则"读回胜出者"会读回一条已经被驳回的提案。"""
        old = _proposal(make_proposal, applicability=[_SIGNATURE])
        async with uow_factory() as seeding:
            await seeding.proposals.add(old)
            await seeding.commit()

        async with uow_factory() as uow:
            await uow.proposals.save(
                old.bumped(status=ProposalStatus.REJECTED), expected_version=old.version
            )
            await uow.commit()

        async with uow_factory() as check:
            assert (
                await check.proposals.find_active_for_pattern(
                    error_class=old.error_class, situation_signature=_SIGNATURE
                )
                is None
            )


def test_the_business_key_is_the_first_element(make_proposal) -> None:
    """业务键取的是 ``applicability[0]``，**不是整个数组**。

    这条钉住的是索引表达式与 Python 取值之间的对应关系：
    PostgreSQL 那边写的是 ``applicability[1]``（下标从 1 起），
    两边必须指向同一个元素。整数组作键会让 ``['a','b']`` 与 ``['a']``
    变成两个模式，而应用层认为它们是同一个。
    """
    proposal = _proposal(make_proposal, applicability=["first", "second"])
    assert active_pattern_key(proposal) == (proposal.error_class.value, "first")

    empty = _proposal(make_proposal, applicability=[])
    assert active_pattern_key(empty) is None


def test_the_key_follows_the_error_class(make_proposal) -> None:
    """两类错误、同一个签名 → 两个不同的模式。"""
    one = _proposal(make_proposal, applicability=[_SIGNATURE])
    other = _proposal(make_proposal, applicability=[_SIGNATURE], error_class=ErrorType.SCOPE_ERROR)
    assert active_pattern_key(one) != active_pattern_key(other)


def test_an_unused_uuid_is_never_a_key(make_proposal) -> None:
    """守一个容易犯的错：``active_pattern_key`` 返回的是 ``str`` 元组。

    拿提案 id 去当键（例如误用 ``item.id``）会让每条提案各成一个模式，
    于是唯一性形同虚设——而所有"两条可以共存"的用例照样全绿。
    """
    key = active_pattern_key(_proposal(make_proposal, applicability=[_SIGNATURE]))
    assert key is not None
    assert all(isinstance(part, str) for part in key)
    assert not any(_looks_like_uuid(part) for part in key)


def _looks_like_uuid(value: str) -> bool:
    """该字符串是否是一个 UUID 的字面量。"""
    try:
        UUID(value)
    except ValueError:
        return False
    return True
