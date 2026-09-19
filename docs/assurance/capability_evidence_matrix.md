# 能力证据矩阵（阶段 0–6，含阶段 7‑专项 R72）

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
| **C0.2** | 提案永不自动生效（枚举无该值 + 服务无该方法 + DB CHECK + 运行期自检） | `build_container()` + `GET /health/cognitive` | `container.py:127` → `assert_structural_invariants` → `_check_i11` | 无 | `test_invariants_selfcheck.py`、`test_experiences_proposals.py` | ✅ `test_proposal_status_constraints.py`（**双向**核对：枚举 ⊆ 数据库，且数据库 ⊆ 枚举） | ✅ `test_black_box_acceptance.py::TestIllegalStatesCannotBeWritten`（4 状态 × 4 参数、DB CHECK 拒绝 + 列表 `can_become_active=False`） | ✅ 状态图模型化测试 + AST 扫描 | **E3** | 见下 |

> **C0.2 在阶段 6.5 内的变化（逐条对着 §一 的原始缺口）：**
>
> * §一 记的"`ck_improvement_proposals_status_valid` 全仓库无测试触发"
>   → 已修：`tests/integration/test_proposal_status_constraints.py` 双向核对，
>   且黑盒层在真实 PostgreSQL 上实测拒绝（4 个非法值 × 4 个参数）。
> * §一 记的"`TestNoPathToActive` 只是名字扫描" → 已删，改为
>   状态图模型化测试 + 服务入口 + 仓储写入 + 数据库四层。
> * §八 评审 B 补充：**"第二道防线"的说法是假的**——详见 ADR-0022 §3
>   的更正与残余风险 R61。**进程内防伪在 Python 里做不到**，
>   本能力挡的是"随手构造"与"经 API 写入"，不是"能在进程内执行任意
>   Python 的主体"。等级仍记 **E3**，但**残余风险栏必须连 R61 一起读**。
> * §八 评审 B 还证伪了一条更强的路径：**仅靠伪造事件流**（6 条事件、
>   引用同一个幽灵 UUID）就能从正式学习入口落库真提案（R62）。
>   它不经过任何被本能力覆盖的接口——这是 E3 覆盖不到的**上游**。

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
| **C2.3** | 状态投影 / 回放（回放同时审计转移合法性） | `POST /replay/cognitive-rounds/{id}` | `replay.py:47` → `container.replay_service` → `application/replay_service.py:61 replay_round` → `projection.py` | 无（只读） | `test_projection.py` | ✅ `test_round_service.py` | ✅ `test_black_box_acceptance.py::TestReplayIsReachableThroughTheApi` | ❌ | **E3** | §一 记的"`ReplayService` 是死代码、路由内联调 `project_round`"已在 §四 修掉（路由现在经容器走服务）。§八 评审 A 复查时确认调用链真实，但指出**出口 `differs_from_projection` 连一条 HTTP 用例都没有**，而 schema 给它设了 `default=False`——删掉路由那一行，响应仍然 `False`，没有任何测试会红。已修：字段改必填，并补两条黑盒用例（字段必须在响应里 + 回放状态与实时投影一致） |

### 阶段 3 — 认知流水线与场景

| # | 能力声明 | 入口 | 调用链 | 副作用 | 单测 | PG 集成 | 黑盒 | 反例 | 等级 | 残余风险 |
|---|---|---|---|---|---|---|---|---|---|---|
| **C3.1** | 完整认知流水线（深度路由 D0–D4 + 15 个认知模块 + 元认知停止） | `POST /conversations/{id}/messages` | `conversations.py` → `CognitiveRuntime.run()` → `orchestrator` / `depth` / 15 模块 / `metacognition` | 事件流 + 回合行 + 产物行 | 15+ 个单测文件 | 部分（`test_round_service.py`） | ❌ | `test_scenarios_*`（内存 + Mock） | **E2** | 场景 A–J 全部跑在内存后端 + Mock Provider；**没有任何一次在真实 PostgreSQL + 真实 API 上跑过** |
| **C3.2** | 场景 A–J（任务书 §15.4 硬性验收） | 无生产入口（纯测试） | `tests/scenarios/` | 无 | ✅ | ❌ | ❌ | ❌ | **E1** | 它是**验收条件**，但只能通过测试触达——这是设计如此，不是缺陷；但"内存 + Mock"意味着它验证的是**流水线逻辑**而非**运行系统** |

