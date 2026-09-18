"""变异测试运行器（阶段 6.5 §六）。

🔴 **为什么是一个脚本而不是一句 ``cosmic-ray exec``。**

cosmic-ray 的一次会话只能盯**一个**模块，而它跑得动的前提是
**测试命令足够窄**——跑全套 1751 个用例的话，每个变异体要 100 秒，
几百个变异体就是十几个小时。

因此本脚本做三件事：

1. **模块 → 聚焦测试命令**的映射写在这里，一眼可读；
2. 每个模块各自 init / exec，互不干扰；
3. 收集结果，算出**逐模块**的分数，并把**每一个存活变异体的 diff**
   写进报告——§六.5 明令禁止只报总分。

## 用法

```
uv run python mutation/run.py            # 全部模块
uv run python mutation/run.py pattern_detector   # 只跑一个
```

## ⚠️ 它会**就地改写**被测源码

cosmic-ray 的工作方式是把变异写进文件、跑测试、再还原。
中断（Ctrl-C、断电）可能留下一个**被改过的源文件**。
因此运行前请确认工作区是干净的，运行后用 ``git status`` 核对。

本脚本在退出前**无条件**把被测模块写回运行前的内容
（见 :func:`_restore`）——包括异常与 Ctrl-C 的路径。
实测过一次超时留下 `*` 被换成 `%,` 的变异，整个包直接 import 不了。
"""

# ↑ 这是一个**命令行工具**：它的输出就是它的交付物。
#   逐行标 noqa 会在下一次加 print 时静默失效。

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORK = REPO / "mutation" / ".work"
REPORT = REPO / "mutation" / "report.md"

__all__ = ["MODULES", "Module"]


@dataclass(frozen=True, slots=True)
class Module:
    """一个被变异测试覆盖的模块。

    Attributes:
        name: 报告里的短名。
        path: 相对仓库根的模块路径。
        tests: 聚焦的测试命令（不含 ``uv run`` 前缀）。
    """

    name: str
    path: str
    tests: str


#: 🔴 **模块 → 聚焦测试的映射。**

#:
#: 判据是"§六.2 点名的模块里，那些有**足够快**的聚焦测试的"。
#: 全套测试（~105 秒）对每个变异体跑一遍是不可行的；
#: 这里每条命令都限制在 10 秒以内。
#:
#: ⚠️ **覆盖面因此是有取舍的，必须说清楚**：
#: ``learning/experience_builder.py`` / ``learning/error_classifier.py`` /
#: ``application/feedback_service.py`` / 两个仓储实现**没有**被纳入——
#: 它们的测试要么依赖完整流水线、要么依赖真实数据库，
#: 单次执行远超 10 秒。这是残余风险，不是遗漏（记在
#: ``mutation/README.md`` 的"未覆盖"一节）。
MODULES: tuple[Module, ...] = (
    Module(
        "evaluation_weighting",
        "src/ai_psi/learning/evaluation_weighting.py",
        "pytest tests/unit/test_evaluation_weighting.py tests/unit/test_pattern_detector.py",
    ),
    Module(
        "pattern_detector",
        "src/ai_psi/learning/pattern_detector.py",
        "pytest tests/unit/test_pattern_detector.py",
    ),
    Module(
        "promotion_policy",
        "src/ai_psi/learning/promotion_policy.py",
        "pytest tests/unit/test_promotion_policy.py tests/unit/test_evaluation_weighting.py",
    ),
    Module(
        "proposal_generator",
        "src/ai_psi/learning/proposal_generator.py",
        "pytest tests/unit/test_proposal_generator.py",
    ),
    Module(
        "offline_evaluator",
        "src/ai_psi/learning/offline_evaluator.py",
        "pytest tests/unit/test_offline_evaluator.py",
    ),
    Module(
        "state_machine",
        "src/ai_psi/cognition/state_machine.py",
        "pytest tests/unit/test_state_machine.py",
    ),
    Module(
        "invariants",
        "src/ai_psi/reliability/invariants.py",
        "pytest tests/unit/test_invariants_selfcheck.py"
        " tests/unit/test_invariant_counterexamples.py",
    ),
    Module(
        "write_policy",
        "src/ai_psi/memory/write_policy.py",
        "pytest tests/unit/test_write_policy.py",
    ),
    Module(
        "proposal_gate",
        "src/ai_psi/application/proposal_gate.py",
        "pytest tests/unit/test_proposal_service.py",
    ),
    Module(
        "experiences",
        "src/ai_psi/domain/experiences.py",
        "pytest tests/unit/test_experiences_proposals.py tests/unit/test_pattern_detector.py",
    ),
)


