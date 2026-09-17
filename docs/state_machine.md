# 认知回合状态机

> 实现位置：`src/ai_psi/cognition/state_machine.py`
> 相关：ADR-0002（事件与状态）、ADR-0008（超时与预算）、ADR-0010（决策语义）

---

## 1. 状态定义

| 状态 | 含义 |
|---|---|
| `CREATED` | 回合已创建，尚未开始处理 |
| `TRIAGING` | 关切识别：判断这个输入是否值得启动认知 |
| `FRAMING` | 问题框定：把关切转成可结束的问题 |
| `RETRIEVING` | 检索证据与相关记忆，构建上下文 |
| `ANALYZING` | 分析：认知分类、概念、逻辑、因果、辩证、哲理 |
| `DELIBERATING` | 假设生成与评估，形成暂定判断 |
| `METACOGNITIVE_REVIEW` | 元认知检查：继续 / 换方法 / 收窄 / 降置信 / 等证据 / 停止 |
| `SYNTHESIZING` | 合成面向用户的回答内容 |
| `RESPONDING` | 渲染自然语言并交付 |
| `COMPLETED` | **终态**：回合完成，已记录停止原因 |
| `WAITING_FOR_EVIDENCE` | 暂停：等待外部证据或用户补充材料 |
| `SUSPENDED` | **终态**：升级为研究任务，本回合挂起 |
| `FAILED` | **终态**：失败，已记录失败阶段与错误类别 |
| `CANCELLED` | **终态**：被用户或系统取消 |

**终态（4 个）**：`COMPLETED` / `SUSPENDED` / `FAILED` / `CANCELLED`。
终态不可再转移——重新激活需要创建一个**新的回合**，并以 `causation_id`
指向原回合，以保持审计链完整。

---

## 2. 状态转移表

这是**唯一权威**的转移定义，代码必须与本文一致（属性测试逐条覆盖）。

| 当前状态 | 允许转移到 |
|---|---|
| `CREATED` | `TRIAGING`, `FAILED`, `CANCELLED` |
| `TRIAGING` | `FRAMING`, `SYNTHESIZING`¹, `FAILED`, `CANCELLED` |
| `FRAMING` | `RETRIEVING`, `FAILED`, `CANCELLED` |
| `RETRIEVING` | `ANALYZING`, `WAITING_FOR_EVIDENCE`, `FAILED`, `CANCELLED` |
| `ANALYZING` | `DELIBERATING`, `RETRIEVING`, `FAILED`, `CANCELLED` |
| `DELIBERATING` | `METACOGNITIVE_REVIEW`, `ANALYZING`, `FAILED`, `CANCELLED` |
| `METACOGNITIVE_REVIEW` | `ANALYZING`, `DELIBERATING`, `RETRIEVING`, `SYNTHESIZING`, `WAITING_FOR_EVIDENCE`, `SUSPENDED`, `FAILED`, `CANCELLED` |
| `SYNTHESIZING` | `RESPONDING`, `FAILED`, `CANCELLED` |
| `RESPONDING` | `COMPLETED`, `FAILED`, `CANCELLED` |
| `WAITING_FOR_EVIDENCE` | `RETRIEVING`, `CANCELLED` |
| `COMPLETED` | —（终态） |
| `SUSPENDED` | —（终态） |
| `FAILED` | —（终态） |
| `CANCELLED` | —（终态） |

> ¹ `TRIAGING → SYNTHESIZING` 是**关切识别为空**时的快捷路径：
> 没有值得启动认知的关切，直接进入合成（通常是 D0 直答或明确说明不需分析）。
> 这条边**只能**由 `TRIAGING` 走出，其他状态不得直接跳到 `SYNTHESIZING`。

### 2.1 标准路径

```
CREATED → TRIAGING → FRAMING → RETRIEVING → ANALYZING → DELIBERATING
        → METACOGNITIVE_REVIEW → SYNTHESIZING → RESPONDING → COMPLETED
```

### 2.2 元认知分支

`METACOGNITIVE_REVIEW` 的出口由 `MetacognitiveDecision` 决定（ADR-0010）：

| MetacognitiveDecision | 目标状态 |
|---|---|
| `CONTINUE` | `ANALYZING` 或 `DELIBERATING` |
| `CHANGE_METHOD` | `RETRIEVING` 或 `ANALYZING` |
| `NARROW_SCOPE` | `DELIBERATING` |
| `LOWER_CONFIDENCE` | `SYNTHESIZING` |
| `REQUEST_EVIDENCE` | `RETRIEVING` |
| `WAIT` | `WAITING_FOR_EVIDENCE` |
| `STOP` | `SYNTHESIZING` |
| `ESCALATE_TO_RESEARCH` | `SUSPENDED` |

