# ADR-0016：真实 LLM Provider（阶段 4）

- **状态**：已接受
- **日期**：2026-09-18
- **相关**：ADR-0003（Provider 抽象）、ADR-0008（预算）、ADR-0012（范围边界）、
  ADR-0015（阶段 3 实现决策）

## 背景

阶段 4 的交付是"真实 LLM Provider"。用户明确指定**把 LLM 改成 DeepSeek
的 `deepseek-v4-flash`**，因此本阶段的实现与验证全部围绕真实 DeepSeek 展开。

本 ADR 记录三类事情：

1. 与任务书字面要求的**有意的偏离**；
2. 为了接住真实模型的行为而做的**设计决定**；
3. **只有真实模型才能暴露的缺陷**——这是本阶段最有价值的部分。

---

## 1. Provider 选择：DeepSeek（OpenAI 兼容），Anthropic 延后

### 决策

实现 **`openai_compatible`** 一个通用 Provider（覆盖 DeepSeek、OpenAI 及
绝大多数兼容 OpenAI 协议的服务），并提供 **`deepseek`** 预设
（base_url `https://api.deepseek.com/v1`，默认模型 `deepseek-v4-flash`）。

### 为什么不实现 Anthropic

任务书 §8.1 要求 Provider 必须实现 Anthropic。但用户指定用 DeepSeek，
而 Anthropic 的协议（`/v1/messages`）与 OpenAI 协议不同，需要另写一个实现。

**写一个既没有测试、又无法实际跑通的 Provider，产出的是"看起来有能力"的空壳**，
正是 ADR-0012 明令避免的东西。因此本阶段**不写** Anthropic，
配置它会在装配时给出明确提示而不是静默回落。

后续要加时，它是 `providers/` 下的一个新文件 + 注册表里的一行，与既有代码无关。

### 实测确认的接口事实

| 项 | 值 |
|---|---|
| 端点 | `POST https://api.deepseek.com/v1/chat/completions` |
| 模型 id | `deepseek-v4-flash`（实测与 `deepseek-flash` 等价；另有 `deepseek-v4-pro`） |
| JSON 模式 | `response_format={"type":"json_object"}` 可用 |
| 响应结构 | OpenAI 兼容；**额外带 `reasoning_content`**（见 §3） |
| 用量 | `usage.prompt_tokens` / `completion_tokens` / `completion_tokens_details.reasoning_tokens` |
| 接受的 max_tokens | 实测 ≥ 18000 不被拒绝 |

---

## 2. 偏离任务书 §8.1：Provider 返回值从 `T` 改为 `ProviderResponse[T]`

### 决策

```python
async def generate_structured[T: BaseModel](...) -> ProviderResponse[T]
async def generate_text(...) -> ProviderResponse[str]
```

`ProviderResponse` 携带 `value` / `usage` / `finish_reason` / `raw_hash` / `output_repair`。

### 原因

任务书 §8.2 要求记录 `input_token_count` / `output_token_count`，
而**只有 Provider 自己知道它们**。要在不改变调用形状的前提下拿到用量，
常见的做法是让 Provider 记一个 `last_usage` ——那是**状态**，
在并发下会串号，而且把"哪一次调用的用量"变成了时序问题。

顺带解决的第二件事：`finish_reason` 是**截断检测的唯一依据**（见 §4），
它必须跟着返回值一起回来。

### 影响

Mock Provider、网关、调用点全部适配。这是本阶段唯一一处**破坏性**接口变更，
任务书 §8.1 的签名因此不再逐字一致——已在本书顶部的偏离清单中登记。

---

## 3. 推理模型的 `reasoning_content`：只记数量，不记内容

### 事实

`deepseek-v4-flash` 是推理模型：响应的 `message` 同时包含 `content` 与
**`reasoning_content`**（完整的内部思维链），用量里另有
`completion_tokens_details.reasoning_tokens` 计数。

### 决策

* Provider **只读 `content`**。`reasoning_content` 连"顺手带上"都不做——
  不读它就写不进日志、写不进事件（认知宪法红线一）。
* `TokenUsage.reasoning_tokens` 保留，`ModelInvocationInfo.reasoning_token_count`
  新增该字段（**只是计数**，属于成本信息）。

