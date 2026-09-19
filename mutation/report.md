# 变异测试报告（阶段 6.5 §六）

> 由 `uv run python mutation/run.py` 生成。**不要手工编辑。**

## 逐模块分数

| 模块 | 杀死 | 计分总数 | 分数 | 存活 | incompetent | 标注等价物 | 登记等价物 |
|---|---|---|---|---|---|---|---|
| `src/ai_psi/learning/pattern_detector.py` | 104 | 104 | **100.0%** | 0 | 0 | 0 | 8 |
| `src/ai_psi/domain/experiences.py` | 169 | 169 | **100.0%** | 0 | 0 | 33 | 20 |

**合计：273/273 = 100.0%**

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

### 登记等价物 · `src/ai_psi/learning/pattern_detector.py`

每条后面的「覆盖 N 条」是它**实际放行**的变异体数。N > 1 不一定是坏事——整族都等价时本来就该一条登记覆盖全族——但它必须与理由的论证范围相符：理由只论证了某一个取值，却在覆盖多个，那就是放行了没被论证过的东西。

- **core/ReplaceTrueWithFalse** @ 第 104 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。同族的 `frozen=True → False` 由 `TestTheResultsAreImmutable` 杀掉
- **core/ReplaceUnaryOperator_USub_Invert** @ 第 285 行（覆盖 1 条）
  ```diff
  -            key=lambda item: (-item.weighted_count, item.error_type.value, item.situation_signature)
  +            key=lambda item: (~item.weighted_count, item.error_type.value, item.situation_signature)
  ```
  **等价理由**：同上（`suppressed.sort` 用的是同一个键表达式）
- **core/ReplaceUnaryOperator_USub_Not** @ 第 355 行（覆盖 1 条）
  ```diff
  -                f"默认权重为 {self._weighting.weight_of(evaluations[-1])}——"
  +                f"默认权重为 {self._weighting.weight_of(evaluations[not 1])}——"
  ```
  **等价理由**：同上：`not 1` 是 `False`，即下标 0——同样落在「全部权重为 0」的不可观察区间里
- **core/ReplaceTrueWithFalse** @ 第 172 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。同上
- **core/NumberReplacer** @ 第 355 行（覆盖 1 条）
  ```diff
  -                f"默认权重为 {self._weighting.weight_of(evaluations[-1])}——"
  +                f"默认权重为 {self._weighting.weight_of(evaluations[- 0])}——"
  ```
  **等价理由**：这一支**只在全部参与计数的评价权重都为 0 时**才进入（判据是 `all(not counts_toward_threshold(...))`，而 `counts_toward_threshold` 就是 `weight_of(x) > 0`）。既然每个元素的权重都是 0，`weight_of(evaluations[i])` 对**任何**下标都是 0——取第一个还是最后一个不可观察。⚠️ 这句话依赖「权重非负」，而它由 `EvaluationWeighting.__post_init__` 显式拒绝负数保证
- **core/ReplaceUnaryOperator_USub_Invert** @ 第 282 行（覆盖 1 条）
  ```diff
  -            key=lambda item: (-item.weighted_count, item.error_type.value, item.situation_signature)
  +            key=lambda item: (~item.weighted_count, item.error_type.value, item.situation_signature)
  ```
  **等价理由**：`~x` 就是 `-x - 1`，是 `-x` 的**单调变换**（相差一个常数 1）。排序只关心相对次序，因此 `~weighted_count` 与 `-weighted_count` 给出完全相同的排列。⚠️ 注意它**不是**「随便什么一元算子都行」：同族的 `not` / 去掉 `-` / `+` 都是真变异，由 `TestDeterministicOrdering` 的两条新用例杀掉
- **core/ReplaceTrueWithFalse** @ 第 152 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。同上
- **core/ReplaceBinaryOperator_Mul_Div** @ 第 199 行（覆盖 1 条）
  ```diff
  -        *,
  +        /,
  ```
  **等价理由**：《`*,` → `/,` 族》

### 登记等价物 · `src/ai_psi/domain/experiences.py`

每条后面的「覆盖 N 条」是它**实际放行**的变异体数。N > 1 不一定是坏事——整族都等价时本来就该一条登记覆盖全族——但它必须与理由的论证范围相符：理由只论证了某一个取值，却在覆盖多个，那就是放行了没被论证过的东西。

- **core/NumberReplacer** @ 第 291 行（覆盖 1 条）
  ```diff
  -        min_length=1,
  +        min_length= 0,
  ```
  **等价理由**：`independence_group` **曾经**是自由字符串，那时这条是真变异；阶段 6.5 §八 给 `Experience` 加了 `_check_independence_group` （逐字核对它 == `independence_group_for(幂等键, 回合)`）之后，它成了派生值，最短也有 5 个字符（`round:` + UUID = 42，`idem:` 至少 5）。下界降到 0 没有任何输入能走到。⚠️ **这条等价的成立条件是那条校验器存在**——它由 `test_a_hand_written_independence_group_is_refused` 与`test_the_group_follows_the_round` 钉住；删掉校验器，那两条会先红，而不是让这里悄悄放行真变异
