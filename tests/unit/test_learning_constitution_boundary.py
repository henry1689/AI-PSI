"""不变量 I12：认知宪法不能被学习模块修改。

🔴 **这道边界此前只写在文档里。**

`cognition/constitution.py` 的 I12 条目写着"learning/ 只读（阶段 6 以
导入依赖测试固定）"，`learning/__init__.py` 的文档也引用了同一件事——
而仓库里**根本没有那个测试**。宪法自己写着"强制必须是代码机制，
不是文档约定"，指向一个不存在的机制比不写更糟：它让人以为有人守着。

本文件就是那个机制。它由两部分组成：

1. **静态扫描**：`learning/` 的源码里不得出现对宪法符号的**赋值**
   （导入是允许的，导入本身就是只读的意图）；
2. **运行期比对**：导入 `learning` 前后宪法指纹必须一致——
   它挡得住写在模块级、导入即执行的副作用。

两者都是**结构性**的：不需要有人记得遵守，违反时测试就红。
"""

from __future__ import annotations

import ast
import importlib
import inspect
from pathlib import Path

import pytest

from ai_psi.cognition import constitution

pytestmark = pytest.mark.unit

#: 宪法里被 `learning/` 读取的全部符号。
#:
#: ⚠️ 这是一个**白名单**：`learning/` 新增对宪法符号的依赖时必须在这里登记，
#: 从而让"学习层开始读一条新的宪法常量"成为一个看得见的决定。
ALLOWED_CONSTITUTION_SYMBOLS = frozenset(
    {
        "AUTO_WRITABLE_MEMORY_TYPES",
        "AUTO_WRITE_MAX_SENSITIVITY",
        "FORBIDDEN_MEMORY_CONTENT_CLASSES",
    }
)


def _learning_sources() -> list[Path]:
    package = Path(inspect.getfile(constitution)).parents[1] / "learning"
    return sorted(package.rglob("*.py"))


def _constitution_imports(tree: ast.AST) -> set[str]:
    """源码里从 :mod:`ai_psi.cognition.constitution` 导入的符号名。"""
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith(
            "cognition.constitution"
        ):
            imported.update(alias.name for alias in node.names)
    return imported


class TestStaticBoundary:
    def test_the_learning_package_exists_and_has_sources(self) -> None:
        """扫描本身不能因为找不到文件而恒真。"""
        assert _learning_sources()

    def test_learning_only_imports_whitelisted_constitution_symbols(self) -> None:
        seen: set[str] = set()
        for path in _learning_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            seen |= _constitution_imports(tree)

        unexpected = sorted(seen - ALLOWED_CONSTITUTION_SYMBOLS)
        assert unexpected == [], (
            f"learning/ 开始依赖新的宪法符号：{unexpected}。"
            "这是有意的话，把它登记进 ALLOWED_CONSTITUTION_SYMBOLS"
        )

    def test_learning_never_writes_to_constitution_symbols(self) -> None:
        """🔴 真正的禁止：**赋值**。

        `import` 只是把名字绑进本模块，改不了源头；而
        `constitution.X = ...` 或 `X = ...`（对导入来的名字）会把
        "学习层改了宪法"变成事实。
        """
        violations: list[str] = []
        for path in _learning_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported = _constitution_imports(tree) | {"constitution"}
            for node in ast.walk(tree):
                targets: list[ast.expr] = []
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                    targets = [node.target]
                for target in targets:
                    name = _target_name(target)
                    if name is not None and name.split(".")[0] in imported:
                        violations.append(f"{path.name}: {ast.unparse(node)[:60]}")
        assert violations == [], f"learning/ 里出现了对宪法符号的赋值：{violations}"


def _target_name(node: ast.expr) -> str | None:
    """取出赋值目标的点分名字（``a`` / ``a.b``），取不到返回 None。"""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


class TestRuntimeBoundary:
    def test_importing_the_learning_package_does_not_change_the_constitution(
        self,
    ) -> None:
        """🔴 导入 `learning` 前后宪法指纹必须一致。

        静态扫描看得见"写了什么"，看不见"导入时执行了什么"——
        这个用例补上那一半。
        """
        before = constitution.constitution_fingerprint()
        for module in (
            "ai_psi.learning.error_classifier",
            "ai_psi.learning.experience_builder",
            "ai_psi.learning.offline_evaluator",
            "ai_psi.learning.pattern_detector",
            "ai_psi.learning.promotion_policy",
            "ai_psi.learning.proposal_generator",
        ):
            importlib.import_module(module)

        assert constitution.constitution_fingerprint() == before

    def test_the_fingerprint_actually_detects_changes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """⚠️ 一个恒定的指纹挡不住任何东西——先证明它会变。"""
        before = constitution.constitution_fingerprint()
        monkeypatch.setattr(constitution, "AUTO_WRITABLE_MEMORY_TYPES", frozenset())
        assert constitution.constitution_fingerprint() != before


class TestInvariantMetadata:
    def test_i12_does_not_point_at_a_nonexistent_test(self) -> None:
        """宪法条目里写下的机制名必须真的存在（本文件就是它）。"""
        statement = constitution.invariant("I12").enforcement
        assert Path(__file__).name in statement
