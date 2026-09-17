"""Provider 的返回值。

🔴 **本模块存在的理由：token 用量必须跟着返回值一起回来。**

任务书 §8.2 要求记录 ``input_token_count`` / ``output_token_count``，
但只有 Provider 自己知道它们。要在不改变调用形状的前提下拿到用量，
通常的做法是让 Provider 记一个 "last_usage" ——那是**状态**，
在并发下会串号，而且它把"哪一次调用的用量"变成了时序问题。

因此阶段 4 把返回值从裸的 ``T`` 改成 :class:`ProviderResponse`。
这是对 ADR-0003 / 任务书 §8.1 签名的**有意偏离**，登记在 ADR-0016。

⚠️ :attr:`TokenUsage.reasoning_tokens` **只是计数**。
推理模型的思维链内容（``reasoning_content``）永远不进入本对象、
不进入日志、不进入事件表——认知宪法红线一。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["ProviderResponse", "TokenUsage"]


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """一次模型调用的 token 用量。

    Attributes:
        input_tokens: 提示词 token 数。Provider 不报告时为 ``None``。
        output_tokens: 生成 token 数。
        reasoning_tokens: 其中用于**内部推理**的 token 数。

            🔴 **只记录数量，不记录内容。** 数量是成本核算需要的；
            内容属于"完整隐藏思维链"，红线一明令不保存。
            推理模型里这一项可能占输出的很大比例，
            不知道它就无法解释"为什么同样的提示词这次更贵"。
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None

    @property
    def total_tokens(self) -> int | None:
        """输入 + 输出；两者都缺时为 ``None``。"""
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)

    @property
    def is_reported(self) -> bool:
        """Provider 是否真的报告了用量。

        区分"用量为 0"与"没有用量信息"——前者是真实的，后者是未知的。
        """
        return self.input_tokens is not None or self.output_tokens is not None

    @classmethod
    def estimated(cls, *, text: str, prompt_chars: int) -> TokenUsage:
        """粗略估算用量（**只有 Mock 应该用它**）。

        真实 Provider 必须报告真实用量；估算值是"看起来像统计的猜测"，
        把它和真实数据混在一起会让成本核算失去意义。
        """
        return cls(
            input_tokens=max(1, prompt_chars // 4),
            output_tokens=max(1, len(text) // 4),
        )


@dataclass(frozen=True, slots=True)
class ProviderResponse[T]:
    """一次模型调用的完整结果。

    Attributes:
        value: 结构化对象或纯文本。
        usage: token 用量。
        finish_reason: Provider 报告的中止原因（``stop`` / ``length`` / ...）。
            🔴 ``length`` 意味着**输出被截断**——调用方必须当作失败处理，
            而不是把半截 JSON 当成结果。
        raw_hash: 原始响应的哈希。**只存哈希，不存内容**（红线一）。
        output_repair: 为拿到合法 JSON 所做的提取方式；``None`` 表示原样即可解析。

            🔴 **它是审计信息，不是"好消息"。** 需要修复说明模型没有按
            约定格式作答——这是提示词或供应商 JSON 模式的信号，
            应当在评测里被看见，而不是被静默吞掉。
    """

    value: T
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str | None = None
    raw_hash: str | None = None
    output_repair: str | None = None

    @property
    def was_truncated(self) -> bool:
        """输出是否因为长度上限被截断。"""
        return self.finish_reason is not None and self.finish_reason.lower() in {
            "length",
            "max_tokens",
            "max_output_tokens",
        }
