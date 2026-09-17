"""HTTP API 层（任务书 §12）。

🔴 **本层是唯一把领域对象翻译成 HTTP 的地方**，也是唯一会碰
FastAPI 的地方。分层规则要求它只能向下依赖：

``api/ → application/ → cognition/ memory/ reliability/ → domain/``

API Schema **不复用领域对象**（ADR-0006）——客户端能看到的字段集合
与领域对象的字段集合是两件不同的事，混用会让"某个内部字段被无意暴露"
变成一次改动的副作用而不是一个需要显式做出的决定。
"""

from __future__ import annotations

__all__: list[str] = []
