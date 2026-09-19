"""可复现性清单：这份结果究竟是谁、用什么、在什么契约下跑出来的（阶段 7 · S3）。

## 它回答什么

一份评测结果若只有"10 个案例通过"，它在**一周后**是没法被理解的：
不知道是哪个提交跑的、数据集有没有被改过、提示词是不是同一版、
模型配置动了没有。这份清单把那些身份**钉在结果里**。

## 三条硬规矩

1. **一切身份都来自权威来源，不猜。** Git SHA 来自 ``git rev-parse``，
   Prompt 版本来自实际发生的模型调用记录（不变量 18），断言摘要来自
   唯一那份 Python 注册表。任何一项取不到就**如实写不可用**并让正式
   可比较性失败——绝不填 ``"unknown"`` 之后照样判成可比较。
2. **秘密永不进入清单。** 只输出白名单字段：API key、数据库 URL、用户名、
   密码、环境变量全集一律不进。配置摘要基于**白名单结构**计算，
   因此密钥变化不会改变摘要，行为参数变化才会。
3. **不给"可比较"一个布尔就完事。** :func:`compare_manifests` 返回机器可读的
   原因码列表；调用方要能知道**是哪一项不一样**。

## 不做什么

S3 **不判断性能回归**。"两个提交的结果能不能放在一起比"与"哪个更好"
是两件事，后者属于后续切片。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

import ai_psi
from ai_psi.evaluation.assertions import registry_digest
from ai_psi.evaluation.models import CASE_SCHEMA_VERSION, GoldenCase

__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "CodeIdentity",
    "ComparabilityResult",
    "EvaluationIdentity",
    "PromptIdentity",
    "ProviderIdentity",
    "ReproducibilityError",
    "ReproducibilityManifest",
    "RuntimeIdentity",
    "StorageIdentity",
    "build_manifest",
    "collect_code_identity",
    "collect_provider_identity",
    "collect_runtime_identity",
    "comparability_reasons",
    "compare_manifests",
    "dataset_digest",
    "prompt_identity",
    "prompt_versions_from_invocations",
    "storage_identity",
]

#: 清单自身的结构版本。**与案例的 ``schema_version`` 是两回事。**
MANIFEST_SCHEMA_VERSION: Final[int] = 1

#: 仓库根（``<root>/src/ai_psi/evaluation/manifest.py`` 往上四层）。
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]

#: 执行 Git 命令的超时。Git 正常是毫秒级的；挂住说明环境有问题，
#: 而"采集身份时卡死"比"报身份不可用"糟糕得多。
_GIT_TIMEOUT_SECONDS: Final[float] = 10.0

#: 🔴 **Provider 配置摘要的白名单。**
#:
#: 只列**会影响生成行为**且**不含秘密**的项。刻意不遍历 ``Settings``
#: 的所有字段——那样一个新增的密钥字段会在无人察觉时进入摘要，
#: 而摘要会被写进报告。
_PROVIDER_PARAMETER_KEYS: Final[tuple[str, ...]] = (
    "json_mode",
    "max_retries",
    "reasoning_headroom_tokens",
)

#: 原因码：身份契约不一致。**机器可读**，调用方据此决定怎么办。
REASON_MANIFEST_SCHEMA_DIFFERS: Final[str] = "manifest_schema_differs"
REASON_DATASET_DIFFERS: Final[str] = "dataset_differs"
REASON_ASSERTION_REGISTRY_DIFFERS: Final[str] = "assertion_registry_differs"
REASON_PROMPT_VERSIONS_DIFFER: Final[str] = "prompt_versions_differ"
REASON_PROVIDER_DIFFERS: Final[str] = "provider_differs"
REASON_MODEL_DIFFERS: Final[str] = "model_differs"
REASON_PROVIDER_CONFIGURATION_DIFFERS: Final[str] = "provider_configuration_differs"
REASON_EXECUTION_MODE_DIFFERS: Final[str] = "execution_mode_differs"
REASON_STORAGE_BACKEND_DIFFERS: Final[str] = "storage_backend_differs"
REASON_MIGRATION_REVISION_DIFFERS: Final[str] = "migration_revision_differs"
REASON_CODE_REVISION_DIFFERS: Final[str] = "code_revision_differs"
REASON_DIRTY_WORKTREE: Final[str] = "dirty_worktree"
REASON_IDENTITY_UNAVAILABLE: Final[str] = "identity_unavailable"


def _digest(payload: object) -> str:
    """对任意**已规范化**的结构取 SHA-256，返回 ``sha256:<hex>``。

    🔴 三个固定规则缺一不可：``sort_keys`` 让键顺序不影响结果；
    ``separators`` 去掉空格让排版不影响结果；``ensure_ascii=False``
    让中文按 UTF-8 编码（转义与否不该改变语义摘要）。
    """
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


class ReproducibilityError(RuntimeError):
    """身份不可用，因此这次运行**不能**被当作正式可复现运行。

    🔴 它不是"运行失败"，而是"运行的结果不可用于基线"——两者要分开报：
    前者说明系统坏了，后者说明**这次跑的环境不满足条件**
    （工作树脏、拿不到提交、Prompt 注册表为空……）。
    """


class CodeIdentity(BaseModel):
    """代码身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: 🔴 **完整** SHA。``None`` 表示**取不到**（如 wheel 安装、无 ``.git``）——
    #: 它不是 ``"unknown"``：那个字符串会被下游当成一个"值"，
    #: 而这里要表达的是"没有值"。
    commit_sha: str | None
    #: 工作树是否干净。``None`` 表示取不到（同上）。
    working_tree_clean: bool | None
    package_version: str


