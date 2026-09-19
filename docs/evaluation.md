# 评测系统

> 实现位置：`evals/`（**阶段 7**）
> 相关：ADR-0005（提案门槛）、`docs/cognitive_constitution.md`

---

## 1. 评测要回答什么

不是"回答看起来多深刻"，而是五个结构性问题：

1. 系统能否稳定地**区分**观察、假设、判断、未知？
2. 系统是否在该停的时候**停得下来**？
3. 系统的置信度是否**校准**（说确定时是否真的更可靠）？
4. 冲突证据是否被**保留**而不是被强行合并？
5. 用户的纠正是否**生效且不复发**？

---

## 2. 基础指标（任务书 §16.1）

### 2.1 质量类

| 指标 | 定义 | 期望方向 |
|---|---|---|
| `cognitive_round_success_rate` | 完成回合 / 总回合 | ↑ |
| `structured_output_parse_rate` | 结构化解析成功 / 总模型调用 | ↑ |
| `fact_hypothesis_confusion_rate` | 事实与假设被混淆的比例 | ↓ |
| `unsupported_certainty_rate` | 无依据的高确定表述比例 | ↓ |
| `conflict_preservation_rate` | 冲突被正确保留的比例 | ↑ |
| `user_correction_recurrence_rate` | 纠正后问题**复发**的比例 | ↓ |

### 2.2 记忆类

| 指标 | 定义 | 期望方向 |
|---|---|---|
| `memory_write_rejection_rate` | 被拒的记忆提案比例 | 观测用（过高=策略过严，过低=策略过松） |
| `stale_memory_retrieval_rate` | 失效记忆被当作有效返回的比例 | ↓ |
| `cross_user_memory_leak_rate` | 跨用户泄漏率 | **= 0（硬指标）** |

### 2.3 过程类

| 指标 | 定义 |
|---|---|
| `metacognitive_stop_rate` | 元认知主动停止的比例 |
| `rumination_rate` | 触发反刍检测的比例 |
| `average_model_calls_per_round` | 平均模型调用数（按深度分组） |
| `average_latency_per_depth` | 平均延迟（按 D0–D4 分组） |
| `average_token_cost_per_round` | 平均 token 成本 |

### 2.4 迭代类

| 指标 | 定义 | 期望 |
|---|---|---|
| `proposal_false_promotion_rate` | 提案被错误提升为生效的比例 | **= 0（硬指标）** |

---

## 3. 五项硬指标

以下必须**恒为零**，任何非零值都是**缺陷**而非"待改进的指标"：

```
proposal_false_promotion_rate  = 0     # 提案永不自动生效
cross_user_memory_leak_rate    = 0     # 跨用户零泄漏
非法状态转移接受率              = 0
删除记忆有效检索率              = 0
超预算认知回合率                = 0
```

**这五项不是"优化目标"，是"不变量的运行时验证"。**
它们同时在 `tests/property/` 中以属性测试形式断言（任务书 §15.2）。

---

## 4. Golden Dataset（`evals/datasets/`）

任务书要求 **≥50 个初始案例**，分为 12 类：

| 类别 | 数量（规划） | 考察点 |
|---|---|---|
| 简单事实 | 5 | D0 路由；不过度分析 |
| 歧义问题 | 5 | 概念澄清；不强行选一个解释 |
| 因果问题 | 4 | 区分相关与因果 |
| 关系推测 | 5 | 观察与心理推测分离（场景 B 类） |
| 价值冲突 | 4 | 不机械折中；事实不决定价值 |
| 哲理问题 | 4 | D3/D4；有条件综合（场景 C 类） |
| 证据冲突 | 4 | 冲突保留（场景 E 类） |
| 用户纠正 | 5 | 版本链与不复发（场景 F 类） |
| 诱导迎合 | 5 | 不虚构证明（场景 D 类） |
| 记忆污染 | 4 | WritePolicy 拦截 |
| 反刍 | 3 | 强制停止（场景 G 类） |
| 无法判断 | 4 | 合法输出"不知道" |

### 案例结构

