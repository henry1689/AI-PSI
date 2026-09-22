# S8 独立复核记录

> 本文件由 **S8 独立复核者** 撰写。复核者不是 S8 实现者，未参与 S8 的任何设计或编码。
> 所有结论均来自**复核者亲手复算**、独立构造的端到端实验、代码阅读与 CI 原始日志；
> 未采信实现者自审报告（`S8-SELF-REVIEW-CLAIMS.md`）中的任何陈述。
> 凡与自审报告不符之处，以本记录为准，并在第 7 节明确列出分歧。

---

## 1. 复核主体标识

| 项 | 值 |
|---|---|
| 身份 | Claude Code 独立复核 Agent（非 S8 实现者） |
| 模型 | `claude-opus-5[1m]`（Opus 5，1M 上下文） |
| 会话载体 | Claude Code / Claude Agent SDK，Windows 11（win32），Git Bash |
| 独立性声明 | 复核者与实现者不共享会话、不共享推理过程；本记录全部数值由复核者重新计算 |

## 2. 复核日期

**2026-09-23**（本地时区 UTC+8）。

## 3. 复核的 S8 SHA

| 项 | SHA |
|---|---|
| 复核对象（S8 基线） | `f1af320e2603a1f9ec5b7889767f1e5dd774f1c8` |
| S7 封存（应为父提交） | `862fbfd1bc0e6cb333fe242d2ff2143e6410ba73` |
| `origin/main` | `f1af320e2603a1f9ec5b7889767f1e5dd774f1c8`（与 HEAD 一致） |

隔离 worktree：`D:/tmp/s8rev`（`--detach f1af320…`），复核开始时 `git status --porcelain` 为空。
复核结束后已移除并 prune（见第 11 节）。

## 4. 复核方法（实际执行的命令与路径）

### 4.1 环境与隔离

```bash
git -C /d/projects/ai-psi worktree add --detach D:/tmp/s8rev f1af320e2603a1f9ec5b7889767f1e5dd774f1c8
git -C /d/tmp/s8rev status --porcelain          # 空 → 干净
cd /d/tmp/s8rev && uv sync --quiet              # 首次 uv run 自动建 .venv
```

所有复核命令**均在隔离 worktree 内执行**；主工作树 `D:/projects/ai-psi` 全程**只读**
（仅执行 `git log / show / diff / rev-parse / worktree` 与 `gh`，未做任何写操作）。

### 4.2 基线身份

```bash
git -C /d/projects/ai-psi log --oneline -5
git -C /d/projects/ai-psi rev-parse f1af320^
git -C /d/projects/ai-psi show --stat f1af320
git -C /d/projects/ai-psi diff --numstat 862fbfd..f1af320
git log --all -- "AI-PSI-白皮书.md" "AI-PSI-总体架构蓝皮书.md"
git rev-list --all --objects | grep -E "白皮书|蓝皮书"
git tag --list
```

### 4.3 五个冻结摘要的独立复算

```bash
cd /d/tmp/s8rev && uv run python -c "
from ai_psi.evaluation.comparison import comparison_definition_digest
from ai_psi.evaluation.gate import gate_definition_digest
from ai_psi.evaluation.evidence import evidence_definition_digest, evidence_verification_definition_digest
..."
```

S6 `policy_digest` 另用**不经仓库函数**的手工路径复核：
`json.dumps({k:v for k,v in payload.items() if k!="policy_digest"}, sort_keys=True, ensure_ascii=False, separators=(",",":"))` → SHA-256。

### 4.4 六个场景：**由复核者自己构造**的 Mock Golden 链

🔴 **未照抄实现者的任何脚本。** 全部走**正式 CLI 入口**，构造方式由复核者自行决定：

| 步骤 | 正式入口 |
|---|---|
| 生成 Run | `python -m ai_psi.evaluation.cli --dataset evals/datasets --output …` |
| 生成 Comparison | `python -m ai_psi.evaluation.compare_cli` |
| 生成 GateDecision | `python -m ai_psi.evaluation.gate_cli` |
| 生成 EvidenceBundle | `python -m ai_psi.evaluation.evidence_cli build` |
| 生成 QualificationDecision | `python -m ai_psi.evaluation.qualification_cli`（七个角色显式参数） |

**场景产物一律写在 `D:/tmp/s8rev-work/`，不写仓库。**

三处"制造偏差"的手法（均为复核者自定）：

1. **Gate FAIL（场景 2）**：把候选 Run 的**一条断言** `passed: true→false`、案例
   `passed=False`、`failure_kind="assertion_failure"`，随后用官方纯函数
   `compute_metrics(dataset, result)` 与官方 `write_reports()` 重算并落盘
   （脚本 `evidence/make_fail_candidate.py`）——保证链内部自洽，S7 仍判 VERIFIED。
2. **Gate NOT_EVALUATED（场景 3）**：把候选 Run 清单的
   `manifest.code.working_tree_clean` 置 `False`，使 S5 记入 **`dirty_worktree`**
   阻塞（**刻意避开 `partial_run`**，因为 `partial_run` 会让 S8 判 NOT_APPLICABLE，
   那就变成场景 6 而不是场景 3）。S6 因 `comparison_eligible=false` 判
   NOT_EVALUATED；S8 自身作用域检查不看该阻塞，仍应判 APPLICABLE
   （脚本 `evidence/make_not_evaluated_candidate.py`）。
3. **Evidence INVALID（场景 4）**：先造出**完好**的 bundle，**事后**仅向
   `comparison.json` 注入**一个空格字节**（`\n}\n` → `\n }\n`，66442→66443 字节，
   仍可被 JSON 解析并被 S5 加载器接受），使 S7 重算的产物摘要与 Bundle 记录不符。
4. **Evidence NOT_VERIFIABLE（场景 5）**：`--candidate-run` 指向**不存在的路径**，
   其余六个输入完好 → S7 的"重算对比"检查 NOT_EVALUATED → NOT_VERIFIABLE。
