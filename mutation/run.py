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
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO = Path(__file__).resolve().parent.parent
WORK = REPO / "mutation" / ".work"
REPORT = REPO / "mutation" / "report.md"

#: 传给 cosmic-ray（以及它派生出来的 pytest）的环境变量。
#:
#: 🔴 **少了它们，变异分数会被系统性压低——而且是在最坏的方向上。**
#:
#: cosmic-ray 在 ``cosmic_ray/testing.py`` 里用 UTF-8 解码被测命令的
#: stdout。Windows 中文区域下，被 spawn 的 pytest 默认按 **GBK** 写管道，
#: `stdout.decode("utf-8")` 直接抛 ``UnicodeDecodeError``；那个异常被
#: `run_tests` 的兜底 `except Exception` 接住，该变异体被记成
#: ``INCOMPETENT``。
#:
#: 要害在 ``INCOMPETENT`` 的判定条件——下面是**逐字读源码**得来的，不是推测：
#:
#: * ``returncode != 0`` → **KILLED**（测试失败、语法错误、收集失败都走这条）
#: * ``returncode == 0`` → SURVIVED
#: * 超时 → KILLED
#: * **只有 `run_tests` 自己抛异常** → INCOMPETENT
#:
#: 也就是说，「变异之后代码跑不起来」**根本不会进这个桶**（它走
#: ``returncode != 0``）。能进来的只有两种：**输出解码失败**，
#: 和**命令根本没启动起来**（``FileNotFoundError`` / ``shlex`` 解析失败）。
#: 两者都是**度量故障**，不是测试强度。
#:
#: 而中文 traceback 只在测试失败时才打印——所以被吞掉的恰好是
#: 「本来会被杀死」的那些。实测（``invariants`` 模块，修复前）：
#: 130 个变异体里 104 个 incompetent、只报 2 个 killed，分数 **7.7%**。
#: 补上这两个变量后 incompetent 归零，同一份测试的真实分数是 **100%**。
#:
#: ⚠️ 副作用：被测进程跑在 UTF-8 模式下。已 grep 过 ``src/`` 与 ``tests/``
#: 全部 ``open`` / ``read_text`` / ``write_text``，没有隐式依赖区域编码的调用。
_SUBPROCESS_ENV: Final[dict[str, str]] = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
}

