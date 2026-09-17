# 领域模型数据字典

> 实现位置：`src/ai_psi/domain/`
> 相关：ADR-0006（EntityMetadata 继承）、ADR-0010（两个决策枚举）、ADR-0012（缺口默认值）

---

## 0. 通用约定

### 0.1 EntityMetadata

**除 `Event` 外，所有领域对象继承 `EntityMetadata`**（ADR-0006）。

| 字段 | 类型 | 约束 |
|---|---|---|
| `id` | `UUID` | 默认 `uuid4()`，不可变 |
| `created_at` | `datetime` | **必须 UTC 且 tz-aware**；naive datetime 直接拒绝 |
| `updated_at` | `datetime` | 同上 |
| `version` | `int` | ≥1，乐观锁；每次持久化更新递增 |
| `created_by` | `str` | 产生该对象的组件或用户标识 |
| `schema_version` | `str` | 该对象结构版本，用于未来迁移 |

**全局规则：**

- **`extra="forbid"`**：未知字段一律拒绝（模型多返回的字段不被静默接受）。
- **时间一律 UTC**：存储与领域对象内部一律 UTC；API 输出层可附加本地时区。
- **乐观锁**：写入时 `version` 不匹配 → `OptimisticLockError`，**绝不静默覆盖**。
- **禁止 naive datetime**：由 `domain/common.py` 的统一校验器强制。

### 0.2 三套模型不互转

`domain/`（领域对象）、`infrastructure/db/models.py`（ORM 实体）、
`api/schemas.py`（API Schema）是**三套独立定义**，
转换必须写显式函数，**不允许 `model_validate` 一键互转**（任务书 §5 开头要求）。

### 0.3 通用枚举

| 枚举 | 成员 |
|---|---|
| `OrdinalLevel` | `VERY_LOW` / `LOW` / `MODERATE` / `HIGH` / `VERY_HIGH` |
| `ConfidenceBand` | `VERY_LOW` / `LOW` / `MODERATE` / `HIGH` / `VERY_HIGH` |
| `TrustLevel` | `UNTRUSTED` / `LOW` / `MEDIUM` / `HIGH` / `VERIFIED` |
| `SensitivityLevel` | `PUBLIC` / `INTERNAL` / `PERSONAL` / `SENSITIVE` / `HIGHLY_SENSITIVE` |
| `VerificationStatus` | `UNVERIFIED` / `SELF_REPORTED` / `THIRD_PARTY` / `VERIFIED` / `DISPUTED` / `REFUTED` |
| `EvidenceDirectness` | `DIRECT` / `INDIRECT` / `INFERRED` / `HEARSAY` |
| `ActorType` | `USER` / `SYSTEM` / `MODEL` / `EXTERNAL` |
| `UncertaintyType` | `ALETHIC`(证据不足) / `EPISTEMIC`(知识缺失) / `LINGUISTIC`(表述歧义) / `ONTOLOGICAL`(概念边界不清) / `NORMATIVE`(价值不可由事实决定) |

**置信度用分档不用百分比**：模型不应伪造精确概率。
内部规则可算出分档，但**模型不能直接输出百分比**。

---

## 1. 输入与事件层

### 1.1 `Event`（`domain/events.py`）

**唯一不继承 `EntityMetadata` 的对象**——事件只追加，不改不删，
`version` / `updated_at` 对它无意义（ADR-0006）。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | `UUID` | |
| `event_type` | `EventType` | 32 种，见 §1.2 |
| `occurred_at` | `datetime` | 事件**发生**时间（UTC） |
| `recorded_at` | `datetime` | 事件**被记录**时间（UTC） |
| `actor_type` | `ActorType` | |
| `actor_id` | `str` | |
| `user_id` | `UUID \| None` | 作用域键 |
| `conversation_id` | `UUID \| None` | |
| `cognitive_round_id` | `UUID \| None` | |
| `correlation_id` | `UUID` | 同一次请求的关联链 |
| `causation_id` | `UUID \| None` | 直接触发本事件的上游事件 |
| `payload` | `dict[str, Any]` | **必须脱敏**，禁止写入高敏感正文 |
| `evidence_refs` | `list[UUID]` | |
| `trust_level` | `TrustLevel` | |
| `sensitivity` | `SensitivityLevel` | |
| `model_info` | `ModelInvocationInfo \| None` | |
| `schema_version` | `str` | |

