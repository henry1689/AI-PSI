# 变异测试报告（阶段 6.5 §六）

> 由 `uv run python mutation/run.py` 生成。**不要手工编辑。**

## 逐模块分数

| 模块 | 杀死 | 计分总数 | 分数 | 存活 | incompetent | 标注等价物 | 登记等价物 |
|---|---|---|---|---|---|---|---|
| `src/ai_psi/learning/evaluation_weighting.py` | 37 | 37 | **100.0%** | 0 | 0 | 0 | 1 |
| `src/ai_psi/learning/pattern_detector.py` | 96 | 96 | **100.0%** | 0 | 0 | 0 | 8 |
| `src/ai_psi/learning/promotion_policy.py` | 77 | 77 | **100.0%** | 0 | 0 | 0 | 3 |
| `src/ai_psi/learning/proposal_generator.py` | 44 | 44 | **100.0%** | 0 | 0 | 44 | 3 |
| `src/ai_psi/learning/offline_evaluator.py` | 227 | 227 | **100.0%** | 0 | 0 | 22 | 8 |
| `src/ai_psi/cognition/state_machine.py` | 21 | 21 | **100.0%** | 0 | 0 | 22 | 0 |
| `src/ai_psi/reliability/invariants.py` | 134 | 134 | **100.0%** | 0 | 0 | 0 | 7 |
| `src/ai_psi/memory/write_policy.py` | 29 | 29 | **100.0%** | 0 | 0 | 11 | 2 |
| `src/ai_psi/application/proposal_gate.py` | 124 | 124 | **100.0%** | 0 | 0 | 22 | 11 |
| `src/ai_psi/domain/experiences.py` | 122 | 122 | **100.0%** | 0 | 0 | 22 | 11 |

**合计：911/911 = 100.0%**

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

### 登记等价物 · `src/ai_psi/learning/evaluation_weighting.py`

每条后面的「覆盖 N 条」是它**实际放行**的变异体数。N > 1 不一定是坏事——整族都等价时本来就该一条登记覆盖全族——但它必须与理由的论证范围相符：理由只论证了某一个取值，却在覆盖多个，那就是放行了没被论证过的东西。

- **core/ReplaceComparisonOperator_Gt_NotEq** @ 第 87 行（覆盖 1 条）
  ```diff
  -        return self.weights[evaluation] > 0
  +        return self.weights[evaluation] != 0
  ```
  **等价理由**：`EvaluationWeighting.__post_init__` 对**每一个**评价状态显式拒绝负权重（`if weight < 0: raise ValueError`），因此权重恒为自然数，`> 0` 与 `!= 0` 在所有可达输入上同答案。⚠️ 判据依赖那条构造期校验：它一旦被放宽，这条立刻变成真变异

### 登记等价物 · `src/ai_psi/learning/pattern_detector.py`

每条后面的「覆盖 N 条」是它**实际放行**的变异体数。N > 1 不一定是坏事——整族都等价时本来就该一条登记覆盖全族——但它必须与理由的论证范围相符：理由只论证了某一个取值，却在覆盖多个，那就是放行了没被论证过的东西。

- **core/ReplaceUnaryOperator_USub_Invert** @ 第 265 行（覆盖 1 条）
  ```diff
  -            key=lambda item: (-item.weighted_count, item.error_type.value, item.situation_signature)
  +            key=lambda item: (~item.weighted_count, item.error_type.value, item.situation_signature)
  ```
  **等价理由**：同上（`suppressed.sort` 用的是同一个键表达式）
- **core/ReplaceTrueWithFalse** @ 第 152 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。同上
- **core/ReplaceTrueWithFalse** @ 第 172 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。同上
- **core/ReplaceBinaryOperator_Mul_Div** @ 第 194 行（覆盖 1 条）
  ```diff
  -        *,
  +        /,
  ```
  **等价理由**：《`*,` → `/,` 族》
- **core/ReplaceUnaryOperator_USub_Not** @ 第 334 行（覆盖 1 条）
  ```diff
  -                f"默认权重为 {self._weighting.weight_of(evaluations[-1])}——"
  +                f"默认权重为 {self._weighting.weight_of(evaluations[not 1])}——"
  ```
  **等价理由**：同上：`not 1` 是 `False`，即下标 0——同样落在「全部权重为 0」的不可观察区间里
