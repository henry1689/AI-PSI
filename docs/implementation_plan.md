# 实施计划与进度

> **本文件是阶段进度的唯一真相来源。**
> 每个阶段结束时必须更新此表。
> 相关：ADR-0012（范围边界与目录偏差）

---

## 1. 总原则

**严格按阶段推进，每个阶段结束时仓库必须处于可运行状态。**

> 大型自主编码任务最常见的失败，不是代码写得慢，
> 而是前期接口尚未稳定，Agent 已经热情洋溢地盖到第八层，
> 最后地基在地下轻轻叹气。

因此：

- 每阶段结束**必须**运行完整质量检查（lint + typecheck + test）；
- 每阶段结束**必须**输出阶段完成报告（模板见 §4）；
- **不通过删除测试、降低类型检查或放宽 Schema 来让检查通过**；
- 接口未稳时**不**提前实现下一阶段。

---

## 2. 阶段进度

| 阶段 | 内容 | 状态 | 完成日期 |
|---|---|---|---|
| **0** | 架构与文档 | ✅ 完成 | 2026-09-17 |
| **1** | 项目骨架与领域对象 | ✅ 完成 | 2026-09-17 |
| **2** | 数据库、事件存储、认知状态机 | ✅ 完成 | 2026-09-17 |
| **3** | Mock LLM 与认知流水线（场景 A–J） | ✅ 完成 | 2026-09-18 |
| **4** | 真实 LLM Provider（DeepSeek） | ✅ 完成 | 2026-09-18 |
| **5** | 长期记忆（PostgreSQL + pgvector） | ✅ 完成 | 2026-09-18 |
| 6 | 反馈、经验与改进提案 | ⬜ 未开始 | — |
| 7 | 评测与回放 | ⬜ 未开始 | — |
| 8 | 完整验收与交付 | ⬜ 未开始 | — |

---

## 3. 各阶段交付与验收

### 阶段 0：架构与文档 ✅

**交付**：README、architecture、cognitive_constitution、domain_model、state_machine、
prompt_contracts、evaluation、security、implementation_plan、risks、12 篇 ADR。

**验收**：文档与任务书无明显冲突；清楚标注 V0.1 与 L5 的区别；
明确不保存完整隐藏思维链；明确 Proposal 不自动生效。
→ 见 `docs/adr/0012` 的偏差登记表。

### 阶段 1：项目骨架与领域对象 ✅

**交付**：Python 项目、配置系统、领域 Schema、枚举、基础异常、
单元测试、属性测试、CI、Docker Compose。

**验收命令**：`make lint` / `make typecheck` / `make test` / `make policy` 全部通过。

**交付清单**：

| 模块 | 内容 |
|---|---|
| `config.py` | pydantic-settings；密钥一律 `SecretStr`；`redacted_summary()` 供安全日志 |
| `domain/` | 19 个领域对象 + 枚举全集 + 结构化异常层次 |
| `cognition/state_machine.py` | 14 状态权威转移表、禁止转移校验、状态超时表、元认知决策映射 |
| `cognition/constitution.py` | 20 条不变量元数据 + 不变量断言 + 记忆写入白名单 |
| `tests/unit/` | 16 个文件，覆盖全部领域对象与状态机 |
| `tests/property/` | 2 个文件，Hypothesis 属性测试 |
| `.github/workflows/ci.yml` | Python 3.12/3.13 矩阵 + 静态检查；预留阶段 2 的 PG service container |

**实测验收结果**：

| 检查 | 结果 |
|---|---|
| `make lint` | ✅ All checks passed（0 error） |
| `make typecheck` | ✅ mypy strict，54 个文件 0 error |
| `make test` | ✅ 全部通过 |
| `make policy` | ✅ 总体 **99%**，`domain/` 与 `cognition/` 各模块 **94–100%**（门槛 85% / 75%） |
| 数据库连通 | ✅ PostgreSQL 16.15 + pgvector 0.8.6 |

**阶段 1 实现中发现的三个任务书缺口**：见 ADR-0012 §4.1（G1 缺失的取消事件、
G2 未定义的 Situation、G3 "必须有反证"与"禁止虚假平衡"的冲突）。

### 阶段 2：数据库、事件存储与状态机 ✅

