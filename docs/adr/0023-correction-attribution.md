# ADR-0023：真实用户纠正的错误归因闭环（阶段 6.6）

- **状态**：已接受
- **日期**：2026-09-19
- **相关**：ADR-0018（反馈、经验与提案）、ADR-0020（经验评价与独立性语义）、
  ADR-0022（ProposalGate 权威边界）、ADR-0021（输入契约与事务原子性）

## 背景

阶段 6.5 的完成报告把 **C6.7「三次同类错误 → DRAFT 提案」降级**了，
理由不是做错了，而是**在默认产品配置下不成立**：

> 默认 Mock 产生的每条 `Experience.error_type` 都是 `None`，
> 于是 `PatternDetector` 把它们全部计入 `unattributable_count` 过滤掉，
> `PromotionPolicy.decide` / `ProposalGenerator.generate` **一次都不会被调用**。

**根因是时序，不是分类规则。** `ErrorClassifier._from_user_feedback`
（阶段 6）早就写好了，它的输入 `ErrorSignals.feedback_types` 也一直存在——
但经验在**回合收尾**时构建（`cognitive_runtime._close_round`），
而用户纠正在那之后才通过另一个 HTTP 调用到达。那一刻 `feedback_types`
永远是空的。这正是 `docs/risks.md` **R58** 记的"四条归因判据在生产上不可达"之一。

而且即便归因写进去了，还有第二个缺口：**没有人会再去跑那条学习链路**。
`LearningService.review()` 只有两个入口（CLI 与 `POST /learning/runs`），
**都不是自动的**——第三次纠正到达后库里会有三条归因，而提案不会出现。

## 决策

### 1. 归因的前提：**两个条件缺一不可**

用户纠正要形成错误类别，必须同时满足：

1. 反馈类型是**否定性的**（`CORRECTION` / `DISAGREEMENT`）；
2. 它**指得出**被纠正的是哪一条产物。

只满足第一条时**不归因**（`error_type` 保持 `None`）。

⚠️ **这取代了阶段 6 的做法**：那时一律归成 `UNKNOWN_ERROR`。
那个类别诚实但**没有分辨力**——所有被纠正过的回合都会落进同一个模式，
"三次同类错误"于是退化成"三次被纠正过"，而模式发现存在的理由
恰恰是分辨"哪一类错在反复发生"。

### 2. 指针由**服务端**解析，类别由**结构**推出

`FeedbackRequest` 增加可选的 `related_artifact_id`（与给人看的
`related_claim` 并存，各司其职）。服务端拿**本回合的事件流**把它反查成
"哪一类产物"——客户端说不了谎。

映射规则（按具体性排序，先命中胜出）：

| 被指产物 | 结构信号 | 类别 |
|---|---|---|
| 证据 | — | `EVIDENCE_ERROR` |
| 假设 | 没有支持证据 | `EVIDENCE_ERROR` |
| 假设 | 有支持证据 | `REASONING_ERROR` |
| 判断 | 声明为 `NORMATIVE` | `VALUE_SUBSTITUTION` |
| 判断 | 其余 | `REASONING_ERROR` |
| 回答 | — | `EXPRESSION_ERROR` |
| **解析不到 / 没给指针** | — | **不归因** |

只有白名单里的产物可以纠正（`CORRECTABLE_KINDS`）：**记忆**有它自己的入口
（`POST /memories/{id}/correct`），**问题**是用户自己提的——
让用户"纠正自己的问题"不会指向系统犯的错。