@dataclass(frozen=True, slots=True)
class Equivalent:
    """一条**逐条登记过**的等价变异。

    🔴 **它不是"杀不掉就算了"，而是"它与原实现在所有可达输入上
    行为一致，且这个结论有理由"。**

    理由必须能被独立复核。写不出理由的存活变异只能走另外两条路：
    补测试、或者接受它（接受意味着承认这行没有测试保护）。

    Attributes:
        module: `MODULES` 里的短名。
        operator: cosmic-ray 的算子名。
        line: 变异所在行（1-based，与 `mutation_specs.start_pos_row` 一致）。
        reason: 为什么它与原实现等价。**必须可复核**，不能是"看起来差不多"。
    """

    module: str
    operator: str
    line: int
    reason: str


#: 逐条登记过的等价变异。
#:
#: ⚠️ 这里的每一条都经过人工判断并写下了理由。**新增条目需要理由**，
#: 加条目本身不该是让分数变绿的手段——`mutation/README.md` 的
#: 处置纪律里写明了这一点。
EQUIVALENTS: tuple[Equivalent, ...] = (
    Equivalent(
        module="write_policy",
        operator="core/ReplaceComparisonOperator_Is_Eq",
        line=65,
        reason=(
            "枚举属性 `WriteDecision.allows_write` 里 `self` 恒为一个 "
            "WriteDecision 成员。枚举成员是单例，`==` 与 `is` 对成员输入"
            "给出相同答案。对**非成员**输入两者会不同（StrEnum 的 `==` "
            "接受裸字符串），但 `self` 不可能是非成员——它由 `WritePolicy"
            ".decide()` 返回，那个方法的每个分支都返回枚举成员"
        ),
    ),
    Equivalent(
        module="write_policy",
        operator="core/ReplaceComparisonOperator_Is_LtE",
        line=65,
        reason=(
            "同上，且 `self <= WriteDecision.APPROVED` 依赖 StrEnum 的 "
            "字典序：四个成员的值是 approved / requires_user_confirmation "
            "/ requires_review / rejected，「approved」恰好排在字典序最前，"
            "因此 `x <= APPROVED` 对全部四个成员给出与 `x is APPROVED` "
            "相同的答案。⚠️ **这是一个侥幸等价**——改任何一个成员的字面量"
            "都会让它变成真变异。之所以仍登记为等价而非补测试，是因为"
            "没有一种输入能区分它们；改值的那一刻 `test_the_flag_matches"
            "_the_decision` 会立刻变红"
        ),
    ),
)