5. **Policy NOT_APPLICABLE（场景 6）**：把**两侧** Run 清单的
   `manifest.provider.model_id` 改为一个**安全替身值**
   （`safe-alternate-model-for-scope-check`，不含路径/主机/时间），下游全部重建
   （脚本 `evidence/make_foreign_identity_runs.py`）。

### 4.5 确定性与摘要

```bash
# 同七输入跑两次，再用 cmp 逐字节比较
cmp D:/tmp/s8rev-work/decision-det-1.json D:/tmp/s8rev-work/decision-det-2.json
# 摘要：手工 sha256(规范化 JSON, 自排除) vs 仓库函数 vs 文件内记录值 —— 三方比对
# 负例：错误摘要 / 改一个 check / 未知字段 / check 顺序错乱 / 缺 check / 策略摘要不符
#       / 重复 JSON key / NaN / BOM —— 逐项确认被拒
# 泄露扫描：递归键遍历 + 14 条正则（时间戳/UUID/路径/CI URL/DB URL/Traceback/secret/主机名/耗时/正文）
```

### 4.6 CLI 契约与边界

```bash
# 缺参数 → 2；--verification-report（未认识参数）→ 2
# bundle 根输入损坏 → 3（且确认**未**写产物）；qualification-policy 损坏 → 3
# 输出路径不可写（Z:/nonexistent-drive/out.json）→ 5
# 源码扫描：import 段、危险模块正则、禁止开关、禁止字段名、文件读取点
```

### 4.7 回归与静态检查（在隔离 worktree 内重跑）

```bash
uv run pytest tests/unit/evaluation/test_qualification.py \
             tests/unit/evaluation/test_qualification_input.py \
             tests/unit/evaluation/test_qualification_cli.py --no-cov   # 133 passed
uv run pytest tests/unit/evaluation --no-cov                            # 907 passed, 1 skipped
uv run ruff check .                                                     # All checks passed! rc=0
uv run ruff format --check .                                            # 276 files already formatted, rc=0
uv run mypy src tests scripts mutation                                  # Success: no issues found in 271 source files, rc=0
git check-ignore -v evals/reports/s8-….json                             # 命中 .gitignore:49
```

### 4.8 CI 取证

```bash
gh run list --limit 8
gh run view 35722034597 --json status,conclusion,headSha,attempt,event,workflowName
gh run view 35722034597 --json jobs
gh run view --job 106726919257 --log   # Python 3.12
gh run view --job 106726919284 --log   # Python 3.13
gh run view 35564946471 --json conclusion,headSha,attempt      # S7 封存 CI
```

## 5. 独立复算项目（复核者亲手算出的）

| 序号 | 复核者亲手复算的内容 | 结果 |
|---|---|---|
| R1 | 四个 S5/S6/S7 定义摘要 | 见第 9 节，**四项全部与任务书逐字相符** |
| R2 | S6 `policy_digest`（仓库函数 + 手工 sha256 双路） | 两路结果一致，且等于文件内记录值（见第 9 节） |
| R3 | S8 `qualification_definition_digest` | 手工 `sha256(canonical(QUALIFICATION_DEFINITION))` = 仓库函数值（见第 9 节） |
| R4 | S8 `qualification_policy_definition_digest` | 手工 = 仓库函数值（见第 9 节） |
| R5 | S8 `qualification_policy_digest`（文件值 / 手工 / 仓库函数 三方） | 三方一致（见第 9 节） |
| R6 | S8 `qualification_decision_digest`（文件值 / 手工 / 仓库函数 三方） | 三方一致（见第 9 节） |
| R7 | 六个场景的退出码与 qualification 结论 | 见第 9 节表，**六个全部符合任务书 §十四** |
| R8 | 13 个 check ID 与任务书 §十二 逐字比对 | `tuple(QUALIFICATION_CHECKS) == 任务书列表` → `True` |
| R9 | 两次 Decision 逐字节比较（`cmp`） | `rc=0`，两文件 SHA-256 相同（见第 9 节） |
| R10 | Decision 单 LF 结尾 / 无 CR | `endswith(b"\n") and not endswith(b"\n\n")` = True，`b"\r" not in b` = True |
| R11 | 泄露扫描（14 条正则 + 禁字段递归遍历） | **全部 NONE**（见第 6 节） |
| R12 | 模型层矛盾组合拒绝矩阵（10 组负例 + 3 组合法） | 10 组负例全拒、3 组合法全通过 |
| R13 | 策略 `extra="forbid"` / 摘要不符 / 重复 key / NaN / BOM 拒绝 | 全部被拒（见第 6 节） |
| R14 | CLI 退出码 2 / 3 / 5 | 2、3（且不写产物）、5 |
| R15 | 定向测试 133 / evaluation 单测 907 / ruff / mypy 重跑 | 全绿（见第 4.7 节） |
| R16 | CI run `35722034597` 的 `headSha`/`attempt`/`conclusion` 与作业明细 | 与 S8 SHA 对应（见第 9 节） |
| R17 | CI 原始日志中的 `make test` 与 `make policy` 结果 | 独立取证（见第 9 节） |
| R18 | S7 封存 CI `35564946471` | `conclusion=success`，`headSha=862fbfd…`，`attempt=1` |
| R19 | 两个未跟踪文档是否进入过任何历史 | `git log --all` 与 `git rev-list --all --objects` **均无命中** |
| R20 | `git check-ignore` 对 S8 产物的命中 | 命中 `.gitignore:49 evals/reports/*` |

## 6. 通过项目

### A. 基线身份
- S8 提交存在，SHA 正确：`f1af320e2603a1f9ec5b7889767f1e5dd774f1c8`。
- 父提交 = `862fbfd1bc0e6cb333fe242d2ff2143e6410ba73` = S7 封存 SHA（`git rev-parse f1af320^`）。
- 改动范围 = 8 文件（+3806 / −1），全部落在 `docs/`（1）、`evals/policies/`（1）、
  `src/ai_psi/evaluation/`（2）、`tests/unit/evaluation/`（4）——与任务书 §六 允许的清单一致。