**交付**：PostgreSQL 模型、Alembic 迁移、Repository、Unit of Work、
Event Store、状态机（接仓储）、幂等支持、集成测试。
**引入依赖**：`sqlalchemy 2.0.54`、`alembic 1.20.0`、`structlog 26.1.0`。

**验收条件与结果**：

| 任务书 §18 验收条件 | 结果 |
|---|---|
| 可以创建和回放事件 | ✅ 集成测试覆盖；回放还额外**审计**转移合法性 |
| 非法状态转换全部拒绝 | ✅ 且拒绝发生在**任何写入之前**（有测试断言不留事件、不推进版本） |
| 事务失败不留下半成品数据 | ✅ 未提交即回滚；异常路径、提前 return、忘记提交全覆盖 |

**交付清单**：

| 模块 | 内容 |
|---|---|
| `infrastructure/db/models.py` | 三张表 + 10 条 CHECK 约束（含不变量 19/20 的 DB 级强制） |
| `infrastructure/db/mappers.py` | 领域对象 ↔ ORM 的**显式**映射（ADR-0006） |
| `infrastructure/db/repositories.py` | 回合仓储（乐观锁）+ 幂等键存储（唯一约束仲裁） |
| `infrastructure/db/unit_of_work.py` | 事务边界；未提交即回滚 |
| `infrastructure/event_store.py` | 只追加事件存储；按 `sequence` 排序 |
| `infrastructure/asyncio_compat.py` | Windows 事件循环兼容（ADR-0014） |
| `infrastructure/logging.py` | structlog + 递归脱敏 |
| `application/ports.py` | 存储相关 Port（Protocol） |
| `application/round_service.py` | 回合创建与状态转移（状态机+事件+投影，同一事务） |
| `application/replay_service.py` | 历史回放（只读） |
| `cognition/projection.py` | 事件流 → 回放结果（纯函数，兼作一致性审计） |
| `migrations/` | Alembic 迁移（连接串从环境变量注入，不进版本库） |

**实测验收结果**：

| 检查 | 结果 |
|---|---|
| `make lint` | ✅ 0 error |
| `make typecheck` | ✅ mypy strict，76 个文件 0 error |
| `make test` | ✅ **543 passed**（单元 + 属性 + 集成） |
| `make policy` | ✅ 总体 **97%**；`domain/`+`cognition/` **99%** |
| 迁移 | ✅ `alembic upgrade head` 通过；10 条 CHECK 约束就位 |
| 幂等 / 乐观锁 | ✅ 集成测试覆盖（含并发语义：不同请求体报冲突而非静默返回） |

**测试期间发现并修复的真实缺陷**：
1. **日志脱敏漏掉嵌套结构** —— 嵌套层只递归、不判键名，
   `{"request": {"headers": {"authorization": ...}}}` 里的密钥原样入日志。
   已修，并有回归测试钉死。同时补上连字符键名（`X-Api-Key`）的归一化。
2. **映射器漏了两个枚举转换** —— mypy 在 `row_to_event` / `row_to_round` 抓到。
3. **生成的迁移缺少 `postgresql` 导入** —— 用了 `postgresql.JSONB` 却没 import，
   会在 `alembic upgrade head` 时 NameError。已在模板中修好，避免后续迁移重犯。

### 阶段 3：Mock LLM 与认知流水线 ✅

**交付**：Provider Protocol、Mock Provider、Prompt Registry、
关切/框定/深度/认知状态/假设/判断/元认知/回答规划与渲染、
完整场景 A–J 集成测试、API 路由。
**本阶段创建**：`providers/`、`prompts/`、`reliability/`、`memory/`、`api/`、
`main.py`、`application/cognitive_runtime.py`；并落地 ADR-0009 的**内存适配器**。
**新增依赖**：`fastapi`、`uvicorn`。

**验收条件与结果**：

| 任务书 §18 验收条件 | 结果 |
|---|---|
| 不使用外部 API 也能完整运行 | ✅ 默认 `Mock` Provider + 内存适配器，`storage_backend=memory` 下零外部依赖跑通全流程 |
| **场景 A～J 全部通过** | ✅ 16 个场景用例全绿（含每个场景的多条结构断言） |
| 循环永不超预算 | ✅ 预算在**调用之前**扣减；可选模块不足则跳过并记录；强制模块始终保留额度 |

**交付清单**：