def _is_equivalent(module_name: str, operator: str, line: int) -> Equivalent | None:
    """该变异是否登记为等价变异。"""
    for item in EQUIVALENTS:
        if item.module == module_name and item.operator == operator and item.line == line:
            return item
    return None


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """跑一条命令，工作目录固定为仓库根。"""
    return subprocess.run(
        command,
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _snapshot(modules: list[Module]) -> dict[str, bytes]:
    """记下被测模块当前的字节内容。"""
    return {item.path: (REPO / item.path).read_bytes() for item in modules}


def _restore(modules: list[Module], before: dict[str, bytes]) -> None:
    """把被测模块写回运行前的字节内容。

    🔴 **这是中断安全的唯一保障。** 没有它，一次超时就会留下一个
    语法不成立的源文件——而 `run.py` 自己是跑在**同一个仓库**里的，
    下一次想用它来恢复都做不到（它 import 的包已经坏了）。

    刻意不做"比对后再决定"：**无条件写回**。因为这段代码运行时，
    工作区的这几个文件**本来就不该**有任何改动。
    """
    for item in modules:
        (REPO / item.path).write_bytes(before[item.path])


@dataclass(frozen=True, slots=True)
class Survivor:
    """一个存活的变异体。"""

    operator: str
    line: int
    diff: str


@dataclass(frozen=True, slots=True)
class Outcome:
    """一个模块的变异测试结果。"""

    module: Module
    killed: int
    survived: tuple[Survivor, ...]
    incompetent: int
    total: int
    equivalents: tuple[tuple[Survivor, str], ...] = ()
    """登记为等价变异、因此**不计分**的存活者（连同理由）。"""
    annotation_filtered: int = 0
    """落在类型标注范围内、因此被**排除**的变异条数。

    🔴 它们不是"杀不掉的变异"，是**可证明的行为等价物**：
    每个被测模块都启用了 ``from __future__ import annotations``，
    标注在运行期只是一段字符串。排除它们是去掉噪声，不是调分——
    前提由 :func:`_assert_pep563_is_active` 逐个模块核对。
    """

    @property
    def scored(self) -> int:
        """参与计分的变异体数——``INCOMPETENT`` 不算。

        ``INCOMPETENT`` 是"变异之后代码根本跑不起来"（例如把文档字符串
        换成数字），它不反映测试强度，被 cosmic-ray 单列。
        """
        return self.killed + len(self.survived)

    @property
    def score(self) -> float:
        """变异分数（0–1）。没有可计分变异体时为 1.0。"""
        return 1.0 if not self.scored else self.killed / self.scored


def _annotation_spans(module_path: str) -> list[tuple[int, int, int, int]]:
    """收集一个模块里**全部类型标注**的源码范围。

    🔴 **为什么要按位置过滤，而不是按算子名过滤。**

    cosmic-ray 的 ``ReplaceBinaryOperator_BitOr_*`` 一族在这几个模块上
    几乎全部来自 ``str | None`` 这类**标注**——一次 ``init`` 就能产出
    十几个变体。但同一个算子也会命中真正的位运算（``a | b``），
    按名字一刀切会把真变异一起砍掉。

    按**位置**过滤的前提是可证明的：本仓库每个模块都有
    ``from __future__ import annotations``（PEP 563），
    标注因此在运行期**从不求值**——它只是一段字符串。
    改动落在标注范围内时，行为必然不变。

    ⚠️ 这条前提不是"通常成立"，它由测试保证：
    ``_assert_pep563_is_active`` 会在过滤前逐个模块核对。

    Returns:
        ``(起始行, 起始列, 结束行, 结束列)``，行是 AST 的 1-based 行号。
    """
    import ast

    source = (REPO / module_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    spans: list[tuple[int, int, int, int]] = []
    for node in ast.walk(tree):
        candidates: list[ast.expr | None] = []
        if isinstance(node, (ast.AnnAssign, ast.arg)):
            candidates.append(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            candidates.append(node.returns)
        elif isinstance(node, ast.ClassDef):
            candidates.extend(item for item in node.bases if isinstance(item, ast.expr))
        for candidate in candidates:
            if candidate is None:
                continue
            spans.append(
                (
                    candidate.lineno,
                    candidate.col_offset,
                    candidate.end_lineno or candidate.lineno,
                    candidate.end_col_offset or 0,
                )
            )
    return spans


def _assert_pep563_is_active(modules: list[Module]) -> None:
    """核对每个被测模块都启用了 ``from __future__ import annotations``。

    🔴 **这是标注过滤的**前提**，因此必须先验证再依赖。**

    少了这一行，标注会在运行期求值，改动它就**可能**改变行为
    （例如 ``str | None`` 在 3.9 上直接 TypeError）——
    那时把它当作等价变异过滤掉，就是拿排除当调分。

    Raises:
        RuntimeError: 有模块没有启用 PEP 563。
    """
    missing = [
        item.path
        for item in modules
        if "from __future__ import annotations"
        not in (REPO / item.path).read_text(encoding="utf-8")
    ]
    if missing:
        msg = (
            "以下模块没有启用 PEP 563，类型标注会在运行期求值，"
            "因此不能按标注范围过滤变异：" + "、".join(missing)
        )
        raise RuntimeError(msg)


def _filter_annotations(module: Module, session: Path) -> int:
    """删掉落在类型标注范围内的变异。Returns: 删掉的条数。"""
    spans = _annotation_spans(module.path)
    connection = sqlite3.connect(session)
    try:
        rows = connection.execute(
            "SELECT job_id, start_pos_row, start_pos_col FROM mutation_specs"
        ).fetchall()
        doomed = [
            job_id
            for job_id, row, column in rows
            if any(
                (start_row, start_col) <= (row, column) <= (end_row, end_col)  # type: ignore[arg-type]
                for start_row, start_col, end_row, end_col in spans
            )
        ]
        connection.executemany(
            "DELETE FROM work_items WHERE job_id = ?", [(job_id,) for job_id in doomed]
        )
        connection.commit()
    finally:
        connection.close()
    return len(doomed)


def _execute(module: Module) -> Outcome:
    """跑一个模块的变异测试。"""
    session = WORK / f"{module.name}.sqlite"
    session.unlink(missing_ok=True)
    config = WORK / f"{module.name}.toml"
    config.write_text(
        "[cosmic-ray]\n"
        f'module-path = "{module.path}"\n'
        "timeout = 60.0\n"
        "excluded-modules = []\n"
        f'test-command = "uv run {module.tests} -x -q --no-cov -p no:cacheprovider"\n'
        "\n[cosmic-ray.distributor]\n"
        'name = "local"\n',
        encoding="utf-8",
    )

    _run(["uv", "run", "cosmic-ray", "init", str(config), str(session)], timeout=300)
    filtered = _filter_annotations(module, session)
    _run(["uv", "run", "cosmic-ray", "exec", str(config), str(session)], timeout=7200)

    connection = sqlite3.connect(session)
    try:
        rows = connection.execute(
            """
            SELECT ms.operator_name, ms.start_pos_row, wr.test_outcome, wr.diff
            FROM work_results wr JOIN mutation_specs ms ON ms.job_id = wr.job_id
            """
        ).fetchall()
    finally:
        connection.close()

    killed = sum(1 for _, _, outcome, _ in rows if outcome == "KILLED")
    incompetent = sum(1 for _, _, outcome, _ in rows if outcome == "INCOMPETENT")
    survivors: list[Survivor] = []
    equivalents: list[tuple[Survivor, str]] = []
    for operator, line, outcome, diff in rows:
        if outcome != "SURVIVED":
            continue
        entry = Survivor(operator=operator, line=line, diff=diff)
        registered = _is_equivalent(module.name, operator, line)
        if registered is None:
            survivors.append(entry)
        else:
            equivalents.append((entry, registered.reason))
    return Outcome(
        module=module,
        killed=killed,
        survived=tuple(survivors),
        equivalents=tuple(equivalents),
        incompetent=incompetent,
        total=len(rows) + filtered,
        annotation_filtered=filtered,
    )


def _summarise(diff: str) -> str:
    """把一段 diff 压成"改了哪一行"。"""
    changed = [
        line
        for line in diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    return "\n".join(changed)


def main(argv: list[str]) -> int:
    """跑变异测试并打印报告。"""
    wanted = set(argv[1:])
    selected = [item for item in MODULES if not wanted or item.name in wanted]
    if not selected:
        print(f"没有匹配的模块：{sorted(wanted)}")
        return 2

    WORK.mkdir(parents=True, exist_ok=True)
    before = _snapshot(selected)
    _assert_pep563_is_active(selected)

    outcomes: list[Outcome] = []
    try:
        for module in selected:
            print(f"== {module.name} …", flush=True)
            outcome = _execute(module)
            outcomes.append(outcome)
            print(
                f"   {outcome.killed}/{outcome.scored} = {outcome.score:.1%}"
                f"（另有 {outcome.incompetent} 个 incompetent）",
                flush=True,
            )
    finally:
        # 🔴 **必须还原。** cosmic-ray 是就地改写的，而一次超时或
        # Ctrl-C 会把一个**语法都不成立**的变异留在源码里
        # （实测：`*` 被换成 `%,`，整个包直接 import 不了）。
        # 那不是"结果不准"，是"仓库坏了"。
        _restore(selected, before)

    total_killed = sum(item.killed for item in outcomes)
    total_scored = sum(item.scored for item in outcomes)
    overall = total_killed / total_scored if total_scored else 1.0

    report = _render(outcomes, overall)
    REPORT.write_text(report, encoding="utf-8")
    print(f"\n报告已写入 {REPORT.relative_to(REPO)}", flush=True)
    print(json.dumps({"overall": overall}, ensure_ascii=False))

    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    return 0 if overall >= 0.9 else 1


def _render(outcomes: list[Outcome], overall: float) -> str:
    """把结果渲染成一份**可直接提交的** Markdown 报告。"""
    lines: list[str] = [
        "# 变异测试报告（阶段 6.5 §六）",
        "",
        "> 由 `uv run python mutation/run.py` 生成。**不要手工编辑。**",
        "",
        "## 逐模块分数",
        "",
        "| 模块 | 杀死 | 计分总数 | 分数 | 存活 | incompetent | 标注等价物 | 登记等价物 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for outcome in outcomes:
        lines.append(
            f"| `{outcome.module.path}` | {outcome.killed} | {outcome.scored} "
            f"| **{outcome.score:.1%}** | {len(outcome.survived)} "
            f"| {outcome.incompetent} | {outcome.annotation_filtered} "
            f"| {len(outcome.equivalents)} |"
        )
    lines += [
        "",
        f"**合计：{sum(item.killed for item in outcomes)}/"
        f"{sum(item.scored for item in outcomes)} = {overall:.1%}**",
        "",
        "⚠️ `incompetent` 是「变异之后代码根本跑不起来」（例如把文档字符串",
        "换成数字），它不反映测试强度，因此**不计分**。",
        "",
        "⚠️ `标注等价物` 是落在**类型标注**范围内的变异。被测模块全部启用",
        "`from __future__ import annotations`（PEP 563），标注在运行期",
        "只是一段字符串——改动它**必然**不改变行为。排除它们是去掉噪声，",
        "不是把分数调上去；前提由 `_assert_pep563_is_active` 逐个模块核对。",
        "",
        "## 存活变异体（逐条）",
        "",
    ]
    if not any(item.survived for item in outcomes):
        lines.append("（无）")
    for outcome in outcomes:
        if not outcome.equivalents:
            continue
        lines.append(f"### 登记等价物 · `{outcome.module.path}`")
        lines.append("")
        for item, reason in outcome.equivalents:
            lines.append(f"- **{item.operator}** @ 第 {item.line} 行")
            lines.append("  ```diff")
            lines.extend(f"  {line}" for line in _summarise(item.diff).splitlines())
            lines.append("  ```")
            lines.append(f"  **等价理由**：{reason}")
        lines.append("")
    lines.append("")
    lines.append("## 存活变异体（逐条）")
    lines.append("")
    for outcome in outcomes:
        if not outcome.survived:
            continue
        lines.append(f"### `{outcome.module.path}`")
        lines.append("")
        for item in outcome.survived:
            lines.append(f"- **{item.operator}** @ 第 {item.line} 行")
            lines.append("  ```diff")
            lines.extend(f"  {line}" for line in _summarise(item.diff).splitlines())
            lines.append("  ```")
        lines.append("")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
