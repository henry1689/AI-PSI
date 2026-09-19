"""E 组：评测存储隔离的配置与 URL 安全（阶段 7 · S2）。

这一组回答的是：**什么样的配置会被拒绝，以及拒绝的时候会不会顺手把密码
打印出来。** 全部是纯单元测试——不需要 PostgreSQL，因为被检查的正是
"在连上任何数据库之前"的那道门。

🔴 **这一组里没有任何一条测试会真的连库。** 守卫跑在连接之前，
所以"拒绝连接开发库"这条断言的是**拒绝**本身，而不是"连上之后没写"。
"""

from __future__ import annotations

import pytest

from ai_psi.evaluation.isolation import (
    EVALUATION_DATABASE_SUFFIX,
    FORBIDDEN_DATABASE_NAMES,
    REFERENCE_DATABASE_SUFFIX,
    DatabaseRole,
    IsolationError,
    IsolationPlan,
    derive_database_url,
    parse_database_url,
    plan_isolation,
    require_mock_provider,
)
from ai_psi.evaluation.postgres import drop_database

pytestmark = pytest.mark.unit

#: 一个本机开发库连接串。**故意带可识别的用户名与密码**，
#: 好让"凭据有没有泄漏到消息里"这件事有断言可写。
#:
#: ⚠️ 用户名刻意**不是** ``ai_psi``：那样它会是每个库名的子串，
#: ``"ai_psi" not in message`` 这种断言就永远为假，测了个寂寞。
_USER = "psi_runner"
_SECRET = "s3cr3t-dev-pw"
_HOST = "localhost"
_PORT = 55432
_DEVELOPMENT = f"postgresql+psycopg://{_USER}:{_SECRET}@{_HOST}:{_PORT}/ai_psi"


def _url(
    database: str,
    *,
    host: str = _HOST,
    port: int = _PORT,
    scheme: str = "postgresql+psycopg",
) -> str:
    """拼一个连接串。"""
    return f"{scheme}://{_USER}:{_SECRET}@{host}:{port}/{database}"


def _plan(
    evaluation: str | None,
    reference: str | None,
    *,
    provider: str = "mock",
) -> IsolationPlan:
    """跑一次预检。"""
    return plan_isolation(
        evaluation_database_url=evaluation,
        reference_database_url=reference,
        development_database_url=_DEVELOPMENT,
        provider=provider,
    )


class TestUrlParsing:
    """结构化解析：不是 ``replace``，也不猜。"""

    def test_parses_the_pieces(self) -> None:
        identity = parse_database_url(_url("ai_psi_eval_test"))
        assert identity.host == _HOST
        assert identity.port == _PORT
        assert identity.database == "ai_psi_eval_test"
        # scheme 被规范化成异步驱动能用的那一个
        assert identity.scheme == "postgresql+psycopg"

    def test_bare_postgresql_scheme_is_accepted_and_normalized(self) -> None:
        """``postgresql://`` 是合法写法，规范化后才可能被异步引擎使用。"""
        identity = parse_database_url(_url("ai_psi_eval_test", scheme="postgresql"))
        assert identity.scheme == "postgresql+psycopg"

    def test_host_is_lowercased(self) -> None:
        """``LOCALHOST`` 与 ``localhost`` 是同一台机器，必须归一到同一个身份。"""
        assert parse_database_url(_url("x_eval_test", host="LOCALHOST")).host == "localhost"

    def test_missing_port_normalizes_to_the_default(self) -> None:
        """``host:5432`` 与 ``host`` 是同一个库——不归一就看不出这一点。"""
        identity = parse_database_url("postgresql+psycopg://u:p@localhost/ai_psi_eval_test")
        assert identity.port == 5432

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "   ",
            "sqlite:///tmp/ai_psi.db",
            "mysql://u:p@localhost/ai_psi_eval_test",
            "postgresql+psycopg://u:p@localhost:55432",
            "postgresql+psycopg:///ai_psi_eval_test",
        ],
    )
    def test_invalid_urls_are_rejected(self, url: str) -> None:
        """空串、非 PostgreSQL、缺库名、缺主机——一律拒绝，不猜。"""
        with pytest.raises(IsolationError):
            parse_database_url(url)