- **core/ReplaceFalseWithTrue** @ 第 752 行（覆盖 1 条）
  ```diff
  -        return only, experience.attribution_confidence, False
  +        return only, experience.attribution_confidence, True
  ```
  **等价理由**：这一行在 `if not matching:  # pragma: no cover` 里面，而那个分支**不可达**：`only` 来自 `kinds`，若它不来自经验自己（那就是上面 748 那一问已返回的情形），就必来自某条记录，于是 `matching` 非空。于是这里改成 `True`（报冲突）也不可观察。⚠️ 它与上面那条 `> 1` → `!= 1` 是**同一件事的两面**：都靠 `kinds` 的构成论证，删掉任一前提两者会一起失配
- **core/NumberReplacer** @ 第 299 行（覆盖 1 条）
  ```diff
  -        min_length=1,
  +        min_length= 0,
  ```
  **等价理由**：`canonical_key` 由 `_check_canonical_identity` 钉死为**派生值**（与 (回合, 评价对象, 种类, 抽取器版本) 逐字一致），而派生值最短也有 42 个字符。把下界从 1 降到 0，**没有任何输入**能走到那条长度检查——一致性校验先拒绝了它。⚠️ 这依赖「派生值永远不短」这个事实，而它由 `canonical_key_for` 的拼接方式保证（四段用 `|` 连接，含 36 字符的 UUID）
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 748 行（覆盖 1 条）
  ```diff
  -    if experience.error_type is only:
  +    if experience.error_type == only:
  ```
  **等价理由**：《枚举比较》：同段说明
- **core/NumberReplacer** @ 第 291 行（覆盖 1 条）
  ```diff
  -        min_length=1,
  +        min_length= 2,
  ```
  **等价理由**：同上，方向反过来：派生值最短也有 5 个字符，抬到 2 拒不掉任何东西
- **core/ReplaceComparisonOperator_Is_GtE** @ 第 813 行（覆盖 1 条）
  ```diff
  -    if evaluation is ExperienceEvaluation.UNASSESSED:
  +    if evaluation >= ExperienceEvaluation.UNASSESSED:
  ```
  **等价理由**：《StrEnum 的字典序》的另一半：除 unassessed 之外的三个值都`< "unassessed"`，因此 `x >= UNASSESSED` 只在 x 就是unassessed 时为真——与 `x is UNASSESSED` 同答案。⚠️ 侥幸等价
- **core/ReplaceComparisonOperator_Gt_NotEq** @ 第 744 行（覆盖 1 条）
  ```diff
  -    if len(kinds) > 1:
  +    if len(kinds) != 1:
  ```
  **等价理由**：紧挨着的上一问是 `if not kinds: return`——**走到这一行时 `kinds` 必非空**。非空集合上 `len(kinds) > 1` 与 `len(kinds) != 1` 是同一条判据。⚠️ 这条依赖那个空集提前返回**紧邻在上**；把它挪走或删掉，这里立刻变成真变异（空集会被误判成冲突）
- **core/ReplaceTrueWithFalse** @ 第 584 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：`ExperienceAssessment` 的 `frozen=True` **本身**就拒绝一切属性赋值（由 `test_it_cannot_be_mutated` 钉住），`slots` 只影响内存布局与 `__dict__` 是否存在，而没有任何代码读 `__dict__`。与 `invariants` @54 同族。⚠️ 同族的 `frozen=True → False` **是真变异**，已经被同一条用例杀掉
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 813 行（覆盖 1 条）
  ```diff
  -    if evaluation is ExperienceEvaluation.UNASSESSED:
  +    if evaluation == ExperienceEvaluation.UNASSESSED:
  ```
  **等价理由**：《枚举比较》：同段说明
- **core/ReplaceComparisonOperator_Gt_NotEq** @ 第 822 行（覆盖 1 条）
  ```diff
  -        and evaluation.rank > ExperienceEvaluation.SUSPECTED.rank
  +        and evaluation.rank != ExperienceEvaluation.SUSPECTED.rank
  ```
  **等价理由**：同上：rank 的四个取值互异且都在小整数缓存内，`!=` 与 `>` 同答案
- **core/ReplaceComparisonOperator_Gt_GtE** @ 第 693 行（覆盖 1 条）
  ```diff
  -            if record.evaluation.rank > evaluation.rank:
  +            if record.evaluation.rank >= evaluation.rank:
  ```
  **等价理由**：`ExperienceEvaluation` 四档的 `rank` **互异**（0/1/2/3，由 `test_the_ranks_are_distinct` 钉住）。等秩 ⟹ 它们是**同一个成员**，于是 `evaluation = record.evaluation` 是一次空操作。`>` 与 `>=` 只在等秩时分叉，而那时分叉不可观察
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 821 行（覆盖 1 条）
  ```diff
  -        evaluator is ExperienceEvaluator.INTERNAL_METACOGNITION
  +        evaluator == ExperienceEvaluator.INTERNAL_METACOGNITION
  ```
  **等价理由**：《枚举比较》：同段说明。⚠️ 这一条尤其要记住它的边界——`is` 与 `==` 的分叉点正是「来了一个非成员」，而那时 `is` 会**静默放行**自我确认（见 R59）
