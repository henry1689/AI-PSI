"""长期记忆仓储的内存实现（ADR-0009）。

🔴 **本实现承载两条最关键的认知不变量，且必须与阶段 5 的
PostgreSQL + pgvector 实现行为一致：**

* **不变量 14**：检索强制 ``user_id`` 作用域。返回结果逐条经过
  :func:`~ai_psi.cognition.constitution.assert_memory_retrievable_by`——
  这是**防御性断言**：过滤逻辑写错时它会立刻抛错，
  而不是安静地把别人的记忆交出去；
* **不变量 15**：删除必须同时作用于主表与检索索引。

**为什么它不属于工作单元**：:class:`~ai_psi.application.ports.MemoryRepository`
的每一个方法自身就是一次原子操作。阶段 5 的 PostgreSQL 实现同样如此
（各自一个事务），因此把它塞进 ``UnitOfWork`` 反而会让两个实现在
"事务边界在哪"这件事上产生差异——而契约测试正是用来防止这种差异的。

⚠️ 代价：``MemoryService.correct`` 的"取代旧记忆 + 写入新记忆 + 记录事件"
在阶段 3 不是跨表原子的。阶段 5 会把记忆仓储纳入工作单元后闭合这一点
（登记于 ADR-0015）。
"""

from __future__ import annotations

import threading
from uuid import UUID

from ai_psi.cognition.constitution import assert_memory_retrievable_by
from ai_psi.domain.enums import MemoryStatus
from ai_psi.domain.exceptions import ConflictError, NotFoundError, OptimisticLockError
from ai_psi.domain.memories import Memory
from ai_psi.reliability.repetition_detector import jaccard, normalized_ngrams

__all__ = ["InMemoryMemoryRepository"]


class InMemoryMemoryRepository:
    """长期记忆的内存实现。

    进程内共享，生命周期与进程一致。测试通过 :meth:`clear` 重置。
    """

    def __init__(self) -> None:
        """初始化空仓储。"""
        self._lock = threading.Lock()
        self._memories: dict[UUID, Memory] = {}
        #: 检索索引（阶段 5 的向量索引在内存中的对应物）。
        #: 🔴 删除必须同时作用于本索引，否则"删了还检索得到"（不变量 15）。
        self._index: set[UUID] = set()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def add(self, memory: Memory) -> None:
        """写入一条记忆。

        Raises:
            ConflictError: 主键已存在。
        """
        with self._lock:
            if memory.id in self._memories:
                msg = f"记忆已存在：{memory.id}"
                raise ConflictError(msg, context={"memory_id": str(memory.id)})
            self._memories[memory.id] = memory
            self._sync_index(memory)

    async def save(self, memory: Memory, *, expected_version: int) -> None:
        """带乐观锁的更新。

        Raises:
            NotFoundError: 记忆不存在。
            OptimisticLockError: 版本不匹配。
        """
        with self._lock:
            current = self._memories.get(memory.id)
            if current is None:
                msg = f"记忆不存在：{memory.id}"
                raise NotFoundError(msg, context={"memory_id": str(memory.id)})
            if current.version != expected_version:
                msg = (
                    f"乐观锁冲突：记忆 {memory.id} 期望版本 {expected_version}，"
                    f"实际版本 {current.version}"
                )
                raise OptimisticLockError(
                    msg,
                    entity_type="Memory",
                    entity_id=str(memory.id),
                    expected_version=expected_version,
                    actual_version=current.version,
                )
            self._memories[memory.id] = memory
            self._sync_index(memory)

    async def delete(self, memory_id: UUID) -> None:
        """删除一条记忆，并**同步作用于检索索引**（不变量 15）。

        V0.1 采用逻辑删除：本体保留（标记为 ``DELETED``）以便追溯
        "为什么发生过修正"（任务书 §10.4），索引侧则是真正的移除——
        所以它不会再出现在任何检索结果里。

        Raises:
            NotFoundError: 记忆不存在。
        """
        with self._lock:
            current = self._memories.get(memory_id)
            if current is None:
                msg = f"记忆不存在，无法删除：{memory_id}"
                raise NotFoundError(msg, context={"memory_id": str(memory_id)})
            self._memories[memory_id] = current.bumped(status=MemoryStatus.DELETED)
            self._index.discard(memory_id)

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    async def get(self, memory_id: UUID) -> Memory | None:
        """按 id 读取记忆。"""
        with self._lock:
            return self._memories.get(memory_id)

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
        query_grams = normalized_ngrams(query)
        with self._lock:
            eligible = [
                memory
                for memory in self._memories.values()
                if memory.id in self._index
                and memory.is_default_retrievable
                and memory.belongs_to(user_id)
            ]

        scored = sorted(
            ((jaccard(query_grams, normalized_ngrams(m.content)), m) for m in eligible),
            key=lambda pair: pair[0],
            reverse=True,
        )
        results = [memory for _, memory in scored[: max(0, limit)]]

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
        with self._lock:
            return [
                memory
                for memory in self._memories.values()
                if memory.belongs_to(user_id)
                and (include_inactive or memory.is_default_retrievable)
            ]

    # ------------------------------------------------------------------
    # 测试支持
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """清空全部记忆与索引（测试夹具用）。"""
        with self._lock:
            self._memories.clear()
            self._index.clear()

    def indexed_ids(self) -> set[UUID]:
        """返回当前检索索引中的 id 集合（供不变量 15 的契约测试断言）。"""
        with self._lock:
            return set(self._index)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _sync_index(self, memory: Memory) -> None:
        """把一条记忆的检索可见性同步到索引上。"""
        if memory.is_default_retrievable:
            self._index.add(memory.id)
        else:
            self._index.discard(memory.id)