- **core/ReplaceUnaryOperator_USub_Invert** @ 第 262 行（覆盖 1 条）
  ```diff
  -            key=lambda item: (-item.weighted_count, item.error_type.value, item.situation_signature)
  +            key=lambda item: (~item.weighted_count, item.error_type.value, item.situation_signature)
  ```
  **等价理由**：`~x` 就是 `-x - 1`，是 `-x` 的**单调变换**（相差一个常数 1）。排序只关心相对次序，因此 `~weighted_count` 与 `-weighted_count` 给出完全相同的排列。⚠️ 注意它**不是**「随便什么一元算子都行」：同族的 `not` / 去掉 `-` / `+` 都是真变异，由 `TestDeterministicOrdering` 的两条新用例杀掉
- **core/NumberReplacer** @ 第 334 行（覆盖 1 条）
  ```diff
  -                f"默认权重为 {self._weighting.weight_of(evaluations[-1])}——"
  +                f"默认权重为 {self._weighting.weight_of(evaluations[- 0])}——"
  ```
  **等价理由**：这一支**只在全部参与计数的评价权重都为 0 时**才进入（判据是 `all(not counts_toward_threshold(...))`，而 `counts_toward_threshold` 就是 `weight_of(x) > 0`）。既然每个元素的权重都是 0，`weight_of(evaluations[i])` 对**任何**下标都是 0——取第一个还是最后一个不可观察。⚠️ 这句话依赖「权重非负」，而它由 `EvaluationWeighting.__post_init__` 显式拒绝负数保证
- **core/ReplaceTrueWithFalse** @ 第 104 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。同族的 `frozen=True → False` 由 `TestTheResultsAreImmutable` 杀掉

### 登记等价物 · `src/ai_psi/learning/promotion_policy.py`

每条后面的「覆盖 N 条」是它**实际放行**的变异体数。N > 1 不一定是坏事——整族都等价时本来就该一条登记覆盖全族——但它必须与理由的论证范围相符：理由只论证了某一个取值，却在覆盖多个，那就是放行了没被论证过的东西。

- **core/ReplaceTrueWithFalse** @ 第 115 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。`PromotionDecision` 的 frozen 由既有的 `TestDecisionShape` 守着
- **core/ReplaceTrueWithFalse** @ 第 87 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。同族的 `frozen=True → False` 由 `TestTheConstructionAndAccessorsAreStable::test_the_evidence_is_immutable` 杀掉
- **core/ReplaceBinaryOperator_Mul_Div** @ 第 138 行（覆盖 1 条）
  ```diff
  -        *,
  +        /,
  ```
  **等价理由**：《`*,` → `/,` 族》

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

- **core/ReplaceComparisonOperator_Eq_Is** @ 第 229 行（覆盖 1 条）
  ```diff
  -            if key == name:
  +            if key is name:
  ```
  **等价理由**：指标名全部是 `snapshot()` 里的**字面量**，而 `delta_for` 的调用方（`HIGHER_IS_BETTER_METRICS` / `LOWER_IS_BETTER_METRICS` 的成员，以及测试里直接写的同一个字面量）用的也是字面量。CPython 把形如标识符的字符串字面量 intern 到同一张表里，因此两侧**是同一个对象**。与 `invariants` @74 同族：⚠️ **实现细节上的侥幸等价**，换一个 Python 实现、或让指标名从配置里读，它立刻变成真变异
- **core/ReplaceTrueWithFalse** @ 第 386 行（覆盖 1 条）
  ```diff
  -            for base, cand in zip(baseline, candidate, strict=True)
  +            for base, cand in zip(baseline, candidate, strict=False)
  ```
  **等价理由**：`snapshot()` 恒定返回**同样七条**指标（`rounds` 为空时返回空元组，而 `compare` 在那之前就返回了），因此 `baseline` 与 `candidate` 的长度永远相等，`strict=True` 的检查**没有输入能触发**。⚠️ 这条依赖「snapshot 的条目数不随数据变化」——哪天有条件指标（例如「没有模型调用时不算这一条」）时，它立刻变成真变异，而那时这条登记会失配并报出来
