# ADR-0024：同一业务模式至多一条活跃提案

> 状态：**已采纳**（阶段 7 第一项）
>
> 前置：[ADR-0023](0023-correction-attribution.md)（错误归因闭环）、
> [ADR-0022](0022-proposal-gate-authoritative-boundary.md)（门禁权威边界）

---

## 1. 背景：R72

阶段 6.6 的独立评审实测出一条 HIGH 缺陷（3/3 复现）：

> 间隔 < ~50ms 的两个纠正请求，会各生成一条内容完全相同的 DRAFT 提案。

机制不是疏漏，是**责任错位**。`LearningService._covered_keys()` 做的是
"**先读**已存在的提案、**再**在循环里生成"，读与写之间没有锁，
而 `improvement_proposals` 表上**没有任何与业务键对应的唯一约束**。

ADR-0023 §6 把"重试不得重复创建 Proposal"推给了提案层：

> `LearningService._covered_keys()` 让已存在的 `(error_class, signature)`
> 不再生成。触发器**不自己记"跑过了"**——那会是第二份真相来源。

这句话在**顺序**重试下成立（黑盒场景 D 钉住），在**并发**下是空的。

代价很具体：重复的提案**删不掉**（只有 evaluate→reject 一条路），
会长期占着评审队列——正是这段代码声称要避免的那件事。

---

## 2. 决定

**唯一性交给 PostgreSQL 裁决；应用层的 `_covered_keys()` 降级为快速路径。**

```sql
CREATE UNIQUE INDEX uq_improvement_proposals_active_pattern
  ON improvement_proposals (error_class, (applicability[1]))
  WHERE cardinality(applicability) > 0
    AND status NOT IN ('rejected', 'approved_for_manual_trial');
```

> **同一个业务模式 ``(error_class, applicability[0])``，
> 至多存在一条"活跃"提案。**

### 2.1 业务键为什么不是整个 `applicability`

用户明确要求：**不得在没有证明二者等价之前，直接建立
`UNIQUE(error_class, applicability)`。** 核对结果：**不等价**，因此不采用。

| 事实 | 位置 |
|---|---|
| `_covered_keys()` 的键是 `(error_class.value, applicability[0])`，且**跳过空 applicability** | `application/learning_service.py` |
| `ProposalService._require_gate_authorisation` 已强制 `applicability[0] == verdict.situation_signature` | `application/proposal_service.py` |
| 生成器**只**写单元素：`applicability=[pattern.situation_signature]` | `learning/proposal_generator.py` |
| 但**领域模型不强制单元素**：`applicability: list[str]` 无长度约束 | `domain/improvement_proposals.py` |
| **空数组确实会落库**：契约夹具 `_proposal()` 不给 applicability → 默认 `[]` | `tests/contract/base.py` |

两处分叉都真实可达：

* **空数组**：整数组唯一会认为两条 `[]` 相同而拒绝，而 `_covered_keys`
  认为这类提案**不覆盖任何东西**（`if proposal.applicability`）——方向相反，
  会把"不设适用范围"的提案从"可以有很多条"变成"至多一条"。
* **多元素数组**：`['a','b']` 与 `['a']` 在整数组索引下是**两个键**，
  在 `_covered_keys` 下是**同一个键**——索引比应用规则**更松**。

索引表达式因此写成 `(applicability[1])`，与应用层**逐字同构**。

⚠️ **PostgreSQL 的数组下标从 1 起**，所以 SQL 里的 `applicability[1]`
就是 Python 里的 `applicability[0]`。两个后端各按自己语言的约定写
（`SqlAlchemyProposalRepository` 用 `[1]`，`InMemoryProposalRepository`
与 `active_pattern_key()` 用 `[0]`），靠契约测试对齐。

### 2.2 部分谓词的第一半：排除空 applicability

空数组下标越界得到 NULL，而 PostgreSQL 的唯一索引**允许多个 NULL**。
不排除它们，就会留下一个"看起来唯一、实际对这类行毫无约束"的约束。

排除之后，**索引里根本不存在 NULL**——不是靠"NULL 互不相等"侥幸不冲突。
方向与应用层一致：`_covered_keys()` 同样跳过它们。

### 2.3 部分谓词的第二半：排除终态（**释放唯一键**）

数据库裁决的是 **"至多一条*活跃*提案"**。

`ProposalStatus.is_terminal` 恰好是 `REJECTED` 与
`APPROVED_FOR_MANUAL_TRIAL`，谓词与领域定义逐字对应，不另写一份名单。

**为什么不把终态也算进去（那会是更强的约束）？**

因为那会把一条**策略**钉死成**不变量**。R55 规定"被裁决过的模式不会再被提议"
——那是为了少打扰评审而做的取舍，ADR-0020 里明写"阶段 7 若引入重新开启
（例如驳回 N 天后、或新增 M 次发生时可再审），改的是 `_covered_keys` 的一行"。
若索引覆盖终态，那个设计变更就必须先换一次索引。

