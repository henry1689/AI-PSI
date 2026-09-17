"""AI-PSI 应用层。

本层是**唯一允许发起持久化写入的地方**（架构规则 3，任务书开发原则第 17 条）。

认知模块（`cognition/`）与模型输出都**没有**数据库句柄——
它们产生领域对象，交给应用服务，由应用服务在事务边界内落库。

阶段 2 包含：

* :mod:`ai_psi.application.ports` —— 存储相关的 Port（Protocol）；
* :mod:`ai_psi.application.round_service` —— 认知回合的创建与状态转移；
* :mod:`ai_psi.application.replay_service` —— 历史回合回放。
"""

from __future__ import annotations

__all__: list[str] = []
