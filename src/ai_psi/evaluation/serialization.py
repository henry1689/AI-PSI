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
    "REPORT_SUMMARY_FIELDS",
    "REPORT_TOP_LEVEL_FIELDS",
    "ReportFormatError",
    "canonical_payload",
    "dumps",
    "parse_report",
    "raw_payload",
    "write_reports",
]

#: 报告 payload 的顶层字段。
#:
#: 🔴 **产物形状的唯一真相来源**：写（:func:`raw_payload` /
#: :func:`canonical_payload`）与读（:func:`parse_report`）用同一份常量。
#: 分两份写，就会出现"加了字段但只有写的那边知道"——而那正是
#: "读得进来但读漏了"这一类 bug 的温床。
REPORT_TOP_LEVEL_FIELDS: Final[tuple[str, ...]] = (
    "case_type",
    "cases",
    "execution_mode",
    "manifest",
    "metrics",
    "provider",
    "schema_version",
    "storage_isolation",
    "summary",
)

#: ``summary`` 子对象的字段。**报告把汇总嵌在这里**，而 ``RunResult``
#: 上是扁平的——这层映射就是 :func:`parse_report` 存在的原因。
REPORT_SUMMARY_FIELDS: Final[tuple[str, ...]] = (
    "failed",
    "passed",
    "passed_overall",
    "total",
)


class ReportFormatError(ValueError):
    """报告 payload 的形状不符合本版本的产物格式。

    ⚠️ 与"两份结果不可比较"无关：这个异常说明**这份文件不是本工具
    产出的报告**，或者产自另一个结构版本。
    """


#: canonical 输出里 ``observation`` 保留的字段。
#:
#: ⚠️ 这里**没有** ``response_text``：措辞不是语义。它不进 canonical，
#: 否则"改一句模板话术"就会表现为"评测结果变了"。
_CANONICAL_OBSERVATION_FIELDS: Final[tuple[str, ...]] = (
    "state",
    "depth",
    "stop_reason_present",
    # 🔴 停止原因的**实际值**也进 canonical（阶段 7 · S5）：它来自回合结果的
    # 结构化字段，不是措辞；而 S4 的 ``stop_reason_distribution`` 既然已经
    # 进了 canonical，逐案例的那份就该一起进——只给聚合不给明细，
    # 读者没法把分布上的一个数字对回具体是哪条案例。
    "stop_reason",
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
            # 🔴 **观测是否成立**（阶段 7 · S5）：它把"没读到"与"读到了但
            # 对不上"变成两个可被 schema 校验的值，而不是靠 ``observed is None``
            # 反推。它**语义稳定**（不是措辞），因此原始与 canonical 都带它。
            "observation_status": assertion.observation_status,
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
        # 🔴 **结构化的失败种类**（阶段 7 · S5）：``execution_error`` 与
        # ``assertion_failure`` 的对策完全不同。S5 的案例转换靠它分类，
        # **不**读 ``failure_reason`` 的文本——那是一句会随措辞变化的散文。
        "failure_kind": result.failure_kind,
        "failure_reason": result.failure_reason,
    }
    if not canonical:
        # 🔴 只在原始输出里出现的易变字段。
        payload["cognitive_round_id"] = (
            None if result.cognitive_round_id is None else str(result.cognitive_round_id)
        )
        payload["failure_detail"] = result.failure_detail
    return payload


def _isolation_payload(result: RunResult, *, canonical: bool) -> dict[str, Any] | None:
    """存储隔离证据。

    🔴 **canonical 里没有库名。** 两次独立的 PostgreSQL 运行用的是两个
    **不同**的 evaluation database（每次都要是全新的、可丢弃的），
    库名进 canonical 会让"逐字节一致"这条承诺当场失效。

    留下来的三个布尔是**结论**，不是过程数据：
    "两个库不是同一个""参考快照没变""参考学习状态没变"。
    它们是稳定的，而且是读者真正要看的。
    """
    isolation = result.storage_isolation
    if isolation is None:
        return None
    if canonical:
        return {
            "reference_learning_state_unchanged": isolation.reference_learning_state_unchanged,
            "reference_snapshot_unchanged": isolation.reference_snapshot_unchanged,
            "same_database": isolation.same_database,
        }
    return {
        "evaluation_database": isolation.evaluation_database,
        "reference_database": isolation.reference_database,
        "same_database": isolation.same_database,
        "reference_snapshot_unchanged": isolation.reference_snapshot_unchanged,
        "reference_learning_state_unchanged": isolation.reference_learning_state_unchanged,
    }


def _manifest_payload(result: RunResult, *, canonical: bool) -> dict[str, Any] | None:
    """可复现性清单。

    * **原始输出**：完整清单——诊断时需要的正是全部身份；
    * **canonical**：只有稳定且影响语义比较的**子集**
      （见 ``ReproducibilityManifest.canonical_identity``）。

    ⚠️ 清单本身**不含**秘密、数据库 URL、绝对路径与 Prompt 正文；
    子集里更是刻意去掉了 ``python_version`` 与 ``working_tree_clean``。
    """
    manifest = result.manifest
    if manifest is None:
        return None
    if canonical:
        return manifest.canonical_identity()
    return manifest.model_dump(mode="json")