class EvaluationIdentity(BaseModel):
    """评测契约身份：数据集、断言注册表、案例 schema、执行模式。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_schema_version: int
    dataset_digest: str
    dataset_case_count: int
    assertion_registry_digest: str
    execution_mode: str


class PromptIdentity(BaseModel):
    """**实际使用**的 Prompt 版本。

    🔴 不是"仓库里注册了多少个"，而是"这次运行真的走到了哪些"。
    两者在多深度案例集上不一样：D0 的案例不会经过哲学分析模块。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: 任务名 → 语义版本。键在构造前已按名排序。
    versions: dict[str, str]
    digest: str


class ProviderIdentity(BaseModel):
    """Provider 的配置身份。

    ⚠️ 这里只有**身份**，没有请求。S3 不调用任何真实 Provider。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_name: str
    model_id: str
    #: 是否具备确定性（同样的输入给同样的输出）。Mock 为 ``True``。
    deterministic: bool
    #: 本次运行是否被允许发起网络请求。S1a/S2 恒为 ``False``。
    network_allowed: bool
    #: 白名单参数（见 :data:`_PROVIDER_PARAMETER_KEYS`）。
    parameters: dict[str, str]
    configuration_digest: str


class RuntimeIdentity(BaseModel):
    """运行环境身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    python_version: str
    ai_psi_version: str


class StorageIdentity(BaseModel):
    """存储身份。

    🔴 **不含数据库名、URL、主机、端口、用户名、密码。**
    评测库的名字每次运行都不一样（专用、可丢弃），把它写进身份
    等于说"两次运行的身份永远不同"。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: str
    alembic_revision: str | None


class ReproducibilityManifest(BaseModel):
    """一份评测运行的完整可复现性清单。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_schema_version: int = MANIFEST_SCHEMA_VERSION
    code: CodeIdentity
    evaluation: EvaluationIdentity
    prompts: PromptIdentity
    provider: ProviderIdentity
    runtime: RuntimeIdentity
    storage: StorageIdentity

    def canonical_identity(self) -> dict[str, object]:
        """canonical JSON 用的**稳定身份子集**。

        🔴 **刻意不含** ``python_version``：补丁版本是运行环境的属性，
        不是结果语义的属性；把它放进去会让"换一台机器"表现为
        "结果不可比"。

        🔴 **刻意不含** ``working_tree_clean``：它属于"这次能不能当基线"
        的判定（见 :func:`compare_manifests`），不属于"结果是什么"。

        其余字段都是**稳定且影响语义比较**的身份：同一提交、同一数据集、
        同一提示词、同一 Provider 配置的两次运行，这一份必然逐字节相同。
        """
        return {
            "assertion_registry_digest": self.evaluation.assertion_registry_digest,
            "case_schema_version": self.evaluation.case_schema_version,
            "commit_sha": self.code.commit_sha,
            "dataset_case_count": self.evaluation.dataset_case_count,
            "dataset_digest": self.evaluation.dataset_digest,
            "execution_mode": self.evaluation.execution_mode,
            "manifest_schema_version": self.manifest_schema_version,
            "model_id": self.provider.model_id,
            "network_allowed": self.provider.network_allowed,
            "package_version": self.code.package_version,
            "prompt_versions_digest": self.prompts.digest,
            "provider_configuration_digest": self.provider.configuration_digest,
            "provider_name": self.provider.provider_name,
            "storage_alembic_revision": self.storage.alembic_revision,
            "storage_backend": self.storage.backend,
        }


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------


