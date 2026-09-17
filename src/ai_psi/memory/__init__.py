"""长期记忆的策略层。

⚠️ **目录偏差**：任务书 §4 规划了本包的全部内容（retrieval / ranking /
write_policy / conflict_detection / lifecycle / redaction），
但阶段 3 的验收（场景 F、H）已经需要 **写入策略**。

因此本包在阶段 3 建立，且只包含 :mod:`ai_psi.memory.write_policy`——
其余模块留给阶段 5，不建空壳（ADR-0012、ADR-0015）。

记忆错误的代价与别处不同：**它是累积的**。
一次错误的回答只影响一次交互，一次错误的记忆写入会持续影响此后所有回合。
所以写入策略是这一层最先需要落地的东西。
"""

from __future__ import annotations

__all__: list[str] = []