class TestCredentialRedaction:
    """凭据不得出现在任何会被打印的地方。"""

    def test_redacted_hides_both_username_and_password(self) -> None:
        identity = parse_database_url(_url("ai_psi_eval_test"))
        assert _SECRET not in identity.redacted
        assert _USER not in identity.redacted
        assert "ai_psi_eval_test" in identity.redacted

    def test_repr_hides_credentials(self) -> None:
        """🔴 dataclass 的默认 repr 会打印密码，所以这里必须自己写。"""
        identity = parse_database_url(_url("ai_psi_eval_test"))
        assert _SECRET not in repr(identity)
        assert _USER not in repr(identity)

    def test_f_string_interpolation_hides_credentials(self) -> None:
        """最常见的泄漏路径就是 ``f"{identity}"``。"""
        identity = parse_database_url(_url("ai_psi_eval_test"))
        assert _SECRET not in f"{identity}"

    @pytest.mark.parametrize(
        ("evaluation", "reference"),
        [
            ("", _url("ai_psi_reference_test")),
            (_url("ai_psi_eval_test"), ""),
            (_url("ai_psi"), _url("ai_psi_reference_test")),
            (_url("ai_psi_eval_test"), _url("ai_psi_eval_test")),
            (_url("postgres"), _url("ai_psi_reference_test")),
            (_url("ai_psi_eval_test"), _url("postgres")),
            (_url("no_suffix_at_all"), _url("ai_psi_reference_test")),
        ],
    )
    def test_no_precheck_failure_leaks_the_password(self, evaluation: str, reference: str) -> None:
        """🔴 每一条预检失败路径都要过这一关：消息里没有密码，也没有用户名。

        异常会被打印到终端、写进日志、粘进 issue——这三处都不该看到凭据。
        """
        with pytest.raises(IsolationError) as excinfo:
            _plan(evaluation, reference)
        message = str(excinfo.value)
        assert _SECRET not in message
        assert _USER not in message


class TestDerivation:
    """专用库名的派生。"""

    def test_derives_from_a_development_name(self) -> None:
        assert derive_database_url(_DEVELOPMENT, DatabaseRole.EVALUATION).endswith(
            f"/ai_psi{EVALUATION_DATABASE_SUFFIX}"
        )
        assert derive_database_url(_DEVELOPMENT, DatabaseRole.REFERENCE).endswith(
            f"/ai_psi{REFERENCE_DATABASE_SUFFIX}"
        )

    def test_strips_the_test_suffix_first(self) -> None:
        """从测试库派生时不该得到 ``ai_psi_test_eval_test``。"""
        derived = derive_database_url(_url("ai_psi_test"), DatabaseRole.EVALUATION)
        assert derived.endswith(f"/ai_psi{EVALUATION_DATABASE_SUFFIX}")
        assert "_test_test" not in derived

    def test_password_containing_the_database_name_is_untouched(self) -> None:
        """🔴 这就是"不能用 ``replace`` 派生"的理由。

        密码里恰好出现库名时，字符串替换会把它一起改掉：
        派生结果看起来对，连过去却认证失败。
        """
        url = "postgresql+psycopg://u:ai_psi_secret@localhost:55432/ai_psi"
        assert "ai_psi_secret" in derive_database_url(url, DatabaseRole.EVALUATION)

    def test_the_two_roles_never_collide(self) -> None:
        evaluation = derive_database_url(_DEVELOPMENT, DatabaseRole.EVALUATION)
        reference = derive_database_url(_DEVELOPMENT, DatabaseRole.REFERENCE)
        assert evaluation != reference