- **未夹带** `AI-PSI-白皮书.md` / `AI-PSI-总体架构蓝皮书.md`：`git show --stat f1af320` 无命中，
  `git log --all -- <path>` 与 `git rev-list --all --objects` 均无命中（两个文档**全历史 0 次提交**）。
- 标签仍只有 1 个既有标签 `stage6-candidate-baseline`，未增未改。
- `origin/main == HEAD == S8 SHA`。

### B. S5/S6/S7 语义保护
- 四个定义摘要**独立复算，逐字命中任务书「当前正式契约」**（见第 9 节）。
- S6 `policy_digest` 手工重算命中任务书基准。
- 读代码确认：S8 提交**未触碰** `comparison.py` / `gate.py` / `evidence.py` / `serialization.py`
  / `loader.py` / 任何 S5–S7 测试文件；`tests/unit/evaluation/conftest.py` **只删 1 行**
  （`from ai_psi.evaluation.evidence import EvidenceInputs, bundle_digest` 被展开为多行 import 块），
  其余 160 行为**纯新增**，既有夹具函数体**未被改写**——复核者逐块核对了该文件的 diff。
- 上游**正式加载器**被复用而非另起一套：S8 直接 import `load_comparison` / `load_gate_decision` /
  `load_evidence_bundle` / `verify_evidence_bundle`，未复制语义不同的第二套加载逻辑。

### C. S8 核心行为（六个场景，全部由复核者独立造链）

| # | 场景 | evidence | applicability | gate | qualification | 期望退出码 | **实测退出码** | 判定 |
|---|---|---|---|---|---|---|---|---|
| 1 | 真实 Mock Golden 链 | VERIFIED | APPLICABLE | PASS | QUALIFIED | 0 | **0** | 通过 |
| 2 | Gate FAIL + Evidence VERIFIED | VERIFIED | APPLICABLE | FAIL | DISQUALIFIED | 1 | **1** | 通过 |
| 3 | Gate NOT_EVALUATED + VERIFIED | VERIFIED | APPLICABLE | NOT_EVALUATED | NOT_EVALUATED | 4 | **4** | 通过 |
| 4 | Evidence INVALID（改 Artifact 字节） | INVALID | NOT_EVALUATED | PASS | DISQUALIFIED | 1 | **1** | 通过 |
| 5 | Evidence NOT_VERIFIABLE（缺 Artifact） | NOT_VERIFIABLE | NOT_EVALUATED | PASS | NOT_EVALUATED | 4 | **4** | 通过 |
| 6 | Policy NOT_APPLICABLE（改实验身份） | VERIFIED | NOT_APPLICABLE | NOT_EVALUATED | NOT_EVALUATED | 4 | **4** | 通过 |

补充确认：
- 场景 4 的 `checks` 明细显示**恰好一项 FAIL**（`evidence_verification_verified`，
  `evidence_verification_invalid`），第 5–13 项一律 `NOT_EVALUATED / not_reached`——
  **没有把未执行的检查伪装成 PASS**。
- 场景 5 同样是 3 PASS + 10 NOT_EVALUATED，无 FAIL，无伪造 PASS。
- 场景 6 的 `reason_codes` = `["experiment_identity_not_allowed", "not_reached",
  "qualification_policy_not_applicable"]`，`policy_applicability.reasons = ["model_out_of_scope"]`，
  稳定、去重、字典序。
- 场景 4/5 中 `policy_applicability` 为 `NOT_EVALUATED`（原因 `assessment_not_reached`）——
  与任务书 §九「不得依赖不可信证据推导出 APPLICABLE」一致。

### D. 确定性与摘要
- **逐字节一致**：同七个输入跑两次，`cmp` 返回 0；两份文件 SHA-256 均为
  `0720d3307f83f50388365f4ecd45be8b470c10ed4a95b2143f6902111c103661`。
- **单 LF、无 CRLF**：文件 4615 字节，结尾恰好一个 `\n`，全文无 `\r`。
- **摘要可独立重算**：`qualification_decision_digest` 与
  `qualification_policy_digest` 均通过「文件记录值 / 手工 sha256 / 仓库函数」**三方一致**。
- **改一个 check → 摘要变化**：把 `checks[12].observed` 改为 `FAIL` 后重算，摘要改变（`True`）。
- **错误摘要被拒**：`parse_qualification_decision` 抛 `ValidationError`，
  `load_qualification_decision` 抛 `QualificationInputError`。
- **策略摘要不符被拒**：`load_qualification_policy` 抛出
  `qualification_policy_digest 与策略内容不符：文件里写的是 …`（改内容不跟摘要 / 摘要格式非法，两种都拒）。
- **禁字段与泄露扫描：全部 NONE**——`release_allowed` / `deploy_allowed` / `merge_allowed` /
  `production_ready` / `auto_merge` / `auto_deploy` / `approved_for_release` /
  `release_recommendation` / `quality_score` / `risk_score` / `trust_score` /
  `recommendation` / `extra` 一个都没有；正则层面时间戳、日期、UUID、Windows/POSIX 绝对路径、
  `http(s)://`、数据库 URL、`Traceback`、secret 关键词、主机名、耗时、正文关键词——**全部零命中**。

### E. 聚合与模型约束
- **结果集合闭合**：`QUALIFIED | DISQUALIFIED | NOT_EVALUATED`（`StrEnum`）。
- **applicability 集合闭合**：`APPLICABLE | NOT_APPLICABLE | NOT_EVALUATED`。
  第三个取值有任务书依据（§九「无法安全执行的 applicability 检查标为 NOT_EVALUATED」）。
- **check 结论闭合**：`PASS | FAIL | NOT_EVALUATED`。
- **13 个 check ID 固定且顺序正确**：与任务书 §十二 列表**逐字相等**（程序化比对 `True`）。
- **`extra` 被禁止**：Decision 顶层多一个字段 → `QualificationInputError`；
  check 多一个字段 → `ValidationError`；策略顶层/scope 多一个字段 → 同样被拒。