| 模块 | 内容 |
|---|---|
| `providers/base.py` | `LLMProvider` Protocol、`LLMMessage`、`ModelConfig`、`InvocationContext` |
| `providers/mock.py` | 确定性规则引擎 + 脚本化响应 + 故障注入 |
| `providers/gateway.py` | 预算记账、有限重试、超时映射、**只记录响应哈希**的调用审计 |
| `providers/registry.py` | 按配置装配 Provider；未实现的名称显式报错 |
| `prompts/payload.py` | 结构化输入的编码与提取；**反引号转义**阻断提示词注入 |
| `prompts/registry.py` | 契约校验（版本 / changelog / 模板结构 / 占位符白名单）、渲染、长度上限 |
| `prompts/versions.py` + 11 个模板 | 11 个 Prompt 任务的契约与模板 |
| `cognition/depth_router.py` | 确定性深度路由 + 预算降级 |
| `cognition/orchestrator.py` | 模块矩阵（纯函数）；与预算表、标称调用数三者不许漂移 |
| `cognition/context_builder.py` | 上下文选择；冲突与失效材料**不参与裁剪** |
| `cognition/epistemic_analyzer.py` | 八类认知状态分类（确定性） |
| `cognition/{concern_detector,inquiry_framer,hypothesis_generator,logical_analyzer,causal_analyzer,concept_analyzer,dialectical_analyzer,philosophical_analyzer,judgment_synthesizer,metacognition,response_renderer}.py` | 11 个模型调用模块 |
| `cognition/hypothesis_evaluator.py`、`response_planner.py` | 确定性模块（评估、回答规划） |
| `reliability/{budgets,repetition_detector,confidence}.py` | 预算记账、反刍信号、置信度上限 |
| `memory/write_policy.py` | 四档写入裁决，默认拒绝 |
| `infrastructure/in_memory/` | 事件存储 / 回合仓储 / 幂等键 / 记忆的内存实现 |
| `application/{cognitive_runtime,artifact_service,memory_service}.py` | 回合执行器、产物记录、记忆读写 |
| `application/ports.py` | 新增 `MemoryRepository` Port |
| `cognition/projection.py` | 新增 `project_artifacts()`（只读审计视图） |
| `api/` + `main.py` | §12.1 与 §12.5 的路由、错误处理、依赖装配 |

**实测验收结果**：

| 检查 | 结果 |
|---|---|
| `make lint` | ✅ 0 error |
| `make typecheck` | ✅ mypy strict，**142 个文件** 0 error |
| `make test` | ✅ **869 passed, 11 skipped** |
| `make policy` | ✅ 总体 **95%**；`domain/`+`cognition/` **98%** |
| 契约测试 | ✅ 同一组断言跑内存与 PostgreSQL **两个实现** |
| 场景 A–J | ✅ 全部通过 |

**测试期间发现并修复的真实缺陷**：

1. **🔴 状态机不允许 `DELIBERATING → SYNTHESIZING` 直达。**
   元认知被跳过（D0 或预算不足）时，回合会撞上一个非法转移。
   修法不是加一条边，而是让**规则层元认知复核**始终执行——
   它不花钱，并且照样留下"为什么停下来"的记录。
2. **🔴 分析模块的模型调用没有地方记录。** 逻辑/因果/辩证/哲理四个模块
   的产出被塞进判断负载，它们的 `model` 与 `prompt_version` **无处可查**，
   直接违反不变量 18。新增事件类型 `cognition.analysis.completed`（ADR-0015 §3.2）。
3. **🔴 四种终态事件从未被发出。** 任务书 §5.2 列出了
   `cognitive_round.completed/failed/suspended/cancelled`，
   阶段 2 却把它们统一写成了 `state_changed`——是一份死的词汇表。
4. **契约测试抓到两处 PostgreSQL 实现与契约不符**：
   事件存储把 `IntegrityError` 泄漏给调用方（应为 `ConflictError`）；
   幂等键 `bind` 在 key 未占位时静默成功（此后每次重试都会得到
   `CONFLICT` 而不是 `REPLAY`，根因却无处记录）。
   **这两处都不是阶段 3 引入的**——它们一直存在，只是在有第二个实现之前无从对照。
5. **`Settings` 的六个 `budget_max_*` 字段是死配置**（无任何读取方），已删除。
   死配置比没有配置更糟：它看起来可调，却不会有任何作用。