### 阶段 4 — 真实 LLM

| # | 能力声明 | 入口 | 调用链 | 副作用 | 单测 | PG 集成 | 黑盒 | 反例 | 等级 | 残余风险 |
|---|---|---|---|---|---|---|---|---|---|---|
| **C4.1** | 真实 LLM Provider（DeepSeek），缺 Key **不**静默回落 | `AI_PSI_LLM_PROVIDER=deepseek` + `POST /messages` | `container.py:134` → `providers/registry.py::build_provider_with_client` → `openai_compatible` | 事件里的 `ModelInvocationInfo`（**只存 token 计数，不存 reasoning**） | `test_openai_compatible_provider.py`、`test_provider_parsing_and_registry.py` | ✅ `test_live_provider.py`（`make test-live`，**真实网络 + 真实计费**） | ❌ | ✅ 缺 Key 显式报错 | **E2** | 真实 Provider 只被集成测试驱动，**没有一次经 HTTP API**；熔断与解析修复在真实网络下的表现未被黑盒覆盖 |
| **C4.2** | 熔断降级 | 同上（隐式） | `providers/resilience.py` 装饰器 + `reliability/circuit_breaker.py` 纯状态机（⚠️ 本栏原名 `providers/circuit.py`，**该文件不存在**，2026-09-19 更正） | 无（进程内状态） | `test_circuit_breaker.py` | ❌ | ❌ | ✅ | **E2** | 熔断状态是**进程内**的，多进程部署下每个进程各自熔断 |

### 阶段 5 — 长期记忆

| # | 能力声明 | 入口 | 调用链 | 副作用 | 单测 | PG 集成 | 黑盒 | 反例 | 等级 | 残余风险 |
|---|---|---|---|---|---|---|---|---|---|---|
| **C5.1** | 记忆写入必须过写入策略（`WritePolicy`） | `POST /conversations/{id}/messages`、`POST /cognitive-rounds/{id}/feedback` | `memory_service.py` → `memory/write_policy.py` | `memories` 行（10 条 CHECK） | `test_write_policy.py`、`test_memory_service.py` | ✅ `test_memory_postgres.py` | ❌ | ✅ | **E2** | 策略是纯函数，测试充分；但**绕过路径**（直接 `uow.memories.add`）没有被对抗性测试钉住 |
| **C5.2** | 记忆检索（pgvector 512 维 + HNSW 余弦索引） | `GET /users/{user_id}/memories` | `memories.py` → `MemoryService.retrieve` → `memory/retrieval.py` + `ranking.py` | 无（只读） | `test_memory_ranking.py`、`test_embeddings.py` | ✅ `test_memory_postgres.py`（维度、索引、删除传播、空值语义） | ❌ | ❌ | **E2** | 🔴 默认向量是**词面 n-gram，非语义**（ADR-0017 §1）：`喜欢简洁` 与 `讨厌啰嗦` 几乎正交。换 Provider 必须重建索引，否则**一条都检索不到且不报错**（risks R43） |
| **C5.3** | 记忆生命周期：纠正 / 删除 / 导出 / 用户数据删除 | `POST /memories/{id}/correct`、`DELETE /memories/{id}`、`POST /users/{id}/export`、`DELETE /users/{id}/data` | `memories.py` → `MemoryService` → `memory/lifecycle.py` | 记忆行 + 生命周期事件 | `test_memory_lifecycle.py`、`test_memory_redaction.py` | 部分 | ⚠️ **部分**：隔离那一半有黑盒（见右），纠正 / 删除 / 用户数据删除三半没有 | ❌ | **E2** | 🔄 **本栏 2026-09-19 更正**：原文写"用户隔离……**没有黑盒验证**——§七.17 要求补"，**已过期**——§七.17 已经补上了：`test_black_box_acceptance.py::TestUserIsolationHoldsThroughTheApi` 的两条（列表隔离 + 导出隔离，后者还断言整包里不含对方的任何痕迹）。**等级仍记 E2**，因为该能力声明含四件事，而黑盒只覆盖其中"导出/列表隔离"这一件；**纠正 / 删除 / 用户数据删除仍只有单测与契约证据** |

