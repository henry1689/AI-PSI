"""AI-PSI 基础设施层。

本包是 **Ports 的具体实现（Adapter）**：数据库、事件存储、任务队列、日志。

架构规则（ADR-0001）：

* 本包**可以**依赖 `domain/` 与标准库/第三方库；
* 本包**不得**被 `domain/` 或 `cognition/` 直接 import——
  它们只依赖 Protocol，具体实现由组合根注入。

阶段 2 包含：

* :mod:`ai_psi.infrastructure.db` —— SQLAlchemy 模型、会话、仓储、工作单元；
* :mod:`ai_psi.infrastructure.event_store` —— 事件存储（append-only）；
* :mod:`ai_psi.infrastructure.logging` —— structlog 配置与脱敏。
"""

from __future__ import annotations

__all__: list[str] = []
