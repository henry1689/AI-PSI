# 变异测试报告（阶段 6.5 §六）

> 由 `uv run python mutation/run.py` 生成。**不要手工编辑。**

## 逐模块分数

| 模块 | 杀死 | 计分总数 | 分数 | 存活 | incompetent | 标注等价物 | 登记等价物 |
|---|---|---|---|---|---|---|---|
| `src/ai_psi/learning/pattern_detector.py` | 96 | 96 | **100.0%** | 0 | 0 | 0 | 8 |
| `src/ai_psi/learning/proposal_generator.py` | 44 | 44 | **100.0%** | 0 | 0 | 44 | 3 |
| `src/ai_psi/learning/offline_evaluator.py` | 227 | 227 | **100.0%** | 0 | 0 | 22 | 8 |
| `src/ai_psi/application/proposal_gate.py` | 121 | 124 | **97.6%** | 3 | 0 | 22 | 11 |
| `src/ai_psi/domain/experiences.py` | 121 | 122 | **99.2%** | 1 | 0 | 22 | 11 |

**合计：609/613 = 99.3%**

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

- **core/ReplaceTrueWithFalse** @ 第 152 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。同上
- **core/ReplaceUnaryOperator_USub_Invert** @ 第 262 行（覆盖 1 条）
  ```diff
  -            key=lambda item: (-item.weighted_count, item.error_type.value, item.situation_signature)
  +            key=lambda item: (~item.weighted_count, item.error_type.value, item.situation_signature)
  ```
  **等价理由**：`~x` 就是 `-x - 1`，是 `-x` 的**单调变换**（相差一个常数 1）。排序只关心相对次序，因此 `~weighted_count` 与 `-weighted_count` 给出完全相同的排列。⚠️ 注意它**不是**「随便什么一元算子都行」：同族的 `not` / 去掉 `-` / `+` 都是真变异，由 `TestDeterministicOrdering` 的两条新用例杀掉
- **core/ReplaceTrueWithFalse** @ 第 172 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。同上
- **core/ReplaceUnaryOperator_USub_Not** @ 第 334 行（覆盖 1 条）
  ```diff
  -                f"默认权重为 {self._weighting.weight_of(evaluations[-1])}——"
  +                f"默认权重为 {self._weighting.weight_of(evaluations[not 1])}——"
  ```
  **等价理由**：同上：`not 1` 是 `False`，即下标 0——同样落在「全部权重为 0」的不可观察区间里
- **core/ReplaceUnaryOperator_USub_Invert** @ 第 265 行（覆盖 1 条）
  ```diff
  -            key=lambda item: (-item.weighted_count, item.error_type.value, item.situation_signature)
  +            key=lambda item: (~item.weighted_count, item.error_type.value, item.situation_signature)
  ```
  **等价理由**：同上（`suppressed.sort` 用的是同一个键表达式）
- **core/ReplaceBinaryOperator_Mul_Div** @ 第 194 行（覆盖 1 条）
  ```diff
  -        *,
  +        /,
  ```
  **等价理由**：《`*,` → `/,` 族》
- **core/ReplaceTrueWithFalse** @ 第 104 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。同族的 `frozen=True → False` 由 `TestTheResultsAreImmutable` 杀掉
- **core/NumberReplacer** @ 第 334 行（覆盖 1 条）
  ```diff
  -                f"默认权重为 {self._weighting.weight_of(evaluations[-1])}——"
  +                f"默认权重为 {self._weighting.weight_of(evaluations[- 0])}——"
  ```
  **等价理由**：这一支**只在全部参与计数的评价权重都为 0 时**才进入（判据是 `all(not counts_toward_threshold(...))`，而 `counts_toward_threshold` 就是 `weight_of(x) > 0`）。既然每个元素的权重都是 0，`weight_of(evaluations[i])` 对**任何**下标都是 0——取第一个还是最后一个不可观察。⚠️ 这句话依赖「权重非负」，而它由 `EvaluationWeighting.__post_init__` 显式拒绝负数保证

### 登记等价物 · `src/ai_psi/learning/proposal_generator.py`

每条后面的「覆盖 N 条」是它**实际放行**的变异体数。N > 1 不一定是坏事——整族都等价时本来就该一条登记覆盖全族——但它必须与理由的论证范围相符：理由只论证了某一个取值，却在覆盖多个，那就是放行了没被论证过的东西。