### 阶段 6 — 反馈、经验与改进提案

| # | 能力声明 | 入口 | 调用链 | 副作用 | 单测 | PG 集成 | 黑盒 | 反例 | 等级 | 残余风险 |
|---|---|---|---|---|---|---|---|---|---|---|
| **C6.1** | 反馈接收 → 事件落库 → 条件性记忆更新（**单事务** + 记忆失败降级） | `POST /cognitive-rounds/{id}/feedback` | `feedback.py:35` → `FeedbackService.record()`（三步同一 uow，唯一 `commit` 在最后） | `user_feedback_received` + 可能的 `experience.evaluated` + 可能的 `memories` 行 | `test_feedback_service.py`、`test_api_feedback.py` | ✅ `test_input_contract.py`、`test_black_box_acceptance.py`（真实 PG） | 部分：`TestAtomicityAndConcurrency` 只覆盖**事务开始前**失败（404） | 部分 | **E2** | §三 已改为**单事务**（评审 C 用数据库触发器实测：记忆写入故障 → 整体回滚零残留；评价事件故障 → 反馈事件也被回滚）。⚠️ §八 评审 C 指出两点：① 黑盒的 §七.13 用例只覆盖"事务开始前失败"，**中途失败没有黑盒用例**；② 事务内仍有一次 embedding provider 调用与一次**无 limit** 的全表扫（R57 同类） |
| **C6.2** | 经验构建 + **确定性**错误归因（含**用户纠正**那条路径） | 回合收尾隐式触发 + `POST /cognitive-rounds/{id}/feedback`（带指针） | `cognitive_runtime._close_round` → `ExperienceBuilder` → `ErrorClassifier`；纠正路径 `feedback_service._attribute_experiences` → 同一分类器 | `experience.created` + `experience.attributed` 事件 | `test_experience_builder.py`、`test_error_classifier.py`、`test_correction_attribution.py` | 部分 | ✅ 黑盒 A–H + `TestTheCorrectionClosesTheAttributionLoop` | ✅（指不出对象 → `None`，不猜；类别冲突 → `None`，不挑） | **E3** | ⚠️ 归因判据里**仍有 3 条在生产上不可达**（`_from_failure` / `_from_budget` / `_from_memory_rejection`，R58）。🔴 用户纠正那条**已接通**（阶段 6.6）：实测的四个值组合（有/无指针 × 有/无否定反馈）全部按预期分流。⚠️ 类别映射是**约定不是测量**（R71），且纠正是**策略性归因**——置信度 `MODERATE` 的语义写在 ADR-0023 §3。🔴 **另有 R78**（2026-09-19 全阶段审计发现）：**生产路径没有 Evidence 输入**，导致 ADR-0023 §2 的规则「假设 / **有**支持证据 → `REASONING_ERROR`」**永不触发**，恒走"无支持证据 → `EVIDENCE_ERROR`"。规则本身没有实现缺陷，是**它的输入条件在当前接口下拿不到**；影响 `EVIDENCE_ERROR` / `REASONING_ERROR` 的比例，而两者之比正是阶段 7 要统计的归因指标 |
| **C6.3** | 模式发现：按**独立性分组**计数同类错误 | CLI `python -m ai_psi.main learn` + `POST /learning/runs` | `learning_service.py:264 review` → `_detector.detect` | 无（只读事件流） | `test_pattern_detector.py` + `tests/scenarios/test_learning_chain.py` | 部分 | 部分：端点可达性已验，**模式的真实形成未在黑盒层验过** | ✅（2 回合不够、不可归因永不达门槛） | **E2** | 计量单位是 `Experience.independence_group`，§八 评审 B 实测它此前**可被随手填**（1 回合 + 1 纠正 → 门槛 3）。已加逐字一致性校验（ADR-0020 §2）。⚠️ 黑盒层仍**无法**造出"三次同类错误"——默认 Mock 下每条经验的 `error_type` 都是 `None` |
| **C6.4** | 门槛裁决（`PromotionPolicy`）：`None` ≠ `False` | 同上 | `learning_service.py` → `_gate.review` → `_policy.decide` | 无 | `test_promotion_policy.py` | 部分 | 部分 | ✅ | **E2** | 同上：可达 ≠ 会跑。默认 Mock 下 `decide` **一次都不会被调用** |
| **C6.5** | 提案生成（`ProposalGenerator`） | 同上 | `learning_service.py:371` → `_generator.generate` | `improvement_proposals` 行（经 `ProposalGate` 授权） | `test_proposal_generator.py` | 部分 | ✅ **脚本化 Mock + 真实 HTTP + 真实 PG 下真的落库了 DRAFT**（评审 A 实测） | ✅（伪造裁决被拒） | **E2** | 黑盒用默认 Mock 时同样不会被调用；评审 A 的实测用了一次**脚本化 Mock**——那仍是对被测系统的黑盒，但那条路径**没有固化成用例** |
| **C6.6** | 提案状态机：登记 → 评估 →（批准 \| 驳回） | `GET/POST /improvement-proposals*`（5 个端点） | `proposals.py` → `ProposalService` | `improvement_proposals` 行 + 审批事件 | `test_proposal_service.py` | ✅ `test_contract_postgres.py`（同一组断言两实现） | ❌ | 部分 | **E2** | ① 契约测试覆盖的是**仓储**，不是**经 API 的状态机**；② 评估/批准/驳回的**事务性**（状态变更 + 审计事件同事务）没有 PG 级验证 |
| **C6.7** | **学习链路端到端：三次同类错误 → DRAFT 提案** | 🔴 **自动**（第三次纠正提交后触发）+ CLI + `POST /learning/runs` | `feedback_service._run_learning_after_commit` → `learning_service.review` → 检测 → 门禁 → 生成 → 落库 | `improvement_proposals` + 事件 | ✅ `tests/scenarios/test_learning_chain.py` | 部分 | ✅ `test_black_box_acceptance.py::TestTheCorrectionClosesTheAttributionLoop`（**A–H 八个场景**） | ✅（2 回合不够、内部怀疑权重为 0、类别冲突不计入） | **E3** | 🔴 **这一条在阶段 6.5 被降级过，阶段 6.6 把它补上了。** 降级的原因不是做错了，而是**默认配置下不成立**——那时每条经验的 `error_type` 都是 `None`，`decide`/`generate` 一次都不会被调用。现在：**A 场景一次都不调 `/learning/runs`**，三个真实回合 + 三次有依据的同类纠正之后 DRAFT 提案自己出现。⚠️ **两个前提**：纠正必须**指得出被纠正的产物**（`related_artifact_id`，从回合摘要接口取），且回合要走到 **D2 以上**（D0 不做假设，没有可指的对象）。🔄 **"不重复创建提案"这一半已在阶段 7 第一项补齐**：阶段 6.6 的独立评审实测出**并发**下会各生成一条内容相同的 DRAFT（R72），现在由部分唯一索引 `uq_improvement_proposals_active_pattern` 裁决（ADR-0024），**顺序**重试仍由黑盒场景 D 钉住。证据见下表的 C6.10 |
| **C6.8** | 认知宪法不被学习模块修改 | 无独立入口（测试层强制） | `tests/unit/test_learning_constitution_boundary.py`（AST 扫描 + 运行时指纹比对） | 无 | ✅ | 不适用 | ❌ | ✅（AST 而非字符串搜索） | **E1** | 它是**测试**而非**运行期强制**：宪法在运行时被学习模块改写不会被拦截，只会在测试里变红。V0.1 可接受，但应明确记录 |
| **C6.9** | 离线评测（`OfflineEvaluator`） | CLI + `POST /learning/runs` | `learning_service.py:338 _evaluate_offline` → `evaluator.compare` | 无（只读） | `test_offline_evaluator.py` | 部分 | ✅ **`offline_regression` 的方向判定已在真实 HTTP + 真实 PG 上断言** | ✅（对照干净时报 `false`、单侧数据报 `null`） | **E3** | §一 记的"**完全的死代码**、零调用者"已在 §四 修掉。§八 评审 A 复查时指出**两个真实阻断点**（评审结论已实证）：① `regressed()` 只在 `for pattern in scan.patterns` 内部被调用，**没有模式就一次都不调**；② `LearningRunResponse` 里**没有这个结论的出口**。两条都已修——退化判定现在算一次、门禁与运行结果共用，并作为**必填三态**字段回传 |

