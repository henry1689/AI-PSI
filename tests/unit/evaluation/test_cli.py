"""CLI 的退出码与产物（阶段 7 · S1a）。

退出码是 CI 唯一会看的东西，因此这里逐条钉住：
``0`` 全过、``1`` 有案例没过、``2`` 数据集本身就坏了。
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ai_psi.evaluation.cli import (
    EXIT_DATASET_ERROR,
    EXIT_OK,
    EXIT_TESTS_FAILED,
    main,
)

pytestmark = pytest.mark.unit

CaseDict = dict[str, Any]
WriteDataset = Callable[..., Path]
WriteRaw = Callable[..., Path]


def _argv(dataset: Path, tmp_path: Path) -> list[str]:
    """命令行参数。产物路径由 :func:`_paths` 单独给出，避免按下标取。"""
    return [
        "--dataset",
        str(dataset),
        "--output",
        str(_paths(tmp_path)[0]),
        "--canonical-output",
        str(_paths(tmp_path)[1]),
    ]


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    """(原始输出路径, canonical 输出路径)。"""
    return tmp_path / "out" / "raw.json", tmp_path / "out" / "canonical.json"


class TestExitCodes:
    """三个退出码各对应一种"别人怎么知道出了什么事"。"""

    def test_all_passing_returns_zero(
        self, case_dict: CaseDict, write_dataset: WriteDataset, tmp_path: Path
    ) -> None:
        assert main(_argv(write_dataset(case_dict), tmp_path)) == EXIT_OK

    def test_a_failing_case_returns_nonzero(
        self, case_dict: CaseDict, write_dataset: WriteDataset, tmp_path: Path
    ) -> None:
        case_dict["expectations"] = {
            "required": [{"assertion": "depth_level", "expected": "d4"}],
            "forbidden": [{"assertion": "analysis_module_ran", "expected": "philosophical"}],
        }
        assert main(_argv(write_dataset(case_dict), tmp_path)) == EXIT_TESTS_FAILED

    def test_a_broken_dataset_returns_two(self, write_raw_file: WriteRaw, tmp_path: Path) -> None:
        root = write_raw_file("- 不是映射\n")
        assert main(_argv(root, tmp_path)) == EXIT_DATASET_ERROR

    def test_a_broken_dataset_runs_no_case(self, write_raw_file: WriteRaw, tmp_path: Path) -> None:
        """🔴 数据集坏了就**一条都不跑**：跑一半的报告会被读成"其余都通过了"。"""
        root = write_raw_file("schema_version: 1\n  bad: [unclosed\n")
        assert main(_argv(root, tmp_path)) == EXIT_DATASET_ERROR
        assert not (tmp_path / "out" / "canonical.json").exists()

    def test_missing_dataset_returns_two(self, tmp_path: Path) -> None:
        assert main(_argv(tmp_path / "nonexistent", tmp_path)) == EXIT_DATASET_ERROR


class TestArtifacts:
    """产物的去向与内容。"""

    def test_reports_are_written_where_asked(
        self, case_dict: CaseDict, write_dataset: WriteDataset, tmp_path: Path
    ) -> None:
        raw_path, canonical_path = _paths(tmp_path)
        assert main(_argv(write_dataset(case_dict), tmp_path)) == EXIT_OK
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
        assert raw["summary"]["total"] == 1
        assert canonical["summary"]["passed_overall"] is True
        # 两份报告出自同一个内存结构：案例集合必须一致。
        assert [c["case_id"] for c in raw["cases"]] == [c["case_id"] for c in canonical["cases"]]

    def test_failure_reports_the_failing_case(
        self, case_dict: CaseDict, write_dataset: WriteDataset, tmp_path: Path
    ) -> None:
        case_dict["expectations"] = {
            "required": [{"assertion": "depth_level", "expected": "d4"}],
            "forbidden": [{"assertion": "analysis_module_ran", "expected": "philosophical"}],
        }
        _, canonical_path = _paths(tmp_path)
        main(_argv(write_dataset(case_dict), tmp_path))
        canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
        assert canonical["summary"]["failed"] == 1
        failed = [c for c in canonical["cases"] if not c["passed"]]
        assert failed[0]["case_id"] == "fixture-001"
        assert failed[0]["failure_reason"]

    def test_two_scenarios_are_reported_separately(
        self, case_dict: CaseDict, write_dataset: WriteDataset, tmp_path: Path
    ) -> None:
        """同一个数据集里一过一败：退出码为 1，且两份报告都能读。"""
        failing = copy.deepcopy(case_dict)
        failing["case_id"] = "fixture-failing"
        failing["expectations"] = {
            "required": [{"assertion": "depth_level", "expected": "d4"}],
            "forbidden": [{"assertion": "analysis_module_ran", "expected": "philosophical"}],
        }
        root = write_dataset(case_dict)
        write_dataset(failing, subdir="bad")
        _, canonical_path = _paths(tmp_path)
        assert main(_argv(root, tmp_path)) == EXIT_TESTS_FAILED
        canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
        assert canonical["summary"] == {
            "total": 2,
            "passed": 1,
            "failed": 1,
            "passed_overall": False,
        }
