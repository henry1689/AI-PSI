# ADR-0012：V0.1 范围边界、留接口清单与目录结构偏差

- **状态**：已接受
- **日期**：2026-09-17
- **相关**：全部 ADR、`docs/implementation_plan.md`

## 背景

任务书 §18 把项目分为阶段 0–8，并要求"每阶段必须保证仓库处于可运行状态"。
§2.3 与 §11.1 列出了本期明确不做的功能，且要求"只留接口，不实现"。

同时，任务书 §4 给出的目录结构覆盖了**全部 8 个阶段**的产物。
若在阶段 1 就创建全部目录，会产生大量空壳包——
它们既无法运行，也无法测试，还会误导后续读者以为那些能力已经存在。

## 决策

### 1. 不创建空壳包

**只为当前阶段有实际内容的模块创建包。**
阶段 1 只创建：

```
src/ai_psi/
├── __init__.py
├── config.py
├── domain/          # 领域对象、枚举、异常
└── cognition/       # 认知状态机、宪法
```

**不创建**（在对应阶段引入）：

| 包 | 引入阶段 |
|---|---|
| `infrastructure/`（db、event_store、task_queue） | 阶段 2 |
| `application/`、`providers/`、`prompts/`、`api/` | 阶段 3 / 4 |
| `memory/` | 阶段 5 |
| `learning/`、`reliability/` | 阶段 6 |
| `evals/runners|metrics` 的实际内容 | 阶段 7 |

理由：开发原则第 2 条"不写推测性代码"，以及铁律"每个阶段仓库可运行"。
一个 import 会失败的空壳包不是"预留接口"，是负债。

**所谓的"留接口"指的是 Protocol 定义与类型占位**，
它们出现在**需要它们的那个阶段**，而不是提前撒满整个代码树。

### 2. 本期明确不做（不实现，也不建占位）

自动修改源代码 · 自动修改系统提示词并立即生效 · 自动训练或微调模型 ·
无限制后台自由思考 · 物理动作与机器人控制 · 多模型民主投票 ·
完整知识图谱数据库 · 自动发布通用认知策略 · 心理诊断 ·
自动生成用户稳定人格画像 · 用完整内部思维链作为长期记忆 ·
自行改变认知宪法 · 自主扩大数据访问权限。

### 3. 与任务书目录结构的偏差登记

| 任务书 §4 列出 | 实际处理 | 原因 |
|---|---|---|
| `src/ai_psi/main.py` | 阶段 3 创建 | 现在没有可启动的应用；空文件无意义 |
| `src/ai_psi/domain/common.py` 等 19 个文件 | 阶段 1 全部创建 | 符合任务书 |
| `src/ai_psi/domain/exceptions.py` | **新增**（任务书未列） | 任务书 §21 第 5 条要求"基础异常"，但 §4 目录结构漏列该文件 |
| `src/ai_psi/cognition/constitution.py` | **新增**（任务书未列） | ADR-0011：宪法必须可测试，Markdown 不可测试 |
| `src/ai_psi/cognition/state_machine.py` | 阶段 1 创建纯转移表 | 任务书 §15.1 阶段 1 单测要求覆盖"状态转换"；仓储接入仍在阶段 2 |
| `alembic.ini` + `migrations/` | **推迟到阶段 2** | 没有 SQLAlchemy 模型时 alembic 配置是死配置；任务书 §18 也把 Alembic 列在阶段 2 |
| `evals/**`、`tests/{integration,scenarios,golden}/` | 建目录，阶段 3/7 填内容 | 目录结构已就位，避免后续结构调整 |
| `templates/{causal_analyzer,dialectical_analyzer,...}` | 阶段 3 创建 | 任务书 §4 与 §9 的模块清单本身不一致（§9 有 12 个分析器，§4 只列 9 个模板目录），以 §9 为准 |

### 4. 补充缺口决策

任务书未定义、本 ADR 统一给出默认值：

| 缺口 | 决策 |
|---|---|
| `Concern.related_goal_ids` 引用的 Goal 实体全书不存在 | V0.1 **不建独立 Goal 聚合**；`related_goal_ids` 指向 `Memory(memory_type=USER_GOAL)` 的 id |
| `Hypothesis.category: str` 无枚举，与全书强枚举风格不一致 | 定义 `HypothesisCategory` 枚举，**必须包含至少一个非人格化/非心理化成员**（对应 §9.6 要求） |
| `tests/golden/` 与 `evals/datasets/` 职责重叠 | `tests/golden/` = 结构化对象快照回归（pytest 跑）；`evals/datasets/` = 行为评测案例（eval runner 跑，不进 pytest 默认集） |
| API 未定义用户创建接口 | V0.1 **不做用户注册**；`user_id` 由调用方提供，服务端校验存在性 |
| `Reflection.repeated_claim_score: float` 与全书置信度分档风格不一致 | 保留 float（它是相似度度量而非置信度，语义不同），但**取值范围限制在 [0, 1]** |
| 13.2 的 `DEGRADED` 不在回合状态枚举中 | 归入系统/Provider 健康状态，**不进回合状态机**（同 ADR-0010 的同类澄清） |
| 6.3 "每个状态有超时"但无数值 | 见 ADR-0008 的状态超时表 |
| 元认知模型调用是否计入 `max_model_calls` | 计入（ADR-0008） |

## 替代方案与取舍

| 方案 | 为什么不选 |
|---|---|
| 按 §4 目录结构一次性创建全部包 | 产生大量 import 失败的空壳；违反"每阶段仓库可运行" |
| 严格按 §4 字面，不新增任何文件 | `exceptions.py` 与 `constitution.py` 是任务书自身要求（§21.5、§14.12）的实现前提，不新增无法落地 |
| 现在就加 Alembic 骨架 | 死配置；且任务书自己的阶段划分把迁移放在阶段 2 |

## 影响

- 后续每个阶段开工时的第一件事是**创建该阶段的包**，最后一件事是
  **更新本 ADR 的偏差表与 `docs/implementation_plan.md` 的进度**。
- `docs/implementation_plan.md` 是阶段进度的唯一真相来源。
- 若未来发现任务书其他自相矛盾处，**新增 ADR** 而非就地修改本文件——
  ADR 的价值在于保留决策的历史轨迹。