---

### 阶段 7‑专项 — R72（编号沿用 `C6.10`，**不重编号**）

> 🔴 **为什么这一节单独列，却仍用 `C6.` 前缀。**
> R72 是**进入阶段 7 之后**完成的专项加固，语义上属于阶段 7，
> 但它的编号仍是 `C6.10`。
>
> ⚠️ **2026-09-19 更正**：本节初稿给的理由是"这个编号已被
> `docs/adr/0024` 与 `stage-6-6-completion-report.md` 引用，
> 重编号会让那些引用静默失效"。**那句话不成立**——逐字核对过：
> 这两份文件里 `C6.10` 的出现次数**都是 0**，它们说的是**能力**，
> 不是编号。那是一句**没有核对就写下的**引用声明。
>
> 更正后**结论不变，理由换成一条经得起核对的**：`C6.10`
> **只在本文件内被引用**（C6.7 那一行末尾的"证据见下表的 C6.10"、
> 本节、以及 §四 的汇总表）。仍然**不重编号**——换成一个 `C7.x`
> 的编号会让这一行看起来像"正式阶段 7 的能力"，
> 而那正是本节要防的误读：**7‑专项 ≠ 阶段 7**。
> 保留 `C6.` 前缀，把"它不属于阶段 6"交给**小节标题**说明，
> 而不是让编号去暗示。

