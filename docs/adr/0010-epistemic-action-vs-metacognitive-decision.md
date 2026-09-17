# ADR-0010：EpistemicAction 与 MetacognitiveDecision 的职责划分

- **状态**：已接受
- **日期**：2026-09-17
- **相关**：ADR-0008（预算与深度路由）、`docs/state_machine.md`

## 背景

**这是一处任务书内部的语义缺口。**

任务书 §5.10 定义了 `Reflection.decision: MetacognitiveDecision`：

```
CONTINUE / CHANGE_METHOD / NARROW_SCOPE / LOWER_CONFIDENCE
REQUEST_EVIDENCE / WAIT / STOP / ESCALATE_TO_RESEARCH
```

任务书 §5.9 定义了 `Judgment.recommended_epistemic_action: EpistemicAction`。

**但 `EpistemicAction` 全书没有给出成员列表。**
而从名字与上下文看，它与 `MetacognitiveDecision` 高度重叠——
两者都涉及"需要更多证据""等待""停止"等语义。

若不加区分地定义，会出现两个并行的、语义交叉的枚举，
后续必然产生"这个情况该用哪个"的持续混乱。

## 决策

**两者服务于不同对象、不同层次，必须分开定义并固化映射关系。**

### 1. MetacognitiveDecision —— 回合的内部控制流

回答的问题是：**"这个认知回合接下来怎么走？"**

它直接驱动状态机转移（任务书 §6.2）：

| MetacognitiveDecision | 状态机目标 |
|---|---|
| `CONTINUE` | → `ANALYZING` / `DELIBERATING` |
| `CHANGE_METHOD` | → `RETRIEVING` / `ANALYZING` |
| `NARROW_SCOPE` | → `DELIBERATING`（收窄后重新判断） |
| `LOWER_CONFIDENCE` | → `SYNTHESIZING`（降低置信度后合成） |
| `REQUEST_EVIDENCE` | → `RETRIEVING`（再检索一轮） |
| `WAIT` | → `WAITING_FOR_EVIDENCE` |
| `STOP` | → `SYNTHESIZING` |
| `ESCALATE_TO_RESEARCH` | → `SUSPENDED` |

它是**过程性**的，用户通常看不到。

### 2. EpistemicAction —— 本次判断的认知处境与建议

回答的问题是：**"就这个问题而言，我们现在处于什么认知状态、建议用户怎么办？"**

定义为：

```python
class EpistemicAction(StrEnum):
    ANSWER              # 可以给出结论
    ANSWER_WITH_CAVEAT  # 可以回答，但必须带明确限定
    REQUEST_EVIDENCE    # 缺关键证据，建议补充材料
    WAIT                # 已发出检索请求，等待结果
    DEFER               # 当前能力/资料不足，暂不判断
    OUT_OF_SCOPE        # 事实无法决定的价值选择，超出认知系统职责
```

它是**结果性**的，是 `Judgment` 的一部分，会传达给用户。

### 3. 映射关系

`Reflection.decision` 与 `Judgment.recommended_epistemic_action` 由
`judgment_synthesizer` 同时产出，映射规则：

| MetacognitiveDecision | 典型 EpistemicAction |
|---|---|
| `STOP`（材料充分） | `ANSWER` |
| `STOP`（存在未解决冲突/未知） | `ANSWER_WITH_CAVEAT` |
| `LOWER_CONFIDENCE` | `ANSWER_WITH_CAVEAT` |
| `NARROW_SCOPE` | `ANSWER_WITH_CAVEAT` |
| `REQUEST_EVIDENCE` | `REQUEST_EVIDENCE` |
| `WAIT` | `WAIT` |
| `ESCALATE_TO_RESEARCH` | `DEFER` |
| 事实无法决定价值选择 | `OUT_OF_SCOPE` |

**映射不是双向的。** 多个 `MetacognitiveDecision` 可映射到同一
`EpistemicAction`，因为控制流的差异（如何收窄、为何降低置信度）
不必全部暴露给用户。

### 4. `OUT_OF_SCOPE` 的特别含义

`OUT_OF_SCOPE` 对应任务书 §9.10 的合法判断之一：
"事实无法决定价值选择"。

它**不是失败**，而是一个诚实的认知结论。
任务书 §9.9 明确要求："不得假装价值冲突存在唯一科学答案"。
因此系统必须能明确说出"这个问题的答案不由事实决定"，
而不是勉强给出一个看似确定的结论。

## 替代方案与取舍

| 方案 | 为什么不选 |
|---|---|
| 合并成一个枚举 | 会混淆"内部控制流"与"对外建议"两个层次；状态机需要前者，用户需要后者 |
| 复用 MetacognitiveDecision 作为 EpistemicAction | `NARROW_SCOPE` / `CHANGE_METHOD` 等对用户毫无意义，是内部实现细节 |
| EpistemicAction 用自由文本 | 违反任务书"所有重要认知产物必须是结构化对象"；无法做一致性校验 |
| 不给 `OUT_OF_SCOPE` | 会逼迫系统在价值问题上伪装确定性，违反不变量 3 与 §9.9 |

## 影响

- `Judgment` 与 `Reflection` 的单元测试需覆盖全部映射分支。
- **Response Renderer 的一致性校验**（任务书 §9.12）依赖
  `EpistemicAction`：`ANSWER` 对应强结论，`ANSWER_WITH_CAVEAT` /
  `DEFER` / `OUT_OF_SCOPE` 对应弱结论。
  "最终回答的结论强度不得高于内部 Judgment 的结论强度"这条不变量，
  在实现上就是通过 `EpistemicAction` 到回答语气档位的映射来强制的。
- 两个枚举都放进 `domain/enums.py`，并在 `docs/domain_model.md` 中并列说明，
  避免后续开发者再次混淆。
