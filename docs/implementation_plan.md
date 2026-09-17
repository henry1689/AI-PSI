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
| 3 | Mock LLM 与认知流水线 | ⬜ 未开始 | — |
| 4 | 真实 LLM Provider | ⬜ 未开始 | — |
| 5 | 长期记忆（pgvector） | ⬜ 未开始 | — |
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

### 阶段 3：Mock LLM 与认知流水线

**交付**：Provider Protocol、Mock Provider、Prompt Registry、
关切/框定/深度/认知分类/假设/判断、元认知停止、Response Planner/Renderer、
完整场景集成测试。
**本阶段创建**：`application/`、`providers/`、`prompts/`、`api/`、`main.py`；
并从 ADR-0009 落地**内存适配器**以支撑场景 F/J。

**验收**：不使用外部 API 也能完整运行；场景 A–J 全部通过；循环永不超预算。

### 阶段 4：真实 LLM Provider

**交付**：Anthropic Provider、OpenAI-compatible Provider、
超时/重试/熔断、结构化输出修复、模型调用统计、Prompt 版本记录。

**验收**：Provider 可配置切换；真实 Provider 不影响领域层；
无 API Key 时自动使用 Mock 或明确失败；解析异常不污染状态。

### 阶段 5：长期记忆

**交付**：Memory Repository（PostgreSQL + pgvector）、WritePolicy、
冲突/过期/取代、用户纠正、删除与导出、用户隔离测试。
**本阶段创建**：`memory/`。

**验收**：用户 A 无法检索用户 B 的私有记忆；superseded 记忆不默认生效；
删除后不再出现在向量结果中。

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
| 任务书 20 项内部冲突 | ✅ 已处理 | 4 项实质矛盾见 ADR-0006/0009/0010/0012，16 项缺口默认值见 ADR-0008/0012 |