| # | 能力声明 | 入口 | 调用链 | 副作用 | 单测 | PG 集成 | 黑盒 | 反例 | 等级 | 残余风险 |
|---|---|---|---|---|---|---|---|---|---|---|
| **C6.10** | **同一业务模式至多一条活跃提案（并发安全）** | 所有提案写入路径（自动触发 / `POST /learning/runs`） | `SqlAlchemyProposalRepository.add` → 部分唯一索引 `uq_improvement_proposals_active_pattern`；冲突 → `LearningService.review` 用**新事务**读回胜出者 → 记入 `already_covered` | `improvement_proposals` 的活跃行（并发下至多一条） | `test_in_memory_pattern_uniqueness.py`（13 项，含两工作单元交错提交） | ✅ `ProposalRepositoryContract`（两后端 27 项同断言） | ✅ **真实 HTTP × 真实 PostgreSQL × barrier × 20 轮**（`test_proposal_pattern_concurrency.py`）；另有迁移 7 项（空库 / 有数据 / 有重复 / downgrade） | ✅ 绕过应用层直接 `INSERT` 两条同键活跃行 → 数据库拒绝；索引名分流依据单独钉住 | **E3** | 🔴 **关掉它就是红的**：`DROP INDEX` 后跑并发用例真的写出 2 条提案、用例变红，恢复索引后变绿（ADR-0024 §6）。⚠️ 降级会**静默**重新打开 R72，见 R76；`find_active_for_pattern` 的多行守卫不可达，见 R77 |

