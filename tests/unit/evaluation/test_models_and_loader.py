"""A 组：案例模型与加载器（阶段 7 · S1a）。

这一组测试回答的是：**什么样的案例会被接受，什么样的会被拒绝。**
被拒的情形逐条对应"如果不拒会怎样"——见每条测试的注释。
"""

from __future__ import annotations

import copy
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from ai_psi.evaluation.loader import DatasetError, load_dataset
from ai_psi.evaluation.models import CASE_SCHEMA_VERSION

pytestmark = pytest.mark.unit

CaseDict = dict[str, Any]
WriteDataset = Callable[..., Path]
WriteRaw = Callable[..., Path]

#: 仓库自带的正式数据集（10 条案例）。
_REPO = Path(__file__).resolve().parents[3]
_DATASET = _REPO / "evals" / "datasets"


class TestAcceptance:
    """合法输入必须被接受。"""

    def test_a_valid_case_loads(self, case_dict: CaseDict, write_dataset: WriteDataset) -> None:
        dataset = load_dataset(write_dataset(case_dict))
        assert len(dataset.cases) == 1
        case = dataset.cases[0]
        assert case.case_id == "fixture-001"
        assert case.schema_version == CASE_SCHEMA_VERSION

    def test_the_shipped_dataset_loads(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        """正式数据集必须能整体加载（10 条、3 类、schema 全部合法）。"""
        dataset = load_dataset(_DATASET)
        assert len(dataset.cases) == 10
        assert dataset.categories() == (
            "evidence_conflict",
            "simple_fact",
            "unable_to_determine",
        )

    def test_a_case_without_forbidden_is_allowed(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        """**不强制**每条案例都有 forbidden——硬要求是"每类至少一条"。

        因此这里必须让同一类别里**另有**一条带 forbidden 的案例：
        单独一条没有 forbidden 的案例会被数据集级规则拒绝（那是另一条测试）。
        """
        plain = copy.deepcopy(case_dict)
        plain["case_id"] = "fixture-plain"
        plain["expectations"] = {
            "required": [{"assertion": "final_state", "expected": "completed"}],
            "forbidden": [],
        }
        root = write_dataset(case_dict)
        write_dataset(plain, subdir="plain")
        dataset = load_dataset(root)
        without = [c for c in dataset.cases if c.case_id == "fixture-plain"]
        assert without[0].expectations.forbidden == []


class TestRejection:
    """坏输入必须被拒绝，且报错要能定位。"""

    def test_unknown_assertion_is_rejected(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        """不认识的断言若被放过，这条期望就永远不会生效——而报告会显示它通过了。"""
        case_dict["expectations"]["required"] = [
            {"assertion": "looks_thoughtful", "expected": True}
        ]
        with pytest.raises(DatasetError, match="不在注册表里"):
            load_dataset(write_dataset(case_dict))

    def test_unknown_category_is_rejected(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        case_dict["category"] = "vibes"
        with pytest.raises(DatasetError, match="category"):
            load_dataset(write_dataset(case_dict))

    def test_wrong_case_type_is_rejected(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        """S1a 只实现 cognitive_behavior；别的类型不该被"顺便"接受。"""
        case_dict["case_type"] = "attribution"
        with pytest.raises(DatasetError, match="case_type"):
            load_dataset(write_dataset(case_dict))

    def test_unsupported_schema_version_is_rejected(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        case_dict["schema_version"] = 99
        with pytest.raises(DatasetError, match="schema_version"):
            load_dataset(write_dataset(case_dict))

    def test_missing_required_field_is_rejected(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        del case_dict["intent"]
        with pytest.raises(DatasetError, match="intent"):
            load_dataset(write_dataset(case_dict))

    def test_wrong_field_type_is_rejected(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        """``"4"`` 不是 ``4``：宽容转换会把"写错类型"变成"期望被悄悄换掉"。"""
        case_dict["expectations"]["required"] = [
            {"assertion": "model_calls_at_most", "expected": "4"}
        ]
        with pytest.raises(DatasetError, match="类型"):
            load_dataset(write_dataset(case_dict))

    def test_case_without_any_assertion_is_rejected(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        """没有断言的案例是"跑了但什么都没验证"——它不该出现在数据集里。"""
        case_dict["expectations"] = {"required": [], "forbidden": []}
        with pytest.raises(DatasetError, match="至少要有一条断言"):
            load_dataset(write_dataset(case_dict))

    def test_empty_case_id_is_rejected(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        case_dict["case_id"] = ""
        with pytest.raises(DatasetError):
            load_dataset(write_dataset(case_dict))

    def test_unknown_field_is_not_silently_ignored(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        """多写一个没人读的字段，是"这条案例验了什么"的常见答案。"""
        case_dict["expected_state"] = "COMPLETED"
        with pytest.raises(DatasetError, match="expected_state"):
            load_dataset(write_dataset(case_dict))

    def test_duplicate_case_id_is_rejected(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        root = write_dataset(case_dict)
        other = copy.deepcopy(case_dict)
        other["intent"] = "另一条，但 case_id 相同"
        write_dataset(other, subdir="other")
        with pytest.raises(DatasetError, match="重复的 case_id"):
            load_dataset(root)

    def test_forbidden_duplicate_of_required_is_rejected(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        """同一条期望不能既要求成立又要求不成立。"""
        case_dict["expectations"]["forbidden"] = [
            {"assertion": "final_state", "expected": "completed"}
        ]
        with pytest.raises(DatasetError, match="成立与不成立"):
            load_dataset(write_dataset(case_dict))

    def test_contradictory_single_valued_expectations_are_rejected(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        """单值断言不能同时期望两个取值（既是 d0 又是 d1 不可能）。"""
        case_dict["expectations"]["required"] = [
            {"assertion": "depth_level", "expected": "d0"},
            {"assertion": "depth_level", "expected": "d1"},
        ]
        with pytest.raises(DatasetError, match="互相矛盾的期望"):
            load_dataset(write_dataset(case_dict))

    def test_forbidden_threshold_assertion_is_rejected(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        """阈值型断言的"不满足"是反向阈值，几乎总是把意思写反。"""
        case_dict["expectations"]["forbidden"] = [
            {"assertion": "model_calls_at_most", "expected": 4}
        ]
        with pytest.raises(DatasetError, match="forbidden"):
            load_dataset(write_dataset(case_dict))

    def test_wrong_expected_enum_value_is_rejected(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        case_dict["expectations"]["required"] = [{"assertion": "depth_level", "expected": "d9"}]
        with pytest.raises(DatasetError, match="不在允许取值里"):
            load_dataset(write_dataset(case_dict))

    def test_category_without_any_forbidden_case_is_rejected(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        """「每类至少一个非快乐路径」的可判定定义：该类别至少有一条 forbidden 断言。"""
        happy = copy.deepcopy(case_dict)
        happy["expectations"] = {
            "required": [{"assertion": "final_state", "expected": "completed"}],
            "forbidden": [],
        }
        root = write_dataset(happy)
        other = copy.deepcopy(happy)
        other["case_id"] = "fixture-002"
        write_dataset(other, subdir="more")
        with pytest.raises(DatasetError, match="非快乐路径"):
            load_dataset(root)


class TestMalformedFiles:
    """坏文件、坏结构、坏路径。"""

    def test_non_mapping_top_level_is_rejected(self, write_raw_file: WriteRaw) -> None:
        root = write_raw_file("- 这是一个列表\n- 不是映射\n")
        with pytest.raises(DatasetError, match="顶层必须是映射"):
            load_dataset(root)

    def test_yaml_syntax_error_reports_the_path(self, write_raw_file: WriteRaw) -> None:
        root = write_raw_file("schema_version: 1\n  bad_indent: [unclosed\n")
        with pytest.raises(DatasetError) as excinfo:
            load_dataset(root)
        assert excinfo.value.path.name == "broken.yaml"
        assert "YAML" in excinfo.value.detail

    def test_multi_document_yaml_is_rejected(self, write_raw_file: WriteRaw) -> None:
        """多文档通常意味着把两条案例写进了同一个文件。"""
        root = write_raw_file("a: 1\n---\nb: 2\n")
        with pytest.raises(DatasetError, match="恰好包含一个 YAML 文档"):
            load_dataset(root)

    def test_unexpected_extension_is_rejected(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        """放在数据集目录里却没人读的文件，会让覆盖率的账面数字包含它。"""
        root = write_dataset(case_dict)
        (root / "notes.txt").write_text("这不是案例", encoding="utf-8")
        with pytest.raises(DatasetError, match="非预期的文件扩展名"):
            load_dataset(root)

    def test_dotfiles_are_skipped(self, case_dict: CaseDict, write_dataset: WriteDataset) -> None:
        root = write_dataset(case_dict)
        (root / ".gitkeep").write_text("", encoding="utf-8")
        assert len(load_dataset(root).cases) == 1

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows 建符号链接需要额外权限")
    def test_symlink_escaping_the_dataset_is_rejected(
        self, case_dict: CaseDict, write_dataset: WriteDataset, tmp_path: Path
    ) -> None:
        """数据集读的是"这里的案例"，不是"这个文件碰巧能打开"。"""
        root = write_dataset(case_dict)
        outside = tmp_path / "outside.yaml"
        outside.write_text(yaml.safe_dump(case_dict, allow_unicode=True), encoding="utf-8")
        (root / "linked.yaml").symlink_to(outside)
        with pytest.raises(DatasetError, match="数据集目录之外"):
            load_dataset(root)


class TestOrdering:
    """顺序必须稳定，且不依赖文件系统的返回顺序。"""

    def test_cases_are_ordered_by_case_id(
        self, case_dict: CaseDict, write_dataset: WriteDataset
    ) -> None:
        root = write_dataset(case_dict, subdir="last")
        for case_id in ("zzz-001", "aaa-001", "mmm-001"):
            case = copy.deepcopy(case_dict)
            case["case_id"] = case_id
            write_dataset(case, subdir=case_id[0])
        dataset = load_dataset(root)
        ids = [case.case_id for case in dataset.cases]
        assert ids == sorted(ids)
