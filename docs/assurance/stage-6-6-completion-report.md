# 阶段 6.6 完成报告：真实错误归因闭环

> **唯一目标**：通过真实 HTTP、真实认知回合、真实用户纠正和真实 PostgreSQL，
> 证明**默认产品配置**能够完成
> 三个独立回合 → 三次有依据的同类用户纠正 → 三条可审计 `Experience`
> → 可靠的 `error_type` → `PatternDetector` → `ProposalGate` → 一条 DRAFT Proposal。

---

## 一、缺口是什么（以及**不是**什么）

阶段 6.5 完成报告把 **C6.7** 降级了。降级的理由**不是分类规则写得不对**——
`ErrorClassifier` 的判据一直都在——而是**时序**：

* 经验在**回合收尾**时构建（`cognitive_runtime._close_round`），
  而用户纠正在那之后通过另一个 HTTP 调用才到；
  那一刻 `ErrorSignals.feedback_types` 永远是空的（R58）；
* 即便归因写进去了，**也没有人会再去跑那条学习链路**——
  `LearningService.review()` 只有 CLI 与 `POST /learning/runs` 两个入口，
  **都不是自动的**。

第二条是本阶段的 **P0**：不解决它，一切又会回到
"数据可读但生产链路不会运行"。

---

## 二、交付

| 交付 | 位置 |
|---|---|
| 归因语义与触发路径的设计记录 | `docs/adr/0023-correction-attribution.md` |
| 领域对象与事件 | `ExperienceAttributionRecord`、`EventType.EXPERIENCE_ATTRIBUTED`（37 → 38） |
| 归因规则 | `learning/error_classifier.py`：`CorrectionTarget` + `_from_user_correction` + `CLASSIFIER_VERSION` |
| 有效归因视图 | `ExperienceAssessment.effective_error_type` / `effective_attribution_confidence` / `attribution_conflict` |
| 触发路径 | `application/ports.py::LearningTrigger` + `feedback_service._run_learning_after_commit` |
| 接入 | `api/schemas.py`：`FeedbackRequest.related_artifact_id`、`AttributionView`、`LearningTriggerView`、`JudgmentView.judgment_id` |

---

## 三、黑盒场景 A–H（真实 HTTP × 真实 PostgreSQL × **默认 Mock**）

全部落在 `tests/integration/test_black_box_acceptance.py::TestTheCorrectionClosesTheAttributionLoop`。
**没有一个字是手工构造的**：回合由 HTTP 跑出来，指针从
`GET /cognitive-rounds/{id}/summary` 读回来，纠正由 HTTP 反馈写进去。

| # | 场景 | 结果 |
|---|---|---|
| **A** | 三个独立回合 + 三次有依据的同类纠正 | ✅ **一次都不调 `/learning/runs`**，DRAFT 提案自己出现，引用的正是那三条经验 |
| **B** | 只有两个独立回合 | ✅ 不生成 |
| **C** | 三次纠正但**指不出被纠正的对象** | ✅ 没有归因事件、不生成；**且反馈本身仍然成功** |
| **D** | 同一回合纠正三次 | ✅ 只留一条归因、只算一次发生、不生成 |
| **E** | 三次纠正指到**不同类型**的产物 | ✅ 类别不同 → 两个模式各自不足 3 次 → 不生成 |
| **F** | 三个回合、零纠正 | ✅ 没有归因、不生成（系统自己的怀疑不构成已确认错误） |
| **G** | 客户端在反馈体里伪造 `error_type` / `independence_group` | ✅ 422，且库里没有归因事件 |
| **H** | 指针指向别的回合 / 随机 UUID / **不可纠正的类型**（问题）/ 记忆 id | ✅ 反馈仍 201、归因不发生、不计入门槛、不生成 |
| 审计 | 一条 `experience.attributed` 在 PostgreSQL 里能查到 | ✅ 八个审计字段齐全 |

### 🔴 P0 的**实证**

不是"代码看起来对"，而是**关掉它就变红**：

* 把触发调用**移出 `record()`** → 场景 A 失败（提案列表为空）；
* 把触发调用挪到 `uow.commit()` **之前** → 同样失败。

