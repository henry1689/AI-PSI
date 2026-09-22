# S8 收口报告

> 本报告由 **S8 收口任务执行者** 撰写，依据的是 **独立复核者** 的复核记录
> （`docs/reviews/s8/S8-INDEPENDENT-REVIEW.md`，与本文同属一个提交）。
> 报告中所有"独立复核"字样均指向该记录，而非执行者的自审结论。

---

## 1. 基线身份

| 项 | 值 |
|---|---|
| S8 SHA | `f1af320e2603a1f9ec5b7889767f1e5dd774f1c8` |
| S7 父 SHA | `862fbfd1bc0e6cb333fe242d2ff2143e6410ba73` |
| 原 CI Run | `35722034597` |
| 原 CI Attempt | `1`（首次运行，未手工重跑） |
| 原 CI 结论 | `success` |
| CI headSha | `f1af320e2603a1f9ec5b7889767f1e5dd774f1c8`（与 S8 SHA 一致） |
| CI 作业 | Lint & Type Check / Test (Python 3.12) / Test (Python 3.13) —— 三项全绿 |
| S8 提交主题 | `feat(eval): add evaluation qualification decision` |
| S8 改动范围 | 8 个文件，全部位于 `src/ai_psi/evaluation/`、`docs/evaluation.md`、`evals/policies/`、`tests/unit/evaluation/` |

---

## 2. 独立复核结论

| 项 | 内容 |
|---|---|
| 独立复核主体 | Claude Code 独立复核 Agent（模型 `claude-opus-5[1m]`），**与实现者不共享会话与推理过程** |
| 复核记录 | `docs/reviews/s8/S8-INDEPENDENT-REVIEW.md` |
| 复核范围 | 任务书 §五 的 A–G 七项（基线身份 / S5-S6-S7 语义保护 / S8 核心行为 / 确定性与摘要 / 聚合与模型约束 / 边界检查 / 回归证据） |
| 复核方法 | 基于 S8 SHA 另建隔离 worktree（`D:/tmp/s8rev`），初始 `git status --porcelain` 为空；**复核者自行构造** Mock Golden 链（不沿用实现者脚本）；主工作树全程只读 |
| 技术验收 | **通过** |
| 回归验收 | **通过** |
| 确定性验收 | **通过** |
| 边界验收 | **通过** |
| 是否发现技术性失败 | **否** |
| 最终建议 | **`ACCEPT`**（U1 结案后维持） |

### 2.1 复核者独立复算出的关键数值

五个冻结摘要（期望值来源：S8 任务书「当前正式契约」），**全部逐字相符**：

| 摘要 | 复算值 |
|---|---|
| `comparison_definition_digest` | `sha256:63b100d5615d9455538fc65b1ae6a8423231ac2141663a772e0fceaff14e1bcf` |
| `gate_definition_digest` | `sha256:c084f264b3a0db5bc2864e6e69d1e739980472883b8c4d7540aaddf24a79cdc6` |
| `evidence_bundle_definition_digest` | `sha256:e003d98d5907225c959feb1bae63855ea75ea0a49616f05795cbd95fde9760bf` |
| `verification_definition_digest` | `sha256:6ca82823a1a7a6f14583024f61efd9de05fce615a6973c488aff035a0771eea5` |
| S6 `policy_digest` | `sha256:fbb5754cf37b8d1588e2ef447f397e96b2def4553a4d9072f91966a2e3475686`（仓库函数 / 手工 sha256 / 文件记录值 三方一致） |

六个场景（由复核者自行造链、走正式 CLI）：

| 场景 | 退出码 | evidence | applicability | gate | qualification | 冻结规范期望 |
|---|---|---|---|---|---|---|
| 真实 Mock Golden 链 | **0** | VERIFIED | APPLICABLE | PASS | **QUALIFIED** | 0 / QUALIFIED |
| Gate FAIL | **1** | VERIFIED | APPLICABLE | FAIL | **DISQUALIFIED** | 1 / DISQUALIFIED |
| Gate NOT_EVALUATED | **4** | VERIFIED | APPLICABLE | NOT_EVALUATED | **NOT_EVALUATED** | 4 / NOT_EVALUATED |
| Evidence INVALID | **1** | INVALID | NOT_EVALUATED | PASS | **DISQUALIFIED** | 1 / DISQUALIFIED |
| Evidence NOT_VERIFIABLE | **4** | NOT_VERIFIABLE | NOT_EVALUATED | PASS | **NOT_EVALUATED** | 4 / NOT_EVALUATED |
| Policy NOT_APPLICABLE | **4** | VERIFIED | NOT_APPLICABLE | NOT_EVALUATED | **NOT_EVALUATED** | 4 / NOT_EVALUATED |

