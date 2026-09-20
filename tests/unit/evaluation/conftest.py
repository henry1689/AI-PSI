"""evaluation 测试的公共夹具。

🔴 **这里的夹具只做两件事**：把一条案例写成临时 YAML、把数据集目录搭出来。
它们**不**模拟加载器或断言判定——被测的就是那些东西。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal, cast

import pytest
import yaml

from ai_psi.evaluation.assertions import AssertionResult, CaseObservation
from ai_psi.evaluation.comparison import (
    EvaluationComparison,
    compare_run_results,
    write_comparison,
)
from ai_psi.evaluation.evidence import EvidenceInputs, bundle_digest
from ai_psi.evaluation.gate import (
    GatePolicy,
    decide,
    load_gate_policy,
    policy_digest,
    write_decision,
)
from ai_psi.evaluation.loader import GoldenDataset
from ai_psi.evaluation.manifest import build_manifest, storage_identity
from ai_psi.evaluation.metrics import compute_metrics
from ai_psi.evaluation.models import GoldenCase
from ai_psi.evaluation.runner import (
    CaseResult,
    ExecutionMode,
    RunResult,
)
from ai_psi.evaluation.serialization import dumps, write_reports


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


# ---------------------------------------------------------------------------
# 阶段 7 · S6：门禁夹具
# ---------------------------------------------------------------------------


def _rule_examples() -> list[dict[str, object]]:
    """四种规则类型各一条，够测聚合与逐类判定。

    ⚠️ 它与 ``evals/policies/s6_mock_golden_v1.json`` **不是同一份**：
    那份是真实契约（测试里直接加载它），这份只是让单元测试不必重复
    14 条规则的样板。
    """
    return [
        {
            "rule_id": "no_case_regressions",
            "rule_type": "transition_count_equals",
            "description": "no case may regress",
            "evidence_key": "case.regression_transition_count",
            "operator": "EQ",
            "expected": 0,
        },
        {
            "rule_id": "no_newly_failed_cases",
            "rule_type": "id_set_empty",
            "description": "no newly failed cases",
            "evidence_key": "failure.newly_failed_case_ids",
            "operator": "EMPTY",
        },
        {
            "rule_id": "candidate_case_pass_rate_full",
            "rule_type": "ratio_equals",
            "description": "candidate passes every executed case",
            "evidence_key": "candidate.case_pass_rate",
            "operator": "EQ",
            "expected": {
                "value": "1.000000",
                "numerator_equals_denominator": True,
                "denominator_gt_zero": True,
                "denominator_ref": "dataset_case_count",
            },
        },
        {
            "rule_id": "candidate_failed_assertions_zero",
            "rule_type": "count_equals",
            "description": "no failed assertions",
            "evidence_key": "candidate.failed_assertions",
            "operator": "EQ",
            "expected": 0,
        },
    ]


@dataclass(frozen=True, slots=True)
class GateFactory:
    """构造与某份对比**匹配**的策略。

    🔴 scope 的值全部取自对比里**已验证的双方身份**，一个都不硬编码——
    否则夹具会与真实契约漂移，测试就变成了在测自己写的那串常量。
    """

    def policy_payload(
        self, comparison: EvaluationComparison, **overrides: object
    ) -> dict[str, object]:
        """构造策略 payload（``policy_digest`` 按内容算出）。

        Args:
            comparison: 用来对齐作用域的对比产物。
            **overrides: 覆盖顶层字段（如 ``rules``、``policy_id``）。
                要改 scope 就传 ``scope={...}``，**整块替换**。
        """
        side = comparison.baseline
        metrics = comparison.metrics_comparison
        assertion_count = 0 if metrics is None else metrics.assertions_overall.total.candidate
        scope: dict[str, object] = {
            "dataset_digest": side.dataset_digest,
            "dataset_case_count": side.dataset_case_count,
            "expected_assertion_count": max(assertion_count, 1),
            "assertion_registry_digest": side.assertion_registry_digest,
            "prompt_digest": side.prompt_versions_digest,
            "provider": side.provider_name,
            "model": side.model_id,
            "provider_configuration_digest": side.provider_configuration_digest,
            "execution_mode": side.result_execution_mode,
            "storage_backend": side.storage_backend,
            "migration_revision": side.alembic_revision,
            "metrics_schema_version": side.metrics_schema_version,
            "metrics_definition_digest": side.metrics_definition_digest,
        }
        contract: dict[str, object] = {
            "comparison_schema_version": comparison.comparison_schema_version,
            "comparison_definition_digest": comparison.comparison_definition_digest,
        }
        payload: dict[str, object] = {
            "policy_schema_version": 1,
            "policy_id": "test_gate",
            "policy_revision": 1,
            "display_name": "Test Gate",
            "purpose": "Unit-test policy for the structured gate",
            "scope": scope,
            "supported_comparison_contract": contract,
            "rules": _rule_examples(),
        }
        payload.update(overrides)
        payload["policy_digest"] = policy_digest(payload)
        return payload

    def policy(self, comparison: EvaluationComparison, **overrides: object) -> GatePolicy:
        """构造并校验一份策略（用于进程内测试）。"""
        return GatePolicy.model_validate(self.policy_payload(comparison, **overrides))

    def comparison(self, outcomes: Mapping[str, Outcome]) -> EvaluationComparison:
        """由一组案例处置造出一份**可比较**的对比（两侧同一份结果）。"""
        runs = ComparisonFactory()
        dataset = runs.dataset(sorted(outcomes))
        result = runs.run(dataset, outcomes)
        return compare_run_results(result, result)

    def comparison_between(
        self,
        baseline_outcomes: Mapping[str, Outcome],
        candidate_outcomes: Mapping[str, Outcome],
    ) -> EvaluationComparison:
        """造一份**两侧不同**的对比（同一个数据集、两个不同提交）。

        ⚠️ 两份结果必须共用同一个数据集，否则 ``dataset_digest`` 不同、
        S5 会直接判不可比较，测的就不是门禁了。
        """
        runs = ComparisonFactory()
        dataset = runs.dataset(sorted(baseline_outcomes))
        baseline = runs.run(dataset, baseline_outcomes, commit_sha=SHA_A)
        candidate = runs.run(dataset, candidate_outcomes, commit_sha=SHA_B)
        return compare_run_results(baseline, candidate)


@pytest.fixture
def gate_factory() -> GateFactory:
    """S6 门禁夹具。"""
    return GateFactory()


# ---------------------------------------------------------------------------
# 阶段 7 · S7：证据链夹具
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceChainPaths:
    """一条**已经落在磁盘上**的五文件证据链。"""

    baseline_run: Path
    candidate_run: Path
    comparison: Path
    policy: Path
    gate_decision: Path

    def inputs(self) -> EvidenceInputs:
        """交给 S7 的五个角色显式输入。"""
        return EvidenceInputs(
            baseline_run=self.baseline_run,
            candidate_run=self.candidate_run,
            comparison=self.comparison,
            policy=self.policy,
            gate_decision=self.gate_decision,
        )

    def as_map(self) -> dict[str, Path]:
        """角色名 → 路径，供"改一个字节"这类专项使用。"""
        return {
            "BASELINE_RUN": self.baseline_run,
            "CANDIDATE_RUN": self.candidate_run,
            "COMPARISON": self.comparison,
            "POLICY": self.policy,
            "GATE_DECISION": self.gate_decision,
        }

    def refresh_descriptors(self, bundle_path: Path) -> None:
        """把证据包里五个描述符的摘要与长度**按当前文件重算**，再重算
        ``bundle_digest``。

        🔴 用途是造出"**内容摘要全对、但在别处有问题**"的场景（例如契约
        版本不受支持）。不这样做，任何一个"文件被改过"的用例都会先撞在
        内容摘要上，测到的就不是它想测的那一件事了。
        """
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        for descriptor, (_, path) in zip(
            payload["artifacts"], self.inputs().by_role(), strict=True
        ):
            data = path.read_bytes()
            descriptor["content_sha256"] = f"sha256:{hashlib.sha256(data).hexdigest()}"
            descriptor["byte_length"] = len(data)
        payload["bundle_digest"] = bundle_digest(payload)
        bundle_path.write_text(dumps(payload), encoding="utf-8", newline="\n")


@dataclass(frozen=True, slots=True)
class EvidenceChainFactory:
    """把一条**自洽**的五文件证据链写到磁盘上。

    🔴 纪律：五份产物全部由**正式模型**构造、由**正式写出函数**落盘，
    不手拼 JSON 字符串——否则测的就成了"两份手写的 JSON 相不相等"，
    与 S7 一点关系都没有。

    🔴 策略与门禁结论是**真的算出来的**：策略先落盘再用
    :func:`~ai_psi.evaluation.gate.load_gate_policy` 读回来（顺带证明
    那份文件是一份合法策略），结论由 :func:`~ai_psi.evaluation.gate.decide`
    产出。这样"重算一致"才有东西可测。
    """

    def write(
        self,
        directory: Path,
        *,
        baseline_outcomes: Mapping[str, Outcome] | None = None,
        candidate_outcomes: Mapping[str, Outcome] | None = None,
        same_commit: bool = False,
        policy_scope: Mapping[str, object] | None = None,
    ) -> EvidenceChainPaths:
        """构造并落盘一条证据链。

        Args:
            directory: 输出目录。
            baseline_outcomes: 基线一侧的案例处置。
            candidate_outcomes: 候选一侧的案例处置；不给则与基线相同
                （自比较，门禁判 ``PASS``）。
            same_commit: 两侧是否使用**同一个**提交号。默认为否——用两个
                可区分的合成 SHA，让"角色交换"真的能被测出来。
            policy_scope: 覆盖策略 scope 的若干项（并重算 ``policy_digest``）。
                用来造出"策略不适用 ⇒ 门禁 ``NOT_EVALUATED``"但仍**自洽**
                的链——结论照旧由 ``decide`` 算出来，不是手填的。

        Returns:
            五个文件的路径。
        """
        runs = ComparisonFactory()
        gate = GateFactory()
        baseline_cases: Mapping[str, Outcome] = baseline_outcomes or {
            "case-001": "pass",
            "case-002": "pass",
        }
        candidate_cases: Mapping[str, Outcome] = candidate_outcomes or dict(baseline_cases)

        dataset = runs.dataset(sorted(baseline_cases))
        baseline = runs.run(dataset, baseline_cases, commit_sha=SHA_A)
        candidate = runs.run(dataset, candidate_cases, commit_sha=SHA_A if same_commit else SHA_B)
        comparison = compare_run_results(baseline, candidate)

        directory.mkdir(parents=True, exist_ok=True)
        baseline_path = directory / "baseline-run.json"
        candidate_path = directory / "candidate-run.json"
        write_reports(
            baseline,
            raw_path=baseline_path,
            canonical_path=directory / "baseline-canonical.json",
        )
        write_reports(
            candidate,
            raw_path=candidate_path,
            canonical_path=directory / "candidate-canonical.json",
        )

        comparison_path = directory / "comparison.json"
        write_comparison(comparison, comparison_path)

        policy_payload = gate.policy_payload(comparison)
        if policy_scope is not None:
            scope = dict(cast("dict[str, object]", policy_payload["scope"]))
            scope.update(policy_scope)
            policy_payload["scope"] = scope
            policy_payload["policy_digest"] = policy_digest(policy_payload)

        policy_path = directory / "policy.json"
        policy_path.write_text(
            json.dumps(policy_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

        decision_path = directory / "gate-decision.json"
        write_decision(decide(comparison, load_gate_policy(policy_path)), decision_path)

        return EvidenceChainPaths(
            baseline_run=baseline_path,
            candidate_run=candidate_path,
            comparison=comparison_path,
            policy=policy_path,
            gate_decision=decision_path,
        )


@pytest.fixture
def evidence_chain_factory() -> EvidenceChainFactory:
    """S7 证据链夹具。"""
    return EvidenceChainFactory()
