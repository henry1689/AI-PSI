# 能力证据矩阵（阶段 0–6）

> **本文件回答一个问题：这个仓库里声称成立的能力，哪些是**真的能从外部触达并观察到**的？**
>
> 建立于阶段 6.5，基线 commit `93727b4`（`5e9c7f0` 记为**候选修复基线**，非正式完成）。
>
> 🔴 **本文件不是成绩单。** 它的主要产出是"哪些格子填不出内容"——
> 那些格子就是阶段 6.5 后续各节要处理的对象。

---

## 一、证据等级定义

**只有 E3 与 E4 才算"能力成立"。** E0–E2 一律记为"未成立"。

| 等级 | 含义 | 判定条件 |
|---|---|---|
| **E0 无** | 能力在仓库中不存在 | 代码里找不到 |
| **E1 仅测试** | 代码存在、被测试调用，但**生产路径到不了** | `src/` 内无调用者，或调用者自身不可达 |
| **E2 生产可达** | 有生产入口、调用链真实成立、持久化副作用可观察 | 但**没有**真实 API + 真实 PostgreSQL 的黑盒验收 |
| **E3 黑盒验过** | 有黑盒端到端测试，跑在真实 HTTP API + 真实 PostgreSQL 上并**已通过** | 测试必须走 `ASGITransport`/真实服务 + `storage_backend=postgres` |
| **E4 抗对抗** | E3 + 有针对性的反例测试，证明"试图绕过会失败" | 反例必须是**实测行为**，不是名字扫描 |

### 填写规则（违反即本矩阵失效）

1. **"代码存在"不是证据。** 一个类写在那里而无人调用，等于没有这个能力。
2. **"覆盖率"不是证据。** 覆盖一个不可达的函数不产生任何可信性。
3. **"测试名称"不是证据。** 测试名叫 `test_no_path_to_active` 但只做字符串搜索，
   它证明的是"源码里没有那个词"，不是"那条路径走不通"。
4. **"文档里写了"不是证据。** ADR 与 README 记录的是**意图与当时的观察**，
   不是**当下可复现的事实**。两者不一致时以本矩阵为准，并把文档差异记为待办。
5. **手工验证不是证据。** 一次跑过、没固化成测试的观察，在下一次改动后即失效。

---

## 二、生产入口总表（§一.3 的"正式生产入口"列只从这里取）

| 类别 | 入口 | 位置 |
|---|---|---|
| 进程入口 | `python -m ai_psi.main` → uvicorn | `src/ai_psi/main.py` |
| 组合根 | `build_container()`（含 `assert_structural_invariants()`） | `src/ai_psi/container.py:108` |
| HTTP API | 7 个 router，共 24 个端点 | `src/ai_psi/api/routes/` |
| 运维脚本 | `scripts/bootstrap_db.py`（**仅**校验连通 + vector 扩展） | `scripts/` |
| CLI | **不存在** | — |
| worker / 后台任务队列 | **不存在**（任务书 §12.1：回合在请求内同步执行完毕） | — |

**HTTP 端点清单**（24 个）：

| 方法 | 路径 | 路由文件 |
|---|---|---|
| POST | `/conversations` | `conversations.py` |
| POST | `/conversations/{id}/messages` | `conversations.py` |
| GET | `/cognitive-rounds/{id}` | `cognitive_rounds.py` |
| GET | `/cognitive-rounds/{id}/response` | `cognitive_rounds.py` |
| GET | `/cognitive-rounds/{id}/summary` | `cognitive_rounds.py` |
| GET | `/users/{user_id}/memories` | `memories.py` |
| POST | `/memories/{id}/correct` | `memories.py` |
| DELETE | `/memories/{id}` | `memories.py` |
| POST | `/users/{user_id}/export` | `memories.py` |
| DELETE | `/users/{user_id}/data` | `memories.py` |
| POST | `/cognitive-rounds/{id}/feedback` | `feedback.py` |
| GET | `/improvement-proposals` | `proposals.py` |
| GET | `/improvement-proposals/{id}` | `proposals.py` |
| POST | `/improvement-proposals/{id}/evaluate` | `proposals.py` |
| POST | `/improvement-proposals/{id}/approve-for-manual-trial` | `proposals.py` |
| POST | `/improvement-proposals/{id}/reject` | `proposals.py` |
| POST | `/replay/cognitive-rounds/{id}` | `replay.py` |
| GET | `/health/live`、`/health/ready`、`/health/cognitive` | `health.py` |

