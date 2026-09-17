"""AI-PSI 认知层。

本包承载认知流程的领域逻辑。阶段 1 只包含两块**纯逻辑**：

* :mod:`ai_psi.cognition.state_machine` —— 认知回合状态机（纯转移表）；
* :mod:`ai_psi.cognition.constitution` —— 认知宪法与不变量断言。

阶段 3 将加入关切识别、问题框定、深度路由、各类分析器、
元认知与回答生成。这些模块通过 **Protocol（Port）** 访问外部世界，
不直接 import ``infrastructure/`` 或 ``providers/``（架构规则 2）。
"""

from __future__ import annotations

__all__: list[str] = []
