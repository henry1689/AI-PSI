"""API 路由。

按任务书 §12 的分组划分，逐阶段落地，**不建空壳**（ADR-0012）：

* ``conversations`` / ``cognitive_rounds``（§12.1）、``replay``（§12.5）
  ——阶段 3；
* ``health``（§12.5）——阶段 3 建、阶段 6 补齐认知资产维度；
* ``memories``（§12.3）——阶段 5；
* ``feedback``（§12.2）与 ``proposals``（§12.4）——阶段 6；
* ``beliefs``（§12.3 第一条）——**本版本不做**：
  信念只活在事件流里、不是记忆，列出它需要另建判断投影（ADR-0017 §7）。
"""

from __future__ import annotations

__all__: list[str] = []