---

## 三、能力矩阵

> 列定义：**入口** = 正式生产入口 ｜ **调用链** = 真实调用链 ｜ **副作用** = 持久化副作用
> ｜ **单测/集成/黑盒** = 对应层级的测试是否存在且有效 ｜ **反例** = 对抗性反例
> ｜ **等级** = 当前证据等级 ｜ **残余风险**

### 阶段 0 — 架构与宪法

| # | 能力声明 | 入口 | 调用链 | 副作用 | 单测 | PG 集成 | 黑盒 | 反例 | 等级 | 残余风险 |
|---|---|---|---|---|---|---|---|---|---|---|
| **C0.1** | 认知宪法以代码常量存在，指纹可查 | `GET /health/cognitive` | `health.py:108` → `constitution_fingerprint()` → `cognition/constitution.py` | 无 | `test_constitution.py` | 不适用 | ❌（`tests/api` 全部用内存后端） | ❌ | **E2** | 指纹变化只降级健康度，不阻断进程；"14 节不变量"中仅 3 条有运行期检查 |
| **C0.2** | 提案永不自动生效（枚举无该值 + 服务无该方法 + DB CHECK + 运行期自检） | `build_container()` + `GET /health/cognitive` | `container.py:127` → `assert_structural_invariants` → `_check_i11` | 无 | `test_invariants_selfcheck.py`、`test_experiences_proposals.py` | 🔴 **无** | ❌ | ⚠️ **仅名字扫描**（见下） | **E2** | ① `ck_improvement_proposals_status_valid` 在迁移里存在，但**全仓库没有任何测试触发它**——ADR-0018/0019 与 README 写的"实测被拒绝"是一次性手工验证，未固化。② 应用 Enum 与 DB CHECK 之间**无一致性测试**：往 Enum 加成员不会让任何测试变红 |

> **C0.2 的对抗性反例实况**：`tests/unit/test_proposal_service.py::TestNoPathToActive`
> 的两条断言按**方法名字符串**搜索服务上有没有 `activate`/`promote`/`publish`/`deploy`/`apply`。
> 它拦不住 `def make_live(...)`、`def enable(...)`、`def set_status("active")`，
> 也拦不住在**仓库层**、**mapper 层**或**直接用 SQL** 写入。
> §四.9 要求删除这两条，改用状态图 + 服务入口 + 仓储写入 + 数据库对抗性测试证明。

### 阶段 1 — 领域对象与状态机

| # | 能力声明 | 入口 | 调用链 | 副作用 | 单测 | PG 集成 | 黑盒 | 反例 | 等级 | 残余风险 |
|---|---|---|---|---|---|---|---|---|---|---|
| **C1.1** | 类型层面强制分离观察 / 假设 / 判断 / 未知 | `POST /conversations/{id}/messages` | `conversations.py` → `CognitiveRuntime.run()` → `cognition/*` 各模块 → 产物落 `ArtifactService` | 事件流（`EventType.*_CREATED` 47 条） | 各领域对象单测 + `test_invariants_selfcheck.py` | 部分（`test_round_service.py`） | ❌ | 部分（`test_invariant_properties.py`） | **E2** | 枚举成员集靠 `_check_i01` 的运行期探测；新增"已确认"语义的状态能否被拦取决于是否含 `confirmed/verified/established/canonical` 四个词 |
| **C1.2** | 认知回合状态机：14 状态 + 转移表 + 硬约束 | `POST /conversations/{id}/messages`、`POST /replay/...` | `round_service.py` → `cognition/state_machine.py::assert_transition` | `cognitive_rounds` 行（10 条 CHECK） | `test_state_machine.py` + `test_state_machine_properties.py`（Hypothesis） | ✅ `test_round_service.py`（含 `completed_requires_stop_reason` 等 CHECK 实测） | ❌ | ✅ 属性测试枚举非法跳转 | **E2** | 属性测试跑在纯函数上；**经 API 触发的非法跳转路径**没有被黑盒覆盖 |

