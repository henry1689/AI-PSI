"""跨工作单元共享的内存数据。

:class:`InMemoryStore` 相当于"数据库"：它持有已提交的数据与全局序号。
每个 :class:`~ai_psi.infrastructure.in_memory.unit_of_work.InMemoryUnitOfWork`
是它的一个事务视图，写入先进入暂存区，``commit()`` 时才合并。

**为什么不做成"直接写进共享字典"**：那样一次失败的事务会留下半成品，
与任务书阶段 2 的验收条件（"事务失败不会留下半成品数据"）直接冲突，
而且会让内存实现与 PostgreSQL 实现的行为在**最不该有差异的地方**产生差异。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from uuid import UUID

from ai_psi.domain.cognitive_rounds import CognitiveRound
from ai_psi.domain.events import Event
from ai_psi.domain.exceptions import OptimisticLockError, ProposalPatternConflictError
from ai_psi.domain.improvement_proposals import ImprovementProposal, active_pattern_key
from ai_psi.domain.memories import Memory

__all__ = [
    "MEMORY_KIND",
    "PROPOSAL_KIND",
    "ROUND_KIND",
    "ExpectedVersions",
    "IdempotencyRecord",
    "InMemoryStore",
    "MemoryIndexEntry",
]

#: 乐观锁复核用到的实体类别。
#:
#: 🔴 用字符串而不是类型对象：这三个类别是 `apply()` 在**已提交表**里
#: 查版本时的分派键，而分派本身需要一次判断——用类型对象既不能免掉
#: 那次判断，又会让映射的键在断言与报错里显示成 `<class '...'>`。
ROUND_KIND = "round"
MEMORY_KIND = "memory"
PROPOSAL_KIND = "proposal"

#: "``(类别, 实体 id)`` → **事务开始时**观察到的版本"。
#:
#: `apply()` 在提交时按它复核，见 :meth:`InMemoryStore.apply`。
ExpectedVersions = dict[tuple[str, UUID], int]


@dataclass
class IdempotencyRecord:
    """幂等键的占位记录。"""

    key: str
    request_hash: str
    cognitive_round_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class MemoryIndexEntry:
    """向量索引中的一条记录。

    对应 PostgreSQL 侧的 ``memory_embeddings`` 表。**刻意与记忆本体分开存放**，
    因为两者的生命周期不同：记忆被逻辑删除时本体保留（审计需要），
    索引行则被物理移除（不变量 15 需要"删了就不再检索得到"）。
    把向量塞进 :class:`~ai_psi.domain.memories.Memory` 会让这两件事
    绑在一起，于是"删除"只能二选一：要么丢掉审计轨迹，要么留下可检索的向量。
    """

    vector: list[float]
    embedding_version: str
    provider: str


@dataclass
class InMemoryStore:
    """内存中的"数据库"。

    线程安全通过一把粗粒度锁实现。V0.1 的内存实现只服务于测试与本地演示，
    并发压力不在其设计目标内；**但它必须是正确的**，
    因为它承载着契约测试。
    """

    #: ``(sequence, event)`` 列表。
    events: list[tuple[int, Event]] = field(default_factory=list)

    #: 回合表。
    rounds: dict[UUID, CognitiveRound] = field(default_factory=dict)

    #: 幂等键表。
    idempotency: dict[str, IdempotencyRecord] = field(default_factory=dict)

    #: 长期记忆主表。
    memories: dict[UUID, Memory] = field(default_factory=dict)

    #: 向量索引。键是记忆 id，**只包含当前可检索的记忆**（不变量 15）。
    index: dict[UUID, MemoryIndexEntry] = field(default_factory=dict)

    #: 改进提案（阶段 6）。状态可变，因此与经验不同——经验留在事件流里。
    proposals: dict[UUID, ImprovementProposal] = field(default_factory=dict)

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _sequence: int = field(default=0, repr=False)

    def next_sequence(self) -> int:
        """分配下一个全局序号。

        🔴 **序号只增不减。** 事务回滚时已经分配的序号**不回收**——
        PostgreSQL 的序列同样如此。把回滚实现成"序号回退"会让内存实现
        与真实实现产生行为差异，而那正是契约测试要防的。

        Returns:
            单调递增的序号，从 1 开始。
        """
        with self._lock:
            self._sequence += 1
            return self._sequence

    def current_sequence(self) -> int:
        """返回已分配的最大序号。"""
        with self._lock:
            return self._sequence

    def apply(
        self,
        *,
        events: list[tuple[int, Event]],
        rounds: dict[UUID, CognitiveRound],
        reservations: dict[str, IdempotencyRecord],
        memories: dict[UUID, Memory],
        index: dict[UUID, MemoryIndexEntry | None],
        proposals: dict[UUID, ImprovementProposal],
        expected_versions: ExpectedVersions | None = None,
    ) -> None:
        """把一次事务的暂存区合并进共享数据。

        **这是内存实现里唯一的写入口**，且只在 ``commit()`` 时被调用。
        把它放在 store 上（而不是让工作单元直接改字段）有两个好处：
        锁的边界清楚，且"未提交的写入不可见"这条规则只有一处需要保证。

        🔴 **乐观锁必须在锁内、在写入之前复核一遍。**

        ``save()`` 里的版本检查读的是**可见版本**（已提交 + 本事务暂存），
        而并发冲突恰恰发生在"暂存之后、提交之前"那一段：
        两个事务都读到 v1、都通过检查、都提交，后提交的那个
        **静默覆盖**前一个——没有任何异常，一次写入凭空消失。
        PostgreSQL 不会这样，因为 ``UPDATE ... WHERE version = ?``
        是原子的。

        所以这里按 :data:`ExpectedVersions` 再查一次**已提交**版本。
        检查在**任何写入之前**完成：失败时什么都没改，
        与 SQL 版本"语句失败、事务回滚"的结果一致。

        Args:
            events: 待追加的 ``(sequence, event)``。
            rounds: 待写入的回合。
            reservations: 待写入的幂等占位。
            memories: 待写入的记忆本体。
            index: 待同步的索引项。
            proposals: 待写入的改进提案。**值为 ``None`` 表示删除该索引项**——
                用 ``None`` 而不是"从字典里去掉"来表达删除，
                是因为"没改过它"与"要删掉它"必须能区分开。
            expected_versions: 事务开始时观察到的版本；``None`` 表示不复核
                （只有测试夹具会这么用，生产路径恒传）。

        Raises:
            OptimisticLockError: 有实体在暂存之后、提交之前被别的写入者改过。
        """
        with self._lock:
            self._assert_versions_still_hold(expected_versions or {})
            self._assert_pattern_uniqueness_holds(proposals)
            self.events.extend(events)
            self.rounds.update(rounds)
            self.idempotency.update(reservations)
            self.memories.update(memories)
            for memory_id, entry in index.items():
                if entry is None:
                    self.index.pop(memory_id, None)
                else:
                    self.index[memory_id] = entry
            self.proposals.update(proposals)

    def _assert_pattern_uniqueness_holds(self, proposals: dict[UUID, ImprovementProposal]) -> None:
        """🔴 提交前复核：同一业务模式至多一条**活跃**提案（阶段 7 · R72）。

        这是内存侧的并发防线，对应 PostgreSQL 的
        ``uq_improvement_proposals_active_pattern`` 部分唯一索引。
        位置与理由同 :meth:`_assert_versions_still_hold`：**在锁内、
        在任何写入之前**完成，失败时什么都没改——与 SQL 侧
        "语句失败、整个事务回滚"的结果一致。

        🔴 **复核的是提交后的最终状态**（已提交 ∪ 本事务暂存），
        不是只看已提交数据。两者在下面两种情形下给出不同答案：

        * **旧提案在本事务里转终态、同时创建同键新提案**：
          只看已提交数据会把那条已经不在活跃集里的旧提案算成占用者，
          **误报冲突**；看最终状态才放行。
        * **本事务暂存了两条相同的新提案**：只看已提交数据看不到它们，
          **漏报**；看最终状态才拦得住。

        ⚠️ **与 SQL 侧的分工（这条差异是"在哪一层原子化"，不是语义差异）：**

        两种情形都在共享契约里有断言
        （``ProposalRepositoryContract::test_a_terminal_proposal_releases_the_pattern``
        与 ``test_a_second_active_proposal_for_the_same_pattern_is_refused``），
        **实测两个后端同答案**：

        * PostgreSQL 侧，``save()`` 的那条 UPDATE 是立即执行的，
          ``add()`` 的 INSERT 在 ``flush()`` 时送出——顺序天然正确；
        * 内存侧，"更新"与"新增"都只是暂存，**唯一能原子化的地方
          就是下面这次锁内复核**。

        换句话说：内存侧这一问之所以必须看**最终状态**，是因为它没有
        "UPDATE 立即生效"这个中间态可用；而不是因为两边对"应该发生什么"
        有分歧。真正**只在内存侧**用一条用例单独断言的，是"两个事务
        交错提交"那一半（``tests/unit/test_in_memory_pattern_uniqueness.py``）
        ——那里 PG 的冲突在 ``add()`` 那一刻就报出来了，两边抛出的
        调用点天然不同，塞进同一组断言只会得到一个对某一侧过松的测试。

        Raises:
            ProposalPatternConflictError: 最终状态里同一个业务模式
                出现了多于一条活跃提案。
        """
        merged = {**self.proposals, **proposals}
        owners: dict[tuple[str, str], UUID] = {}
        for item in merged.values():
            if item.status.is_terminal:
                continue
            key = active_pattern_key(item)
            if key is None:
                continue
            if key in owners:
                error_class, signature = key
                msg = (
                    f"该模式已有一条活跃提案：{error_class} / {signature}"
                    f"（提交时复核，占用者是 {owners[key]}）"
                )
                raise ProposalPatternConflictError(
                    msg,
                    context={"error_class": error_class, "situation_signature": signature},
                )
            owners[key] = item.id

    def _assert_versions_still_hold(self, expected_versions: ExpectedVersions) -> None:
        """提交前的乐观锁复核。**调用方必须已经持有锁。**

        ⚠️ 已提交表里**没有**这个实体 → 跳过。那不是冲突，是
        "本事务刚创建了它"：`add()` 不记录期望版本，而 `save()`
        记录的期望版本只在实体已被提交过时才有意义。
        """
        for (kind, entity_id), expected in expected_versions.items():
            actual = self._committed_version(kind, entity_id)
            if actual is None or actual == expected:
                continue
            msg = (
                f"乐观锁冲突：{kind} {entity_id} 在提交前被其他写入者改过"
                f"（期望版本 {expected}，已提交版本 {actual}）。"
                "🔴 内存实现在这里必须与 PostgreSQL 的 "
                "`UPDATE ... WHERE version = ?` 表现一致——"
                "少了这条复核，后提交的事务会**静默覆盖**前一个，"
                "没有任何异常，一次写入凭空消失"
            )
            raise OptimisticLockError(
                msg,
                entity_type=kind,
                entity_id=str(entity_id),
                expected_version=expected,
                actual_version=actual,
            )

    def _committed_version(self, kind: str, entity_id: UUID) -> int | None:
        """**已提交**数据里该实体的版本；不存在则 ``None``。

        🔴 只读已提交，不读"可见"。两者的差别正是这个缺陷本身：
        可见 = 已提交 + 本事务暂存，而并发冲突发生在暂存之后。
        """
        entity: CognitiveRound | Memory | ImprovementProposal | None
        if kind == ROUND_KIND:
            entity = self.rounds.get(entity_id)
        elif kind == MEMORY_KIND:
            entity = self.memories.get(entity_id)
        elif kind == PROPOSAL_KIND:
            entity = self.proposals.get(entity_id)
        else:  # pragma: no cover - 类别由本模块的三个常量封闭
            return None
        return None if entity is None else entity.version

    def clear(self) -> None:
        """清空全部数据（测试夹具用）。"""
        with self._lock:
            self.events.clear()
            self.rounds.clear()
            self.idempotency.clear()
            self.memories.clear()
            self.index.clear()
            self.proposals.clear()
            self._sequence = 0
