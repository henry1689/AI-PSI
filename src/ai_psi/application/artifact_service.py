"""认知产物的记录服务。

认知产物（观察、关切、问题、假设、判断、反思……）**不建独立的数据表**，
而是作为事件负载写进事件流（ADR-0002：事件是真相来源，当前状态是它的投影）。

这么做的三个理由：

1. **审计完整**：产物与产生它的事件天然同源，"这个判断是怎么来的"
   不需要跨表 join 就能回答；
2. **回放一致**：回放读的就是事件流，产物不需要额外的一致性保证；
3. **不需要迁移**：阶段 3 不引入新表，也就没有新的迁移风险。

代价是查询需要投影。V0.1 的回合规模（每次十几条事件）完全承受得起。

🔴 **本服务是唯一能写入认知产物的入口**（架构规则 3）。
认知模块自己不碰持久化——它们只负责计算并返回对象。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ai_psi.application.ports import UnitOfWorkFactory
from ai_psi.domain.common import utc_now
from ai_psi.domain.enums import ActorType, EventType, SensitivityLevel, TrustLevel
from ai_psi.domain.events import Event, ModelInvocationInfo
from ai_psi.domain.exceptions import DomainError

__all__ = ["ArtifactService", "RoundScope"]


@dataclass(frozen=True, slots=True)
class RoundScope:
    """一个回合的关联标识。

    把四个标识收成一个不可变对象，是为了避免每个记录调用都要传四遍——
    那种写法迟早会在某条路径上漏掉一个，而漏掉 ``correlation_id``
    会让事件链断掉，且只在回放时才被发现。
    """

    cognitive_round_id: UUID | None
    correlation_id: UUID
    user_id: UUID | None = None
    conversation_id: UUID | None = None

    def require_round_id(self) -> UUID:
        """返回非空的回合 id。

        ``cognitive_round_id`` 允许为 ``None`` 只有一个原因：
        ``user.message.received`` 事件发生在回合被创建**之前**。
        回合一旦创建，后续所有写入都必须带上它——
        这个访问器把"必须已绑定回合"变成类型安全的调用。

        Raises:
            DomainError: 尚未绑定回合。
        """
        if self.cognitive_round_id is None:
            msg = "该作用域尚未绑定认知回合，不能写入回合级产物"
            raise DomainError(msg)
        return self.cognitive_round_id


class ArtifactService:
    """把认知产物记录为事件。"""

    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        """初始化。

        Args:
            uow_factory: 工作单元工厂。
        """
        self._uow_factory = uow_factory

    async def record(
        self,
        *,
        scope: RoundScope,
        event_type: EventType,
        payload: dict[str, Any],
        actor_id: str,
        model_info: ModelInvocationInfo | None = None,
        causation_id: UUID | None = None,
        trust_level: TrustLevel = TrustLevel.MEDIUM,
        sensitivity: SensitivityLevel = SensitivityLevel.INTERNAL,
        evidence_refs: tuple[UUID, ...] = (),
    ) -> Event:
        """记录一个认知产物。

        Args:
            scope: 回合关联标识。
            event_type: 事件类型。
            payload: 事件负载。**必须脱敏**——不得写入密钥或 Authorization 头；
                用户正文可以进入（它是系统的记录对象，不是日志），
                但见 ``docs/security.md`` §3 的边界说明。
            actor_id: 产生该产物的组件名。
            model_info: 若该产物由模型调用产生，记录调用审计信息。
                🔴 不变量 18：所有模型调用必须记录模型与 Prompt 版本。
            causation_id: 直接触发本事件的上游事件 id。
            trust_level: 信任等级。
            sensitivity: 敏感度。
            evidence_refs: 相关证据 id。

        Returns:
            已写入的事件。
        """
        event = Event(
            event_type=event_type,
            occurred_at=utc_now(),
            actor_type=ActorType.SYSTEM,
            actor_id=actor_id,
            user_id=scope.user_id,
            conversation_id=scope.conversation_id,
            cognitive_round_id=scope.cognitive_round_id,
            correlation_id=scope.correlation_id,
            causation_id=causation_id,
            payload=payload,
            evidence_refs=list(evidence_refs),
            trust_level=trust_level,
            sensitivity=sensitivity,
            model_info=model_info,
        )
        async with self._uow_factory() as uow:
            await uow.events.append(event)
            await uow.commit()
        return event
