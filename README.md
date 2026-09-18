# AI-PSI Cognitive Runtime

> **V0.1 — 认知运行时骨架**
>
> 一个持续自主认知、自反思、受控迭代的语言认知系统。
> 目标方向是 **L5 Personal Superintelligence**；本仓库当前交付的是通往该方向的
> **V0.1 认知内核**，不是 L5 系统本身。

---

## 这个系统是什么

不是聊天机器人，也**不是**靠反复提示"再思考一下"来模拟认知。

它通过**结构化对象、事件记录、认知状态机、证据关系、元认知控制、长期经验
和受控策略提案**，形成一个**可追踪、可测试、可纠正**的认知闭环。

V0.1 要验证的不是"回答看起来多深刻"，而是下面五件事在代码中真正成立：

1. 系统知道当前在思考什么（认知回合状态机 + 结构化对象）
2. 系统能区分**观察 / 假设 / 判断 / 未知**（类型层面强制分离）
3. 系统知道**为什么继续**以及**为什么停止**（元认知 + 停止原因必录）
4. 系统能用后续反馈形成**可核验经验**（Experience + 错误分类）
5. 系统能提出改进，但**不能未经验证就改变自己**（Proposal 永不自动生效）

---

## V0.1 与 L5 愿景的区别

**这一点必须说清楚，避免把骨架当成品。**

| | V0.1（本仓库） | L5 愿景（北极星） |
|---|---|---|
| 定位 | 可靠的认知运行时骨架 | 个人超级智能 |
| 思考 | 结构化认知对象 + 有限深度路由（D0–D4） | 自主长期研究与跨领域综合 |
| 记忆 | 经规则校验的长期记忆，用户可纠正/删除 | 持续自我模型与长期认知增强 |
| 迭代 | 只生成 Proposal，**需人工审批** | 受控自主演进 |
| 模型 | 可替换组件（Mock / Anthropic / OpenAI-compatible） | 多模型验证与协同 |

### 🔴 三条不可越界的红线

1. **不保存、不展示模型的完整隐藏思维链。**
   系统只保存**结构化认知摘要**：证据、主要理由、反证、判断、修正条件、
   置信度依据。`Judgment.rationale_summary` 是结构化理由，不是模型内部推理流。

2. **ImprovementProposal 永远不能自动生效。**
   V0.1 中提案只能处于 `DRAFT` / `PENDING_EVALUATION` / `EVALUATED` /
   `REJECTED` / `APPROVED_FOR_MANUAL_TRIAL`。**没有 `ACTIVE` 路径。**

   这条不是靠约定，而是四层同时成立：类型里不存在那个值；
   服务上没有 `activate`/`promote`/`publish`/`deploy`/`apply` 任何方法；
   数据库的 `status` CHECK 由该枚举生成（实测 `'active'` 被拒绝）；
   `/health/cognitive` 在运行期检查这个结构性保证还在不在。
   批准与驳回**也必须在评估之后**——未经评估的批准等于凭印象拍板。

3. **认知宪法不能被学习模块修改。**
   宪法以代码常量形式落在 `src/ai_psi/cognition/constitution.py`，
   配套不变量测试。想改宪法 = 改代码 = 走代码评审，不是"模型自己决定"。

### V0.1 明确不做

自动修改源代码 · 自动改系统提示词并生效 · 自动训练或微调模型 ·
无限制后台自由思考 · 物理动作与机器人控制 · 多模型民主投票 ·
完整知识图谱数据库 · 自动发布通用认知策略 · 心理诊断 ·
自动生成用户稳定人格画像 · 用完整内部思维链作长期记忆 ·
系统自行改变认知宪法 · 自主扩大数据访问权限。

以上各项**只留接口，不实现**。清单见 `docs/adr/0012-v0-1-scope-boundaries.md`。

---

## 当前进度

| 阶段 | 内容 | 状态 |
|---|---|---|
| 阶段 0 | 架构与文档（ADR、领域模型、状态机、认知宪法） | ✅ 已完成 |
| 阶段 1 | 项目骨架与领域对象 | ✅ 已完成 |
| 阶段 2 | 数据库、事件存储、认知状态机 | ✅ 已完成 |
| 阶段 3 | Mock LLM 与认知流水线（场景 A–J） | ✅ 已完成 |
| 阶段 4 | 真实 LLM Provider（DeepSeek） | ✅ 已完成 |
| 阶段 5 | 长期记忆（PostgreSQL + pgvector） | ✅ 已完成 |
| 阶段 6 | 反馈、经验与改进提案 | ✅ 已完成 |
| 阶段 7 | 评测与回放 | ⬜ 未开始 |
| 阶段 8 | 完整验收与交付 | ⬜ 未开始 |

