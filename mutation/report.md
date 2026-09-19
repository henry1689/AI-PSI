# 变异测试报告（阶段 6.5 §六）

> 由 `uv run python mutation/run.py` 生成。**不要手工编辑。**

## 🔴 等价登记失配（先修这个）

有 **18** 条登记本轮**一条变异体都没匹配上**。
登记里带行号，在被登记的那一行**上面**加任何东西都会让它整体下移，
于是分数无缘无故掉下来、而报告里只看得到「多了几个存活变异体」。
行号也可能漂到**另一个**变异体上，把真变异当等价放行——
所以这一节非空时，下面的分数**不可信**。

- `experiences` · **core/NumberReplacer** @ 第 297 行（片段 `min_length= 0`）
  - 片段 `min_length= 0` 命中的实际行是 **204**、**209**、**268**、**291**、**299**、**306**、**473**、**486**、**561**、**577** —— 把 line 改成它。（命中多行说明这条登记覆盖的是整族，那是**正常的**，前提是理由里论证了整族）
- `experiences` · **core/NumberReplacer** @ 第 297 行（片段 `min_length= 2`）
  - 片段 `min_length= 2` 命中的实际行是 **204**、**209**、**268**、**291**、**299**、**306**、**473**、**486**、**561**、**577** —— 把 line 改成它。（命中多行说明这条登记覆盖的是整族，那是**正常的**，前提是理由里论证了整族）
- `experiences` · **core/NumberReplacer** @ 第 289 行（片段 `min_length= 0`）
  - 片段 `min_length= 0` 命中的实际行是 **204**、**209**、**268**、**291**、**299**、**306**、**473**、**486**、**561**、**577** —— 把 line 改成它。（命中多行说明这条登记覆盖的是整族，那是**正常的**，前提是理由里论证了整族）
- `experiences` · **core/NumberReplacer** @ 第 289 行（片段 `min_length= 2`）
  - 片段 `min_length= 2` 命中的实际行是 **204**、**209**、**268**、**291**、**299**、**306**、**473**、**486**、**561**、**577** —— 把 line 改成它。（命中多行说明这条登记覆盖的是整族，那是**正常的**，前提是理由里论证了整族）
- `experiences` · **core/ReplaceTrueWithFalse** @ 第 522 行（片段 `slots=False`）
  - 片段 `slots=False` 命中的实际行是 **584** —— 把 line 改成它。
- `experiences` · **core/ReplaceComparisonOperator_Gt_GtE** @ 第 591 行（片段 `rank >= evaluation.rank`）
  - 片段 `rank >= evaluation.rank` 命中的实际行是 **693** —— 把 line 改成它。
- `experiences` · **core/ReplaceComparisonOperator_IsNot_Lt** @ 第 632 行（片段 `evaluation < ExperienceEvaluation.UNASSESSED`）
  - 片段 `evaluation < ExperienceEvaluation.UNASSESSED` 命中的实际行是 **805** —— 把 line 改成它。
- `experiences` · **core/ReplaceComparisonOperator_IsNot_NotEq** @ 第 632 行（片段 `evaluation != ExperienceEvaluation.UNASSESSED`）
  - 片段 `evaluation != ExperienceEvaluation.UNASSESSED` 命中的实际行是 **805** —— 把 line 改成它。
- `experiences` · **core/ReplaceComparisonOperator_Is_GtE** @ 第 640 行（片段 `evaluation >= ExperienceEvaluation.UNASSESSED`）
  - 片段 `evaluation >= ExperienceEvaluation.UNASSESSED` 命中的实际行是 **813** —— 把 line 改成它。
- `experiences` · **core/ReplaceComparisonOperator_Is_Eq** @ 第 640 行（片段 `evaluation == ExperienceEvaluation.UNASSESSED`）
  - 片段 `evaluation == ExperienceEvaluation.UNASSESSED` 命中的实际行是 **813** —— 把 line 改成它。
- `experiences` · **core/ReplaceComparisonOperator_Is_Eq** @ 第 648 行（片段 `evaluator == ExperienceEvaluator.INTERNAL_METACOGNITION`）
  - 片段 `evaluator == ExperienceEvaluator.INTERNAL_METACOGNITION` 命中的实际行是 **821** —— 把 line 改成它。
- `experiences` · **core/ReplaceComparisonOperator_Gt_IsNot** @ 第 649 行（片段 `rank is not ExperienceEvaluation.SUSPECTED.rank`）
  - 片段 `rank is not ExperienceEvaluation.SUSPECTED.rank` 命中的实际行是 **822** —— 把 line 改成它。