### 为什么计数可以留、内容不可以

计数回答的是"这次为什么更贵"，是成本核算需要的；
内容属于"完整隐藏思维链"，红线一明令不保存。两者是不同的东西。

### 验证

* `tests/unit/test_openai_compatible_provider.py`：响应里塞入一个哨兵字符串，
  断言它不出现在返回对象的任何地方；
* `tests/integration/test_live_provider.py`：真实回合跑完后扫描**全部事件**，
  断言事件流里没有 `reasoning_content`。

---

## 4. 截断：在解析之前判定，且不可重试

### 问题（阶段 4 实测发现）

一次真实回合失败，报的是"模型输出无法解析为 JSON 对象
（已尝试原样 / 去代码块 / 括号配平扫描）"。

**这个诊断是错的。** 真实原因是被 `finish_reason=length` 截断——
截断的 JSON 当然解析不了。两处具体的害处：

1. 错误信息指向一个不存在的问题（"JSON 语法"），真正的原因（上限太小）无从得知；
2. `StructuredOutputError` 默认**可重试**，于是同样的上限被反复撞上，
   预算被烧掉，问题却还在。

### 决策

* Provider 在**解析之前**检查 `finish_reason`，截断直接抛
  `StructuredOutputError(retryable=False)`；
* 网关保留一道**兜底**检查，防的是不遵守协议约定的第三方 Provider；
* 错误信息里明确写出可操作的两个旋钮（`max_output_tokens` 与
  `AI_PSI_LLM_REASONING_HEADROOM_TOKENS`）。

---

## 5. 可选模块失败 → 降级，而不是丢掉整个回合

### 问题（阶段 4 实测发现）

一次 D2 的真实回合，`logical_analyzer` 因截断失败，
**整个回合跟着失败**——用户明明只差最后一步就能拿到回答。

### 决策

`ANALYZING` 阶段的模块本来就是"预算不够就跳过"的**可选**步骤
（见 `MODULE_MATRIX`）。既然预算不足可以跳过，**模型侧出问题也应当同样处理**：
捕获 `ProviderError`，记入 `skipped_steps` 并继续。

依据：

* 任务书 §13.2 明确允许降级（"真实 LLM 不可用时……可返回当前认知服务降级一类的提示"）；
* 阶段 4 的验收条件逐字写着 **"解析异常不会污染状态"**。

**强制模块（判断合成、回答渲染）不在此列**——跳过它们的"降级"等于没有回答。

### 降级必须留痕

被跳过的步骤进入 `RoundOutcome.skipped_steps`，并随**终态事件**落库
（`transition(..., diagnostics=...)` 的新参数），同时出现在
`GET /cognitive-rounds/{id}/summary` 的 `skipped_steps` 字段里。

**理由**：降级本身可以接受，但事后必须能分辨"少做了一个分析"
与"分析跑了但没产出"。只活在内存里的 `RoundOutcome` 上等于没人知道。

### 同时修掉的预算缺陷

元认知裁定 `CHANGE_METHOD` 会**再次**进入 `ANALYZING`，
而那时上一次判断已经把保留额度降到了"只剩渲染"（1）。
于是一轮可选分析刚好把额度花到 0，接下来的判断合成与回答渲染都没有额度——
回合以 `BudgetExhaustedError` 失败。

修法：**保留额度在进入分析阶段时就抬高**（判断 + 元认知 + 渲染三重保留），
而不是等分析结束才设。宁可少跑一个可选分析（跳过会被记录），
也不能拿不出结论。

回归测试：`tests/scenarios/test_scenarios_f_j.py::test_change_method_keeps_the_mandatory_tail`。

---

## 6. 缺 API Key：明确失败，**不**静默回落到 Mock

任务书 §18 允许"自动使用 Mock **或**明确失败"二选一。本项目选后者。

**静默回落会让一次配置失误伪装成"系统跑得很好"**：用户以为在跟真实模型对话，
实际拿到的是规则引擎拼出来的结构化占位输出，而且没有任何地方会告诉他。

Mock 仍然可以通过显式配置（`AI_PSI_LLM_PROVIDER=mock`）使用——
只是它必须是一个**被选择**的结果。

---

## 7. 推理预留与契约输出上限：按实测标定