**双时间戳的理由**：回放时必须区分"事情何时发生"与"我们何时知道"，
否则乱序到达的事件会被错误地按记录时间排序。

### 1.2 事件类型（32 种）

```
user.message.received        user.feedback.received      user.correction.received
observation.created          concern.created             inquiry.created
cognitive_round.started      cognitive_round.state_changed
evidence.attached            concept.identified          assumption.identified
hypothesis.created           hypothesis.evaluated
belief.created               belief.revised              belief.superseded
judgment.created             metacognition.completed
response.generated           response.delivered
memory.proposed              memory.approved             memory.rejected
memory.expired               memory.corrected
experience.created
improvement_proposal.created improvement_proposal.evaluated
cognitive_round.completed    cognitive_round.suspended   cognitive_round.failed
```

### 1.3 `ModelInvocationInfo`（`domain/events.py`）

| 字段 | 说明 |
|---|---|
| `invocation_id` | UUID；**重试必须使用新的 invocation_id** |
| `provider` / `model` | 必须记录 |
| `task_name` / `prompt_version` | 必须记录（不变量 18） |
| `started_at` / `completed_at` / `latency_ms` | |
| `input_token_count` / `output_token_count` | 成本统计 |
| `retry_count` | |
| `result_status` | |
| `response_hash` | **原始响应哈希**——用于审计比对 |

> 🔴 **不保存供应商返回的完整隐藏推理内容。** 只保留哈希（ADR-0003）。

---

## 2. 认知对象层

### 2.1 `Observation`（`domain/observations.py`）

| 字段 | 类型 |
|---|---|
| `user_id` | `UUID \| None` |
| `source_type` | `SourceType`（USER_MESSAGE / EXTERNAL_DOC / SYSTEM_EVENT / TOOL_RESULT） |
| `source_id` | `str` |
| `content` | `str` |
| `observed_at` | `datetime` |
| `directness` | `EvidenceDirectness` |
| `trust_level` | `TrustLevel` |
| `verification_status` | `VerificationStatus` |
| `possible_expiry` | `datetime \| None` |
| `limitations` | `list[str]` |
| `sensitivity` | `SensitivityLevel` |

> 🔴 **只描述看见或收到什么，禁止心理诊断。**
> 「用户回复很短」→ Observation；「用户情绪低落」→ 必须是 Hypothesis。

### 2.2 `Concern`（`domain/concerns.py`）

| 字段 | 类型 | 说明 |
|---|---|---|
| `source_event_ids` | `list[UUID]` | 来源，**必填非空**——无依据的关切应被过滤 |
| `category` | `ConcernCategory` | 9 类，见下 |
| `statement` / `why_it_matters` | `str` | |
| `related_goal_ids` | `list[UUID]` | 指向 `Memory(memory_type=USER_GOAL)` 的 id（ADR-0012） |
| `impact` / `urgency` / `uncertainty` | `OrdinalLevel` | |
| `expected_information_value` | `OrdinalLevel` | 解决它能带来多少信息 |
| `cognitive_cost` | `OrdinalLevel` | 解决它要花多少认知预算 |
| `status` | `ConcernStatus` | OPEN / ADDRESSED / DEFERRED / EXPIRED / DISMISSED |
| `expiry_at` | `datetime \| None` | |

`ConcernCategory`：`USER_REQUEST` / `KNOWLEDGE_GAP` / `CONFLICT` /
`PREDICTION_ERROR` / `CONCEPTUAL_AMBIGUITY` / `LONG_TERM_GOAL` /
`RELATIONSHIP_BOUNDARY` / `EXPLORATION` / `SYSTEM_RELIABILITY`