- **core/ReplaceComparisonOperator_Is_LtE** @ 第 750 行（覆盖 1 条）
  ```diff
  -    matching = [item for item in records if item.error_type is only]
  +    matching = [item for item in records if item.error_type <= only]
  ```
  **等价理由**：与上面那条 `Is_GtE` 同一段前提：走到这一行时 `kinds` 只有一个元素，因此**每一条记录**的 `error_type` 都就是 `only`，`only <= only` 恒为真。⚠️ 这里 `<=` 与 `is` 同答案**不靠字面量**——自己与自己比较，任何 StrEnum 都成立，比 `Is_GtE` 那条更稳
- **core/ReplaceComparisonOperator_Is_GtE** @ 第 750 行（覆盖 1 条）
  ```diff
  -    matching = [item for item in records if item.error_type is only]
  +    matching = [item for item in records if item.error_type >= only]
  ```
  **等价理由**：`_effective_attribution` 里这一行的原判据是 `item.error_type is only`，而**走到它时 `kinds` 必然只有一个元素**——多于一个时上面已经走了冲突分支提前返回。既然所有记录的 `error_type` 都等于 `only`，`is` 与 `>=`（StrEnum 按字符串比，自己 ≥ 自己为真）同答案。⚠️ 这条依赖「kinds 只有一个元素 ⟹ 全部记录同类别」，而它由冲突分支保证；`test_two_extremes_disagreeing_is_a_conflict` 钉住了那个分支本身
- **core/ReplaceComparisonOperator_Gt_IsNot** @ 第 822 行（覆盖 1 条）
  ```diff
  -        and evaluation.rank > ExperienceEvaluation.SUSPECTED.rank
  +        and evaluation.rank is not ExperienceEvaluation.SUSPECTED.rank
  ```
  **等价理由**：`_EXPERIENCE_EVALUATION_RANK` 的取值是 0/1/2/3，全部落在 CPython 的小整数缓存里，因此 `rank is not 1` 与 `rank != 1` 同答案。而 `rank == 0`（unassessed）在那之前已经被 588 那一问拦掉，到不了这里。⚠️ 同样是**侥幸等价**：门槛一旦超过 256，`is not` 立刻变成真变异
- **core/NumberReplacer** @ 第 299 行（覆盖 1 条）
  ```diff
  -        min_length=1,
  +        min_length= 2,
  ```
  **等价理由**：同上，方向反过来：把下界抬到 2 也不会拒掉任何东西——派生值同样是 42 字符起步。两半都要登记，因为「没有输入能走到这里」对**两侧**都成立
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 750 行（覆盖 1 条）
  ```diff
  -    matching = [item for item in records if item.error_type is only]
  +    matching = [item for item in records if item.error_type == only]
  ```
  **等价理由**：《枚举比较》：同段说明。与下面的 `<=` 共用同一段前提
- **core/ReplaceComparisonOperator_IsNot_Lt** @ 第 805 行（覆盖 1 条）
  ```diff
  -        if evaluation is not ExperienceEvaluation.UNASSESSED:
  +        if evaluation < ExperienceEvaluation.UNASSESSED:
  ```
  **等价理由**：StrEnum 的 `<` 比**字符串**。实测：suspected / supported / confirmed 三个值都 `< "unassessed"`，而 unassessed 不 `<` 自己——于是 `x < UNASSESSED` 与 `x is not UNASSESSED` 对全部四个成员答案相同。⚠️ **侥幸等价**，见本段的《StrEnum 的字典序》
- **core/ReplaceComparisonOperator_IsNot_NotEq** @ 第 805 行（覆盖 1 条）
  ```diff
  -        if evaluation is not ExperienceEvaluation.UNASSESSED:
  +        if evaluation != ExperienceEvaluation.UNASSESSED:
  ```
  **等价理由**：《枚举比较》：同段说明。`!=` 与 `is not` 对成员输入同答案
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 706 行（覆盖 1 条）
  ```diff
  -                    if item.error_type is effective_type
  +                    if item.error_type == effective_type
  ```
  **等价理由**：《枚举比较》。这一行还多一层：`effective_type` **可以是 `None`**（冲突或无法归因时），而 `item.error_type` 永远是成员。`成员 == None` 与 `成员 is None` 同样为假——StrEnum 的 `__eq__` 继承自 `str`，对 `None` 返回 `NotImplemented`，于是回落到同一性比较。两条分支（有类别 / 无类别）答案都相同。守行为的是 `test_the_basis_comes_from_the_records_that_agree` 与 `test_a_conflict_has_no_basis_at_all`

## 存活变异体（逐条）

（无）