### 数据（31 次真实调用）

| 任务 | 输出均值/最大 | 推理均值/最大 |
|---|---|---|
| `concern_detector` | 366 / 516 | 170 / 274 |
| `inquiry_framer` | 2081 / 2421 | 1430 / 1695 |
| `hypothesis_generator` | 2546 / 4363 | 1676 / 2107 |
| `logical_analyzer` | 6801 / 7294 | 5496 / 5496 |
| `causal_analyzer` | 2502 / 4689 | 1664 / 3000+ |
| `concept_analyzer` | 7147 / 7147 | 5110 / 5110 |
| `dialectical_analyzer` | 2877 / 2877 | 1521 / 1521 |
| `philosophical_analyzer` | 3307 / 3307 | 1954 / 1954 |
| `judgment_synthesizer` | 1699 / 2340 | 975 / 1634 |
| `metacognition` | 2290 / 3216 | 2102 / 3019 |
| `response_renderer` | 1119 / 2080 | 683 / 1367 |

### 决策

1. **`DEFAULT_REASONING_HEADROOM = 16384`。**
   🔴 **它是"上限"不是"花费"**：模型没有生成到那么多 token 就不会被计费，
   但上限设小了会截断，而截断要丢掉一次分析甚至整个回合。因此宁可宽、不可紧。
2. **配置层的默认值是 `None`（= 用 Provider 的默认值）。**
   阶段 4 踩过一次坑：配置层与 Provider 层各写了一个默认值，
   改 Provider 的那个"毫无效果"——配置层的 1024 静默覆盖了它。
   **两个都看起来权威的默认值，比没有默认值更难排查。**
   回归测试：`tests/unit/test_provider_parsing_and_registry.py::TestReasoningHeadroomPlumbing`
   （含一条 `0` 必须被当作合法配置、而不是"没配"的断言）。
3. **按实测重标定五个契约的输出上限**：`logical_analyzer` 1500→1800、
   `causal_analyzer` 1200→2000、`concept_analyzer` 1800→2600、
   `dialectical_analyzer` 1500→1800、`metacognition` 1200→1500。

### 版本号跟踪的是模板正文，不是传输参数

上述 2.3 的改动**没有**递增 Prompt 版本号：`max_output_tokens` 是发给供应商的
**上限**，它不改变提示词说了什么，也就不改变模型的行为（除非小到截断，
那时是配置错误而非语义变化）。改动与依据记录在本 ADR，模板正文一字未改。

反过来，任何改动 `templates/` 下文件内容的变更**必须**递增版本号。
这条约定写在 `prompts/versions.py` 的注释里。

---

## 8. 配置层的两处调整

1. **密钥同时接受业界通用名**：`deepseek_api_key` 用 `AliasChoices`
   同时认 `AI_PSI_DEEPSEEK_API_KEY` 与 `DEEPSEEK_API_KEY`。
   要求用户为同一个密钥再配一份不必要，而"密钥只从环境变量来"这条约束没有放松。
2. **测试环境隔离密钥**：`tests/conftest.py` 的 autouse 夹具会把供应商密钥
   从测试环境里摘掉（`live` 标记的用例除外）。
   开发机上往往**真的**配着 `DEEPSEEK_API_KEY`，如果测试依赖"环境里恰好没有密钥"，
   它们就会在开发机上失败、在 CI 上通过——那是最难解释的一类测试失败。

---

## 9. 真实模型暴露的缺陷汇总

**这一节是本阶段最有价值的部分。** 上述缺陷**没有一个是 Mock 能测出来的**——
Mock 对任何模型名照单全收、对任何输出长度照单全收。

