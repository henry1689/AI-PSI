# AI-PSI Cognitive Runtime

> 认知运行时（**不是**聊天机器人）。北极星：L5 Personal Superintelligence。

## 🔴 第一铁律：分阶段推进

**严格按阶段 0→8 走。只做当前 Step，做完停下汇报。绝不一次性写完、不擅自扩大范围。**

- 需求书（22 节）：`docs/AI_PSI_V0_1_TASK_SPEC.md`
- 恢复状态：`D:\AI文件\personal-assistant\tasks\2026-09\2026-09-17-ai-psi-V0.1-阶段0-1-完成.md`
- 进度：阶段 0+1 完成（428 测试全绿 / 覆盖率 99%）→ 下轮进阶段 2
- **本轮边界**：不碰真实 LLM、向量检索、自迭代发布
- 已定决策不重问（Python 版本、路径、DB 策略、依赖引入时机）

## 技术栈

- Python **3.13**｜依赖用 **uv** 管理
- pydantic v2 + pydantic-settings｜psycopg3
- PostgreSQL 16 + pgvector（docker compose，端口 **55432**）
- **尚未引入**：FastAPI · SQLAlchemy · Alembic · structlog（阶段 2/3 才进，现在不预写）
- 无前端、无 npm、无 K8s

## 命令（本机没装 make，用等价 uv 命令）

- lint：`uv run ruff check .` + `uv run ruff format --check .`
- 类型：`uv run mypy src tests scripts`（strict）
- 测试：`uv run pytest --cov=ai_psi --cov-report=term-missing`
- 起库：`docker compose up -d` ｜ 建库校验：`uv run python scripts/bootstrap_db.py`
- 提交前全跑：lint + typecheck + test + 覆盖率闸门

## 质量闸门

- mypy **strict** 必须过
- 覆盖率：domain/cognition **≥85%**、总体 **≥75%**，不达标即失败

## 禁止

- 禁止提交 `.env`、禁止在代码中写密钥
- 禁止跳过测试 / mypy 直接进下一阶段
- 禁止提前引入后续阶段的依赖
