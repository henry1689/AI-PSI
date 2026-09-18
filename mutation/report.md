# 变异测试报告（阶段 6.5 §六）

> 由 `uv run python mutation/run.py` 生成。**不要手工编辑。**

## 逐模块分数

| 模块 | 杀死 | 计分总数 | 分数 | 存活 | incompetent | 标注等价物 | 登记等价物 |
|---|---|---|---|---|---|---|---|
| `src/ai_psi/domain/experiences.py` | 122 | 122 | **100.0%** | 0 | 0 | 22 | 11 |

**合计：122/122 = 100.0%**

🔴 `incompetent` **不是**「变异之后代码跑不起来」——那是 KILLED。
cosmic-ray 的 `run_tests` 只在**它自己抛异常**时才返回这个值：
被测命令的 `returncode != 0`（测试失败、语法错误、收集失败）一律算
KILLED，超时也是。所以能进这个桶的只有两种——**输出解码失败**、
**命令根本没启动起来**。两者都是**度量故障**，不是测试不够强。

实测过一次：Windows 中文区域下被 spawn 的 pytest 按 GBK 写管道，
`stdout.decode('utf-8')` 抛 `UnicodeDecodeError`，**104 个本来会被
杀死**的变异体被记成了 incompetent，模块分数从 100% 掉到 7.7%。
判定表见 `run.py` 的 `_SUBPROCESS_ENV`。

因此：**`incompetent` 非零，或某个模块计分变异体为 0，都直接判失败**（退出码非 0），不看分数。0/0 会被算成 100%，那是必须堵死的。

⚠️ `标注等价物` 是落在**类型标注**范围内的变异。被测模块全部启用
`from __future__ import annotations`（PEP 563），标注在运行期
只是一段字符串——改动它**必然**不改变行为。排除它们是去掉噪声，
不是把分数调上去；前提由 `_assert_pep563_is_active` 逐个模块核对。

### 登记等价物 · `src/ai_psi/domain/experiences.py`

每条后面的「覆盖 N 条」是它**实际放行**的变异体数。N > 1 不一定是坏事——整族都等价时本来就该一条登记覆盖全族——但它必须与理由的论证范围相符：理由只论证了某一个取值，却在覆盖多个，那就是放行了没被论证过的东西。

- **core/ReplaceComparisonOperator_Gt_NotEq** @ 第 597 行（覆盖 1 条）
  ```diff
  -        and evaluation.rank > ExperienceEvaluation.SUSPECTED.rank
  +        and evaluation.rank != ExperienceEvaluation.SUSPECTED.rank
  ```
  **等价理由**：同上：rank 的四个取值互异且都在小整数缓存内，`!=` 与 `>` 同答案
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 588 行（覆盖 1 条）
  ```diff
  -    if evaluation is ExperienceEvaluation.UNASSESSED:
  +    if evaluation == ExperienceEvaluation.UNASSESSED:
  ```
  **等价理由**：《枚举比较》：同段说明
- **core/NumberReplacer** @ 第 287 行（覆盖 1 条）
  ```diff
  -        min_length=1,
  +        min_length= 2,
  ```
  **等价理由**：同上，方向反过来：把下界抬到 2 也不会拒掉任何东西——派生值同样是 42 字符起步。两半都要登记，因为「没有输入能走到这里」对**两侧**都成立
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 596 行（覆盖 1 条）
  ```diff
  -        evaluator is ExperienceEvaluator.INTERNAL_METACOGNITION
  +        evaluator == ExperienceEvaluator.INTERNAL_METACOGNITION
  ```
  **等价理由**：《枚举比较》：同段说明。⚠️ 这一条尤其要记住它的边界——`is` 与 `==` 的分叉点正是「来了一个非成员」，而那时 `is` 会**静默放行**自我确认（见 R59）