其余边界（复核者独立取证）：缺参数 → 2；`--verification-report`（未识别参数）→ 2；bundle 根输入损坏 → 3 且**未写产物**；qualification-policy 损坏 → 3；输出路径不可写 → 5。

确定性：同输入两次 Decision `cmp` rc=0，两份文件 SHA-256 同为
`0720d3307f83f50388365f4ecd45be8b470c10ed4a95b2143f6902111c103661`，
4615 字节，结尾恰好一个 LF，无 CR。

其他：13 个 check ID 与任务书 §十二 逐字相等；禁字段与 14 条泄露正则全部无命中；
模型层 10 组矛盾负例全拒、3 组合法全过；策略 `extra=forbid` / 摘要不符 / 重复 key /
NaN / BOM 全拒；`verify_evidence_bundle` 唯一调用点在 `qualification.py:1522`，
每次构建**恰好 1 次**；9 个 skip **全部是 live 用例**，PostgreSQL 集成测试未被规避。

### 2.2 复核者登记的观察（**均不构成阻断**）

| 编号 | 观察 | 处置 |
|---|---|---|
| O1 | `docs/evaluation.md:810` 字面出现 `signed` / `trusted source` / `attested` | **属否定式声明**（正说明本层不使用这些措辞），非发布授权表述。无需改动 |
| O2 | 任务书 §十五 要求 Policy 侧也提供 canonical dump 与原子写；实际只有 parse / load / digest | Policy 仅作输入、无写出路径。**登记为维护项**，本次不改 |
| O3 | applicability 第三取值 `NOT_EVALUATED` 超出 §九 的两值列举 | 有 §九 自身依据（"无法安全执行的 applicability 检查标为 NOT_EVALUATED"），属合理扩展 |
| O4 | 开工时两个未跟踪文档导致"工作树干净"不满足 | 实现者按 §四 停并报告，**非缺陷**。见第 4 节 |
| O5 | `docs/evaluation.md` 中"不进入下一阶段"零命中 | 文档完整性轻微不足，**不改变 ACCEPT**；登记为维护项 |

---

## 3. 验收统计

任务书 §二十四 共 **60 项**验收标准（完整版含末两项"未创建或修改标签""未进入下一阶段"）。

| 类别 | 数量 | 说明 |
|---|---|---|
| **PASS** | **59** | 见下方分布 |
| **NOT_SATISFIED** | **0**（当前） | 历史项见下一行，已转为程序性例外 |
| **APPROVED_PROCEDURAL_EXCEPTION** | **1** | 第 3 项「开工工作树干净」——**不计入 PASS**，见第 4 节 |
| **UNVERIFIED** | **0** | 验收项层面无未确认项 |
| **FAIL** | **0** | 无 |

> ⚠️ 第 3 项在统计中**单列为程序性例外**，**未**按普通通过处理。
> 第 57 项「工作树最终干净」初始为 `PENDING_REMEDIATION`，
> 文档纳管提交 `8e641a205238000a5c1e4e46c6de03adc7dd0f65` 后转为
> `SATISFIED_AFTER_DOCUMENT_INTAKE`，此项计入 PASS 并在此注明其曾有中间态。

### 3.1 复核过程层面的 UNVERIFIED（非验收项，如实登记）