__all__ = ["MODULES", "Incompetent", "Module"]


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
        "pytest tests/unit/test_experiences_proposals.py"
        " tests/unit/test_experiences_semantics.py"
        " tests/unit/test_pattern_detector.py",
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
    mutation: str
    """变异后那一行里的片段，用来指出这条登记覆盖**哪一族**变异体。

    🔴 **少了它，登记等价会连带放行没被论证过的变异体。** 同一行上常常
    有一族变异体，例如 ``UUID(int=index + 1)`` 会同时产出 ``index + 0``
    和 ``index + 2``。只按（模块, 算子, 行号）匹配的话，为 ``+ 2`` 登记
    一条等价，``+ 0`` 也跟着不算分了——而后者**真的能被测出来**。

    片段要不要带上被改的值，取决于这条登记的论证覆盖到哪里：

    * 只论证了**某一个**取值 → 片段必须含被改的值（如 ``index + 2``），
      否则同行的其它取值被顺带放行；
    * 论证覆盖了**整族**（例如那一行上所有 NumberReplacer 变体都等价）
      → 刻意用一个不含值的片段（如 ``forbidden[``）让它一次覆盖全族，
      **并且理由里必须写明为什么整族都成立**。

    ⚠️ 无论哪种，片段都必须按**变异后**的样子写：NumberReplacer 把
    ``index + 1`` 改成 ``index + 2``（单空格），写 ``index + 1`` 永远不命中。

    报告里会列出每条登记实际放行了几条变异体，覆盖太宽时看得见。
    """

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
        mutation="self == WriteDecision.APPROVED",
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
        mutation="self <= WriteDecision.APPROVED",
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
    Equivalent(
        module="invariants",
        operator="core/NumberReplacer",
        line=198,
        mutation="forbidden[",
        reason=(
            "`forbidden` 的四个词（confirmed / verified / established / "
            "canonical）**没有一个**能构造出 `HypothesisStatus`，这一点由 "
            "`test_i11...` 之前的 `constructible` 分支与 "
            "`TestCheckInventory` 的正向用例各自验证过。因此 `forbidden[0]`、"
            "`[1]`、`[-1]` 取到的都是**一个同样不可构造的词**，"
            "detail 里那句「构造 X 会失败」对四个取值**同为真**。"
            "被改的只有那句说明文字举的例子，而没有任何代码读这句话——"
            "它只出现在人看的报告里"
        ),
    ),
    Equivalent(
        module="invariants",
        operator="core/NumberReplacer",
        line=403,
        mutation="probe = _probe_proposal(",
        reason=(
            "`_check_i11` 拿到这个探针**只读一个属性**：`can_become_active`。"
            "它是 `ImprovementProposal` 上的类级属性，与支撑经验条数无关；"
            "0 条、1 条、2 条的提案在该分支上行为完全相同"
            "（实测 `ImprovementProposal(supporting_experience_ids=[])` "
            "构造成功且 `can_become_active` 仍为 False）。"
            "⚠️ 与 `_check_i10` 的同名写法不同：那里的 1 与门槛是"
            "**被 spy 用例钉住的**（`TestTheCheckProbesTheInputsItClaims`），"
            "因为 `_check_i10` 的 detail 会声称自己验了「单条」和「三条」"
        ),
    ),
    Equivalent(
        module="invariants",
        operator="core/NumberReplacer",
        line=353,
        mutation="index + 2",
        reason=(
            "探针的契约是一条三合一的话：**n 个互异、非全零、且构造合法**"
            "的 UUID。`+1` 给出 1,2,3，`+2` 给出 2,3,4——两条都满足全部三项，"
            "而具体取值没有第二类观察者（`meets_escalation_threshold` 只做 "
            "`len(set(...))`），所以 `+2` 改不出任何可观察差异。"
            "⚠️ 同一行上的另外七个 NumberReplacer 变体**不是**等价："
            "`+ 0` / `* 1` / `// 1` / `** 1` 给出 0,1,2，`<< 1` 给出 0,2,4，"
            "`^ 1` 给出 1,0,3——**三个集合都含 `UUID(int=0)`**，也就是本模块"
            "自己的固定探针标识 `_PROBE_UUID`；`/ 1` 给出浮点，`uuid.UUID` "
            "照收而 `.hex` 会抛 TypeError。"
            "🔴 杀掉它们的**不是** count/distinct 那两条用例——"
            "`{0,1,2}`、`{0,2,4}`、`{1,0,3}` 全都互异、条数也对，那两条对它们"
            "全部通过；真正杀掉的是 `test_the_probe_ids_are_genuine_uuids` "
            "与 `test_the_probe_ids_never_collide_with_the_fixed_probe_id`"
        ),
    ),
    Equivalent(
        module="invariants",
        operator="core/ReplaceTrueWithFalse",
        line=54,
        mutation="frozen=True, slots=False",
        reason=(
            "`frozen=True` **本身**就拒绝一切属性赋值（`FrozenInstanceError`），"
            "与 `slots` 无关——实测在一个只有 `frozen=True` 的 dataclass 上"
            "`obj.y = 2` 同样抛 `FrozenInstanceError`。`slots` 改的是内存布局"
            "与 `__dict__` 是否存在，而本仓库没有任何代码读 `__dict__`，"
            "所以这条变异在所有可达输入上行为一致。"
            "⚠️ 同族还有一条 `frozen=True → False`，**那一条是真变异**"
            "（它让检查结果可以被事后改写），由 `test_checks_are_frozen` 杀掉"
        ),
    ),
    Equivalent(
        module="invariants",
        operator="core/ReplaceComparisonOperator_Eq_Is",
        line=74,
        mutation="is invariant_id",
        reason=(
            "`_statement_of` 的实参只有三个**字面量**（I01 / I10 / I11），"
            "而 `INVARIANTS` 里的 `invariant_id` 也是字面量。CPython 会把"
            "形如标识符的字符串字面量intern 到同一张表里，因此两个对象"
            "**是同一个**，`is` 与 `==` 对全部可达输入答案相同。"
            "⚠️ **这是一个实现细节上的侥幸等价**，与 `write_policy` 的 "
            "`<=` 那条同类：换一个 Python 实现（或改成从数据里读编号）"
            "它立刻变成真变异。之所以仍登记为等价而不是补测试，是因为"
            "能杀掉它的只有「拿拼接出来的字符串去查」那种断言——"
            "那测的是「别对字符串用 is」这条代码风格，"
            "而不是本模块对外的任何保证"
        ),
    ),
    # ------------------------------------------------------------------
    # domain/experiences.py：**枚举比较**与**派生字段长度**两族。
    #
    # 🔴 这一族数量不少，但它们的成立条件是**同一个**，所以理由写在一起：
    #
    # 《枚举比较》：本仓库一律用 `is` 比较枚举成员。`is` 与
    # `==` / `!=` / `is not` 在所有可达输入上同答案，因为
    # (1) 枚举成员是单例；(2) 调用方**只传成员**——字段的类型标注是
    # `ExperienceEvaluator`，pydantic 会把 JSON 里的字符串**强制转换**
    # 成成员，所以到达比较时它已经是成员了。
    # ⚠️ 这条理由的边界值得记住：**传裸字符串**会让 `is` 与 `==` 分家，
    # 而那时 `is` 会把一个裸的 "internal_metacognition" 判成
    # 「不是内部元认知」——**静默地放行自我确认**。当前不可达，
    # 已记入 docs/risks.md（R59），不是靠"测不出来"糊过去的。
    #
    # 《StrEnum 的字典序》：少数几条是 `<` / `>=` 落在同一个枚举上。
    # StrEnum 的排序比的是**字符串**，而这些成员的字面量恰好使答案与
    # `is` / `is not` 相同——**侥幸等价**。守它的用例是
    # `TestTheEnumLiteralsSomeEquivalencesRestOn`：改任何一个成员的字面量，
    # 那条会先红，而不是让这里的登记悄悄开始放行真变异。
    # ------------------------------------------------------------------
    Equivalent(
        module="experiences",
        operator="core/NumberReplacer",
        line=287,
        mutation="min_length= 0",
        reason=(
            "`canonical_key` 由 `_check_canonical_identity` 钉死为**派生值**"
            "（与 (回合, 评价对象, 种类, 抽取器版本) 逐字一致），而派生值"
            "最短也有 42 个字符。把下界从 1 降到 0，**没有任何输入**能"
            "走到那条长度检查——一致性校验先拒绝了它。"
            "⚠️ 这依赖「派生值永远不短」这个事实，而它由 `canonical_key_for` "
            "的拼接方式保证（四段用 `|` 连接，含 36 字符的 UUID）"
        ),
    ),
    Equivalent(
        module="experiences",
        operator="core/NumberReplacer",
        line=287,
        mutation="min_length= 2",
        reason=(
            "同上，方向反过来：把下界抬到 2 也不会拒掉任何东西——"
            "派生值同样是 42 字符起步。两半都要登记，因为"
            "「没有输入能走到这里」对**两侧**都成立"
        ),
    ),
    Equivalent(
        module="experiences",
        operator="core/ReplaceTrueWithFalse",
        line=470,
        mutation="slots=False",
        reason=(
            "`ExperienceAssessment` 的 `frozen=True` **本身**就拒绝一切属性"
            "赋值（由 `test_it_cannot_be_mutated` 钉住），`slots` 只影响"
            "内存布局与 `__dict__` 是否存在，而没有任何代码读 `__dict__`。"
            "与 `invariants` @54 同族。⚠️ 同族的 `frozen=True → False` "
            "**是真变异**，已经被同一条用例杀掉"
        ),
    ),
    Equivalent(
        module="experiences",
        operator="core/ReplaceComparisonOperator_Gt_GtE",
        line=539,
        mutation="rank >= evaluation.rank",
        reason=(
            "`ExperienceEvaluation` 四档的 `rank` **互异**（0/1/2/3，"
            "由 `test_the_ranks_are_distinct` 钉住）。等秩 ⟹ 它们是"
            "**同一个成员**，于是 `evaluation = record.evaluation` 是一次"
            "空操作。`>` 与 `>=` 只在等秩时分叉，而那时分叉不可观察"
        ),
    ),
    Equivalent(
        module="experiences",
        operator="core/ReplaceComparisonOperator_IsNot_Lt",
        line=580,
        mutation="evaluation < ExperienceEvaluation.UNASSESSED",
        reason=(
            "StrEnum 的 `<` 比**字符串**。实测：suspected / supported / "
            'confirmed 三个值都 `< "unassessed"`，而 unassessed 不 `<` 自己'
            "——于是 `x < UNASSESSED` 与 `x is not UNASSESSED` 对全部四个成员"
            "答案相同。⚠️ **侥幸等价**，见本段的《StrEnum 的字典序》"
        ),
    ),
    Equivalent(
        module="experiences",
        operator="core/ReplaceComparisonOperator_IsNot_NotEq",
        line=580,
        mutation="evaluation != ExperienceEvaluation.UNASSESSED",
        reason="《枚举比较》：同段说明。`!=` 与 `is not` 对成员输入同答案",
    ),
    Equivalent(
        module="experiences",
        operator="core/ReplaceComparisonOperator_Is_GtE",
        line=588,
        mutation="evaluation >= ExperienceEvaluation.UNASSESSED",
        reason=(
            "《StrEnum 的字典序》的另一半：除 unassessed 之外的三个值都"
            '`< "unassessed"`，因此 `x >= UNASSESSED` 只在 x 就是'
            "unassessed 时为真——与 `x is UNASSESSED` 同答案。⚠️ 侥幸等价"
        ),
    ),
    Equivalent(
        module="experiences",
        operator="core/ReplaceComparisonOperator_Is_Eq",
        line=588,
        mutation="evaluation == ExperienceEvaluation.UNASSESSED",
        reason="《枚举比较》：同段说明",
    ),
    Equivalent(
        module="experiences",
        operator="core/ReplaceComparisonOperator_Is_Eq",
        line=596,
        mutation="evaluator == ExperienceEvaluator.INTERNAL_METACOGNITION",
        reason=(
            "《枚举比较》：同段说明。⚠️ 这一条尤其要记住它的边界——"
            "`is` 与 `==` 的分叉点正是「来了一个非成员」，而那时 `is` 会"
            "**静默放行**自我确认（见 R59）"
        ),
    ),
    Equivalent(
        module="experiences",
        operator="core/ReplaceComparisonOperator_Gt_IsNot",
        line=597,
        mutation="rank is not ExperienceEvaluation.SUSPECTED.rank",
        reason=(
            "`_EXPERIENCE_EVALUATION_RANK` 的取值是 0/1/2/3，全部落在 CPython "
            "的小整数缓存里，因此 `rank is not 1` 与 `rank != 1` 同答案。"
            "而 `rank == 0`（unassessed）在那之前已经被 588 那一问拦掉，"
            "到不了这里。⚠️ 同样是**侥幸等价**：门槛一旦超过 256，"
            "`is not` 立刻变成真变异"
        ),
    ),
    Equivalent(
        module="experiences",
        operator="core/ReplaceComparisonOperator_Gt_NotEq",
        line=597,
        mutation="rank != ExperienceEvaluation.SUSPECTED.rank",
        reason="同上：rank 的四个取值互异且都在小整数缓存内，`!=` 与 `>` 同答案",
    ),
    # ------------------------------------------------------------------
    # 《slots 族》：`@dataclass(frozen=True, slots=True)` → `slots=False`。
    #
    # 🔴 这是**跨模块的同一个事实**，不是七次巧合：`frozen=True`
    # **本身**就拒绝一切属性赋值（实测：只有 `frozen=True`、没有 `slots`
    # 的 dataclass 上 `obj.y = 2` 同样抛 `FrozenInstanceError`）。
    # `slots` 只影响内存布局与 `__dict__` 是否存在，而本仓库没有任何代码
    # 读 `__dict__`——所以它改不出任何**可观察**差异。
    #
    # ⚠️ 同族的 `frozen=True → False` 是**真变异**（它让结论可以被事后
    # 改写），每个模块各有一条用例守着，逐条写在下面条目的旁边。
    #
    # 之所以仍逐条登记而不是写成一条"族规则"：片段 `slots=False` 已经
    # 把它精确地限定在 `slots` 那一半上，逐条列出来便于复核"这一处
    # 到底有没有人守 frozen"。条数多但理由只有一句。
    # ------------------------------------------------------------------
    Equivalent(
        module="pattern_detector",
        operator="core/ReplaceTrueWithFalse",
        line=104,
        mutation="slots=False",
        reason="《slots 族》。同族的 `frozen=True → False` 由 `TestTheResultsAreImmutable` 杀掉",
    ),
    Equivalent(
        module="pattern_detector",
        operator="core/ReplaceTrueWithFalse",
        line=152,
        mutation="slots=False",
        reason="《slots 族》。同上",
    ),
    Equivalent(
        module="pattern_detector",
        operator="core/ReplaceTrueWithFalse",
        line=172,
        mutation="slots=False",
        reason="《slots 族》。同上",
    ),
    Equivalent(
        module="promotion_policy",
        operator="core/ReplaceTrueWithFalse",
        line=87,
        mutation="slots=False",
        reason=(
            "《slots 族》。同族的 `frozen=True → False` 由 "
            "`TestTheConstructionAndAccessorsAreStable::test_the_evidence_is_immutable` 杀掉"
        ),
    ),
    Equivalent(
        module="promotion_policy",
        operator="core/ReplaceTrueWithFalse",
        line=115,
        mutation="slots=False",
        reason="《slots 族》。`PromotionDecision` 的 frozen 由既有的 `TestDecisionShape` 守着",
    ),
    Equivalent(
        module="pattern_detector",
        operator="core/ReplaceUnaryOperator_USub_Invert",
        line=262,
        mutation="(~item.weighted_count,",
        reason=(
            "`~x` 就是 `-x - 1`，是 `-x` 的**单调变换**（相差一个常数 1）。"
            "排序只关心相对次序，因此 `~weighted_count` 与 `-weighted_count` "
            "给出完全相同的排列。⚠️ 注意它**不是**「随便什么一元算子都行」："
            "同族的 `not` / 去掉 `-` / `+` 都是真变异，由 "
            "`TestDeterministicOrdering` 的两条新用例杀掉"
        ),
    ),
    Equivalent(
        module="pattern_detector",
        operator="core/ReplaceUnaryOperator_USub_Invert",
        line=265,
        mutation="(~item.weighted_count,",
        reason="同上（`suppressed.sort` 用的是同一个键表达式）",
    ),
    Equivalent(
        module="pattern_detector",
        operator="core/NumberReplacer",
        line=334,
        mutation="evaluations[- 0]",
        reason=(
            "这一支**只在全部参与计数的评价权重都为 0 时**才进入"
            "（判据是 `all(not counts_toward_threshold(...))`，而 "
            "`counts_toward_threshold` 就是 `weight_of(x) > 0`）。"
            "既然每个元素的权重都是 0，`weight_of(evaluations[i])` 对**任何**"
            "下标都是 0——取第一个还是最后一个不可观察。⚠️ 这句话依赖"
            "「权重非负」，而它由 `EvaluationWeighting.__post_init__` 显式拒绝负数保证"
        ),
    ),
    Equivalent(
        module="pattern_detector",
        operator="core/ReplaceUnaryOperator_USub_Not",
        line=334,
        mutation="evaluations[not 1]",
        reason="同上：`not 1` 是 `False`，即下标 0——同样落在「全部权重为 0」的不可观察区间里",
    ),
    # ------------------------------------------------------------------
    # 《`*,` → `/,` 族》：函数签名里"后面全是关键字参数"的那个星号。
    #
    # 🔴 这一族**改变的是 API 的宽容度，不是行为**。
    #
    # `def f(self, *, a, b)` → `def f(self, /, a, b)` 之后，`a` / `b`
    # 从"只能按关键字传"变成"两种都行"。而本仓库里这些函数
    # **全部按关键字调用**（唯一被允许的那种），因此每一个现有调用的
    # 行为都不变——变异体只是**多允许**了一种此前会报错的写法。
    #
    # 换句话说：没有任何一条断言能"观察"到它的区别，除非去断言
    # "位置调用必须报错"——那测的是 Python 的调用约定，不是本系统的
    # 任何保证。所以登记为等价，而不是写四条这样的用例。
    # ------------------------------------------------------------------
    Equivalent(
        module="pattern_detector",
        operator="core/ReplaceBinaryOperator_Mul_Div",
        line=194,
        mutation="/,",
        reason="《`*,` → `/,` 族》",
    ),
    Equivalent(
        module="promotion_policy",
        operator="core/ReplaceBinaryOperator_Mul_Div",
        line=138,
        mutation="/,",
        reason="《`*,` → `/,` 族》",
    ),
    Equivalent(
        module="proposal_generator",
        operator="core/ReplaceBinaryOperator_Mul_Div",
        line=81,
        mutation="/,",
        reason="《`*,` → `/,` 族》",
    ),
    Equivalent(
        module="proposal_generator",
        operator="core/ReplaceBinaryOperator_Mul_Div",
        line=187,
        mutation="/,",
        reason="《`*,` → `/,` 族》",
    ),
    Equivalent(
        module="proposal_gate",
        operator="core/ReplaceComparisonOperator_Is_Eq",
        line=359,
        mutation="pattern.error_type == error_type",
        reason="《枚举比较》",
    ),
    Equivalent(
        module="proposal_gate",
        operator="core/ReplaceComparisonOperator_Is_Eq",
        line=362,
        mutation="item.error_type == error_type",
        reason=(
            "《枚举比较》。⚠️ 同一行上的 `or` 与 `>=` / `<=` **不是**等价，"
            "已由 `TestTheScanLookupIgnoresHalfMatchesInSuppressed` 杀掉"
        ),
    ),
    Equivalent(
        module="proposal_gate",
        operator="core/ReplaceFalseWithTrue",
        line=126,
        mutation="compare=True",
        reason=(
            "所有**真**结论携带的都是同一个 `_GATE_TOKEN` 对象（同一性相同），"
            "而手工构造的结论根本构造不出来（`__post_init__` 会抛）。"
            "于是把 `_token` 纳入相等性比较，`==` 的结果一个字都不变。"
            "⚠️ 同行的 `repr=True` **不是**等价——它会把凭据印进日志与断言输出，"
            "已由 `test_the_token_stays_out_of_the_repr` 杀掉"
        ),
    ),
    Equivalent(
        module="proposal_gate",
        operator="core/ReplaceAndWithOr",
        line=157,
        mutation="or self.decision is not None",
        reason=(
            "`and` 比 `or` 结合得紧，因此这一改等价于 "
            "`pattern is not None or (decision is not None and ...)`。"
            "两条返回路径上 `pattern` 与 `decision` **总是同生共死**"
            "（要么都给、要么都是 None），所以"
            "「pattern 有而 decision 没有」这个能让两者分叉的状态不可达。"
            "🔴 这条等价依赖那条耦合，而它由 "
            "`TestEveryVerdictKeepsThePatternAndTheDecisionTogether` 显式钉住"
        ),
    ),
    Equivalent(
        module="proposal_gate",
        operator="core/ReplaceOrWithAnd",
        line=207,
        mutation="'；'.join(self.reasons) and '未给出理由'",
        reason=(
            "`self.reasons` 在两条返回路径上**都不可能为空**："
            "一条是 `list(suppressed) or [兜底]`，另一条来自 "
            "`PromotionPolicy.decide`（五条条件各至少追加一句）。"
            "因此 `'未给出理由'` 这个兜底目前**不可达**，"
            "`or` 与 `and` 给出同样的消息。"
            "⚠️ 这也意味着那段兜底是死代码——保留它是为了将来"
            "某条路径真的不带理由时消息仍然可读，"
            "而那时这条登记会失配并报出来"
        ),
    ),
    Equivalent(
        module="proposal_generator",
        operator="core/ReplaceComparisonOperator_Is_Eq",
        line=198,
        mutation="error_type == ErrorType.UNKNOWN_ERROR",
        reason=(
            "《枚举比较》。⚠️ 同行的 `>=` **不是**等价"
            "（实测 `value_substitution` 与 `user_model_error` 在字典序上"
            '都 `>= "unknown_error"`），已由 '
            "`test_only_unknown_error_gets_the_investigation_text` 杀掉"
        ),
    ),
    Equivalent(
        module="proposal_gate",
        operator="core/ReplaceTrueWithFalse",
        line=66,
        mutation="slots=False",
        reason=(
            "《slots 族》。同族的 `frozen=True → False` "
            "由 `TestTheGateEvidenceDefaults::test_it_is_immutable` 杀掉"
        ),
    ),
    Equivalent(
        module="proposal_gate",
        operator="core/ReplaceTrueWithFalse",
        line=99,
        mutation="slots=False",
        reason=(
            "《slots 族》。同族的 `frozen=True → False` "
            "由 `TestTheGateVerdictIsDerivedNotFilled::test_the_verdict_is_immutable` 杀掉"
        ),
    ),
    Equivalent(
        module="proposal_gate",
        operator="core/ReplaceBinaryOperator_Mul_Div",
        line=233,
        mutation="/,",
        reason="《`*,` → `/,` 族》",
    ),
    Equivalent(
        module="proposal_gate",
        operator="core/ReplaceBinaryOperator_Mul_Div",
        line=275,
        mutation="/,",
        reason="《`*,` → `/,` 族》",
    ),
    Equivalent(
        module="proposal_gate",
        operator="core/ReplaceBinaryOperator_Mul_Div",
        line=349,
        mutation="/,",
        reason="《`*,` → `/,` 族》",
    ),
    Equivalent(
        module="proposal_gate",
        operator="core/ReplaceComparisonOperator_IsNot_NotEq",
        line=134,
        mutation="self._token != _GATE_TOKEN",
        reason=(
            "`_GATE_TOKEN` 是 `object()`，而 `object` 的 `__eq__` / `__ne__` "
            "**就是**同一性比较（没有子类覆写）。`_token` 的取值只有两个："
            "那个 token 本身，或 `None`。三种组合下 `!=` 与 `is not` 答案相同。"
            "⚠️ 这条依赖「凭据是裸 object」——哪天它换成一个自定义了 "
            "`__eq__` 的类型，立刻变成真变异"
        ),
    ),
    Equivalent(
        module="offline_evaluator",
        operator="core/ReplaceTrueWithFalse",
        line=43,
        mutation="slots=False",
        reason=(
            "《slots 族》。同族的 `frozen=True → False` 由 `TestTheValueObjectsAreImmutable` 杀掉"
        ),
    ),
    Equivalent(
        module="offline_evaluator",
        operator="core/ReplaceTrueWithFalse",
        line=64,
        mutation="slots=False",
        reason="《slots 族》",
    ),
    Equivalent(
        module="offline_evaluator",
        operator="core/ReplaceTrueWithFalse",
        line=82,
        mutation="slots=False",
        reason=(
            "《slots 族》。同族的 `frozen=True → False` 由 `TestTheValueObjectsAreImmutable` 杀掉"
        ),
    ),
    Equivalent(
        module="offline_evaluator",
        operator="core/ReplaceTrueWithFalse",
        line=202,
        mutation="slots=False",
        reason=(
            "《slots 族》。同族的 `frozen=True → False` 由 `TestTheValueObjectsAreImmutable` 杀掉"
        ),
    ),
    Equivalent(
        module="offline_evaluator",
        operator="core/ReplaceComparisonOperator_Eq_Is",
        line=229,
        mutation="key is name",
        reason=(
            "指标名全部是 `snapshot()` 里的**字面量**，而 `delta_for` 的调用方"
            "（`HIGHER_IS_BETTER_METRICS` / `LOWER_IS_BETTER_METRICS` 的成员，"
            "以及测试里直接写的同一个字面量）用的也是字面量。CPython 把形如"
            "标识符的字符串字面量 intern 到同一张表里，因此两侧**是同一个对象**。"
            "与 `invariants` @74 同族：⚠️ **实现细节上的侥幸等价**，"
            "换一个 Python 实现、或让指标名从配置里读，它立刻变成真变异"
        ),
    ),
    Equivalent(
        module="offline_evaluator",
        operator="core/ReplaceComparisonOperator_Is_Eq",
        line=259,
        mutation="item.state == RoundState.COMPLETED",
        reason=(
            "《枚举比较》。⚠️ 同行的 `<=` **不是**等价"
            '（`"analyzing"` 与 `"cancelled"` 都排在 `"completed"` 前面），'
            "已由 `TestOnlyCompletedRoundsCountAsCompleted` 杀掉"
        ),
    ),
    Equivalent(
        module="offline_evaluator",
        operator="core/ReplaceBinaryOperator_Mul_Div",
        line=329,
        mutation="/,",
        reason="《`*,` → `/,` 族》",
    ),
    Equivalent(
        module="offline_evaluator",
        operator="core/ReplaceTrueWithFalse",
        line=386,
        mutation="strict=False",
        reason=(
            "`snapshot()` 恒定返回**同样七条**指标（`rounds` 为空时返回空元组，"
            "而 `compare` 在那之前就返回了），因此 `baseline` 与 `candidate` "
            "的长度永远相等，`strict=True` 的检查**没有输入能触发**。"
            "⚠️ 这条依赖「snapshot 的条目数不随数据变化」——"
            "哪天有条件指标（例如「没有模型调用时不算这一条」）时，"
            "它立刻变成真变异，而那时这条登记会失配并报出来"
        ),
    ),
    Equivalent(
        module="evaluation_weighting",
        operator="core/ReplaceComparisonOperator_Gt_NotEq",
        line=87,
        mutation="self.weights[evaluation] != 0",
        reason=(
            "`EvaluationWeighting.__post_init__` 对**每一个**评价状态显式拒绝负权重"
            "（`if weight < 0: raise ValueError`），因此权重恒为自然数，"
            "`> 0` 与 `!= 0` 在所有可达输入上同答案。"
            "⚠️ 判据依赖那条构造期校验：它一旦被放宽，这条立刻变成真变异"
        ),
    ),
)