- **core/ReplaceTrueWithFalse** @ 第 64 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》
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
- **core/ReplaceBinaryOperator_Mul_Div** @ 第 329 行（覆盖 1 条）
  ```diff
  -        *,
  +        /,
  ```
  **等价理由**：《`*,` → `/,` 族》

### 登记等价物 · `src/ai_psi/reliability/invariants.py`

每条后面的「覆盖 N 条」是它**实际放行**的变异体数。N > 1 不一定是坏事——整族都等价时本来就该一条登记覆盖全族——但它必须与理由的论证范围相符：理由只论证了某一个取值，却在覆盖多个，那就是放行了没被论证过的东西。

- **core/NumberReplacer** @ 第 403 行（覆盖 2 条）
  ```diff
  -    probe = _probe_proposal(1)
  +    probe = _probe_proposal( 2)
  ```
  **等价理由**：`_check_i11` 拿到这个探针**只读一个属性**：`can_become_active`。它是 `ImprovementProposal` 上的类级属性，与支撑经验条数无关；0 条、1 条、2 条的提案在该分支上行为完全相同（实测 `ImprovementProposal(supporting_experience_ids=[])` 构造成功且 `can_become_active` 仍为 False）。⚠️ 与 `_check_i10` 的同名写法不同：那里的 1 与门槛是**被 spy 用例钉住的**（`TestTheCheckProbesTheInputsItClaims`），因为 `_check_i10` 的 detail 会声称自己验了「单条」和「三条」
- **core/NumberReplacer** @ 第 198 行（覆盖 2 条）
  ```diff
  -            f"（构造 {forbidden[0]} 会失败）"
  +            f"（构造 {forbidden[ 1]} 会失败）"
  ```
  **等价理由**：`forbidden` 的四个词（confirmed / verified / established / canonical）**没有一个**能构造出 `HypothesisStatus`，这一点由 `test_i11...` 之前的 `constructible` 分支与 `TestCheckInventory` 的正向用例各自验证过。因此 `forbidden[0]`、`[1]`、`[-1]` 取到的都是**一个同样不可构造的词**，detail 里那句「构造 X 会失败」对四个取值**同为真**。被改的只有那句说明文字举的例子，而没有任何代码读这句话——它只出现在人看的报告里
- **core/NumberReplacer** @ 第 198 行（覆盖 2 条）
  ```diff
  -            f"（构造 {forbidden[0]} 会失败）"
  +            f"（构造 {forbidden[ -1]} 会失败）"
  ```
  **等价理由**：`forbidden` 的四个词（confirmed / verified / established / canonical）**没有一个**能构造出 `HypothesisStatus`，这一点由 `test_i11...` 之前的 `constructible` 分支与 `TestCheckInventory` 的正向用例各自验证过。因此 `forbidden[0]`、`[1]`、`[-1]` 取到的都是**一个同样不可构造的词**，detail 里那句「构造 X 会失败」对四个取值**同为真**。被改的只有那句说明文字举的例子，而没有任何代码读这句话——它只出现在人看的报告里
- **core/ReplaceComparisonOperator_Eq_Is** @ 第 74 行（覆盖 1 条）
  ```diff
  -        if item.invariant_id == invariant_id:
  +        if item.invariant_id is invariant_id:
  ```
  **等价理由**：`_statement_of` 的实参只有三个**字面量**（I01 / I10 / I11），而 `INVARIANTS` 里的 `invariant_id` 也是字面量。CPython 会把形如标识符的字符串字面量intern 到同一张表里，因此两个对象**是同一个**，`is` 与 `==` 对全部可达输入答案相同。⚠️ **这是一个实现细节上的侥幸等价**，与 `write_policy` 的 `<=` 那条同类：换一个 Python 实现（或改成从数据里读编号）它立刻变成真变异。之所以仍登记为等价而不是补测试，是因为能杀掉它的只有「拿拼接出来的字符串去查」那种断言——那测的是「别对字符串用 is」这条代码风格，而不是本模块对外的任何保证