> 大型自主编码任务最常见的失败，不是写得慢，而是接口尚未稳定就盖到第八层。
> 因此本项目**严格按阶段推进，每个阶段结束时仓库都处于可运行状态**。

### 当前质量指标（阶段 6 实测）

| 检查 | 结果 |
|---|---|
| `make lint`（ruff） | 0 error |
| `make typecheck`（mypy strict，**203 个文件**） | 0 error |
| `make test` | **1542 passed, 9 skipped**（单元 + 属性 + 契约 + 场景 + API + 集成） |
| 测试覆盖率 | 总体 **96%**；`domain/` 与 `cognition/` **98%**（门槛 85% / 75%） |
| `learning/` 覆盖率 | **100%**（六个模块）——阶段 6 之前是**零测试** |
| 契约测试 | 事件存储 / 回合仓储 / 幂等键 / 长期记忆 / **改进提案**：同一组断言跑**内存与 PostgreSQL 两个实现** |
| 场景 A–J | **全部通过**（任务书 §15.4） |
| **真实模型端到端** | ✅ `make test-live`：**5 passed**（真实 DeepSeek；D0/D2/D4 三类回合全部跑通） |
| **向量检索** | ✅ 512 维 + HNSW 余弦索引，在**真实 PostgreSQL** 上验证维度、索引、删除传播、空值语义 |
| **不变量自检** | ✅ I01 / I10 / I11 在**运行期**被检查（试着构造 `"active"` 会失败），并接入 `/health/cognitive`；测试逐条把它们按坏，要求自检指出来 |
| **不变量 11 的 DB 级强制** | ✅ 实测 `INSERT ... status='active'` 被 PostgreSQL 拒绝（`ck_improvement_proposals_status_valid`） |
| 预算 | **无超预算回合**：调用前扣减 + 可选模块跳过 + 强制尾部保留 |
| 数据库迁移 | `alembic upgrade head` 通过；**32 条 CHECK 约束**（`cognitive_rounds` 10 / `memories` 10 / `improvement_proposals` 6 / `events` 4 / 索引与幂等 2） |
| 开发数据库 | PostgreSQL 16.15 + pgvector 0.8.6 |

> **阶段 6 的三条验收条件都是"否定式"的**——它们说的是"什么不会自动发生"。
> 因此对应的测试也写成否定式：单次经验不能推广（门槛 < 2 直接拒绝）、
> 未经评估不能批准或驳回（409）、
> 整个 API 里没有任何一条路径能让提案生效（不变量 11）。

> **零依赖与真实模型两条路都通。**
> 默认配置（`AI_PSI_LLM_PROVIDER=mock` + `AI_PSI_STORAGE_BACKEND=memory`）
> 下整套认知闭环零外部依赖；把 `AI_PSI_LLM_PROVIDER` 改成 `deepseek`
> 并在环境里配好密钥，同一套代码就跑在真实推理模型上。

---

## 本地启动

### 前置