## 四、按等级汇总

> 🔄 **下表已含阶段 6.6 与阶段 7‑专项（R72）的更新**
> （C6.2、C6.7 升到 E3；C6.10 为阶段 7‑专项新增）。

| 等级 | 数量 | 能力 |
|---|---|---|
| **E4 抗对抗** | **0** | — |
| **E3 黑盒验过** | **6** | C0.2、C2.3、C6.2、C6.7、C6.9、**C6.10**（阶段 7‑专项 R72） |
| **E2 生产可达** | **16** | C0.1、C1.1、C1.2、C2.1、C2.2、C3.1、C4.1、C4.2、C5.1、C5.2、C5.3、C6.1、C6.3、C6.4、C6.5、C6.6 |
| **E1 仅测试** | **2** | C3.2、C6.8 |
| **E0 无** | 0 | — |
| **合计** | **24** | 阶段 0（2）+ 阶段 1（2）+ 阶段 2（3）+ 阶段 3（2）+ 阶段 4（2）+ 阶段 5（3）+ 阶段 6（9）+ **阶段 7‑专项（1，编号 C6.10）** |

> ⚠️ **本矩阵只覆盖到阶段 6 与阶段 7‑专项（R72）。**
> **任务书 §18 定义的正式阶段 7 路线图（Golden Cases / Eval Runner / 指标 /
> 报告 / 版本比较）一项都还没开始**，因此它在矩阵里**没有行**——
> 那是"未实现"，不是"漏登记"。详见 `docs/implementation_plan.md` §2。

**与 §一 的对比**（`0 项 E3` → `3 项 E3`，`7 项 E1` → `2 项 E1`）：

| 项 | §一 | 现在 | 变化的原因 |
|---|---|---|---|
| C0.2 | E2 | **E3** | DB CHECK 有了双向一致性测试 + 黑盒实测拒绝；名字扫描那两条已删 |
| C2.3 | E2 | **E3** | `ReplayService` 接通；出口 `differs_from_projection` 有了 HTTP 用例 |
| C6.9 | E1 | **E3** | 从死代码接通到 CLI + HTTP，且**退化方向判定**已在黑盒层断言 |
| C6.3 / C6.4 / C6.5 / C6.7 | E1 | **E2** | 有了生产入口（CLI + `POST /learning/runs`），但**没有黑盒证据**——见下 |
| C6.10 | （无） | **E3** | 阶段 7 第一项新增。它不是"新功能"，是**把既有承诺补成可兑现的**：ADR-0023 §6 曾把"重试不重复创建提案"推给提案层，那句话在并发下是空的（R72）。现在由数据库裁决，且**反向验证过**（DROP INDEX → 用例变红） |
| C3.2 / C6.8 | E1 | E1 | 未变（前者按设计只能由测试触达；后者是测试层强制，不是运行期强制） |

> ⚠️ **"E2"这一档必须带着它的限定读。** §一 的 E2 定义是"有生产入口、
> 调用链真实成立、持久化副作用可观察"。C6.3–C6.7 满足这个定义，
> 但 §八 评审 A 用默认配置实测：**`decide` / `generate` 一次都不会被调用**
> （每条经验的 `error_type` 都是 `None`）。链路是通的，
> **"可达"不等于"会跑"**——C6.7 的端到端证明至今仍在内存 Harness 里。

