"""evaluation 测试的公共夹具。

🔴 **这里的夹具只做两件事**：把一条案例写成临时 YAML、把数据集目录搭出来。
它们**不**模拟加载器或断言判定——被测的就是那些东西。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml


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
