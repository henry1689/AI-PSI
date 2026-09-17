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

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ai_psi.domain.exceptions import ConfigurationError
from ai_psi.providers.embeddings import (
    AVAILABLE_EMBEDDING_PROVIDERS,
    DEFAULT_EMBEDDING_DIMENSION,
)

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
        # 允许用字段名构造（测试与代码里写 `Settings(deepseek_api_key=...)`），
        # 而不只是走环境变量别名。
        populate_by_name=True,
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
    #: SQLAlchemy 风格的连接串。
    database_url: str = Field(
        default="postgresql+psycopg://ai_psi:ai_psi_dev_pw@localhost:55432/ai_psi",
        description="PostgreSQL 连接串（含凭证，禁止写入日志）",
    )

    #: 集成测试专用连接串。
    #:
    #: 🔴 **集成测试绝不复用开发库。** 测试会 TRUNCATE 表，
    #: 指错一次就是开发数据的静默丢失。未显式配置时，
    #: 由 :meth:`resolved_test_database_url` 从开发库名派生（追加 ``_test``）。
    test_database_url: str | None = Field(
        default=None,
        description="集成测试专用数据库连接串；None 表示按开发库名派生",
    )

    # ⚠️ 认知预算**不在这里**。
    #
    # 阶段 1 曾在此放了一组 `budget_max_*` 字段，但阶段 3 实现后发现：
    # 真正生效的是 `CognitiveBudget.for_depth()` 的**按深度预算表**（ADR-0008），
    # 那组字段没有任何读取方——它们是死配置。
    #
    # 死配置比没有配置更糟：它看起来可调，却不会有任何作用。
    # 因此阶段 3 删除了它们，并在 ADR-0015 中登记。

    #: 存储后端（阶段 3 起）：``postgres`` 或 ``memory``。
    #:
    #: ``memory`` 让整套系统在零外部依赖下完整运行（ADR-0009），
    #: 用于测试与本地演示；``postgres`` 是真实开发环境。
    storage_backend: str = Field(default="postgres")

    # ------------------------------------------------------------------
    # 认知可靠性（阶段 3）
    # ------------------------------------------------------------------
    #: 判定"连续两轮判断重复"的相似度阈值（任务书 §13.3）。
    #:
    #: 🔴 阈值属于配置而非硬编码——反刍的判定标准会随模型与任务变化，
    #: 把它钉死在代码里会让调整必须走发版（ADR-0008 的同一思路）。
    repetition_threshold: float = Field(default=0.85, ge=0.0, le=1.0)

    # ------------------------------------------------------------------
    # LLM Provider（阶段 4：真实 Provider 已接入）
    # ------------------------------------------------------------------
    #: Provider 名称：``mock`` / ``deepseek`` / ``openai_compatible``。
    llm_provider: str = "mock"

    #: 模型标识；``None`` 时按 Provider 取默认值（见 providers/registry.py）。
    llm_model: str | None = None

    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)

    #: 是否启用供应商的 JSON 输出模式。
    #: 不支持 ``response_format`` 的兼容服务器应置为 ``False``——
    #: 那会退化为"只靠提示词约束 + 提取修复"，成功率低一些但仍可用。
    llm_json_mode: bool = True

    #: 给**推理模型**的额外输出预留（token）。
    #:
    #: 推理模型把输出预算的一部分花在内部推理上（DeepSeek 实测：
    #: 只要求回一个词的请求也会烧掉约 50 个推理 token，
    #: 而 ``logical_analyzer`` 单次常达 5–8k）。
    #: 契约里的 ``max_output_tokens`` 描述的是**答案**的长度，
    #: 因此这里额外加上这段预留，否则答案会被推理挤掉后截断。
    #:
    #: 🔴 ``None`` 表示**用 Provider 自己的默认值**
    #: （``providers/openai_compatible.py`` 的 ``DEFAULT_REASONING_HEADROOM``）。
    #: 刻意不给一个具体数字：那样就会出现两个都"看起来权威"的默认值，
    #: 而配置层那个会**静默覆盖** Provider 层——阶段 4 就踩过这个坑，
    #: 表现为"改了 Provider 的默认值却毫无效果"。
    llm_reasoning_headroom_tokens: int | None = Field(default=None, ge=0)

    #: 熔断：连续多少次"供应商不可用"类失败后打开（任务书 §8.1）。
    llm_circuit_failure_threshold: int = Field(default=5, ge=1)

    #: 熔断打开后的冷却秒数；冷却结束后放一个探针请求。
    llm_circuit_recovery_seconds: float = Field(default=30.0, gt=0)

    # 密钥一律只从环境变量注入（任务书 §17.1）。
    #
    # ⚠️ 同时接受**业界通用名**（``DEEPSEEK_API_KEY``）与本项目的
    # ``AI_PSI_`` 前缀名。要求用户为同一个密钥再配一份不必要，
    # 而"密钥只从环境变量来"这条约束并没有因此放松。
    deepseek_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("AI_PSI_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
    )
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com/v1",
        validation_alias=AliasChoices("AI_PSI_DEEPSEEK_BASE_URL", "DEEPSEEK_BASE_URL"),
    )

    openai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("AI_PSI_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    openai_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("AI_PSI_OPENAI_BASE_URL", "OPENAI_BASE_URL"),
    )

    #: ⚠️ Anthropic Provider 在阶段 4 **未实现**（ADR-0016）。
    #: 配置它会在装配时明确报错，而不是静默回落到 Mock。
    anthropic_api_key: SecretStr | None = None

    # ------------------------------------------------------------------
    # 向量（阶段 5：长期记忆检索）
    # ------------------------------------------------------------------
    #: 向量来源：``local`` 或 ``openai_compatible``。
    #:
    #: ``local`` 是**确定性词面向量**（不是语义向量，见
    #: :mod:`ai_psi.providers.embeddings`）：零成本、离线、完全可测。
    #: ``openai_compatible`` 走外部 ``/embeddings`` 接口。
    embedding_provider: str = "local"

    #: 向量维度。🔴 **它是数据库列的固定属性**——
    #: 改这里必须同时改迁移，否则装配时会明确失败。
    embedding_dimension: int = Field(default=DEFAULT_EMBEDDING_DIMENSION, gt=0)

    #: 外部向量模型名（``embedding_provider=openai_compatible`` 时必填）。
    embedding_model: str | None = None

    #: 外部向量服务的基础地址（``.../v1``）。
    embedding_base_url: str | None = None

    #: 外部向量服务的密钥。
    #:
    #: ⚠️ 刻意**不复用** ``openai_api_key``：向量与对话可以是两家不同的服务，
    #: 让一个密钥字段同时代表两件事，会在"只想换向量服务"时被迫改掉对话的密钥。
    embedding_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("AI_PSI_EMBEDDING_API_KEY", "EMBEDDING_API_KEY"),
    )

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

    @field_validator("storage_backend")
    @classmethod
    def _validate_storage_backend(cls, value: str) -> str:
        allowed = {"postgres", "memory"}
        normalized = value.strip().lower()
        if normalized not in allowed:
            msg = f"storage_backend 必须是 {sorted(allowed)} 之一，收到 {value!r}"
            raise ValueError(msg)
        return normalized

    @field_validator("embedding_provider")
    @classmethod
    def _validate_embedding_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in AVAILABLE_EMBEDDING_PROVIDERS:
            msg = (
                f"embedding_provider 必须是 {sorted(AVAILABLE_EMBEDDING_PROVIDERS)} 之一，"
                f"收到 {value!r}"
            )
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

    def resolved_test_database_url(self) -> str:
        """返回集成测试要用的连接串。

        未显式配置 ``test_database_url`` 时，在开发库名后追加 ``_test`` 派生。
        派生的结果**必须与开发库不同**——相同则说明派生逻辑有误，
        这里主动报错而不是让测试去清空开发数据。

        Returns:
            测试库连接串。

        Raises:
            ConfigurationError: 派生结果与开发库相同。
        """
        if self.test_database_url:
            resolved = self.test_database_url
        else:
            base, separator, name = self.database_url.rpartition("/")
            resolved = f"{base}/{name}_test" if separator else f"{self.database_url}_test"

        if resolved == self.database_url:
            msg = "测试库连接串与开发库相同。集成测试会清空数据表，因此两者必须指向不同的数据库"
            raise ConfigurationError(msg)
        return resolved

    def database_name(self) -> str:
        """返回开发库的库名（用于诊断输出，不含凭证）。"""
        return self.database_url.rpartition("/")[2]

    def test_database_name(self) -> str:
        """返回测试库的库名（用于诊断输出，不含凭证）。"""
        return self.resolved_test_database_url().rpartition("/")[2]

    def redacted_summary(self) -> dict[str, Any]:
        """返回可安全写入日志的配置摘要。

        任何凭证字段一律以 ``"***"`` 或 ``None`` 呈现，绝不输出明文。
        """
        return {
            "env": self.env.value,
            "debug": self.debug,
            "log_level": self.log_level,
            "log_include_user_content": self.log_include_user_content,
            "storage_backend": self.storage_backend,
            "repetition_threshold": self.repetition_threshold,
            # 连接串本身含密码，只暴露主机/库名的存在性，不暴露内容
            "database_url": "***" if self.database_url else None,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "llm_json_mode": self.llm_json_mode,
            "llm_max_retries": self.llm_max_retries,
            "llm_reasoning_headroom_tokens": self.llm_reasoning_headroom_tokens,
            "llm_circuit_failure_threshold": self.llm_circuit_failure_threshold,
            # 密钥永远只暴露"有没有配"，不暴露值
            "deepseek_api_key": "***" if self.deepseek_api_key else None,
            "deepseek_base_url": self.deepseek_base_url,
            "anthropic_api_key": "***" if self.anthropic_api_key else None,
            "openai_api_key": "***" if self.openai_api_key else None,
            "openai_base_url": self.openai_base_url,
            "embedding_provider": self.embedding_provider,
            "embedding_dimension": self.embedding_dimension,
            "embedding_model": self.embedding_model,
            "embedding_base_url": self.embedding_base_url,
            "embedding_api_key": "***" if self.embedding_api_key else None,
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