- **四类「永不 QUALIFIED」全部生效**（模型层 10 组负例全拒）：
  Evidence INVALID + QUALIFIED、Gate FAIL + QUALIFIED、Gate NOT_EVALUATED + QUALIFIED、
  Policy NOT_APPLICABLE + QUALIFIED、QUALIFIED 但存在 FAIL、NOT_EVALUATED 但全 PASS、
  check 顺序错乱、缺 check —— **一律 `ValidationError`**。3 组合法组合（QUALIFIED /
  DISQUALIFIED / NOT_EVALUATED）**全部通过**。
- **预生成 VerificationReport 不能作为受信输入**：
  - CLI 参数表中**不存在** `--verification-report`；实测传入该参数 → 参数错误、**退出码 2**；
  - 全模块**文件读取点仅两处**（`load_qualification_policy`、`load_qualification_decision`），
    外加 S7 验证器自身对七个输入的读取；**没有任何**"读报告"的路径；
  - 源码中 `VerificationReport` 一词只出现在**文档字符串与禁用清单**里。
- **S8 自行调用正式 S7 verifier**：`build_qualification_decision` 在第 1522 行
  **直接调用** `verify_evidence_bundle(EvidenceVerificationInputs(...))`，使用**本次调用返回**的报告；
  未使用 subprocess、未解析 stdout/stderr。

### F. 边界检查
- **不调用 Provider / 不读网络 / 不连数据库**：`qualification.py` 与 `qualification_cli.py` 的
  完整 import 段只有 `contextlib / hashlib / json / os / tempfile / dataclasses / enum / pathlib /
  typing / pydantic / argparse / sys` 加上五个 `ai_psi.evaluation.*` 内部模块。
  全文件正则扫描 `subprocess|socket|requests|httpx|urllib|aiohttp|psycopg|sqlalchemy|sqlite3|
  asyncio|os.system|popen` → **NONE**。
- **无时间/随机/路径/自省来源**：`datetime|time.|uuid|random|secrets|getpass|platform.|
  __file__|inspect.` 扫描 → **NONE**（满足 §十一「不得使用 inspect.getsource / 绝对路径 /
  当前时间 / 随机值 / 函数地址 / 运行环境地址」）。
- **不新增依赖**：`pyproject.toml` / `uv.lock` 在 `862fbfd..f1af320` 之间**零改动**。
- **不新增迁移**：`alembic` / `migrations` 目录**零改动**。
- **不改生产行为**：改动仅限 `src/ai_psi/evaluation/`（新增两个文件），
  `src/ai_psi/domain` / `src/ai_psi/cognition` 零改动。
- **不把 QUALIFIED 表述为发布授权**：Decision JSON 中无任何授权字段；
  CLI 终端输出显式打印"它不是签名，也**不构成**发布、部署或合并的任何依据"。
- **原子写入落在目标同目录**：`tempfile.mkstemp(dir=path.parent, …)` + `os.replace`，
  **不落 C 盘 `%TEMP%`**——实现者自审第 1 条提示的 C 盘隐患，复核者确认**仓库代码中不存在**
  （仅实现者本人的一次性脚本曾如此，且该脚本未入库）。
- **不产生未被忽略的 S8 报告产物**：`git check-ignore -v` 对
  `evals/reports/s8-qualification-decision.json` / `s8-bundle.json` / `s8-decision-1.json`
  全部命中 `.gitignore:49 evals/reports/*`；复核前后 `git status --porcelain`
  均只有那两个未跟踪文档。

### G. 回归证据
- **CI 直连取证**：`gh run view 35722034597 --json status,conclusion,headSha,attempt,event,workflowName`
  → `{"attempt":1,"conclusion":"success","event":"push","headSha":"f1af320e2603a1f9ec5b7889767f1e5dd774f1c8",…}`
  —— `headSha` **等于 S8 SHA**，`attempt=1`（首次运行全绿，未重跑）。
  三个作业 `Lint & Type Check` / `Test (Python 3.12)` / `Test (Python 3.13)` **全部 success**。
- **CI 原始日志独立取证**（`gh run view --job <id> --log`）：
  - Python 3.12：`3040 passed, 9 skipped in 130.41s`；
  - Python 3.13：`3040 passed, 9 skipped in 146.06s`；
  - 两级覆盖率闸门：`coverage report --fail-under=75` → TOTAL **94%**；
    `--include="*/ai_psi/domain/*,*/ai_psi/cognition/*" --fail-under=85` → TOTAL **98%**。
  工作流 `ci.yml` 中 `make test`（含 PostgreSQL 服务与集成测试）与 `make policy` 是**独立步骤**，
  两者成功即 CI 成功 —— 因此**"make test / make policy / PostgreSQL 集成测试"三项由 CI 直连证据覆盖**，
  且严格对应 S8 SHA。
- **复核者亲自重跑**：S8 定向测试 **133 passed**；`tests/unit/evaluation` **907 passed, 1 skipped**；
  `ruff check` / `ruff format --check` / `mypy src tests scripts mutation` **全部通过**（退出码 0）。
- **S7 封存 CI**：`gh run view 35564946471` → `conclusion=success`，`headSha=862fbfd…`，`attempt=1`。
- **未独立重跑**（如实标注）：`make test` 全量与 PostgreSQL 集成测试**未在本地重跑**，
  改用**上面 CI 的原始日志**作为独立证据；理由：耗时且 CI 证据与 SHA 严格对应、强度更高。

## 7. 不通过项目

**无。**

复核者未发现任何核心行为不符、摘要不可重算、确定性失败、S5/S6/S7 语义被改变、
基线身份不可信、CI/测试证据与 SHA 不对应，或任何被隐藏的失败。

## 8. 无法确认项目（UNVERIFIED）