**代价如实写在这里**：`_covered_keys()` 含终态，因此系统今天仍然不会自动
重新提议（R55 不变）。但那条规则是**纯应用层的、且不并发安全**——
理论上"一次驳回恰好落在另一个运行的读→写窗口内"可以让新 DRAFT 出现。
这是**策略边界**（重新提议），不是重复活跃提案；R72 的目标仍然闭合。

### 2.4 身份键上**没有**的东西

| 候选 | 结论 | 理由 |
|---|---|---|
| tenant / owner / scope | **不加** | `ImprovementProposalRow` 与领域对象上都没有 user/tenant 列。`ExperienceReader.load()` 读全部经验、`_covered_keys()` 全局取键——现有语义就是**全局单一模式**。加一个没有生产者的维度，只会让约束与实际写入的键对不上 |
| classifier / pattern 版本 | **不加** | `classifier_version` 在**归因记录**上，不在模式上。把它并入键，等于"分类器升一版就给同一模式再发一条提案"——那正是要消灭的重复 |
| normalized applicability | **不做归一化** | 情境签名是**派生值**：`f"{depth.value}|{evidence_bucket}|h{hypothesis_count}"`，例如 `d2|no_evidence|h2`。它不是用户输入，再套一层归一化只会引入"两条不同的签名被折叠"的风险 |
| proposal status | **只作部分谓词** | 见 §2.3 |

---

## 3. 冲突的应用语义

```
运行 A：掠过 covered_keys（空）→ 门禁授权 → 生成 → INSERT → **提交成功**
运行 B：掠过 covered_keys（空）→ 门禁授权 → 生成 → INSERT → 唯一冲突
        ↓
   整个工作单元回滚（提案与事件都没写）
        ↓
   在**新的事务**里 find_active_for_pattern() 读回 A 那条
        ↓
   校验通过 → 记入 already_covered，continue（不抛、不 500、不写第二条）
   校验不过 → 原样抛出（说明冲突不是这个约束造成的，必须暴露）
```

**冲突在 `SqlAlchemyProposalRepository.add()` 的 `await session.flush()`
那一刻抛出**，不会推迟到 `commit()`：`session.add()` 只做登记，
真正执行 INSERT 的是那次显式 flush。

**捕获点在 `ProposalService.create()` 的 `async with` 之外**——
异常穿出时那个工作单元已经 `rollback() + close()`。
读回用**新开**的工作单元。🔴 旧事务已被 PostgreSQL 置为 aborted，
在它上面发任何语句都会以 `InFailedSqlTransaction` 失败。

**不使用 savepoint**：它的价值是"让调用方在同一个事务里继续干活"，
而失败方在该事务里**再无别的事可做**——提案不能落、事件不能写。
整体回滚 + 新事务读回是更小的机制。

**不使用 `INSERT ... ON CONFLICT DO NOTHING`**：目标索引是**部分表达式索引**，
推断需要同时给出表达式与 `index_where`；而且它要绕过 `proposal_to_row`
改用 Core insert，丢掉的正是仓储层唯一的映射函数。

### 3.1 一次 Learning Run 处理多个 Pattern 时

**不存在"整个 Run 的 UoW"。** `review()` 自身不开事务；每个模式的
`create()` 是**自己的事务**，且**已经独立提交**。因此某个模式冲突时：
前面已经生成的提案不受影响，冲突的那个进 `already_covered`，
循环继续处理后面的模式。**不需要补偿逻辑，也不存在"部分失败"这种中间态。**

### 3.2 对外形状**零变化**

API 的 `already_covered` 是 **int**（`api/schemas.py`）。
失败方的表现与"顺序重试时被快速路径拦下"**逐字相同**——
两个学习运行得到稳定、可审计、不可区分的结果，
且不需要为这条罕见路径新增接口面。

不新增事件类型：失败方**什么都没写**，与它从未运行过在事件流上等价。

---

## 4. 两个后端的分工

| | PostgreSQL | 内存 |
|---|---|---|
| 裁决手段 | 部分唯一索引（原子） | 提交时在锁内复核 |
| 冲突在哪个调用抛出 | `add()` 的 `flush()` | 正常路径在 `add()`；两个事务交错时在 `commit()` |
| 复核依据 | 索引本身 | **提交后的最终状态**（已提交 ∪ 本事务暂存） |

内存侧必须看**最终状态**，两种情形会给出不同答案：

* **旧提案在本事务里转终态、同时创建同键新提案**：只看已提交数据会把
  那条已经不在活跃集里的旧提案算成占用者，**误报冲突**；
* **本事务暂存了两条相同的新提案**：只看已提交数据看不到它们，**漏报**。

⚠️ **这条差异是"在哪一层原子化"，不是语义差异**：
两种情形都在共享契约里有断言，**实测两个后端同答案**
（PostgreSQL 侧 `save()` 的 UPDATE 立即执行、`add()` 的 INSERT 在 flush 时送出，
顺序天然正确）。真正只在内存侧单独断言的，是"两个事务交错提交"那一半——
那里 PG 的冲突在 `add()` 那一刻就报出来了，两边抛出的调用点天然不同。

