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