**与阶段 0 文档的偏差**（详见 ADR-0015）：13 个 Prompt 任务 → 11 个
（`response_planner` 改为确定性代码）；预算表按实测重标定；
`epistemic_analyzer` 与 `hypothesis_evaluator` 改为确定性模块。

### 阶段 4：真实 LLM Provider ✅

**交付**：OpenAI 兼容 Provider（覆盖 DeepSeek）、Provider 级熔断、
结构化输出修复、token 用量统计、真实模型端到端测试。
**新增依赖**：`httpx` 提为主依赖。
**本阶段创建**：`providers/{response,parsing,http,resilience,openai_compatible}.py`、
`reliability/circuit_breaker.py`。

⚠️ **Anthropic Provider 未实现**（用户指定用 DeepSeek；一个无法测试也无法
跑通的实现只是空壳，ADR-0016 §1）。

**验收条件与结果**：

| 任务书 §18 验收条件 | 结果 |
|---|---|
| Provider 可配置切换 | ✅ `AI_PSI_LLM_PROVIDER`：`mock` / `deepseek` / `openai_compatible` |
| 真实 Provider 不影响领域层 | ✅ 领域层零改动；提供方只出现在 `providers/` 与组合根 |
| 无 API Key 时自动使用 Mock 或明确失败 | ✅ **明确失败**（选后者，ADR-0016 §6） |
| 解析异常不会污染状态 | ✅ 可选模块降级 + 留痕；强制模块失败时可诊断（不变量 16、20） |
| **真实模型端到端** | ✅ **D0/D2/D4 三类问题均跑通真实回合并给出真实回答** |

**交付清单**：

| 模块 | 内容 |
|---|---|
| `providers/response.py` | `ProviderResponse[T]` / `TokenUsage`（含推理 token 计数） |
| `providers/parsing.py` | 从模型文本里提取 JSON：去代码块 → 括号配平扫描；报告修复方式 |
| `providers/http.py` | HTTP 错误 → 领域异常的映射（429 / 5xx / 4xx / 超时 / 连接） |
| `providers/openai_compatible.py` | `/chat/completions` 调用、JSON 模式、截断判定、用量读取 |
| `providers/resilience.py` | 熔断装饰器（只把"供应商不健康"类失败计入） |
| `providers/registry.py` | Provider 工厂与预置（DeepSeek 的 base_url 与默认模型） |
| `reliability/circuit_breaker.py` | 纯状态机熔断器（时钟可注入） |
| `tests/integration/test_live_provider.py` | 真实模型端到端（`live` 标记，默认跳过） |

**实测验收结果**：

| 检查 | 结果 |
|---|---|
| `make lint` | ✅ 0 error |
| `make typecheck` | ✅ mypy strict，**164 个文件** 0 error |
| `make test` | ✅ **968 passed, 16 skipped** |
| `make policy` | ✅ 总体 **95%**；`domain/`+`cognition/` **98%** |
| `make test-live` | ✅ **5 passed**（真实 DeepSeek，34 秒） |
| 真实回合（D0 / D2 / D4） | ✅ 全部完成并给出真实回答 |

**真实模型暴露的 6 个缺陷**（**没有一个能靠 Mock 发现**，详见 ADR-0016 §9）：

1. 🔴 **模型名写死** —— 未显式配置时网关把 `"mock-model-v1"` 发给真实供应商，
   DeepSeek 直接 400，回合在建关切阶段就失败。
2. 🔴 **关切检测对直接提问返回空列表** —— 用户提问却得到 `NO_CONCERN_DETECTED`，
   拿不到任何回答。提示词升至 v1.1.0。
3. 🔴 **截断被误报成 JSON 语法错误** —— 诊断指向不存在的问题，而且可重试，
   于是同样的上限被反复撞上、预算被烧掉。
4. 🔴 **可选分析模块失败拖垮整个回合** —— 用户只差最后一步就能拿到回答。
5. 🔴 **`CHANGE_METHOD` 路径耗尽强制尾部额度** —— 回合以 `BudgetExhaustedError` 失败。
6. 🔴 **推理预留有两个默认值** —— 配置层静默覆盖 Provider 层，
   表现为"改了默认值却毫无效果"。

**实测成本**（`deepseek-v4-flash`，一次完整回合）：

