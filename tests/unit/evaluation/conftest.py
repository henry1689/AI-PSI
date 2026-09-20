"""evaluation 测试的公共夹具。

🔴 **这里的夹具只做两件事**：把一条案例写成临时 YAML、把数据集目录搭出来。
它们**不**模拟加载器或断言判定——被测的就是那些东西。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

import pytest
import yaml

from ai_psi.evaluation.assertions import AssertionResult, CaseObservation
from ai_psi.evaluation.loader import GoldenDataset
from ai_psi.evaluation.manifest import build_manifest, storage_identity
from ai_psi.evaluation.metrics import compute_metrics
from ai_psi.evaluation.models import GoldenCase
from ai_psi.evaluation.runner import (
    CaseResult,
    ExecutionMode,
    RunResult,
)


@pytest.fixture
def case_dict() -> dict[str, Any]:
    """一条**合法**案例的最小字典。

    测试按需改其中的键来构造反例；改一个键就得到一个反例，
    比每条测试各写一份完整 YAML 更不容易抄漏。
    """
    return {
        "schema_version": 1,
        "case_type": "cognitive_behavior",
        "case_id": "fixture-001",
        "category": "simple_fact",
        "intent": "夹具案例：只用于基础设施测试",
        "stimulus": {
            "input": "这是一个用于测试的合成输入。",
            "user_context": {},
            "requested_depth": None,
        },
        # 默认带上一条 forbidden：数据集级规则要求"每个类别至少有一条
        # 非快乐路径案例"，只有 required 的单条数据集会被那条规则拒绝。
        "expectations": {
            "required": [
                {"assertion": "final_state", "expected": "completed"},
            ],
            "forbidden": [
                {"assertion": "analysis_module_ran", "expected": "philosophical"},
            ],
        },
    }


@pytest.fixture
def write_dataset(tmp_path: Path) -> Callable[..., Path]:
    """把一条案例写进临时数据集，返回数据集根目录。

    第二次调用会**再写一条**（用 ``case_id`` 命名文件），
    因此同一个 ``dataset_root`` 可以容纳多条案例。
    """

    def _write(case: dict[str, Any], *, subdir: str | None = None) -> Path:
        root = tmp_path / "datasets"
        target = root if subdir is None else root / subdir
        target.mkdir(parents=True, exist_ok=True)
        path = target / f"{case['case_id']}.yaml"
        path.write_text(
            yaml.safe_dump(case, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        return root

    return _write


@pytest.fixture
def write_raw_file(tmp_path: Path) -> Callable[[str, str], Path]:
    """往临时数据集里写一个**原始文本**文件（用于 YAML 语法错误等用例）。"""

    def _write(text: str, name: str = "broken.yaml") -> Path:
        root = tmp_path / "datasets"
        root.mkdir(parents=True, exist_ok=True)
        path = root / name
        path.write_text(text, encoding="utf-8")
        return root

    return _write


# ---------------------------------------------------------------------------
# 阶段 7 · S5：对比夹具
# ---------------------------------------------------------------------------

#: 两个**形状合法**但内容不同的完整 SHA。
#:
#: ⚠️ 用 ``a``×40 / ``b``×40 而不是真实提交号：测试里的"不同版本"只需要是
#: **可区分的完整 SHA**，写一个真实提交号反而会让人以为它指的是某段历史。
SHA_A: Final[str] = "a" * 40
SHA_B: Final[str] = "b" * 40

#: 合成运行用的固定 Provider 身份。
_PROVIDER_NAME: Final[str] = "mock"
_MODEL_ID: Final[str] = "mock-model-v1"

#: 一个案例在合成结果里的处置。
#:
#: * ``pass`` —— 跑起来了，断言全过；
#: * ``fail`` —— 跑起来了，required 断言对不上（**观测到了**）；
#: * ``unobservable`` —— 跑起来了，但 required 断言读不到值；
#: * ``error`` —— 压根没跑起来（没有观测）。
Outcome = Literal["pass", "fail", "unobservable", "error"]


def _observation(*, state: str, stop_reason: str | None) -> CaseObservation:
    return CaseObservation(
        state=state,
        depth="d0",
        stop_reason_present=stop_reason is not None,
        stop_reason=stop_reason,
        response_present=True,
        response_text="合成的回答文本（不进 canonical，也不进对比产物）",
        judgment_present=False,
        model_calls_used=1,
        metacognitive_loops=0,
    )


def _assertions(outcome: Outcome) -> tuple[AssertionResult, ...]:
    """每案例固定两条断言：一条 required、一条 forbidden。

    这与夹具案例的 ``expectations`` 一一对应——合成结果必须与案例契约
    说得通，否则造出来的就是一份"自洽但假"的输入。
    """
    if outcome == "pass":
        return (
            AssertionResult(
                name="final_state",
                mode="required",
                expected="completed",
                observed="completed",
                passed=True,
                observation_status="observed",
                detail="合成：终态符合期望",
            ),
            AssertionResult(
                name="analysis_module_ran",
                mode="forbidden",
                expected="philosophical",
                observed=False,
                passed=True,
                observation_status="observed",
                detail="合成：未执行不该执行的模块",
            ),
        )
    if outcome == "fail":
        return (
            AssertionResult(
                name="final_state",
                mode="required",
                expected="completed",
                observed="failed",
                passed=False,
                observation_status="observed",
                detail="合成：终态是 failed",
            ),
            AssertionResult(
                name="analysis_module_ran",
                mode="forbidden",
                expected="philosophical",
                observed=False,
                passed=True,
                observation_status="observed",
                detail="合成：未执行不该执行的模块",
            ),
        )
    # ``unobservable`` 与 ``error`` 的断言都取不到观测值，区别在案例有没有观测。
    return tuple(
        AssertionResult(
            name=name,
            mode=mode,
            expected="completed" if mode == "required" else "philosophical",
            observed=None,
            passed=False,
            observation_status="unobservable",
            detail="合成：[不可观测]",
        )
        for name, mode in (("final_state", "required"), ("analysis_module_ran", "forbidden"))
    )


@dataclass(frozen=True, slots=True)
class ComparisonFactory:
    """构造**合法且自洽**的对比输入。

    🔴 合成夹具的纪律（任务书 §二十四 B）：

    * 走**正式模型**构造，不手工拼 JSON 字符串；
    * 指标由 :func:`~ai_psi.evaluation.metrics.compute_metrics` **重新算**
      出来，不手填——否则测的就成了"两份手写的数字相不相等"，与本模块无关；
    * 不提交、不冒充历史评测：``commit_sha`` 用的是 ``a``×40，一眼看得出是合成值。
    """

    def dataset(self, case_ids: Sequence[str], *, category: str = "simple_fact") -> GoldenDataset:
        """构造一个数据集（每个 ``case_id`` 一条案例）。"""
        cases = tuple(
            GoldenCase.model_validate(
                {
                    "schema_version": 1,
                    "case_type": "cognitive_behavior",
                    "case_id": case_id,
                    "category": category,
                    "intent": "合成夹具案例",
                    "stimulus": {"input": f"合成输入 {case_id}"},
                    "expectations": {
                        "required": [{"assertion": "final_state", "expected": "completed"}],
                        "forbidden": [
                            {"assertion": "analysis_module_ran", "expected": "philosophical"}
                        ],
                    },
                }
            )
            for case_id in case_ids
        )
        return GoldenDataset(root=Path("synthetic"), cases=cases)

    def run(
        self,
        dataset: GoldenDataset,
        outcomes: Mapping[str, Outcome],
        *,
        commit_sha: str = SHA_A,
    ) -> RunResult:
        """构造一份运行结果。

        Args:
            dataset: 数据集（决定 ``total_cases`` 与类别）。
            outcomes: ``case_id`` → 处置。**必须覆盖数据集里的每一条案例**。
            commit_sha: 合成提交号。

        Returns:
            自洽的运行结果：案例、清单、指标三者互相说得通。
        """
        missing = sorted({case.case_id for case in dataset.cases} - set(outcomes))
        if missing:
            msg = f"outcomes 缺少这些案例：{missing}"
            raise AssertionError(msg)

        cases: list[CaseResult] = []
        for case in dataset.cases:
            outcome = outcomes[case.case_id]
            cases.append(
                CaseResult(
                    case_id=case.case_id,
                    category=case.category.value,
                    observation=(
                        None
                        if outcome == "error"
                        else _observation(
                            state="completed" if outcome != "fail" else "failed",
                            stop_reason="DIRECT_ANSWER",
                        )
                    ),
                    assertions=_assertions(outcome),
                    passed=outcome == "pass",
                    failure_kind=(
                        None
                        if outcome == "pass"
                        else ("execution_error" if outcome == "error" else "assertion_failure")
                    ),
                    failure_reason=None if outcome == "pass" else "合成失败",
                )
            )

        passed = sum(1 for case in cases if case.passed)
        manifest = build_manifest(
            cases=dataset.cases,
            execution_mode=ExecutionMode.IN_MEMORY.value,
            prompt_versions={"reflector": "1.0.0"},
            provider_name=_PROVIDER_NAME,
            model_id=_MODEL_ID,
            provider_parameters={"json_mode": True, "max_retries": 2},
            storage=storage_identity(backend="memory", alembic_revision=None),
        )
        # 🔴 覆盖 Git 身份：真实仓库在跑测试时**工作树是脏的**（代码正在被改），
        # 而"能不能比较"不该取决于开发者此刻有没有提交。合成值写死为
        # "干净 + 一个完整 SHA"，让每一条用例都站在同一个起点上。
        manifest = manifest.model_copy(
            update={
                "code": manifest.code.model_copy(
                    update={"commit_sha": commit_sha, "working_tree_clean": True}
                )
            }
        )

        result = RunResult(
            case_type="cognitive_behavior",
            provider=_PROVIDER_NAME,
            total=len(cases),
            passed=passed,
            failed=len(cases) - passed,
            passed_overall=passed == len(cases),
            cases=tuple(cases),
            execution_mode=ExecutionMode.IN_MEMORY.value,
            manifest=manifest,
        )
        # 🔴 指标**由结果重算**，不手填。
        return result.model_copy(update={"metrics": compute_metrics(dataset, result)})

    def revise(self, result: RunResult, dataset: GoldenDataset, **changes: object) -> RunResult:
        """改结果里的字段并**重算指标**，保持输入自洽。

        🔴 用 ``model_copy`` 改完案例却留着旧的 ``metrics``，造出来的是一份
        **内部矛盾**的输入——那种输入会被完整性校验挡下，于是测试"通过"了，
        但通过的理由与它想验证的东西无关。改案例就必须重算指标。
        """
        revised = result.model_copy(update={**changes, "metrics": None})
        return revised.model_copy(update={"metrics": compute_metrics(dataset, revised)})

    def patch(self, result: RunResult, section: str, **changes: object) -> RunResult:
        """改清单里的某一分组（用于构造身份差异）。

        ⚠️ 只动清单，**不动案例**——于是"身份不同、结果相同"这种情形
        可以被精确构造出来，而那正是 eligibility 判定要处理的东西。
        """
        manifest = result.manifest
        if manifest is None:  # pragma: no cover - 夹具总是带清单
            msg = "夹具结果必须带清单"
            raise AssertionError(msg)
        updated = getattr(manifest, section).model_copy(update=changes)
        return result.model_copy(
            update={"manifest": manifest.model_copy(update={section: updated})}
        )

    def set_execution_mode(self, result: RunResult, mode: str) -> RunResult:
        """同时改**结果**与**清单**里的执行模式（保持内部自洽）。"""
        patched = self.patch(result, "evaluation", execution_mode=mode)
        return patched.model_copy(update={"execution_mode": mode})


@pytest.fixture
def comparison_factory() -> ComparisonFactory:
    """S5 对比夹具。"""
    return ComparisonFactory()
