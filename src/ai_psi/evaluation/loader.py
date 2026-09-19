"""Golden Case 数据集的加载与严格校验（阶段 7 · S1a）。

## 为什么这里这么严

数据集是**评测的输入**。输入坏了而没被发现，出来的是一份**看起来很正常的报告**——
它比一个报错的加载器危险得多。所以本模块的总原则是：

> **宁可拒绝整个数据集，也不要跳过一条读不懂的案例。**

任何一条案例解析失败 → 抛 :class:`DatasetError`，**不返回部分结果**。
调用方（CLI）据此立即停止，不执行任何案例。

## YAML 解析的边界

🔴 这里用 ``PyYAML``，它是 ``uvicorn[standard]`` 的**传递依赖**——
不是本仓库新增的依赖，但也**不在** ``pyproject.toml`` 的 ``dependencies`` 里。
因此：

* 它是运行期存在的（``uvicorn[standard]`` 是本项目的运行时依赖）；
* 它没有 ``py.typed``，mypy strict 会报 ``import-untyped``，
  所以 ``pyproject.toml`` 里为它开了**单独一条** override；
* 未类型化的边界被收在 :func:`_parse_yaml` 一个函数里，**返回值是 ``object``**，
  下游全部是严格类型。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml
from pydantic import ValidationError

from ai_psi.evaluation.models import GoldenCase

__all__ = ["DATASET_SUFFIXES", "DatasetError", "GoldenDataset", "load_dataset"]

#: 允许的案例文件扩展名。别的扩展名一律**报错**，不跳过——
#: "放在数据集目录里但没人读"的文件，会让覆盖率的账面数字包含它。
DATASET_SUFFIXES: Final[tuple[str, ...]] = (".yaml", ".yml")


class DatasetError(ValueError):
    """数据集加载失败。

    Attributes:
        path: 出问题的文件（数据集级问题时是根目录）。
        detail: 人话说明。
    """

    def __init__(self, path: Path, detail: str) -> None:
        """初始化。

        Args:
            path: 出问题的路径。
            detail: 说明。
        """
        super().__init__(f"{path}: {detail}")
        self.path = path
        self.detail = detail


@dataclass(frozen=True, slots=True)
class GoldenDataset:
    """加载完成的数据集。

    Attributes:
        root: 数据集根目录（已解析为绝对路径）。
        cases: 全部案例，**按 ``case_id`` 升序**——顺序不依赖文件系统的返回顺序。
    """

    root: Path
    cases: tuple[GoldenCase, ...]

    def categories(self) -> tuple[str, ...]:
        """返回数据集里实际出现过的类别（排序后）。"""
        return tuple(sorted({case.category.value for case in self.cases}))


def _parse_yaml(text: str, path: Path) -> object:
    """把 YAML 文本解析成一个 Python 对象。

    🔴 这是**未类型化边界**：PyYAML 没有类型标注，返回值只能是 ``object``。
    下游必须自己把它交给 pydantic 校验，不得直接当结构用。

    Raises:
        DatasetError: YAML 语法错误，或文档不只一个。
    """
    try:
        documents = list(yaml.safe_load_all(text))
    except yaml.YAMLError as exc:
        raise DatasetError(path, f"YAML 解析失败：{exc}") from exc

    if len(documents) != 1:
        raise DatasetError(
            path,
            f"一个文件必须恰好包含一个 YAML 文档，实际有 {len(documents)} 个"
            "（多文档通常意味着把两条案例写进了同一个文件）",
        )
    return documents[0]


def _read_case(path: Path) -> GoldenCase:
    """读取并校验一条案例。

    Raises:
        DatasetError: 文件读不了、顶层不是映射、或字段不合法。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DatasetError(path, f"读取失败：{exc}") from exc

    raw = _parse_yaml(text, path)
    if not isinstance(raw, dict):
        raise DatasetError(
            path,
            f"YAML 顶层必须是映射（键值对），实际是 {type(raw).__name__}",
        )

    try:
        return GoldenCase.model_validate(raw)
    except ValidationError as exc:
        raise DatasetError(path, f"字段校验失败：\n{exc}") from exc


