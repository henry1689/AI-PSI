# ADR-0003：LLM Provider 抽象

- **状态**：已接受
- **日期**：2026-09-17
- **相关**：ADR-0001（架构风格）、`docs/prompt_contracts.md`

## 背景

任务书要求：大模型是可替换组件；必须支持完全使用 Mock LLM 运行测试；
任何模型返回值都视为不可信输入；默认不保存供应商返回的完整隐藏推理内容；
所有模型调用必须记录模型和 Prompt 版本。

同时，开发原则第 1 条明确：**先完成领域模型、事件系统、状态机和测试，
再接入真实大模型。**

## 决策

**定义 `LLMProvider` Protocol 作为 Port，三个实现作为 Adapter。**

接口（任务书 §8.1）：

```python
class LLMProvider(Protocol):
    async def generate_structured(
        self, *, task_name, messages, response_model, model_config, invocation_context
    ) -> T: ...
    async def generate_text(
        self, *, task_name, messages, model_config, invocation_context
    ) -> str: ...
```

**五项关键约定：**

1. **结构化调用优先。**
   所有认知模块（Concern / Inquiry / Hypothesis / Judgment / …）
   一律使用 `generate_structured(response_model=...)`，
   返回经过 Pydantic 校验的对象。`generate_text` **只用于**
   `response_renderer`（生成面向用户的自然语言）。

2. **模型输出是不可信输入。**
   Provider 层负责：Schema 校验、字段过滤、解析失败重试、连续失败降级。
   解析失败**绝不**产生部分有效的领域对象（任务书不变量 16）。

3. **不保存隐藏思维链。**
   Provider 只提取结构化字段；供应商返回的 `reasoning` / 思维链内容
   一律丢弃，不写入数据库、不进日志、不进事件 payload。
   `ModelInvocationInfo` 只保存 `response_hash`（原始响应哈希）用于审计比对。

4. **Mock Provider 是一等公民，不是测试替身。**
   Mock 必须能驱动完整的认知流水线，使场景 A–J 全部通过（阶段 3 验收条件）。
   它按 `task_name` 返回预置的结构化对象，并支持注入"损坏输出"用于测试降级路径。

5. **Provider 级熔断与健康状态。**
   连续失败触发熔断，系统进入 `DEGRADED`。
   **注意：`DEGRADED` 是系统/Provider 健康状态，不是认知回合状态**
   （见 ADR-0010 的同类澄清与 `docs/state_machine.md`）。

**阶段安排：** 阶段 3 只实现 Protocol + Mock；真实 Provider（Anthropic、
OpenAI-compatible）在阶段 4。阶段 1 **不创建** `providers/` 包。

## 替代方案与取舍

| 方案 | 为什么不选 |
|---|---|
| 直接用某家 SDK（如 anthropic 客户端）贯穿代码 | 违反"领域逻辑不能依赖特定供应商"，且 Mock 测试会变得别扭 |
| 用 LangChain 等框架统一 | 引入大量隐式行为与版本漂移，且框架本身可能变化；任务书要求"清晰异常类型" |
| 只做 `generate_text` + 正则解析 JSON | 正则解析脆弱，且正是任务书要防的"不可信输入"处理方式 |
| 在 Provider 内直接写数据库 | 直接违反任务书第 17 条 |

## 影响

- 阶段 3 的认知流水线可以在**零外部 API** 的情况下完整开发与测试。
- 每个 Prompt 必须有唯一任务名与语义版本号（见 `docs/prompt_contracts.md`）。
- `ModelInvocationInfo` 是审计与成本统计的唯一来源，必须每次调用都记录。
- 切换 Provider 是配置变更，不是代码变更。
