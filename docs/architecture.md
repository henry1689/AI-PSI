# 架构说明

> 本文说明 AI-PSI Cognitive Runtime V0.1 的整体结构、分层规则与关键数据流。
> 具体决策的**理由**记录在 `docs/adr/`；本文只描述**是什么**。

---

## 1. 这个系统要解决什么

普通聊天机器人是"输入 → 模型 → 输出"。
本系统在中间插入了一个**可追踪、可测试、可纠正的认知过程**：

```
普通聊天机器人：
    用户输入 ──────────────────────→ 模型 ──→ 回答

AI-PSI V0.1：
    用户输入 → 事件 → 关切 → 认知任务 → 检索 → 深度路由
              → 概念/前提/假设 → 证据支持与反对 → 暂定判断
              → 元认知检查 → 停止/继续/等待 → 回答
              → 结构化认知结果持久化 → 反馈 → 经验 → （必要时）改进提案
```

关键区别在于：**中间每一步都留下结构化产物**，
因此可以回答"系统当时为什么这么判断""为什么停下来""哪条证据支持了它"。

---

## 2. 分层结构

```
┌──────────────────────────────────────────────────────────────┐
│  api/                     HTTP 边界（FastAPI）                │  ← 阶段 3
│  路由 / Schema / 错误处理 / 依赖注入                           │
├──────────────────────────────────────────────────────────────┤
│  application/             应用服务：唯一允许发起写入的层        │  ← 阶段 2
│  cognitive_runtime / event_service / memory_service / …       │
├──────────────────────────────────────────────────────────────┤
│  领域逻辑层                                                   │
│  cognition/   认知流水线、状态机、分析器、元认知、回答生成      │  ← 阶段 1/3
│  memory/      检索、排序、写入策略、冲突、生命周期、脱敏        │  ← 阶段 5
│  learning/    错误分类、经验构建、模式发现、提案生成            │  ← 阶段 6
│  reliability/ 预算、反刍检测、置信度、熔断、健康、不变量        │  ← 阶段 3/6
├──────────────────────────────────────────────────────────────┤
│  domain/                  领域对象：纯类型，零 IO              │  ← 阶段 1
│  18 个结构化对象 + 枚举 + 异常                                 │
├──────────────────────────────────────────────────────────────┤
│  providers/  infrastructure/     Ports 的具体实现（Adapter）   │  ← 阶段 2/4
│  LLM Provider / 数据库 / 事件存储 / 向量索引 / 任务队列        │
└──────────────────────────────────────────────────────────────┘
```

---

## 3. 四条硬性依赖规则

违反其中任何一条都视为架构缺陷。

### 规则 1：`domain/` 零依赖

`domain/` 只依赖 `pydantic`（以及标准库）。
**不做 IO、不含 async、不 import 项目内其他包。**

理由：领域对象是全部逻辑的共同语言。它一旦依赖了外部世界，
所有使用它的地方都会被间接耦合，Provider 替换与内存测试都会失效。

### 规则 2：领域逻辑通过 Port 访问外部

`cognition/`、`memory/`、`learning/` **不直接 import**
`infrastructure/` 或 `providers/`，而是依赖 Protocol：

| Port | Adapter（阶段） |
|---|---|
| `LLMProvider` | `MockProvider`(3) / `AnthropicProvider`(4) / `OpenAICompatibleProvider`(4) |
| `MemoryRepository` | `InMemoryMemoryRepository`(3) / `PostgresMemoryRepository`(5) |
| `EventStore` | `InMemoryEventStore`(3) / `PostgresEventStore`(2) |
| `VectorIndex` | `InMemoryVectorIndex`(3) / `PgVectorIndex`(5) |
| `Clock` | `SystemClock` / `FixedClock`（测试） |

**拥有 Port 的层定义 Port，实现层依赖拥有者的接口**（依赖倒置）。
Port 的具体实现由组合根（`api/dependencies.py`，阶段 3）注入。

### 规则 3：只有 `application/` 能发起持久化写入

这条是任务书开发原则第 17 条的落地：
"不允许模型直接写数据库；所有写入必须经过应用服务和领域规则。"

- 模型输出 → 经 Schema 校验 → 变成领域对象 → **交给应用服务** → 才可能落库。
- 认知模块**没有** `session`、`commit`、`save` 之类的入口。
- 每个写入方法同时负责写入事件与投影（ADR-0002）。

### 规则 4：依赖方向永远向内

外层可以 import 内层；内层永远不知道外层存在。
具体表现为 `domain/` 不知道 `application/`，`cognition/` 不知道 `api/`。

---

## 4. 认知回合的数据流

一个回合的完整生命周期：

