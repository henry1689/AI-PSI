"""内存后端的乐观锁：**提交时必须复核一次**（阶段 6.5 §八 评审 C 的 C2）。

## 这条用例拦的是什么

内存实现的 ``save()`` 检查读的是"可见版本"（已提交 + 本事务暂存）。
两个事务都读到 v1、都通过检查、都提交时，**后提交的那个静默覆盖
前一个**——没有任何异常，一次写入凭空消失。

PostgreSQL 不会这样，因为 ``UPDATE ... WHERE version = ?`` 是原子的。

## 为什么它不是一个双后端契约用例

两者挡住这件事的**层次不同**：

* PostgreSQL：``save()`` 的那条 UPDATE 本身就是原子的，冲突在
  ``save()`` 那一刻就报出来；
* 内存：``save()`` 只是一次普通的字典读，唯一能原子化的地方是
  ``commit()`` 里、锁内的那一次复核。

因此"冲突在哪个调用上抛出来"两边天然不同，把它们塞进同一组契约断言
只会得到一个要么对 PG 过松、要么对内存过严的测试。
共同的部分——**后提交者不得静默覆盖**——在本文件与
``RoundRepositoryContract::test_stale_version_raises_instead_of_overwriting``
里各按各自的层次断言。

## ⚠️ 诚实说明它的边界

**当前生产代码路径的临界区内没有 ``await`` 点**（仓储方法体内不 await），
所以这条缺陷在今天的配置下不会自然发生。它写在这里，是因为
临界区里出现第一个 ``await``（真实向量 provider 的网络调用、
或将来加入的任何等待）时，内存后端就会开始静默丢更新——
而那时契约测试**不会红**，因为"过期版本被拒"那条路径仍然是通的。
"""

from __future__ import annotations

from uuid import UUID

import pytest

from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.domain.cognitive_rounds import CognitiveRound
from ai_psi.domain.exceptions import OptimisticLockError
from ai_psi.infrastructure.in_memory.store import InMemoryStore
from ai_psi.infrastructure.in_memory.unit_of_work import make_in_memory_unit_of_work_factory
from ai_psi.providers.embeddings import LocalHashingEmbedding

pytestmark = pytest.mark.unit


@pytest.fixture
def uow_factory() -> UnitOfWorkFactory:
    return make_in_memory_unit_of_work_factory(InMemoryStore(), LocalHashingEmbedding())


async def _seed(uow_factory: UnitOfWorkFactory) -> UUID:
    """先提交一个回合，返回它的 id。

    ⚠️ 返回**真实写入的那个 id** 而不是另开一个 ``uuid4()``：
    后者会让后面每一次 ``get`` 都返回 ``None``，而症状是
    "断言 `base is not None` 失败"，看起来像仓储坏了。
    """
    created = CognitiveRound(created_by="test")
    async with uow_factory() as seeding:
        await seeding.rounds.add(created)
        await seeding.commit()
    return created.id


class TestTheCommitTimeRecheck:
    """🔴 两个事务同时读到同一版本时，**只允许一个提交成功**。"""

    async def test_the_loser_raises_instead_of_overwriting(self, uow_factory) -> None:
        """评审 C 的探针，逐字复现：先建、再双方各写、再先后提交。"""
        round_id = await _seed(uow_factory)

        async with uow_factory() as first, uow_factory() as second:
            # 🔴 两个事务都**先读到同一个版本**——真实并发就是这样开始的。
            # 少了这一步（让第二个事务在前一个提交之后才读），
            # 它测的会是 `save()` 里那条过期版本检查，而那条一直是通的。
            base = await first.rounds.get(round_id)
            other = await second.rounds.get(round_id)
            assert base is not None and other is not None
            assert base.version == other.version == 1

            await first.rounds.save(base.bumped(stop_reason="winner"), expected_version=1)
            await second.rounds.save(other.bumped(stop_reason="loser"), expected_version=1)
            # 两次 `save()` 都过了——这正是缺陷的形状：
            # 那一刻谁都看不见对方，两边都以为自己是唯一的写入者。

            await first.commit()
            with pytest.raises(OptimisticLockError, match="提交前被其他写入者改过"):
                await second.commit()

    async def test_the_winner_is_the_one_that_committed(self, uow_factory) -> None:
        """正向对照：**赢家的写入必须真的在库里**。

        少了这一条，一条"两边都拒绝"的实现也能让上一条全绿——
        而那会让所有正常写入失败。
        """
        round_id = await _seed(uow_factory)

        async with uow_factory() as first, uow_factory() as second:
            base = await first.rounds.get(round_id)
            other = await second.rounds.get(round_id)
            assert base is not None and other is not None

            await first.rounds.save(base.bumped(stop_reason="winner"), expected_version=1)
            await second.rounds.save(other.bumped(stop_reason="loser"), expected_version=1)
            await first.commit()
            with pytest.raises(OptimisticLockError):
                await second.commit()

        async with uow_factory() as check:
            stored = await check.rounds.get(round_id)
        assert stored is not None
        assert stored.stop_reason == "winner"
        # 🔴 版本只推进了**一次**。两边都提交成功的话它会是 3，
        # 而"输了的那次写入也没留下痕迹"同样要紧——
        # 半写入比丢更新更难查。
        assert stored.version == 2

    async def test_a_serial_writer_is_not_affected(self, uow_factory) -> None:
        """反向对照：**串行**的两个事务照常各自成功。

        少了这一条，一条"任何两个事务都冲突"的实现也能让上面两条全绿。
        """
        round_id = await _seed(uow_factory)

        async with uow_factory() as first:
            base = await first.rounds.get(round_id)
            assert base is not None
            await first.rounds.save(base.bumped(stop_reason="first"), expected_version=base.version)
            await first.commit()

        async with uow_factory() as second:
            # 第二个事务**重新读**，拿到的是 v2——它没有过期
            latest = await second.rounds.get(round_id)
            assert latest is not None and latest.version == 2
            await second.rounds.save(latest.bumped(stop_reason="second"), expected_version=2)
            await second.commit()

        async with uow_factory() as check:
            stored = await check.rounds.get(round_id)
        assert stored is not None
        assert stored.version == 3
        assert stored.stop_reason == "second"

    async def test_a_rollback_does_not_leave_a_stale_expectation(self, uow_factory) -> None:
        """回滚必须把期望版本一起丢掉。

        少了这条清理，同一个工作单元里"回滚之后再写"会拿着
        回滚前那一次的期望版本。
        """
        round_id = await _seed(uow_factory)

        async with uow_factory() as uow:
            base = await uow.rounds.get(round_id)
            assert base is not None
            await uow.rounds.save(base.bumped(stop_reason="扔掉"), expected_version=base.version)
            await uow.rollback()

            # 回滚之后重新开始：这一次观察到的仍然是 v1
            fresh = await uow.rounds.get(round_id)
            assert fresh is not None and fresh.version == 1
            await uow.rounds.save(fresh.bumped(stop_reason="重新来"), expected_version=1)
            await uow.commit()

        async with uow_factory() as check:
            stored = await check.rounds.get(round_id)
        assert stored is not None
        assert stored.stop_reason == "重新来"
        assert stored.version == 2