> 🔴 **E3 的判据没有变，变的是仓库。** §一 当时写"没有任何一项达到 E3，
> 因为交集为空"是**准确的**；现在这个交集由
> `tests/integration/test_black_box_acceptance.py`（真实 HTTP × 真实 PostgreSQL）
> 与 `tests/integration/test_input_contract.py`（双后端参数化）填上了一部分。
> ⚠️ 但 `tests/api/` 仍然全部用 `storage_backend="memory"`——
> 交集是**新增**的，不是**填满**的。

---

## 五、本矩阵暴露的待处理项（映射到后续各节）

| # | 发现 | 对应节 | 状态 |
|---|---|---|---|
| M1 | 学习链路（C6.3–C6.5、C6.7）**无生产入口**，三次错误门槛在系统里不可操作 | §二、§七 | ✅ 已接通（CLI + HTTP）；⚠️ **默认配置下仍不会被触发**，见 C6.7 |
| M2 | `learning_service.py`（`93727b4`）零引用、无测试、**未走 ProposalGate** | §二.13–15、§四.2 | ✅ 已修（门禁接入 + 场景测试） |
| M3 | `OfflineEvaluator`（C6.9）**完全死代码**，零调用者 | §四.1–2、§七.15–16 | ✅ 已修，且退化方向判定已黑盒断言 |
| M4 | `ReplayService`（C2.3）死代码——路由内联调 `project_round` | §四.1–2 | ✅ 已修 |
| M5 | `ck_improvement_proposals_status_valid` 无测试触发；README/ADR 的"实测"未固化 | §四.6–8、§七.10 | ✅ 已修（双向一致性测试 + 黑盒实测） |
| M6 | 应用 Enum ↔ DB CHECK **无一致性测试** | §四.7 | ✅ 已修 |
| M7 | `TestNoPathToActive` 是名字扫描，拦不住 `make_live`/`enable`/`set_status`/仓储层直写 | §四.9–10、§七.9、§七.11 | ✅ 已删，改为状态图 + 服务 + 仓储 + 数据库四层 |
| M8 | 反馈路径（C6.1）在真实 PostgreSQL 上从未验证 | §三、§七.13 | ✅ 已修（单事务 + 真实 PG 契约用例）；⚠️ 黑盒 §七.13 只覆盖事务外失败 |
| M9 | 并发（真正同时到达的两个请求）完全没有测试 | §七.14 | ⚠️ **部分**：幂等键仲裁已覆盖；"两条并发反馈只允许一个成功"在 V0.1 **没有等价物**，如实记为缺口 |
| M10 | 用户隔离（C5.3）无黑盒验证 | §七.17 | ✅ 已修（且先证明"真的有东西可泄漏"） |
| M11 | 归因 8 条判据中 4 条生产不可达，漏报率未量化 | §二.2–5、阶段 7 | ❌ **未变**（R58，阶段 7 待办） |
| M12 | 学习链路的经验**没有评价状态**，内部元认知可产生"确认真实错误" | §二.3–8 | ✅ 已修（四档评价 + 权重 0/0/1/1 + 单一执行点） |
| M13 | 经验**没有规范身份语义**（`origin_round_id`/`canonical_key`/`independence_group`），同一回合重复抽取可冒充独立经验 | §二.9–12 | ✅ 已修；⚠️ §八 评审 B 又发现 `independence_group` 当时**没有校验**（B0），已补 |
| M14 | 修订后 README 的"不变量 11 的 DB 级强制 ✅"应降级为"迁移中存在，测试未固化" | §四.8 | ✅ 已修（现在**有**测试固化了） |

---

## 六、基线状态

| 项 | 值 |
|---|---|
| 候选修复基线 | `5e9c7f0`（**候选**，非正式完成） |
| 矩阵建立时的 HEAD | `93727b4` |
| 矩阵更新时的 HEAD | 阶段 6.5 §九 收尾提交 |
| 工作区 | 干净 |
| 与本矩阵有关的既有声明 | `README.md` §当前质量指标、ADR-0018、ADR-0019、`docs/risks.md` |