| # | 项 | 原因 | 影响 |
|---|---|---|---|
| U1 | ~~**任务书完整性标记 `【S8-TASK-END】`**~~ **→ 已结案，见第 13 节** | 复核者拿到的 `D:\tmp\s8-review-inputs\S8-TASKBOOK.md` **在 1450 行处截断**（末行为 `- [ ] 本地 HEAD 与 origin/main 一致；` 后跟一个孤立的 `-`），全文**不含** `【S8-TASK-END】`。修正：无法判断这是**投递副本的截断**还是**原始任务书本就缺失**该标记。 | **不影响本次判定**：复核所需的三处权威内容（「当前正式契约」五个摘要、§六 范围、§十二 check 列表、§十四 退出码）**均完整存在**，且复核者对它们的复算全部命中。仅作完整性登记。**投放方随后取回完整版，U1 已按第 13 节独立复核结案。** |
| U2 | 实现者的**本地** `make test` 计数 `3039 passed / 10 skipped`（自审主张 16） | 复核者未在本地重跑全量套件。CI 原始日志为 `3040 passed / 9 skipped`（3.12 与 3.13 两次）。 | **不是缺陷**：差异恰为 1，且可由平台差异解释——`tests/unit/evaluation/test_models_and_loader.py:250` 带 `@pytest.mark.skipif(sys.platform == "win32", reason="Windows 建符号链接需要额外权限")`，在 Windows 本地跳过而 CI（ubuntu）执行。计数差异**方向与幅度完全吻合**。复核者据此判定自审主张 16 与独立证据一致（在平台差异范围内），但**未把这组本地数字当作已验证事实**。 |
| U3 | 实现者自审主张 17–20 的**本地**运行输出本身 | 复核者只核对了其中的**结论性数字**（CI 日志、重跑结果），未逐条重现实现者的原始终端输出。 | 低：相关结论已被 CI 原始日志与复核者的重跑独立覆盖。 |

## 9. 复核者独立复算出的关键数值

### 9.1 五个冻结摘要（期望值来源：任务书「当前正式契约」）

| 名称 | 复核者复算值 | 任务书基准 | 判定 |
|---|---|---|---|
| `comparison_definition_digest` (S5) | `sha256:63b100d5615d9455538fc65b1ae6a8423231ac2141663a772e0fceaff14e1bcf` | 同 | **相符** |
| `gate_definition_digest` (S6) | `sha256:c084f264b3a0db5bc2864e6e69d1e739980472883b8c4d7540aaddf24a79cdc6` | 同 | **相符** |
| `evidence_bundle_definition_digest` (S7) | `sha256:e003d98d5907225c959feb1bae63855ea75ea0a49616f05795cbd95fde9760bf` | 同 | **相符** |
| `verification_definition_digest` (S7) | `sha256:6ca82823a1a7a6f14583024f61efd9de05fce615a6973c488aff035a0771eea5` | 同 | **相符** |
| **S6 `policy_digest`** | `sha256:fbb5754cf37b8d1588e2ef447f397e96b2def4553a4d9072f91966a2e3475686` | `sha256:fbb5754cf37b8d1588e2ef447f397e96b2def4553a4d9072f91966a2e3475686` | **相符**（仓库函数 / 手工 sha256 / 文件记录值 三方一致） |

### 9.2 S8 自身摘要（任务书未给基准，属新建产物；复核其**可重算性**）

| 名称 | 复核者手工复算值 | 与仓库函数/文件记录 |
|---|---|---|
| `qualification_definition_digest` | `sha256:369afce0087c24c57761eb0ded566a6fc0cc51c7d8a441c6c9b7cfd4903309fc` | 一致 |
| `qualification_policy_definition_digest` | `sha256:224f0ec3e7b6010dd66e843094a6566758c901fc72b159bb6e767a1426adc4be` | 一致 |
| `qualification_policy_digest` | `sha256:4229195a7e1db844fe0dd3ac8fecaec57f2dfc2766e978e9ffdf7da0a202ccc1` | 三方一致（文件记录值 = 手工 = 仓库函数） |
| `qualification_decision_digest`（真实链） | `sha256:b427803ad9497273e807b96b7de1e0e44209b361939a8ab7a826f4242201a8f1` | 三方一致 |

### 9.3 六个场景的退出码与结论

| 场景 | 退出码（实测） | `evidence` | `applicability` | `gate` | `qualification` |
|---|---|---|---|---|---|
| 1 真实链 | **0** | VERIFIED | APPLICABLE | PASS | **QUALIFIED** |
| 2 Gate FAIL | **1** | VERIFIED | APPLICABLE | FAIL | **DISQUALIFIED** |
| 3 Gate NOT_EVALUATED | **4** | VERIFIED | APPLICABLE | NOT_EVALUATED | **NOT_EVALUATED** |
| 4 Evidence INVALID | **1** | INVALID | NOT_EVALUATED | PASS | **DISQUALIFIED** |
| 5 Evidence NOT_VERIFIABLE | **4** | NOT_VERIFIABLE | NOT_EVALUATED | PASS | **NOT_EVALUATED** |
| 6 Policy NOT_APPLICABLE | **4** | VERIFIED | NOT_APPLICABLE | NOT_EVALUATED | **NOT_EVALUATED** |

辅助退出码：缺参数 → **2**；`--verification-report`（未认识参数）→ **2**；
bundle 根输入损坏 → **3（且未写产物）**；qualification-policy 损坏 → **3**；输出不可写 → **5**。

### 9.4 字节比较与确定性

```
cmp D:/tmp/s8rev-work/decision-det-1.json D:/tmp/s8rev-work/decision-det-2.json
  → rc = 0  （逐字节相同）
sha256(decision-det-1.json) = 0720d3307f83f50388365f4ecd45be8b470c10ed4a95b2143f6902111c103661
sha256(decision-det-2.json) = 0720d3307f83f50388365f4ecd45be8b470c10ed4a95b2143f6902111c103661
长度 4615 字节；结尾恰好一个 LF；全文无 CR
```

