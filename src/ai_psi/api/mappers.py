"""领域/事件负载 → API Schema 的**显式映射**（ADR-0006）。

🔴 **不做一键互转。**

``model_validate`` 一把梭在这里有个具体的坏处：事件负载里的字段集合
由**写入方**决定，而 API 字段集合由**暴露策略**决定。
两者一旦耦合，某天有人在事件负载里加了一个内部字段，
它就会自动出现在 API 响应里——没有人做过"要不要暴露它"的决定。

因此这里逐字段构造。啰嗦，但每一个字段都是有意为之。
"""

from __future__ import annotations

from typing import Any

from ai_psi.api.schemas import (
    JudgmentView,
    MemoryView,
    ModelInvocationView,
    ReflectionView,
)
from ai_psi.domain.events import ModelInvocationInfo
from ai_psi.domain.memories import Memory

__all__ = ["judgment_view", "memory_view", "model_invocation_view", "reflection_view"]


def judgment_view(payload: dict[str, Any] | None) -> JudgmentView | None:
    """把 ``judgment.created`` 事件中的判断负载映射为 API 视图。

    🔴 **``selected_hypothesis_ids`` 不外发。**
    内部假设 id 对客户端没有意义，暴露它只会让"客户端依赖了内部标识"
    成为日后重构的阻力。

    Args:
        payload: 事件负载中的 ``judgment`` 字段。

    Returns:
        API 视图；``payload`` 为空时返回 ``None``。
    """
    if not payload:
        return None
    return JudgmentView(
        conclusion=str(payload["conclusion"]),
        rationale_summary=[str(item) for item in payload.get("rationale_summary", [])],
        strongest_counterarguments=[
            str(item) for item in payload.get("strongest_counterarguments", [])
        ],
        unresolved_unknowns=[str(item) for item in payload.get("unresolved_unknowns", [])],
        applicability=[str(item) for item in payload.get("applicability", [])],
        confidence_band=payload["confidence_band"],
        confidence_basis=[str(item) for item in payload.get("confidence_basis", [])],
        revision_conditions=[str(item) for item in payload.get("revision_conditions", [])],
        recommended_epistemic_action=payload["recommended_epistemic_action"],
        uncertainty_type=str(payload["uncertainty_type"]),
    )


def reflection_view(payload: dict[str, Any] | None) -> ReflectionView | None:
    """把 ``metacognition.completed`` 中的反思负载映射为 API 视图。

    Args:
        payload: 事件负载中的 ``reflection`` 字段。

    Returns:
        API 视图；``payload`` 为空时返回 ``None``。
    """
    if not payload:
        return None
    return ReflectionView(
        new_evidence_present=bool(payload["new_evidence_present"]),
        new_reasoning_path_present=bool(payload["new_reasoning_path_present"]),
        repeated_claim_score=float(payload["repeated_claim_score"]),
        scope_drift_detected=bool(payload["scope_drift_detected"]),
        confirmation_bias_risk=str(payload["confirmation_bias_risk"]),
        user_pleasing_bias_risk=str(payload["user_pleasing_bias_risk"]),
        abstraction_escape_risk=str(payload["abstraction_escape_risk"]),
        unsupported_certainty_detected=bool(payload["unsupported_certainty_detected"]),
        missing_counterexample_detected=bool(payload["missing_counterexample_detected"]),
        stop_condition_reached=bool(payload["stop_condition_reached"]),
        marginal_value=str(payload["marginal_value"]),
        decision=str(payload["decision"]),
        reasons=[str(item) for item in payload.get("reasons", [])],
    )


def model_invocation_view(info: ModelInvocationInfo) -> ModelInvocationView:
    """把模型调用记录映射为 API 视图。

    🔴 **响应哈希是唯一与内容有关的东西**，而且它不可逆。
    不变量 18 要求"记录模型与 Prompt 版本"，本视图正好暴露那两项。

    Args:
        info: 调用审计记录。

    Returns:
        API 视图。
    """
    return ModelInvocationView(
        invocation_id=info.invocation_id,
        provider=info.provider,
        model=info.model,
        task_name=info.task_name,
        prompt_version=info.prompt_version,
        latency_ms=info.latency_ms,
        retry_count=info.retry_count,
        result_status=info.result_status,
        response_hash=info.response_hash,
    )


def memory_view(memory: Memory, *, conflicts_within_results: bool = False) -> MemoryView:
    """把领域记忆映射为 API 视图。

    🔴 **这里包含正文，这是有意的。** 记忆是用户自己的数据，
    ``GET /users/{id}/memories`` 与导出的全部意义就是把它们交还用户。
    需要"不含正文"的是**审计**（事件负载），那条规则由
    :func:`ai_psi.memory.redaction.audit_payload_for_memory` 与
    它背后的断言强制，两者不是一回事。

    ``embedding_version`` **不外发**：它是检索实现的内部约定，
    客户端拿到它既做不了什么，又会让"换向量 Provider"看起来像是
    一个破坏性变更。

    Args:
        memory: 领域记忆。
        conflicts_within_results: 本条与同一批结果中其他成员是否存在显式冲突。

    Returns:
        API 视图。
    """
    return MemoryView(
        id=memory.id,
        user_id=memory.user_id,
        memory_type=memory.memory_type,
        content=memory.content,
        status=memory.status,
        verification_status=memory.verification_status,
        sensitivity=memory.sensitivity,
        retention_policy=memory.retention_policy,
        applicability=list(memory.applicability),
        valid_from=memory.valid_from,
        valid_until=memory.valid_until,
        supersedes_id=memory.supersedes_id,
        contradicts_ids=list(memory.contradicts_ids),
        conflicts_within_results=conflicts_within_results,
        created_at=memory.created_at,
        updated_at=memory.updated_at,
        version=memory.version,
    )