def _metrics_payload(result: RunResult) -> dict[str, Any] | None:
    """指标层结果（阶段 7 · S4）。

    🔴 **原始与 canonical 用的是同一份**：指标模型里**没有**易变字段——
    计数是整数，比率是固定精度的字符串，分布是排好序的映射，
    失败索引只有 ID 与稳定分类。因此不需要像清单那样做子集投影。

    ⚠️ 这**不是**放松要求：如果将来往指标里加了时间戳或耗时，
    就必须在这里重新划一次边界，而不是让它顺手进 canonical。
    """
    metrics = result.metrics
    if metrics is None:
        return None
    return metrics.model_dump(mode="json")


def canonical_payload(result: RunResult) -> dict[str, Any]:
    """构造 canonical（可比较）结果。"""
    return {
        "schema_version": result.schema_version,
        "case_type": result.case_type,
        "provider": result.provider,
        "execution_mode": result.execution_mode,
        "storage_isolation": _isolation_payload(result, canonical=True),
        "manifest": _manifest_payload(result, canonical=True),
        "metrics": _metrics_payload(result),
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
        "execution_mode": result.execution_mode,
        "storage_isolation": _isolation_payload(result, canonical=False),
        "manifest": _manifest_payload(result, canonical=False),
        "metrics": _metrics_payload(result),
        "summary": {
            "total": result.total,
            "passed": result.passed,
            "failed": result.failed,
            "passed_overall": result.passed_overall,
        },
        "cases": [_case_payload(case, canonical=False) for case in result.cases],
    }


def parse_report(payload: dict[str, Any]) -> RunResult:
    """把一份报告 payload 还原成 :class:`~ai_psi.evaluation.runner.RunResult`。

    🔴 **不能直接 ``RunResult.model_validate(payload)``**：报告把汇总
    （``total`` / ``passed`` / ``failed`` / ``passed_overall``）放在
    ``summary`` 子对象里，而 ``RunResult`` 上是**扁平**的四个字段。
    这层映射必须显式写出来，否则"字段对不上"会表现为一堆
    ``missing`` 错误，读的人还得自己去猜哪一层错了。

    严格性：

    * 顶层字段必须是 :data:`REPORT_TOP_LEVEL_FIELDS` 的**恰好**那个集合——
      多一个少一个都拒绝。多出来的字段意味着这份报告产自另一个结构版本，
      而"忽略不认识的东西"正是让版本漂移静默发生的方式；
    * ``summary`` 同理，对照 :data:`REPORT_SUMMARY_FIELDS`；
    * 其余字段交给 pydantic 逐层校验（``RunResult`` 及其子模型全是
      ``extra="forbid"``）。

    Args:
        payload: 已解析的 JSON 对象。

    Returns:
        还原后的运行结果。

    Raises:
        ReportFormatError: 字段集合不对，或清单是 canonical 的扁平子集。
        pydantic.ValidationError: 字段值不合法（由调用方转换）。
    """
    unknown = sorted(set(payload) - set(REPORT_TOP_LEVEL_FIELDS))
    missing = sorted(set(REPORT_TOP_LEVEL_FIELDS) - set(payload))
    if unknown or missing:
        msg = f"报告顶层字段不符合本版本的格式（多出：{unknown or '无'}；缺少：{missing or '无'}）"
        raise ReportFormatError(msg)

    manifest = payload["manifest"]
    if isinstance(manifest, dict) and "code" not in manifest:
        # 🔴 canonical 的清单是**扁平的稳定身份子集**，刻意去掉了
        # ``working_tree_clean``（那是"能不能当基线"的判据，不是"结果是什么"）。
        # 拿它来比较，会让"工作树是否干净"这条校验永远无从执行——
        # 那种"校验通过"是假的，所以这里必须**明确拒绝**。
        msg = (
            "清单是扁平身份子集（没有 code 分组）——这看起来是 **canonical** 结果。"
            "对比需要**原始**结果：canonical 刻意去掉了 working_tree_clean，"
            "而它是「这份结果能不能当基线」的判据"
        )
        raise ReportFormatError(msg)

    summary = payload["summary"]
    if not isinstance(summary, dict):
        msg = f"summary 必须是对象，实际是 {type(summary).__name__}"
        raise ReportFormatError(msg)
    unknown_summary = sorted(set(summary) - set(REPORT_SUMMARY_FIELDS))
    missing_summary = sorted(set(REPORT_SUMMARY_FIELDS) - set(summary))
    if unknown_summary or missing_summary:
        msg = (
            f"summary 字段不符合本版本的格式"
            f"（多出：{unknown_summary or '无'}；缺少：{missing_summary or '无'}）"
        )
        raise ReportFormatError(msg)

    return RunResult.model_validate(
        {
            "schema_version": payload["schema_version"],
            "case_type": payload["case_type"],
            "provider": payload["provider"],
            "execution_mode": payload["execution_mode"],
            "storage_isolation": payload["storage_isolation"],
            "manifest": manifest,
            "metrics": payload["metrics"],
            "cases": payload["cases"],
            "total": summary["total"],
            "passed": summary["passed"],
            "failed": summary["failed"],
            "passed_overall": summary["passed_overall"],
        }
    )


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