| 深度 | 调用数 | 输入 token | 输出 token | 其中推理 |
|---|---|---|---|---|
| D0 | 4 | ~3 700 | ~1 800–2 200 | ~600–1 000 |
| D2 | 8–10 | ~6 000–13 500 | ~6 400–18 000 | ~3 700–12 600 |
| D4 | 13 | ~21 400 | ~33 100 | **~21 800** |

⚠️ **推理 token 占输出的大头。** 后续项：提示词没有约束输出规模
（`logical_analyzer` 要求八项检查却不限长度），瘦身需评测支撑，列为阶段 7。

### 阶段 5：长期记忆 ✅

**交付**：Memory Repository（PostgreSQL + pgvector）、向量 Provider、
WritePolicy（阶段 3 已交付）、冲突/过期/取代、用户纠正、删除与导出、用户隔离测试。
**新增依赖**：`pgvector`（Python 包，仅提供 SQLAlchemy 的 `Vector` 类型）。
**本阶段创建**：`providers/embeddings.py`、
`memory/{retrieval,ranking,conflict_detection,lifecycle,redaction}.py`、
`infrastructure/db/memory_repository.py`、`api/routes/memories.py`。

**验收条件与结果**（任务书 §18 阶段 5）：

| 验收条件 | 结果 |
|---|---|
| 用户 A 无法检索用户 B 的私有记忆 | ✅ 过滤写在 SQL 的 `WHERE` 里，取回之后**不再过滤**；结果逐条过 `assert_memory_retrievable_by` 防御性断言 |
| superseded 记忆不默认生效 | ✅ 状态过滤在 SQL；**索引行被物理删除** |
| 删除后不再出现在向量结果中 | ✅ 不是"查询恰好带了过滤"，而是索引表里那一行真的没了——可直接查表断言 |
| pgvector 检索 | ✅ 512 维向量 + HNSW 余弦索引，真实数据库上验证 |

**交付清单**：

| 模块 | 内容 |
|---|---|
| `providers/embeddings.py` | `EmbeddingProvider` 协议 + 本地确定性实现（默认）+ OpenAI 兼容实现 |
| `infrastructure/db/models.py` | `memories` 与 `memory_embeddings` 两张表（含 HNSW 索引与 12 条 CHECK） |
| `infrastructure/db/memory_repository.py` | `SqlAlchemyMemoryRepository`：向量索引维护是仓储的职责 |
| `memory/retrieval.py` | 相似度、召回规模、零向量判定、词面重合度（两实现共用） |
| `memory/ranking.py` | 综合排序：相关度 + 时效；冲突不降权；同分有确定兜底 |
| `memory/conflict_detection.py` | 重复（**确定**判据）与疑似冲突（**线索**） |
| `memory/lifecycle.py` | 过期判定与保留策略说明 |
| `memory/redaction.py` | 审计脱敏（不含正文）与导出（含正文） |
| `application/memory_service.py` | 一个事务内完成写入/纠正/删除 + 审计；导出与用户数据删除；`reindex` |
| `api/routes/memories.py` | §12.3 的五条路由 |

**实测验收结果**：

| 检查 | 结果 |
|---|---|
| `make lint` | ✅ 0 error |
| `make typecheck` | ✅ mypy strict，**175 个文件** 0 error |
| `make test` | ✅ **1156 passed, 5 skipped** |
| `make policy` | ✅ 总体 **96%**；`domain/`+`cognition/` **98%** |
| 契约测试 | ✅ 记忆仓储的内存实现与 PostgreSQL 实现跑**同一组断言**（阶段 3 预留的占位类直接启用，断言一行未改） |
| 真实数据库 | ✅ 向量维度、HNSW 索引、删除传播、外键级联、`IS NOT DISTINCT FROM` 空值语义均在真实 PostgreSQL 上验证 |

**本阶段闭合的既有债务**：

* **ADR-0015 §5**：记忆写入与事件写入不在同一事务 → 记忆仓储挂进
  `UnitOfWork`，`TestAtomicity` 用"一写事件就失败"的存储作为直接证据。

**本阶段明确不做的事**（均登记于 ADR-0017 §7）：

* `GET /users/{user_id}/beliefs`（信念不是记忆，需要另建判断投影）；
* `RetentionPolicy.SESSION` 的会话级失效（V0.1 没有"会话"这一等对象）；
* 语义级反刍阈值重标定（需要评测数据，阶段 7）；
* 后台过期扫描（读时判定 + 显式调用，避免时序不确定）。

