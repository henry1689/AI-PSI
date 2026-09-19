"""评测基础设施（阶段 7）。

🔴 **本包当前只实现 S1a 一个切片：Golden Case 契约 + 10 条行为案例 +
Mock 上的确定性执行 + JSON 结果。**

它**不是**完整的阶段 7。下面这些明确**不在**本包里：

* 指标层（``§16.1`` 的 15 项与 5 项硬指标）；
* ``attribution`` / ``replay`` / ``comparison`` / ``live_provider`` 案例类型；
* 归因漏报率（R46 / R58）；
* Markdown 报告、Baseline/Candidate 对比、发布阈值；
* 重执行式历史回放与独立评测数据库。

完整切分见 ``docs/implementation_plan.md`` 与阶段 7 规划；
``evals/`` 目录下只有**数据与入口**，可复用实现都在本包内，
以便从第一行起就受 mypy strict 与 ruff 管辖。
"""

from __future__ import annotations

__all__: list[str] = []
