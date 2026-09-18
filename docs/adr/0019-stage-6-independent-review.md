# ADR-0019：阶段 6 的独立评审与修正

- **状态**：已接受
- **日期**：2026-09-18
- **相关**：ADR-0005（策略不得自动生效）、ADR-0018（反馈、经验与改进提案）

## 背景

阶段 6 交付后，代码作者与测试全部由同一个执行者完成——
按项目的协作规则，这属于"运动员自审"。因此补做了一次**独立评审**：
三个互不通气的子 Agent 各自只读、不改代码，分别从
"学习链路与不变量"、"反馈与提案服务"、"对抗性验证"三个视角审查
`git diff 7ca13ef..HEAD`。

**本 ADR 记录评审发现的缺陷、确认后的修正，以及三条被推翻的表述。**

> ⚠️ 评审本身也可能报错。下面每一条都经过代码复核或实跑复现才采纳；
> 复核后判定为"文档说过头而非代码有缺陷"的，记在 §3 而不是 §2。

---

## 1. 最重要的一条：验收条件在跑起来的系统里不成立

**评审发现（两位独立复现）**：`learning/` 的六个模块在 `src/` 里
**零调用者**。运行时唯一写 `experience.created` 的 `_close_round`
手写事件负载，把 `error_type` 硬编码成 `None`、
`attribution_confidence` 硬编码成 `"very_low"`。

后果比"没接上"更严重：`PatternDetector` 会过滤掉所有不可归因的经验，
因此**即便手工把运行时产出的经验喂进去，也永远发现不了模式**。
"三次同类错误可生成 Proposal"这条验收条件，
此前只在测试里成立，在真实运行数据上不可达。

**修正**：`_close_round` 改走 `ExperienceBuilder`，判据来自元认知反思
（`Reflection`）与判断（`Judgment`）的结构字段。

新增 `tests/scenarios/test_learning_chain.py`：**只用真实回合产出的事件**
跑完整链路——三个回合 → 三个 `experience.created` → 还原 `Experience`
→ 模式发现 → 门槛裁决 → 生成 `DRAFT` 提案。中间不手工构造任何一条经验。

同一文件还钉住了反方向：两次真实回合**不构成**模式；判不了的经验
跑多少次都凑不出模式。

---

## 2. 确认并修正的缺陷

### 2.1 门槛的计量单位错了（阻断级）

`PatternDetector` 按 `Experience.id` 去重，而 `Experience.id` 是
每次 `ExperienceBuilder.build()` 新生成的 `uuid4`——
**同一个回合的同一个错误构建三次就是三条"独立经验"**，
一次错误足以凑满三次的门槛。评审实跑复现了 `allowed=True` 与提案落库。

修正：去重键改为 `(cognitive_round_id, judgment_id)`。

门槛的语义是"**这个错误在不同的回合里发生过三次**"，
不是"我们手上有三个经验对象"。

> ⚠️ 初版的测试 `test_duplicate_ids_do_not_inflate_the_count` 传的是
> **同一个对象三次**——它验证的是"同一 id 不重复计数"，
> 恰好错过了真实会发生的重复。两条用例看着像，实际差着一个 `uuid4()`。

### 2.2 `regressed()` 会把真实退化报成"未退化"

`OfflineEvaluator.regressed()` 只把三个"越高越好"的指标算作退化信号，
而 `unsupported_certainty_rate` 与 `rumination_rate` 是**方向明确的**
负向指标，被排除在外。

评审实测：基线两回合无问题、候选两回合这两个指标各 +1.0 →
`regressed()` 返回 `False`，进而 `_check_regression` 走
"离线评测未暴露稳定退化"分支。

这正好是"未评估 ≠ 不成立"要防的**反方向**：不是把"没查"当成"没问题"，
而是把**查出来的问题**说成没问题。

修正：新增 `HIGHER_IS_BETTER_METRICS` / `LOWER_IS_BETTER_METRICS`
两个方向集合，两个方向都判。方向有争议的指标（调用成本、token、延迟）
**仍然不判**——它们上升可能是"用更多算力换更好的结论"。
新增用例把"哪些指标被排除"变成一个可见的决定。

### 2.3 `first_round_id` / `last_round_id` 与文档不符

`ErrorPattern` 的这两个字段写着"最早与最晚的一次，用于判断
'这个问题还活着吗'"，实现却按 `sorted(unique, key=str)` 取首尾——
排序键是经验 id（uuid4，随机）。下游据此判断会取到错误的回合。

修正：按 `created_at` 排序（`id` 作并列时的确定键）。
`experience_ids` 仍按字典序排列——它要可复现，不承载时间语义。