| 编号 | 内容 | 结案 |
|---|---|---|
| U1 | 首次投递给复核者的任务书副本在 1450 行截断、无 `【S8-TASK-END】` | **已结案**：成因是**投递副本截断**，非任务书缺失。复核者自行 diff 完整版确认差异仅 1 个 hunk、**零删除**、前 1447 行逐字节相同；差异只含 2 个勾选项复述 + §二十五 报告模板，**不引入新要求**；§二十五 使之显式化的两项（verifier 调用次数、必需测试是否被跳过）复核者已按原标准**补做并全部通过** |
| U2 | 本地 `3039 passed / 10 skipped` 与 CI `3040 passed / 9 skipped` 相差 1 | **已结案**：`tests/unit/evaluation/test_models_and_loader.py:250` 带 `skipif(sys.platform == "win32")`（Windows 建符号链接需额外权限），缺额方向与幅度完全吻合，非缺陷 |
| U3 | 未逐条重现自审主张 17–20 的原始终端输出 | 结论性数字已由 CI 原始日志与复核者重跑独立覆盖 |
| U4 | 未重跑全量 `make test` 与 PostgreSQL 集成测试 | 改用 `gh run view --job <id> --log` 的 **CI 原始日志**独立取证（与 S8 SHA 严格对应）；**未运行任何 live 测试** |

---

## 4. 程序性偏离

### 偏离一：开工工作树干净 —— 第 3 项

| 项 | 值 |
|---|---|
| 判定 | `NOT_SATISFIED` |
| 处置 | `APPROVED_PROCEDURAL_EXCEPTION` |
| 是否计入普通 PASS | **否** |

**如实记录的理由**：

1. S8 开始与收口时，主工作树均存在两个**项目负责人创建的**未跟踪文档
   （`AI-PSI-白皮书.md`、`AI-PSI-总体架构蓝皮书.md`）；
2. 两个文件**不是** S8 产物，也**不是** S8 执行者生成的文件；
3. 两个文件**未进入** S8 提交——`git show --stat f1af320` 恰为 8 个文件，不含它们；
   且 `git log --all -- <path>` 显示两文件在 S8 时点**全历史 0 次提交**；
4. S8 执行者**没有**修改、删除、`stash` 或 `reset` 这些文件
   （其 `mtime` 为 2026-09-22 10:20:01 与 11:43:20，**早于** S8 提交时间 19:26:45）；
5. 任务书 §四 明确要求保护未知文件，执行者据此**停止并报告**，未擅自处理；
6. 该历史状态**无法事后改写**；
7. 该偏离**不影响** S8 在隔离干净 worktree 中的功能结果——
   同一 S8 SHA 在干净 worktree 上得到 `QUALIFIED` / exit 0（复核者独立复现）；
8. **该项未按普通通过处理。**

> 🔴 **该例外不应成为未来阶段忽略工作树清洁要求的先例。**
> 本次例外的成因是"项目负责人本人的上位文档恰好未纳管"，
> 而非"清洁要求可以被放宽"。

### 偏离二：工作树最终干净 —— 第 57 项

| 阶段 | 判定 |
|---|---|
| 收口开始时 | `PENDING_REMEDIATION` |
| 文档纳管提交 `8e641a2…` 之后 | `SATISFIED_AFTER_DOCUMENT_INTAKE` |

**S8 原提交 `f1af320` 不包含这两个文档，本报告不声称它包含。**
两个文档由**后于** S8 基线的独立提交 `8e641a205238000a5c1e4e46c6de03adc7dd0f65` 纳管。

---

## 5. 文档纳管

| 项 | 内容 |
|---|---|
| 原始路径 | 仓库根目录 `AI-PSI-白皮书.md`、`AI-PSI-总体架构蓝皮书.md` |
| 纳管后路径 | 同原始路径（**未移动目录**） |
| 文档纳管提交 SHA | `8e641a205238000a5c1e4e46c6de03adc7dd0f65` |
| 提交信息 | `docs: add AI-PSI whitepaper and architecture blueprint candidates` |
| 内容是否修改 | **否**。提交前后工作区 sha256 逐字节相同：白皮书 `63184e467faddf11414ac9ec5deeda786c2a9e363c1d87e4521d050cddc2e890`、蓝皮书 `c478dd467039ae7952c22f64356b323f5c44eab744d63a4f24e49102af3a400b` |
| 是否与 S8 提交分离 | **是**。该提交只含这两个文件（`--name-status` 为 2 个 `A`），不含任何 S8 代码或本报告 |
| 是否推送到远程 | 否 |

### 5.1 一项需登记的既有约定：换行存储形态

