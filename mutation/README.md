# 变异测试（阶段 6.5 §六）

> 工具：**cosmic-ray 8.7**（`uv sync --all-groups` 会装上）。
> 运行：`uv run python mutation/run.py`；只跑一个模块：`uv run python mutation/run.py pattern_detector`。
> 报告：每次运行覆盖 `mutation/report.md`。

---

## 1. 为什么需要它

覆盖率回答的是"这行代码**跑过**没有"，而变异测试回答的是
"这行代码**改成别的**会不会被发现"。两者差得很远：

阶段 6.5 在 `evaluation_weighting.py` 上跑了第一次，38 个变异体里
**7 个存活**——其中三个是把 `return self.weights[evaluation] > 0`
改成 `< 0`、`!= 0`、`> 1`。那意味着当时**没有任何一条测试**
断言过一个有权重的状态"算数"：一个恒返回 `False` 的实现
与真实现给出完全一样的测试结果，而恒 `False` 意味着
**系统永远不生成提案**，且不报任何错。

这正是 §六 要防的东西。

---

## 2. 怎么跑

```bash
uv sync --all-groups              # 装上 cosmic-ray
uv run python mutation/run.py     # 全部模块（约 20–40 分钟）
```

⚠️ **它会就地改写被测源码。** cosmic-ray 的工作方式是把变异写进文件、
跑测试、再还原。运行器在 `finally` 里**无条件**把源码写回运行前的内容——
这不是防御性编程：实测有一次超时把 `*` 换成了 `%,`，
整个包当场 import 不了，而 `run.py` 自己也在同一个仓库里。

**运行期间不要跑测试、不要改被测模块。** 变异还在文件里时，
任何一次 `pytest` 的结果都不代表真实代码。

---

## 2.1 🔴 报送分数之前，先确认这个分数是可信的

这套工具**曾经报出过一个完全失真的分数**，而且失真方向是最坏的那个。
两件事必须在读分数之前做掉：

### （1）`incompetent` 必须是 0

`INCOMPETENT` 的判定条件（逐字读 `cosmic_ray/testing.py` 的 `run_tests` 得来）：

| 被测命令 | 结果 |
|---|---|
| `returncode != 0` | **KILLED**（测试失败、语法错误、收集失败都走这条） |
| `returncode == 0` | SURVIVED |
| 超时 | KILLED |
| **`run_tests` 自己抛异常** | INCOMPETENT |

也就是说 **「变异之后代码跑不起来」根本进不了这个桶**——它走
`returncode != 0`，是 KILLED。能进来的只有两种：**输出解码失败**，
和**命令根本没启动起来**（`FileNotFoundError` / `shlex` 解析失败）。
两者都是**度量故障**，不是「测试不够强」。

> ⚠️ 早先的版本把这里写成「跑不起来的变异体也算 incompetent，可以接受」。
> **那是错的**：照那张表办事的人会接受一个坏分数。