- Python **3.12+**（本项目在 3.13.2 上开发验证）
- [uv](https://docs.astral.sh/uv/)
- Docker Desktop（阶段 2 起需要 PostgreSQL；阶段 1 的测试不需要数据库）

### 安装依赖

```bash
uv sync --all-groups
```

### 质量检查

```bash
# 若有 make（Linux / macOS / CI）
make lint && make typecheck && make test

# Windows 无 make 时，用等价命令
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests scripts
uv run pytest --cov=ai_psi --cov-report=term-missing
```

### 接入真实模型

```bash
# 密钥只从环境变量来（也接受业界通用名 DEEPSEEK_API_KEY / OPENAI_API_KEY）
export AI_PSI_DEEPSEEK_API_KEY=...
export AI_PSI_LLM_PROVIDER=deepseek        # 默认模型 deepseek-v4-flash
uv run python -m ai_psi.main
```

| 变量 | 说明 |
|---|---|
| `AI_PSI_LLM_PROVIDER` | `mock`（默认）/ `deepseek` / `openai_compatible` |
| `AI_PSI_LLM_MODEL` | 留空 = 按 Provider 取默认值 |
| `AI_PSI_LLM_JSON_MODE` | 不支持 `response_format` 的兼容服务器请置 `false` |
| `AI_PSI_LLM_REASONING_HEADROOM_TOKENS` | 推理模型的输出预留；留空 = 用 Provider 默认值 |
| `AI_PSI_LLM_CIRCUIT_*` | 熔断阈值与冷却时间 |

> ⚠️ **缺 API Key 时会明确失败，不会静默回落到 Mock。**
> 静默回落会让你以为在跟真实模型对话，实际拿到的是规则引擎的输出
> （ADR-0016 §6）。想用规则引擎，就显式配 `AI_PSI_LLM_PROVIDER=mock`。
>
> ⚠️ **Anthropic Provider 尚未实现**（ADR-0016 §1）；配置它同样会明确报错。

真实模型端到端测试会**花钱、会联网**，因此需要两道开关：

```bash
make test-live        # 需要 AI_PSI_DEEPSEEK_API_KEY 已配置
```

### 启动认知服务

```bash
# 零外部依赖：内存存储 + Mock Provider
AI_PSI_STORAGE_BACKEND=memory uv run python -m ai_psi.main

# 或用真实数据库存事件、回合与记忆
docker compose up -d && uv run alembic upgrade head
uv run python -m ai_psi.main

# 提交一条消息并拿回结构化认知摘要
curl -s -X POST localhost:8000/api/v1/conversations | jq -r .conversation_id
curl -s -X POST localhost:8000/api/v1/conversations/<id>/messages \
     -H 'Content-Type: application/json' \
     -d '{"content":"水在标准大气压下通常多少摄氏度沸腾？"}' | jq
curl -s localhost:8000/api/v1/cognitive-rounds/<round_id>/summary | jq
curl -s -X POST localhost:8000/api/v1/replay/cognitive-rounds/<round_id> | jq

# 给这个回合一条反馈（🔴 user_id 就是提交消息时传的那个）
curl -s -X POST localhost:8000/api/v1/cognitive-rounds/<round_id>/feedback \
     -H 'Content-Type: application/json' \
     -d '{"feedback_type":"correction",
          "content":"你误解了我的意思，我说的适应是改变方法，不是放弃原则",
          "allow_memory_update":true}' | jq

# 改进提案：登记 → 评估 → 批准（每一步都留下审计事件）
curl -s localhost:8000/api/v1/improvement-proposals | jq
curl -s -X POST localhost:8000/api/v1/improvement-proposals/<proposal_id>/evaluate \
     -H 'Content-Type: application/json' \
     -d '{"verdict":"improved","evidence":["历史回放 200 回合"],"actor_id":"评审"}' | jq
curl -s -X POST localhost:8000/api/v1/improvement-proposals/<proposal_id>/approve-for-manual-trial \
     -H 'Content-Type: application/json' -d '{"approved_by":"评审"}' | jq
```

> 🔴 **反馈里的 `allow_memory_update` 不是"允许写记忆"，而是"允许提给写入流程"。**
> 真正写入仍要过记忆写入策略（ADR-0004）——命中内容红线的反馈一样被挡住，
> 响应里的 `memory_effect` 会说明发生了什么（`written` / `not_eligible` /
> `rejected_by_policy`）。没有它，调用方只能靠猜。
>
> 🔴 **`approve-for-manual-trial` 之后没有任何自动动作。**
> 批准的是"做一次人工试验"，不是上线（不变量 11）。
> 而且在评估之前调它会得到 409。

⚠️ **向量（embedding）默认走本地确定性实现，它不是语义向量。**

它用字符 n-gram 的哈希把文本映射到固定维度，余弦相似度因此近似于
**词面重合度**：`喜欢简洁` 与 `讨厌啰嗦` 在这个空间里几乎正交。
它让 pgvector 的全部机制（固定维度、HNSW 索引、删除传播、版本兼容）
在没有外部服务的情况下就能跑通并被测试钉死。

需要真语义检索时换成外部服务——检索路径一行都不用动：

```bash
AI_PSI_EMBEDDING_PROVIDER=openai_compatible
AI_PSI_EMBEDDING_MODEL=text-embedding-3-small
AI_PSI_EMBEDDING_BASE_URL=https://your-endpoint/v1
AI_PSI_EMBEDDING_API_KEY=...
```

🔴 **换向量 Provider 之后必须重建索引**（`MemoryService.reindex`）：
检索只比对同版本的向量，旧向量会立刻全部失效——**旧记忆一条都检索不到**，
而且不会报任何错（ADR-0017 §7、risks.md R43）。

🔴 **`AI_PSI_EMBEDDING_DIMENSION` 是数据库列的固定属性**，改它必须同时改迁移。

### 启动开发数据库

```bash
docker compose up -d                    # PostgreSQL 16 + pgvector，端口 55432
uv run python scripts/bootstrap_db.py   # 校验连通并确保 vector 扩展就绪
uv run alembic upgrade head             # 应用数据库迁移（等价于 make migrate）
docker compose down                     # 停止（保留数据卷）
```

环境变量见 `.env.example`；复制为 `.env` 后填入本地值，`.env` 已被 git 忽略。

### 运行测试

```bash
make test              # 全部测试（单元 + 属性 + 集成）——**需要数据库**
make test-unit         # 只跑单元与属性测试（快，不需要数据库）
make test-integration  # 只跑集成测试
```

集成测试使用**独立派生**的测试库（开发库名 + `_test`），
并在每个用例前清空数据表。配置层会拒绝测试库与开发库相同的情形——
集成测试会 `TRUNCATE`，指错库就是数据丢失（ADR-0014）。

---

## 目录结构

```
ai-psi/
├── docs/               架构、领域模型、状态机、认知宪法、ADR
├── src/ai_psi/
│   ├── main.py              进程入口（HTTP 服务）
│   ├── container.py         依赖装配（组合根：唯一知道"哪个实现"的地方）
│   ├── config.py            配置系统（环境变量注入，密钥不落日志）
│   ├── domain/              领域对象、枚举、基础异常（纯类型，零 IO）
│   ├── cognition/           状态机、宪法、投影、深度路由、编排表、15 个认知模块
│   ├── prompts/             契约、版本、Schema、模板、结构化输入的编解码
│   ├── providers/           协议、Mock、网关、解析修复、HTTP 错误映射、熔断装饰器、OpenAI 兼容实现
│   ├── reliability/         预算记账、反刍信号、置信度上限、熔断状态机、**不变量自检**、认知健康度
│   ├── memory/              写入策略 / 检索 / 排序 / 冲突检测 / 生命周期 / 脱敏
│   ├── learning/            **错误归因 / 经验构建 / 模式发现 / 门槛裁决 / 提案生成 / 离线评估**
│   ├── application/         应用服务（唯一允许发起写入的层）+ 全部 Port
│   ├── infrastructure/      PostgreSQL、内存适配器、事件存储、日志
│   └── api/                 FastAPI 路由、Schema、错误处理、依赖注入
├── migrations/              Alembic 迁移
├── tests/
│   ├── unit/                单元测试（零 IO）
│   ├── property/            Hypothesis 属性测试
│   ├── contract/            **存储契约测试**（同一组断言跑两个实现）
│   ├── scenarios/           任务书 §15.4 的场景 A–J（阶段 3 验收条件）
│   ├── api/                 HTTP 接口测试（内存后端）
│   └── integration/         集成测试（需要 PostgreSQL）+ 真实模型测试（`live`，默认跳过）
├── scripts/                 运维与开发脚本
└── evals/                   评测数据集与运行器（阶段 7）
```

> **只创建有实际内容的包。** 每个包只在本阶段有实体内容时才创建，
> 不预留空壳（`docs/adr/0012-v0-1-scope-boundaries.md`）。
> 阶段 6 因此才建出 `learning/`——它在阶段 1 的目录规划里就存在，
> 但直到有了真正的学习链路才被创建。

### 分层与依赖方向

```
api/  →  application/  →  cognition/ prompts/ memory/ reliability/  →  domain/
                          ↑                                            ↑
                    providers/ infrastructure/  ──────────────────────────
```

- `domain/` **零依赖**（只依赖 pydantic）；
- `cognition/` 等通过 **Protocol（Port）** 访问外部，不 import 实现；
- **只有 `application/` 能发起持久化写入**；
- 依赖方向永远向内（`docs/adr/0001-architecture-style.md`）。

---

## 文档

| 文档 | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 总体架构与分层 |
| [docs/cognitive_constitution.md](docs/cognitive_constitution.md) | 认知宪法（14 节不变量） |
| [docs/domain_model.md](docs/domain_model.md) | 领域对象数据字典 |
| [docs/state_machine.md](docs/state_machine.md) | 认知回合状态机 |
| [docs/security.md](docs/security.md) | 安全、隐私与 Prompt 注入边界 |
| [docs/evaluation.md](docs/evaluation.md) | 评测指标与 Golden Dataset |
| [docs/implementation_plan.md](docs/implementation_plan.md) | 阶段 0–8 实施计划与进度 |
| [docs/risks.md](docs/risks.md) | 风险清单 |
| [docs/adr/](docs/adr/) | 架构决策记录 |

---

## 许可

专有软件，保留所有权利。见 [LICENSE](LICENSE)。