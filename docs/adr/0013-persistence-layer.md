# ADR-0013：持久化层设计

- **状态**：已接受
- **日期**：2026-09-17
- **相关**：ADR-0002（事件与状态）、ADR-0006（三套模型不互转）、`docs/architecture.md`

## 背景

阶段 2 要把"事件记录 + 当前状态投影"从设计变成可运行的代码：
PostgreSQL 模型、Alembic 迁移、Repository、Unit of Work、Event Store、
幂等支持。任务书给出的验收条件是：

> 可以创建和回放事件；非法状态转换全部拒绝；**事务失败不会留下半成品数据**。

## 决策

### 1. 异步 SQLAlchemy 2.x + psycopg3 异步驱动

认知流水线本身是异步的（模型调用、并发检索），阶段 3 的 FastAPI 也是异步的。
现在用同步再在阶段 3 转换，意味着重写全部仓储签名与事务边界——
代价远高于一开始就选异步。

**会话工厂必须设 ``expire_on_commit=False``。** 异步场景下，
提交后若对象仍标记为过期，任何属性访问都会触发隐式 IO，
而异步环境里的隐式 IO 会直接抛 ``MissingGreenlet``。

### 2. 事件用 ``sequence`` 排序，不用时间戳

`events` 表有一个数据库生成的单调递增列 `sequence`，**回放按它排序**。

`recorded_at` 的精度不足以区分同一微秒内的多次写入，
按它排序会得到**不确定的顺序**，而"回放可复现"正是事件记录存在的理由。
`occurred_at` 更不能用于排序——乱序到达的外部事件时间戳可能很旧。

领域事件对象**不携带 sequence**（它是存储层概念，事件在构造时尚未写入）。
增量回放的游标因此由存储层提供：`latest_sequence_for_round()`。

### 3. 事件表没有 UPDATE / DELETE 路径

`EventStore` Protocol 刻意不提供修改方法，SQLAlchemy 实现同样不暴露。
这**不是遗漏**（ADR-0002）。有测试断言这两点，防止未来有人"顺手加一个"。

### 4. 关键不变量下沉到数据库 CHECK 约束

应用层已用 Pydantic 模型校验器拦住了非法数据，但**关键不变量再加一道数据库级 CHECK**：

| 约束 | 对应不变量 |
|---|---|
| `completed_requires_stop_reason` | 🔴 I19：完成回合必须有停止原因 |
| `failed_requires_diagnostics` | 🔴 I20：失败回合必须可诊断 |
| `model_calls_within_budget` | 超预算回合率必须为 0 |
| `state_valid` / `error_category_valid` / `actor_type_valid` … | 枚举白名单 |

理由：I19/I20 守的是**可诊断性**。一个"完成了但不知道为什么停"或
"失败了但查不出原因"的回合，在事后排查时等同于**没有记录**。
应用层有 bug 时，数据库是最后一道防线。
有集成测试绕过应用层直接 INSERT，验证这些约束真的生效。

### 5. 约束表达式由 Python 枚举生成

`enum_check_expression(column, EnumClass)` 从枚举成员生成 `CHECK` 表达式，
让**数据库约束与 Python 枚举自动保持同步**：新增一个枚举成员而不写迁移，
数据库会直接拒绝该值。

### 6. 约束命名约定是迁移可回滚的前提

`Base.metadata` 指定了 `naming_convention`。不指定时 PostgreSQL 会为
CHECK/UNIQUE 约束生成随机名，而 Alembic 的 `downgrade` 需要按名字
`DROP CONSTRAINT`——**随机名意味着迁移无法可靠回滚**。

### 7. 乐观锁用显式 UPDATE，不用 SQLAlchemy 的 `version_id_col`

`save(round_, expected_version=...)` 生成
`UPDATE ... WHERE id = ? AND version = ?`，受影响行数为 0 时再区分
"记录不存在"（`NotFoundError`）与"版本冲突"（`OptimisticLockError`）。

选显式写法而非 `version_id_col` 的原因：领域对象自己也管理 `version`
（`bumped()`），两者会互相覆盖，语义变得难以推理。
显式 UPDATE 与 Port 的契约一一对应，也更好测。

字段映射由 `round_to_values()` 统一提供，插入与更新共用一份列表。
有单元测试断言它**覆盖了 ORM 的全部列**——新增领域字段却忘了加进映射，
表现为"能存不能改"，是最难发现的一类缺陷。

### 8. 幂等靠数据库唯一约束仲裁

`reserve()` 用 `INSERT ... ON CONFLICT DO NOTHING RETURNING`，
由唯一约束决定谁拿到占位权。**先查后插存在竞态窗口**——
两个并发请求可能都查到"不存在"然后都插入。

三种结果语义分明：

| 结果 | 含义 | 处理 |
|---|---|---|
| `RESERVED` | 首次见到该 key | 继续创建回合 |
| `REPLAY` | 同 key 同请求体，回合已建 | 直接返回既有回合 |
| `CONFLICT` | 同 key 不同请求体，或上次请求未完成 | 报错，**不**默默返回旧结果 |

### 9. 迁移只放在 migrations/，不写进 alembic.ini

连接串含密码，**不能进版本库**。`migrations/env.py` 从
`AI_PSI_DATABASE_URL` 读取；集成测试再通过 `Config.attributes`
传入测试库地址把它覆盖掉。

## 替代方案与取舍

| 方案 | 为什么不选 |
|---|---|
| 同步 SQLAlchemy | 阶段 3 要整体重写仓储与事务边界，代价更高 |
| 用 `recorded_at` 排序回放 | 同微秒写入顺序不确定，回放不可复现 |
| 用 ULID / 雪花 id 取代自增 sequence | 需要额外依赖；BIGSERIAL 已满足"全局单调"这一实际需求 |
| 只靠应用层校验，不加 DB 约束 | I19/I20 是诊断性的最后防线，值得冗余 |
| 用 `version_id_col` 自动乐观锁 | 与领域对象自管的 `version` 语义冲突 |
| 先查后插实现幂等 | 有竞态窗口，并发下会创建重复回合 |
| 把连接串写进 alembic.ini | 密码进版本库 |

## 影响

- 阶段 3 的内存适配器（ADR-0009）必须实现**同一套 Protocol**，
  且通过同一组契约测试——否则"阶段 3 通过、阶段 5 爆炸"。
- 新增领域字段时，`round_to_values()` 的覆盖断言会立刻失败并提醒补映射。
- 未来若要归档旧事件，"事件只追加"的约束要求归档也走只读路径。