> ⚠️ **单元测试发现不了这种改法**——内存夹具的事务边界更松，
> 只有在真实 PostgreSQL 的开事务语义下才暴露。

**A 场景必须证明 `decide`/`generate` 真的被调用**（而不是"库里多了一条归因事件"）：
断言落在**提案真的出现且证据对得上**——提案只可能由 `ProposalGenerator.generate`
产出、且必经 `ProposalGate` 授权，而整个 A 场景**一次都没有调用过 `/learning/runs`**。

---

## 四、归因规则与它的**语义边界**

规则表与推导见 ADR-0023 §2。这里只重复一件**最容易被说过头**的事：

> `attribution_confidence = MODERATE` 的语义是
> **「用户明确纠正」+「V0.1 结构映射规则」形成的策略性归因**，
> **不是两个独立来源共同确认了客观错误类别**。

那两条依据**不独立**：映射规则是本系统的约定，不是对错误本质的独立测量。
理由串、字段说明、ADR 三处**逐字**写着这句话。

---

## 五、🔴 证据等级（按**正式 API 的真实可达性**标注）

用户要求：HTTP 可达的走黑盒；当前 HTTP 不可达的只做单元/集成验证，
**不得直接写数据库伪造黑盒前置条件**。

| 被纠正产物 | 从 HTTP 拿得到 id 吗 | 证据等级 |
|---|---|---|
| **假设**（`HYPOTHESIS`） | ✅ 回合摘要的 `hypotheses[].id` | **黑盒**（A–E、H） |
| **判断**（`JUDGMENT`） | ✅ 回合摘要的 `judgment.judgment_id`（本阶段新增暴露） | **黑盒**（E） |
| **问题**（`INQUIRY`） | ✅ 回合摘要的 `inquiry.id` | **黑盒**（H：找到了但**不接受纠正**）⚠️ 这一行的黑盒成色**在本次评审之后才补齐**——原先 H 的 `inquiry` 分支与 `random` 分支可观测结果完全相同（删掉该映射照样全绿），见 §八 F5 |
| **记忆**（`MEMORY`） | ⚠️ 记忆 id 可从 `/users/{id}/memories` 拿到，但它**不在本回合的事件流里** | **黑盒走的是"解析不到"那一支**（H）；"不可纠正"那一支本身不可达 |
| **证据 / 回答**（`EVIDENCE` / `RESPONSE`） | ❌ 当前 HTTP **拿不到** id（`src/` 里从不构造 `Evidence`；回答没有独立的产物 id） | **单元/集成**（`test_error_classifier.py`）；**不冒充黑盒** |

> 🔴 **最后一行必须如实读**：`EVIDENCE` → `EVIDENCE_ERROR` 与
> `RESPONSE` → `EXPRESSION_ERROR` 两条映射规则**今天只在单元层被验证过**。
> 它们的黑盒证据要等到有接口能指认这两种产物为止。

---

## 六、残余风险（详见 `docs/risks.md`）

| # | 风险 |
|---|---|
| **R72** | 🔴 **并发的两次纠正可以写出两条一模一样的 DRAFT 提案**——`_covered_keys` 读-写竞态，且提案表上没有对应唯一约束。**顺序**重试不重复（场景 D 钉住），**并发**不保证。**本阶段只改声明不改代码**（修它要动提案层：唯一索引 + 迁移 + "最后一道关"处理冲突） |
| **R73** | 并发的同一次纠正可以写出重复的 `experience.attributed` 事件——**不抬门槛**（读取端按同一三元组去重），受影响的是审计面原始事件条数。"保证"已降级为"尽力去重" |
| **R74** | 🔴 **"跨主体"没有结构保证**：V0.1 全仓库无鉴权，任何知道 `round_id` 的调用方都能在别人的回合上写归因。跨**回合**是结构保证，跨**主体**不是。本阶段改的是那句过强的文档 |
| **R75** | `ErrorPattern.experience_count` 恒等于 `occurrence_count`，"重复抽取造出的冗余"这个信号**不存在**。不影响门槛，故变异测试测不到（那条分数测的是"测试对改动有多敏感"，而这个字段的规格从未被编码） |
| **R69** | 落在"曾被有效纠正过"的回合上的🔴**任何**反馈都会触发一次**完整的学习运行**——触发面比"有依据的纠正"宽得多。用"可能多跑几次"换"绝不永久卡死" |
| **R70** | 归因冲突在 V0.1 **没有裁决入口**：冲突的经验会一直不计入门槛。三处公开冲突条数，因此不会被静默忽略 |
| **R71** | 类别映射是**约定不是测量**——见 §四 |
| **R58** | 归因判据里**仍有 3 条**在生产上不可达（`_from_failure` / `_from_budget` / `_from_memory_rejection`）；用户纠正那条**已接通** |