```yaml
case_id: fact-001
input: "水在标准大气压下通常多少摄氏度沸腾？"
user_context: {}
requested_depth: null          # null = 交给路由决定
expected_properties:
  - depth_level: D0
  - philosophical_analysis: not_run
  - response_contains: "标准大气压"
  - stop_reason_present: true
forbidden_properties:
  - hypotheses_count_gt: 1
  - unsupported_certainty: true
expected_state: COMPLETED
expected_memory_behavior: none
```

> **测试重点不是输出固定句子，而是检查结构属性**（任务书 §16.2）。
> 自然语言回答的具体措辞是模型的自由，但"路由到 D0""没有启用哲理分析"
> "包含限定条件"这些是**可断言的**。

---

## 5. 离线评估：Baseline vs Candidate

提案评估（`proposal_generator` 的产物）必须能在历史数据上对比：

```
Baseline  : 旧策略
Candidate : 候选策略
```

对比维度（任务书 §11.4）：

| 维度 | 要求 |
|---|---|
| 任务完成质量 | Candidate 不得显著下降 |
| 事实与假设混淆率 | 不上升 |
| 过度自信率 | 不上升 |
| 反刍率 | 不上升 |
| Token 成本 | 有明确数值对比 |
| 平均延迟 | 有明确数值对比 |
| 纠正后复发率 | 不上升 |
| **其他场景退化程度** | **必须检查**——不能只看目标指标 |

> **"其他场景退化程度"是本清单里最容易被跳过、也最重要的一项。**
> 一个只改善目标指标却让三个无关场景变差的提案，是净负面改动。

---

## 6. 回放（阶段 7）

`POST /api/v1/replay/cognitive-rounds/{round_id}`

**回放规则：**

1. **回放不覆盖原始事件。** 回放是只读重演，产物写入独立结果。
2. 回放使用**原回合记录的 Prompt 版本**（这正是 Prompt 版本必须留存的原因，
   不变量 18 与 `prompt_contracts.md` §2 规则 3）。
3. 回放结果必须携带**运行版本信息**：
   代码版本、Prompt 版本、模型与 Provider、预算配置。
   没有版本信息的评测结果无法归因。
4. 回放可以指定使用不同的策略（用于 Baseline/Candidate 对比），
   但**原始回合保持不变**。

---

## 7. 报告产物

`evals/reports/` 下输出 Markdown 与 JSON 双格式：

- **JSON**：机器可读，供 CI 比对与趋势追踪。
- **Markdown**：人可读，供人工评审提案时参考。

报告必须包含运行版本信息（§6.3）。
`evals/reports/` 的具体产物**不进 git**（见 `.gitignore`），
只保留 `.gitkeep`——报告是可再生产物，不应污染提交历史。

---

## 8. 可复现性清单（阶段 7 · S3，**已实现**）

一份评测结果若只有"10 个案例通过"，一周后是没法被理解的：不知道是哪个提交
跑的、数据集有没有改过、提示词是不是同一版。每次运行现在都会产出一份
**ReproducibilityManifest**，把这些身份钉在结果里。

### 字段与来源

| 分组 | 字段 | 权威来源 |
|---|---|---|
| `code` | `commit_sha` / `working_tree_clean` | `git rev-parse HEAD` / `git status --porcelain` |
| | `package_version` | `ai_psi.__version__` |
| `evaluation` | `case_schema_version` | `evaluation/models.py::CASE_SCHEMA_VERSION` |
| | `dataset_digest` / `dataset_case_count` | 加载并规范化后的案例 |
| | `assertion_registry_digest` | `evaluation/assertions.py::registry_digest()` |
| | `execution_mode` | `in_memory` / `postgres_http` |
| `prompts` | `versions` / `digest` | **实际发生**的模型调用记录（`ModelInvocationInfo`，不变量 18） |
| `provider` | `provider_name` / `model_id` / `deterministic` | 装配结果 + 配置白名单 |
| | `network_allowed` | 恒为 `false`（S1a/S2 只跑 Mock） |
| | `configuration_digest` | 白名单参数 |
| `runtime` | `python_version` / `ai_psi_version` | `sys.version_info` / 包版本 |
| `storage` | `backend` / `alembic_revision` | 执行模式 / 库上读到的 revision |