### 阶段 2 — 持久化与事件存储

| # | 能力声明 | 入口 | 调用链 | 副作用 | 单测 | PG 集成 | 黑盒 | 反例 | 等级 | 残余风险 |
|---|---|---|---|---|---|---|---|---|---|---|
| **C2.1** | 事件存储 + 幂等键（唯一约束） | `POST /conversations/{id}/messages` | 各服务 → `uow.events.append` → `infrastructure/db/` | `events` 行（4 条 CHECK）+ 幂等唯一索引 | `test_events.py` | ✅ `test_event_store.py`（含 `recorded_not_before_occurred`、`actor_type_valid` CHECK 实测） | ❌ | ✅ 契约测试同一组断言跑两个实现 | **E2** | 无 |
| **C2.2** | 回合持久化 + 乐观锁（`expected_version`） | 同上 | `round_service.py` → `uow.rounds` | `cognitive_rounds.version` 自增 | `test_cognitive_rounds.py` | ✅ `test_contract_postgres.py` | ❌ | ✅ 契约测试含版本冲突 | **E2** | **并发**（真正同时到达的两个请求）没有测试，只有顺序化的版本冲突 |
| **C2.3** | 状态投影 / 回放（回放同时审计转移合法性） | `POST /replay/cognitive-rounds/{id}` | `replay.py:52` → `cognition/projection.py::project_round` | 无（只读） | `test_projection.py` | ❌ | ❌ | ❌ | **E2** | 🔴 **`application/replay_service.py`（115 行）是死代码**：生产路由**内联**调 `project_round`，没走这个服务；它只被 `tests/integration/test_round_service.py` 引用 |

### 阶段 3 — 认知流水线与场景

| # | 能力声明 | 入口 | 调用链 | 副作用 | 单测 | PG 集成 | 黑盒 | 反例 | 等级 | 残余风险 |
|---|---|---|---|---|---|---|---|---|---|---|
| **C3.1** | 完整认知流水线（深度路由 D0–D4 + 15 个认知模块 + 元认知停止） | `POST /conversations/{id}/messages` | `conversations.py` → `CognitiveRuntime.run()` → `orchestrator` / `depth` / 15 模块 / `metacognition` | 事件流 + 回合行 + 产物行 | 15+ 个单测文件 | 部分（`test_round_service.py`） | ❌ | `test_scenarios_*`（内存 + Mock） | **E2** | 场景 A–J 全部跑在内存后端 + Mock Provider；**没有任何一次在真实 PostgreSQL + 真实 API 上跑过** |
| **C3.2** | 场景 A–J（任务书 §15.4 硬性验收） | 无生产入口（纯测试） | `tests/scenarios/` | 无 | ✅ | ❌ | ❌ | ❌ | **E1** | 它是**验收条件**，但只能通过测试触达——这是设计如此，不是缺陷；但"内存 + Mock"意味着它验证的是**流水线逻辑**而非**运行系统** |

### 阶段 4 — 真实 LLM