- **core/ReplaceBinaryOperator_Mul_Div** @ 第 81 行（覆盖 1 条）
  ```diff
  -        *,
  +        /,
  ```
  **等价理由**：《`*,` → `/,` 族》
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 198 行（覆盖 1 条）
  ```diff
  -        if error_type is ErrorType.UNKNOWN_ERROR:
  +        if error_type == ErrorType.UNKNOWN_ERROR:
  ```
  **等价理由**：《枚举比较》。⚠️ 同行的 `>=` **不是**等价（实测 `value_substitution` 与 `user_model_error` 在字典序上都 `>= "unknown_error"`），已由 `test_only_unknown_error_gets_the_investigation_text` 杀掉
- **core/ReplaceBinaryOperator_Mul_Div** @ 第 187 行（覆盖 1 条）
  ```diff
  -        *,
  +        /,
  ```
  **等价理由**：《`*,` → `/,` 族》

### 登记等价物 · `src/ai_psi/learning/offline_evaluator.py`

每条后面的「覆盖 N 条」是它**实际放行**的变异体数。N > 1 不一定是坏事——整族都等价时本来就该一条登记覆盖全族——但它必须与理由的论证范围相符：理由只论证了某一个取值，却在覆盖多个，那就是放行了没被论证过的东西。

- **core/ReplaceTrueWithFalse** @ 第 386 行（覆盖 1 条）
  ```diff
  -            for base, cand in zip(baseline, candidate, strict=True)
  +            for base, cand in zip(baseline, candidate, strict=False)
  ```
  **等价理由**：`snapshot()` 恒定返回**同样七条**指标（`rounds` 为空时返回空元组，而 `compare` 在那之前就返回了），因此 `baseline` 与 `candidate` 的长度永远相等，`strict=True` 的检查**没有输入能触发**。⚠️ 这条依赖「snapshot 的条目数不随数据变化」——哪天有条件指标（例如「没有模型调用时不算这一条」）时，它立刻变成真变异，而那时这条登记会失配并报出来
- **core/ReplaceTrueWithFalse** @ 第 202 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。同族的 `frozen=True → False` 由 `TestTheValueObjectsAreImmutable` 杀掉
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 259 行（覆盖 1 条）
  ```diff
  -        completed = sum(1 for item in rounds if item.state is RoundState.COMPLETED)
  +        completed = sum(1 for item in rounds if item.state == RoundState.COMPLETED)
  ```
  **等价理由**：《枚举比较》。⚠️ 同行的 `<=` **不是**等价（`"analyzing"` 与 `"cancelled"` 都排在 `"completed"` 前面），已由 `TestOnlyCompletedRoundsCountAsCompleted` 杀掉
- **core/ReplaceTrueWithFalse** @ 第 64 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》
- **core/ReplaceBinaryOperator_Mul_Div** @ 第 329 行（覆盖 1 条）
  ```diff
  -        *,
  +        /,
  ```
  **等价理由**：《`*,` → `/,` 族》
- **core/ReplaceTrueWithFalse** @ 第 43 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。同族的 `frozen=True → False` 由 `TestTheValueObjectsAreImmutable` 杀掉
- **core/ReplaceTrueWithFalse** @ 第 82 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。同族的 `frozen=True → False` 由 `TestTheValueObjectsAreImmutable` 杀掉
- **core/ReplaceComparisonOperator_Eq_Is** @ 第 229 行（覆盖 1 条）
  ```diff
  -            if key == name:
  +            if key is name:
  ```
  **等价理由**：指标名全部是 `snapshot()` 里的**字面量**，而 `delta_for` 的调用方（`HIGHER_IS_BETTER_METRICS` / `LOWER_IS_BETTER_METRICS` 的成员，以及测试里直接写的同一个字面量）用的也是字面量。CPython 把形如标识符的字符串字面量 intern 到同一张表里，因此两侧**是同一个对象**。与 `invariants` @74 同族：⚠️ **实现细节上的侥幸等价**，换一个 Python 实现、或让指标名从配置里读，它立刻变成真变异

### 登记等价物 · `src/ai_psi/application/proposal_gate.py`

每条后面的「覆盖 N 条」是它**实际放行**的变异体数。N > 1 不一定是坏事——整族都等价时本来就该一条登记覆盖全族——但它必须与理由的论证范围相符：理由只论证了某一个取值，却在覆盖多个，那就是放行了没被论证过的东西。

- **core/ReplaceFalseWithTrue** @ 第 126 行（覆盖 1 条）
  ```diff
  -    _token: object = field(default=None, repr=False, compare=False)
  +    _token: object = field(default=None, repr=False, compare=True)
  ```
  **等价理由**：所有**真**结论携带的都是同一个 `_GATE_TOKEN` 对象（同一性相同），而手工构造的结论根本构造不出来（`__post_init__` 会抛）。于是把 `_token` 纳入相等性比较，`==` 的结果一个字都不变。⚠️ 同行的 `repr=True` **不是**等价——它会把凭据印进日志与断言输出，已由 `test_the_token_stays_out_of_the_repr` 杀掉