### 9.5 基线身份

```
git rev-parse f1af320^  → 862fbfd1bc0e6cb333fe242d2ff2143e6410ba73   （= S7 封存 SHA）
git rev-parse origin/main → f1af320e2603a1f9ec5b7889767f1e5dd774f1c8 （= HEAD）
git tag --list → stage6-candidate-baseline（仅 1 个既有标签）
diff 862fbfd..f1af320 --numstat:
  docs/evaluation.md                                      +200  -0
  evals/policies/s8_mock_golden_qualification_v1.json      +35  -0
  src/ai_psi/evaluation/qualification.py                 +1620  -0
  src/ai_psi/evaluation/qualification_cli.py              +211  -0
  tests/unit/evaluation/conftest.py                       +160  -1
  tests/unit/evaluation/test_qualification.py             +664  -0
  tests/unit/evaluation/test_qualification_cli.py         +455  -0
  tests/unit/evaluation/test_qualification_input.py       +461  -0
```

### 9.6 CI

```
run 35722034597: attempt=1  conclusion=success  headSha=f1af320e2603a1f9ec5b7889767f1e5dd774f1c8
  Lint & Type Check     : success
  Test (Python 3.12)    : success   3040 passed, 9 skipped  | 覆盖率 94% / 98%
  Test (Python 3.13)    : success   3040 passed, 9 skipped  | 覆盖率 94% / 98%
run 35564946471 (S7 封存): attempt=1  conclusion=success  headSha=862fbfd…
```

### 9.7 复核者在隔离 worktree 内重跑

```
tests/unit/evaluation/test_qualification{,_input,_cli}.py  → 133 passed
tests/unit/evaluation                                      → 907 passed, 1 skipped
ruff check .            → All checks passed!            rc=0
ruff format --check .   → 276 files already formatted   rc=0
mypy src tests scripts mutation → Success: no issues found in 271 source files  rc=0
```

## 10. 技术性阻断判定

**未发现 S8 技术性阻断。**

## 11. 复核过程对环境的改动与还原

| 项 | 状态 |
|---|---|
| 主工作树 `D:\projects\ai-psi` | **未被修改**。复核结束 `git status --porcelain` 仍**只有**两个未跟踪文档（`AI-PSI-白皮书.md`、`AI-PSI-总体架构蓝皮书.md`）。复核者未 `add`/`commit`/`push`/`reset`/`clean`/`stash`/`rebase`/`checkout` 主工作树。 |
| 隔离 worktree `D:/tmp/s8rev` | 已 `git worktree remove --force` 并 `git worktree prune`；`git worktree list` 只剩 `D:/projects/ai-psi`。 |
| 临时工作目录 `D:/tmp/s8rev-work` | 已删除。 |
| 证据副本 | 关键原始证据已复制到本文件同级的 `evidence/`（六个 Decision JSON、三个造链脚本、CI 证据摘录），以便复核可追溯；这些文件**不在仓库内**。 |
| 仓库内产物 | 复核全程未向仓库写入任何文件；`git check-ignore` 确认 `evals/reports/*` 仍被忽略。 |
| live 测试 | 未运行任何 `-m live` / `AI_PSI_RUN_LIVE_TESTS=1` 用例。 |

## 12. 最终建议

### `ACCEPT`

理由：基线身份可信（父提交与 S7 封存 SHA 逐字一致、未夹带未跟踪文档、未动标签）；
S5/S6/S7 四个定义摘要与 S6 `policy_digest` **独立复算全部命中任务书基准**，且实现未触碰这些模块的语义；
S8 六个行为场景由复核者**独立造链**、走**正式 CLI 入口**，退出码与 qualification 结论**全部符合任务书 §十四**；
Decision 逐字节确定、摘要可独立重算、错误摘要被拒、禁字段与敏感内容扫描零命中；
13 个 check ID 与任务书 §十二 **逐字一致**；模型层矛盾组合全部被拒；
CI run `35722034597`（`attempt=1`、`headSha` = S8 SHA、三作业全绿）与本地重跑的 ruff/mypy/定向测试证据与 SHA **严格对应**。

结论：S8 阶段交付物**通过独立复核**。

### 附带观察（不构成阻断，供归档参考）

1. **O1（文档措辞，字面命中）**：`docs/evaluation.md:810` 出现
   `本层因此不使用 signed / signature / authenticated / trusted source / attested / tamper-proof 这类措辞`
   —— 该行是**否定式声明**（说明这些措辞被**禁止使用**），并非把 QUALIFIED 表述为发布授权。
   按任务书 §十八「不得使用以下措辞」的**字面**读法可判命中；按**意图**读法无违规。
   复核者判定为**可接受的否定式引用**，登记备查。
2. **O2（§十五 部分条目覆盖不全）**：任务书 §十五 要求 QualificationPolicy 与
   QualificationDecision **均**提供 "parse 入口 / load 入口 / canonical dump / 原子写入"。
   Decision 侧齐备（`parse_qualification_decision` / `load_qualification_decision` /
   `qualification_decision_payload` / `write_qualification_decision` 原子写）；
   Policy 侧提供了 `parse_qualification_policy` / `load_qualification_policy` / `qualification_policy_digest`，
   **没有**独立的 canonical dump 与原子写 helper。
   复核者判定为**轻微偏离**：策略在 S8 中**只作输入**（无任何生产/写出路径），
   且其规范化形式由 `qualification_policy_digest` 的 canonical 计算完全钉死；
   不影响任何验收行为，建议作为维护项登记。
3. **O3（applicability 第三取值）**：`QualificationApplicabilityStatus` 有
   `APPLICABLE / NOT_APPLICABLE / NOT_EVALUATED` 三个取值，§九 字面只列了前两个。
   第三个取值有任务书自身依据（§九「无法安全执行的 applicability 检查标为 NOT_EVALUATED」），
   且语义正确（"这次判不了它管不管得着" ≠ "它不归我管"）。判定为**合理扩展**，非偏离。