- 仓库 `core.autocrlf = true`，且**无** `.gitattributes`；
- 全仓 **54 个已跟踪 `.md` 全部以 LF 存储，0 个以 CRLF 存储**；
- 两个文档的工作区字节为 **CRLF**（白皮书 57960B / 2000 处 CRLF，蓝皮书 52526B / 1934 处 CRLF）；
- 按仓库默认配置纳管后，索引内存储为 **LF**（57960−55960 = 2000 字节差，恰等于 CRLF 对数，可核对）；
- **工作区文件未被改写**（提交前后 sha256 相同，提交后 `git status --porcelain` 为空）。

**说明**：未对两个文档运行任何格式化、转换或换行处理；上述行为是**仓库既定配置对任何文件都会做的**
归一化，与项目负责人本人执行 `git add` 的结果一致（不改动工作区文件）。
此事实在此显式登记，以便与"不改变换行格式"的要求对照复核。

---

## 6. 最终工作树

| 项 | 结果 |
|---|---|
| `git status --porcelain` | **空**（文档纳管提交后实测） |
| 是否存在未跟踪文件 | 否 |
| 是否存在未提交修改 | 否 |
| 是否存在隔离 worktree 残留 | 否（`git worktree list` 仅 `D:/projects/ai-psi`） |
| `D:\tmp` 本任务临时目录 | 已清理（见第 9 节） |
| `C:\` 新建临时目录 | 无（本任务的临时产物均在 `D:\tmp`） |
| S8 SHA 是否被改写 | **否**（`git rev-parse f1af320…` 不变） |
| 当前 HEAD | 位于 S8 之后的文档提交，**不得**与 S8 SHA 混同 |

---

## 7. 最终判定

# `ACCEPTED_WITH_EXCEPTION`

依据任务书 §十五 B 的十项条件逐条核对：

| # | 条件 | 状态 |
|---|---|---|
| 1 | 独立复核未发现 S8 技术性失败 | ✅ 复核建议 `ACCEPT`，无技术性阻断 |
| 2 | 核心行为符合任务书 | ✅ 六场景 + 边界全部符合冻结规范 |
| 3 | 回归证据有效 | ✅ CI 原始日志与 S8 SHA 严格对应 |
| 4 | S5/S6/S7 语义未改变 | ✅ 五个冻结摘要独立复算逐字相符 |
| 5 | S8 基线身份明确 | ✅ 父提交 = S7 封存 SHA |
| 6 | 两个文档与 S8 提交分离 | ✅ 独立提交 `8e641a2…` |
| 7 | 最终工作树干净 | ✅ `git status --porcelain` 为空 |
| 8 | "开工工作树干净"如实登记为程序性偏离 | ✅ 第 4 节，**未伪造成通过** |
| 9 | 项目负责人批准接受该偏离 | ✅ 见任务回执第 4 节 |
| 10 | 收口报告完整 | ✅ 本文件 |

**未使用 `ACCEPTED`**，因为第 3 项验收标准历史上确未满足，按 §十五 A 不得使用无例外状态。

---

## 8. 后续维护项

**本次登记、本次不处理**：

1. **`_atomic_write_text` 存在三份实现** —— `gate.py` / `evidence.py` / `qualification.py`。
   整合会触及已封存的 S5/S6/S7 模块，**不得在本任务中重构**，应作为独立维护项处理。
2. **O2：QualificationPolicy 侧缺 canonical dump 与原子写入口**（任务书 §十五 曾提及）。
   Policy 目前仅作输入、无写出路径，故未阻塞 S8；若未来出现写出需求须补齐。
3. **O5：`docs/evaluation.md` 未记载"不进入下一阶段"的边界声明**。文档完整性轻微不足。

---

## 9. 本任务对环境与历史的改动（如实登记）

| 动作 | 是否执行 |
|---|---|
| 修改 S8 实现代码 / S5-S6-S7 代码 | 否 |
| 修改测试或评测阈值 | 否 |
| 修改两个项目文档内容 | 否 |
| 新增依赖 / 新增迁移 | 否 |
| 运行真实 Provider 或 live 测试 | 否 |
| 使用 `git reset --hard` / `git clean -fd` / `git stash` | 否 |
| 删除未知文件 | 否 |
| `amend` S8 提交 / `rebase` / 改写历史 | 否 |
| `git push` / 修改 `origin/main` | 否 |
| 提前执行 AI-PSI-DT-001 | 否 |