- **core/ReplaceTrueWithFalse** @ 第 54 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：`frozen=True` **本身**就拒绝一切属性赋值（`FrozenInstanceError`），与 `slots` 无关——实测在一个只有 `frozen=True` 的 dataclass 上`obj.y = 2` 同样抛 `FrozenInstanceError`。`slots` 改的是内存布局与 `__dict__` 是否存在，而本仓库没有任何代码读 `__dict__`，所以这条变异在所有可达输入上行为一致。⚠️ 同族还有一条 `frozen=True → False`，**那一条是真变异**（它让检查结果可以被事后改写），由 `test_checks_are_frozen` 杀掉
- **core/NumberReplacer** @ 第 353 行（覆盖 1 条）
  ```diff
  -        supporting_experience_ids=[UUID(int=index + 1) for index in range(experience_count)],
  +        supporting_experience_ids=[UUID(int=index + 2) for index in range(experience_count)],
  ```
  **等价理由**：探针的契约是一条三合一的话：**n 个互异、非全零、且构造合法**的 UUID。`+1` 给出 1,2,3，`+2` 给出 2,3,4——两条都满足全部三项，而具体取值没有第二类观察者（`meets_escalation_threshold` 只做 `len(set(...))`），所以 `+2` 改不出任何可观察差异。⚠️ 同一行上的另外七个 NumberReplacer 变体**不是**等价：`+ 0` / `* 1` / `// 1` / `** 1` 给出 0,1,2，`<< 1` 给出 0,2,4，`^ 1` 给出 1,0,3——**三个集合都含 `UUID(int=0)`**，也就是本模块自己的固定探针标识 `_PROBE_UUID`；`/ 1` 给出浮点，`uuid.UUID` 照收而 `.hex` 会抛 TypeError。🔴 杀掉它们的**不是** count/distinct 那两条用例——`{0,1,2}`、`{0,2,4}`、`{1,0,3}` 全都互异、条数也对，那两条对它们全部通过；真正杀掉的是 `test_the_probe_ids_are_genuine_uuids` 与 `test_the_probe_ids_never_collide_with_the_fixed_probe_id`
- **core/NumberReplacer** @ 第 403 行（覆盖 2 条）
  ```diff
  -    probe = _probe_proposal(1)
  +    probe = _probe_proposal( 0)
  ```
  **等价理由**：`_check_i11` 拿到这个探针**只读一个属性**：`can_become_active`。它是 `ImprovementProposal` 上的类级属性，与支撑经验条数无关；0 条、1 条、2 条的提案在该分支上行为完全相同（实测 `ImprovementProposal(supporting_experience_ids=[])` 构造成功且 `can_become_active` 仍为 False）。⚠️ 与 `_check_i10` 的同名写法不同：那里的 1 与门槛是**被 spy 用例钉住的**（`TestTheCheckProbesTheInputsItClaims`），因为 `_check_i10` 的 detail 会声称自己验了「单条」和「三条」

### 登记等价物 · `src/ai_psi/memory/write_policy.py`

每条后面的「覆盖 N 条」是它**实际放行**的变异体数。N > 1 不一定是坏事——整族都等价时本来就该一条登记覆盖全族——但它必须与理由的论证范围相符：理由只论证了某一个取值，却在覆盖多个，那就是放行了没被论证过的东西。

- **core/ReplaceComparisonOperator_Is_LtE** @ 第 65 行（覆盖 1 条）
  ```diff
  -        return self is WriteDecision.APPROVED
  +        return self <= WriteDecision.APPROVED
  ```
  **等价理由**：同上，且 `self <= WriteDecision.APPROVED` 依赖 StrEnum 的 字典序：四个成员的值是 approved / requires_user_confirmation / requires_review / rejected，「approved」恰好排在字典序最前，因此 `x <= APPROVED` 对全部四个成员给出与 `x is APPROVED` 相同的答案。⚠️ **这是一个侥幸等价**——改任何一个成员的字面量都会让它变成真变异。之所以仍登记为等价而非补测试，是因为没有一种输入能区分它们；改值的那一刻 `test_the_flag_matches_the_decision` 会立刻变红
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 65 行（覆盖 1 条）
  ```diff
  -        return self is WriteDecision.APPROVED
  +        return self == WriteDecision.APPROVED
  ```
  **等价理由**：枚举属性 `WriteDecision.allows_write` 里 `self` 恒为一个 WriteDecision 成员。枚举成员是单例，`==` 与 `is` 对成员输入给出相同答案。对**非成员**输入两者会不同（StrEnum 的 `==` 接受裸字符串），但 `self` 不可能是非成员——它由 `WritePolicy.decide()` 返回，那个方法的每个分支都返回枚举成员