> **本矩阵建立后，README / ADR 中与上表冲突的表述以本表为准，
> 差异逐条登记在 §五。**

---

## 七、§八 独立评审对**本矩阵覆盖不到的东西**的发现

§八 的四次独立评审里有**一整个类别的问题落在上面 23 项能力之外**。
不写进这一节，它们就会以"矩阵全绿"的形式消失。

### 7.1 双后端契约一致性（**不是一项能力，是每条能力共用的前提**）

`CLAUDE.md` 的质量闸门写着"存储 Port 的改动必须同时满足 `tests/contract/`
与 `tests/integration/test_contract_postgres.py` 里的**同一组**契约断言"。
评审 C 用两个反例证明这条前提在评审那一刻**是假的**：

| 反例 | memory | postgres |
|---|---|---|
| `Idempotency-Key` 129 字符 | **201**（回合跑完并落库） | **500**（`StringDataRightTruncation`） |
| 正文含 `U+0000` | **201** | **500**（`UntranslatableCharacter`） |

两者的根因是同一个：**"内存能收、PostgreSQL 收不下"，而没有任何机制
逼着两边对齐**。契约测试只覆盖四个**仓储**，`FeedbackService` /
`MemoryService` / `RoundService` / `ProposalService` **没有双后端契约**。
两条都已修，并补了双后端参数化的用例。

### 7.2 乐观锁：内存后端在真交错下**静默丢更新**（C2.2 的隐含前提被推翻）

`save()` 的检查读的是"可见版本"，而并发冲突发生在**暂存之后、提交之前**：
两个事务都读到 v1、都通过检查、都提交，后提交者静默覆盖前一个。
PostgreSQL 不会这样（`UPDATE ... WHERE version = ?` 是原子的）。
已修：提交时在锁内、写入之前复核。风险 R48 的措辞一并更正。

### 7.3 两条"声称已闭合的洞没闭合"

* **ADR-0021 §3** 把阶段 6 的 `actor_id` 事故写成教训，
  然后只对**请求体字段**做了对齐——漏了**请求头**，也漏了
  `RejectProposalRequest.reason`（该 ADR 的表格明确把它列在 `_NOTE_MAX` 名下）。
* **ADR-0020 §2** 与 ADR-README 的 G19 都写了一段
  `CREATE UNIQUE INDEX ... ON events ((payload -> 'experience' ->> 'canonical_key'))`，
  **读起来像已经建好了**——实测 dev/test 两个库该索引数均为 0，
  迁移链 head 里没有任何一个迁移提到它。已降级为"设计意向"并写明理由（R60）。

### 7.4 一个**证伪**：黑盒文件"16/16 不可复现"

评审 A 报告 `test_black_box_acceptance.py` 在干净条件下约 22% 的批次失败
（症状：刚通过 HTTP 建好的数据随后不可见、反馈 404）。

🔴 **复核结论：那是并发污染，不是产品缺陷。**
证据：评审 A 与评审 C 的 pytest **同时在打同一个 `ai_psi_test`**，
而 `clean_tables` 每个用例 `TRUNCATE` 一次。我做的复核是：

* **15 次干净串行运行，次次 16/16 绿**（当时两个评审都已结束）；
* 反过来**两个完整 `tests/integration` 并发跑**，稳定复现
  33 处 `DeadlockDetected` + `OperationalError` + "读到 0 行"。

这与"并行会话共享工作树"是同一类环境问题。**测试库不具备并发运行的安全性**
——这一条本身值得记，但它是环境属性，不是被测系统的属性。

### 7.5 §四"死代码清零"是**证伪**的

只做了 §一 点名的两项。已补：删除 6 个全仓零引用的符号、
登记 12 个"仅测试可达"的公开符号，并把统计口径写进
`docs/implementation_plan.md` §6.1。