### 2.4 生成侧的门槛"第二道保险"只是一句转发

`ProposalGenerator.generate` 只检查 `decision.allowed`。
手工构造一个 `PromotionDecision(allowed=True)` 加上**一条经验**的模式，
就能生成一条可落库的提案——单次经验推广为全局策略。

修正：`generate` 会重新核对"裁决声称命中的条件，证据撑不撑得起"：

* 没有任何触发条件的裁决 → 不产出；
* `pattern is None`（如"离线评测暴露稳定退化"这类不来自模式的触发）
  → 不产出（提案的核心是它引用的证据，没有证据的提案不该占用评审时间）;
* 声称命中"同类错误重复出现"却不足门槛 → **抛错**。这不是运行时状态，
  而是被构造出来的裁决。

### 2.5 不变量 11 的自检能被一个"名字无辜"的新成员骗过

`AUTO_PROMOTION_FORBIDDEN_VALUES` 是**四个词的名单**
（`active`/`applied`/`promoted`/`live`），而类型层与兜底层共用它。
评审往 `ProposalStatus` 里加一个 `ENABLED = "enabled"`（语义就是"已生效"）——
两层同时通过，自检照绿，`detail` 里还在宣称
"7 个提案状态中无一可表示已生效"。

**修正：主检查改成白名单。** `EXPECTED_PROPOSAL_STATUSES` 钉死五个成员，
多一个或少一个都报红。它不认识"已生效"这个词，**它只认识"集合变了"**。

同时删掉了 `_check_i11` 里与它等价的第二段检查
（`present` 与 `constructible` 是同一个谓词，是同一份证据的第二次采集）。

### 2.6 `model_construct` 能造出 `status='active'` 的提案并进内存仓储

`ImprovementProposal.model_construct(status="active")` 会跳过全部校验，
构造出 `status` 是**裸字符串**的对象。类型注解拦不住它，
内存仓储会把它原样存下并读回；SQL 实现会以一个
`AttributeError: 'str' object has no attribute 'value'` 崩溃——
**两种都不是"有意拦截"**。

修正：新增 `assert_status_is_a_member`，在两个仓储的
`add` / `save` 入口调用（对象变成持久化数据的那一刻）。
契约测试在两个实现上共同断言。

### 2.7 自检崩溃会让健康端点返回 500

`_check_i10` 只捕获 `ValueError`。评审把守卫改成抛
`ConstitutionViolationError`（比 `ValueError` 更贴切）之后，
异常穿出 `failing_checks()` → `invariant_dimension()` →
`/health/cognitive` 返回 **500**，而不是把"地基坏了"报成 `degraded`。

修正：新增 `_guarded`——**任何一项检查自己崩掉，都算这项检查未通过**。
"检查跑不起来"等于"这条保证没有被验证"，必须以不健康的形式报出来。

同时补强 `_check_i10` 的探针：初版只捕获"抛了 ValueError"，
任何一条无关的 ValueError 都能骗过它。现在三问缺一不可——
拒绝 `threshold=1`、单条不达标、`threshold` 条达标。

### 2.8 空白输入造成 500（两处）

* 反馈正文 `"   "` + `allow_memory_update=true` → `Memory.content` 的
  去空白校验失败 → pydantic `ValidationError`（不是领域异常）→ **500**；
  而且反馈事件**已经提交**，客户端拿到的 500 响应里连 `audit_event_id`
  都没有——"话记下了"这件事对调用方不可见。
  根因是 `WritePolicy` 对一个**根本构造不出 `Memory`** 的内容说了"批准"。
* 驳回理由 `"   "` → 服务层抛**裸 `ValueError`** → 不在 HTTP 状态映射表里
  → **500 + 一整条堆栈**。

修正：三处同时收紧——`WritePolicy` 先拒空白（策略放行的东西下游必须能构造出
对象）；请求 schema 去空白并逐条校验 `evidence`；服务层改用新的
`InvalidRequestError`（映射 422），不再拿 `ValueError` 表示"输入不对"。

### 2.9 请求 schema 接受的长度超过数据库列宽

`approved_by` / `rejected_by` / `actor_id` 只写了 `min_length=1`，
而事件表的 `actor_id` 是 `varchar(128)`。同一个请求
**内存后端 200、PostgreSQL 后端 500**——两个后端对同一个 HTTP 契约
给出不同结果，而契约测试只看仓储层，抓不到这条。

修正：三个字段加 `max_length=128`（由 `_ACTOR_ID_MAX` 常量与列宽对齐）。