### 登记等价物 · `src/ai_psi/application/proposal_gate.py`

每条后面的「覆盖 N 条」是它**实际放行**的变异体数。N > 1 不一定是坏事——整族都等价时本来就该一条登记覆盖全族——但它必须与理由的论证范围相符：理由只论证了某一个取值，却在覆盖多个，那就是放行了没被论证过的东西。

- **core/ReplaceBinaryOperator_Mul_Div** @ 第 275 行（覆盖 1 条）
  ```diff
  -        *,
  +        /,
  ```
  **等价理由**：《`*,` → `/,` 族》
- **core/ReplaceTrueWithFalse** @ 第 99 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。同族的 `frozen=True → False` 由 `TestTheGateVerdictIsDerivedNotFilled::test_the_verdict_is_immutable` 杀掉
- **core/ReplaceAndWithOr** @ 第 157 行（覆盖 1 条）
  ```diff
  -            and self.decision is not None
  +            or self.decision is not None
  ```
  **等价理由**：`and` 比 `or` 结合得紧，因此这一改等价于 `pattern is not None or (decision is not None and ...)`。两条返回路径上 `pattern` 与 `decision` **总是同生共死**（要么都给、要么都是 None），所以「pattern 有而 decision 没有」这个能让两者分叉的状态不可达。🔴 这条等价依赖那条耦合，而它由 `TestEveryVerdictKeepsThePatternAndTheDecisionTogether` 显式钉住
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
- **core/ReplaceBinaryOperator_Mul_Div** @ 第 233 行（覆盖 1 条）
  ```diff
  -        *,
  +        /,
  ```
  **等价理由**：《`*,` → `/,` 族》
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 362 行（覆盖 1 条）
  ```diff
  -        if item.error_type is error_type and item.situation_signature == signature:
  +        if item.error_type == error_type and item.situation_signature == signature:
  ```
  **等价理由**：《枚举比较》。⚠️ 同一行上的 `or` 与 `>=` / `<=` **不是**等价，已由 `TestTheScanLookupIgnoresHalfMatchesInSuppressed` 杀掉
- **core/ReplaceTrueWithFalse** @ 第 66 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：《slots 族》。同族的 `frozen=True → False` 由 `TestTheGateEvidenceDefaults::test_it_is_immutable` 杀掉
- **core/ReplaceComparisonOperator_IsNot_NotEq** @ 第 134 行（覆盖 1 条）
  ```diff
  -        if self._token is not _GATE_TOKEN:
  +        if self._token != _GATE_TOKEN:
  ```
  **等价理由**：`_GATE_TOKEN` 是 `object()`，而 `object` 的 `__eq__` / `__ne__` **就是**同一性比较（没有子类覆写）。`_token` 的取值只有两个：那个 token 本身，或 `None`。三种组合下 `!=` 与 `is not` 答案相同。⚠️ 这条依赖「凭据是裸 object」——哪天它换成一个自定义了 `__eq__` 的类型，立刻变成真变异
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 359 行（覆盖 1 条）
  ```diff
  -        if pattern.error_type is error_type and pattern.situation_signature == signature:
  +        if pattern.error_type == error_type and pattern.situation_signature == signature:
  ```
  **等价理由**：《枚举比较》
- **core/ReplaceOrWithAnd** @ 第 207 行（覆盖 1 条）
  ```diff
  -            f"{self.situation_signature}）：{'；'.join(self.reasons) or '未给出理由'}"
  +            f"{self.situation_signature}）：{'；'.join(self.reasons) and '未给出理由'}"
  ```
  **等价理由**：`self.reasons` 在两条返回路径上**都不可能为空**：一条是 `list(suppressed) or [兜底]`，另一条来自 `PromotionPolicy.decide`（五条条件各至少追加一句）。因此 `'未给出理由'` 这个兜底目前**不可达**，`or` 与 `and` 给出同样的消息。⚠️ 这也意味着那段兜底是死代码——保留它是为了将来某条路径真的不带理由时消息仍然可读，而那时这条登记会失配并报出来

