"""存储相关的 Port（Protocol）。

按架构规则 2，**拥有 Port 的层定义 Port**。这些接口的消费者是
:mod:`ai_psi.application` 的服务，因此定义在这里；实现位于
:mod:`ai_psi.infrastructure`。

阶段 2 提供 PostgreSQL 实现。阶段 3 会提供**内存实现**以支撑
场景 A–J 的零依赖运行（ADR-0009）——两者必须共享同一套契约测试，
否则"阶段 3 通过、阶段 5 爆炸"。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self, runtime_checkable
from uuid import UUID

from ai_psi.domain.cognitive_rounds import CognitiveRound
from ai_psi.domain.enums import ErrorType, EventType, ProposalStatus
from ai_psi.domain.events import Event
from ai_psi.domain.improvement_proposals import ImprovementProposal
from ai_psi.domain.memories import Memory

__all__ = [
    "EventStore",
    "IdempotencyOutcome",
    "IdempotencyReservation",
    "IdempotencyStore",
    "MemoryRepository",
    "ProposalRepository",
    "RoundRepository",
    "UnitOfWork",
    "UnitOfWorkFactory",
]


# ---------------------------------------------------------------------------
# 事件存储
# ---------------------------------------------------------------------------


@runtime_checkable
class EventStore(Protocol):
    """事件存储。

    🔴 **只追加**。本 Protocol 刻意不提供 ``update`` / ``delete``——
    事件是系统的真相来源，当前状态只是它的投影（ADR-0002）。
    修改历史事件等同于销毁审计证据。
    """

    async def append(self, event: Event) -> None:
        """追加单个事件。"""
        ...

    async def append_many(self, events: list[Event]) -> None:
        """批量追加。

        要么全部成功，要么全部失败——不允许出现"写了一半"的事件流。

        Args:
            events: 待追加的事件，按期望的写入顺序给出。

        Raises:
            DomainError: 同一批次内存在重复的 ``id``。
        """
        ...

    async def read_stream(
        self,
        *,
        cognitive_round_id: UUID,
        after_sequence: int | None = None,
    ) -> list[Event]:
        """按 ``sequence`` 升序读取某个回合的事件流。

        Args:
            cognitive_round_id: 回合 id。
            after_sequence: 只返回该序之后的事件，用于增量回放。

        Returns:
            按 ``sequence`` 升序排列的事件。**顺序由 sequence 决定，不是时间戳**——
            同一微秒内的多次写入用时间戳排序会得到不确定结果。
        """
        ...

    async def read_by_correlation(self, *, correlation_id: UUID) -> list[Event]:
        """读取同一因果关联链上的全部事件。"""
        ...

    async def latest_sequence(self) -> int:
        """返回当前最大序号；空表返回 0。用于增量回放的游标。"""
        ...

    async def latest_sequence_for_round(self, *, cognitive_round_id: UUID) -> int:
        """返回某个回合事件流的最大序号；无事件时返回 0。

        增量回放需要它：领域事件对象**不携带序号**（序号是存储层概念，
        事件在构造时尚未写入数据库），因此游标只能由存储层提供。
        """
        ...

    async def read_by_event_type(
        self,
        *,
        event_type: EventType,
        limit: int | None = None,
    ) -> list[Event]:
        """按事件类型读取，按 ``sequence`` 升序。

        🔴 **这不是"顺手加的一个查询方法"，而是模式发现的唯一入口。**

        "同类错误至少出现 3 次"（任务书 §11.3）要跨回合统计，
        而回合是一段段独立的事件流——按回合读拿不到全局视图。
        没有这个方法，学习层只剩两条路：把整张事件表拉回来在内存里过滤
        （每调一次就是一次全表扫描），或者另建一张投影表
        （于是"投影与事件流不一致"成为一个新的可能）。

        ``events`` 表上已有 ``ix_events_type_recorded`` 索引，
        因此这是一个**本来就该存在**的查询。

        Args:
            event_type: 目标事件类型。
            limit: 返回条数上限；``None`` 表示不限制。
                ⚠️ 顺序是 ``sequence`` 升序，因此 ``limit`` 取的是**最早的 N 条**。
                要"最近 N 条"应当先取 ``latest_sequence()`` 再自行截断——
                存储层不替调用方猜它要哪一头。

        Returns:
            命中事件，按 ``sequence`` 升序。
        """
        ...

    async def count(self) -> int:
        """事件总数。"""
        ...


# ---------------------------------------------------------------------------
# 回合仓储
# ---------------------------------------------------------------------------


@runtime_checkable
class RoundRepository(Protocol):
    """认知回合仓储（当前状态投影）。"""

    async def add(self, round_: CognitiveRound) -> None:
        """插入新回合。

        Raises:
            ConflictError: 主键或幂等键已存在。
        """
        ...

    async def get(self, round_id: UUID) -> CognitiveRound | None:
        """按 id 读取；不存在返回 ``None``。"""
        ...

    async def save(self, round_: CognitiveRound, *, expected_version: int) -> None:
        """带乐观锁的更新。

        🔴 实现**必须**使用 ``WHERE id = :id AND version = :expected_version``，
        且当受影响行数为 0 时抛 :class:`~ai_psi.domain.exceptions.OptimisticLockError`。

        **绝不静默覆盖**——那是并发场景下最难排查的一类数据损坏（ADR-0002）。

        Args:
            round_: 新的领域状态（其 ``version`` 应为 ``expected_version + 1``）。
            expected_version: 调用方读到的版本号。

        Raises:
            OptimisticLockError: 版本不匹配，说明期间有其他写入者提交了变更。
        """
        ...

    async def find_by_idempotency_key(self, key: str) -> CognitiveRound | None:
        """按幂等键查找既有回合。"""
        ...


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


class IdempotencyOutcome(StrEnum):
    """幂等键占位的结果。"""

    RESERVED = "reserved"
    """首次见到该 key，占位成功——调用方有权创建新回合。"""

    REPLAY = "replay"
    """同一 key、同一请求体、且回合已创建——调用方应**直接返回既有回合**。"""

    CONFLICT = "conflict"
    """同一 key 但请求体不同，或上一次占位尚未完成。

    两种情况都不能继续——前者是客户端复用 key 的 bug，
    后者需要调用方退避重试。**不得**默默返回旧结果，那会掩盖问题。
    """


@dataclass(frozen=True, slots=True)
class IdempotencyReservation:
    """幂等占位的结果。

    Attributes:
        outcome: 占位结论。
        cognitive_round_id: 仅当 ``outcome`` 为 ``REPLAY`` 时非空。
    """

    outcome: IdempotencyOutcome
    cognitive_round_id: UUID | None = None


@runtime_checkable
class IdempotencyStore(Protocol):
    """幂等键存储（任务书 §13.4）。"""

    async def reserve(self, *, key: str, request_hash: str) -> IdempotencyReservation:
        """尝试占位。

        🔴 实现必须**原子地**处理并发：两个携带同一 key 的请求同时到达时，
        只能有一个拿到 :attr:`IdempotencyOutcome.RESERVED`。

        Args:
            key: 客户端的 ``Idempotency-Key``。
            request_hash: 请求体的哈希，用于识别"误用同一个 key"。
        """
        ...

    async def bind(self, *, key: str, cognitive_round_id: UUID) -> None:
        """把占位与已创建的回合绑定。"""
        ...


# ---------------------------------------------------------------------------
# 工作单元
# ---------------------------------------------------------------------------


@runtime_checkable
class UnitOfWork(Protocol):
    """工作单元——**事务边界的唯一入口**。

    使用方式::

        async with uow_factory() as uow:
            await uow.rounds.save(round_, expected_version=1)
            await uow.events.append(event)
            await uow.commit()

    🔴 **事件与投影必须在同一事务中提交**（ADR-0002）。
    分开提交会产生"事件写了但状态没变"或反之的中间态，
    而这类不一致恰恰是最难排查的。

    ``__aexit__`` 在未显式 ``commit()`` 时**必须回滚**——
    异常路径下静默提交会留下半成品数据（任务书阶段 2 验收条件）。
    """

    events: EventStore
    rounds: RoundRepository
    idempotency: IdempotencyStore
    #: 🔴 **阶段 5 起记忆也在工作单元内。**
    #:
    #: 阶段 3 时它是独立 Port（每个方法自带一个事务），于是
    #: "取代旧记忆 + 写入新记忆 + 记录事件"跨在三个事务上——
    #: 最后一步失败会留下一条**没有审计记录的纠正**：旧记忆已被取代、
    #: 新记忆已写入，但事件流里查不到这件事发生过。
    #: 记忆的后果是累积的，这种"改都改了、却说不清为什么改"的状态
    #: 比一次整体失败糟糕得多（ADR-0015 §5、ADR-0017）。
    memories: MemoryRepository
    #: 改进提案仓储（阶段 6）。
    #:
    #: 与记忆同理：提案的状态流转与它产生的审计事件必须同事务。
    #: "状态改了但没记录为什么改"在提案上尤其致命——
    #: 提案的全部意义就是**留下一个可被追溯的改进理由**。
    proposals: ProposalRepository

    async def __aenter__(self) -> Self:
        """进入事务作用域。"""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """退出作用域；未提交则回滚。

        ⚠️ 参数类型必须写具体，**不能图省事写成 ``object``**。
        函数参数是**逆变**的：协议若声明 ``exc: object``，
        实现里收窄成 ``BaseException | None`` 就不再满足协议，
        于是任何具体实现都无法通过类型检查。这里踩过一次。
        """
        ...

    async def commit(self) -> None:
        """提交事务。"""
        ...

    async def rollback(self) -> None:
        """回滚事务。"""
        ...


# ---------------------------------------------------------------------------
# 长期记忆
# ---------------------------------------------------------------------------


@runtime_checkable
class MemoryRepository(Protocol):
    """长期记忆仓储（任务书 §10）。

    🔴 **本 Port 在阶段 3 定型，阶段 5 换上 PostgreSQL + pgvector 实现，
    并挂进 :class:`UnitOfWork`。** 接口形状直接依据任务书 §10 的记忆操作
    清单设计，不自创语义——Port 设计不当会让阶段 5 的替换波及场景测试
    （risks.md R12）。事实证明这份形状是够用的：两个实现跑同一组契约断言，
    场景测试一行未改。

    三条语义契约，**任何实现都必须满足**，并由同一组契约测试固定：

    1. :meth:`retrieve` 必须强制 ``user_id`` 作用域，且**不得**返回
       非默认可检索状态（``SUPERSEDED`` / ``EXPIRED`` / ``DELETED`` 等）的记忆；
    2. :meth:`delete` 必须**同时**作用于主表与检索索引——
       "删了但还检索得到"是本系统最不能接受的失效模式（不变量 15）；
    3. **向量索引的维护是实现的职责**：:meth:`add` / :meth:`save` /
       :meth:`delete` 各自负责让索引与记忆状态保持一致。
       交给调用方意味着"忘了同步索引"是一个随时会发生的错误，
       而它的后果正是不变量 15 要防的那件事。
    """

    async def add(self, memory: Memory) -> None:
        """写入一条记忆。

        Raises:
            ConflictError: 主键已存在。
        """
        ...

    async def get(self, memory_id: UUID) -> Memory | None:
        """按 id 读取；不存在返回 ``None``。"""
        ...

    async def save(self, memory: Memory, *, expected_version: int) -> None:
        """带乐观锁的更新。

        Raises:
            OptimisticLockError: 版本不匹配。
        """
        ...

    async def retrieve(
        self,
        *,
        user_id: UUID | None,
        query: str,
        limit: int,
    ) -> list[Memory]:
        """按作用域检索默认有效的记忆。

        🔴 ``user_id`` **没有默认值**：调用方必须显式写出作用域。
        一个可以省略的作用域参数，迟早在某条调用路径上被省略。

        Args:
            user_id: 检索发起者。``None`` 表示系统级记忆作用域。
            query: 检索文本（阶段 5 起走向量；默认 Provider 是词面向量，
                见 :mod:`ai_psi.providers.embeddings`）。
            limit: 返回条数上限。

        Returns:
            按相关度降序排列的记忆，**不超过 ``limit`` 条**。

        Raises:
            ScopeViolationError: 若返回结果会包含其他用户的记忆。
        """
        ...

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
                用户查看"为什么被纠正"时需要它为 ``True``（任务书 §10.4）。
        """
        ...

    async def delete(self, memory_id: UUID) -> None:
        """删除一条记忆，**并同步作用于检索索引**（不变量 15）。"""
        ...

    async def indexed_ids(self) -> set[UUID]:
        """返回当前检索索引中存在的记忆 id 集合。

        🔴 **它不是测试钩子，而是不变量 15 唯一可被直接观测的形式。**

        :meth:`retrieve` 返回空可以有很多原因——相似度不够、``limit`` 太小、
        状态过滤把它挡掉了——任何一条都能让"删了还检索得到"这个缺陷
        **看起来是被修好的**。只有直接问索引本身，才能区分
        "索引里确实没有它"与"这次查询恰好没查出来"。

        两个实现都必须提供它，并由同一组契约断言固定。
        """
        ...


