"""可靠性层：预算、重复度检测与置信度上限。

⚠️ **目录偏差**：任务书 §4 把这三个模块规划在这里，但阶段 3 的验收
（"循环永不超预算"、反刍停止、不变量 3）已经需要它们。
因此本包在阶段 3 建立，只包含当前真正有内容的三块；
`circuit_breaker.py` / `health.py` / `invariants.py` 留给阶段 4 与阶段 8
（不建空壳，ADR-0012）。
"""

from __future__ import annotations

__all__: list[str] = []