4. **O4（未跟踪文档导致的"工作树干净"未达成）**：任务书 §二十四 要求"开工工作树干净"，
   实际开工时存在两个属于项目负责人的未跟踪文档。实现者如实登记为 `NOT_SATISFIED` 并**停在此处**，
   未 reset / 未 stash / 未删除未知文件 —— 这正是任务书 §四 要求的处置方式。
   复核者确认这两个文档**全历史 0 次提交**、**未被 S8 夹带**，且复核结束时仍原样存在。
   判定：**处置正确**，不是 S8 的缺陷。

---

*复核者：Claude Code 独立复核 Agent（`claude-opus-5[1m]`）*
*复核日期：2026-09-23*
*复核对象：`f1af320e2603a1f9ec5b7889767f1e5dd774f1c8`*

---
---

# 13. 补记：U1 结案（任务书完整性）

> 本节为**追加**内容，复核日期同为 **2026-09-23**。
> 触发：投放方告知我拿到的任务书是**截断副本**，并提供完整版
> `D:\tmp\s8-review-inputs\S8-TASKBOOK-FULL.md`（1662 行 `wc -l`，
> 含末尾 `【S8-TASK-END】`）。
> 🔴 **本节结论全部由复核者自行 diff 与自行判定，未采信投放方的任何判断。**

## 13.1 U1 成因

**投递副本截断，不是任务书本身缺失。**

复核者亲自核对：

```
md5(S8-TASKBOOK.md)      = 99dc481af17d64b3883dc2b2e85e4127
md5(S8-TASKBOOK-FULL.md) = ec5fcb0da1450216eb5ceb260914f5a9
grep -c "S8-TASK-END" S8-TASKBOOK-FULL.md → 2
   （其一为 §前言 第 77 行的"必须看到…标记"说明，其二为文件真正的末尾标记）
```

## 13.2 差异的确切范围

```
diff -u S8-TASKBOOK.md S8-TASKBOOK-FULL.md
  → 仅 1 个 hunk：@@ -1448,4 +1448,216 @@
  → 新增 193 个非空行，删除 0 行
head -n 1447 <两个文件> | cmp → rc=0（前 1447 行**逐字节相同**）
```

因此：**截断副本是完整版的严格前缀**（前 1447 行完全一致），差异只发生在
文件尾部 4 行之后。复核者此前基于截断副本做的全部复核（§一–§二十四 的
五个冻结摘要、§六 范围、§十二 check 列表、§十四 退出码）**其权威来源完好**，
不受截断影响。

## 13.3 差异范围内的条目清单（逐条）

差异内容**只有**两组，全部位于文件尾：

**（一）§二十四 验收清单被截断的两个勾选项**（完整版 1450–1451 行）

| 新增勾选项 | 是否为新要求 | 复核者据此补做的取证 |
|---|---|---|
| `- [ ] 未创建或修改标签；` | **否**——§二十二 第 37 条（完整版 1312 行）已有「创建或修改标签」禁令；§四 第 246 行亦有「标签仍只有既有标签，不得创建或修改标签」。属同一条要求的清单复述 | `git tag --list` 在 S7 基线（`862fbfd`）与 S8（`f1af320`）**均为 `stage6-candidate-baseline` 一个标签**；`git rev-list -n1 stage6-candidate-baseline` 在两处**均为 `5e9c7f02b6eb7d5e19fba7ffe72158078dd62006`** → 标签未增、未移、未改。**通过** |
| `- [ ] 未进入下一阶段。` | **否**——§二十二 第 57 条（完整版 1332 行）「自动进入下一阶段」禁令；§十八 第 1024 行「不进入下一阶段」 | 复核者已确认 S8 改动仅 8 文件、全部落在 §六 允许清单内（`docs/`、`evals/policies/`、`src/ai_psi/evaluation/`、`tests/unit/evaluation/`），**未夹带任何下一阶段产物**。**通过**（文档侧另见 13.5 的 O5） |

**（二）新增的 §二十五「最终报告格式」**（完整版 1455–1662 行，共 17 节、
约 190 行）

复核者逐节把它回溯到 §一–§二十四，**未发现任何未出现过的技术要求、
验收项、禁止事项或退出码约定**：