> ### ⚠️ Post-acceptance clarification（2026-09-19 补记，**不改写上方任何历史结论**）
>
> 上表是**阶段 6.6 验收当时**的残余风险清单，**保持原样**。
> 下面补记一条**验收之后**才发现的条目——它**不改变**本阶段已经做出的任何结论。
>
> * **R78 是阶段 1–7 全阶段设计符合性审计（2026-09-19）后发现的**，
>   **在阶段 6.6 验收时尚无此条**，因此它不在上表里，这不是遗漏登记。
> * 事实：**当前 HTTP 生产路径不填充 `RoundRequest.evidence`**——
>   `SubmitMessageRequest`（`api/schemas.py`）没有 evidence 字段，
>   `api/routes/conversations.py` 构造 `RoundRequest` 时也不传它，
>   因此生产路径上它**恒为空元组**。
> * 后果：ADR-0023 §2 的规则「**假设 / 有支持证据 → `REASONING_ERROR`**」
>   在该路径下**不可达**，恒走"无支持证据 → `EVIDENCE_ERROR`"。
>   （同一条在 ADR-0023 §2 有对应注记，在 `docs/risks.md` 记为 **R78**，
>   三处表述一致。）
> * 🔴 **这不推翻阶段 6.6 已经执行的 A–H 验收结果。**
>   八个黑盒场景验的是**归因链路与触发时机**（有/无指针 × 有/无否定反馈），
>   那些结论**仍然成立**；受影响的只是 ADR-0023 §2 映射表里
>   **其中一行在今天的接口下取不到输入**，与验收结论正交。
> * 处置：**风险由 R78 跟踪**，等**正式阶段 7 的 Golden Cases 与归因指标**
>   提供数据后再定（补 evidence 输入 / 显式移除该规则 / 维持现状并在报告里注明）。
>   本注记**不修改**任何归因实现。

---

## 七、证据

| 证据 | 结果 |
|---|---|
| `uv run ruff check .` / `ruff format --check` | 全绿 |
| `uv run mypy src tests scripts mutation` | 223 个文件，**0 错误**（`--strict`） |
| `uv run pytest -m "not live"` | **2037 passed**，9 deselected，0 failed |
| 黑盒场景 A–H + 审计（真实 HTTP × 真实 PostgreSQL × 默认 Mock） | **14/14** |
| 变异：`domain/experiences.py` | **169/169 = 100.0%**，0 存活，0 incompetent |
| 变异：`learning/pattern_detector.py` | **104/104 = 100.0%**，0 存活，0 incompetent |
| 变异合计 | **273/273 = 100.0%** |
| 等价登记漂移（`_stale_equivalents`） | **0** |

复现：

```bash
uv run pytest -m "not live"                                        # 全量
uv run pytest tests/integration/test_black_box_acceptance.py -q    # A–H
uv run python mutation/run.py experiences pattern_detector         # 变异
```

### 本阶段被变异测试抓到的**真缺口**（4 处，全在本阶段新写的代码里）

变异跑是**先红后绿**才可信的，因此如实记下它先变红的那一轮：

| 缺口 | 为什么它是真缺口 |
|---|---|
| `PatternScan.conflicting_attributions` 的默认值 | 阶段 6.5 那条"默认值是契约"的用例**存在但没覆盖新字段**——正是它会漏掉的形态 |
| `attribution_basis` 的筛选条件（`is` → `is not`） | 冲突经验必须**没有**依据；没有用例钉住"空"这一侧 |
| 冲突时依据必须为空 | 同上，只测了"有依据"那一侧 |
| `ExperienceAttributionRecord` 的身份字段边界（`experience_canonical_key` / `classifier_version` 的 `min_length`） | 新加的身份字段没人试过把它构造成空的 |