- `experiences` · **core/ReplaceComparisonOperator_Gt_NotEq** @ 第 649 行（片段 `rank != ExperienceEvaluation.SUSPECTED.rank`）
  - 片段 `rank != ExperienceEvaluation.SUSPECTED.rank` 命中的实际行是 **822** —— 把 line 改成它。
- `pattern_detector` · **core/ReplaceUnaryOperator_USub_Invert** @ 第 262 行（片段 `(~item.weighted_count,`）
  - 片段 `(~item.weighted_count,` 命中的实际行是 **282**、**285** —— 把 line 改成它。（命中多行说明这条登记覆盖的是整族，那是**正常的**，前提是理由里论证了整族）
- `pattern_detector` · **core/ReplaceUnaryOperator_USub_Invert** @ 第 265 行（片段 `(~item.weighted_count,`）
  - 片段 `(~item.weighted_count,` 命中的实际行是 **282**、**285** —— 把 line 改成它。（命中多行说明这条登记覆盖的是整族，那是**正常的**，前提是理由里论证了整族）
- `pattern_detector` · **core/NumberReplacer** @ 第 334 行（片段 `evaluations[- 0]`）
  - 片段 `evaluations[- 0]` 命中的实际行是 **355** —— 把 line 改成它。
- `pattern_detector` · **core/ReplaceUnaryOperator_USub_Not** @ 第 334 行（片段 `evaluations[not 1]`）
  - 片段 `evaluations[not 1]` 命中的实际行是 **355** —— 把 line 改成它。
- `pattern_detector` · **core/ReplaceBinaryOperator_Mul_Div** @ 第 194 行（片段 `/,`）
  - 片段 `/,` 命中的实际行是 **199** —— 把 line 改成它。

## 逐模块分数

| 模块 | 杀死 | 计分总数 | 分数 | 存活 | incompetent | 标注等价物 | 登记等价物 |
|---|---|---|---|---|---|---|---|
| `src/ai_psi/learning/pattern_detector.py` | 102 | 109 | **93.6%** | 7 | 0 | 0 | 3 |
| `src/ai_psi/domain/experiences.py` | 162 | 189 | **85.7%** | 27 | 0 | 33 | 0 |

**合计：264/298 = 88.6%**

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
- **core/ReplaceTrueWithFalse** @ 第 172 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。同上
- **core/ReplaceTrueWithFalse** @ 第 152 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。同上

## 存活变异体（逐条）

### `src/ai_psi/learning/pattern_detector.py`

- **core/NumberReplacer** @ 第 191 行
  ```diff
  -    conflicting_attributions: int = 0
  +    conflicting_attributions: int = 1
  ```
- **core/ReplaceUnaryOperator_USub_Invert** @ 第 282 行
  ```diff
  -            key=lambda item: (-item.weighted_count, item.error_type.value, item.situation_signature)
  +            key=lambda item: (~item.weighted_count, item.error_type.value, item.situation_signature)
  ```
- **core/ReplaceUnaryOperator_USub_Not** @ 第 355 行
  ```diff
  -                f"默认权重为 {self._weighting.weight_of(evaluations[-1])}——"
  +                f"默认权重为 {self._weighting.weight_of(evaluations[not 1])}——"
  ```
- **core/NumberReplacer** @ 第 355 行
  ```diff
  -                f"默认权重为 {self._weighting.weight_of(evaluations[-1])}——"
  +                f"默认权重为 {self._weighting.weight_of(evaluations[- 0])}——"
  ```
- **core/ReplaceBinaryOperator_Mul_Div** @ 第 199 行
  ```diff
  -        *,
  +        /,
  ```
- **core/NumberReplacer** @ 第 191 行
  ```diff
  -    conflicting_attributions: int = 0
  +    conflicting_attributions: int = -1
  ```
- **core/ReplaceUnaryOperator_USub_Invert** @ 第 285 行
  ```diff
  -            key=lambda item: (-item.weighted_count, item.error_type.value, item.situation_signature)
  +            key=lambda item: (~item.weighted_count, item.error_type.value, item.situation_signature)
  ```

### `src/ai_psi/domain/experiences.py`

- **core/NumberReplacer** @ 第 291 行
  ```diff
  -        min_length=1,
  +        min_length= 0,
  ```
- **core/ReplaceComparisonOperator_Is_GtE** @ 第 750 行
  ```diff
  -    matching = [item for item in records if item.error_type is only]
  +    matching = [item for item in records if item.error_type >= only]
  ```
- **core/ReplaceComparisonOperator_Is_IsNot** @ 第 706 行
  ```diff
  -                    if item.error_type is effective_type
  +                    if item.error_type is not effective_type
  ```
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 821 行
  ```diff
  -        evaluator is ExperienceEvaluator.INTERNAL_METACOGNITION
  +        evaluator == ExperienceEvaluator.INTERNAL_METACOGNITION
  ```
