"""评测结果的两种输出：**原始**与 **canonical**（阶段 7 · S1a）。

## 为什么必须分成两份

一份结果里混着两类东西：

* **语义稳定**：案例 id、类别、路由深度、终态、断言名、expected、observed、passed；
* **易变**：回合 id（每次运行都不同）、异常原文里的绝对路径、回答措辞。

把它们混在一份文件里，会得到一个**看起来可比较、实际不可比较**的产物：
任何人只要跑两次就会看到 diff，于是"结果不一致"这件事被日常噪声掩盖，
真正的不一致反而没人看。

因此：

* :func:`raw_payload` —— **诊断用**。保留回合 id、断言判定原文、异常原文、
  回答正文。它**不承诺**两次运行逐字节一致。
* :func:`canonical_payload` —— **比较用**。只保留语义稳定字段。
  两次相同的 Mock 运行必须**逐字节一致**（由 ``cmp`` 验证）。

🔴 **不要**声称原始输出也逐字节一致——它包含运行期生成的标识符。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

from ai_psi.evaluation.runner import CaseResult, RunResult

__all__ = [
    "canonical_payload",
    "dumps",
    "raw_payload",
    "write_reports",
]

#: canonical 输出里 ``observation`` 保留的字段。
#:
#: ⚠️ 这里**没有** ``response_text``：措辞不是语义。它不进 canonical，
#: 否则"改一句模板话术"就会表现为"评测结果变了"。
_CANONICAL_OBSERVATION_FIELDS: Final[tuple[str, ...]] = (
    "state",
    "depth",
    "stop_reason_present",
    "response_present",
    "judgment_present",
    "model_calls_used",
    "metacognitive_loops",
    "confidence_band",
    "epistemic_action",
    "unresolved_unknown_count",
    "counterargument_count",
    "hypothesis_count",
    "analysis_kinds",
)


def _observation_payload(result: CaseResult, *, canonical: bool) -> dict[str, Any] | None:
    observation = result.observation
    if observation is None:
        return None
    dumped = observation.model_dump(mode="json")
    if not canonical:
        return dumped
    return {name: dumped[name] for name in _CANONICAL_OBSERVATION_FIELDS}


def _case_payload(result: CaseResult, *, canonical: bool) -> dict[str, Any]:
    assertions = [
        {
            "name": assertion.name,
            "mode": assertion.mode,
            "expected": assertion.expected,
            "observed": assertion.observed,
            "passed": assertion.passed,
            # 判定原文含阈值比较的中间说明；原始输出保留它以便诊断，
            # canonical 丢弃它——它的措辞会随实现改写而变化。
            **({} if canonical else {"detail": assertion.detail}),
        }
        for assertion in result.assertions
    ]
    payload: dict[str, Any] = {
        "case_id": result.case_id,
        "category": result.category,
        "observation": _observation_payload(result, canonical=canonical),
        "assertions": assertions,
        "passed": result.passed,
        "failure_reason": result.failure_reason,
    }
    if not canonical:
        # 🔴 只在原始输出里出现的易变字段。
        payload["cognitive_round_id"] = (
            None if result.cognitive_round_id is None else str(result.cognitive_round_id)
        )
        payload["failure_detail"] = result.failure_detail
    return payload


def canonical_payload(result: RunResult) -> dict[str, Any]:
    """构造 canonical（可比较）结果。"""
    return {
        "schema_version": result.schema_version,
        "case_type": result.case_type,
        "provider": result.provider,
        "summary": {
            "total": result.total,
            "passed": result.passed,
            "failed": result.failed,
            "passed_overall": result.passed_overall,
        },
        "cases": [_case_payload(case, canonical=True) for case in result.cases],
    }


def raw_payload(result: RunResult) -> dict[str, Any]:
    """构造原始（诊断）结果。**不承诺逐字节可复现。**"""
    return {
        "schema_version": result.schema_version,
        "case_type": result.case_type,
        "provider": result.provider,
        "summary": {
            "total": result.total,
            "passed": result.passed,
            "failed": result.failed,
            "passed_overall": result.passed_overall,
        },
        "cases": [_case_payload(case, canonical=False) for case in result.cases],
    }


def dumps(payload: dict[str, Any]) -> str:
    """把结果序列化成**稳定**的 JSON 文本。

    稳定来自四条固定规则，缺一条两次运行就可能不逐字节相同：

    1. ``sort_keys=True`` —— 对象字段顺序固定；
    2. ``ensure_ascii=False`` —— 中文不进 escaped 形式（仍按 UTF-8 落盘）；
    3. ``indent=2`` —— 给人看也能读，且格式唯一；
    4. 结尾恰好一个换行 —— 文件结束规则固定。

    Args:
        payload: :func:`raw_payload` 或 :func:`canonical_payload` 的产物。

    Returns:
        JSON 文本，UTF-8 可写。
    """
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def write_reports(
    result: RunResult,
    *,
    raw_path: Path,
    canonical_path: Path,
) -> None:
    """把原始与 canonical 两份结果写到磁盘。

    两份报告都由**同一个内存结构**渲染——不允许各自生成。

    Args:
        result: 运行结果。
        raw_path: 原始输出路径。
        canonical_path: canonical 输出路径。
    """
    for path in (raw_path, canonical_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n"：Windows 上也不写 CRLF，否则两份"逐字节一致"的文件
    # 会在跨平台比较时先败在行尾上。
    raw_path.write_text(dumps(raw_payload(result)), encoding="utf-8", newline="\n")
    canonical_path.write_text(dumps(canonical_payload(result)), encoding="utf-8", newline="\n")