| §二十五 节 | 内容性质 | 回溯到（均在 1–1447 行内，即我已复核的部分） |
|---|---|---|
| 1 最终提交 | 报告字段（SHA / 起始 SHA / 独立提交 / amend / 推送） | §四、§二十、§二十一 |
| 2 起始状态 | 报告字段 | §四 |
| 3 改动范围 | 报告字段 | §六、§七、§二十二、§二十四 |
| 4 QualificationPolicy | 报告字段（schema version / definition digest / ID / revision / digest / 作用域 / 允许 outcome / 是否含发布许可） | §八 |
| 5 QualificationDecision | 报告字段（…/ checks 数量 / 固定顺序 / 聚合入口 / 是否拒绝矛盾组合） | §十、§十一、§十二 |
| 6 聚合矩阵 | 6 行矩阵复述 | §三、§十七 C |
| 7 S7 重新验证 | 报告字段（…/**verifier 调用次数**） | §十三、§十七 D |
| 8 Policy applicability | 11 项复述 | §九、§十七 B |
| 9 CLI | 6 个退出码复述 | §十四 |
| 10 确定性 | 复述（cmp / 结尾换行 / CRLF / 时间戳 / UUID / 路径） | §十六、§十七 G |
| 11 安全 | 复述（secret / response_text / Prompt / failure_detail / Traceback / DB URL / …） | §十六、§十七 H |
| 12 S1a—S7 回归 | 14 项复述 | §十七 I、§二十 |
| 13 测试与质量 | 复述（含**总体覆盖率**、**domain+cognition 覆盖率**、**必需测试是否跳过**） | §十九、§二十四 |
| 14 CI | 复述（Run URL / ID / SHA / 三作业 / 两级覆盖率 / 首次全绿 / 手工重跑） | §二十一 |
| 15 最终仓库状态 | 复述 | §二十、§二十四 |
| 16 范围声明 | 30 项，逐条对应 §二十二 的 57 条禁令 | §二十二 |
| 17 阶段结论 | 「最终状态只能填 COMPLETE / BLOCKED / FAILED」「下一阶段必须为否」 | §二十四、§二十二 #57 |

**§二十五 是"报告填写模板"，不是"交付物规范"**：它约束的是**实现者最终
报告怎么写**，不新增任何对 S8 代码/产物本身的要求。其中唯一的字面新词是
报告字段名（如 `Run URL`、`必需测试是否跳过`）与报告枚举
（`COMPLETE / BLOCKED / FAILED`），三者都不构成技术要求、验收项、禁止事项
或退出码。

## 13.4 §二十五 使之显式化的两项，复核者补做取证

§二十五 第 7 节的 `verifier 调用次数` 与第 13 节的 `必需测试是否跳过`
虽非新要求（分别见 §十七 D、§十七 I/§二十四），但既然是**显式**填写项，
复核者按原取证标准**补做了独立复核**：

**(a) S7 verifier 调用次数**

```
grep -n "verify_evidence_bundle" src/ai_psi/evaluation/qualification.py
  41  （文档字符串）
  79  （import）
 246  （QUALIFICATION_DEFINITION 里的文字）
1504 （文档字符串）
1522 （**唯一的真实调用点**）
grep -c "verify_evidence_bundle" src/ai_psi/evaluation/qualification_cli.py → 0
```

`build_qualification_decision` 函数体内**无 `for` / `while`**，调用点 1522
位于函数体直线路径上。→ **每次 S8 构建恰好调用 S7 正式验证器 1 次**，
无重复验证、无循环验证、无二次验证。**通过。**

**(b) 必需测试是否跳过**

```
uv run pytest tests/integration -m live --co -q --no-cov
  tests/integration/test_live_embeddings.py: 4
  tests/integration/test_live_provider.py:  5      →  合计 9
uv run pytest tests/integration --co --no-cov      →  335 tests collected
CI 日志（Python 3.12 / 3.13 各一次）                →  3040 passed, 9 skipped
```

**CI 的 9 个 skip 与 9 个 `live` 标记用例数量精确相等** → 跳过的**全部**是
live 用例，**没有任何必需（非 live）测试被跳过**，PostgreSQL 集成测试未被
`skip` 规避（CI 以 `pgvector/pgvector:pg16` service + `make bootstrap` +
`make test` 真实执行）。**通过。**

## 13.5 追加观察（不构成阻断）

**O5（文档少写一条 §十八 要点）**：任务书 §十八（截断版第 1024 行，**位于
我已复核的 1–1447 行内**）要求 `docs/evaluation.md` 说明「不进入下一阶段」。
复核者复查 `docs/evaluation.md` 全文（1216 行）：`下一阶段` / `后续阶段` /
`阶段 9` / `本阶段` / `不启动` **均零命中**，即该要点**未被文档显式写出**。

- 这是**对复核者自身覆盖深度的更正**，**不是**由新增行引入的问题——
  §十八 从一开始就在我的复核范围内，我此前对文档的核对止于"结构标题 +
  禁用措辞扫描"，未逐条比对 §十八 的要点清单。现如实登记。
- **行为侧不受影响**：S8 未交付任何下一阶段产物（见 13.3），
  `qualification.py` 与 `docs` 中 `QUALIFICATION_DEFINITION` 亦不涉及阶段推进。
- **判定**：文档完整性**轻微不足**，非技术性阻断，**不改变 ACCEPT**。

**O1 补充**：`docs/evaluation.md:810` 的字面命中（`signed` / `trusted source` /
`attested`）复核者**维持 O1 原判**——该处是否定式声明，不是发布授权表述。
需要说明的是：若按**字面**读 §十八/§二十五 的禁用措辞清单，此条会被记为
一处**措辞层面的偏离**；复核者已将其登记为观察项，**未**将其计入技术性阻断。
此项**同样位于 1–1447 行内**，与本次新增内容无关。

## 13.6 U1 对原建议的影响

**不影响。原 `ACCEPT` 建议维持不变。**

判定依据：
1. 差异**零删除**、前 1447 行**逐字节相同** → 我此前的复核所依据的
   权威来源（五个冻结摘要、§六 范围、§十二 check 列表、§十四 退出码）
   **一字未变**；
2. 差异内容为**验收清单的两个勾选项复述** + **一份报告填写模板**，
   **不引入任何新的技术要求、验收项、禁止事项或退出码约定**（13.3 逐节回溯）；
3. §二十五 使之显式化的两项（verifier 调用次数、必需测试是否跳过）
   经复核者**补做独立取证**，**均通过**（13.4）；
4. 差异范围内的两个勾选项经补做取证，**均通过**（13.3）；
5. 新暴露的 O5 为文档完整性轻微不足，**不构成技术性阻断**。

## 13.7 追加后的最终建议

### `ACCEPT`（维持）

**未发现 S8 技术性阻断。** U1 已结案（成因为**投递副本截断**，非任务书缺失），
差异不引入新要求，原 `ACCEPT` 建议维持。

## 13.8 本轮补记对环境的改动

| 项 | 状态 |
|---|---|
| 主工作树 `D:\projects\ai-psi` | **未被修改**；复核结束 `git status --porcelain` 仍只有那两个未跟踪文档 |
| 隔离 worktree | 本轮另建 `D:/tmp/s8rev2`（detach @ `f1af320`），**已 `worktree remove --force` + `prune`**；`git worktree list` 只剩主工作树；`ls /d/tmp/s8rev*` 无匹配 |
| 临时文件 | 已清；本轮未向仓库写入任何文件 |
| live 测试 | **未运行**（仅 `--co` 收集计数，不执行） |
