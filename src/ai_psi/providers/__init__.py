"""LLM Provider 层。

🔴 **这里是"模型"与"系统"之间唯一的缝。**

上层（`cognition/`、`application/`）只依赖 :class:`~ai_psi.providers.base.LLMProvider`
协议；本包的任何具体实现都不被上层 import。阶段 4 加入 Anthropic /
OpenAI-compatible 实现时，业务代码一行都不用改（ADR-0003）。
"""

from __future__ import annotations

__all__: list[str] = []