- **core/ReplaceBinaryOperator_Mul_Div** @ 第 349 行（覆盖 1 条）
  ```diff
  -    *,
  +    /,
  ```
  **等价理由**：《`*,` → `/,` 族》
- **core/ReplaceTrueWithFalse** @ 第 66 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。同族的 `frozen=True → False` 由 `TestTheGateEvidenceDefaults::test_it_is_immutable` 杀掉
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 359 行（覆盖 1 条）
  ```diff
  -        if pattern.error_type is error_type and pattern.situation_signature == signature:
  +        if pattern.error_type == error_type and pattern.situation_signature == signature:
  ```
  **等价理由**：《枚举比较》
- **core/ReplaceAndWithOr** @ 第 157 行（覆盖 1 条）
  ```diff
  -            and self.decision is not None
  +            or self.decision is not None
  ```
  **等价理由**：`and` 比 `or` 结合得紧，因此这一改等价于 `pattern is not None or (decision is not None and ...)`。两条返回路径上 `pattern` 与 `decision` **总是同生共死**（要么都给、要么都是 None），所以「pattern 有而 decision 没有」这个能让两者分叉的状态不可达。🔴 这条等价依赖那条耦合，而它由 `TestEveryVerdictKeepsThePatternAndTheDecisionTogether` 显式钉住
- **core/ReplaceComparisonOperator_IsNot_NotEq** @ 第 134 行（覆盖 1 条）
  ```diff
  -        if self._token is not _GATE_TOKEN:
  +        if self._token != _GATE_TOKEN:
  ```
  **等价理由**：`_GATE_TOKEN` 是 `object()`，而 `object` 的 `__eq__` / `__ne__` **就是**同一性比较（没有子类覆写）。`_token` 的取值只有两个：那个 token 本身，或 `None`。三种组合下 `!=` 与 `is not` 答案相同。⚠️ 这条依赖「凭据是裸 object」——哪天它换成一个自定义了 `__eq__` 的类型，立刻变成真变异
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 362 行（覆盖 1 条）
  ```diff
  -        if item.error_type is error_type and item.situation_signature == signature:
  +        if item.error_type == error_type and item.situation_signature == signature:
  ```
  **等价理由**：《枚举比较》。⚠️ 同一行上的 `or` 与 `>=` / `<=` **不是**等价，已由 `TestTheScanLookupIgnoresHalfMatchesInSuppressed` 杀掉
- **core/ReplaceBinaryOperator_Mul_Div** @ 第 275 行（覆盖 1 条）
  ```diff
  -        *,
  +        /,
  ```
  **等价理由**：《`*,` → `/,` 族》
- **core/ReplaceBinaryOperator_Mul_Div** @ 第 233 行（覆盖 1 条）
  ```diff
  -        *,
  +        /,
  ```
  **等价理由**：《`*,` → `/,` 族》
- **core/ReplaceOrWithAnd** @ 第 207 行（覆盖 1 条）
  ```diff
  -            f"{self.situation_signature}）：{'；'.join(self.reasons) or '未给出理由'}"
  +            f"{self.situation_signature}）：{'；'.join(self.reasons) and '未给出理由'}"
  ```
  **等价理由**：`self.reasons` 在两条返回路径上**都不可能为空**：一条是 `list(suppressed) or [兜底]`，另一条来自 `PromotionPolicy.decide`（五条条件各至少追加一句）。因此 `'未给出理由'` 这个兜底目前**不可达**，`or` 与 `and` 给出同样的消息。⚠️ 这也意味着那段兜底是死代码——保留它是为了将来某条路径真的不带理由时消息仍然可读，而那时这条登记会失配并报出来
- **core/ReplaceTrueWithFalse** @ 第 99 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。同族的 `frozen=True → False` 由 `TestTheGateVerdictIsDerivedNotFilled::test_the_verdict_is_immutable` 杀掉

### 登记等价物 · `src/ai_psi/domain/experiences.py`

每条后面的「覆盖 N 条」是它**实际放行**的变异体数。N > 1 不一定是坏事——整族都等价时本来就该一条登记覆盖全族——但它必须与理由的论证范围相符：理由只论证了某一个取值，却在覆盖多个，那就是放行了没被论证过的东西。