def _stale_equivalents(selected: list[Module], outcomes: list[Outcome]) -> list[Equivalent]:
    """本轮**一条变异体都没匹配上**的等价登记。

    🔴 **存在的理由：等价登记会随着源码编辑悄悄失效。**

    登记里带行号，而在被登记的那一行**上面**加任何东西（注释、空行、
    一个新分支）都会让行号整体下移，这条登记从此匹配不到任何人。
    它的症状是分数**无缘无故掉下来**，而报告里只有一条"存活"，
    看不出"这条其实早就登记过了"。

    实测过两次，其中一次就是引入这个检查的那个提交自己：

    * 给 `_check_i10` 补了 8 行注释 → `invariants` 的两条登记（353 / 303）
      双双失配，分数从 95.3% 掉到 93.7%；
    * 往 `__all__` 里插了一行 → 另外四条（196 / 364 / 314 / 72）整体下移
      一位，全部失配。

    两次的症状都一样：分数无缘无故掉下来，而报告上只是多了几个看不出
    所以然的存活变异体。

    ⚠️ **只覆盖正向失效。** 反方向——行号漂到**另一个**变异体所在的行——
    这里看不出来。所幸匹配是（模块, 算子, 行号, 片段）四元组，行号漂移
    通常会被算子或片段挡住；但**不含被改值的片段挡不住**（例如
    `forbidden[` 落在同行任何 NumberReplacer 上都命中）。真的发生了
    只需要一次 `git log -p` 就能查清，因此这里不假装能自动发现它。
    """
    matched = {entry for outcome in outcomes for _, entry in outcome.equivalents}
    names = {item.name for item in selected}
    return [entry for entry in EQUIVALENTS if entry.module in names and entry not in matched]


