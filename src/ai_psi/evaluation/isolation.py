"""评测存储隔离：结构化 URL、数据库名守卫与 fail-closed 预检（阶段 7 · S2）。

## 为什么需要这一层

S1a 的隔离靠"存储后端是内存"这一条：进程结束，一切消失，没有库可污染。
S2 要跑真实 PostgreSQL，于是隔离不能再靠"根本没碰数据库"，只能靠
**两个不同的、可丢弃的数据库**：

* **参考库（reference）** —— 代表必须保持不变的既有状态。预置哨兵数据，
  评测前后逐字段比对；
* **评测库（evaluation）** —— 只接收本次评测的写入。

## fail closed 的含义

本模块的所有检查都在**第一个案例执行之前**跑完，且**没有任何回落路径**：
不回落内存、不回落 SQLite、不回落默认开发库、不回落参考库。
任何一条不过就抛 :class:`IsolationError`，调用方停止执行。

🔴 **回落的危险在于它是静默的。** 一个"评测库连不上就退回开发库"的兜底，
会让评测看起来跑得好好的，而它刚刚写进了真实数据。宁可整套评测不跑。

## 凭据处理

:class:`DatabaseIdentity` 的 ``repr`` 是脱敏的（用户名与密码都不出现），
且凭据字段全部标了 ``repr=False``。异常消息、日志与报告一律只经
:attr:`DatabaseIdentity.redacted` 或 :attr:`DatabaseIdentity.database` 输出——
前者连用户名都不出现，后者只有库名。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from ai_psi.domain.exceptions import ConfigurationError

__all__ = [
    "EVALUATION_DATABASE_SUFFIX",
    "FORBIDDEN_DATABASE_NAMES",
    "REFERENCE_DATABASE_SUFFIX",
    "DatabaseIdentity",
    "DatabaseRole",
    "IsolationError",
    "IsolationPlan",
    "derive_database_url",
    "parse_database_url",
    "plan_isolation",
    "require_mock_provider",
]

#: 任何情况下都不允许作为评测目标或参考目标的库名。
#:
#: * ``postgres`` —— 管理库。连上它意味着"连错了一条库"，
#:   而它恰是建库流程自己要用的那个库；
#: * ``template0`` / ``template1`` —— 模板库。**写入它们会破坏建库能力**，
#:   且 PostgreSQL 默认禁止连接 ``template0``。
#:
#: ⚠️ **开发库名不在这里**：它取决于配置（``AI_PSI_DATABASE_URL``），
#: 不能在源码里硬编码一个"本机的库名"。它以参数形式参与检查，
#: 见 :func:`plan_isolation`。
FORBIDDEN_DATABASE_NAMES: Final[frozenset[str]] = frozenset({"postgres", "template0", "template1"})

#: 评测库名必须以此结尾。它是"这个库可丢弃"的**机器可验证**标记——
#: 清理逻辑据此判断自己删的是不是专用临时库。
EVALUATION_DATABASE_SUFFIX: Final[str] = "_eval_test"

#: 参考库名必须以此结尾。
REFERENCE_DATABASE_SUFFIX: Final[str] = "_reference_test"

#: 派生前先剥掉的测试后缀（ADR-0014 的测试库命名规则）。
_TEST_SUFFIX: Final[str] = "_test"

#: PostgreSQL 默认端口。归一化端口是"两条书写不同的 URL 指向同一个库"
#: 能被识别出来的前提之一（``host:5432`` 与 ``host`` 是同一个库）。
_DEFAULT_PORT: Final[int] = 5432

#: 解析后统一使用的方言。``postgresql://`` 与 ``postgres://`` 都是它的同义写法，
#: 但只有带 ``+psycopg`` 的那条能被 SQLAlchemy 的异步引擎直接使用。
_CANONICAL_SCHEME: Final[str] = "postgresql+psycopg"

_ACCEPTED_SCHEMES: Final[frozenset[str]] = frozenset(
    {"postgresql", "postgresql+psycopg", "postgres"}
)

#: 评测唯一允许的 Provider。
_MOCK_PROVIDER: Final[str] = "mock"


class IsolationError(ConfigurationError):
    """评测隔离预检失败。

    🔴 **消息里不得出现密码或用户名。** 所有涉及连接串的部分都经
    :attr:`DatabaseIdentity.redacted` 输出。异常会被打印到终端、写进日志、
    粘进 issue——这三处都不该看到凭据。
    """

    default_code = "evaluation_isolation_error"


class DatabaseRole(StrEnum):
    """两个专用数据库的角色。"""

    REFERENCE = "reference"
    EVALUATION = "evaluation"


_ROLE_SUFFIX: Final[dict[DatabaseRole, str]] = {
    DatabaseRole.REFERENCE: REFERENCE_DATABASE_SUFFIX,
    DatabaseRole.EVALUATION: EVALUATION_DATABASE_SUFFIX,
}

_ROLE_LABEL: Final[dict[DatabaseRole, str]] = {
    DatabaseRole.REFERENCE: "参考库（reference）",
    DatabaseRole.EVALUATION: "评测库（evaluation）",
}


@dataclass(frozen=True, slots=True, repr=False)
class DatabaseIdentity:
    """一个连接串的**结构化**身份。

    刻意不保存"原始字符串"：那样任何一次 ``repr``、日志插值或
    ``f"{url}"`` 都会泄漏密码。重建连接串所需的字段都在这里，而
    :meth:`with_database` 用 :func:`~urllib.parse.urlunsplit` 重建——
    **不是对整串做 ``replace``**（后者会把密码里恰好出现的子串一起换掉）。
    """

    #: 规范化后的方言（恒为 ``postgresql+psycopg``）。
    scheme: str
    #: 主机名，已小写化。``LOCALHOST`` 与 ``localhost`` 是同一台机器。
    host: str
    #: 端口，已归一化到默认值。
    port: int
    #: 数据库名。
    database: str
    #: 原始 netloc。**含凭据**，因此不进 repr。
    netloc: str = field(repr=False)
    #: 用户名。不进 repr，也不进任何错误消息。
    username: str = field(repr=False, default="")
    #: 密码。同上。
    password: str = field(repr=False, default="")
    #: 原始 query string（如连接参数）。
    query: str = ""

    def __repr__(self) -> str:
        """脱敏表示。**默认的 dataclass repr 会打印密码，因此这里自己写。**"""
        return f"DatabaseIdentity({self.redacted!r})"

    @property
    def endpoint(self) -> tuple[str, int, str]:
        """身份键：``(主机, 端口, 库名)``——**不含用户名与密码**。

        🔴 凭据不能并进身份键。同一台服务器上的同一个库，换一个账号连过去
        还是同一个库；把账号算进身份，"两条 URL 指向同一个数据库"这件事
        就会被账号差异掩盖，而守卫恰恰要在那时报错。
        """
        return (self.host, self.port, self.database)

    @property
    def redacted(self) -> str:
        """可安全打印的形式：用户名与密码都不出现。"""
        return f"{self.scheme}://***@{self.host}:{self.port}/{self.database}"

    @property
    def normalized_url(self) -> str:
        """规范化方言后的**完整**连接串（含凭据）。

        🔴 只应交给连接层，绝不可打印、绝不写进报告。
        """
        return urlunsplit((self.scheme, self.netloc, f"/{self.database}", self.query, ""))

    def with_database(self, name: str) -> str:
        """换一个库名，其余部分原样保留。

        Args:
            name: 新的数据库名。

        Returns:
            重建后的连接串（**含凭据**，只应交给连接层）。
        """
        return urlunsplit((self.scheme, self.netloc, f"/{name}", self.query, ""))


def parse_database_url(url: str, *, role: DatabaseRole | None = None) -> DatabaseIdentity:
    """把一个连接串解析成结构化身份。

    Args:
        url: SQLAlchemy 风格的 PostgreSQL 连接串。
        role: 出错时用于定位的角色名；``None`` 时用中性说法。

    Returns:
        结构化身份。

    Raises:
        IsolationError: 不是 PostgreSQL、缺主机或库名、端口非法、串为空。
    """
    label = "数据库" if role is None else _ROLE_LABEL[role]
    if not url or not url.strip():
        raise IsolationError(f"{label}的连接串为空")

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _ACCEPTED_SCHEMES:
        raise IsolationError(
            f"{label}必须是 PostgreSQL 连接串（V0.1 不支持其他后端，见 ADR-0001），"
            f"收到的 scheme 是 {scheme!r}"
        )

    # ``parts.hostname`` 已经是小写形式。
    host = parts.hostname or ""
    if not host:
        raise IsolationError(f"{label}的连接串缺少主机名")

    try:
        port = parts.port or _DEFAULT_PORT
    except ValueError:
        # ⚠️ 不把原串带进消息——它含密码。
        raise IsolationError(f"{label}的连接串端口不是合法数字") from None

    database = parts.path.lstrip("/")
    if not database:
        raise IsolationError(f"{label}的连接串缺少数据库名")

    return DatabaseIdentity(
        scheme=_CANONICAL_SCHEME,
        host=host,
        port=port,
        database=database,
        netloc=parts.netloc,
        username=parts.username or "",
        password=parts.password or "",
        query=parts.query,
    )


def derive_database_url(base_url: str, role: DatabaseRole) -> str:
    """从基准连接串派生某个角色的专用数据库连接串。

    基准通常是开发库（``ai_psi``）或测试库（``ai_psi_test``）；
    两者都会先剥掉 ``_test``，再拼上角色后缀，得到
    ``ai_psi_reference_test`` / ``ai_psi_eval_test``。

    🔴 派生是**结构化**的：解析成身份、换库名、再重建，
    而不是对整串做 ``replace``。

    Args:
        base_url: 基准连接串。
        role: 要派生的角色。

    Returns:
        派生出的连接串（**含凭据**）。

    Raises:
        IsolationError: 基准串非法，或库名剥掉 ``_test`` 后为空。
    """
    base = parse_database_url(base_url)
    stem = base.database
    if stem.endswith(_TEST_SUFFIX):
        stem = stem[: -len(_TEST_SUFFIX)]
    if not stem:
        raise IsolationError("基准连接串的库名在剥掉测试后缀后为空，无法派生专用库名")
    return base.with_database(f"{stem}{_ROLE_SUFFIX[role]}")


def require_mock_provider(provider: str) -> None:
    """🔴 评测只允许 Mock Provider。

    live Provider 会让评测结果依赖供应商状态：同样的数据集两次运行会得到
    不同的观测，而"canonical JSON 逐字节一致"这条承诺当场失效。

    Args:
        provider: 配置或装配结果里的 provider 名。

    Raises:
        IsolationError: 不是 ``mock``。
    """
    normalized = provider.strip().lower()
    if normalized != _MOCK_PROVIDER:
        raise IsolationError(
            f"评测只允许 {_MOCK_PROVIDER!r} Provider，收到 {provider!r}；"
            "真实 Provider 的评测属于后续切片"
        )


@dataclass(frozen=True, slots=True)
class IsolationPlan:
    """通过全部预检后确定的两个数据库目标。"""

    reference: DatabaseIdentity
    evaluation: DatabaseIdentity

    @property
    def same_database(self) -> bool:
        """两条 URL 是否指向同一个数据库。**通过预检后恒为 ``False``。**"""
        return self.reference.endpoint == self.evaluation.endpoint

    def summary(self) -> dict[str, object]:
        """可写进**原始**报告的最小隔离证据。

        🔴 只有库名，没有完整 URL、用户名或密码。
        """
        return {
            "evaluation_database": self.evaluation.database,
            "reference_database": self.reference.database,
            "same_database": self.same_database,
        }


def plan_isolation(
    *,
    evaluation_database_url: str | None,
    reference_database_url: str | None,
    development_database_url: str,
    provider: str,
) -> IsolationPlan:
    """跑完全部 fail-closed 预检，返回两个数据库目标。

    🔴 **这是"在第一个案例执行之前失败"的落点。** 调用方必须先调它、
    拿到 :class:`IsolationPlan`，然后才允许建库、迁移、发 HTTP 请求。

    检查顺序（任一不过立即抛，不做任何回落）：

    1. Provider 必须是 Mock；
    2. 评测库 URL 必须显式提供；
    3. 参考库 URL 必须显式提供；
    4. 三条 URL 都能解析成 PostgreSQL 身份；
    5. 两个库不是同一个数据库（按 ``(主机, 端口, 库名)`` 比较）；
    6. 评测库不是开发库；
    7. 参考库名安全（非系统库、非开发库、带 ``_reference_test`` 后缀）；
    8. 评测库名安全（非系统库、非开发库、带 ``_eval_test`` 后缀）。

    🔴 **第 5、6 条必须排在第 7、8 条之前**，否则它们是死代码：
    "两个 URL 指向同一个数据库"意味着库名相同，而库名相同就至少有一个
    过不了后缀检查——每次都先被后缀规则拦下，"这其实是同一个库"
    这个**最危险的**配错反而永远不会被报出来。

    Args:
        evaluation_database_url: 专用评测库连接串；``None`` 表示未提供。
        reference_database_url: 专用参考库连接串；``None`` 表示未提供。
        development_database_url: 开发库连接串，用于"不得指向开发库"的检查。
        provider: 本次评测将要使用的 Provider 名。

    Returns:
        两个库的结构化身份。

    Raises:
        IsolationError: 任一条检查不过。消息**不含**用户名与密码。
    """
    require_mock_provider(provider)

    if not evaluation_database_url:
        raise IsolationError(
            "缺少专用评测库连接串（--evaluation-database-url）。"
            "评测**绝不**从 AI_PSI_DATABASE_URL 或环境里猜测目标库——"
            "猜错一次的代价是往真实数据里写评测回合"
        )
    if not reference_database_url:
        raise IsolationError(
            "缺少专用参考库连接串（--reference-database-url）。没有它就无法证明评测没有改动既有状态"
        )

    development = parse_database_url(development_database_url)
    reference = parse_database_url(reference_database_url, role=DatabaseRole.REFERENCE)
    evaluation = parse_database_url(evaluation_database_url, role=DatabaseRole.EVALUATION)

    # 🔴 身份比较排在后缀检查之前，理由见 docstring 第 4 段。
    if reference.endpoint == evaluation.endpoint:
        raise IsolationError(
            f"评测库与参考库指向同一个数据库（{evaluation.redacted}）。"
            "两者必须是不同的库，否则评测的写入会直接落进参考状态"
        )
    if evaluation.endpoint == development.endpoint:
        raise IsolationError(
            f"评测库不能是开发库（{evaluation.redacted}）——评测会在库里留下回合、事件与学习状态"
        )

    _require_safe_name(reference, role=DatabaseRole.REFERENCE, development=development)
    _require_safe_name(evaluation, role=DatabaseRole.EVALUATION, development=development)
    return IsolationPlan(reference=reference, evaluation=evaluation)


def _require_safe_name(
    identity: DatabaseIdentity,
    *,
    role: DatabaseRole,
    development: DatabaseIdentity,
) -> None:
    """库名守卫：系统库、开发库、缺专用后缀一律拒绝。

    Args:
        identity: 待检查的身份。
        role: 该身份的角色。
        development: 开发库身份。

    Raises:
        IsolationError: 库名不安全。
    """
    label = _ROLE_LABEL[role]
    name = identity.database

    if name in FORBIDDEN_DATABASE_NAMES:
        raise IsolationError(
            f"{label}不能用系统库名 {name!r}；禁止使用的库名：{sorted(FORBIDDEN_DATABASE_NAMES)}"
        )
    if name == development.database:
        raise IsolationError(f"{label}不能用开发库名 {name!r}")

    suffix = _ROLE_SUFFIX[role]
    if not name.endswith(suffix):
        raise IsolationError(
            f"{label}的库名必须以 {suffix!r} 结尾，收到的却是不含专用标记的名称。"
            "该后缀是「这个库可以被丢弃」的机器可验证标记——"
            "没有它，清理逻辑就不敢删，守卫也认不出专用评测库"
        )