⚠️ **V0.1 没有认证层**（risks.md R40）：作用域过滤防的是"代码写错导致的
串号"，不是"恶意调用者"。这条边界写在路由的模块文档里。

### 阶段 6：反馈、经验与改进提案

**交付**：Feedback API、Experience Builder、Error Classifier、
Pattern Detector、ImprovementProposal、Proposal 评估接口、自动生效硬禁令。
**本阶段创建**：`learning/`、`reliability/` 的剩余部分。

**验收**：单次普通经验不能推广；三次同类错误可生成 Proposal；
Proposal 始终需要外部审批。

### 阶段 7：评测与回放

**交付**：50+ Golden Cases、Eval Runner、指标、历史回放、
Markdown/JSON 报告、Baseline 对照接口。

**验收**：可比较两个 Prompt 版本；可统计认知质量与成本；
回放不覆盖原始事件；评测结果具有运行版本信息。

⚠️ **阶段 4 交接下来两件明确属于本阶段的工作**（都有实测数据支撑）：

1. **提示词输出规模瘦身**（ADR-0016 §10、`risks.md` R38）。
   一次 D4 回合实测输出 33k token，其中推理占 22k——因为模板要求
   `logical_analyzer` 做八项检查却不限定每项多长，模型在自由发挥。
   在模板里限定列表长度与条数能显著降低成本，但**会改变认知输出质量**，
   必须先有评测数据回答"有多少反例是被'简洁'砍掉的"。
2. **反刍阈值的重新标定**（`risks.md` R32）。
   当前 0.85 的词面阈值接上真实模型后只能捕捉逐字重复；
   真实模型的两次判断会改写措辞。需要评测数据决定阈值，
   或改用阶段 5 的向量检索做语义相似度。

### 阶段 8：完整验收与交付

**交付**：完整 README、API 文档、架构图、数据字典、本地启动说明、
测试报告、已知限制、下一版本路线图、示例对话与回放报告。

---

## 4. 阶段报告模板

每阶段结束按此模板输出（任务书 §20）：

```markdown
# 阶段 N 完成报告

## 1. 本阶段完成内容
## 2. 新增或修改文件（路径：作用）
## 3. 架构决策（决策 / 原因 / 替代方案 / 影响）
## 4. 测试结果（单元 / 集成 / 属性 / 覆盖率 / Lint / 类型检查）
## 5. 未完成内容
## 6. 已知问题与风险
## 7. 与任务书的偏差（偏差 / 原因 / 后续处理）
## 8. 下一阶段计划
```

---

## 5. 每阶段开工清单

1. 读 `docs/AI_PSI_V0_1_TASK_SPEC.md` 对应章节与本文件；
2. 检查本阶段是否有**新 ADR**需要（涉及新依赖、新接口、任务书未定处）；
3. 创建该阶段需要的包（**不提前创建后续阶段的包**，ADR-0012）；
4. 实现 → 测试 → `make check`；
5. 更新本文件的进度表与 ADR-0012 的偏差表；
6. 按 §4 模板输出阶段报告。

---

## 6. 当前已知的未结事项

| 事项 | 状态 | 处理 |
|---|---|---|
| `make` 本机安装 | ✅ 已解决 | `winget install ezwinports.make`（GNU Make 4.4.1），需重启终端生效 |
| 本机无法直连 Docker Hub | ✅ 已解决 | compose 支持 `AI_PSI_PG_IMAGE` 覆盖；本地 `.env` 指向镜像源 |
| ruff 会格式化 Markdown 代码块 | ✅ 已解决 | `pyproject.toml` 排除 `**/*.md`（ADR-0007） |
| 记忆写入与事件写入不在同一事务 | ✅ 阶段 5 已闭合 | 记忆仓储纳入 `UnitOfWork`，四步同事务；`TestAtomicity` 为直接证据（ADR-0017 §3） |
| 反刍检测是**词面**相似度 | ⚠️ 阶段 3 已知局限 | 只捕捉逐字重复；改写过的同一论点不触发。语义级检测待阶段 5 的向量检索（`risks.md` R32） |
| 任务书 20 项内部冲突 | ✅ 已处理 | 4 项实质矛盾见 ADR-0006/0009/0010/0012，16 项缺口默认值见 ADR-0008/0012 |