**必须被过滤掉的：** 无依据的主动问题、纯粹为了表现聪明的探索、
不相关旧记忆、未授权的隐私推测。

### 2.3 `Inquiry`（`domain/inquiries.py`）

把关切转成**可结束的**问题。

| 字段 | 说明 |
|---|---|
| `concern_id` | 上游关切 |
| `question` / `why_it_matters` | |
| `scope` / `out_of_scope` | 范围与**排除范围**（两者都必填） |
| `known_observation_ids` | 已知 |
| `current_belief_ids` | 当前信念 |
| `key_unknowns` | 关键未知 |
| `ambiguous_concepts` | 需澄清的概念 |
| `assumptions_to_check` | 待检前提 |
| `expected_output_type` | `ExpectedOutputType` |
| `verification_method` | `str \| None` |
| `stop_conditions` | **停止条件（必填非空）** |
| `reopen_conditions` | 重新触发条件 |
| `depth_level` | `CognitiveDepth`（D0–D4） |
| `status` | `InquiryStatus` |

### 2.4 `Evidence`（`domain/evidence.py`）

| 字段 | 说明 |
|---|---|
| `observation_id` | 来源观察 |
| `source_uri` / `source_name` | |
| `content_summary` | 摘要（不是全文） |
| `published_at` / `retrieved_at` | |
| **`independence_group`** | **同源证据标记** |
| `reliability` / `directness` / `freshness` | 三个独立的证据质量维度 |
| `supports_claim_ids` / `opposes_claim_ids` | 支持与反对 |
| `limitations` | |
| `verification_status` | |

> 🔴 **同源证据规则**：多个转载来源**不能**自动算作多个独立证据。
> 它们共享同一个 `independence_group`，在证据计数与置信度计算中
> **只按一组计**。这是防止"看起来证据很多"的主要机制。

### 2.5 `Concept` / `Assumption`（`domain/concepts.py`, `domain/assumptions.py`）

`Concept`：`term` / `working_definition` / `alternative_definitions` /
`boundaries` / `ambiguity_notes` / `related_concepts` / `context_scope`

`Assumption`：`statement` / `source` / `necessity`（`AssumptionNecessity`）/
`testability`（`Testability`）/ `evidence_ids` / `status`（`AssumptionStatus`）

### 2.6 `Hypothesis`（`domain/hypotheses.py`）

| 字段 | 说明 |
|---|---|
| `inquiry_id` | |
| `statement` | |
| `category` | `HypothesisCategory`（ADR-0012 新增枚举） |
| `supporting_evidence_ids` / `opposing_evidence_ids` | |
| `assumption_ids` | |
| `predicted_observations` | 若成立，应能观察到什么 |
| **`falsification_conditions`** | **可反驳条件（必填非空）** |
| `applicability` | 适用范围 |
| `uncertainty_type` | `UncertaintyType` |
| `status` | `HypothesisStatus` |

`HypothesisCategory` 至少包含：
`FACTUAL` / `MECHANISTIC` / `CONTEXTUAL` / `INTENTIONAL` /
**`NON_AGENTIC`（非人格化解释）** / `ALTERNATIVE` / `SYSTEMIC`

> 🔴 **约束：**
> - 默认状态 `CANDIDATE`；
> - **禁止直接写入事实记忆**（不变量 1）；
> - 必须允许 `REJECTED` / `UNRESOLVED`；
> - 高风险或高深度问题**至少保留一个非人格化/非心理化解释**（§9.6）；
> - 不强制简单问题生成多个假设。

### 2.7 `Belief` / `Judgment`

**`Belief`**（`domain/beliefs.py`）——跨回合的稳定信念：

`user_id` / `statement` / `belief_type`（`BeliefType`）/ `status`（`BeliefStatus`）/
`supporting_evidence_ids` / `opposing_evidence_ids` / `assumption_ids` /
`applicability` / `uncertainty_type` / `confidence_band` /
**`confidence_basis`（置信度依据）** /
`valid_from` / `valid_until` / `revision_conditions` / `supersedes_id`