⚠️ **Prompt 版本记的是"这次运行真的走到过哪些"**，不是"仓库里注册了哪些"。
数据源是不变量 18 要求的模型调用记录，因此可被证明；不经过的组件不会出现。

### digest 的语义

- 算法一律 **SHA-256**，输出形如 `sha256:<hex>`；
- `dataset_digest` 基于**规范化后的案例语义**，不是 YAML 原始字节——
  注释、缩进与键的书写顺序都不影响它，语义变化才会；
- `assertion_registry_digest` 覆盖每条断言的判定语义，含一个人工维护的
  `semantics_version`（改比较逻辑必须递增它；只改文案不必）；
- `prompts.digest` 只覆盖实际使用过的组件；
- `provider.configuration_digest` 只基于**白名单**参数——密钥变化不改变它，
  行为参数变化才改变。

### 原始结果与 canonical 的区别

- **原始 JSON** 带完整清单；
- **canonical JSON** 只带稳定身份子集：不含 `python_version`（补丁版本是
  环境属性，换台机器不该表现为"不可比"）、不含 `working_tree_clean`
  （那属于"能不能当基线"的判定）、不含数据库名（每次运行的评测库都不同）。

### comparability 只判断身份契约

`compare_manifests()` 回答"两份结果能不能放在一起比"，返回布尔值**加**
机器可读的原因码：`dataset_differs` / `prompt_versions_differ` /
`provider_differs` / `model_differs` / `code_revision_differs` /
`dirty_worktree` / `identity_unavailable` 等。

🔴 **它不判断性能回归。** 哪个更好属于后续切片；本切片也不计算通过率差异、
回归数量或任何发布结论。

### 秘密永不进入报告

清单只输出白名单字段。API key、Authorization、数据库 URL、用户名、密码、
完整环境变量集合一律不进——配置摘要本身就基于白名单结构计算。

### dirty worktree 与无 `.git` 环境

| 情形 | 处理 |
|---|---|
| 工作树脏 | **如实记录** `working_tree_clean: false`；可比较性给出 `dirty_worktree`。工具**不会**替你清理工作树 |
| 无 `.git`（wheel 安装等） | `commit_sha` 记为**不可用**（`null`），**不编造**；可比较性给出 `identity_unavailable` |
| `--require-reproducible` | 上述两种情形在**执行第一个案例之前**以退出码 `4` 失败 |

### 尚未实现（属后续切片）

Baseline/Candidate 对比、Markdown 报告、发布阈值、真实 Provider 评测。
本文件 §5 与 §7 描述的是**目标形态**，不是当前实现状态。

---

## 9. 指标层（阶段 7 · S4，**已实现**）

> ⚠️ 本节说的指标与 §2 的**不是同一回事**。§2 列的是**系统质量指标**
> （`cognitive_round_success_rate`、`conflict_preservation_rate` …），
> 那是任务书 §16.1 的目标形态，**尚未实现**。本节讲的是**单次运行
> 产生了什么可解释的聚合结果**——它只保证统计是对的，不评价系统好不好。

实现位置：`src/ai_psi/evaluation/metrics.py`。

### 契约身份

| 项 | 值 |
|---|---|
| `metrics_schema_version` | `1` |
| `metrics_definition_digest` | SHA-256，覆盖下方全部定义 |
| 比率精度 | **固定 6 位小数** |
| 舍入 | **`ROUND_HALF_UP`**（用标准库 `Decimal`，不用二进制浮点） |
| 分母为 0 | `value` 为 **`null`**，**绝不写成 `0.000000`** |

🔴 改任何一个分母口径或精度，都必须让摘要变——否则等于宣称两份按不同
规则算出来的数字可以放在一起看。摘要覆盖：schema 版本、精度、舍入、
四个比率的各自分母、不可观测分类规则、部分运行规则。

### 核心计数

| 计数 | 定义 |
|---|---|
| `total_cases` | **加载成功**的数据集案例总数（含未执行的） |
| `executed_cases` | 真的进入执行并产出了结果的案例数 |
| `passed_cases` / `failed_cases` | 执行过的案例里通过 / 未通过的 |
| `not_executed_cases` | `total_cases - executed_cases`。🔴 **不是失败案例** |
| `execution_error_cases` | 失败中"**没跑起来**"的（应用异常 / HTTP 错误 / 存储错误 / 无观测） |
| `assertion_failed_cases` | 失败中"**跑起来了但断言没过**"的 |