def _git(repo_root: Path, *arguments: str) -> str | None:
    """执行一条只读 Git 命令；失败或超时返回 ``None``。

    🔴 ``check=False`` + 显式判返回码，而不是让异常冒上来：
    这个函数唯一的失败语义就是"取不到"，而调用方需要的正是那个 ``None``。
    """
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def collect_code_identity(repo_root: Path | None = None) -> CodeIdentity:
    """采集代码身份。

    优先从 Git 读；**没有 ``.git`` 时如实返回不可用**，不伪造 SHA
    （阶段 7 · S3 §七：wheel 或无 ``.git`` 环境不得编造当前提交）。

    Args:
        repo_root: 仓库根；``None`` 时用本包所在的仓库根。

    Returns:
        代码身份。``commit_sha`` / ``working_tree_clean`` 可能为 ``None``。
    """
    root = _REPO_ROOT if repo_root is None else repo_root

    raw_sha = _git(root, "rev-parse", "HEAD")
    commit_sha = raw_sha.strip() if raw_sha is not None else None
    if commit_sha == "":
        commit_sha = None

    raw_status = _git(root, "status", "--porcelain")
    # ⚠️ ``status --porcelain`` 的输出**为空**才是干净；取不到时是 None
    # 而不是 False——"不干净"与"不知道"是两件事。
    working_tree_clean: bool | None = None if raw_status is None else not raw_status.strip()

    return CodeIdentity(
        commit_sha=commit_sha,
        working_tree_clean=working_tree_clean,
        package_version=ai_psi.__version__,
    )


def collect_runtime_identity() -> RuntimeIdentity:
    """采集运行环境身份。

    ⚠️ 只记 Python 版本与包版本。**不记**机器名、用户名、主目录、
    ``pip freeze`` 全集、IP——它们既不帮助复现，又会把运行环境指纹
    带进可以公开的报告。
    """
    info = sys.version_info
    return RuntimeIdentity(
        python_version=f"{info.major}.{info.minor}.{info.micro}",
        ai_psi_version=ai_psi.__version__,
    )


def dataset_digest(cases: Sequence[GoldenCase]) -> str:
    """数据集内容的**语义摘要**。

    🔴 **不是对 YAML 原始字节取哈希。** 注释、缩进、键的书写顺序都不是
    评测语义；换一种等价排版不该让数据集变成"另一个数据集"。
    这里先让每个案例经过 pydantic 模型（规范化结构），按 ``case_id``
    稳定排序后再序列化。

    Args:
        cases: 已加载的案例。

    Returns:
        形如 ``sha256:<hex>`` 的摘要。
    """
    entries = [
        case.model_dump(mode="json") for case in sorted(cases, key=lambda item: item.case_id)
    ]
    return _digest(entries)


def prompt_identity(versions: Mapping[str, str]) -> PromptIdentity:
    """由**实际使用的** Prompt 版本构造身份。

    Args:
        versions: 任务名 → 语义版本。

    Returns:
        Prompt 身份；``versions`` 已按任务名排序。
    """
    ordered = {name: versions[name] for name in sorted(versions)}
    return PromptIdentity(versions=ordered, digest=_digest(ordered))