---

## 3. 禁止的转移（必须被拒绝）

以下转移在任何情况下都非法，属性测试需逐一验证被拒绝：

| 禁止转移 | 原因 |
|---|---|
| `CREATED` → `SYNTHESIZING` / `COMPLETED` | 跳过全部认知过程 |
| `CREATED` → `RESPONDING` | 没有判断就回答 |
| 任意状态 → `COMPLETED`（非经 `RESPONDING`） | 完成必须经过回答 |
| `TRIAGING` → `ANALYZING` | 跳过问题框定 |
| `FRAMING` → `ANALYZING` | 跳过检索 |
| `SYNTHESIZING` → `ANALYZING` | 合成阶段不得回退分析（要回退须先经元认知） |
| `RESPONDING` → `ANALYZING` | 回答一旦开始渲染不得回退 |
| 任意终态 → 任意状态 | 终态冻结 |
| `WAITING_FOR_EVIDENCE` → 除 `RETRIEVING`/`CANCELLED` 外任何状态 | 等待只能被证据唤醒 |

---

## 4. 硬约束

这些约束在**每次转移时**由 `state_machine` 校验，违反则抛
`IllegalStateTransitionError`。

### 4.1 计数约束

| 约束 | 默认值 | 超出时 |
|---|---|---|
| 每回合最大模型调用次数 | 12（D0=3…D4=12，ADR-0008） | → `FAILED`，记录 `BudgetExhaustedError` |
| 最大元认知循环次数 | 2 | 强制 `STOP` → `SYNTHESIZING` |
| 最大假设数量 | 4 | 生成器截断，不报错 |

### 4.2 反刍约束

进入 `METACOGNITIVE_REVIEW` 时检查：连续两轮的
**新增证据数 == 0 且 新增推理路径 == 0 且 文本重复度超阈值**
→ **强制 `STOP`**，停止原因记为 `NO_MARGINAL_COGNITIVE_GAIN`。

对应任务书 §13.3 与不变量 8："没有新证据或新路径时，元认知不得允许无限继续。"

### 4.3 完整性约束

- 进入 `COMPLETED` 前**必须**已记录停止原因（不变量 19）。
- 进入 `FAILED` 时**必须**记录失败阶段与错误类别（不变量 20）。
- 进入 `RESPONDING` 前，`Judgment` 必须已持久化——
  这样即使渲染失败，已完成的分析工作不丢失。

### 4.4 幂等与并发

- 同一回合的同一状态转移**不可重复执行**（由 `version` 乐观锁保证）。
- API 重试须携带 `Idempotency-Key`，不得创建重复回合。
- 状态转移与事件写入在**同一事务**中提交（ADR-0002）。

### 4.5 超时

每个状态有超时（完整表见 ADR-0008）。两条特别约定：

- `METACOGNITIVE_REVIEW` 超时 **不判失败**，降级为强制 `STOP` → `SYNTHESIZING`；
- 所有状态超时都受回合级 `max_duration_seconds` 总上限约束。

### 4.6 失败不破坏已写入事件

回合进入 `FAILED` 时，此前已提交的事件**保持有效**。
失败本身也写入 `cognitive_round.failed` 事件。

---

## 5. 与事件的关系

每次状态转移都写入 `cognitive_round.state_changed` 事件，payload 至少含：

```json
{
  "from_state": "ANALYZING",
  "to_state": "DELIBERATING",
  "reason": "analyzer_completed",
  "budget_snapshot": {"model_calls_used": 4, "metacognitive_loops": 0}
}
```

状态转移**不是**唯一真相来源——事件日志才是。当前状态是其投影（ADR-0002）。

---

## 6. 测试要求

| 测试类型 | 覆盖内容 |
|---|---|
| 单元 | 每条合法转移被接受；每条禁止转移被拒绝；终态冻结 |
| 属性（Hypothesis） | **任意**状态序列下：非法跳转恒被拒绝；循环次数恒不超预算；终态恒不可离开 |
| 集成 | 完整回合走通全部状态；失败路径记录阶段与错误类别；超时路径行为正确 |
| 场景 | 场景 G（反刍停止）验证强制 STOP 与停止原因 |

**属性测试是本文件最重要的验收手段**——
它不枚举用例，而是断言"无论怎么走都不会违反约束"。