### 登记等价物 · `src/ai_psi/domain/experiences.py`

每条后面的「覆盖 N 条」是它**实际放行**的变异体数。N > 1 不一定是坏事——整族都等价时本来就该一条登记覆盖全族——但它必须与理由的论证范围相符：理由只论证了某一个取值，却在覆盖多个，那就是放行了没被论证过的东西。

- **core/ReplaceComparisonOperator_Gt_GtE** @ 第 539 行（覆盖 1 条）
  ```diff
  -            if record.evaluation.rank > evaluation.rank:
  +            if record.evaluation.rank >= evaluation.rank:
  ```
  **等价理由**：`ExperienceEvaluation` 四档的 `rank` **互异**（0/1/2/3，由 `test_the_ranks_are_distinct` 钉住）。等秩 ⟹ 它们是**同一个成员**，于是 `evaluation = record.evaluation` 是一次空操作。`>` 与 `>=` 只在等秩时分叉，而那时分叉不可观察
- **core/ReplaceComparisonOperator_Gt_NotEq** @ 第 597 行（覆盖 1 条）
  ```diff
  -        and evaluation.rank > ExperienceEvaluation.SUSPECTED.rank
  +        and evaluation.rank != ExperienceEvaluation.SUSPECTED.rank
  ```
  **等价理由**：同上：rank 的四个取值互异且都在小整数缓存内，`!=` 与 `>` 同答案
- **core/ReplaceComparisonOperator_Is_GtE** @ 第 588 行（覆盖 1 条）
  ```diff
  -    if evaluation is ExperienceEvaluation.UNASSESSED:
  +    if evaluation >= ExperienceEvaluation.UNASSESSED:
  ```
  **等价理由**：《StrEnum 的字典序》的另一半：除 unassessed 之外的三个值都`< "unassessed"`，因此 `x >= UNASSESSED` 只在 x 就是unassessed 时为真——与 `x is UNASSESSED` 同答案。⚠️ 侥幸等价
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
- **core/NumberReplacer** @ 第 287 行（覆盖 1 条）
  ```diff
  -        min_length=1,
  +        min_length= 2,
  ```
  **等价理由**：同上，方向反过来：把下界抬到 2 也不会拒掉任何东西——派生值同样是 42 字符起步。两半都要登记，因为「没有输入能走到这里」对**两侧**都成立
- **core/ReplaceTrueWithFalse** @ 第 470 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：`ExperienceAssessment` 的 `frozen=True` **本身**就拒绝一切属性赋值（由 `test_it_cannot_be_mutated` 钉住），`slots` 只影响内存布局与 `__dict__` 是否存在，而没有任何代码读 `__dict__`。与 `invariants` @54 同族。⚠️ 同族的 `frozen=True → False` **是真变异**，已经被同一条用例杀掉
- **core/ReplaceComparisonOperator_Is_Eq** @ 第 588 行（覆盖 1 条）
  ```diff
  -    if evaluation is ExperienceEvaluation.UNASSESSED:
  +    if evaluation == ExperienceEvaluation.UNASSESSED:
  ```
  **等价理由**：《枚举比较》：同段说明
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
- **core/ReplaceComparisonOperator_Gt_IsNot** @ 第 597 行（覆盖 1 条）
  ```diff
  -        and evaluation.rank > ExperienceEvaluation.SUSPECTED.rank
  +        and evaluation.rank is not ExperienceEvaluation.SUSPECTED.rank
  ```
  **等价理由**：`_EXPERIENCE_EVALUATION_RANK` 的取值是 0/1/2/3，全部落在 CPython 的小整数缓存里，因此 `rank is not 1` 与 `rank != 1` 同答案。而 `rank == 0`（unassessed）在那之前已经被 588 那一问拦掉，到不了这里。⚠️ 同样是**侥幸等价**：门槛一旦超过 256，`is not` 立刻变成真变异

## 存活变异体（逐条）

（无）
