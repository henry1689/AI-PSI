"""对比输入的严格加载与内部自洽性（阶段 7 · S5）。

对应任务书 §二十六 的 B、C 两组。

🔴 这一组的存在理由：**被检验的数据不得给自己作证**。
文件里那份 ``metrics`` 只是一份"声称"，加载器必须把每一个能由
结构化案例结果重算的计数**重新数一遍**再比。数字对不上时，
整份输入都不可信——而不是"以文件里写的为准"。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ai_psi.evaluation.comparison import (
    BLOCKER_ASSERTION_IDENTITY_AMBIGUOUS,
    BLOCKER_CASE_SET_INCONSISTENT,
    BLOCKER_DIRTY_WORKTREE,
    BLOCKER_DUPLICATE_CASE_ID,
    BLOCKER_IDENTITY_UNAVAILABLE,
    BLOCKER_INPUT_METRICS_MISMATCH,
    ComparisonInputError,
    compare_run_results,
    evaluate_comparison_eligibility,
    load_run_result,
)
from ai_psi.evaluation.metrics import RatioMetric
from ai_psi.evaluation.serialization import (
    REPORT_SUMMARY_FIELDS,
    REPORT_TOP_LEVEL_FIELDS,
    ReportFormatError,
    canonical_payload,
    parse_report,
    raw_payload,
    write_reports,
)

pytestmark = pytest.mark.unit

Payload = dict[str, Any]
Tamper = Callable[[Payload], None]


def _write(result: Any, tmp_path: Path, name: str = "run.json") -> Path:
    path = tmp_path / name
    write_reports(result, raw_path=path, canonical_path=tmp_path / f"{name}.canonical")
    return path


def _tamper(path: Path, mutate: Tamper) -> Path:
    """读回合法报告，改一处，再写回去。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


