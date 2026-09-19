"""D 组：结果序列化与确定性（阶段 7 · S1a）。

🔴 这一组是 S1a 的**核心验收条件**：同样的输入跑两次，
canonical JSON 必须**逐字节一致**。原始输出**不**做这个承诺——
它包含运行期生成的标识符。
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ai_psi.evaluation.loader import load_dataset
from ai_psi.evaluation.runner import GoldenRunner, RunResult, build_mock_runtime
from ai_psi.evaluation.serialization import (
    canonical_payload,
    dumps,
    raw_payload,
    write_reports,
)

pytestmark = pytest.mark.unit

CaseDict = dict[str, Any]
WriteDataset = Callable[..., Path]

#: canonical 输出里**不允许**出现的键——它们要么是运行期生成的，
#: 要么是措辞。出现即说明"两次运行一致"这条承诺已经不成立。
_VOLATILE_KEYS = ("cognitive_round_id", "failure_detail", "response_text", "detail")


async def _run_twice(root: Path) -> tuple[RunResult, RunResult]:
    runner = GoldenRunner(build_mock_runtime())
    cases = load_dataset(root).cases
    return await runner.run_dataset(cases), await runner.run_dataset(cases)


@pytest.fixture
def two_cases(case_dict: CaseDict, write_dataset: WriteDataset) -> Path:
    """一个两条案例的数据集（同类别，其中一条带 forbidden）。"""
    plain = copy.deepcopy(case_dict)
    plain["case_id"] = "fixture-002"
    plain["expectations"] = {
        "required": [{"assertion": "final_state", "expected": "completed"}],
        "forbidden": [],
    }
    root = write_dataset(case_dict)
    write_dataset(plain, subdir="b")
    return root


class TestDeterminism:
    """两次运行必须一致。"""

    async def test_canonical_payload_is_equal_across_runs(self, two_cases: Path) -> None:
        first, second = await _run_twice(two_cases)
        assert canonical_payload(first) == canonical_payload(second)

    async def test_canonical_bytes_are_identical_across_runs(self, two_cases: Path) -> None:
        """🔴 逐字节一致——比"结构相等"更强，也更接近 ``cmp`` 的判据。"""
        first, second = await _run_twice(two_cases)
        assert dumps(canonical_payload(first)) == dumps(canonical_payload(second))

    async def test_raw_payload_may_differ(self, two_cases: Path) -> None:
        """原始输出**本来就该**不同：回合 id 每次运行都不一样。"""
        first, second = await _run_twice(two_cases)
        ids_first = [c["cognitive_round_id"] for c in raw_payload(first)["cases"]]
        ids_second = [c["cognitive_round_id"] for c in raw_payload(second)["cases"]]
        assert ids_first != ids_second
        assert all(ids_first)

    async def test_case_order_is_stable(self, two_cases: Path) -> None:
        first, second = await _run_twice(two_cases)
        assert [c.case_id for c in first.cases] == [c.case_id for c in second.cases]
        assert [c.case_id for c in first.cases] == sorted(c.case_id for c in first.cases)

    async def test_assertion_order_is_the_declaration_order(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        """断言顺序稳定：required 按声明顺序在前，forbidden 在后。"""
        case_dict["expectations"] = {
            "required": [
                {"assertion": "final_state", "expected": "completed"},
                {"assertion": "response_present", "expected": True},
            ],
            "forbidden": [{"assertion": "analysis_module_ran", "expected": "philosophical"}],
        }
        dataset = load_dataset(write_dataset(case_dict))
        runner = GoldenRunner(build_mock_runtime())
        result = await runner.run_case(dataset.cases[0])
        assert [a.name for a in result.assertions] == [
            "final_state",
            "response_present",
            "analysis_module_ran",
        ]
        assert [a.mode for a in result.assertions] == [
            "required",
            "required",
            "forbidden",
        ]


class TestPayloadSeparation:
    """原始与 canonical 的职责边界。"""

    async def test_canonical_excludes_volatile_fields(self, two_cases: Path) -> None:
        result, _ = await _run_twice(two_cases)
        text = dumps(canonical_payload(result))
        for key in _VOLATILE_KEYS:
            assert f'"{key}"' not in text, f"canonical 里不该出现易变字段 {key}"

    async def test_raw_keeps_the_diagnostic_fields(self, two_cases: Path) -> None:
        """原始输出必须能用来回到出问题的那个回合。"""
        result, _ = await _run_twice(two_cases)
        payload = raw_payload(result)
        assert payload["cases"][0]["cognitive_round_id"]
        assert "detail" in payload["cases"][0]["assertions"][0]

    async def test_canonical_keeps_the_semantic_fields(self, two_cases: Path) -> None:
        result, _ = await _run_twice(two_cases)
        observation = canonical_payload(result)["cases"][0]["observation"]
        assert observation is not None
        for key in (
            "state",
            "depth",
            "model_calls_used",
            "metacognitive_loops",
            "hypothesis_count",
            "analysis_kinds",
        ):
            assert key in observation


class TestEncoding:
    """文本层面的稳定性。"""

    def test_dumps_is_sorted_and_indented(self) -> None:
        text = dumps({"b": 1, "a": {"d": 2, "c": 3}})
        assert text.index('"a"') < text.index('"b"')
        assert text.index('"c"') < text.index('"d"')
        assert "\n" in text

    def test_dumps_ends_with_exactly_one_newline(self) -> None:
        text = dumps({"a": 1})
        assert text.endswith("\n")
        assert not text.endswith("\n\n")

    def test_dumps_keeps_chinese_readable(self) -> None:
        """``ensure_ascii=False``：中文不被转义，报告仍然人可读。"""
        assert "完成" in dumps({"状态": "完成"})

    async def test_written_files_are_byte_identical(self, two_cases: Path, tmp_path: Path) -> None:
        first, second = await _run_twice(two_cases)
        path_a = tmp_path / "a.json"
        path_b = tmp_path / "b.json"
        write_reports(first, raw_path=tmp_path / "raw1.json", canonical_path=path_a)
        write_reports(second, raw_path=tmp_path / "raw2.json", canonical_path=path_b)
        assert path_a.read_bytes() == path_b.read_bytes()

    async def test_written_files_use_lf_endings(self, two_cases: Path, tmp_path: Path) -> None:
        """Windows 上也不写 CRLF——否则跨平台比较会先败在行尾上。"""
        result, _ = await _run_twice(two_cases)
        path = tmp_path / "canonical.json"
        write_reports(result, raw_path=tmp_path / "raw.json", canonical_path=path)
        assert b"\r\n" not in path.read_bytes()
        assert json.loads(path.read_text(encoding="utf-8"))["summary"]["total"] == 2