def _iter_case_files(root: Path) -> tuple[Path, ...]:
    """列出数据集里的全部案例文件，**顺序稳定**。

    Raises:
        DatasetError: 目录不存在，出现非预期扩展名，或文件指向目录之外。
    """
    if not root.is_dir():
        raise DatasetError(root, "数据集根目录不存在或不是目录")

    resolved_root = root.resolve()
    files: list[Path] = []
    # ``sorted`` 而不是 ``rglob`` 的原始顺序：文件系统的返回顺序不保证稳定，
    # 而"两次运行结果一致"是 S1a 的验收条件之一。
    for candidate in sorted(root.rglob("*")):
        if candidate.is_dir():
            continue
        if candidate.name.startswith("."):
            # 版本控制占位文件（``.gitkeep`` 之类）不是案例，明确跳过。
            continue
        if candidate.suffix.lower() not in DATASET_SUFFIXES:
            allowed = "、".join(DATASET_SUFFIXES)
            raise DatasetError(
                candidate,
                f"非预期的文件扩展名 {candidate.suffix!r}（只允许 {allowed}）",
            )
        # 🔴 路径防御：符号链接可以指向数据集目录之外。
        # 评测读的是"数据集里的案例"，不是"这个文件碰巧能打开"。
        if not candidate.resolve().is_relative_to(resolved_root):
            raise DatasetError(
                candidate,
                f"文件解析后落在数据集目录之外（{resolved_root}）——已拒绝加载",
            )
        files.append(candidate)
    return tuple(files)


def _check_dataset_rules(root: Path, cases: tuple[GoldenCase, ...]) -> None:
    """数据集级校验：这些规则**单看一条案例看不出来**。

    ``case_id`` 的唯一性在 :func:`load_dataset` 里边读边查（那里同时知道
    两个文件路径，能给出"它已经在哪个文件里出现过"），因此不在这里重复。

    Raises:
        DatasetError: 数据集为空，或某类别缺少非快乐路径案例。
    """
    if not cases:
        raise DatasetError(root, "数据集里一条案例都没有（至少需要一条）")

    # 「每类至少有一个非快乐路径」——本切片对它的**可判定定义**是：
    # 该类别里至少有一条案例声明了 forbidden 断言。
    # 没有这个定义，这条验收条件就只能靠肉眼，
    # 而肉眼看不出"这一类的三条其实都在验证同一件顺理成章的事"。
    with_forbidden = {case.category.value for case in cases if case.expectations.forbidden}
    missing = sorted({case.category.value for case in cases} - with_forbidden)
    if missing:
        raise DatasetError(
            root,
            f"这些类别没有任何一条「非快乐路径」案例（没有 forbidden 断言）：{missing}。"
            "每个类别至少要有一条案例声明它**不允许**发生什么",
        )


def load_dataset(root: Path) -> GoldenDataset:
    """加载并校验整个数据集。

    Args:
        root: 数据集根目录。

    Returns:
        加载完成的数据集，案例按 ``case_id`` 升序。

    Raises:
        DatasetError: 任何一条案例不合法，或数据集级规则不满足。
            出错时不返回部分结果——**坏数据集不该产出半份报告**。
    """
    files = _iter_case_files(root)

    loaded: list[GoldenCase] = []
    paths: dict[str, Path] = {}
    for path in files:
        case = _read_case(path)
        if case.case_id in paths:
            raise DatasetError(
                path,
                f"重复的 case_id {case.case_id!r}；它已经在 {paths[case.case_id]} 里出现过",
            )
        loaded.append(case)
        paths[case.case_id] = path

    ordered = tuple(sorted(loaded, key=lambda item: item.case_id))
    _check_dataset_rules(root, ordered)
    return GoldenDataset(root=root.resolve(), cases=ordered)
