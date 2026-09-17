"""自迭代层：经验、错误分类、模式发现与改进提案（任务书 §11）。

🔴 **本层的能力边界写在类型里，不是写在文档里。**

V0.1 的自迭代只做：收集经验；分类错误；发现重复模式；生成改进提案；
离线评估。它**不做**：自动改代码、自动改 Prompt 并上线、自动改变记忆规则、
自动改变认知宪法、自动将 Proposal 标记为生产生效（任务书 §11.1）。

这些"不做"不是靠自觉，而是靠三处结构性保证：

* ``ProposalStatus`` 里**根本没有** ``ACTIVE`` 成员（不变量 11）——
  没有这个值，就没有代码能把它设进去；
* ``PROPOSAL_ESCALATION_THRESHOLD = 3`` 与
  ``ImprovementProposal.meets_escalation_threshold`` 拒绝小于 2 的门槛
  （不变量 10）；
* :func:`~ai_psi.cognition.constitution.assert_no_automatic_promotion`
  在提案状态流转的入口处兜底。
"""

from __future__ import annotations

__all__: list[str] = []
