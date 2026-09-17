"""AI-PSI 配置系统。

设计约束（任务书 §17.1）：

* 密钥只允许通过环境变量注入，禁止写入代码或配置文件；
* 密钥类型一律使用 :class:`~pydantic.SecretStr`，避免任何 `repr` / 日志
  意外泄漏明文；
* 配置对象必须提供 :meth:`Settings.redacted_summary`，供启动时安全打印。

本模块**不含任何 IO 副作用**（不建连接、不起服务），只做配置解析与校验。
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Environment", "Settings", "get_settings", "reset_settings_cache"]


class Environment(StrEnum):
    """运行环境。"""

    DEVELOPMENT = "development"
    TESTING = "testing"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """AI-PSI 运行时配置。

    所有字段均可通过 ``AI_PSI_`` 前缀的环境变量覆盖，或写在项目根目录的
    ``.env`` 中（``.env`` 已被 git 忽略）。
    """

    model_config = SettingsConfigDict(
        env_prefix="AI_PSI_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # 运行时
    # ------------------------------------------------------------------
    env: Environment = Environment.DEVELOPMENT
    debug: bool = False
    log_level: str = "INFO"

    #: 结构化日志是否包含用户正文。默认 ``False`` —— 任务书 §17.1 日志脱敏要求。
    log_include_user_content: bool = False

    # ------------------------------------------------------------------
    # 数据库
    # ------------------------------------------------------------------
    #: SQLAlchemy 风格的连接串。阶段 1 仅用于连通性校验，ORM 在阶段 2 引入。
    database_url: str = Field(
        default="postgresql+psycopg://ai_psi:ai_psi_dev_pw@localhost:55432/ai_psi",
        description="PostgreSQL 连接串（含凭证，禁止写入日志）",
    )

    # ------------------------------------------------------------------
    # 认知预算默认值（任务书 §13.1；按深度等级覆盖表见 ADR-0008）
    # ------------------------------------------------------------------
    budget_max_model_calls: int = Field(default=12, ge=1)
    budget_max_metacognitive_loops: int = Field(default=2, ge=0)
    budget_max_hypotheses: int = Field(default=4, ge=1)
    budget_max_retrieved_memories: int = Field(default=20, ge=0)
    budget_max_context_tokens: int = Field(default=32_000, ge=1)
    budget_max_duration_seconds: int = Field(default=120, ge=1)

    # ------------------------------------------------------------------
    # LLM Provider（阶段 4 才实现真实 Provider，此处仅保留配置位）
    # ------------------------------------------------------------------
    llm_provider: str = "mock"
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)
    llm_model: str | None = None

    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        normalized = value.upper()
        if normalized not in allowed:
            msg = f"log_level 必须是 {sorted(allowed)} 之一，收到 {value!r}"
            raise ValueError(msg)
        return normalized

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        if not value.startswith(("postgresql://", "postgresql+psycopg://")):
            msg = "database_url 必须是 PostgreSQL 连接串（V0.1 不支持 SQLite，见 ADR-0001）"
            raise ValueError(msg)
        return value

    # ------------------------------------------------------------------
    # 派生视图
    # ------------------------------------------------------------------
    @property
    def psycopg_dsn(self) -> str:
        """转换为 psycopg 直接可用的 DSN（去掉 SQLAlchemy 方言后缀）。"""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    @property
    def is_production(self) -> bool:
        return self.env is Environment.PRODUCTION

    def redacted_summary(self) -> dict[str, Any]:
        """返回可安全写入日志的配置摘要。

        任何凭证字段一律以 ``"***"`` 或 ``None`` 呈现，绝不输出明文。
        """
        return {
            "env": self.env.value,
            "debug": self.debug,
            "log_level": self.log_level,
            "log_include_user_content": self.log_include_user_content,
            # 连接串本身含密码，只暴露主机/库名的存在性，不暴露内容
            "database_url": "***" if self.database_url else None,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "anthropic_api_key": "***" if self.anthropic_api_key else None,
            "openai_api_key": "***" if self.openai_api_key else None,
            "openai_base_url": self.openai_base_url,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """返回进程内单例配置。

    使用 :func:`reset_settings_cache` 可在测试中重载环境变量后重新读取。
    """
    return Settings()


def reset_settings_cache() -> None:
    """清空配置缓存（仅供测试与运行时热重载使用）。"""
    get_settings.cache_clear()