- **core/NumberReplacer** @ 第 561 行
  ```diff
  -    experience_canonical_key: str = Field(min_length=1, description="被归因经验的规范标识")
  +    experience_canonical_key: str = Field(min_length= 0, description="被归因经验的规范标识")
  ```
- **core/NumberReplacer** @ 第 291 行
  ```diff
  -        min_length=1,
  +        min_length= 2,
  ```
- **core/NumberReplacer** @ 第 577 行
  ```diff
  -    classifier_version: str = Field(min_length=1, description="分类器/规则版本")
  +    classifier_version: str = Field(min_length= 0, description="分类器/规则版本")
  ```
- **core/ReplaceComparisonOperator_Is_LtE** @ 第 750 行
  ```diff
  -    matching = [item for item in records if item.error_type is only]
  +    matching = [item for item in records if item.error_type <= only]
  ```
- **core/ReplaceComparisonOperator_Gt_GtE** @ 第 693 行
  ```diff
  -            if record.evaluation.rank > evaluation.rank:
  +            if record.evaluation.rank >= evaluation.rank:
  ```
- **core/NumberReplacer** @ 第 577 行
  ```diff
  -    classifier_version: str = Field(min_length=1, description="分类器/规则版本")
  +    classifier_version: str = Field(min_length= 2, description="分类器/规则版本")
  ```
- **core/ReplaceComparisonOperator_Is_NotEq** @ 第 706 行
  ```diff
  -                    if item.error_type is effective_type
  +                    if item.error_type != effective_type
  ```
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 750 行
  ```diff
  -    matching = [item for item in records if item.error_type is only]
  +    matching = [item for item in records if item.error_type == only]
  ```
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 748 行
  ```diff
  -    if experience.error_type is only:
  +    if experience.error_type == only:
  ```
- **core/ReplaceComparisonOperator_IsNot_NotEq** @ 第 805 行
  ```diff
  -        if evaluation is not ExperienceEvaluation.UNASSESSED:
  +        if evaluation != ExperienceEvaluation.UNASSESSED:
  ```
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 813 行
  ```diff
  -    if evaluation is ExperienceEvaluation.UNASSESSED:
  +    if evaluation == ExperienceEvaluation.UNASSESSED:
  ```
- **core/NumberReplacer** @ 第 299 行
  ```diff
  -        min_length=1,
  +        min_length= 0,
  ```
- **core/ReplaceComparisonOperator_IsNot_Lt** @ 第 805 行
  ```diff
  -        if evaluation is not ExperienceEvaluation.UNASSESSED:
  +        if evaluation < ExperienceEvaluation.UNASSESSED:
  ```
- **core/ReplaceComparisonOperator_Gt_NotEq** @ 第 822 行
  ```diff
  -        and evaluation.rank > ExperienceEvaluation.SUSPECTED.rank
  +        and evaluation.rank != ExperienceEvaluation.SUSPECTED.rank
  ```
- **core/ReplaceComparisonOperator_Is_GtE** @ 第 813 行
  ```diff
  -    if evaluation is ExperienceEvaluation.UNASSESSED:
  +    if evaluation >= ExperienceEvaluation.UNASSESSED:
  ```
- **core/ReplaceComparisonOperator_Gt_NotEq** @ 第 744 行
  ```diff
  -    if len(kinds) > 1:
  +    if len(kinds) != 1:
  ```
- **core/NumberReplacer** @ 第 561 行
  ```diff
  -    experience_canonical_key: str = Field(min_length=1, description="被归因经验的规范标识")
  +    experience_canonical_key: str = Field(min_length= 2, description="被归因经验的规范标识")
  ```
- **core/ReplaceTrueWithFalse** @ 第 584 行
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 706 行
  ```diff
  -                    if item.error_type is effective_type
  +                    if item.error_type == effective_type
  ```
- **core/ReplaceFalseWithTrue** @ 第 752 行
  ```diff
  -        return only, experience.attribution_confidence, False
  +        return only, experience.attribution_confidence, True
  ```
- **core/ReplaceFalseWithTrue** @ 第 623 行
  ```diff
  -    attribution_conflict: bool = False
  +    attribution_conflict: bool = True
  ```
- **core/NumberReplacer** @ 第 299 行
  ```diff
  -        min_length=1,
  +        min_length= 2,
  ```
- **core/ReplaceComparisonOperator_Gt_IsNot** @ 第 822 行
  ```diff
  -        and evaluation.rank > ExperienceEvaluation.SUSPECTED.rank
  +        and evaluation.rank is not ExperienceEvaluation.SUSPECTED.rank
  ```