| # | 能力声明 | 入口 | 调用链 | 副作用 | 单测 | PG 集成 | 黑盒 | 反例 | 等级 | 残余风险 |
|---|---|---|---|---|---|---|---|---|---|---|
| **C4.1** | 真实 LLM Provider（DeepSeek），缺 Key **不**静默回落 | `AI_PSI_LLM_PROVIDER=deepseek` + `POST /messages` | `container.py:134` → `providers/registry.py::build_provider_with_client` → `openai_compatible` | 事件里的 `ModelInvocationInfo`（**只存 token 计数，不存 reasoning**） | `test_openai_compatible_provider.py`、`test_provider_parsing_and_registry.py` | ✅ `test_live_provider.py`（`make test-live`，**真实网络 + 真实计费**） | ❌ | ✅ 缺 Key 显式报错 | **E2** | 真实 Provider 只被集成测试驱动，**没有一次经 HTTP API**；熔断与解析修复在真实网络下的表现未被黑盒覆盖 |
| **C4.2** | 熔断降级 | 同上（隐式） | `providers/circuit.py` 装饰器 | 无（进程内状态） | `test_circuit_breaker.py` | ❌ | ❌ | ✅ | **E2** | 熔断状态是**进程内**的，多进程部署下每个进程各自熔断 |

### 阶段 5 — 长期记忆

| # | 能力声明 | 入口 | 调用链 | 副作用 | 单测 | PG 集成 | 黑盒 | 反例 | 等级 | 残余风险 |
|---|---|---|---|---|---|---|---|---|---|---|
| **C5.1** | 记忆写入必须过写入策略（`WritePolicy`） | `POST /conversations/{id}/messages`、`POST /cognitive-rounds/{id}/feedback` | `memory_service.py` → `memory/write_policy.py` | `memories` 行（10 条 CHECK） | `test_write_policy.py`、`test_memory_service.py` | ✅ `test_memory_postgres.py` | ❌ | ✅ | **E2** | 策略是纯函数，测试充分；但**绕过路径**（直接 `uow.memories.add`）没有被对抗性测试钉住 |
| **C5.2** | 记忆检索（pgvector 512 维 + HNSW 余弦索引） | `GET /users/{user_id}/memories` | `memories.py` → `MemoryService.retrieve` → `memory/retrieval.py` + `ranking.py` | 无（只读） | `test_memory_ranking.py`、`test_embeddings.py` | ✅ `test_memory_postgres.py`（维度、索引、删除传播、空值语义） | ❌ | ❌ | **E2** | 🔴 默认向量是**词面 n-gram，非语义**（ADR-0017 §1）：`喜欢简洁` 与 `讨厌啰嗦` 几乎正交。换 Provider 必须重建索引，否则**一条都检索不到且不报错**（risks R43） |
| **C5.3** | 记忆生命周期：纠正 / 删除 / 导出 / 用户数据删除 | `POST /memories/{id}/correct`、`DELETE /memories/{id}`、`POST /users/{id}/export`、`DELETE /users/{id}/data` | `memories.py` → `MemoryService` → `memory/lifecycle.py` | 记忆行 + 生命周期事件 | `test_memory_lifecycle.py`、`test_memory_redaction.py` | 部分 | ❌ | ❌ | **E2** | 用户隔离（A 的数据不混入 B）在单测与契约测试里成立，但**没有黑盒验证**——§七.17 要求补 |

### 阶段 6 — 反馈、经验与改进提案