- **core/NumberReplacer** @ 第 287 行（覆盖 1 条）
  ```diff
  -        min_length=1,
  +        min_length= 2,
  ```
  **等价理由**：同上，方向反过来：把下界抬到 2 也不会拒掉任何东西——派生值同样是 42 字符起步。两半都要登记，因为「没有输入能走到这里」对**两侧**都成立
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
  +        min_length= 0,
  ```
  **等价理由**：`canonical_key` 由 `_check_canonical_identity` 钉死为**派生值**（与 (回合, 评价对象, 种类, 抽取器版本) 逐字一致），而派生值最短也有 42 个字符。把下界从 1 降到 0，**没有任何输入**能走到那条长度检查——一致性校验先拒绝了它。⚠️ 这依赖「派生值永远不短」这个事实，而它由 `canonical_key_for` 的拼接方式保证（四段用 `|` 连接，含 36 字符的 UUID）
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
- **core/ReplaceComparisonOperator_IsNot_NotEq** @ 第 580 行（覆盖 1 条）
  ```diff
  -        if evaluation is not ExperienceEvaluation.UNASSESSED:
  +        if evaluation != ExperienceEvaluation.UNASSESSED:
  ```
  **等价理由**：《枚举比较》：同段说明。`!=` 与 `is not` 对成员输入同答案
- **core/ReplaceComparisonOperator_Gt_GtE** @ 第 539 行（覆盖 1 条）
  ```diff
  -            if record.evaluation.rank > evaluation.rank:
  +            if record.evaluation.rank >= evaluation.rank:
  ```
  **等价理由**：`ExperienceEvaluation` 四档的 `rank` **互异**（0/1/2/3，由 `test_the_ranks_are_distinct` 钉住）。等秩 ⟹ 它们是**同一个成员**，于是 `evaluation = record.evaluation` 是一次空操作。`>` 与 `>=` 只在等秩时分叉，而那时分叉不可观察
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 596 行（覆盖 1 条）
  ```diff
  -        evaluator is ExperienceEvaluator.INTERNAL_METACOGNITION
  +        evaluator == ExperienceEvaluator.INTERNAL_METACOGNITION
  ```
  **等价理由**：《枚举比较》：同段说明。⚠️ 这一条尤其要记住它的边界——`is` 与 `==` 的分叉点正是「来了一个非成员」，而那时 `is` 会**静默放行**自我确认（见 R59）
- **core/ReplaceComparisonOperator_Is_GtE** @ 第 588 行（覆盖 1 条）
  ```diff
  -    if evaluation is ExperienceEvaluation.UNASSESSED:
  +    if evaluation >= ExperienceEvaluation.UNASSESSED:
  ```
  **等价理由**：《StrEnum 的字典序》的另一半：除 unassessed 之外的三个值都`< "unassessed"`，因此 `x >= UNASSESSED` 只在 x 就是unassessed 时为真——与 `x is UNASSESSED` 同答案。⚠️ 侥幸等价
- **core/ReplaceComparisonOperator_Gt_IsNot** @ 第 597 行（覆盖 1 条）
  ```diff
  -        and evaluation.rank > ExperienceEvaluation.SUSPECTED.rank
  +        and evaluation.rank is not ExperienceEvaluation.SUSPECTED.rank
  ```
  **等价理由**：`_EXPERIENCE_EVALUATION_RANK` 的取值是 0/1/2/3，全部落在 CPython 的小整数缓存里，因此 `rank is not 1` 与 `rank != 1` 同答案。而 `rank == 0`（unassessed）在那之前已经被 588 那一问拦掉，到不了这里。⚠️ 同样是**侥幸等价**：门槛一旦超过 256，`is not` 立刻变成真变异

## 存活变异体（逐条）

### `src/ai_psi/application/proposal_gate.py`

- **core/ReplaceComparisonOperator_Eq_GtE** @ 第 362 行
  ```diff
  -        if item.error_type is error_type and item.situation_signature == signature:
  +        if item.error_type is error_type and item.situation_signature >= signature:
  ```
- **core/ReplaceComparisonOperator_Is_LtE** @ 第 362 行
  ```diff
  -        if item.error_type is error_type and item.situation_signature == signature:
  +        if item.error_type <= error_type and item.situation_signature == signature:
  ```
- **core/NumberReplacer** @ 第 248 行
  ```diff
  -        if threshold < 2:
  +        if threshold < 1:
  ```

### `src/ai_psi/domain/experiences.py`

- **core/ReplaceComparisonOperator_NotEq_Gt** @ 第 374 行
  ```diff
  -        if self.canonical_key != expected:
  +        if self.canonical_key > expected:
  ```