### 2.10 `UNAVAILABLE_METRICS` 自己漏了两项

这份清单的文档说它"让人一眼看到哪些没算，而不是从'报告里没有这一项'
去推断"——因此"既没算也没列"是最坏的情况。评审逐项比对后发现它漏了
§11.4 的「其他场景退化程度」与 §16.1 的「按深度分组的延迟/调用数」。

修正：补齐三条，并新增用例断言 §11.4 的八项**每一项**要么被算出来、
要么在清单里。

### 2.11 自检从不在启动路径上被调用

`assert_structural_invariants` 的文档写着"适合放在进程启动路径上：
地基被改坏时进程不该起来"，而它在 `src/` 里**只有健康端点**
调了兄弟函数 `failing_checks()`。实际效果是"某次请求时才被发现"。

修正：`build_container`（组合根、也正是进程启动路径）开头调用它。

---

## 3. 被推翻的表述（文档说过头，代码没错）

### 3.1 "字段级拒绝自由文本"说过头了

ADR-0018 §1 写的是"没有任何一栏能装下用户消息/模型输出/反馈正文"。
**这是错的。** `applicable_conditions`、`counterexamples`、
`situation_signature`、`failure_stage`、`fix_direction` 都是无约束的
`str`，调用方往里填用户原话，它会一路进 `Experience`、
进 `ErrorPattern`、最终列进提案的 `applicability` 并持久化。

准确的说法是：**没有任何一栏是"为"承载用户原文而设的；
系统自己填的字段全是结构信号。** 本层没有机制拦住调用方往里填原文。

修正：改文档，并在两处把残余风险**显式钉成用例**
（`test_the_field_set_test_is_not_enough_on_its_own`、
`test_situation_signature_is_the_only_other_free_text_channel`）。
`_close_round` 因此**不**把 `Judgment.strongest_counterarguments`
（模型输出）填进 `counterexamples`。

### 3.2 "服务层的 `ValueError` 在 API 上是 422"不成立

ADR-0018 §5 这么写过。API 上的 422 实际来自 schema 的 `min_length=1`
提前拦下，与服务层的 `ValueError` 无关；服务层的那个分支
在 API 层根本不可达，而真走到它就是一个 500（见 §2.8）。

现在服务层抛的是 `InvalidRequestError`，这条表述才成立——但原因不同，
记在这里。

### 3.3 "数据库 CHECK 由枚举生成、自动保持同步"

`enum_check_expression` 只喂 SQLAlchemy 的 metadata；
**实际执行的 DDL 来自迁移里手写的字面量**。枚举加了成员而没写新迁移时，
数据库里仍是旧 CHECK——"自动保持同步"不成立。
（不变量 11 本身仍然成立：现有 CHECK 实测拒绝
`active` / `ACTIVE` / `Active` / `applied` / `live` / `promoted`。）

---

## 4. 评审未能推翻的部分（如实记录）

* **反馈不能绕过写入策略**：三种构造都没能突破。服务层只产出
  `MemoryWriteProposal`，写入的唯一出口是 `MemoryService.propose`。
* **"未评估 ≠ 不成立"**：`None` / `False` / `0` 的分流在代码与测试里
  确实分开，理由文本各不相同（§2.2 修的是它**下游**的一处反方向漏洞）。
* **不变量 11 的数据库级强制**：实测比声称的更强——六种"已生效"拼写
  全部被 `ck_improvement_proposals_status_valid` 拒绝。
* **真实并发下乐观锁确实挡住了**：在真实 PostgreSQL 上复现了真交错
  （失败方读到的是旧版本，且**没有留下任何事件**）。
* **两个实现的 UUID 排序等价**：PG 的 `uuid` 比较是 16 字节 memcmp，
  与规范小写十六进制串的字典序一致；用 3000 个随机 UUID 实测一致。
* **错误响应不泄漏堆栈**：500 的响应体是固定文案。

---

## 5. 这次评审暴露的一条流程事实

阶段 6 的所有代码、测试与文档由同一个执行者完成，**测试通过并不等于
结论成立**——上面 §2.1、§2.2、§2.4 三条都是"测试全绿而缺陷仍在"，
而且 §2.1 的那条用例与真正该验的东西只差一个 `uuid4()`。

> **测试的作者与设计的作者是同一个人时，"测试通过"证明的是
> 自洽，不是正确。** 独立评审在这个项目里不是形式，它这次抓到的
> 是验收条件本身不成立。

后续阶段应保持这个做法：**每阶段结束时至少做一次对抗性评审**，
重点是"找出测试在假装验证的地方"和"尝试构造反例"。
