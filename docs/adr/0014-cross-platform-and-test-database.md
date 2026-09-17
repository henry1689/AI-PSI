# ADR-0014：跨平台陷阱与测试数据库隔离

- **状态**：已接受
- **日期**：2026-09-17
- **相关**：ADR-0007（工具链）、ADR-0013（持久化层）

## 背景

阶段 2 是实现阶段第一次真正碰平台差异与外部状态（数据库）。
**开发机是 Windows，CI 是 ubuntu-latest**——这个组合会让一类缺陷
只在开发机上出现，而开发机恰恰最容易被当成"应该没问题"。

本 ADR 记录阶段 2 实际踩到并解决的跨平台与测试隔离问题，
避免后续阶段重复踩。

## 决策

### 1. Windows 事件循环必须显式选 selector

**症状**：`psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run in async mode.`

Windows 默认的 `ProactorEventLoop` 不被 psycopg3 异步驱动支持。
Linux / CI 上一切正常——**这类缺陷只会在开发机上出现**。

解决方式集中在 `ai_psi/infrastructure/asyncio_compat.py`，
而不是散落在 alembic、测试、应用入口各写一遍：

| 场景 | 方式 |
|---|---|
| 我们自建循环（alembic） | `asyncio.Runner(loop_factory=make_selector_loop)`——**优先**，无全局副作用 |
| 框架自建循环（pytest-asyncio、uvicorn） | `install_selector_loop_policy()` 设置全局策略 |

**pytest-asyncio 需要两条路都覆盖**：
`pytest_asyncio_loop_factories` hook 只对**异步测试**生效；
当**同步测试**请求异步夹具时，框架会走另一条创建循环的路径，
那条路径读的是全局策略。只做其一，另一类用例仍会失败。

### 2. `.ini` 配置文件必须保持 ASCII

**症状**：`UnicodeDecodeError: 'gbk' codec can't decode byte 0x82`

Python 的 `configparser` 用 **locale 编码**（zh-CN Windows 上是 GBK）
读取 `.ini` 文件，不是 UTF-8。`alembic.ini` 里的中文注释会让
alembic 在导入阶段直接崩溃。

**规则：`alembic.ini` 保持纯 ASCII，注释一律用英文。**
这个坑踩了两次（第二次是注释里的破折号 `—`，UTF-8 为 `E2 80 94`，
末字节 0x94 同样触发 GBK 解码失败）。

Python 源码文件不受影响——PEP 3120 规定源码默认 UTF-8。
`.env` 也不受影响——`pydantic-settings` 显式指定了 `utf-8`。

### 3. 集成测试使用**独立派生**的测试数据库

集成测试会 `TRUNCATE` 数据表。**指错库就是开发数据的静默丢失。**

- 测试库连接串从开发库名派生（追加 `_test`）；
- `Settings.resolved_test_database_url()` 在派生结果与开发库相同时
  **主动抛 `ConfigurationError`**——宁可配置报错，也不要让测试去清空开发数据；
- 可用 `AI_PSI_TEST_DATABASE_URL` 显式指定（CI 或特殊部署）。

有单元测试覆盖派生逻辑与"两库相同即拒绝"这条规则。

### 4. 测试引擎必须用 `NullPool`

pytest-asyncio 为每个用例创建独立事件循环，而**连接池中的连接绑定在
创建它的循环上**。池化会让第二个用例拿到上个循环的连接并抛
`attached to a different loop`。

`NullPool` 每条语句新建连接、用完即关，从根上避开这个问题。
代价是每用例多一次 TCP 握手——在这个规模上完全可接受。

替代方案是"整个测试会话共用一个事件循环"（`loop_scope="session"`），
但那要求所有异步夹具与测试标记一致的 loop scope，配置面更大，
且一旦有人漏标就会得到难以理解的现象。

### 5. 集成测试在**每个用例开始前**清库

不做"事务回滚"式隔离——工作单元自己会 `commit()`，
那会跨出外层事务，反而**测不到真实的提交路径**，
而"提交路径正确"恰恰是阶段 2 的验收条件之一。

用例开始前清空而非结束后清空：失败用例留下的数据仍可用于排查。

### 6. 测试执行分层

| 命令 | 内容 | 需要数据库 |
|---|---|---|
| `make test-unit` | 单元 + 属性测试 | 否 |
| `make test-integration` | 集成测试 | 是 |
| `make test` | 全部 | 是 |
| `make check` | lint + typecheck + test + 覆盖率闸门 | 是 |

`make test` 默认包含集成测试——任务书要求开发/测试库必须是 PostgreSQL，
把数据库排除在默认测试路径之外，等于让最需要验证的那部分长期不被运行。

## 替代方案与取舍

| 方案 | 为什么不选 |
|---|---|
| 用 `asyncio.set_event_loop_policy` 一把梭 | 全局副作用；能用 `loop_factory` 时应当优先用它。两者按场景分工 |
| 集成测试连开发库然后用完清干净 | 一次失误就是开发数据全丢，风险收益完全不对等 |
| 集成测试用 SQLite | 任务书明确要求避免 SQLite 行为差异（JSONB、CHECK、ARRAY 都不同） |
| 用测试容器（testcontainers） | 引入新依赖与 Docker 依赖；派生库 + 迁移已满足需求，且更快 |
| 事务回滚式测试隔离 | 工作单元会 commit，回滚式隔离测不到真实提交路径 |
| 把 `.ini` 改成 UTF-8 编码声明 | `configparser` 不接受编码声明；ASCII 是唯一可靠做法 |

## 影响

- 阶段 3 的 FastAPI 应用入口同样需要 `install_selector_loop_policy()`——
  uvicorn 在 Windows 上也会创建 Proactor 循环。
- 新增任何 `.ini` / `.cfg` 配置文件时，牢记 ASCII 约束。
- CI 的 postgres service container 已就位，集成测试在 CI 上会真实运行。
- 阶段 5 引入 pgvector 后，测试库的 `CREATE EXTENSION vector` 已由夹具处理。
