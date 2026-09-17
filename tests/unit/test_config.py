"""配置系统的单元测试。

🔴 重点：**密钥只允许通过环境变量注入，且绝不出现在任何可打印输出中。**
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from ai_psi.config import Environment, Settings

pytestmark = pytest.mark.unit


def settings(**overrides: Any) -> Settings:
    """构造一个**绕过 ``.env``** 的配置对象。

    测试不依赖开发机上的 ``.env`` 文件——否则同样的代码在不同机器上
    会得到不同结果，测试也就不再可复现。
    """
    return Settings(_env_file=None, **overrides)


class TestDefaults:
    def test_default_environment_is_development(self) -> None:
        assert settings().env is Environment.DEVELOPMENT

    def test_default_provider_is_mock(self) -> None:
        """真实 Provider 在阶段 4 才实现——默认必须是 Mock。"""
        assert settings().llm_provider == "mock"

    def test_default_does_not_log_user_content(self) -> None:
        """🔴 任务书 §17.1：日志默认脱敏。"""
        assert settings().log_include_user_content is False

    def test_budget_defaults_match_task_book(self) -> None:
        s = settings()
        assert s.budget_max_model_calls == 12
        assert s.budget_max_metacognitive_loops == 2
        assert s.budget_max_hypotheses == 4
        assert s.budget_max_retrieved_memories == 20
        assert s.budget_max_context_tokens == 32_000
        assert s.budget_max_duration_seconds == 120

    def test_secrets_default_to_none(self) -> None:
        s = settings()
        assert s.anthropic_api_key is None
        assert s.openai_api_key is None


class TestEnvOverride:
    def test_env_var_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AI_PSI_LOG_LEVEL", "debug")
        assert settings().log_level == "DEBUG"

    def test_prefix_is_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """不带前缀的环境变量不得影响配置。"""
        monkeypatch.setenv("LOG_LEVEL", "ERROR")
        assert settings().log_level == "INFO"

    def test_database_url_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AI_PSI_DATABASE_URL", "postgresql+psycopg://u:p@db:5432/other")
        assert settings().database_url.endswith("/other")

    def test_unrelated_env_vars_are_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """compose 用的 POSTGRES_* 变量不应污染配置。"""
        monkeypatch.setenv("POSTGRES_USER", "someone")
        assert settings().env is Environment.DEVELOPMENT


class TestValidation:
    def test_invalid_log_level_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="log_level"):
            settings(log_level="LOUD")

    def test_log_level_is_normalized_to_upper(self) -> None:
        assert settings(log_level="warning").log_level == "WARNING"

    def test_sqlite_url_is_rejected(self) -> None:
        """🔴 V0.1 不支持 SQLite——避免行为差异（ADR-0007）。"""
        with pytest.raises(ValidationError, match="PostgreSQL"):
            settings(database_url="sqlite:///./dev.db")

    def test_mysql_url_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            settings(database_url="mysql://u@localhost/db")

    def test_budget_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            settings(budget_max_model_calls=0)

    def test_timeout_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            settings(llm_timeout_seconds=0)


class TestDerivedViews:
    def test_psycopg_dsn_strips_sqlalchemy_dialect(self) -> None:
        s = settings(database_url="postgresql+psycopg://u:p@h:5432/db")
        assert s.psycopg_dsn == "postgresql://u:p@h:5432/db"

    def test_is_production(self) -> None:
        assert settings(env="production").is_production
        assert not settings().is_production


class TestSecretRedaction:
    """🔴 密钥绝不出现在任何可打印输出中。"""

    def test_api_keys_are_secret_str(self) -> None:
        s = settings(anthropic_api_key="sk-ant-secret-value")
        assert isinstance(s.anthropic_api_key, SecretStr)

    def test_secret_is_not_in_repr(self) -> None:
        s = settings(anthropic_api_key="sk-ant-secret-value")
        assert "sk-ant-secret-value" not in repr(s)

    def test_redacted_summary_masks_secrets(self) -> None:
        s = settings(
            anthropic_api_key="sk-ant-secret-value",
            openai_api_key="sk-openai-secret-value",
        )
        summary = s.redacted_summary()
        rendered = str(summary)

        assert "sk-ant-secret-value" not in rendered
        assert "sk-openai-secret-value" not in rendered
        assert summary["anthropic_api_key"] == "***"
        assert summary["openai_api_key"] == "***"

    def test_redacted_summary_masks_database_url(self) -> None:
        """连接串含密码，不得出现在日志中。"""
        summary = settings().redacted_summary()
        assert summary["database_url"] == "***"
        assert "ai_psi_dev_pw" not in str(summary)

    def test_redacted_summary_keeps_non_secret_fields(self) -> None:
        summary = settings().redacted_summary()
        assert summary["env"] == "development"
        assert summary["llm_provider"] == "mock"

    def test_absent_secrets_render_as_none_not_masked(self) -> None:
        """None 与「已设置」应当可区分。"""
        assert settings().redacted_summary()["anthropic_api_key"] is None


class TestDerivedBudgetConfig:
    def test_budget_values_are_ints(self) -> None:
        s = settings()
        assert isinstance(s.budget_max_model_calls, int)
        assert isinstance(s.budget_max_context_tokens, int)

    def test_llm_settings_are_carried(self) -> None:
        s = settings(llm_provider="anthropic", llm_model="claude-opus-5", llm_max_retries=3)
        assert s.llm_provider == "anthropic"
        assert s.llm_model == "claude-opus-5"
        assert s.llm_max_retries == 3

    def test_negative_retries_rejected(self) -> None:
        with pytest.raises(ValidationError):
            settings(llm_max_retries=-1)


class TestTestDatabaseResolution:
    """🔴 集成测试会清空数据表——测试库与开发库**必须**不同。"""

    def test_derives_test_database_by_suffix(self) -> None:
        s = settings(database_url="postgresql+psycopg://u:p@h:5432/ai_psi")
        assert s.resolved_test_database_url().endswith("/ai_psi_test")

    def test_explicit_url_wins(self) -> None:
        s = settings(test_database_url="postgresql+psycopg://u:p@h:5432/custom_test")
        assert s.resolved_test_database_url().endswith("/custom_test")

    def test_identical_urls_are_rejected(self) -> None:
        """派生结果与开发库相同 → 主动报错，而不是让测试去清空开发数据。"""
        from ai_psi.domain.exceptions import ConfigurationError

        same = "postgresql+psycopg://u:p@h:5432/ai_psi"
        with pytest.raises(ConfigurationError, match="必须指向不同的数据库"):
            settings(database_url=same, test_database_url=same).resolved_test_database_url()

    def test_names_are_exposed_without_credentials(self) -> None:
        s = settings(database_url="postgresql+psycopg://u:secret@h:5432/ai_psi")
        assert s.database_name() == "ai_psi"
        assert s.test_database_name() == "ai_psi_test"
        assert "secret" not in s.database_name()


class TestCaching:
    def test_get_settings_is_cached(self) -> None:
        from ai_psi.config import get_settings, reset_settings_cache

        reset_settings_cache()
        assert get_settings() is get_settings()

    def test_reset_clears_cache(self) -> None:
        from ai_psi.config import get_settings, reset_settings_cache

        reset_settings_cache()
        first = get_settings()
        reset_settings_cache()
        assert get_settings() is not first