- **core/NumberReplacer** @ 第 287 行（覆盖 1 条）
  ```diff
  -        min_length=1,
  +        min_length= 0,
  ```
  **等价理由**：`canonical_key` 由 `_check_canonical_identity` 钉死为**派生值**（与 (回合, 评价对象, 种类, 抽取器版本) 逐字一致），而派生值最短也有 42 个字符。把下界从 1 降到 0，**没有任何输入**能走到那条长度检查——一致性校验先拒绝了它。⚠️ 这依赖「派生值永远不短」这个事实，而它由 `canonical_key_for` 的拼接方式保证（四段用 `|` 连接，含 36 字符的 UUID）
- **core/ReplaceComparisonOperator_Gt_GtE** @ 第 539 行（覆盖 1 条）
  ```diff
  -            if record.evaluation.rank > evaluation.rank:
  +            if record.evaluation.rank >= evaluation.rank:
  ```
  **等价理由**：`ExperienceEvaluation` 四档的 `rank` **互异**（0/1/2/3，由 `test_the_ranks_are_distinct` 钉住）。等秩 ⟹ 它们是**同一个成员**，于是 `evaluation = record.evaluation` 是一次空操作。`>` 与 `>=` 只在等秩时分叉，而那时分叉不可观察
- **core/ReplaceComparisonOperator_Gt_IsNot** @ 第 597 行（覆盖 1 条）
  ```diff
  -        and evaluation.rank > ExperienceEvaluation.SUSPECTED.rank
  +        and evaluation.rank is not ExperienceEvaluation.SUSPECTED.rank
  ```
  **等价理由**：`_EXPERIENCE_EVALUATION_RANK` 的取值是 0/1/2/3，全部落在 CPython 的小整数缓存里，因此 `rank is not 1` 与 `rank != 1` 同答案。而 `rank == 0`（unassessed）在那之前已经被 588 那一问拦掉，到不了这里。⚠️ 同样是**侥幸等价**：门槛一旦超过 256，`is not` 立刻变成真变异
- **core/ReplaceTrueWithFalse** @ 第 470 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：`ExperienceAssessment` 的 `frozen=True` **本身**就拒绝一切属性赋值（由 `test_it_cannot_be_mutated` 钉住），`slots` 只影响内存布局与 `__dict__` 是否存在，而没有任何代码读 `__dict__`。与 `invariants` @54 同族。⚠️ 同族的 `frozen=True → False` **是真变异**，已经被同一条用例杀掉
- **core/ReplaceComparisonOperator_IsNot_Lt** @ 第 580 行（覆盖 1 条）
  ```diff
  -        if evaluation is not ExperienceEvaluation.UNASSESSED:
  +        if evaluation < ExperienceEvaluation.UNASSESSED:
  ```
  **等价理由**：StrEnum 的 `<` 比**字符串**。实测：suspected / supported / confirmed 三个值都 `< "unassessed"`，而 unassessed 不 `<` 自己——于是 `x < UNASSESSED` 与 `x is not UNASSESSED` 对全部四个成员答案相同。⚠️ **侥幸等价**，见本段的《StrEnum 的字典序》
- **core/ReplaceComparisonOperator_Is_GtE** @ 第 588 行（覆盖 1 条）
  ```diff
  -    if evaluation is ExperienceEvaluation.UNASSESSED:
  +    if evaluation >= ExperienceEvaluation.UNASSESSED:
  ```
  **等价理由**：《StrEnum 的字典序》的另一半：除 unassessed 之外的三个值都`< "unassessed"`，因此 `x >= UNASSESSED` 只在 x 就是unassessed 时为真——与 `x is UNASSESSED` 同答案。⚠️ 侥幸等价
- **core/ReplaceComparisonOperator_IsNot_NotEq** @ 第 580 行（覆盖 1 条）
  ```diff
  -        if evaluation is not ExperienceEvaluation.UNASSESSED:
  +        if evaluation != ExperienceEvaluation.UNASSESSED:
  ```
  **等价理由**：《枚举比较》：同段说明。`!=` 与 `is not` 对成员输入同答案

## 存活变异体（逐条）

（无）