def collect_provider_identity(
    *,
    provider_name: str,
    model_id: str,
    parameters: Mapping[str, object],
) -> ProviderIdentity:
    """由白名单参数构造 Provider 身份。

    Args:
        provider_name: **装配出来的** Provider 名（不是配置里写的字符串）。
        model_id: 模型标识。
        parameters: 白名单参数；调用方只传 :data:`_PROVIDER_PARAMETER_KEYS`
            里的键。

    Returns:
        Provider 身份，含配置摘要。

    Raises:
        KeyError: 传入了白名单之外的参数——**宁可报错，也不静默丢弃**：
            静默丢弃会让摘要看起来覆盖了某个参数，而其实没有。
    """
    unknown = sorted(set(parameters) - set(_PROVIDER_PARAMETER_KEYS))
    if unknown:
        msg = (
            f"Provider 配置摘要只接受白名单字段，收到未列入的 {unknown}；"
            "新增字段必须同时把它加进 _PROVIDER_PARAMETER_KEYS 并确认它不含秘密"
        )
        raise KeyError(msg)

    normalized = {key: str(parameters[key]) for key in sorted(parameters)}
    return ProviderIdentity(
        provider_name=provider_name,
        model_id=model_id,
        deterministic=provider_name == "mock",
        network_allowed=False,
        parameters=normalized,
        configuration_digest=_digest(
            {"model_id": model_id, "parameters": normalized, "provider_name": provider_name}
        ),
    )


def storage_identity(*, backend: str, alembic_revision: str | None) -> StorageIdentity:
    """构造存储身份。

    Args:
        backend: ``memory`` 或 ``postgresql``。
        alembic_revision: PostgreSQL 模式下的实际 revision；内存模式为 ``None``。

    Returns:
        存储身份。
    """
    return StorageIdentity(backend=backend, alembic_revision=alembic_revision)


# ---------------------------------------------------------------------------
# 可比较性
# ---------------------------------------------------------------------------