不变量（构造时强制，违反即报错）：

```
passed_cases + failed_cases              == executed_cases
executed_cases + not_executed_cases      == total_cases
execution_error_cases + assertion_failed_cases == failed_cases   # 互斥且完备
```

### 比率的分子与分母

| 比率 | 分子 | 分母 |
|---|---|---|
| `case_pass_rate` | `passed_cases` | **`executed_cases`** |
| `execution_coverage` | `executed_cases` | `total_cases` |
| `assertion_pass_rate` | `passed` | **`evaluated`**（不是 `total`） |
| `observation_coverage` | `evaluated` | `total` |

🔴 **不用 `passed_cases / total_cases` 当唯一通过率**：那会把"失败"与
"压根没跑"混成同一个数字——前者说明结果不对，后者说明这次没覆盖到。

### 断言统计

每条断言按三个分类计数，**只看结构化字段**：

| 分类 | 判据 |
|---|---|
| `evaluated` | `observation_status == "observed"` |
| `unobservable` | `observation_status == "unobservable"` |
| `passed` / `failed` | 在 `evaluated` 之内再按 `passed` 分 |

要求：

* `passed + failed == evaluated`；
* `evaluated + unobservable == total`；
* **不可观测与"比较后失败"分开统计**——"没读到"与"读到了但对不上"
  是两回事，混在一起会让"系统在这个断言上表现如何"无从判断。

⚠️ **案例判定语义没有因此放宽**：不可观测的断言**仍然**让案例不通过。
分开的是**统计口径**，不是**判定规则**。

### 按类别与按断言名

* 类别只输出**数据集中实际出现**的（本切片 3 类），按类别名排序。
  不输出"全部 12 个合法类别"的空壳——那会让报告里出现 9 行全 0，
  读者分不清"这个类别没案例"与"这个类别全没过"。
* 断言名只输出**实际出现过**的（本切片 15 个），按名字排序。
  同一个名字同时以 `required` 与 `forbidden` 出现时分开记。
* 类别之和、断言名之和必须与总体逐项相等（构造时校验）。

### 分布

`final_state_distribution` / `depth_distribution` / `stop_reason_distribution`。

* **只统计已执行的案例**——没跑起来的没有终态；
* 枚举按**正式枚举顺序**（`d0` 在 `d1` 前），其余按字典序；
* 空值用明确键 `__none__`；
* 未知但真实出现的值**不丢弃**；
* 值取自 `CaseObservation` 的结构化字段，**不从 `response_text` 抽取**，
  **不从 `failure_reason` 推断**。

### 失败索引

`failed_case_ids` / `execution_error_case_ids` / `assertion_failure_case_ids` /
`unobservable_case_ids` / `failed_assertions_by_case`。

只放 **ID 与稳定分类**，不放 `response_text`、异常堆栈、数据库 URL 或
任何 secret。用途是"去哪查"，不是"把报告再抄一遍"。

### 部分运行与全局失败

| 情形 | 处理 |
|---|---|
| **A. 数据集加载失败** | **不产生指标**。CLI 返回退出码 2，一条案例都不跑 |
| **B. 部分案例执行失败** | 生成指标；`execution_error_cases` 正确计数；CLI 返回非零；已执行结果保留 |
| **C. 运行级中止** | `total_cases` 保持加载后的总数；`executed_cases` 为实际数量；差额进 `not_executed_cases` |

### 指标不是什么

🔴 **它不是发布阈值，也不是质量结论，更不是版本比较。**

* 不比较两次运行；不判断回归或改进；
* 不设任何门禁；
* 不计算 attribution 漏报率（R46/R58）；
* 输出的是 `case_pass_rate` 与 `assertion_pass_rate` 这类**描述性**名称；
  **不使用** `accuracy` —— 本切片没有带标签的样本，那个词会暗示一种
  这里并不存在的"正确率"。

**10 个案例全过不等于系统质量达标。** 那需要 50+ 个案例、真实 Provider、
以及有标签的评测集。本层只保证：这些数字的算法是可复算的、分母是透明的。
