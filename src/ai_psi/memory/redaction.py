"""审计脱敏与数据导出（任务书 §10.5、§17.1）。

任务书 §10.5 有一条容易被略过的要求：

> **审计信息不得保留被删除内容正文。**

这条要求之所以重要，是因为它堵住了一个很自然的退路——
"内容删了，但审计事件里留一份副本，方便日后追溯"。
那样做的结果是：**用户以为删掉的东西，其实一直躺在事件表里**，
而事件表是只追加的，再也删不掉。

因此本模块提供两件互相对称的东西：

* :func:`audit_payload_for_memory` —— **给审计用，绝不含正文**；
* :func:`export_record` —— **给用户用，含正文**（导出本来就是
  把用户自己的数据交还给他）。

区分它们的方式不是"记得别写错"，而是 :func:`assert_audit_payload_safe`
——它在构造审计负载之后、写入事件之前执行，把"有没有夹带正文"
变成一条会当场失败的断言。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from ai_psi.domain.exceptions import ConstitutionViolationError
from ai_psi.domain.memories import Memory
from ai_psi.memory.lifecycle import retention_requirements

__all__ = [
    "FORBIDDEN_AUDIT_KEYS",
    "USER_DATA_EXPORT_VERSION",
    "assert_audit_payload_safe",
    "audit_payload_for_memory",
    "export_record",
]

#: 审计负载里不允许出现的键名。
#:
#: ⚠️ 这是一道**兜底**，不是主要防线。主要防线是
#: :func:`audit_payload_for_memory` 根本不接收正文——
#: 一个拿不到正文的函数不可能把它写进负载。
#: 列在这里是因为事件负载也可能被人手工组装，
#: 而那正是最容易出错的地方。
FORBIDDEN_AUDIT_KEYS: Final[frozenset[str]] = frozenset(
    {"content", "text", "body", "message", "raw", "transcript"}
)

#: 导出格式版本。用户拿到一份导出文件之后，格式会独立于代码演进，
#: 因此它必须自带版本号，否则"这份文件是哪个版本导出的"无从判断。
USER_DATA_EXPORT_VERSION: Final[str] = "1.0.0"


def assert_audit_payload_safe(payload: dict[str, Any], *, operation: str) -> None:
    """断言一个审计负载里没有夹带正文。

    Raises:
        ConstitutionViolationError: 负载含有禁止的键名。
    """
    offending = sorted(key for key in payload if key.lower() in FORBIDDEN_AUDIT_KEYS)
    if offending:
        msg = (
            f"{operation} 的审计负载含有疑似正文的字段 {offending}。"
            "审计信息不得保留内容正文（任务书 §10.5）——"
            "删除的意义在于内容真的不再存在"
        )
        raise ConstitutionViolationError(
            msg,
            invariant_id="S10.5",
            context={"operation": operation, "offending_keys": offending},
        )


def audit_payload_for_memory(memory: Memory, *, operation: str) -> dict[str, Any]:
    """构造一条记忆的**审计安全**负载。

    含：id、类型、状态、敏感度、**内容长度**。
    不含：内容正文，也不含内容的哈希。

    为什么不含哈希：哈希是内容的**可验证承诺**。对一段取值空间很小的
    内容（"用户有抑郁症"这样的短句），持有哈希的人可以枚举候选、
    逐个比对来还原原文。删除之后还要留下这样一条线索，
    与"内容真的不再存在"相差不远，但不是。

    Args:
        memory: 目标记忆。
        operation: 操作名，写入负载与错误信息。

    Returns:
        可安全写入事件的负载。
    """
    payload: dict[str, Any] = {
        "operation": operation,
        "memory_id": str(memory.id),
        "memory_type": memory.memory_type.value,
        "status": memory.status.value,
        "sensitivity": memory.sensitivity.value,
        "content_length": len(memory.content),
    }
    assert_audit_payload_safe(payload, operation=operation)
    return payload


def export_record(memory: Memory, *, now: datetime) -> dict[str, Any]:
    """把一条记忆转换为导出用的字典。

    与审计负载相反，导出**必须**包含正文：任务书 §10.5 要求的
    "按 user_id 导出"如果导出的是一堆 id 和长度，那就不是导出。

    Args:
        memory: 目标记忆。
        now: 导出时刻，用于标注当前是否有有效性与过期信息。

    Returns:
        可 JSON 序列化的字典。
    """
    expired = memory.valid_until is not None and memory.valid_until <= now
    return {
        "id": str(memory.id),
        "user_id": str(memory.user_id) if memory.user_id is not None else None,
        "memory_type": memory.memory_type.value,
        "content": memory.content,
        "status": memory.status.value,
        "verification_status": memory.verification_status.value,
        "sensitivity": memory.sensitivity.value,
        "retention_policy": memory.retention_policy.value,
        "retention_note": retention_requirements(memory),
        "applicability": list(memory.applicability),
        "access_scope": list(memory.access_scope),
        "valid_from": memory.valid_from.isoformat(),
        "valid_until": memory.valid_until.isoformat() if memory.valid_until else None,
        "expired_at_export": expired,
        "supersedes_id": str(memory.supersedes_id) if memory.supersedes_id else None,
        "superseded": memory.status.value == "superseded",
        "contradicts_ids": [str(item) for item in memory.contradicts_ids],
        "source_event_ids": [str(item) for item in memory.source_event_ids],
        "evidence_ids": [str(item) for item in memory.evidence_ids],
        "created_at": memory.created_at.isoformat(),
        "updated_at": memory.updated_at.isoformat(),
        "version": memory.version,
    }


def export_bundle(
    *,
    user_id: UUID | None,
    memories: Sequence[Memory],
    now: datetime,
) -> dict[str, Any]:
    """构造一份完整的用户数据导出。

    包含**被取代、已删除的记忆**。用户要"导出我的数据"时，
    他有权看到完整的版本链——"为什么发生过修正"正是靠这条链回答的
    （任务书 §10.4）。只导出有效记忆会让纠错痕迹凭空消失。

    Args:
        user_id: 导出目标。
        memories: 该用户的全部记忆（含非活跃）。
        now: 导出时刻。

    Returns:
        可 JSON 序列化的导出包。
    """
    records = [export_record(memory, now=now) for memory in memories]
    return {
        "export_version": USER_DATA_EXPORT_VERSION,
        "user_id": str(user_id) if user_id is not None else None,
        "generated_at": now.isoformat(),
        "memory_count": len(records),
        "active_count": sum(1 for memory in memories if memory.is_default_retrievable),
        "memories": records,
    }
