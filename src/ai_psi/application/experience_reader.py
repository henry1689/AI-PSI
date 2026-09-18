"""从事件流读回经验与它们的评价（阶段 6.5 §二.13）。

🔴 **为什么要单独一个读取器，而不是各读各的：**

``ProposalGate`` 必须"从仓储重新查询真实经验并重新计算门槛"。
而"重新查询"这件事只有在**与主链路用同一段读取代码**时才有意义——
如果门禁自己写一份读取逻辑，两份代码迟早会在某个边界上分家
（比如一个过滤了无法解析的记录、另一个直接抛异常），
而分家的表现是"门禁算出的次数与链路算出的不一样"，
两者都不报错，只是永远对不上。

因此：**读取只有一处实现**，链路与门禁都走它。

⚠️ 本模块**只读**，不写任何东西。它不开事务写、不留事件。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.domain.enums import EventType
from ai_psi.domain.events import Event
from ai_psi.domain.experiences import (
    Experience,
    ExperienceAssessment,
    ExperienceEvaluationRecord,
    assess_experiences,
)

__all__ = [
    "ExperienceLoad",
    "ExperienceReader",
    "evaluation_from_event",
    "experience_from_event",
]


#: 事件负载里比 ``Experience`` 多出来的**审计**字段。
#:
#: 它们由 :meth:`~ai_psi.application.cognitive_runtime._RoundExecution._close_round`
#: 一并写进负载，供审计与调试使用（归因理由）——但 ``Experience``
#: 上并没有这两栏（``stop_reason`` 属于回合，不属于经验）。
#: 读回来时**必须**剥掉：``EntityMetadata`` 是 ``extra="forbid"`` 的，
#: 带着它们去 ``model_validate`` 会直接失败。
_AUDIT_ONLY_EXPERIENCE_KEYS: Final[frozenset[str]] = frozenset(
    {"stop_reason", "attribution_reasons"}
)


@dataclass(frozen=True, slots=True)
class ExperienceLoad:
    """一次读取的结果。

    🔴 **``unreadable`` 必须被上报，不能吞掉。**

    事件负载是历史数据，而领域对象会演进；用一次异常把整段历史作废，
    代价是"这个系统再也不能从过去学习"。所以读不回来的记录逐条跳过——
    但"只读到 2 条"与"历史上只有 2 条"是**完全不同**的两件事，
    少报这一项会让前者冒充后者。

    Attributes:
        assessments: 经验及其**有效评价**（已合并追加评价）。
        unreadable_experiences: 负载解析失败的经验条数。
        unreadable_evaluations: 负载解析失败的评价条数。
        orphan_evaluations: 指向不存在经验的评价条数。
    """

    assessments: tuple[ExperienceAssessment, ...] = ()
    unreadable_experiences: int = 0
    unreadable_evaluations: int = 0
    orphan_evaluations: int = 0

    @property
    def experiences(self) -> tuple[Experience, ...]:
        """读回来的经验（顺序与 ``assessments`` 一致）。"""
        return tuple(item.experience for item in self.assessments)


class ExperienceReader:
    """把事件流里的经验与评价读回成领域对象。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        """初始化。

        Args:
            uow_factory: 工作单元工厂。
        """
        self._uow_factory = uow_factory

    async def load(self) -> ExperienceLoad:
        """读回全部经验与评价。

        Returns:
            读取结果。
        """
        async with self._uow_factory() as uow:
            created = await uow.events.read_by_event_type(
                event_type=EventType.EXPERIENCE_CREATED
            )
            evaluated = await uow.events.read_by_event_type(
                event_type=EventType.EXPERIENCE_EVALUATED
            )

        experiences: list[Experience] = []
        unreadable_experiences = 0
        for event in created:
            experience = experience_from_event(event)
            if experience is None:
                unreadable_experiences += 1
            else:
                experiences.append(experience)

        records: list[ExperienceEvaluationRecord] = []
        unreadable_evaluations = 0
        for event in evaluated:
            record = evaluation_from_event(event)
            if record is None:
                unreadable_evaluations += 1
            else:
                records.append(record)

        known = {item.id for item in experiences}
        orphan = [item for item in records if item.experience_id not in known]
        kept = [item for item in records if item.experience_id in known]

        return ExperienceLoad(
            assessments=assess_experiences(experiences, kept),
            unreadable_experiences=unreadable_experiences,
            unreadable_evaluations=unreadable_evaluations,
            orphan_evaluations=len(orphan),
        )


def experience_from_event(event: Event) -> Experience | None:
    """把 ``experience.created`` 的负载还原成领域对象；失败返回 ``None``。

    🔴 **这是解析这种负载的唯一实现。** 反馈路径也要从事件里还原经验
    （它得知道自己该抬高哪几条），两处各写一份的话，负载结构一变
    就会出现"一处读得到、一处读不到"——而读不到的那处**不报错**，
    只是安静地少抬高几条经验。
    """
    payload = event.payload.get("experience")
    if not isinstance(payload, dict):  # pragma: no cover - 负载恒为字典
        return None
    cleaned = {
        key: value for key, value in payload.items() if key not in _AUDIT_ONLY_EXPERIENCE_KEYS
    }
    try:
        return Experience.model_validate(cleaned)
    except Exception:
        # ⚠️ 这里**刻意吞掉一切异常**：单条坏记录不该让整段历史作废。
        # 代价是"读不回来"必须被计数上报（见 ExperienceLoad.unreadable_experiences），
        # 否则"只读到 2 条"会被误当成"历史上只有 2 条"。
        return None


def evaluation_from_event(event: Event) -> ExperienceEvaluationRecord | None:
    """把 ``experience.evaluated`` 的负载还原成领域对象；失败返回 ``None``。

    与 :func:`experience_from_event` 同理：**只此一处实现**。
    """
    payload = event.payload.get("evaluation")
    if not isinstance(payload, dict):  # pragma: no cover - 负载恒为字典
        return None
    try:
        return ExperienceEvaluationRecord.model_validate(payload)
    except Exception:
        # 与经验同理：一条坏的评价记录不该让整段评价历史作废。
        # 代价是它会**降低**这条经验的评价——因此也要计数上报。
        return None
