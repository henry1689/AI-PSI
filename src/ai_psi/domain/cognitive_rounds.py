"""认知回合与认知预算。

🔴 **本模块承载不变量 19 与 20**（任务书 §14）：

* **不变量 19**：所有完成回合必须有停止原因
  → ``COMPLETED`` 时 ``stop_reason`` 必填
* **不变量 20**：所有失败回合必须能查询失败阶段和错误类别
  → ``FAILED`` 时 ``failure_stage`` 与 ``error_category`` 必填

**为什么这两条写在模型校验器里而不是业务逻辑里：**
放在业务逻辑里，任何一条新增的完成路径都可能忘记设置它们，
而错误要到很久以后才暴露。放在模型校验器里（配合
``validate_assignment=True``），**构造或赋值的那一刻**就会失败。

预算的深度差异表见 ADR-0008。
"""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_psi.domain.common import EntityMetadata, UtcDatetimeOptional
from ai_psi.domain.enums import CognitiveDepth, ErrorType, RoundState

__all__ = ["CognitiveBudget", "CognitiveRound"]


class CognitiveBudget(BaseModel):
    """一次认知回合的资源上限（任务书 §13.1）。

    🔴 **元认知的模型调用计入 ``max_model_calls``**（ADR-0008）。
    否则该字段就不再是真实的成本上限，预算也就失去了意义。
    ``max_metacognitive_loops`` 限制的是**循环轮数**而非调用次数，两者共同约束。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_model_calls: int = Field(default=12, ge=1)
    max_metacognitive_loops: int = Field(default=2, ge=0)
    max_hypotheses: int = Field(default=4, ge=1)
    max_retrieved_memories: int = Field(default=20, ge=0)
    max_context_tokens: int = Field(default=32_000, ge=1)
    max_duration_seconds: int = Field(default=120, ge=1)
    max_cost_units: float | None = Field(default=None, gt=0)

    @classmethod
    def for_depth(cls, level: CognitiveDepth) -> Self:
        """按认知深度返回预算（ADR-0008 的预算表）。

        Args:
            level: D0–D4。

        Returns:
            该深度对应的预算配置。
        """
        return cls(**_BUDGET_BY_DEPTH[level])


#: ADR-0008 的深度预算表。修改此表必须同步修改 ADR-0008。
#:
#: 🔴 ``max_model_calls`` 在阶段 3 **按实测的模块成本重新标定**过。
#: 阶段 1 写下的 3/5/8/11/12 是**估算**；阶段 3 实现模块矩阵后逐档核算：
#:
#:   D0 标称 4（关切 + 框定 + 判断 + 渲染），无元认知循环
#:   D1 标称 6（+ 逻辑 + 元认知）
#:   D2 标称 8（+ 假设 + 因果），另留 2 次供一次额外循环（判断 + 元认知）
#:   D3 标称 10（+ 概念 + 辩证），另留 2
#:   D4 标称 11（+ 哲理），另留 2
#:
#: 标称值必须与 :data:`ai_psi.cognition.depth_router.NOMINAL_MODEL_CALLS_BY_DEPTH`
#: 以及 :data:`ai_psi.cognition.orchestrator.MODULE_MATRIX` 一致，
#: 由 ``tests/unit/test_orchestrator.py`` 断言三者不漂移。
_BUDGET_BY_DEPTH: dict[CognitiveDepth, dict[str, int]] = {
    CognitiveDepth.D0: {
        "max_model_calls": 4,
        "max_metacognitive_loops": 0,
        "max_hypotheses": 1,
        "max_retrieved_memories": 5,
        "max_context_tokens": 4_000,
        "max_duration_seconds": 15,
    },
    CognitiveDepth.D1: {
        "max_model_calls": 6,
        "max_metacognitive_loops": 1,
        "max_hypotheses": 2,
        "max_retrieved_memories": 10,
        "max_context_tokens": 8_000,
        "max_duration_seconds": 30,
    },
    CognitiveDepth.D2: {
        "max_model_calls": 10,
        "max_metacognitive_loops": 2,
        "max_hypotheses": 4,
        "max_retrieved_memories": 15,
        "max_context_tokens": 16_000,
        "max_duration_seconds": 60,
    },
    CognitiveDepth.D3: {
        "max_model_calls": 12,
        "max_metacognitive_loops": 2,
        "max_hypotheses": 4,
        "max_retrieved_memories": 20,
        "max_context_tokens": 24_000,
        "max_duration_seconds": 90,
    },
    CognitiveDepth.D4: {
        "max_model_calls": 13,
        "max_metacognitive_loops": 2,
        "max_hypotheses": 4,
        "max_retrieved_memories": 20,
        "max_context_tokens": 32_000,
        "max_duration_seconds": 120,
    },
}


class CognitiveRound(EntityMetadata):
    """一次认知回合（任务书 §6，转移表见 ``docs/state_machine.md``）。

    🔴 状态转移的合法性由 :mod:`ai_psi.cognition.state_machine` 校验；
    本对象负责的是**状态之间的耦合约束**（哪些字段在哪些状态下必须存在）。
    """

    user_id: UUID | None = Field(default=None, description="归属用户")
    conversation_id: UUID | None = Field(default=None, description="所属会话")
    trigger_event_id: UUID | None = Field(
        default=None,
        description="触发本回合的事件",
    )

    state: RoundState = Field(default=RoundState.CREATED)
    depth_level: CognitiveDepth = Field(
        default=CognitiveDepth.D0,
        description="深度路由结果。指标 ``average_latency_per_depth`` 依赖此字段分组",
    )
    budget: CognitiveBudget = Field(default_factory=CognitiveBudget)

    model_calls_used: int = Field(default=0, ge=0, description="已消耗的模型调用数")
    metacognitive_loops: int = Field(default=0, ge=0, description="已执行的元认知循环轮数")

    stop_reason: str | None = Field(
        default=None,
        description="**停止原因**。🔴 ``COMPLETED`` 状态下必填（不变量 19）",
    )
    failure_stage: str | None = Field(
        default=None,
        description="**失败阶段**。🔴 ``FAILED`` 状态下必填（不变量 20）",
    )
    error_category: ErrorType | None = Field(
        default=None,
        description="**错误类别**。🔴 ``FAILED`` 状态下必填（不变量 20）",
    )

    idempotency_key: str | None = Field(
        default=None,
        description="API 幂等键。API 重试不得创建重复回合（任务书 §13.4）",
    )
    correlation_id: UUID | None = Field(default=None, description="关联链标识")
    causation_id: UUID | None = Field(
        default=None,
        description="上游回合 id。重新激活挂起的回合时指向原回合，保持审计链完整",
    )

    started_at: UtcDatetimeOptional = Field(default=None, description="开始处理时间")
    completed_at: UtcDatetimeOptional = Field(default=None, description="结束时间")

    @model_validator(mode="after")
    def _enforce_stop_reason(self) -> Self:
        """🔴 **不变量 19**：所有完成回合必须有停止原因。"""
        if self.state is RoundState.COMPLETED and not self.stop_reason:
            msg = (
                "state=COMPLETED 时必须提供 stop_reason（不变量 19）。"
                "没有停止原因的完成回合无法回答「为什么停下来」"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _enforce_failure_diagnostics(self) -> Self:
        """🔴 **不变量 20**：失败回合必须能查询失败阶段与错误类别。"""
        if self.state is RoundState.FAILED:
            missing = [
                name
                for name, value in (
                    ("failure_stage", self.failure_stage),
                    ("error_category", self.error_category),
                )
                if value is None
            ]
            if missing:
                msg = f"state=FAILED 时必须提供 {missing}（不变量 20）。失败回合必须可诊断"
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _enforce_budget_ceiling(self) -> Self:
        """模型调用数不得超过预算——超预算的回合率是硬指标，必须为 0。

        注意：``max_metacognitive_loops`` 超限**不在此处判失败**——
        那会降级为强制 STOP 进入 ``SYNTHESIZING``（ADR-0008）。
        """
        if self.model_calls_used > self.budget.max_model_calls:
            msg = (
                f"model_calls_used ({self.model_calls_used}) 超过 "
                f"max_model_calls ({self.budget.max_model_calls})"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _check_time_order(self) -> Self:
        if (
            self.started_at is not None
            and self.completed_at is not None
            and self.completed_at < self.started_at
        ):
            msg = "completed_at 不得早于 started_at"
            raise ValueError(msg)
        return self

    @property
    def is_terminal(self) -> bool:
        """回合是否已到终态。"""
        return self.state.is_terminal

    @property
    def remaining_model_calls(self) -> int:
        """剩余可用模型调用次数。"""
        return self.budget.max_model_calls - self.model_calls_used
