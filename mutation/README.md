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

需要**逐条写理由**。当前登记的两条都在 `write_policy.py` 第 65 行：

| 变异 | 为什么等价 |
|---|---|
| `is` → `==` | 枚举成员是单例，对成员输入两者给出相同答案 |
| `is` → `<=` | ⚠️ **侥幸等价**：`WriteDecision` 四个成员的值按字典序排列时 `approved` 恰好最前，因此 `x <= APPROVED` 与 `x is APPROVED` 对所有成员答案相同 |

第二条特别值得留下：它不是"必然等价"，是"当前值排序下恰好等价"。
改任何一个成员的字面量都会让它变成真变异——而那一刻
`test_the_flag_matches_the_decision` 会立刻变红。

---

## 6. 与其它质量闸门的关系

| 闸门 | 它回答的问题 | 变异测试补的是什么 |
|---|---|---|
| `make lint` / `make typecheck` | 代码合法吗 | 无关 |
| 覆盖率闸门（domain/cognition ≥85%） | 代码跑过吗 | **跑过不等于测过** |
| 契约测试 | 两个后端一致吗 | 一致地错也一致 |
| §七 黑盒验收 | 能力真的可达吗 | 无关 |

四者互不替代。变异测试是唯一一个**主动尝试把代码改坏**的闸门。
