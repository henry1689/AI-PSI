"""提示词中的结构化输入：编码与提取。

🔴 **这是"数据"与"指令"之间的那道墙。**

任务书 §17.2 要求：外部资料与用户内容**只能作为数据进入**。
提示词模板把指令写在围栏之外，把数据放进围栏之内；
只要围栏无法被内容提前闭合，注入文本就永远出不了数据区。

防线是这样建立的：

1. 数据以 JSON 编码后放入带专用标识的围栏块；
2. **JSON 编码后再把 `` ` `` 转义成 ``\\u0060``** —— 用户内容里就算写满
   三连反引号，也不可能拼出一个真正的闭合标记；
3. 提取时用同一个标识匹配，其余内容一律当作数据。

第 2 步是关键。仅靠"用 JSON 编码"是不够的：JSON **不转义反引号**，
所以一条包含 ```` ``` ```` 的用户消息原本可以直接截断数据块。
这个缺陷由 ``tests/unit/test_prompt_registry.py`` 的注入用例钉死。
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

from pydantic import BaseModel

from ai_psi.domain.exceptions import StructuredOutputError

__all__ = [
    "PAYLOAD_FENCE",
    "encode_payload",
    "extract_payload",
    "extract_payload_or_none",
    "render_payload_block",
]

#: 数据块的语言标识。
#:
#: 刻意不用 ``json``：提示词里常有说明性的 JSON 示例，
#: 用通用标识会让"哪一段是输入"变成猜测。
PAYLOAD_FENCE: Final[str] = "ai-psi-input"

_PAYLOAD_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"^```{PAYLOAD_FENCE}[ \t]*\n(.*?)\n```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)


def _escape_backticks(encoded: str) -> str:
    """把 JSON 文本中的反引号转义为 ``\\u0060``。

    JSON 标准不转义反引号，因此这是必需的额外一步：
    没有它，用户内容里的三连反引号可以提前闭合数据块。

    替换是**无损**的——``\\u0060`` 在 JSON 中还原为 ``\\``` 本身，
    提取方解析回来的字符串与原文逐字相同。

    Args:
        encoded: ``json.dumps`` 的输出。

    Returns:
        反引号已转义的 JSON 文本。
    """
    return encoded.replace("`", "\\u0060")


def encode_payload(payload: BaseModel | dict[str, Any]) -> str:
    """把结构化输入编码为**不可逃逸**的 JSON 文本。

    Args:
        payload: 输入对象或字典。

    Returns:
        已转义反引号的 JSON 文本（无围栏）。
    """
    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    return _escape_backticks(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def render_payload_block(payload: BaseModel | dict[str, Any]) -> str:
    """把结构化输入渲染为带围栏的数据块。

    Args:
        payload: 输入对象或字典。

    Returns:
        形如 ```` ```ai-psi-input ... ``` ```` 的文本块。
    """
    return f"```{PAYLOAD_FENCE}\n{encode_payload(payload)}\n```"


def extract_payload(messages: list[Any]) -> dict[str, Any]:
    """从渲染后的消息中解析结构化输入。

    Mock Provider 用它"看见"与真实模型相同的输入。

    Args:
        messages: 渲染后的消息列表（任何带 ``content: str`` 的对象）。

    Returns:
        解析出的输入字典。

    Raises:
        StructuredOutputError: 不存在输入块，或输入块不是 JSON 对象。
    """
    payload = extract_payload_or_none(messages)
    if payload is None:
        msg = (
            f"渲染后的提示词中找不到 ```{PAYLOAD_FENCE} 输入块——"
            "请检查 Prompt 模板是否包含 {{input}} 占位符"
        )
        raise StructuredOutputError(msg)
    return payload


def extract_payload_or_none(messages: list[Any]) -> dict[str, Any] | None:
    """尽力解析结构化输入；不存在时返回 ``None``。

    从**最后一条**含输入块的消息中取**最后一个**输入块——
    多轮补充说明时以最新的为准。

    Args:
        messages: 渲染后的消息列表。

    Returns:
        解析出的字典；找不到输入块时返回 ``None``。

    Raises:
        StructuredOutputError: 输入块存在但不是合法 JSON 对象。
    """
    for message in reversed(messages):
        content = getattr(message, "content", message)
        if not isinstance(content, str):
            continue
        matches = _PAYLOAD_PATTERN.findall(content)
        if not matches:
            continue
        try:
            parsed = json.loads(matches[-1])
        except json.JSONDecodeError as exc:
            msg = f"提示词中的 ```{PAYLOAD_FENCE} 块不是合法 JSON"
            raise StructuredOutputError(msg, validation_errors=(str(exc),)) from exc
        if not isinstance(parsed, dict):
            msg = f"```{PAYLOAD_FENCE} 块必须是 JSON 对象，收到 {type(parsed).__name__}"
            raise StructuredOutputError(msg)
        return parsed
    return None
