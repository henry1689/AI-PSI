# ADR-0007：Python 版本与工具链

- **状态**：已接受
- **日期**：2026-09-17
- **相关**：ADR-0012（范围边界）、`pyproject.toml`

## 背景

任务书指定 Python 3.12+、Ruff、mypy **或** pyright、pytest + pytest-asyncio、
Hypothesis、pydantic-settings、Docker Compose、GitHub Actions。

本机实测环境（2026-09-17）：

| 项 | 实测 |
|---|---|
| `python` | 3.11.15 |
| `py -V:3.13` | **3.13.2**（本机默认） |
| `py -V:3.12` | 不存在 |
| uv | 0.11.28 |
| Docker | 28.1.1（Docker Desktop） |
| make | **未安装**（后经用户同意用 winget 安装 GNU Make 4.4.1） |

## 决策

### 1. Python 版本

**开发与验证使用本机 3.13.2**；`requires-python = ">=3.12"`。

理由：3.13 满足任务书"3.12+"的要求；实测全部依赖在 3.13 上**均有 wheel**
（pydantic 2.13.5 / psycopg 3.3.5 / mypy 2.3.1 / ruff 0.16.8 / pytest 9.1.1），
未出现需要现场编译的包。声明 `>=3.12` 保留了在 3.12 上运行的兼容性，
CI 矩阵可扩展到 3.12。

### 2. 类型检查器

**选 mypy（strict）而非 pyright**（任务书为二选一）。

理由：Pydantic v2 官方提供 `pydantic.mypy` 插件，能把动态构造的
`__init__` 签名暴露给类型检查器；pyright 需要额外配置才能达到类似效果。
本项目大量使用 Pydantic 模型，mypy + 插件是阻力最小的路径。

配置要点：
- `strict = true`，`warn_unreachable = true`
- **禁止用 `# type: ignore` 绕过错误**——需要忽略时必须写具体错误码并附原因

#### 2.1 测试代码的类型检查策略（实现阶段补充）

`src/` 与 `scripts/` 走完整 strict。`tests/` **只放宽三项**，其余保持开启：

| 放宽项 | 原因 |
|---|---|
| `disallow_untyped_decorators` | Hypothesis 的 `@given` / `@settings` 装饰器类型推断噪声过大 |
| `disallow_untyped_defs` | pytest 按**名字**注入 fixture，参数无法有意义地标注 |
| `disallow_incomplete_defs` | 同上——测试函数通常写了返回值标注但没写参数标注 |

`check_untyped_defs` **保持开启**，因此未标注函数的**函数体**仍会被检查，
`arg-type` / `call-arg` 等真实错误依旧会被捕获。

**需要"故意构造非法对象"的测试**（验证拒绝路径），一律使用
`tests/helpers.py` 的 `rejects()`，**不得**用裸构造 + 忽略注释。
`rejects()` 在构造**意外成功**时会主动失败——否则校验没触发时测试会静默通过。

#### 2.2 两个踩过的配置陷阱

**陷阱一：`module` 模式必须是全限定模块名。**
写成 `["tests.*", "test_*"]` 时，`test_*` 不被接受，
mypy 会**拒绝整张 override 表**——表现为设置"静默不生效"，
连命令行 `--allow-untyped-defs` 都压不住那些错误。

**陷阱二：`disallow_untyped_defs` 与 `disallow_incomplete_defs` 是两个开关。**
错误信息 "Function is missing a type annotation for one or more parameters"
来自**后者**。只关前者不解决问题——测试函数通常是"有返回值标注、无参数标注"，
触发的正是 `disallow_incomplete_defs`。

**前置条件**：要让 `module = ["tests.*"]` 生效，`tests/` 下必须有 `__init__.py`。
没有它，mypy 把每个测试文件当作**顶层模块**（模块名是文件名），override 不匹配。

### 3. Lint / 格式化

**Ruff 同时承担 lint 与 format**（任务书允许）。

两条关键配置，均源于实测踩坑：

- **必须忽略 `RUF001` / `RUF002` / `RUF003`（歧义 Unicode 字符）。**
  本项目注释与文档字符串全部以中文书写，这三条规则会对中文标点产生**大量误报**。
- **必须排除 `**/*.md`。**
  Ruff 0.14+ 会格式化 Markdown 中的 Python 代码块。`docs/` 下的文档——
  尤其任务书 `AI_PSI_V0_1_TASK_SPEC.md`——含有大量作为**示例**的代码片段，
  其中不乏带省略号占位符的伪代码。实测 `ruff format` 试图改写任务书中的
  `LLMProvider` Protocol 定义，这会破坏需求文档的可读性与原意。

### 4. 依赖管理

**uv**（本机已装 0.11.28），虚拟环境建在项目内 `.venv`。
`uv.lock` **提交进仓库**以保证可复现构建。

### 5. 依赖引入节奏

**只声明真正被代码使用的依赖，不预留。**

阶段 1 的依赖仅：`pydantic`、`pydantic-settings`、`psycopg[binary]`。
FastAPI / SQLAlchemy / Alembic / structlog **在对应阶段（2、3）引入**。
理由见 ADR-0012 与开发原则第 2 条（简单优先，不写推测性代码）。

## 替代方案与取舍

| 方案 | 为什么不选 |
|---|---|
| 用 uv 装 Python 3.12 | 本机 3.13 实测无兼容问题；装 3.12 只增加一个版本而无收益 |
| 同时锁 3.12 + 3.13 双版本 CI | CI 时间与配置成本翻倍，V0.1 阶段收益不足；`>=3.12` 已保留兼容性 |
| 选 pyright | 与 Pydantic v2 的配合需要额外配置，阻力更大 |
| 依赖全量预装（含 FastAPI/SQLAlchemy） | 违反"不写推测性代码"；未使用的依赖同样会带来版本漂移与安全面 |

## 影响

- 本机 `make` 由 `winget install ezwinports.make`（GNU Make 4.4.1）提供，
  安装后需重启终端使 PATH 生效。
- CI 在 ubuntu-latest 上使用 Makefile 原样执行——**本地与 CI 命令完全一致**。
- 未安装 make 的环境可用等价命令：
  `uv run ruff check .` / `uv run mypy src tests scripts` / `uv run pytest`。