> ### ⚠️ 实现状态注记（post-acceptance，2026-09-19 补记）
>
> **上表的规则一条都没有被删除或改写。**下面这段只记录一条
> **实现层面的可达性事实**——它是在全阶段审计中发现的，当时并未记录。
>
> 🔴 **规则「假设 / 有支持证据 → `REASONING_ERROR`」在当前的 HTTP 生产
> 输入路径下不可达。**
>
> 原因是**上游**的，不在本 ADR 的决策范围内：
>
> * `SubmitMessageRequest`（`api/schemas.py`）**没有 evidence 字段**；
>   `api/routes/conversations.py` 构造 `RoundRequest` 时也不传它；
> * 因此生产路径上 `RoundRequest.evidence` **恒为空元组**
>   （`application/cognitive_runtime.py` 里它的默认值就是 `()`）；
> * 于是 `hypothesis_generator.evaluate` 里
>   `supporting = [item.id for item in evidence if ...]` **恒为空**，
>   `Hypothesis.supporting_evidence_ids` 恒为空列表；
> * 于是本服务的 `CorrectionTarget.has_supporting_evidence` 恒为 `False`
>   ——**永远走"没有支持证据"那一行**。
>
> **一处独立佐证**：阶段 7 的并发验收用例（`test_proposal_pattern_concurrency.py`）
> 跑了 20 轮，每一轮的情境签名都是 `d2|no_evidence|h2`
> ——`evidence_bucket` 恒为 `no_evidence`。
>
> **后果**：`EVIDENCE_ERROR` 可能被系统性多产、`REASONING_ERROR` 少产，
> 而这两者的比例正是阶段 7 要统计的归因指标之一。
> **不要**据此认为上表的规则设计有问题——**规则本身没有实现缺陷，
> 是它的输入条件在当前接口下拿不到**。
>
> 处置：**保留为风险**，见 `docs/risks.md` **R78**。本注记**不**改变
> 任何归因实现，也**不**意味着该规则已被实现或已被删除。

### 3. 🔴 **置信度说的是什么**（这一条最容易被说过头）

`attribution_confidence` 定为 `MODERATE`。但它的语义**必须**表述为：

> 这是**基于「用户明确纠正」+「V0.1 结构映射规则」形成的策略性归因**，
> **不是两个独立来源共同确认了客观错误类别**。

那两条依据**不独立**：映射规则是本系统的**约定**，不是对错误本质的
独立测量。把"用户说了 + 我们查出来它是什么"说成"互相印证"，
是把一条策略抬高成一次验证——而这个仓库里所有别的置信度
（`STAGE_ERROR_CATEGORY` 的阶段映射、元认知信号）都能追到
一条**独立于用户**的判据，唯独这一条不能。

理由串里逐字写着这句话，`ExperienceAttributionRecord` 的字段说明里也写着。

### 4. 归因是**追加记录**，不是改写经验

新增领域对象 `ExperienceAttributionRecord` 与事件类型
`experience.attributed`（**37 → 38**），与 `ExperienceEvaluationRecord`
完全同构：经验不可变，归因在它之后才到。

🔴 **它与评价是两个事件，不能合并**：评价回答"这条经验**该不该计权**"，
归因回答"它**到底是哪一类错**"。"用户确认了但他指不出错在哪"
是一个真实且常见的状态——合并成一个事件会让它无处安放。

审计字段按 §十一 第 7 条要求逐项落实：

| 要求 | 落在哪 |
|---|---|
| 关联回合 | `cognitive_round_id` |
| 关联原判断或回答 | `judgment_id` + `related_artifact_id` + `artifact_kind` |
| 纠正内容 | `evidence_refs`（反馈事件 id——正文在那条事件里，ADR-0018 §4） |
| 分类结果 | `error_type` |
| 分类依据 | `reasons` |
| 分类器版本 | `classifier_version`（新增 `CLASSIFIER_VERSION`） |

### 5. 🔴 归因冲突：**退回"不可归因"，绝不挑一个**

一条经验的**全部**归因视图（含它自己那份）去重后多于一个类别时：

* `attribution_conflict = True`；
* `effective_error_type = None`；
* 全部归因事件**原样保留**；
* 冲突条数在 `PatternScan.conflicting_attributions`、
  `ExperienceLoad.conflicting_attributions` → `GateVerdict.data_quality`
  与学习运行响应上公开。

**"最后一条说了算"不是解决冲突，是把冲突藏起来**——两条矛盾的归因里
至少有一条是错的，而"有一条是错的"这件事本身必须可见。

⚠️ 本口径比"只看外部归因之间是否矛盾"**更紧一档**：经验自带的类别
与外部归因不一致同样算冲突。理由是同一条，而紧的那一档更容易
说清楚、也更难被绕开。

### 6. 🔴 **提交之后**触发学习（本 ADR 的 P0）

```
async with self._uow_factory() as uow:      # 反馈 / 评价 / 归因 / 记忆，一个事务
    ...
    await uow.commit()                       # ← 必须先提交

learning = await self._run_learning_after_commit(...)
```

**触发条件：本次反馈之后，该回合存在有效归因**（不论是不是本次新写的）。

