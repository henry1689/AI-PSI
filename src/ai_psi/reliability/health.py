"""认知健康度（任务书 §12.5、§13.2）。

把散落在各处的健康信号汇总成一份**可查询的报告**。

🔴 **"进程活着"与"认知系统健康"是两件事。**

一台机器可以网络全通、数据库连得上、进程响应正常，同时
宪法被改坏了、Prompt 契约缺了一份、改进提案门槛被调到了 1。
就绪探针会全部报绿——而它绿得毫无意义，因为**这些才是真正要守的东西**。

因此本模块的检查对象是**认知资产**：

* 宪法指纹；
* Prompt 契约完整性；
* 预算与反刍阈值配置；
* 向量 Provider 的标识与维度；
* **不变量的结构性保证**（见 :mod:`ai_psi.reliability.invariants`）——
  这一项是阶段 6 加的，它回答"类型级的硬约束现在还在不在"。

⚠️ 本模块**不接收容器**。它只接受具体的值，由调用方（路由）负责取值。
让可靠性层认识组合根，会让它无法在没有完整运行时的场合被复用——
而"启动时做一次自检"恰恰就是这种场合。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from ai_psi.reliability.invariants import RUNTIME_CHECKED_INVARIANTS, failing_checks

__all__ = [
    "HealthDimension",
    "HealthReport",
    "budget_dimension",
    "constitution_dimension",
    "embedding_dimension",
    "invariant_dimension",
    "prompt_contract_dimension",
    "provider_dimension",
    "report",
]


@dataclass(frozen=True, slots=True)
class HealthDimension:
    """一个健康维度。

    Attributes:
        name: 维度名。
        ok: 是否健康。
        detail: 说明。**必须给出具体值**（"3 个任务契约"而不是"正常"）——
            只说"正常"的检查在出问题时提供不了任何信息。
    """

    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class HealthReport:
    """一次健康检查的完整结果。

    Attributes:
        status: ``ok`` 或 ``degraded``（任务书 §13.2 的降级状态）。
        dimensions: 各维度结果。
        unchecked_invariants: **未做运行期检查**的不变量编号。
            🔴 它们是显式列出的，而不是被省略——
            "报告里没有这一项"与"这一项没检查"必须能区分开。
    """

    status: str
    dimensions: tuple[HealthDimension, ...] = field(default=())
    unchecked_invariants: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        """是否全部健康。"""
        return all(item.ok for item in self.dimensions)

    def dimension(self, name: str) -> HealthDimension | None:
        """按名字取一个维度。"""
        for item in self.dimensions:
            if item.name == name:
                return item
        return None


#: 状态值常量。``degraded`` 是**系统健康状态**，不是回合状态（ADR-0012）。
STATUS_OK: Final[str] = "ok"
STATUS_DEGRADED: Final[str] = "degraded"


def report(
    dimensions: Sequence[HealthDimension], *, all_invariant_ids: Sequence[str]
) -> HealthReport:
    """汇总各维度。

    Args:
        dimensions: 各健康维度。
        all_invariant_ids: 宪法里登记的全部不变量编号。

    Returns:
        汇总报告。**任一维度不健康即整体 ``degraded``**。
    """
    unchecked = tuple(sorted(set(all_invariant_ids) - set(RUNTIME_CHECKED_INVARIANTS)))
    return HealthReport(
        status=STATUS_OK if all(item.ok for item in dimensions) else STATUS_DEGRADED,
        dimensions=tuple(dimensions),
        unchecked_invariants=unchecked,
    )


# ---------------------------------------------------------------------------
# 各维度
# ---------------------------------------------------------------------------


def constitution_dimension(*, fingerprint: str) -> HealthDimension:
    """宪法维度：指纹存在即认为宪法可加载。

    指纹本身写进 ``detail``——**它变了意味着宪法被改过**，
    而"这次运行的宪法是不是我上次看到的那一份"是排查时第一个要问的问题。
    """
    return HealthDimension(
        name="constitution",
        ok=bool(fingerprint),
        detail=f"fingerprint={fingerprint}（修改宪法会改变它）",
    )


def prompt_contract_dimension(*, task_names: Sequence[str]) -> HealthDimension:
    """Prompt 契约维度。"""
    count = len(task_names)
    return HealthDimension(
        name="prompt_contracts",
        ok=count > 0,
        detail=f"{count} 个任务契约已注册：{'、'.join(sorted(task_names))}",
    )


def budget_dimension(
    *,
    repetition_threshold: float,
    model_call_ceilings: Sequence[int] = (),
) -> HealthDimension:
    """预算与反刍阈值维度。

    🔴 检查的是**配置是否自相矛盾**，不是"配置是不是某个特定值"：
    * 反刍阈值必须在 ``(0, 1]`` 内——0 会让每一轮都被判为重复；
    * 每档深度的调用上限必须为正——0 意味着什么分析都跑不了。

    Args:
        repetition_threshold: 反刍相似度阈值。
        model_call_ceilings: 各深度的模型调用上限。

    Returns:
        维度结果。
    """
    problems: list[str] = []
    if not 0.0 < repetition_threshold <= 1.0:
        problems.append(f"repetition_threshold={repetition_threshold} 不在 (0, 1] 内")
    if model_call_ceilings and min(model_call_ceilings) <= 0:
        problems.append(f"存在非正的调用上限：{sorted(model_call_ceilings)}")

    detail = f"repetition_threshold={repetition_threshold}"
    if model_call_ceilings:
        detail += f"；调用上限={sorted(model_call_ceilings)}"
    if problems:
        return HealthDimension(name="budget", ok=False, detail=f"{detail}；{'；'.join(problems)}")
    return HealthDimension(name="budget", ok=True, detail=detail)


def embedding_dimension(*, provider: str, version: str, dimension: int) -> HealthDimension:
    """向量 Provider 维度。

    🔴 **版本与维度都要报出来。**

    它们决定了"库里已有的向量还能不能用于检索"：维度是数据库列的固定属性，
    版本决定哪些向量参与比对。换 Provider 后旧记忆**会静默地全部检索不到**——
    而在这份报告里，至少能看到当前用的是哪个向量空间（risks.md R43）。
    """
    return HealthDimension(
        name="embedding",
        ok=dimension > 0 and bool(version),
        detail=f"provider={provider}；version={version}；dimension={dimension}",
    )


def provider_dimension(*, status: str, detail: str) -> HealthDimension:
    """LLM Provider 维度。

    🔴 熔断打开时状态是 ``degraded``（任务书 §13.2）。
    """
    return HealthDimension(name="llm_provider", ok=status == STATUS_OK, detail=detail)


def invariant_dimension() -> HealthDimension:
    """不变量的结构性保证维度（阶段 6）。

    🔴 这一项查的是**地基还在不在**：类型级的硬约束
    （提案不可能生效、假设不可能变成事实、门槛不可能低于 2）
    是否仍然成立。它不查"某次运行的行为是否正确"——那是测试的领域。

    Returns:
        维度结果。
    """
    failures = failing_checks()
    if failures:
        detail = "；".join(f"{item.invariant_id}：{item.detail}" for item in failures)
        return HealthDimension(name="invariants", ok=False, detail=detail)
    checked = "、".join(sorted(RUNTIME_CHECKED_INVARIANTS))
    return HealthDimension(
        name="invariants",
        ok=True,
        detail=f"结构性保证完好（运行期检查覆盖 {checked}）",
    )
