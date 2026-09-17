"""API 路由。

按任务书 §12 的分组划分。阶段 3 只实现**与认知回合直接相关**的三组：

* ``conversations``（§12.1 创建会话与提交消息）
* ``cognitive_rounds``（§12.1 回合状态、回答、结构化摘要）
* ``replay`` 与 ``health``（§12.5）

其余分组（``memories`` / ``feedback`` / ``proposals`` / ``beliefs``）
依赖阶段 5/6 才交付的能力，**本阶段不建空壳**（ADR-0012）。
"""

from __future__ import annotations

__all__: list[str] = []