def _stale_hint(entry: Equivalent, outcomes: list[Outcome]) -> str:
    """失配登记的自诊断：**这个模型里到底有哪些行/算子**。

    🔴 「失配了，自己去找」把成本推给了下一个人，而这是这套机制里
    最常发生的一种故障——光作者自己就撞了两次（补注释、往 `__all__`
    插一行），每次都表现成"分数无缘无故掉了几个点"。

    所以这里直接把候选摆出来：同一个算子在哪些行有变异体、最接近的
    那一行差多少。
    """
    outcome = next((item for item in outcomes if item.module.name == entry.module), None)
    if outcome is None:
        return "（本轮没有跑这个模块）"

    same_operator = [spec for spec in outcome.specs if spec.operator == entry.operator]
    if not same_operator:
        return f"本模块没有任何 `{entry.operator}` 变异体——算子名也可能写错了"

    # 🔴 先按**片段**精确找，而不是按"最近的行号"猜。
    #    一个算子在本模块里可能有十几行，最近的往往不是它——
    #    用片段命中就直接给出了确定答案。
    exact = sorted(
        {spec.line for spec in same_operator if entry.mutation in _added_lines(spec.diff)}
    )
    if exact:
        where = "、".join(f"**{line}**" for line in exact)
        extra = (
            "（命中多行说明这条登记覆盖的是整族，那是**正常的**，前提是理由里论证了整族）"
            if len(exact) > 1
            else ""
        )
        return f"片段 `{entry.mutation}` 命中的实际行是 {where} —— 把 line 改成它。{extra}"

    rows = sorted({spec.line for spec in same_operator})
    where = "、".join(str(line) for line in rows[:8])
    more = "" if len(rows) <= 8 else f" …（共 {len(rows)} 行）"
    return (
        f"这个片段在本模块**一处也没命中**。`{entry.operator}` 在以下行有变异体："
        f"{where}{more}。⚠️ 片段必须按**变异后**的样子写"
        f"（例如 `index + 2`，不是 `index + 1`）"
    )


