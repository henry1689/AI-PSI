"""长期记忆的策略层。

本包按任务书 §4 的规划分两批落地，**中间不建空壳**（ADR-0012）：

* 阶段 3：:mod:`~ai_psi.memory.write_policy`——场景 F、H 的验收需要它；
* 阶段 5：``retrieval`` / ``ranking`` / ``conflict_detection`` /
  ``lifecycle`` / ``redaction``——长期记忆落地时一并交付。

记忆错误的代价与别处不同：**它是累积的**。
一次错误的回答只影响一次交互，一次错误的记忆写入会持续影响此后所有回合。
所以写入策略是这一层最先需要落地的东西。
"""

from __future__ import annotations

__all__: list[str] = []