---

## 5. 迁移：**发现重复就报错，不自动清理**

第一次建这个索引时，库里可能已经存在同键的多条活跃提案——正是 R72 那个
竞态留下的产物。迁移的处理是：**先诊断，报错并列出冲突，然后中止。**

```
存在同键的多条活跃提案，唯一索引未能建立（阶段 7 · R72）。
共 1 组冲突：
  - (evidence_error, d2|no_evidence|h2) × 2
      - e0f08d49-…
      - eae266a2-…
迁移**不会自动清理**：提案是审计记录，删掉哪一条是人的决定。
```

**为什么不自动清理**：提案是**给人评审的审计对象**，
"这一条曾经存在过"本身是信息。自动删掉一条，等于让一次真实的系统缺陷
从记录里消失。治理这条数据的决定权在用户，不在迁移脚本。

**为什么仍要"可读"**：直接让 `CREATE UNIQUE INDEX` 失败的话，运维看到的
只有 PostgreSQL 的一句 `could not create unique index`——既不知道冲突键、
也不知道涉及哪些提案。而 `RuntimeError` 里那段报文是**逐条**列出来的。

**降级 = 重新打开 R72。** `downgrade()` 只是 `DROP INDEX`，
没有任何测试会在降级后的库里自动变红——除非有人主动去跑
`tests/integration/test_proposal_pattern_concurrency.py`。
因此降级应当被当作"确认要放弃这条保证"，而不是一次无副作用的回退。

---

## 6. 验证

| 证据 | 结果 |
|---|---|
| 契约测试（两后端同断言） | 27 项，含"同键第二条活跃提案被拒""终态释放唯一键""空 applicability 不参与唯一性""`find_active_for_pattern` 三态" |
| 并发黑盒（真实 HTTP × 真实 PostgreSQL × barrier × 20 轮） | 20/20 |
| 迁移（空库 / 有数据 / 有重复 / downgrade） | 7/7 |
| **反向验证：DROP INDEX** | **红 → 恢复 → 绿**（见下） |

### 🔴 反向验证：这条保证到底由谁承重

只改迁移文件不算数。实际做的是：在测试库上 `DROP INDEX` →
`pg_indexes` 复查它**确实不在** → 跑并发用例。

结果：用例**变红**，报文是

```
AssertionError: 两个运行合计创建了 2 条提案：
  ['726dfc76-…', '826748e8-…']
```

跑完之后库里留着一组 `(evidence_error, d2|no_evidence|h2) × 2`——
**竞态被真实复现**。随后 `pg_indexes` 确认索引恢复，同一用例变绿。

> 这次跑红顺带证明了 §5 那段诊断的必要性：恢复索引时真的撞上了
> `UniqueViolation: could not create unique index`。迁移里的诊断不是假想防御。

⚠️ **反向验证用 `DROP INDEX`，不用 `downgrade`。**
`tests/integration/conftest.py` 的 `_test_database` 是 **session 级**夹具，
每次 pytest 启动都会对测试库跑一次 `upgrade head`——`downgrade` 制造的
"没有索引"会被它**原地撤销**，用例照常变绿。那是**假验证**。
（`DROP INDEX` 之后 `alembic_version` 仍停在 head，`upgrade head` 是 no-op。）

⚠️ **选择器必须写死 node id。** pytest 给整数参数生成的 id 是 `[0]`
而不是 `iteration0`；用 `-k iteration0` 会**一个都不选中**并以退出码 5
结束——而 5 看起来和"红了"一样，验证会变成一次**空跑**。
实测踩过一次。

### 为什么 20 轮不靠运气

每一轮都有 barrier 把两个学习运行按在 `_covered_keys` 之后的同一个点上，
因此每一轮都是**确定性地**进入竞态。20 轮的作用是覆盖"轮与轮之间
是否互相污染"，而不是"多试几次总会有一次撞上"。

每轮还必须从**空库**开始：情境签名是 `f"{depth}|{evidence_bucket}|h{n}"`，
而 HTTP 侧改不动这三个分量（请求体没有 evidence 字段、深度由路由器选档、
假设数由 mock 决定），所以"换个新回合"**不等于**"换个新签名"——
20 轮会得到同一个 `d2|no_evidence|h2`，第二轮起 `_covered_keys` 直接命中，
用例会"通过"但什么都没验。

---

## 7. 影响与残余

* `_covered_keys()` **一行未改**（除文档）：它继续做廉价预筛，
  但**不再是并发防线**。它的文档已写明这一点。
* **R72 关闭**：数据库是最终裁决者。
* **R55 不变**：被驳回过的模式仍不会被自动重新提议（纯应用层策略）。
* 新增残余 **R76**：降级会静默重新打开 R72（见 §5）。
* 不改门槛、不改归因规则、不扩展自动批准、提案仍只能生成 DRAFT。