* **为什么必须提交之后**：学习链路通过 `ExperienceReader` 开**自己的**
  事务读事件流。在提交之前调它，那条新连接看不到刚才那条归因——
  症状是"库里一切正常，提案就是不出现"。
* **为什么是同步调用**：V0.1 没有 worker、没有队列（任务书 §12.1：
  回合在请求内同步执行完毕）。凭空引入后台任务就是阶段 7 的活。
* **为什么用"之后存在"而不是"本次新写"**：前者对**重试**幂等——
  重复纠正不会写第二条归因，但仍会触发一次运行。于是
  "学习链路偶发失败 + 客户端重试"不会把这条链路**永久卡死**，
  那比多跑一次糟得多。
* **"重试不得重复创建 Proposal" 不由触发器保证**，由提案层保证：
  `LearningService._covered_keys()` 让已存在的 `(error_class, signature)`
  不再生成。触发器**不自己记"跑过了"**——那会是第二份真相来源。

  > ⚠️ **这句话在并发下不成立，必须如实读**（评审 6.6 §F1 实测，
  > 记为 **R72**）。`_covered_keys()` 是"先读已存在的提案、再生成"，
  > 读与写之间没有锁，`improvement_proposals` 上也没有
  > `(error_class, applicability)` 唯一约束——**间隔 < ~50ms 的两个
  > 纠正请求会各自生成一条内容相同的 DRAFT 提案**。
  >
  > 准确的表述是：**顺序**重试不重复创建（黑盒场景 D 钉住了这一半），
  > **并发**不保证。修它属于提案层（唯一索引 + 迁移 + 在"最后一道关"
  > 处理冲突），不在本阶段范围内，因此本阶段**只改这句话，不改代码**——
  > 让一行声称的保证回到它真正成立的范围里。

**接线**：`application/ports.py::LearningTrigger`，由组合根注入
（`container.py` 里 `feedback_service` 因此移到 `learning_service` **之后**
构造；`LearningService` 不依赖 `FeedbackService`，不构成环）。
**参数刻意没有默认值**——给 `None` 默认值的症状是"忘了接就静默不再触发"，
而那与"还没攒够三次"在外部看来一模一样。

### 7. 🔴 触发失败**不得**丢数据，也**不得**泄漏异常原文

反馈与归因在触发之前**已经提交**。触发器整体包在 `try/except Exception` 里：

* 失败**不向上抛**——抛出去会把一个已经成功的反馈报成 500，
  客户端于是重试，而重试会走同一条死路；
* 完整异常（含 SQL、路径、堆栈）**只进服务端日志**；
* 对外只给一个**稳定错误码** `learning_trigger_failed` + 一个 `trace_id`，
  两者能在日志里对上。

## 影响

* `Experience` 的消费端改读 **`ExperienceAssessment.effective_error_type`**
  / `effective_attribution_confidence`（`PatternDetector.detect`）；
  ⚠️ 直接读 `experience.error_type` 会重新掉回"默认配置下恒为 `None`"。
* `FeedbackResponse` 增加 `attribution` 与 `learning` 两个字段。
  `learning` **没有默认值**：它的存在本身就是要求"必须把触发结果说出来"。
* `JudgmentView` 增加 `judgment_id`——它存在的理由**是被引用**，
  与"不外发 `selected_hypothesis_ids`"不矛盾：后者是内部标识，
  前者是**被纠正的锚点**。
* 事件总数 37 → 38。
* `FeedbackService` 的构造参数增加 `learning_trigger`（**无默认值**），
  因此所有夹具都要显式给一个。

## 残余风险

* 每条纠正都会触发一次完整的学习运行（代价随历史线性增长）。
  它以"绝不卡死"换"可能多跑一次"，见 §6。
* 映射规则是 V0.1 的约定，不是对错误本质的测量——§3。
* 归因冲突在 V0.1 **没有裁决入口**：冲突的经验会一直不计入门槛，
  直到有人手工处理。这是刻意的（"冲突解决前不计数"），
  但没有 UI 或接口去解决它。
* 🔴 **§2 的规则「有支持证据 → `REASONING_ERROR`」在当前 HTTP 生产
  输入路径下不可达**（原因见 §2 的实现状态注记）。处置为保留风险，
  见 `docs/risks.md` **R78**——它等阶段 7 的 Golden Cases 与归因指标
  提供数据后再决定要不要补 evidence 输入。
