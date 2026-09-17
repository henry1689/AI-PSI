"""Prompt 层：契约、版本与模板。

🔴 **提示词不许散落在业务代码里**（任务书 §8.3）。
每一次模型调用都对应本包中的一个 :class:`~ai_psi.prompts.registry.PromptContract`：
有任务名、有语义版本、有输入/输出 Schema、有长度上限、有测试样例、有变更记录。

散落的 f-string 提示词是这个系统最危险的隐性技术债——
它无法测试、无法版本化、无法回滚，而且**改动的影响不可追踪**。
"""

from __future__ import annotations

__all__: list[str] = []
