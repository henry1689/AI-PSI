"""结构化输出的提取与修复（任务书 §8.1"结构化解析失败处理"）。

🔴 **本模块处理的是"真实模型不按 Schema 说话"这件事。**

Mock 永远返回现成的字典；真实模型会加解释、会包代码块、
会在 JSON 前后写一句话。任务书 §8.1 把这叫"结构化输出修复"。

**修复的边界必须清楚：**

* 只做**提取**——把 JSON 从周围的文字里找出来；
* **不做猜测**——不补字段、不改枚举值、不"理解"模型想说什么。
  猜出来的对象会以"模型本来就这么答"的身份进入事件流，
  此后无法与真实输出区分（prompts/payload.py 的同类原则）。

🔴 **失败时绝不泄漏原始内容。**
模型输出可能包含用户正文或注入文本，错误信息只带
"为什么失败"与"多长"，不带内容本身（`docs/security.md` §3）。
"""

from __future__ import annotations

import json
from typing import Any, Final

from ai_psi.domain.exceptions import StructuredOutputError

__all__ = ["describe_shape", "extract_json_object", "strip_code_fence"]

_FENCE_PREFIXES: Final[tuple[str, ...]] = ("```json", "```JSON", "```")


def strip_code_fence(text: str) -> str:
    """剥掉最外层的 Markdown 代码块围栏。

    模型经常把 JSON 包在 ```` ```json ... ``` ```` 里，
    即使提示词明确要求"只返回 JSON"。

    Args:
        text: 原始文本。

    Returns:
        去掉围栏后的文本；没有围栏时原样返回。
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    for prefix in _FENCE_PREFIXES:
        if not stripped.startswith(prefix):
            continue
        body = stripped[len(prefix) :]
        if body.endswith("```"):
            body = body[:-3]
        return body.strip()
    return stripped  # pragma: no cover - startswith("```") 时必然命中某个前缀


def _scan_balanced_object(text: str) -> str | None:
    """扫描出第一个**括号配平**的 JSON 对象。

    必须感知字符串与转义，否则 ``{"a": "}"}`` 里的右括号会被误判为结束。

    Args:
        text: 已去掉围栏的文本。

    Returns:
        配平的子串；找不到时返回 ``None``。
    """
    start = text.find("{")
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def extract_json_object(text: str) -> dict[str, Any]:
    """从模型返回的文本里提取 JSON 对象。

    尝试顺序：原样解析 → 去围栏 → 扫描配平子串。
    三者都失败就报错，**不做任何猜测性修补**。

    Args:
        text: 模型返回的原始文本。

    Returns:
        解析出的 JSON 对象。

    Raises:
        StructuredOutputError: 三种方式都拿不到合法的 JSON 对象。
            🔴 错误的 ``context`` 只含诊断信息（长度、失败方式），
            **不含模型输出内容**。
    """
    parsed, _ = extract_json_object_with_strategy(text)
    return parsed


def extract_json_object_with_strategy(text: str) -> tuple[dict[str, Any], str | None]:
    """同 :func:`extract_json_object`，但**同时报告用了哪种提取方式**。

    为什么要报告：如果模型很少直接给出合法 JSON、总要靠去围栏或扫描，
    那是一个应当被看见的信号——它说明提示词约束没生效，
    或者该换用供应商的 JSON 模式。静默修复会让这个信号永远不可见。

    Args:
        text: 模型返回的原始文本。

    Returns:
        ``(解析出的对象, 修复方式)``。修复方式为 ``None`` 表示原样解析成功。

    Raises:
        StructuredOutputError: 三种方式都拿不到合法的 JSON 对象。
    """
    if not text.strip():
        msg = "模型返回了空内容，无法提取结构化输出"
        raise StructuredOutputError(msg, validation_errors=("empty_response",))

    stripped = text.strip()
    defenced = strip_code_fence(text)
    balanced = _scan_balanced_object(defenced)

    attempts: list[tuple[str, str | None]] = [(stripped, None), (defenced, "stripped_code_fence")]
    if balanced is not None:
        attempts.append((balanced, "scanned_balanced_object"))

    reasons: list[str] = []
    for candidate, strategy in attempts:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            reasons.append(f"{exc.msg} (pos {exc.pos})")
            continue
        if isinstance(parsed, dict):
            # 原样解析成功时不必报告"去掉空白"这种无意义的差异
            return parsed, strategy if candidate != stripped else None
        reasons.append(f"顶层不是 JSON 对象，而是 {type(parsed).__name__}")

    msg = "模型输出无法解析为 JSON 对象（已尝试原样 / 去代码块 / 括号配平扫描）"
    raise StructuredOutputError(
        msg,
        validation_errors=tuple(dict.fromkeys(reasons)) or ("no_json_object_found",),
    )


def describe_shape(text: str) -> str:
    """描述输出的**形状**而不泄漏内容（仅用于日志与诊断）。

    🔴 **连"开头 20 个字符"都不给。** 模型输出的开头完全可能就是
    用户正文（例如它复述了问题），把它写进日志等于绕开了脱敏。

    Args:
        text: 模型输出。

    Returns:
        形如 ``"len=523 fenced=True starts_with_brace=False"`` 的说明。
    """
    stripped = text.strip()
    return (
        f"len={len(text)} "
        f"fenced={stripped.startswith('```')} "
        f"starts_with_brace={stripped.startswith('{')} "
        f"ends_with_brace={stripped.endswith('}')}"
    )