| # | 缺陷 | 影响 | 修法 |
|---|---|---|---|
| 1 | `_make_gateway` 把 `settings.llm_model or "mock-model-v1"` 当模型名 | 未显式配置时把 Mock 的模型名发给真实供应商 → **DeepSeek 直接 400**，回合在建关切阶段就失败 | 在 `CognitiveRuntime.__init__` 里用 `resolve_model(settings)` 解析一次；回归测试 `test_runtime_model_binding.py`（不联网） |
| 2 | `concern_detector` 对直接提问返回空关切列表 | 回合以 `NO_CONCERN_DETECTED` 结束，**用户拿不到任何回答** | 提示词 v1.1.0：明确"用户正在提问时至少必须输出一个 `user_request` 关切"，空列表只适用于系统自发触发的场景 |
| 3 | 截断被误报成 JSON 语法错误 | 诊断指向不存在的问题；且可重试 → 反复烧预算 | 解析前判截断，标为不可重试（§4） |
| 4 | 可选模块失败拖垮整个回合 | 用户拿不到回答 | 降级 + 留痕（§5） |
| 5 | `CHANGE_METHOD` 路径耗尽强制尾部额度 | 回合以 `BudgetExhaustedError` 失败 | 保留额度在进入分析阶段时抬高（§5） |
| 6 | 推理预留有两个默认值，配置层静默覆盖 Provider 层 | "改了默认值却毫无效果" | 配置层默认改为 `None`（§7） |

---

## 10. 成本：一个必须被说清楚的数字

实测（`deepseek-v4-flash`，一次完整回合）：

| 深度 | 模型调用 | 输入 token | 输出 token | 其中推理 |
|---|---|---|---|---|
| D0（简单事实） | 4 | ~3 700 | ~1 800–2 200 | ~600–1 000 |
| D2（多假设） | 8–10 | ~6 000–13 500 | ~6 400–18 000 | ~3 700–12 600 |
| D4（哲理） | 13 | ~21 400 | ~33 100 | **~21 800** |

**推理 token 占了输出的大头。** 这不是缺陷，是推理模型的成本结构——
但它意味着一次 D4 回合的代价远高于按"答案长度"估算的直觉。

### 一个明确的后续项

`DEFAULT_REASONING_HEADROOM = 16384` 这个数字偏大，本身就是一个**信号**：
说明**提示词没有约束输出规模**，模型在自由发挥
（`logical_analyzer` 要求检查八件事、填八个列表，却没有说每个列表多长）。

真正的解法是让模板显式限定列表长度与条数，把推理量降下来。
但这会**改变认知输出的质量**，需要评测数据支撑（有多少反例、多少缺失信息
是被"简洁"砍掉的），因此列为**阶段 7** 的工作，不在本阶段顺手做。

---

## 替代方案与取舍

| 方案 | 为什么不选 |
|---|---|
| 同时实现 Anthropic Provider | 无法测试也无法跑通的实现只是空壳（ADR-0012） |
| 让 Provider 记一个 `last_usage` 属性 | 是状态，并发下会串号，且把"哪次调用的用量"变成时序问题 |
| 缺 Key 时静默回落到 Mock | 让配置失误伪装成"系统跑得很好"，且无处告知 |
| 截断后重试 | 同样的上限得到同样的截断，只是烧预算 |
| 截断在网关里统一判 | 那样 Provider 会先尝试解析，产出一个误导性的诊断 |
| 可选模块失败即回合失败 | 用户只差一步就能拿到回答，代价不成比例 |
| 把 `reasoning_content` 存下来"供审计" | 直接违反认知宪法红线一 |
| 保留配置层的具体默认值 | 两个权威默认值互相覆盖，比没有默认值更难排查 |
| 本阶段顺手做提示词瘦身 | 会改变认知输出质量，需要评测支撑（阶段 7） |

## 影响

- `LLMProvider` 协议返回值改变（唯一破坏性变更，见 §2）。
- `ModelInvocationInfo` 新增 `reasoning_token_count`（增量、有默认值）。
- `Settings` 新增 `llm_json_mode` / `llm_reasoning_headroom_tokens` /
  `llm_circuit_failure_threshold` / `llm_circuit_recovery_seconds` /
  `deepseek_api_key` / `deepseek_base_url`；密钥字段改用 `AliasChoices`。
- `CognitiveRoundService.transition` 新增 `diagnostics` 参数。
- `RoundSummaryResponse` 新增 `skipped_steps`。
- 新增包内模块：`providers/{response,parsing,http,resilience,openai_compatible}.py`、
  `reliability/circuit_breaker.py`。
- 新增测试标记 `live`（默认跳过，`make test-live` 显式运行）。
- `concern_detector` 提示词升至 v1.1.0，旧版本模板保留（回放需要）。