```
① 用户消息到达
   API 校验 → 生成 user.message.received 事件 → 创建 CognitiveRound(CREATED)

② TRIAGING：关切识别
   ConcernDetector 从事件中提取 0..N 个 Concern
   （无依据的主动问题、为表现聪明的探索、不相关旧记忆、未授权隐私推测 → 过滤掉）
   过滤后为空 → 直接走 D0 直答分支

③ FRAMING：问题框定
   InquiryFramer 把 Concern 转成**可结束的问题**：
   核心问题 / 范围 / 排除范围 / 已知 / 未知 / 需澄清概念 /
   产物类型 / 停止条件 / 重新触发条件

④ RETRIEVING：上下文构建
   ContextBuilder 按主题相关性、时间相关性、来源可靠性、个人作用域、
   当前有效性、冲突信息、敏感性与 Token 预算选择上下文。
   **不是把所有历史塞给模型。**

⑤ 深度路由
   route_depth() 依据确定性规则 + 预算，选定 D0–D4（ADR-0008）

⑥ ANALYZING：分析
   按深度启用：EpistemicAnalyzer（分类为 OBSERVED/SUPPORTED/TENTATIVE/
   CONFLICTING/UNKNOWN/POSSIBLY_OUTDATED/INACCESSIBLE/OUT_OF_CAPABILITY）、
   ConceptAnalyzer、LogicalAnalyzer、CausalAnalyzer、
   DialecticalAnalyzer(D3+)、PhilosophicalAnalyzer(D4)

⑦ DELIBERATING：假设与判断
   HypothesisGenerator（1..4 个，须含非人格化解释，每个带可反驳条件）
   → HypothesisEvaluator（证据支持/反对）
   → JudgmentSynthesizer（暂定结论 / 支持依据 / 最强反证 / 未解决未知 /
     适用范围 / 判断强度 / 修正条件 / 下一认知动作）

⑧ METACOGNITIVE_REVIEW：元认知
   规则层（循环次数、新证据、新推理路径、文本相似度、状态漂移、预算、
   停止条件、输出完整性）+ 模型层（迎合风险、确认偏差、抽象逃逸、
   虚假平衡、忽视反例、不恰当确定）
   → MetacognitiveDecision（CONTINUE/CHANGE_METHOD/NARROW_SCOPE/
     LOWER_CONFIDENCE/REQUEST_EVIDENCE/WAIT/STOP/ESCALATE_TO_RESEARCH）

⑨ SYNTHESIZING → RESPONDING
   ResponsePlanner 决定说什么、藏什么、要不要澄清
   → ResponseRenderer 只生成自然语言，**不得改变 Judgment 的事实内容与置信等级**

⑩ 一致性校验（硬约束）
   最终回答的结论强度 ≤ 内部 Judgment 的结论强度

⑪ COMPLETED：持久化 + 记忆提案
   结构化认知结果落库 → MemoryProposal → WritePolicy → 批准/拒绝/待核验
   → 记录停止原因

⑫ 后续反馈
   user.feedback / user.correction → ExperienceBuilder → 形成经验
   → 同类错误累计达门槛 → ImprovementProposal（**永不自动生效**）
```

---

## 5. 关键设计约束

### 5.1 结构化优先

所有重要认知产物都是 Pydantic 对象，不允许只存在于长文本中。
唯一的自由文本产物是**面向用户的自然语言回答**，且它由 `Judgment` 派生，
不能反过来影响 `Judgment`。

### 5.2 观察、假设、判断必须分离

| 类型 | 允许的内容 | 禁止的内容 |
|---|---|---|
| `Observation` | 看见了什么 | 心理诊断、推断 |
| `Hypothesis` | 可能的解释 | 直接当作事实 |
| `Belief` | 有依据或标记为暂定的信念 | 无依据的断言 |
| `Judgment` | 本次回合的暂定结论 | 无保留的确定结论（存在高可信冲突时） |

例：「用户回复很短」→ Observation；「用户情绪低落」→ Hypothesis。

### 5.3 不保存隐藏思维链

只保存结构化认知摘要：证据、主要理由、反证、判断、修正条件、置信度依据。
供应商返回的 `reasoning` 字段一律丢弃（ADR-0003）。
`ModelInvocationInfo` 只保留 `response_hash` 供审计。

### 5.4 模型输出是不可信输入

模型返回值必须经过：Schema 校验 → 字段限制 → 枚举白名单 → 范围校验。
任一环节失败：有限重试 → 降级或失败。
**绝不产生部分有效的领域对象。**（不变量 16）

---

## 6. 可靠性机制

| 机制 | 位置 | 作用 |
|---|---|---|
| 认知预算 | `reliability/budgets.py` | 限制模型调用数、循环数、假设数、Token、时长（ADR-0008） |
| 状态超时 | `cognition/state_machine.py` | 每个状态有超时（ADR-0008） |
| 反刍检测 | `reliability/repetition_detector.py` | 无新证据/新路径时强制 STOP |
| 熔断 | `reliability/circuit_breaker.py` | Provider 连续失败 → `DEGRADED`（系统健康状态） |
| 乐观锁 | `domain/common.py` 的 `version` | 防止静默覆盖 |
| 幂等键 | `application/` | API 重试不创建重复回合 |
| 不变量断言 | `cognition/constitution.py` | 20 条认知不变量（ADR-0011） |

**降级承诺：** 真实 LLM 不可用时，**不伪造完整认知结果**。
可以返回"当前认知服务降级"，可以让低复杂度任务走规则路径，
但已有事件和状态不得损坏，重试必须有限。

---

## 7. V0.1 与未来

本架构是**沿同一方向向上生长**的地基，不是可抛弃的原型。
后续阶段（长期研究、跨领域综合、个人认知增强、多模型验证、更高级哲理能力）
都在这套分层与不变量之上扩展。

**唯一不可通过配置放宽的是认知宪法**——
它的变更必须是一次显式的代码评审（ADR-0011）。