> 🔴 不变量 2：**Belief 必须有依据，或明确标记为暂定。**
> 实现为**析取**校验：`status` 为 `TENTATIVE` 时允许无依据，
> **其余任何状态**（ACTIVE / DISPUTED / …）都要求 `confidence_basis` 非空。
> 一个既没有依据、又没被标注为暂定的信念，会在后续推理中被当作可靠前提使用。

**`Judgment`**（`domain/judgments.py`）——本次回合的暂定结论：

| 字段 | 说明 |
|---|---|
| `inquiry_id` | |
| `selected_hypothesis_ids` | 被采纳的假设 |
| `conclusion` | 暂定结论 |
| `rationale_summary` | **结构化理由摘要，不是隐藏思维链** |
| `strongest_counterarguments` | 最强反证（必填） |
| `unresolved_unknowns` | 未解决未知 |
| `applicability` | 适用范围 |
| `confidence_band` / `confidence_basis` | |
| `revision_conditions` | 修正条件 |
| `recommended_epistemic_action` | `EpistemicAction`（ADR-0010） |

**合法判断形态**（§9.10）：结论较可靠 / 结论暂定 / 多种解释并存 /
无法判断 / 需要更多证据 / 事实无法决定价值选择（→ `OUT_OF_SCOPE`）。

### 2.8 `Reflection`（`domain/reflections.py`）

| 字段 | 类型 | 说明 |
|---|---|---|
| `cognitive_round_id` | `UUID` | |
| `new_evidence_present` / `new_reasoning_path_present` | `bool` | 反刍检测基础 |
| `repeated_claim_score` | `float` | **范围 [0, 1]** |
| `scope_drift_detected` | `bool` | 状态漂移 |
| `confirmation_bias_risk` / `user_pleasing_bias_risk` / `abstraction_escape_risk` | `OrdinalLevel` | 模型层检查 |
| `unsupported_certainty_detected` / `missing_counterexample_detected` | `bool` | |
| `stop_condition_reached` | `bool` | |
| `marginal_value` | `OrdinalLevel` | 继续还有多少边际价值 |
| `decision` | `MetacognitiveDecision` | 见 ADR-0010 |
| `reasons` | `list[str]` | **必填非空** |

---

## 3. 状态层

### 3.1 `CognitiveRound`（`domain/cognitive_rounds.py`）

| 字段 | 说明 |
|---|---|
| `user_id` / `conversation_id` | 作用域 |
| `state` | `RoundState`（14 态，见 `docs/state_machine.md`） |
| `depth_level` | `CognitiveDepth`——路由结果，指标按此分组 |
| `budget` | `CognitiveBudget`（ADR-0008） |
| `model_calls_used` / `metacognitive_loops` | 实时计数 |
| `stop_reason` | **`COMPLETED` 必填**（不变量 19） |
| `failure_stage` / `error_category` | **`FAILED` 必填**（不变量 20） |
| `idempotency_key` | API 幂等 |
| `correlation_id` / `causation_id` | 审计链 |
| `started_at` / `completed_at` | |

> 🔴 **不成立的状态组合由模型校验器直接拒绝**：
> `state=COMPLETED` 而 `stop_reason is None` → 校验失败；
> `state=FAILED` 而 `failure_stage is None` → 校验失败。

### 3.2 `CognitiveBudget`（`domain/cognitive_rounds.py`）

`max_model_calls`(12) / `max_metacognitive_loops`(2) / `max_hypotheses`(4) /
`max_retrieved_memories`(20) / `max_context_tokens`(32000) /
`max_duration_seconds`(120) / `max_cost_units`(None)

提供 `for_depth(level: CognitiveDepth) -> CognitiveBudget` 工厂
（深度差异表见 ADR-0008）。

---

## 4. 记忆与经验层

### 4.1 `Memory`（`domain/memories.py`）