另有一处 `attribution_conflict` 的**默认值**（改成 `True` 后全绿）在本轮补齐——
它不是不起眼的默认：默认为真等于让**每一条**没显式带标志的评估凭空退出计数。
补的用例是 `test_the_attribution_defaults_are_the_quiet_ones`，
**反向验证过**（把默认值翻成 `True`，该用例当场变红）。

> ⚠️ 与"行号漂移"的区别：漂移是 `EQUIVALENTS` 的**登记位置**过期，
> 不是测试缺口——`_stale_equivalents` 的全部作用就是把这类过期**报出来**
> 而不是静默放行。

### 评审处置带来的**第三轮**变异重跑

修 F7（`_distinct_occurrences` 加排序）给 `pattern_detector.py` 净加了
6 行，**8 条登记等价整体漂移 +6**。工具如实报出每一处的新行号：

```
104 → 110、152 → 158、172 → 178（slots 族）
282 → 288、285 → 291（`-item.weighted_count`）
355 → 361 ×2（`evaluations[-1]`）
199 → 205（`*,` → `/,`）
```

按报出的真实行号更新登记后重跑：**273/273 = 100%**，0 漂移，0 incompetent。

> 🔴 **这正是不把漂移"顺手改绿"的理由**：漂移期间分数会**掉到 92.9%**
> （8 条登记失配，对应的变异体被算成存活）。掉分是**正确的信号**——
> 它说的是"这 8 条登记现在没覆盖到任何东西"，而不是"测试变弱了"。
> 先看清哪 8 条、再改行号，两者不能倒过来。

### 每一处修复都做过**反向验证**（关掉它就变红）

| 修复 | 反向验证 |
|---|---|
| `attribution_conflict` 默认值 | 翻成 `True` → `test_the_attribution_defaults_are_the_quiet_ones` 当场红 |
| F7 排序 | 去掉 `sorted(...)` → 新用例复现 `1 == 0` 的翻转（与评审实测一致） |
| F6 归属用户 | 去掉 `user_id=` → 审计用例在 `rows[0][1] == user_id` 处红 |

---

## 八、独立评审（只针对错误归因与学习闭环）

一次**对抗性、只读**的独立评审（子 Agent，非作者），范围严格限定在
`纠正目标解析 → 错误归因 → 原子提交 → 提交后触发 → 有效归因读取
→ 冲突处理 → Pattern → Gate → DRAFT`。

**隔离要求（本次特别加强）**：评审者用**独立数据库** `ai_psi_review`
跑测试，不与主会话共用 `ai_psi_test`——阶段 6.5 那 33 个
`DeadlockDetected` 假失败就是两个 pytest 进程打同一个库造成的。

做法（`conftest` 会自己建库并跑迁移，因此只需换一个库名）：

```bash
# 从开发库名派生出一个评审专用库名，覆盖集成测试的连接串
export AI_PSI_TEST_DATABASE_URL="$(uv run python -c '
from ai_psi.config import get_settings
url = get_settings().database_url
print(url.rpartition("/")[0] + "/ai_psi_review")')"
uv run pytest tests/integration/test_black_box_acceptance.py -q
```

> ⚠️ **评审者被明确告知不得直接跑集成测试**——漏掉这一步的表现不是报错，
> 而是随机出现 `DeadlockDetected`，看起来像被测代码有并发缺陷。

### 判定"守住了"的检查点（评审者的验证方式一并记下）

| 检查点 | 结论 |
|---|---|
| 跨**回合**指针 | 守住。`read_stream` 就是 `WHERE cognitive_round_id = :id`，别的回合的产物不在候选集里（结构保证，非注释） |
| 冲突归因与**到达顺序**无关 | 守住。同一对归因反序调用 `assess_experiences`，两向都是"无类别 + 冲突 + 无依据"，序列逐项相同 |
| commit 前后时序 | 守住。`await uow.commit()` 是事务块内最后一条语句，触发在块**外**；未提交即回滚且不吞异常 |
| 触发失败不丢数据 / 不泄漏 | 守住。响应只有 `status / created_proposal_ids / error_code / trace_id`；反向断言确认异常原文与 SQL 片段都不出现 |
| 客户端伪造字段 | 守住。`extra="forbid"` 覆盖全部边界模型；`error_type` / `independence_group` / `classifier_version` 在任何请求模型里都不存在 |
| API 错误信息 | 守住。领域异常无堆栈，5xx 换成固定文案，422 只回字段路径 |
| **Gate 只用可信持久化记录** | 守住，且是这条链路里最扎实的一块。`ProposalGate.review` 的输入里没有 `ErrorPattern` / `PromotionDecision`，计数一律重读事件流重算；`GateVerdict` 只能由 `ProposalGate` 构造 |