实测（`invariants` 模块，修复前）：130 个变异体里 **104 个 incompetent**、
只报 **2 个 killed**，分数 **7.7%**。成因是 cosmic-ray 用 **UTF-8** 解码
被测命令的输出，而 Windows 中文区域下被 spawn 的 pytest 默认按 **GBK**
写管道——`UnicodeDecodeError` 被 `run_tests` 的兜底 `except` 接住。
而中文 traceback 只在测试失败时才打印，所以被吞掉的**恰好是本来会被杀死
的那些**。补上 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`（写死在
`_SUBPROCESS_ENV` 里）之后 incompetent 归零，同一份测试的真实分数是 **100%**。

**所以：`incompetent` 非零，或任何模块的计分变异体为 0，`run.py` 直接以
非 0 退出，不看分数。** 0/0 会被算成 100%——那是必须堵死的：
被测命令整个起不来时，屏幕上会是一个漂亮的 `100.0%` 和绿灯。

### （2）等价登记有没有失配

`EQUIVALENTS` 里带**行号**。在被登记的那一行**上面**加任何东西
（注释、空行、一个新分支）都会让它整体下移，这条登记从此谁也匹配不到——
症状是**分数无缘无故掉下来**，而报告上只看得到「多了几个存活变异体」。

实测过两次，第二次就是引入这个机制的那个提交自己：

* 给 `_check_i10` 补了 8 行注释 → 两条登记（353 / 303）双双失配，
  分数从 95.3% 掉到 93.7%；
* 往 `__all__` 里插了一行 → 另外四条（196 / 364 / 314 / 72）整体下移
  一位，全部失配。

两次的症状都一样：分数无缘无故掉下来，而报告上只是多了几个看不出
所以然的存活变异体。

运行器每次跑完会反过来核对，失配时**打印到 stderr、写进报告第一节、
并让退出码非 0**。这一节非空时，下面的分数**不可信**。

⚠️ **它只覆盖正向失效**（登记没匹配上任何变异体）。反方向——行号漂到
**另一个**变异体所在的行——它看不出来。匹配是（模块, 算子, 行号, 片段）
四元组，所以行号漂移通常会被算子或片段挡住；但**不含被改值的片段挡不住**
（例如 `forbidden[` 落在同行任何 NumberReplacer 上都命中）。真的发生了
用一次 `git log -p` 就能查清，因此这里不假装能自动发现它。

**跑完必须真跑一次再提交。** 上面第二次失效正是「手算行号」的结果——
作者按注释行数推了 +11，漏掉了 `__all__` 那一行。

---

## 3. 覆盖了什么，**没**覆盖什么

### 覆盖（§六.2 点名的模块）

| 模块 | 为什么选它 |
|---|---|
| `learning/evaluation_weighting.py` | 门槛权重的唯一来源 |
| `learning/pattern_detector.py` | 计数单位与门槛的落点 |
| `learning/promotion_policy.py` | §11.3 的五条触发条件 |
| `learning/proposal_generator.py` | 落库前的最后一道自洽检查 |
| `learning/offline_evaluator.py` | 退化判定的方向 |
| `cognition/state_machine.py` | 不变量 17 |
| `reliability/invariants.py` | 结构性保证的运行期自检 |
| `memory/write_policy.py` | 记忆写入白名单 |
| `application/proposal_gate.py` | §二.13–15 的权威复核 |
| `domain/experiences.py` | 经验的身份与评价语义 |

### 🔴 **未覆盖（残余风险，不是遗漏）**

| 模块 | 为什么没纳入 |
|---|---|
| `learning/experience_builder.py` | 它的测试挂着完整流水线，单次执行远超可接受范围 |
| `learning/error_classifier.py` | 同上 |
| `application/feedback_service.py` | 需要真实回合与记忆服务 |
| `infrastructure/in_memory/*`、`infrastructure/db/*` | 需要真实 PostgreSQL；契约测试会 TRUNCATE 表 |
| `application/proposal_service.py` | 依赖学习链路造数据，单次执行约 20 秒 |

**判据是"聚焦测试能不能在约 10 秒内跑完"**——变异测试的成本是
`变异体数 × 单次测试耗时`，全套 1751 个用例跑一遍是 105 秒，
几百个变异体就是十几个小时。

这几项的替代覆盖是**契约测试 + 黑盒验收**（`tests/integration/`），
它们不测"某个变异会不会被发现"，但它们测的是**两个后端给出同一个结果**——
那恰恰是这些模块最容易出问题的地方（R5、R13）。

---

## 4. §六.3 点名要验证的变异

| 要求 | 落在哪 |
|---|---|
| `>=3` → `>=2` | `pattern_detector`、`promotion_policy` 的 `NumberReplacer` |
| 删除回合去重 | `pattern_detector._distinct_occurrences` 的 `ZeroIterationForLoop` |
| `and` → `or` | 各模块的 `ReplaceAndWithOr` |
| 翻转 higher/lower-is-better | `offline_evaluator` 的 `HIGHER_IS_BETTER_METRICS` 分支 |
| 忽略 `round_state` | `error_classifier._from_failure` 与 `experience_builder` |
| 接受 `ACTIVE`/`ENABLED` | `invariants._check_i11`、`improvement_proposals.assert_status_is_a_member` |
| 绕过版本检查 | 仓储层；**未纳入**（见上表），由契约测试的 `test_stale_version_raises_instead_of_overwriting` 覆盖 |
| 删除事务回滚 | 工作单元；**未纳入**，由契约测试的 `test_exception_discards_everything` 覆盖 |
| 将 `UNASSESSED` 计入门槛 | `evaluation_weighting` 的权重表 |
| 最终回答置信度高于 `Judgment` | `constitution.assert_response_not_stronger_than_judgment` |
| 无新证据时继续循环 | `metacognition` 的反刍规则；**未纳入**（不在 §六.2 的模块清单里），由 `tests/unit/test_metacognition_rules.py` 覆盖 |

`run.py` 打印的 `operator_name` 就是变异算子名，可以在报告里逐条对回这张表。

---

## 5. 存活变异的处置纪律

§六.5 要求**逐条记录并解释**，不得只报总分。`mutation/report.md`
自动列出每一个存活变异体的 diff；处置只有三种：

1. **补测试** —— 它是真实的缺口。补完之后重跑，确认它被杀掉。
2. **登记为等价变异** —— 它与原实现在**所有可达输入上**行为一致。
   登记在 `run.py` 的 `EQUIVALENTS` 表里，**必须写理由**。
3. **接受** —— 它测的是确实无关紧要的东西，而补测试的收益低于噪声。
   接受意味着承认这一行没有测试保护，而那是**已知的**。

**不允许**为了把分数推上去而写"只对变异体有效"的断言，
也不允许往 `EQUIVALENTS` 里塞写不出理由的条目。
分辨方法很直接：**每一条等价理由都必须能被独立复核**——
"因为构造时已拒绝负权重，所以 `>0` 与 `!=0` 在所有可达输入上等价"
是可复核的；"看起来差不多"不是。

### 两类被**排除**的变异

它们不计分，且理由各不相同——分开写是因为混在一起会掩盖差别。

#### 5.1 落在类型标注里的变异（自动排除，`annotation_filtered`）

cosmic-ray 的 `ReplaceBinaryOperator_BitOr_*` 一族在 `X | None`
这类标注上一次产出十几个变体。被测模块**全部**启用了
`from __future__ import annotations`（PEP 563），标注在运行期
只是一段字符串，改动它**必然**不改变行为。

⚠️ **前提先被验证再被依赖。** `_assert_pep563_is_active` 会在过滤前
逐个模块核对那一行 import 存在；缺了它，标注会在运行期求值，
改动就**可能**改变行为，那时过滤就是拿排除当调分。

⚠️ **按位置过滤，不按算子名过滤。** 同一个 `BitOr` 算子也会命中
真正的位运算（`a | b`），按名字一刀切会把真变异一起砍掉。
过滤依据是 AST 里每个标注节点的源码范围。

#### 5.2 登记等价物（人工判断，`EQUIVALENTS`）

需要**逐条写理由**，且理由必须能被独立复核。

🔴 **匹配用的是四元组 + 片段，不是（模块, 算子, 行号）。**

同一行上常常有一族变异体。`UUID(int=index + 1)` 会同时产出 `index + 0`、
`index + 2`、`index * 1`、`index // 1`、`index << 1`、`index ^ 1`、`index / 1`。
只按三元组匹配的话，为 `+ 2` 登记一条等价，**其余六条跟着不算分了**——
而其中六条里有五条会产出 `UUID(int=0)`（正是本模块自己的固定探针标识），
`/ 1` 更是产出一个 `.hex` 都读不出来的坏 UUID。它们**真的能被测出来**。

所以 `Equivalent` 多了一个 `mutation` 字段：变异后那一行里**独有**的片段。
登记的语义是「**这一条**变异在所有可达输入上不可观察」，
不是「这一行不必再查」。

当前登记的条目：

| 模块 | 变异 | 覆盖 | 为什么等价 |
|---|---|---|---|
| `write_policy` @65 | `is` → `==` | 1 条 | 枚举成员是单例，对成员输入两者给出相同答案 |
| `write_policy` @65 | `is` → `<=` | 1 条 | ⚠️ **侥幸等价**：`WriteDecision` 四个成员的值按字典序排列时 `approved` 恰好最前，因此 `x <= APPROVED` 与 `x is APPROVED` 对所有成员答案相同 |
| `invariants` @197 | `forbidden[0]` → `[1]` / `[-1]` | 2 条 | 四个候选词**没有一个**能构造出 `HypothesisStatus`，所以那句说明文字对四个取值同为真；没有任何代码读这句话 |
| `invariants` @365 | `_probe_proposal(1)` → `(0)` / `(2)` | 2 条 | `_check_i11` 只用这个探针读 `can_become_active`——它是类级属性，与支撑经验条数无关 |
| `invariants` @315 | `index + 1` → `index + 2` | 1 条 | 探针的契约是「n 个互异、非全零、构造合法的 UUID」，两个取值都满足。⚠️ 同行的另外七条**不是**等价——它们会产出 `UUID(int=0)`（即本模块自己的 `_PROBE_UUID`）或浮点 UUID |
| `invariants` @73 | `==` → `is` | 1 条 | CPython 会 intern 形如标识符的字符串字面量，实参与 `INVARIANTS` 里的是同一个对象。⚠️ **实现细节上的侥幸等价**，换实现即失效 |
| `invariants` @52 | `slots=True` → `False` | 1 条 | `frozen=True` **本身**就拒绝一切属性赋值，与 `slots` 无关（实测：只有 `frozen=True` 的 dataclass 上 `obj.y = 2` 同样抛 `FrozenInstanceError`）。`slots` 只影响内存布局与 `__dict__` 是否存在，而没有任何代码读 `__dict__`。⚠️ 同族的 `frozen=True → False` **是真变异**，由 `test_checks_are_frozen` 杀掉 |

报告里每条登记后面会写「覆盖 N 条」——**N 必须与理由的论证范围相符**。
理由只论证了某一个取值却在覆盖多个，就是放行了没被论证过的东西。

两条「侥幸等价」值得单独留意：它们不是"必然等价"，是"当前取值/当前实现下
恰好等价"。改 `WriteDecision` 任何一个成员的字面量、或换一个 Python 实现，
它们立刻变成真变异——而前者发生时 `test_the_flag_matches_the_decision`
会立刻变红。

#### 5.3 `INCOMPETENT` 也要逐条解释

§六.5 的"逐条记录"不只针对存活者。`INCOMPETENT` 不计分，但**不能只报个数**——
上面 2.1 节那 104 条就是靠一个计数藏住的。报告里现在会为它们单列一节，
给出算子、行号与 worker 输出的末尾若干行（同类的只列前 5 条，避免淹没报告）。

---

## 6. 与其它质量闸门的关系

| 闸门 | 它回答的问题 | 变异测试补的是什么 |
|---|---|---|
| `make lint` / `make typecheck` | 代码合法吗 | 无关 |
| 覆盖率闸门（domain/cognition ≥85%） | 代码跑过吗 | **跑过不等于测过** |
| 契约测试 | 两个后端一致吗 | 一致地错也一致 |
| §七 黑盒验收 | 能力真的可达吗 | 无关 |

四者互不替代。变异测试是唯一一个**主动尝试把代码改坏**的闸门。