class TestReportFormat:
    """报告形状的常量与读写两侧必须一致。"""

    def test_top_level_fields_match_what_is_written(self, comparison_factory: Any) -> None:
        """🔴 常量与写入实现**不得漂移**。

        如果 ``raw_payload`` 新增了一个字段而常量没跟上，
        加载器就会把它当成"来路不明的字段"拒掉——而且只在读的时候才发现。
        """
        dataset = comparison_factory.dataset(["case-001"])
        result = comparison_factory.run(dataset, {"case-001": "pass"})
        assert set(raw_payload(result)) == set(REPORT_TOP_LEVEL_FIELDS)
        assert set(canonical_payload(result)) == set(REPORT_TOP_LEVEL_FIELDS)
        assert set(raw_payload(result)["summary"]) == set(REPORT_SUMMARY_FIELDS)

    def test_summary_is_hoisted_into_flat_run_result(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        result = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "fail"})
        restored = parse_report(raw_payload(result))
        assert restored.total == result.total
        assert restored.passed == result.passed
        assert restored.failed == result.failed
        assert restored.passed_overall == result.passed_overall
        assert restored == result

    def test_unknown_top_level_field_is_rejected(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        result = comparison_factory.run(dataset, {"case-001": "pass"})
        payload = raw_payload(result)
        payload["extra"] = 1
        with pytest.raises(ReportFormatError, match="顶层字段"):
            parse_report(payload)

    def test_missing_top_level_field_is_rejected(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        result = comparison_factory.run(dataset, {"case-001": "pass"})
        payload = raw_payload(result)
        del payload["metrics"]
        with pytest.raises(ReportFormatError, match="顶层字段"):
            parse_report(payload)

    def test_unknown_summary_field_is_rejected(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        result = comparison_factory.run(dataset, {"case-001": "pass"})
        payload = raw_payload(result)
        payload["summary"]["skipped"] = 0
        with pytest.raises(ReportFormatError, match="summary"):
            parse_report(payload)

    def test_canonical_manifest_is_refused(self, comparison_factory: Any) -> None:
        """🔴 canonical 的清单是**扁平子集**，没有 ``working_tree_clean``。

        拿它来做对比，会让"工作树是否干净"这条校验永远无从执行——
        那种"校验通过"是假的，所以必须在读的时候就说清楚。
        """
        dataset = comparison_factory.dataset(["case-001"])
        result = comparison_factory.run(dataset, {"case-001": "pass"})
        payload = canonical_payload(result)
        assert "code" not in payload["manifest"]
        with pytest.raises(ReportFormatError, match="canonical"):
            parse_report(payload)


class TestStrictLoading:
    """B 组：输入加载。"""

    def test_valid_result_loads(self, comparison_factory: Any, tmp_path: Path) -> None:
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        result = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        path = _write(result, tmp_path)
        loaded = load_run_result(path)
        assert loaded == result
        assert list(loaded.cases[0].model_dump()) == list(result.cases[0].model_dump())

    def test_broken_json_is_rejected(self, comparison_factory: Any, tmp_path: Path) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        result = comparison_factory.run(dataset, {"case-001": "pass"})
        path = _write(result, tmp_path)
        text = path.read_text(encoding="utf-8")
        path.write_text(text[:-20], encoding="utf-8", newline="\n")
        with pytest.raises(ComparisonInputError, match="JSON 解析失败"):
            load_run_result(path)

    def test_non_utf8_is_rejected(self, comparison_factory: Any, tmp_path: Path) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        result = comparison_factory.run(dataset, {"case-001": "pass"})
        path = _write(result, tmp_path)
        path.write_bytes(b"\xff\xfe\x00\x00 not utf-8")
        with pytest.raises(ComparisonInputError, match="UTF-8"):
            load_run_result(path)

    def test_missing_file_reports_a_read_failure(self, tmp_path: Path) -> None:
        with pytest.raises(ComparisonInputError, match="读取失败"):
            load_run_result(tmp_path / "does-not-exist.json")

    @pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_numbers_are_rejected(self, tmp_path: Path, constant: str) -> None:
        """🔴 ``NaN`` 会静默毒化整份产物：``NaN != NaN``。

        Python 的 ``json`` 模块**默认接受**这三个常量（对标准的扩展），
        所以必须显式拦下。
        """
        path = tmp_path / f"{constant}.json"
        path.write_text('{"total": ' + constant + "}", encoding="utf-8", newline="\n")
        with pytest.raises(ComparisonInputError, match=constant):
            load_run_result(path)

    def test_top_level_array_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "array.json"
        path.write_text("[1, 2, 3]", encoding="utf-8", newline="\n")
        with pytest.raises(ComparisonInputError, match="顶层必须是对象"):
            load_run_result(path)

    def test_missing_manifest_is_rejected(self, comparison_factory: Any, tmp_path: Path) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        result = comparison_factory.run(dataset, {"case-001": "pass"})
        path = _write(result, tmp_path)
        _tamper(path, lambda payload: payload.__setitem__("manifest", None))
        with pytest.raises(ComparisonInputError, match="可复现性清单"):
            load_run_result(path)

    def test_missing_metrics_is_rejected(self, comparison_factory: Any, tmp_path: Path) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        result = comparison_factory.run(dataset, {"case-001": "pass"})
        path = _write(result, tmp_path)
        _tamper(path, lambda payload: payload.__setitem__("metrics", None))
        with pytest.raises(ComparisonInputError, match="指标层结果"):
            load_run_result(path)

    def test_unknown_case_field_is_rejected(self, comparison_factory: Any, tmp_path: Path) -> None:
        """``extra="forbid"`` 一路生效到案例层。"""
        dataset = comparison_factory.dataset(["case-001"])
        result = comparison_factory.run(dataset, {"case-001": "pass"})
        path = _write(result, tmp_path)
        _tamper(path, lambda payload: payload["cases"][0].__setitem__("score", 1.0))
        with pytest.raises(ComparisonInputError, match="结果结构校验失败"):
            load_run_result(path)

    def test_result_schema_version_is_checked(
        self, comparison_factory: Any, tmp_path: Path
    ) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        result = comparison_factory.run(dataset, {"case-001": "pass"})
        path = _write(result, tmp_path)
        _tamper(path, lambda payload: payload.__setitem__("schema_version", 99))
        with pytest.raises(ComparisonInputError, match="结果版本"):
            load_run_result(path)

    def test_invalid_ratio_metric_is_rejected(
        self, comparison_factory: Any, tmp_path: Path
    ) -> None:
        """值与分子分母对不上的比率**读不进来**。

        一个"分子分母都对、值算错了"的结果，比一个明显越界的结果更难发现——
        所以校验器在构造时就把值重算一遍。
        """
        dataset = comparison_factory.dataset(["case-001"])
        result = comparison_factory.run(dataset, {"case-001": "pass"})
        path = _write(result, tmp_path)
        _tamper(
            path,
            lambda payload: payload["metrics"]["cases"]["case_pass_rate"].__setitem__(
                "value", "0.500000"
            ),
        )
        with pytest.raises(ComparisonInputError, match="结果结构校验失败"):
            load_run_result(path)

    def test_input_content_is_data_not_code(self, comparison_factory: Any, tmp_path: Path) -> None:
        """🔴 输入文件里的内容**只是数据**。

        加载器不 ``eval``、不 ``exec``、不导入任何东西——一段看起来像
        代码的字符串，读进来后仍然是一段字符串。
        """
        dataset = comparison_factory.dataset(["case-001"])
        result = comparison_factory.run(dataset, {"case-001": "pass"})
        path = _write(result, tmp_path)
        marker = "__import__('os').system('echo pwned')"
        _tamper(
            path, lambda payload: payload["manifest"]["provider"].__setitem__("model_id", marker)
        )
        loaded = load_run_result(path)
        assert loaded.manifest is not None
        assert loaded.manifest.provider.model_id == marker

    def test_loader_does_not_mutate_the_input_file(
        self, comparison_factory: Any, tmp_path: Path
    ) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        result = comparison_factory.run(dataset, {"case-001": "pass"})
        path = _write(result, tmp_path)
        before = path.read_bytes()
        load_run_result(path)
        assert path.read_bytes() == before


class TestInputIntegrity:
    """C 组：输入自洽性——重算的计数必须与文件里那份一致。"""

    def test_consistent_input_has_no_mismatch(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        result = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "fail"})
        comparison = compare_run_results(result, result)
        assert comparison.integrity_mismatches == ()
        assert comparison.comparison_eligible is True

    @pytest.mark.parametrize(
        ("mutate", "expected_path"),
        [
            # ⚠️ 每条改动都必须**保持文件自身自洽**：模型的聚合不变量会拦下
            # 不自洽的输入，而那属于"文件坏了"（加载期拒绝），
            # 与"读得进来、但与证据不符"是两回事。
            #
            # ---- A 类：案例结果被改，指标没跟着变 ----
            (lambda p: p["cases"][0].__setitem__("passed", False), "cases.passed_cases"),
            (
                lambda p: p["cases"][0].update({"failure_kind": "execution_error"}),
                "cases.execution_error_cases",
            ),
            (
                lambda p: p["cases"][0]["assertions"][0].__setitem__("passed", False),
                "assertions.overall.passed",
            ),
            (
                lambda p: p["cases"][0]["assertions"][0].update(
                    {"observation_status": "unobservable"}
                ),
                "assertions.overall.unobservable",
            ),
            (lambda p: p["cases"][0]["observation"].__setitem__("depth", "d2"), "distributions"),
            # ---- B 类：指标被改，案例没跟着变 ----
            # 这两处**不参与**模型的聚合校验，所以文件本身仍然自洽——
            # 正是"模型管不到、只能靠重算发现"的那部分。
            (
                lambda p: p["metrics"]["distributions"]["final_state"].__setitem__("completed", 1),
                "distributions",
            ),
            (
                lambda p: p["metrics"]["failures"].__setitem__("failed_case_ids", ["case-001"]),
                "failures",
            ),
            # ---- C 类：几处一起改才自洽的计数平移 ----
            # ⚠️ 要动 ``failed_cases`` 就必须同时安排"两种失败互斥且完备"，
            # 否则 ``CaseMetrics`` 自己的不变量会拦下它。S4 的模型校验比想象的密：
            # 三条互斥完备关系 + 明细汇总关系，手工造一份"自洽但错"的指标并不容易。
            (
                lambda p: (
                    p["metrics"]["cases"].update(
                        {
                            "passed_cases": 1,
                            "failed_cases": 1,
                            "execution_error_cases": 1,
                            "assertion_failed_cases": 0,
                        }
                    ),
                    p["metrics"]["categories"][0].update({"passed": 1, "failed": 1}),
                ),
                "cases.passed_cases",
            ),
        ],
    )
    def test_stored_metrics_disagreeing_with_recomputed_blocks(
        self, comparison_factory: Any, tmp_path: Path, mutate: Tamper, expected_path: str
    ) -> None:
        """🔴 文件说自己通过了 1 条，而结构化结果里有 2 条通过——
        **整份输入都不可信**，不是"以文件为准"。"""
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        result = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        path = _write(result, tmp_path)
        _tamper(path, mutate)
        loaded = load_run_result(path)
        comparison = compare_run_results(loaded, loaded)
        assert comparison.comparison_eligible is False
        assert BLOCKER_INPUT_METRICS_MISMATCH in comparison.blockers
        assert any(item.startswith(expected_path) for item in comparison.integrity_mismatches)

    def test_ratio_numerator_and_denominator_are_recomputed(self, comparison_factory: Any) -> None:
        """🔴 比率的**分子分母**也要被复核，不只是"它与 value 自洽"。

        ``RatioMetric`` 的模型校验保证 ``value`` 与分子分母一致，却管不了
        分子分母本身是不是这次运行的真实计数。一份"2/2 写成 1/1、
        value 也跟着改对"的指标能顺利通过模型校验，却与案例结果对不上。
        """
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        left = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        metrics = left.metrics
        assert metrics is not None
        lied = RatioMetric(numerator=1, denominator=1, value="1.000000")
        broken = left.model_copy(
            update={
                "metrics": metrics.model_copy(
                    update={
                        "cases": metrics.cases.model_copy(update={"case_pass_rate": lied}),
                    }
                )
            }
        )
        eligibility = evaluate_comparison_eligibility(broken, left)
        assert BLOCKER_INPUT_METRICS_MISMATCH in eligibility.blockers

    def test_self_inconsistent_metrics_are_refused_at_load_time(
        self, comparison_factory: Any, tmp_path: Path
    ) -> None:
        """更狠的一种错：连**文件内部**都对不上。

        这种输入根本读不进来——``CaseMetrics`` 自己的不变量就会拦下它。
        与"读得进来但与结构化结果不符"分开报，是因为两者的处置完全不同：
        前者是文件坏了，后者是文件与证据不符。
        """
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        result = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        path = _write(result, tmp_path)
        _tamper(
            path,
            lambda p: p["metrics"]["cases"].update(
                {"execution_error_cases": 1, "assertion_failed_cases": 0}
            ),
        )
        with pytest.raises(ComparisonInputError, match="结果结构校验失败"):
            load_run_result(path)

    def test_total_cases_must_match_the_manifest(self, comparison_factory: Any) -> None:
        """§九 第 1 项：清单里的案例总数是**权威**，指标必须与它一致。"""
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        left = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        # 造一份"指标说 3 条、清单说 2 条"的输入。
        metrics = left.metrics
        assert metrics is not None
        cases = metrics.cases.model_copy(update={"total_cases": 3, "not_executed_cases": 1})
        broken = left.model_copy(update={"metrics": metrics.model_copy(update={"cases": cases})})
        eligibility = evaluate_comparison_eligibility(broken, left)
        assert BLOCKER_INPUT_METRICS_MISMATCH in eligibility.blockers

    def test_dirty_worktree_blocks(self, comparison_factory: Any) -> None:
        """工作树脏 = 这份结果来自一个**无法从提交号重建**的代码状态。"""
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        right = comparison_factory.patch(left, "code", working_tree_clean=False)
        assert BLOCKER_DIRTY_WORKTREE in evaluate_comparison_eligibility(left, right).blockers

    def test_unknown_worktree_state_blocks_as_unavailable(self, comparison_factory: Any) -> None:
        """🔴 "不知道干不干净"与"不干净"都要拦——它不能算作"没问题"。"""
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        right = comparison_factory.patch(left, "code", working_tree_clean=None)
        assert BLOCKER_IDENTITY_UNAVAILABLE in evaluate_comparison_eligibility(left, right).blockers

    def test_ambiguous_assertion_identity_blocks(self, comparison_factory: Any) -> None:
        """同一案例里两条 ``(mode, name, expected)`` 完全相同的断言。

        案例模型**不允许**这样写，所以出现即意味着这份结果不是那个模型
        产出的——它是数据损坏，不是"断言多了几条"。
        """
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        case = left.cases[0]
        duplicated = case.model_copy(update={"assertions": (*case.assertions, case.assertions[0])})
        broken = left.model_copy(update={"cases": (duplicated,)})
        eligibility = evaluate_comparison_eligibility(broken, left)
        assert BLOCKER_ASSERTION_IDENTITY_AMBIGUOUS in eligibility.blockers

    def test_duplicate_case_ids_block(self, comparison_factory: Any) -> None:
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        duplicated = left.model_copy(update={"cases": (left.cases[0], left.cases[0])})
        eligibility = evaluate_comparison_eligibility(duplicated, left)
        assert BLOCKER_DUPLICATE_CASE_ID in eligibility.blockers
        assert BLOCKER_CASE_SET_INCONSISTENT in eligibility.blockers

    def test_execution_mode_must_agree_with_the_manifest(self, comparison_factory: Any) -> None:
        """结果说自己走的是内存路径，清单说走的 PostgreSQL——这份结果不自洽。"""
        dataset = comparison_factory.dataset(["case-001"])
        left = comparison_factory.run(dataset, {"case-001": "pass"})
        broken = comparison_factory.patch(left, "evaluation", execution_mode="postgres_http")
        eligibility = evaluate_comparison_eligibility(broken, left)
        assert eligibility.comparison_eligible is False
        assert "input_schema_invalid" in eligibility.blockers

    def test_partial_run_is_reported_as_not_independently_checkable(
        self, comparison_factory: Any
    ) -> None:
        """🔴 **如实报告查不了的部分**。

        类别的 ``total`` 来自**数据集**（含未执行的案例），而结果文件里
        只有执行过的案例。部分运行时它无从独立复核——此时记进 notes，
        而不是假装查过。"""
        dataset = comparison_factory.dataset(["case-001", "case-002"])
        full = comparison_factory.run(dataset, {"case-001": "pass", "case-002": "pass"})
        partial = comparison_factory.revise(full, dataset, cases=full.cases[:1])
        eligibility = evaluate_comparison_eligibility(partial, partial)
        # 部分运行**本身**不是阻塞项：它是一份合法的结果。
        assert eligibility.comparison_eligible is True
        comparison = compare_run_results(partial, partial)
        assert any("部分运行" in note for note in comparison.integrity_notes)