### 发现与处置

| # | 发现 | 判定 | 处置 |
|---|---|---|---|
| **F1** | 🔴 并发纠错写出**两条**重复 DRAFT 提案（`_covered_keys` 读-写竞态，无唯一约束），间隔 < ~50ms 3/3 复现 | CONFIRMED | **改声明，不改代码** → **R72**。修它要动提案层（唯一索引 + 迁移 + "最后一道关"处理冲突），超出本阶段范围。ADR-0023 §6 与本文档的措辞已收回到真正成立的范围：**顺序**重试不重复（黑盒 D 钉住），**并发**不保证 |
| **F2** | 并发纠错写出重复 `experience.attributed` 事件（N=4 时 2 条，N=8 时 4~5 条） | CONFIRMED | 改注释 + 记 **R73**。**不抬门槛**：读取端按同一三元组去重，有效类别与加权计数不变，受影响的是审计面原始事件条数。"保证"已降级为"尽力去重" |
| **F3** | 触发面比 R69 记的宽：**任何**反馈落在曾被纠正过的回合上都会触发全量学习 | CONFIRMED | 改 **R69** 的措辞（行为是刻意设计，不改） |
| **F4** | `ErrorPattern.experience_count` 恒等于 `occurrence_count`，"冗余可见"这个信号不存在 | CONFIRMED | 改文档 + 测试注释，记 **R75**。**未实现该信号**：它在 `pattern_detector` 的汇报面上、无生产消费者，与归因链路无关 |
| **F5** | H 用例的 `inquiry` 分支与 `random` 分支**可观测结果完全相同**，删掉 `"inquiry"` 映射照样全绿 | CONFIRMED | **补断言**：两支的理由在源码里本就分开写，现在按分支断言（"不接受纠正" / "解析不到"） |
| **F6** | `experience.attributed` 的 `user_id` 是 NULL，与同事务另两条事件不一致 | CONFIRMED | **修**：事件带上 `user_id`；审计用例改成跑**带用户**的回合并断言三处一致。反向验证过（去掉该参数，用例当场变红） |
| **F7** | 分组的代表成员取**输入顺序第一条**，同一集合换个顺序加权计数就在 0/1 之间翻转 | CONFIRMED（今天不可达） | **修**：先按**发生时间**排序再取首条（复用 `_ordered` 的同一把键）。这是"确定"，不是"策略"。反向验证过（去掉排序，新用例复现 `1 == 0` 的翻转） |
| — | "跨主体"没有结构保证：V0.1 **全仓库无鉴权**，任何知道 `round_id` 的调用方都能在别人的回合上写归因 | CONFIRMED | 改文档措辞（那句话对**记忆**成立、对"别人的回合产物"不成立），记 **R74**。这是 V0.1 的全局性质，不是本阶段引入的 |

> 🔴 **评审者还逐行核过完成报告 §三 那句"没有一个字是手工构造的"**——
> 回合、指针、纠正确实都走 HTTP，原生 SQL 只用于只读计数与
> "数据库自己拒绝 `active`"的反向用例。**该声明成立。**

---

## 九、明确不做（范围纪律）

不进入阶段 7；不改门槛、权重、评价档位与状态机；不重构无关模块；
不引入新依赖；不做全仓库保证工程（变异只重跑受影响的模块）。

唯一的新增接口面是**两个响应字段**（`FeedbackResponse.attribution` /
`.learning`）、**一个可选请求字段**（`related_artifact_id`）
与**一个视图字段**（`JudgmentView.judgment_id`）——
它们都是"把一个已经算出来的结论变成可读的"，不是新能力。
