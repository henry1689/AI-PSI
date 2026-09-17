# AI-PSI Cognitive Runtime

> 认知运行时（**不是**聊天机器人）。北极星：L5 Personal Superintelligence。

## 🔴 第一铁律：分阶段推进

**严格按阶段 0→8 走。只做当前 Step，做完停下汇报。绝不一次性写完、不擅自扩大范围。**

- 需求书（22 节）：`docs/AI_PSI_V0_1_TASK_SPEC.md`
- 实施计划与进度（**唯一真相来源**）：`docs/implementation_plan.md`
- 恢复状态：`D:\AI文件\personal-assistant\tasks\2026-09\2026-09-18-ai-psi-V0.1-阶段4完成.md`
- 进度：**阶段 0/1/2/3/4/5 完成**（1156 测试 + 5 跳过 / 总体覆盖率 96%，真实模型端到端已跑通）→ 下轮进阶段 6
- **本轮边界**：不碰自迭代发布（阶段 6）、评测与回放（阶段 7）
- 已定决策不重问（Python 版本、路径、DB 策略、预算表见 ADR-0008、LLM 用 DeepSeek 见 ADR-0016）

## 技术栈

- Python **3.13**｜依赖用 **uv** 管理
- pydantic v2 + pydantic-settings｜psycopg3｜SQLAlchemy 2（异步）｜Alembic
- FastAPI + uvicorn｜httpx（真实 Provider）｜structlog
- **LLM：DeepSeek `deepseek-v4-flash`**（OpenAI 兼容；Anthropic 未实现，ADR-0016）
- **向量：默认本地确定性实现**（词面，**非语义**，ADR-0017 §1）；可切 `openai_compatible`
- PostgreSQL 16 + pgvector（docker compose，端口 **55432**）
- 无前端、无 npm、无 K8s

## 命令

本机已装 GNU Make 4.4.1（winget），直接用 `make`：

- lint：`make lint` ｜ 类型：`make typecheck` ｜ 测试：`make test`
- 快跑（不需要数据库）：`make test-unit`
- 真实模型端到端（**会花钱、会联网**）：`make test-live`（需配好 API Key）
- 提交前全跑：`make check`（lint + typecheck + test + 覆盖率闸门）
- 起库：`make up` ｜ 迁移：`make migrate` ｜ 建库校验：`make bootstrap`
- 起服务：`uv run python -m ai_psi.main`

> 若 `make` 不在 PATH（终端启动于安装之前），等价命令见 `Makefile` 顶部注释。

## 质量闸门

- mypy **strict** 必须过
- 覆盖率：domain/cognition **≥85%**、总体 **≥75%**，不达标即失败
- 场景 A–J（`tests/scenarios/`）是阶段 3 的硬性验收条件，**不得为了通过而放宽断言**
- 存储 Port 的改动必须同时满足 `tests/contract/` 与
  `tests/integration/test_contract_postgres.py` 里的**同一组**契约断言
- 记忆仓储是 `UnitOfWork` 的一部分（`uow.memories`），**不单独注入**——
  分开会让记忆写入与它的审计事件落在两个事务上（ADR-0017 §3）

## 禁止

- 禁止提交 `.env`、禁止在代码中写密钥
- 禁止跳过测试 / mypy 直接进下一阶段
- 禁止提前引入后续阶段的依赖
- 禁止为通过检查而删除测试、放宽 Schema 或加 `# type: ignore`；
  任何豁免必须写进 ADR
- 禁止让缺 API Key 静默回落到 Mock（ADR-0016 §6）
- 禁止让缺向量配置静默回落到本地向量（ADR-0017 §1）
- 禁止保存或打印模型的 `reasoning_content`（认知宪法红线一）；
  只允许保留推理 token 的**计数**
- 禁止在**审计**负载里保留记忆正文或它的哈希（ADR-0017 §5）——
  导出是另一回事，它本来就该含正文