| # | 能力声明 | 入口 | 调用链 | 副作用 | 单测 | PG 集成 | 黑盒 | 反例 | 等级 | 残余风险 |
|---|---|---|---|---|---|---|---|---|---|---|
| **C6.1** | 反馈接收 → 事件落库 → 条件性记忆更新 | `POST /cognitive-rounds/{id}/feedback` | `feedback.py:35` → `FeedbackService.record()`（事件先落、记忆后做，**两个事务**） | `user_feedback_received` 事件 + 可能的 `memories` 行 | `test_feedback_service.py`、`test_api_feedback.py` | ❌ | ❌（`test_api_feedback.py` 用内存后端） | 部分（空白输入 500 的回归） | **E2** | 🔴 **PostgreSQL 上从未验证过**——反馈路径的 `memory_effect` 四种取值在真实数据库下的行为无证据。§三 要求同事务，与 ADR-0018 §4 冲突（待决） |
| **C6.2** | 经验构建 + **确定性**错误归因 | 无独立入口（回合收尾隐式触发） | `cognitive_runtime.py:1140` → `ExperienceBuilder.build()` → `ErrorClassifier` | `experience_created` 事件 | `test_experience_builder.py`、`test_error_classifier.py` | ❌ | ❌ | ✅（不可归因返回 `None`，不猜） | **E2** | 🔴 8 条归因判据中**4 条在生产上不可达**：`_from_failure`（失败回合不构经验）、`_from_budget`（刻意不喂）、`_from_memory_rejection`（无生产者）、`_from_user_feedback`（反馈在回合后到达，无回流路径）——**漏报率可能很高**，阶段 7 应量化 |
| **C6.3** | 模式发现：按**不同回合**计数同类错误 | **无** | `PatternDetector.detect` 只被 `learning_service.py` 引用，而后者**零引用** | 无 | `test_pattern_detector.py` + `tests/scenarios/test_learning_chain.py` | ❌ | ❌ | ✅（2 回合不够、不可归因永不达门槛） | **E1** | 去重键 `(cognitive_round_id, judgment_id)` 已修正，但**在生产路径上不可达** |
| **C6.4** | 门槛裁决（`PromotionPolicy`）：`None` ≠ `False` | **无** | 同上 | 无 | `test_promotion_policy.py` | ❌ | ❌ | ✅ | **E1** | `PromotionEvidence.user_corrections` 在**测试外没有生产者**（`learning_service` 里算了，但它自己不可达） |
| **C6.5** | 提案生成（`ProposalGenerator`） | **无** | 同上 | 无 | `test_proposal_generator.py` | ❌ | ❌ | ✅（伪造裁决被拒） | **E1** | 同上 |
| **C6.6** | 提案状态机：登记 → 评估 →（批准 \| 驳回） | `GET/POST /improvement-proposals*`（5 个端点） | `proposals.py` → `ProposalService` | `improvement_proposals` 行 + 审批事件 | `test_proposal_service.py` | ✅ `test_contract_postgres.py`（同一组断言两实现） | ❌ | 部分 | **E2** | ① 契约测试覆盖的是**仓储**，不是**经 API 的状态机**；② 评估/批准/驳回的**事务性**（状态变更 + 审计事件同事务）没有 PG 级验证 |
| **C6.7** | **学习链路端到端：三次同类错误 → DRAFT 提案** | 🔴 **无生产入口** | `tests/scenarios/test_learning_chain.py` 内直连四个纯组件 | 无（测试内） | ✅（该场景测试本身） | ❌ | ❌ | ✅（2 回合不够） | **E1** | 🔴🔴 **这是阶段 6 最重要的一条验收条件，也是证据最弱的一条。** 它在跑起来的系统里**不可操作**：没有 CLI、没有路由、没有 worker 能触发它。`learning_service.py`（`93727b4`）本意是补这个缺口，但它**零引用**、**无测试**，且**未走 `ProposalGate`**，不满足 §二.13–15 |
| **C6.8** | 认知宪法不被学习模块修改 | 无独立入口（测试层强制） | `tests/unit/test_learning_constitution_boundary.py`（AST 扫描 + 运行时指纹比对） | 无 | ✅ | 不适用 | ❌ | ✅（AST 而非字符串搜索） | **E1** | 它是**测试**而非**运行期强制**：宪法在运行时被学习模块改写不会被拦截，只会在测试里变红。V0.1 可接受，但应明确记录 |
| **C6.9** | 离线评测（`OfflineEvaluator`） | 🔴 **无** | 🔴 **零调用者**（连 `learning_service.py` 都没引用它） | 无 | `test_offline_evaluator.py` | ❌ | ❌ | ✅（higher/lower-is-better 双向、未评估不判退化） | **E1** | 🔴 **完全的死代码**。§七.15–16 要求黑盒验证"lower-is-better 从 0 升到 1 判退化""未评估不得自动判未退化"——当前没有任何路径能触达它 |