class TestPlanIsolation:
    """fail-closed 预检：任一条件不满足都必须拒绝。"""

    def test_a_safe_pair_passes(self) -> None:
        plan = _plan(_url("ai_psi_eval_test"), _url("ai_psi_reference_test"))
        assert plan.same_database is False
        assert plan.evaluation.database == "ai_psi_eval_test"

    def test_missing_evaluation_url_is_rejected(self) -> None:
        with pytest.raises(IsolationError, match="评测库"):
            _plan(None, _url("ai_psi_reference_test"))

    def test_missing_reference_url_is_rejected(self) -> None:
        with pytest.raises(IsolationError, match="参考库"):
            _plan(_url("ai_psi_eval_test"), None)

    def test_identical_urls_are_rejected(self) -> None:
        same = _url("ai_psi_eval_test")
        with pytest.raises(IsolationError, match="同一个数据库"):
            _plan(same, same)

    def test_differently_written_urls_for_the_same_database_are_rejected(self) -> None:
        """🔴 字符串比较挡不住这一条，只有**结构化身份**比较能。

        两条 URL 写法完全不同——不同 scheme、不同大小写、端口一个显式一个
        默认——但它们指向的是同一个数据库。按字符串比会放行，
        评测于是写进参考库。
        """
        evaluation = "postgresql+psycopg://u:p@localhost:5432/ai_psi_same_test"
        reference = "postgresql://u:p@LOCALHOST/ai_psi_same_test"
        with pytest.raises(IsolationError, match="同一个数据库"):
            _plan(evaluation, reference)

    def test_evaluation_pointing_at_the_development_database_is_rejected(self) -> None:
        with pytest.raises(IsolationError):
            _plan(_DEVELOPMENT, _url("ai_psi_reference_test"))

    @pytest.mark.parametrize("name", sorted(FORBIDDEN_DATABASE_NAMES))
    def test_system_database_names_are_rejected(self, name: str) -> None:
        """``postgres`` 是管理库，``template0`` / ``template1`` 是模板库。"""
        with pytest.raises(IsolationError, match="系统库名"):
            _plan(_url(name), _url("ai_psi_reference_test"))

    def test_evaluation_name_without_the_dedicated_marker_is_rejected(self) -> None:
        """``ai_psi_test`` 是**普通测试库**，不是专用评测库。"""
        with pytest.raises(IsolationError, match="专用标记"):
            _plan(_url("ai_psi_test"), _url("ai_psi_reference_test"))

    def test_reference_name_without_the_dedicated_marker_is_rejected(self) -> None:
        with pytest.raises(IsolationError, match="专用标记"):
            _plan(_url("ai_psi_eval_test"), _url("ai_psi_test"))

    @pytest.mark.parametrize(
        "provider", ["", "deepseek", "openai_compatible", "MOCK!", "anthropic"]
    )
    def test_non_mock_providers_are_rejected(self, provider: str) -> None:
        """🔴 live Provider 会让"两次运行逐字节一致"当场失效。"""
        with pytest.raises(IsolationError, match="Provider"):
            _plan(_url("ai_psi_eval_test"), _url("ai_psi_reference_test"), provider=provider)

    def test_mock_provider_is_accepted_case_insensitively(self) -> None:
        require_mock_provider("MOCK")
        require_mock_provider(" mock ")

    def test_summary_carries_no_credentials(self) -> None:
        """写进报告的那份摘要只有库名与布尔。"""
        plan = _plan(_url("ai_psi_eval_test"), _url("ai_psi_reference_test"))
        rendered = repr(plan.summary())
        assert _SECRET not in rendered
        assert _USER not in rendered


class TestCleanupGuard:
    """删除临时库前的二次名称守卫。

    ⚠️ 这些用例**不会真的删库**：守卫跑在 ``asyncio.to_thread`` 之前，
    名字不安全时直接抛异常，连接根本不会建立。
    """

    async def test_refuses_to_drop_the_development_database(self) -> None:
        identity = parse_database_url(_DEVELOPMENT)
        with pytest.raises(IsolationError, match="开发库"):
            await drop_database(identity, development=identity)

    @pytest.mark.parametrize("name", sorted(FORBIDDEN_DATABASE_NAMES))
    async def test_refuses_to_drop_a_system_database(self, name: str) -> None:
        development = parse_database_url(_DEVELOPMENT)
        identity = parse_database_url(_url(name))
        with pytest.raises(IsolationError, match="系统库"):
            await drop_database(identity, development=development)

    @pytest.mark.parametrize("name", ["ai_psi-eval", "ai_psi.eval.test", "ai_psi%22eval"])
    async def test_refuses_names_with_unsafe_characters(self, name: str) -> None:
        """🔴 库名会被拼进 ``DROP DATABASE "..."``；字符集守卫是第二道锁。

        后缀守卫保证的是**结尾**，它不限制前缀里能出现什么。
        """
        development = parse_database_url(_DEVELOPMENT)
        identity = parse_database_url(_url(name))
        with pytest.raises(IsolationError, match="不允许的字符"):
            await drop_database(identity, development=development)
