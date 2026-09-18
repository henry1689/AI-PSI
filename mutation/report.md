# 变异测试报告（阶段 6.5 §六）

> 由 `uv run python mutation/run.py` 生成。**不要手工编辑。**

## 逐模块分数

| 模块 | 杀死 | 计分总数 | 分数 | 存活 | incompetent | 标注等价物 | 登记等价物 |
|---|---|---|---|---|---|---|---|
| `src/ai_psi/reliability/invariants.py` | 134 | 134 | **100.0%** | 0 | 0 | 0 | 7 |

**合计：134/134 = 100.0%**

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

### 登记等价物 · `src/ai_psi/reliability/invariants.py`

每条后面的「覆盖 N 条」是它**实际放行**的变异体数。N > 1 不一定是坏事——整族都等价时本来就该一条登记覆盖全族——但它必须与理由的论证范围相符：理由只论证了某一个取值，却在覆盖多个，那就是放行了没被论证过的东西。

- **core/NumberReplacer** @ 第 198 行（覆盖 2 条）
  ```diff
  -            f"（构造 {forbidden[0]} 会失败）"
  +            f"（构造 {forbidden[ -1]} 会失败）"
  ```
  **等价理由**：`forbidden` 的四个词（confirmed / verified / established / canonical）**没有一个**能构造出 `HypothesisStatus`，这一点由 `test_i11...` 之前的 `constructible` 分支与 `TestCheckInventory` 的正向用例各自验证过。因此 `forbidden[0]`、`[1]`、`[-1]` 取到的都是**一个同样不可构造的词**，detail 里那句「构造 X 会失败」对四个取值**同为真**。被改的只有那句说明文字举的例子，而没有任何代码读这句话——它只出现在人看的报告里
- **core/NumberReplacer** @ 第 403 行（覆盖 2 条）
  ```diff
  -    probe = _probe_proposal(1)
  +    probe = _probe_proposal( 2)
  ```
  **等价理由**：`_check_i11` 拿到这个探针**只读一个属性**：`can_become_active`。它是 `ImprovementProposal` 上的类级属性，与支撑经验条数无关；0 条、1 条、2 条的提案在该分支上行为完全相同（实测 `ImprovementProposal(supporting_experience_ids=[])` 构造成功且 `can_become_active` 仍为 False）。⚠️ 与 `_check_i10` 的同名写法不同：那里的 1 与门槛是**被 spy 用例钉住的**（`TestTheCheckProbesTheInputsItClaims`），因为 `_check_i10` 的 detail 会声称自己验了「单条」和「三条」
- **core/ReplaceComparisonOperator_Eq_Is** @ 第 74 行（覆盖 1 条）
  ```diff
  -        if item.invariant_id == invariant_id:
  +        if item.invariant_id is invariant_id:
  ```
  **等价理由**：`_statement_of` 的实参只有三个**字面量**（I01 / I10 / I11），而 `INVARIANTS` 里的 `invariant_id` 也是字面量。CPython 会把形如标识符的字符串字面量intern 到同一张表里，因此两个对象**是同一个**，`is` 与 `==` 对全部可达输入答案相同。⚠️ **这是一个实现细节上的侥幸等价**，与 `write_policy` 的 `<=` 那条同类：换一个 Python 实现（或改成从数据里读编号）它立刻变成真变异。之所以仍登记为等价而不是补测试，是因为能杀掉它的只有「拿拼接出来的字符串去查」那种断言——那测的是「别对字符串用 is」这条代码风格，而不是本模块对外的任何保证
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
- **core/ReplaceTrueWithFalse** @ 第 54 行（覆盖 1 条）
  ```diff
  -@dataclass(frozen=True, slots=True)
  +@dataclass(frozen=True, slots=False)
  ```
  **等价理由**：`frozen=True` **本身**就拒绝一切属性赋值（`FrozenInstanceError`），与 `slots` 无关——实测在一个只有 `frozen=True` 的 dataclass 上`obj.y = 2` 同样抛 `FrozenInstanceError`。`slots` 改的是内存布局与 `__dict__` 是否存在，而本仓库没有任何代码读 `__dict__`，所以这条变异在所有可达输入上行为一致。⚠️ 同族还有一条 `frozen=True → False`，**那一条是真变异**（它让检查结果可以被事后改写），由 `test_checks_are_frozen` 杀掉
- **core/NumberReplacer** @ 第 198 行（覆盖 2 条）
  ```diff
  -            f"（构造 {forbidden[0]} 会失败）"
  +            f"（构造 {forbidden[ 1]} 会失败）"
  ```
  **等价理由**：`forbidden` 的四个词（confirmed / verified / established / canonical）**没有一个**能构造出 `HypothesisStatus`，这一点由 `test_i11...` 之前的 `constructible` 分支与 `TestCheckInventory` 的正向用例各自验证过。因此 `forbidden[0]`、`[1]`、`[-1]` 取到的都是**一个同样不可构造的词**，detail 里那句「构造 X 会失败」对四个取值**同为真**。被改的只有那句说明文字举的例子，而没有任何代码读这句话——它只出现在人看的报告里

## 存活变异体（逐条）

（无）