class ComparabilityResult(BaseModel):
    """两次运行的**身份契约**是否一致。

    ⚠️ 它**不是**性能结论。``comparable=True`` 只说明"这两份结果可以放在
    同一张表里比较"，不代表任何一方更好——那属于后续切片。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    comparable: bool
    #: 机器可读的原因码；一致时为空元组。
    reasons: tuple[str, ...]


def comparability_reasons(
    left: ReproducibilityManifest,
    right: ReproducibilityManifest,
) -> tuple[str, ...]:
    """逐项比较两份清单，返回**原因码**列表。

    比较项与它们各自对应的问题：

    ==============================  ==============================
    身份项                          不一致意味着
    ==============================  ==============================
    ``manifest_schema_version``     清单结构本身变了
    ``case_schema_version``         案例契约变了
    ``dataset_digest``              数据集内容变了
    ``assertion_registry_digest``   断言语义变了
    ``prompts.digest``              提示词版本组合变了
    ``provider_name``               换了 Provider
    ``model_id``                    换了模型
    ``configuration_digest``        生成参数变了
    ``execution_mode``              换了执行路径
    ``storage.backend``             换了存储后端
    ``storage.alembic_revision``    数据库 schema 版本变了
    ``code.commit_sha``             代码不是同一个版本
    ``code.working_tree_clean``     跑的不是干净的树
    ==============================  ==============================

    Args:
        left: 第一份清单。
        right: 第二份清单。

    Returns:
        原因码（按出现顺序）。空元组表示身份契约完全一致。
    """
    reasons: list[str] = []

    if left.manifest_schema_version != right.manifest_schema_version:
        reasons.append(REASON_MANIFEST_SCHEMA_DIFFERS)
    if left.evaluation.case_schema_version != right.evaluation.case_schema_version:
        reasons.append(REASON_MANIFEST_SCHEMA_DIFFERS)
    if left.evaluation.dataset_digest != right.evaluation.dataset_digest:
        reasons.append(REASON_DATASET_DIFFERS)
    if left.evaluation.assertion_registry_digest != right.evaluation.assertion_registry_digest:
        reasons.append(REASON_ASSERTION_REGISTRY_DIFFERS)
    if left.prompts.digest != right.prompts.digest:
        reasons.append(REASON_PROMPT_VERSIONS_DIFFER)
    if left.provider.provider_name != right.provider.provider_name:
        reasons.append(REASON_PROVIDER_DIFFERS)
    if left.provider.model_id != right.provider.model_id:
        reasons.append(REASON_MODEL_DIFFERS)
    if left.provider.configuration_digest != right.provider.configuration_digest:
        reasons.append(REASON_PROVIDER_CONFIGURATION_DIFFERS)
    if left.evaluation.execution_mode != right.evaluation.execution_mode:
        reasons.append(REASON_EXECUTION_MODE_DIFFERS)
    if left.storage.backend != right.storage.backend:
        reasons.append(REASON_STORAGE_BACKEND_DIFFERS)
    if left.storage.alembic_revision != right.storage.alembic_revision:
        reasons.append(REASON_MIGRATION_REVISION_DIFFERS)

    # ---- 身份可用性：取不到的身份不能被当成"一致" ----
    if (
        left.code.commit_sha is None
        or right.code.commit_sha is None
        or left.code.working_tree_clean is None
        or right.code.working_tree_clean is None
    ):
        reasons.append(REASON_IDENTITY_UNAVAILABLE)
    else:
        if left.code.commit_sha != right.code.commit_sha:
            # 🔴 不同提交**不得被宣称为等价**。本切片不判断谁更好，
            # 但必须说清"它们不是同一个代码版本"。
            reasons.append(REASON_CODE_REVISION_DIFFERS)
        if not (left.code.working_tree_clean and right.code.working_tree_clean):
            # 工作树脏意味着"这份结果来自一个无法从 SHA 重建的代码状态"。
            reasons.append(REASON_DIRTY_WORKTREE)

    return tuple(reasons)


def compare_manifests(
    left: ReproducibilityManifest | None,
    right: ReproducibilityManifest | None,
) -> ComparabilityResult:
    """判定两次运行的身份契约是否一致。

    Args:
        left: 第一份清单；``None`` 表示身份根本没有采集到。
        right: 第二份清单；``None`` 同上。

    Returns:
        ``comparable`` 与原因码列表。**永远返回原因，不只返回布尔**——
        一个 ``False`` 没法告诉调用方该去查哪一项。
    """
    if left is None or right is None:
        return ComparabilityResult(comparable=False, reasons=(REASON_IDENTITY_UNAVAILABLE,))
    reasons = comparability_reasons(left, right)
    return ComparabilityResult(comparable=not reasons, reasons=reasons)


def prompt_versions_from_invocations(invocations: Iterable[Mapping[str, object]]) -> dict[str, str]:
    """从模型调用记录里提取**实际使用过的** Prompt 版本。

    🔴 这是"实际使用了哪些 Prompt"的唯一可信来源：不变量 18 要求每次模型
    调用都必须记录 ``task_name`` 与 ``prompt_version``，因此这条链路能
    **证明**用到了什么，而不是靠读代码猜。

    Args:
        invocations: 调用记录（``task_name`` / ``prompt_version`` 字段）。

    Returns:
        任务名 → 版本。同一任务出现多次时取最后一次见到的版本
        （同一进程内模板不会变，重复只会是同一个值）。
    """
    versions: dict[str, str] = {}
    for invocation in invocations:
        task_name = invocation.get("task_name")
        version = invocation.get("prompt_version")
        if isinstance(task_name, str) and isinstance(version, str):
            versions[task_name] = version
    return versions


def build_manifest(
    *,
    cases: Sequence[GoldenCase],
    execution_mode: str,
    prompt_versions: Mapping[str, str],
    provider_name: str,
    model_id: str,
    provider_parameters: Mapping[str, object],
    storage: StorageIdentity,
    repo_root: Path | None = None,
) -> ReproducibilityManifest:
    """把各部分身份组装成一份清单。

    ⚠️ ``execution_mode`` 收的是**字符串**而不是 ``ExecutionMode`` 枚举：
    枚举定义在 ``runner`` 里，而 ``runner`` 要反过来导入本模块（结果要带上
    清单）。收字符串就把这条环断开了。

    Args:
        cases: 本次运行的案例（用于数据集摘要与计数）。
        execution_mode: 执行路径（``ExecutionMode`` 的值）。
        prompt_versions: **实际使用的** Prompt 版本。
        provider_name: 装配出来的 Provider 名。
        model_id: 模型标识。
        provider_parameters: 白名单参数。
        storage: 存储身份。
        repo_root: 仓库根；``None`` 时自动推断。

    Returns:
        完整清单。
    """
    return ReproducibilityManifest(
        code=collect_code_identity(repo_root),
        evaluation=EvaluationIdentity(
            case_schema_version=CASE_SCHEMA_VERSION,
            dataset_digest=dataset_digest(cases),
            dataset_case_count=len(cases),
            assertion_registry_digest=registry_digest(),
            execution_mode=execution_mode,
        ),
        prompts=prompt_identity(prompt_versions),
        provider=collect_provider_identity(
            provider_name=provider_name,
            model_id=model_id,
            parameters=provider_parameters,
        ),
        runtime=collect_runtime_identity(),
        storage=storage,
    )