def _added_lines(diff: str) -> str:
    """取出 diff 里**变异后**的那些行（去掉 ``+++`` 文件头）。"""
    return "\n".join(
        text for text in diff.splitlines() if text.startswith("+") and not text.startswith("+++")
    )


def _is_equivalent(module_name: str, operator: str, line: int, diff: str) -> Equivalent | None:
    """该变异是否登记为等价变异。

    🔴 **必须带上变异后那一行的内容一起比对**：``(模块, 算子, 行号)``
    三元组区分不了同一行上的多个变异体。见 :class:`Equivalent`。
    """
    added = _added_lines(diff)
    for item in EQUIVALENTS:
        if (
            item.module == module_name
            and item.operator == operator
            and item.line == line
            and item.mutation in added
        ):
            return item
    return None


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """跑一条命令，工作目录固定为仓库根。

    🔴 环境里强行注入 :data:`_SUBPROCESS_ENV`：见那个常量上的说明，
    少了它，被测命令的中文输出会让 cosmic-ray 自己崩在解码上，
    而崩出来的结果是**看起来像测试不够强**的 ``INCOMPETENT``。
    """
    return subprocess.run(
        command,
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, **_SUBPROCESS_ENV},
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
class Incompetent:
    """一个 ``INCOMPETENT`` 变异体，**连同它为什么如此**。

    🔴 **在这个配置下，它几乎只能是「度量坏了」。** 判定条件见
    :data:`_SUBPROCESS_ENV`：``returncode != 0`` 一律 KILLED，
    只有 ``run_tests`` 自己抛异常才进这里——而被测命令跑不起来
    （语法错误、构造失败）走的正是 ``returncode != 0``。

    记 ``output`` 是因为这个分类**曾经骗过人**：中文输出让
    ``stdout.decode("utf-8")`` 抛 ``UnicodeDecodeError``，
    于是「这个变异体被杀死」被记成了「这个变异体无所谓」。
    只报一个计数的话，报告里看到的是「104 个 incompetent」，
    看不到任何一句 ``Traceback``——于是没人会去查。
    """

    operator: str
    line: int
    output: str