# ---------------------------------------------------------------------------
# 改进提案（阶段 6）
# ---------------------------------------------------------------------------


@runtime_checkable
class ProposalRepository(Protocol):
    """改进提案仓储（任务书 §5.12、§12.4）。

    🔴 **提案有表，经验没有。**

    经验是不可变的观察，写入之后从不更新，因此它的家在事件流里——
    加一张表只会引入"投影与事件流不一致"的可能。提案不同：
    状态会变（``DRAFT`` → … → 终态），需要乐观锁、按 id 取、
    按状态列表查询。这与 ``cognitive_rounds`` 的理由完全一样
    （ADR-0002：表是"为查询与并发控制而物化的投影"）。

    🔴 **本 Port 刻意没有"让提案生效"的方法。**

    没有任何接口能把提案变成"已生效"——``ProposalStatus`` 里根本
    没有这个成员（不变量 11）。缺的不是实现，是**这个概念本身**。
    """

    async def add(self, proposal: ImprovementProposal) -> None:
        """写入一条新提案。

        Raises:
            ConflictError: 主键已存在。
        """
        ...

    async def get(self, proposal_id: UUID) -> ImprovementProposal | None:
        """按 id 读取；不存在返回 ``None``。"""
        ...

    async def save(self, proposal: ImprovementProposal, *, expected_version: int) -> None:
        """带乐观锁的更新（状态流转用）。

        Raises:
            OptimisticLockError: 版本不匹配。
            NotFoundError: 提案不存在。
        """
        ...

    async def list_all(
        self,
        *,
        status: ProposalStatus | None = None,
        error_class: ErrorType | None = None,
        limit: int | None = None,
    ) -> list[ImprovementProposal]:
        """列出提案。

        Args:
            status: 只看某个状态；``None`` 表示全部。
            error_class: 只看某类错误；``None`` 表示全部。
            limit: 条数上限；``None`` 表示不限制。

        Returns:
            按 ``created_at`` 降序、同时间按 id 升序排列——
            **顺序必须确定**，"最近的三条提案"每次查出来不一样
            会让任何一次排查都无从下手。
        """
        ...


#: 工作单元工厂。
#:
#: 应用服务依赖**工厂**而不是工作单元实例：每次操作需要自己的事务边界，
#: 共享一个工作单元会让两次操作意外落在同一事务里。
#:
#: 这也是 Ports & Adapters 的关键点——服务只认 :class:`UnitOfWork` 协议，
#: 阶段 3 换成内存实现时，服务代码一行都不用改（ADR-0009）。
UnitOfWorkFactory = Callable[[], UnitOfWork]
