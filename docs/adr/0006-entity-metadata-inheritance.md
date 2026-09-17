# ADR-0006：EntityMetadata 继承与领域对象基类

- **状态**：已接受
- **日期**：2026-09-17
- **相关**：`docs/domain_model.md`、`src/ai_psi/domain/common.py`

## 背景

**这是一处任务书内部的实质矛盾。**

任务书 §5.1 明确要求：

> 所有重要对象**至少包含** `EntityMetadata`：
> `id` / `created_at` / `updated_at` / `version` / `created_by` / `schema_version`
>
> 要求：时间统一存储 UTC；更新采用乐观锁；禁止静默覆盖旧版本；重要对象保留变更事件。

但 §5.3–§5.12 给出的 13 个领域对象定义（Observation、Concern、Inquiry、Evidence、
Concept、Assumption、Hypothesis、Belief、Judgment、Reflection、Memory、Experience、
ImprovementProposal）**没有一个继承了它**，每个类都只列出了自己的业务字段。

两处直接冲突，必须选择一种解释。

## 决策

**§5.1 是总则，§5.3–§5.12 是各对象的业务字段说明，二者互补。**

实现为：所有领域对象继承 `EntityMetadata`，基类提供通用字段，
子类只声明业务字段。

```python
class EntityMetadata(BaseModel):
    model_config = ConfigDict(frozen=False, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    created_at: datetime          # 一律 UTC，tz-aware
    updated_at: datetime
    version: int = 1              # 乐观锁
    created_by: str               # 产生该对象的组件或用户标识
    schema_version: str           # 该对象结构的版本，用于未来迁移
```

**四条配套规则：**

1. **时间一律 UTC 且 tz-aware。**
   禁止 naive datetime——否则跨时区比较会静默出错。
   API 输出层可附加本地时区，但**存储与领域对象内部一律 UTC**。

2. **`version` 是乐观锁，不是历史计数。**
   每次持久化更新递增；版本不匹配 → 拒绝写入并抛 `OptimisticLockError`，
   **绝不静默覆盖**。

3. **`schema_version` 用于对象结构演进。**
   允许未来反序列化旧版本对象时做迁移，而不是直接崩溃。

4. **`extra="forbid"`。**
   未知字段一律拒绝。这与"模型返回值是不可信输入"（任务书原则 16）
   的要求一致——模型多返回的字段不会被静默接受。

**关于 `Event` 的例外：** `Event` 自带 `schema_version` 字段且不使用乐观锁
（事件只追加、永不更新），因此它是**唯一不继承 `EntityMetadata`** 的对象。
`Event` 改用独立的 `recorded_at` + `occurred_at` 双时间戳，
以区分"事件发生时间"与"事件被记录时间"——这在事件回放中至关重要。

## 替代方案与取舍

| 方案 | 为什么不选 |
|---|---|
| 只按 §5.3–§5.12 字面实现，不继承 | 会直接违反 §5.1 与 §19「禁止静默覆盖旧版本」，乐观锁无处安放 |
| 把通用字段平铺进每个类 | 13 个类重复 6 个字段，且无法统一施加校验规则；改一处要改十三处 |
| 用 Mixin 而非基类继承 | 在 Pydantic v2 中与继承等价但更难表达字段顺序与校验继承，收益为零 |
| 给 Event 也用 EntityMetadata | `version`/`updated_at` 对"永不更新"的事件无意义，且会诱导可变事件 |

## 影响

- `domain/common.py` 是全部领域对象的依赖根，必须保持零外部依赖。
- 需要为 `EntityMetadata` 单独写单元测试：UTC 强制、`extra="forbid"`、
  `version` 默认值、`id` 唯一性。
- API Schema **不复用**领域对象（任务书 §5 开头明确要求），
  由 `api/schemas.py` 单独定义，避免层间耦合。
- 数据库实体同样不复用领域对象，由 `infrastructure/db/models.py` 定义。
  **三套模型（domain / db / api）之间的转换必须显式，不允许 `model_validate` 一键互转。**