@dataclass(frozen=True, slots=True)
class Outcome:
    """一个模块的变异测试结果。"""

    module: Module
    killed: int
    survived: tuple[Survivor, ...]
    incompetent: tuple[Incompetent, ...]
    total: int
    equivalents: tuple[tuple[Survivor, Equivalent], ...] = ()
    """登记为等价变异、因此**不计分**的存活者（连同它匹配到的那条登记）。

    ⚠️ 存的是**整条登记**而不是理由字符串：跑完之后要反过来核对
    "这一轮到底有哪几条登记真的匹配上了"，见 :func:`_stale_equivalents`。
    """
    specs: tuple[Survivor, ...] = ()
    """本模块**全部**变异体（不只存活的）。

    🔴 存全量是为了让等价登记失配时能自报诊断：告诉人「第 197 行没有
    变异体，同一个算子在 198 行有」。行号漂移是这套机制最常见的故障，
    而「失配了，自己去找」把成本推给了下一个人。
    """
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

        ⚠️ **``INCOMPETENT`` 不是「变异之后代码跑不起来」**，
        它走的是 ``returncode != 0`` → KILLED（见 :data:`_SUBPROCESS_ENV`
        里的逐字判定表）。能进这个桶的只有解码失败与命令起不来，
        两者都是度量故障。

        因此 ``incompetent`` 非零时**这一行的分数没有意义**——
        `main` 会因此 fail-closed（退出码非 0），而不是放它过去。
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
                (start_row, start_col) <= (row, column) <= (end_row, end_col)
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
    # 🔴 cosmic-ray 用 `shlex.split` 切测试命令，Windows 上反斜杠会被当转义
    #    吃掉，命令于是根本起不来 —— 而那正好落进 INCOMPETENT 桶，
    #    整个模块的分数会变成「0/0 = 100%」。宁可在这里当场红。
    if "\\" in module.tests:
        msg = (
            f"模块 {module.name} 的测试命令里有反斜杠：{module.tests!r}。"
            "cosmic-ray 用 shlex.split 解析，反斜杠会被吃掉，命令起不来，"
            "而结果会被记成清一色的 INCOMPETENT（0/0 会被算成 100%）。"
            "请改用正斜杠"
        )
        raise ValueError(msg)

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
            SELECT ms.operator_name, ms.start_pos_row, wr.test_outcome,
                   wr.diff, wr.output
            FROM work_results wr JOIN mutation_specs ms ON ms.job_id = wr.job_id
            """
        ).fetchall()
    finally:
        connection.close()

    killed = sum(1 for _, _, outcome, _, _ in rows if outcome == "KILLED")
    survivors: list[Survivor] = []
    equivalents: list[tuple[Survivor, Equivalent]] = []
    incompetents: list[Incompetent] = []
    for operator, line, outcome, diff, output in rows:
        if outcome == "INCOMPETENT":
            # 🔴 记下**为什么**。这个分类曾经把 104 个真·被杀的变异体
            # 装了进去（cosmic-ray 解码中文输出失败），只留一个计数的话
            # 报告里看不出任何异常。见 `_SUBPROCESS_ENV`。
            incompetents.append(Incompetent(operator=operator, line=line, output=output))
            continue
        if outcome != "SURVIVED":
            continue
        entry = Survivor(operator=operator, line=line, diff=diff)
        registered = _is_equivalent(module.name, operator, line, diff)
        if registered is None:
            survivors.append(entry)
        else:
            equivalents.append((entry, registered))
    return Outcome(
        module=module,
        killed=killed,
        survived=tuple(survivors),
        equivalents=tuple(equivalents),
        incompetent=tuple(incompetents),
        specs=tuple(
            Survivor(operator=operator, line=line, diff=diff) for operator, line, _, diff, _ in rows
        ),
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
                f"（另有 {len(outcome.incompetent)} 个 incompetent）",
                flush=True,
            )
            if outcome.incompetent:
                # 🔴 不静默。这个分类曾经吞掉 104 个「会被杀死」的变异体，
                # 屏幕上却只有一行无害的计数。详见 `_SUBPROCESS_ENV`。
                print(
                    f"   ⚠️ {len(outcome.incompetent)} 个变异体被记为 incompetent，"
                    f"全部不可计分——先看报告里的样本，别直接接受这个分数",
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
    total_incompetent = sum(len(item.incompetent) for item in outcomes)
    overall = total_killed / total_scored if total_scored else 1.0

    # 🔴 等价登记会随源码编辑行号漂移而**静默失效**——症状是分数无缘无故
    #    掉下来。跑完必须反过来核对"有哪几条登记一条都没匹配上"。
    stale = _stale_equivalents(selected, outcomes)
    if stale:
        print(
            f"\n⚠️ {len(stale)} 条等价登记没有匹配到任何变异体（行号多半已漂移）：",
            file=sys.stderr,
        )
        for item in stale:
            print(
                f"    {item.module} {item.operator} @ 第 {item.line} 行"
                f"（片段 {item.mutation!r}）—— {_stale_hint(item, outcomes)}",
                file=sys.stderr,
            )

    report = _render(outcomes, overall, stale)
    REPORT.write_text(report, encoding="utf-8")
    print(f"\n报告已写入 {REPORT.relative_to(REPO)}", flush=True)
    print(
        json.dumps(
            {
                "overall": overall,
                "stale_equivalents": len(stale),
                "incompetent": total_incompetent,
            },
            ensure_ascii=False,
        )
    )

    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)

    # 🔴 **度量坏了就当失败，而不是报一个漂亮的分数。**
    #
    # `overall` 在 `total_scored == 0` 时是 1.0——那正是"被测命令根本起不来"
    # 的样子：每个变异体都 INCOMPETENT，一个都不计分，于是 0/0 被算成 100%
    # 并且绿灯放行。告警可以被人忽略，**绿灯不会**。
    #
    # 同理，incompetent 非零意味着有变异体的结果**没被测量**，
    # 而按 `_SUBPROCESS_ENV` 里的判定表，那几乎只可能是度量故障。
    if total_incompetent or total_scored == 0:
        print(
            f"\n🔴 结果不可信：incompetent={total_incompetent}、计分变异体={total_scored}。"
            "看报告里的样本节定位原因（解码失败 / 命令起不来），"
            "**不要**按这个分数下结论。",
            file=sys.stderr,
        )
        return 1
    return 0 if overall >= 0.9 and not stale else 1


def _render(outcomes: list[Outcome], overall: float, stale: list[Equivalent]) -> str:
    """把结果渲染成一份**可直接提交的** Markdown 报告。"""
    lines: list[str] = [
        "# 变异测试报告（阶段 6.5 §六）",
        "",
        "> 由 `uv run python mutation/run.py` 生成。**不要手工编辑。**",
        "",
    ]
    if stale:
        lines += [
            "## 🔴 等价登记失配（先修这个）",
            "",
            f"有 **{len(stale)}** 条登记本轮**一条变异体都没匹配上**。",
            "登记里带行号，在被登记的那一行**上面**加任何东西都会让它整体下移，",
            "于是分数无缘无故掉下来、而报告里只看得到「多了几个存活变异体」。",
            "行号也可能漂到**另一个**变异体上，把真变异当等价放行——",
            "所以这一节非空时，下面的分数**不可信**。",
            "",
        ]
        for item in stale:
            lines.append(
                f"- `{item.module}` · **{item.operator}** @ 第 {item.line} 行"
                f"（片段 `{item.mutation}`）"
            )
            lines.append(f"  - {_stale_hint(item, outcomes)}")
        lines.append("")
    lines += [
        "## 逐模块分数",
        "",
        "| 模块 | 杀死 | 计分总数 | 分数 | 存活 | incompetent | 标注等价物 | 登记等价物 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for outcome in outcomes:
        lines.append(
            f"| `{outcome.module.path}` | {outcome.killed} | {outcome.scored} "
            f"| **{outcome.score:.1%}** | {len(outcome.survived)} "
            f"| {len(outcome.incompetent)} | {outcome.annotation_filtered} "
            f"| {len(outcome.equivalents)} |"
        )
    lines += [
        "",
        f"**合计：{sum(item.killed for item in outcomes)}/"
        f"{sum(item.scored for item in outcomes)} = {overall:.1%}**",
        "",
        "🔴 `incompetent` **不是**「变异之后代码跑不起来」——那是 KILLED。",
        "cosmic-ray 的 `run_tests` 只在**它自己抛异常**时才返回这个值：",
        "被测命令的 `returncode != 0`（测试失败、语法错误、收集失败）一律算",
        "KILLED，超时也是。所以能进这个桶的只有两种——**输出解码失败**、",
        "**命令根本没启动起来**。两者都是**度量故障**，不是测试不够强。",
        "",
        "实测过一次：Windows 中文区域下被 spawn 的 pytest 按 GBK 写管道，",
        "`stdout.decode('utf-8')` 抛 `UnicodeDecodeError`，**104 个本来会被",
        "杀死**的变异体被记成了 incompetent，模块分数从 100% 掉到 7.7%。",
        "判定表见 `run.py` 的 `_SUBPROCESS_ENV`。",
        "",
        "因此：**`incompetent` 非零，或某个模块计分变异体为 0，都直接判失败**"
        "（退出码非 0），不看分数。0/0 会被算成 100%，那是必须堵死的。",
        "",
        "⚠️ `标注等价物` 是落在**类型标注**范围内的变异。被测模块全部启用",
        "`from __future__ import annotations`（PEP 563），标注在运行期",
        "只是一段字符串——改动它**必然**不改变行为。排除它们是去掉噪声，",
        "不是把分数调上去；前提由 `_assert_pep563_is_active` 逐个模块核对。",
        "",
    ]

    # 🔴 先列 incompetent 的样本，再列存活变异体。
    #   顺序是有意的：incompetent 非零说明**这个分数本身可能不可信**，
    #   读到存活清单之前就该先看到它。
    _render_incompetents(lines, outcomes)

    for outcome in outcomes:
        if not outcome.equivalents:
            continue
        lines.append(f"### 登记等价物 · `{outcome.module.path}`")
        lines.append("")
        lines.append(
            "每条后面的「覆盖 N 条」是它**实际放行**的变异体数。"
            "N > 1 不一定是坏事——整族都等价时本来就该一条登记覆盖全族——"
            "但它必须与理由的论证范围相符：理由只论证了某一个取值，"
            "却在覆盖多个，那就是放行了没被论证过的东西。"
        )
        lines.append("")
        for survivor, entry in outcome.equivalents:
            covered = sum(
                1
                for other_survivor, other_entry in outcome.equivalents
                if other_entry is entry and other_survivor.line == entry.line
            )
            lines.append(f"- **{survivor.operator}** @ 第 {survivor.line} 行（覆盖 {covered} 条）")
            lines.append("  ```diff")
            lines.extend(f"  {line}" for line in _summarise(survivor.diff).splitlines())
            lines.append("  ```")
            lines.append(f"  **等价理由**：{entry.reason}")
        lines.append("")
    lines.append("## 存活变异体（逐条）")
    lines.append("")
    if not any(outcome.survived for outcome in outcomes):
        lines.append("（无）")
        lines.append("")
    for outcome in outcomes:
        if not outcome.survived:
            continue
        lines.append(f"### `{outcome.module.path}`")
        lines.append("")
        for survivor in outcome.survived:
            lines.append(f"- **{survivor.operator}** @ 第 {survivor.line} 行")
            lines.append("  ```diff")
            lines.extend(f"  {line}" for line in _summarise(survivor.diff).splitlines())
            lines.append("  ```")
        lines.append("")
    return "\n".join(lines)


#: 报告里最多列出几个 incompetent 样本。
#:
#: 样本是用来**归因**的，不是用来穷举的：同一类故障（编码、构造失败、
#: 语法错误）的 traceback 长得一模一样，列 200 条只会把报告淹掉。
#: 计数仍然在表格里，一条都不少。
_MAX_INCOMPETENT_SAMPLES = 5


def _render_incompetents(lines: list[str], outcomes: list[Outcome]) -> None:
    """把 ``INCOMPETENT`` 的样本写进报告。

    🔴 存在的理由：这个分类**曾经把一个度量故障伪装成「测试不够强」**。
    只留一个计数时，报告上是「104 个 incompetent」，看不出任何异常；
    而其中 104 个的 ``output`` 全是同一句 ``UnicodeDecodeError``。

    ⚠️ 这一节非空时，**这个分数不可信**——按 `_SUBPROCESS_ENV` 里的判定表，
    能进这个桶的只有解码失败与命令起不来，没有第三种可能。
    """
    total = sum(len(item.incompetent) for item in outcomes)
    if not total:
        return

    lines.append("## 🔴 incompetent 样本（分数不可信，先看这里）")
    lines.append("")
    lines.append(
        f"共 **{total}** 条。它们**不计分**，而 `run.py` 会因此 fail-closed。"
        "判定表：`returncode != 0` 一律 KILLED（跑不起来也是它），"
        "**只有 cosmic-ray 自己抛异常才进这个桶**——所以这里出现的东西，"
        "要么是输出解码失败，要么是被测命令根本没启动。"
    )
    lines.append("")
    for outcome in outcomes:
        if not outcome.incompetent:
            continue
        shown = outcome.incompetent[:_MAX_INCOMPETENT_SAMPLES]
        lines.append(f"### `{outcome.module.path}`（{len(outcome.incompetent)} 条）")
        lines.append("")
        if len(outcome.incompetent) > len(shown):
            lines.append(
                f"（只列前 {len(shown)} 条；同类故障的 traceback 相同，重复列出来只会淹没报告）"
            )
            lines.append("")
        for entry in shown:
            lines.append(f"- **{entry.operator}** @ 第 {entry.line} 行")
            output = entry.output.strip()
            lines.append("  ```text")
            if output:
                lines.extend(f"  {line}" for line in output.splitlines()[-12:])
            else:
                lines.append("  （cosmic-ray 没有留下输出）")
            lines.append("  ```")
        lines.append("")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