---

## 四、按等级汇总

| 等级 | 数量 | 能力 |
|---|---|---|
| **E4 抗对抗** | **0** | — |
| **E3 黑盒验过** | **0** | — |
| **E2 生产可达** | **16** | C0.1、C0.2、C1.1、C1.2、C2.1、C2.2、C2.3、C3.1、C4.1、C4.2、C5.1、C5.2、C5.3、C6.1、C6.2、C6.6 |
| **E1 仅测试** | **7** | C3.2、C6.3、C6.4、C6.5、C6.7、C6.8、C6.9 |
| **E0 无** | 0 | — |
| **合计** | **23** | 阶段 0（2）+ 阶段 1（2）+ 阶段 2（3）+ 阶段 3（2）+ 阶段 4（2）+ 阶段 5（3）+ 阶段 6（9） |

> 🔴 **没有任何一项能力达到 E3。** 原因是一句话：**这个仓库里没有任何一个测试，
> 让真实 HTTP 请求跑在真实 PostgreSQL 上。** `tests/api/` 全部用
> `storage_backend="memory"`；`tests/integration/` 全部直接调服务层，不过 HTTP。
> 两个维度各自被覆盖，**交集为空**——而 §七 要的正是那个交集。

---

## 五、本矩阵暴露的待处理项（映射到后续各节）

| # | 发现 | 对应节 |
|---|---|---|
| M1 | 学习链路（C6.3–C6.5、C6.7）**无生产入口**，三次错误门槛在系统里不可操作 | §二、§七 |
| M2 | `learning_service.py`（`93727b4`）零引用、无测试、**未走 ProposalGate** | §二.13–15、§四.2 |
| M3 | `OfflineEvaluator`（C6.9）**完全死代码**，零调用者 | §四.1–2、§七.15–16 |
| M4 | `ReplayService`（C2.3）死代码——路由内联调 `project_round` | §四.1–2 |
| M5 | `ck_improvement_proposals_status_valid` 无测试触发；README/ADR 的"实测"未固化 | §四.6–8、§七.10 |
| M6 | 应用 Enum ↔ DB CHECK **无一致性测试** | §四.7 |
| M7 | `TestNoPathToActive` 是名字扫描，拦不住 `make_live`/`enable`/`set_status`/仓储层直写 | §四.9–10、§七.9、§七.11 |
| M8 | 反馈路径（C6.1）在真实 PostgreSQL 上从未验证 | §三、§七.13 |
| M9 | 并发（真正同时到达的两个请求）完全没有测试 | §七.14 |
| M10 | 用户隔离（C5.3）无黑盒验证 | §七.17 |
| M11 | 归因 8 条判据中 4 条生产不可达，漏报率未量化 | §二.2–5、阶段 7 |
| M12 | 学习链路的经验**没有评价状态**，内部元认知可产生"确认真实错误" | §二.3–8 |
| M13 | 经验**没有规范身份语义**（`origin_round_id`/`canonical_key`/`independence_group`），同一回合重复抽取可冒充独立经验 | §二.9–12 |
| M14 | 修订后 README 的"不变量 11 的 DB 级强制 ✅"应降级为"迁移中存在，测试未固化" | §四.8 |

---

## 六、基线状态

| 项 | 值 |
|---|---|
| 候选修复基线 | `5e9c7f0`（**候选**，非正式完成） |
| 矩阵建立时的 HEAD | `93727b4` |
| 工作区 | 干净 |
| 与本矩阵有关的既有声明 | `README.md` §当前质量指标、ADR-0018、ADR-0019、`docs/risks.md` |

> **本矩阵建立后，README / ADR 中与上表冲突的表述以本表为准，
> 差异逐条登记在 §五。** 在阶段 6.5 完成前，不得用文档中的 ✅ 覆盖本表中的具体等级。
