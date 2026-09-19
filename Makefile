# AI-PSI Cognitive Runtime — 开发任务入口
#
# ⚠️ Windows 默认不含 make。本机未装 make 时，用右侧等价的 uv 命令：
#   make lint      →  uv run ruff check .  &&  uv run ruff format --check .
#   make typecheck →  uv run mypy src tests scripts
#   make test      →  uv run pytest --cov=ai_psi --cov-report=term-missing
#   make fmt       →  uv run ruff check --fix .  &&  uv run ruff format .
#   make up        →  docker compose up -d
#   make down      →  docker compose down
# CI（ubuntu-latest）自带 make，直接使用本文件的 recipe。

UV ?= uv
PY ?= $(UV) run python
COV = --cov=ai_psi --cov-report=term-missing --cov-report=xml

.PHONY: help install lint fmt typecheck test test-unit test-integration policy check \
        up down logs ps bootstrap migrate migrate-new clean test-live eval-golden \
        eval-golden-postgres

help:
	@echo "AI-PSI 开发命令："
	@echo "  install          安装依赖（含 dev 组）"
	@echo "  lint             ruff check + ruff format --check"
	@echo "  fmt              ruff 自动修复 + 格式化"
	@echo "  typecheck        mypy --strict"
	@echo "  test             全部测试（单元 + 属性 + 契约 + 场景 + API + 集成，**需要数据库**）"
	@echo "  test-unit        单元/属性/契约/场景/API（快，不需要数据库）"
	@echo "  test-integration 只跑集成测试（需要数据库）"
	@echo "  test-live        真实模型端到端（**会花钱**，需 AI_PSI_RUN_LIVE_TESTS=1）"
	@echo "  eval-golden      Golden Case 评测（Mock、不联网、不写生产库）"
	@echo "  eval-golden-postgres  S2 评测（正式 HTTP + 专用 PostgreSQL；需 EVAL_DB/REF_DB）"
	@echo "  policy           覆盖率闸门（domain/cognition 85%、总体 75%）"
	@echo "  check            lint + typecheck + test + policy（提交前跑这个）"
	@echo "  up               启动 PostgreSQL 16 + pgvector 容器"
	@echo "  down             停止容器（保留数据卷）"
	@echo "  bootstrap        校验数据库连通并确保 vector 扩展就绪"
	@echo "  migrate          把开发库迁移到最新版本"
	@echo "  migrate-new      生成一条新迁移（用法：make migrate-new M='说明'）"
	@echo "  clean            清理缓存与临时产物"

install:
	$(UV) sync --all-groups

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt:
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

# mutation/ 也要检查：它是 §六 的测量工具，而**测量工具自己出错时
# 报出来的是一个漂亮的分数**（实测过一次：104 个被杀的变异体被记成
# incompetent，分数从 100% 掉到 7.7%）。工具不能没有类型网。
typecheck:
	$(UV) run mypy src tests scripts mutation

test:
	$(UV) run pytest $(COV)

# 单元 / 属性 / 契约 / 场景 / API —— 全部零外部依赖（ADR-0009）。
# 集成测试需要 PostgreSQL，因此不在这一组里。
test-unit:
	$(UV) run pytest tests/unit tests/property tests/contract tests/scenarios tests/api $(COV)

test-integration:
	$(UV) run pytest tests/integration -m integration

# 真实模型测试：**会花钱、会联网**，因此需要显式开关。
# 只跑 live 用例，且需要 AI_PSI_DEEPSEEK_API_KEY 已配置。
test-live:
	AI_PSI_RUN_LIVE_TESTS=1 $(UV) run pytest tests/integration -m live -v

# Golden Case 评测（阶段 7 · S1a）。
#
# 🔴 **它不在 pytest 的默认收集范围里**（`testpaths = ["tests"]`，ADR-0012 §4）：
# 这里跑的是**数据集**，不是测试用例。把它塞进 `make test` 会让每次提交
# 都跑一遍完整评测，而评测的产出（`evals/reports/`）本来就不该进提交历史。
eval-golden:
	$(PY) -m ai_psi.evaluation.cli \
		--dataset evals/datasets \
		--output evals/reports/s1a-results.json \
		--canonical-output evals/reports/s1a-canonical.json

# S2 评测：正式 HTTP 路由 + 真实 PostgreSQL（阶段 7 · S2）。
#
# 🔴 **两座库都必须显式给出**，不从 AI_PSI_DATABASE_URL 猜：
#
#     make eval-golden-postgres \
#         EVAL_DB="postgresql+psycopg://user:pw@host:port/xxx_eval_test" \
#         REF_DB="postgresql+psycopg://user:pw@host:port/yyy_reference_test"
#
#   两个库名必须分别以 `_eval_test` / `_reference_test` 结尾，且互不相同；
#   命令行入口会先跑完全部隔离预检，任一不过就一条案例都不执行。
#
# ⚠️ 本目标**不**打印连接串。它只把参数原样交给 CLI。
eval-golden-postgres:
	@test -n "$(EVAL_DB)" || { echo "缺少 EVAL_DB（专用评测库连接串）" >&2; exit 2; }
	@test -n "$(REF_DB)" || { echo "缺少 REF_DB（专用参考库连接串）" >&2; exit 2; }
	$(PY) -m ai_psi.evaluation.cli \
		--mode postgres-http \
		--dataset evals/datasets \
		--evaluation-database-url "$(EVAL_DB)" \
		--reference-database-url "$(REF_DB)" \
		--output evals/reports/s2-results.json \
		--canonical-output evals/reports/s2-canonical.json

policy:
	$(UV) run coverage report --fail-under=75
	$(UV) run coverage report --include="*/ai_psi/domain/*,*/ai_psi/cognition/*" --fail-under=85

check: lint typecheck test policy

up:
	docker compose up -d
	@echo "等待容器健康检查通过..."
	@docker compose ps

down:
	docker compose down

logs:
	docker compose logs -f postgres

ps:
	docker compose ps

bootstrap:
	$(PY) scripts/bootstrap_db.py

migrate:
	$(UV) run alembic upgrade head

# 生成新迁移。M 为必填说明，例如：
#   make migrate-new M="add memory table"
migrate-new:
ifndef M
	$(error 必须提供迁移说明，例如：make migrate-new M="add memory table")
endif
	$(UV) run alembic revision --autogenerate -m "$(M)"

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