| 字段 | 说明 |
|---|---|
| `user_id` | **作用域键——检索必须强制**（不变量 14） |
| `memory_type` | `MemoryType`（10 类） |
| `content` | |
| `source_event_ids` / `evidence_ids` | 来源 |
| `verification_status` | |
| `applicability` | |
| `sensitivity` | 写入前必须分类（§17.1） |
| `retention_policy` | `RetentionPolicy` |
| `access_scope` | |
| `valid_from` / `valid_until` | |
| `supersedes_id` | 版本链 |
| `contradicts_ids` | 冲突标记 |
| `status` | `MemoryStatus`（ACTIVE / SUPERSEDED / DISPUTED / EXPIRED / DELETED） |
| `embedding_version` | 向量模型版本 |

`MemoryType`：`EPISODIC` / `SEMANTIC` / `USER_CONFIRMED_FACT` /
`USER_PREFERENCE` / `USER_GOAL` / `CONCEPTUAL` / `STRATEGY` /
`FAILURE_CASE` / `SELF_MODEL`

> 🔴 不变量 6：**`SUPERSEDED` / `DELETED` 的记忆不得作为默认有效结果返回。**
> 🔴 不变量 15：删除必须传播到向量索引。

### 4.2 `Experience`（`domain/experiences.py`）

| 字段 | 说明 |
|---|---|
| `cognitive_round_id` / `judgment_id` | 来源回合与判断 |
| `situation_signature` | 情境签名，用于模式发现 |
| `inquiry_type` | |
| `evidence_available_at_time` | **当时**有哪些证据——用于判断是否信息不足 |
| `predicted_feedback` / `actual_feedback` | 预测与实际 |
| `later_evidence_ids` | 后来才出现的证据 |
| `error_type` | `ErrorType \| None`（12 类） |
| `attribution_confidence` | `ConfidenceBand` |
| `strategy_used` / `strategy_effectiveness` | |
| `applicable_conditions` / `counterexamples` | |
| `verification_status` | |

`evidence_available_at_time` 是关键字段：它区分
**"当时判断错了"** 与 **"当时信息本就不足"**。
没有它，系统会把信息缺失误判为推理错误。

### 4.3 `ImprovementProposal`（`domain/improvement_proposals.py`）

`target_component` / `observed_problem` / `error_class` /
`supporting_experience_ids` / `counterexamples` / `proposed_change` /
`expected_benefit` / `possible_regressions` / `applicability` /
`evaluation_plan` / `success_metrics` / `rollback_conditions` /
`approval_level`（`ApprovalLevel`）/ `status`（`ProposalStatus`）

`ProposalStatus`：`DRAFT` → `PENDING_EVALUATION` → `EVALUATED` →
(`REJECTED` | `APPROVED_FOR_MANUAL_TRIAL`)

> 🔴 **不存在 `ACTIVE`。** 提案永不自动生效（不变量 11，ADR-0005）。
> 🔴 产生门槛：同类错误 ≥3 次，或满足 §11.3 的任一条件。
> **单次普通错误只创建 Experience。**

### 4.4 `UserModel`（`domain/user_models.py`）

`user_id` / `attribute` / `value` / `evidence_ids` / `confidence_band` /
`status`（`UserModelStatus`）/ `valid_from` / `valid_until`

> 🔴 不变量 13：**用户模型中的推测不得标记为确认事实。**
> 禁止根据少量对话生成稳定人格诊断（§2.3）。

---

## 5. 错误分类

`ErrorType`（12 类，`domain/enums.py`）：

```
FACTUAL_ERROR      REASONING_ERROR    CONCEPTUAL_ERROR   EVIDENCE_ERROR
CALIBRATION_ERROR  SCOPE_ERROR        VALUE_SUBSTITUTION USER_MODEL_ERROR
EXPRESSION_ERROR   PROCESS_ERROR      MEMORY_ERROR       UNKNOWN_ERROR
```

`VALUE_SUBSTITUTION`（把价值偏好当作事实）与 `CALIBRATION_ERROR`（置信度失准）
是本系统特别关注的类型——它们最容易被普通聊天系统忽略。
