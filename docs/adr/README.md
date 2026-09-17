# 架构决策记录（ADR）

本目录记录 AI-PSI Cognitive Runtime 的架构决策。
每条 ADR 保留**决策当时**的背景、替代方案与影响——
目的是让后来者能理解"为什么是这样"，而不只是"是什么"。

**格式**：状态 / 日期 / 相关 → 背景 → 决策 → 替代方案与取舍 → 影响

**规则**：
- ADR 一旦接受，**不修改内容**。要改变决策就新增一条 ADR 并标注取代关系。
- 任务书自身的矛盾与缺口的处理，同样记录为 ADR（不就地改任务书）。

---

## 索引

### 任务书指定（5 篇）

| # | 标题 | 要点 |
|---|---|---|
| [0001](0001-architecture-style.md) | 架构风格 | 轻量 DDD + 事件记录 + Ports & Adapters；四条硬性依赖规则 |
| [0002](0002-event-and-state-model.md) | 事件与状态模型 | 事件追加不改；状态为投影；同事务写入；乐观锁 |
| [0003](0003-llm-provider-abstraction.md) | LLM Provider 抽象 | Protocol + 可替换 Adapter；结构化优先；不存思维链 |
| [0004](0004-memory-policy.md) | 记忆策略 | 写入白名单默认拒绝；读取作用域硬隔离；纠正版本链；删除必传播 |
| [0005](0005-no-automatic-strategy-promotion.md) | 策略不得自动生效 | `ProposalStatus` 无 `ACTIVE`；单次经验不产生提案 |

### 冲突处理与默认值（7 篇）

| # | 标题 | 处理的矛盾/缺口 |
|---|---|---|
| [0006](0006-entity-metadata-inheritance.md) | EntityMetadata 继承 | **C1**：§5.1 要求 vs §5.3–5.12 未继承 |
| [0007](0007-python-and-toolchain.md) | Python 与工具链 | 3.13 选型；mypy strict；ruff 必须排除 `*.md` |
| [0008](0008-depth-routing-budgets-and-timeouts.md) | 深度路由、预算与超时 | 缺口：各级预算差异、状态超时数值、元认知调用计数 |
| [0009](0009-in-memory-adapters-for-early-stages.md) | 早期阶段内存适配器 | **C2**：阶段 3 场景 F/J 依赖阶段 5 交付物 |
| [0010](0010-epistemic-action-vs-metacognitive-decision.md) | 两个决策枚举的划分 | **C12**：语义重叠且关系未定义 |
| [0011](0011-constitution-as-code.md) | 认知宪法即代码 | 缺口：Markdown 不可测试、不可强制 |
| [0012](0012-v0-1-scope-boundaries.md) | 范围边界与目录偏差 | **C13** + 全量目录偏差登记 + 8 项缺口默认值 |

### 阶段 2 实现决策（2 篇）

| # | 标题 | 要点 |
|---|---|---|
| [0013](0013-persistence-layer.md) | 持久化层设计 | 异步 SQLAlchemy + psycopg3；事件按 `sequence` 排序；不变量下沉到 DB CHECK；幂等靠唯一约束仲裁；乐观锁用显式 UPDATE |
| [0014](0014-cross-platform-and-test-database.md) | 跨平台陷阱与测试库隔离 | Windows 事件循环；`.ini` 必须 ASCII；测试库独立派生 + 拒绝与开发库相同；`NullPool`；集成测试分层 |

### 阶段 3 实现决策（1 篇）

| # | 标题 | 要点 |
|---|---|---|
| [0015](0015-stage-3-cognitive-pipeline.md) | 阶段 3 认知流水线 | 11 个模型任务 + 2 个确定性模块；预算表按实测重标定；D0 走规则层元认知复核；新增 `cognition.analysis.completed` 事件；终态具名事件；契约测试抓到两处 PostgreSQL 不一致；记忆不进 `UnitOfWork`；API 同步执行并返回真实终态 |

### 阶段 4 实现决策（1 篇）

| # | 标题 | 要点 |
|---|---|---|
| [0016](0016-real-llm-provider.md) | 真实 LLM Provider | 选 DeepSeek（OpenAI 兼容），Anthropic 延后；协议返回值改为 `ProviderResponse[T]` 以带回 token 用量；`reasoning_content` 只记数量不记内容；截断在解析前判定且不可重试；可选模块失败降级并留痕；缺 Key 明确失败不回落；**真实模型暴露的 6 个缺陷**；成本实测数据 |
| [0017](0017-long-term-memory.md) | 长期记忆（阶段 5） | 向量来源：本地确定性实现（默认，**非语义**）+ OpenAI 兼容实现；向量单独一张表换取不变量 15 的结构性保证；**记忆仓储挂进工作单元**闭合 ADR-0015 §5 的债务；重复（确定）与冲突（线索）分开；审计不留正文、导出含正文；`/beliefs` 明确暂缓 |

---

## 任务书内部矛盾总览

**4 项实质矛盾**（不处理则无法实现）：

| # | 矛盾 | ADR |
|---|---|---|
| C1 | `EntityMetadata` 要求与 13 个类定义不一致 | 0006 |
| C2 | 阶段 3 验收依赖阶段 5 交付物 | 0009 |
| C12 | `EpistemicAction` 与 `MetacognitiveDecision` 语义重叠 | 0010 |
| C13 | `DEGRADED` 不在回合状态枚举中 | 0012 |

**16 项缺口**（有默认解）：见 ADR-0008 与 ADR-0012。

### 阶段 5 新发现的缺口（见 ADR-0017）

| # | 缺口 | 处理 |
|---|---|---|
| G8 | §12.3 列出 `GET /users/{user_id}/beliefs`，但信念只存在于事件流里、不是记忆 | **本阶段不实现**，由 `test_beliefs_route_is_not_shipped_in_stage5` 钉住可见（ADR-0017 §7） |
| G9 | `RetentionPolicy.SESSION` / `UNTIL_SUPERSEDED` 需要"会话"这个 V0.1 里不存在的一等对象 | 导出结果里**直说尚未实现**，不描述做不到的规则（ADR-0017 §7） |
| G10 | §10.1 要求"重复和冲突检查"，但两者确信程度差一个数量级 | 重复用**确定**判据并拒绝写入；冲突只产出**线索**，绝不写进 `contradicts_ids`（ADR-0017 §4） |
| G11 | §14 的不变量 15 只说了"不得出现在检索结果中"，没说索引本身要不要动 | 向量独立成表，删除是一次真实 DELETE，可直接查表断言（ADR-0017 §2） |

### 阶段 3 新发现的缺口（见 ADR-0015）

| # | 缺口 | 处理 |
|---|---|---|
| G4 | §9 的四个分析模块在 §5.2 的事件清单里**没有任何对应事件** | 新增 `cognition.analysis.completed`（事件总数 32 → 33） |
| G5 | §5.2 列出的四种回合终态事件在阶段 2 从未被发出（死词汇表） | 终态额外写一条具名事件，同一事务内 |
| G6 | §12.1 的响应示例把 `status` 写成固定的 `"CREATED"`，但 V0.1 没有任务队列 | 同步执行并返回**真实终态**，偏差登记在 ADR-0015 §6 |
| G7 | `response_planner` 被 §4 目录列为 Prompt 任务，但它的每一项决策都能从 `Judgment` 直接读出 | 改为确定性代码（ADR-0015 §1.1） |
