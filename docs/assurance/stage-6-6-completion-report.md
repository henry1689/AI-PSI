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
| **问题**（`INQUIRY`） | ✅ 回合摘要的 `inquiry.id` | **黑盒**（H：找到了但**不接受纠正**） |
| **记忆**（`MEMORY`） | ⚠️ 记忆 id 可从 `/users/{id}/memories` 拿到，但它**不在本回合的事件流里** | **黑盒走的是"解析不到"那一支**（H）；"不可纠正"那一支本身不可达 |
| **证据 / 回答**（`EVIDENCE` / `RESPONSE`） | ❌ 当前 HTTP **拿不到** id（`src/` 里从不构造 `Evidence`；回答没有独立的产物 id） | **单元/集成**（`test_error_classifier.py`）；**不冒充黑盒** |

> 🔴 **最后一行必须如实读**：`EVIDENCE` → `EVIDENCE_ERROR` 与
> `RESPONSE` → `EXPRESSION_ERROR` 两条映射规则**今天只在单元层被验证过**。
> 它们的黑盒证据要等到有接口能指认这两种产物为止。

---

## 六、残余风险（详见 `docs/risks.md`）

| # | 风险 |
|---|---|
| **R69** | 每条有依据的纠正都会触发一次**完整的学习运行**——用"可能多跑一次"换"绝不永久卡死" |
| **R70** | 归因冲突在 V0.1 **没有裁决入口**：冲突的经验会一直不计入门槛。三处公开冲突条数，因此不会被静默忽略 |
| **R71** | 类别映射是**约定不是测量**——见 §四 |
| **R58** | 归因判据里**仍有 3 条**在生产上不可达（`_from_failure` / `_from_budget` / `_from_memory_rejection`）；用户纠正那条**已接通** |

---

## 七、明确不做（范围纪律）

不进入阶段 7；不改门槛、权重、评价档位与状态机；不重构无关模块；
不引入新依赖；不做全仓库保证工程（变异只重跑受影响的模块）。

唯一的新增接口面是**两个响应字段**（`FeedbackResponse.attribution` /
`.learning`）、**一个可选请求字段**（`related_artifact_id`）
与**一个视图字段**（`JudgmentView.judgment_id`）——
它们都是"把一个已经算出来的结论变成可读的"，不是新能力。
